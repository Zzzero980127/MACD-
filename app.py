import os
import re
import requests
import pandas as pd
import datetime
import psycopg2
import threading
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# LINE Bot 金鑰
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 全域動態股票名稱與代號對照字典
STOCK_NAME_TO_ID = {}
STOCK_ID_TO_NAME = {}

def update_stock_symbol_map():
    """ 自動向證交所 (TWSE) 與櫃買中心 (TPEx) 動態抓取『全市場上市櫃股票名稱與代號』 """
    global STOCK_NAME_TO_ID, STOCK_ID_TO_NAME
    print("🔄 正在動態更新全台股名稱與代號對照表...", flush=True)
    temp_name_map = {}
    temp_id_map = {}

    try:
        # 1. 抓取上市股票 (TWSE)
        twse_url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res_twse = requests.get(twse_url, timeout=10)
        if res_twse.status_code == 200:
            for item in res_twse.json():
                code = item.get("Code", "").strip()
                name = item.get("Name", "").strip()
                if len(code) == 4 and code.isdigit() and name:
                    temp_name_map[name] = code
                    temp_id_map[code] = name

        # 2. 抓取上櫃股票 (TPEx)
        tpex_url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes"
        res_tpex = requests.get(tpex_url, timeout=10)
        if res_tpex.status_code == 200:
            for item in res_tpex.json():
                code = item.get("SecuritiesCompanyCode", "").strip()
                name = item.get("CompanyName", "").strip()
                if len(code) == 4 and code.isdigit() and name:
                    temp_name_map[name] = code
                    temp_id_map[code] = name

        if temp_name_map:
            STOCK_NAME_TO_ID = temp_name_map
            STOCK_ID_TO_NAME = temp_id_map
            print(f"✅ 台股對照表更新完成！成功載入 {len(STOCK_NAME_TO_ID)} 檔上市櫃股票。", flush=True)
    except Exception as e:
        print(f"⚠️ 動態抓取證交所名單失敗: {e}", flush=True)

# 伺服器啟動時執行一次名單更新
update_stock_symbol_map()

def get_db_connection():
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception: return None

def get_latest_report_from_db():
    """ 從 PostgreSQL 撈取 cron_job.py 計算好的最新 AI 選股報告 """
    conn = get_db_connection()
    if not conn:
        return "⚠️ 資料庫連線失敗，請稍後再試。"
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT content FROM history WHERE date = 'LATEST';")
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row and row[0]:
            return row[0]
        else:
            return "📅 目前尚無 AI 選股報告，請等待每日盤後自動計算。"
    except Exception as e:
        return f"⚠️ 讀取選股報告失敗: {e}"

def fetch_single_stock_price_public(stock_id):
    """ 【個股查詢專用】100% 走無 Token 公用通道，絕不佔用選股 Token 配額 """
    start_date = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
    try:
        res = requests.get(url, timeout=5.0)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
            df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
            df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
            df = df.dropna(subset=['Close'])
            if len(df) >= 35: return df
    except Exception: pass
    return None

def fetch_single_stock_foreign_public(stock_id):
    """ 【個股查詢專用】100% 走無 Token 公用通道抓取外資籌碼 """
    start_date = (datetime.datetime.now() - datetime.timedelta(days=12)).strftime("%Y-%m-%d")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={start_date}"
    try:
        res = requests.get(url, timeout=5.0)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"])
            foreign_df = df[df['name'].str.contains('Foreign|外資', case=False, na=False)].copy()
            if not foreign_df.empty:
                foreign_df['net_buy'] = (foreign_df['buy'] - foreign_df['sell']) / 1000
                daily_summary = foreign_df.groupby('date')['net_buy'].sum().reset_index()
                daily_summary = daily_summary.sort_values('date')
                if len(daily_summary) >= 2:
                    today_foreign = float(daily_summary.iloc[-1]['net_buy'])
                    prev_foreign = float(daily_summary.iloc[-2]['net_buy'])
                    return round(today_foreign), round(prev_foreign)
    except Exception: pass
    return 0, 0

def find_stock_id(user_input):
    """ 智慧辨識使用者輸入：支援 4 位數代號、完整名稱、簡稱模糊搜尋 """
    text = user_input.strip()

    if len(text) == 4 and text.isdigit():
        return text

    if text in STOCK_NAME_TO_ID:
        return STOCK_NAME_TO_ID[text]

    for name, code in STOCK_NAME_TO_ID.items():
        if text in name:
            return code

    match = re.search(r'\b\d{4}\b', text)
    if match:
        return match.group(0)

    return None

def analyze_stock(stock_id):
    """ 單一個股分析邏輯 """
    df = fetch_single_stock_price_public(stock_id)
    stock_name = STOCK_ID_TO_NAME.get(stock_id, stock_id)
    
    if df is None:
        return f"❌ 找不到 [{stock_name} ({stock_id})] 的近期行情數據，請確認代號或名稱是否正確。"

    df['MA5'] = df['Close'].rolling(5).mean()
    df['MA20'] = df['Close'].rolling(20).mean()
    df['MA60'] = df['Close'].rolling(min(60, len(df))).mean()
    df['Vol_MA5'] = df['Volume'].rolling(5).mean()
    
    df['STD20'] = df['Close'].rolling(20).std(ddof=0)
    df['BB_Upper'] = df['MA20'] + (df['STD20'] * 2)

    exp1 = df['Close'].ewm(span=12, adjust=False).mean()
    exp2 = df['Close'].ewm(span=26, adjust=False).mean()
    df['DIF'] = exp1 - exp2
    df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
    df['OSC'] = df['DIF'] - df['MACD']

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(latest['Close'])
    prev_close = float(prev['Close'])
    pct_change = ((close - prev_close) / prev_close) * 100
    
    ma5, ma20, ma60 = float(latest['MA5']), float(latest['MA20']), float(latest['MA60'])
    osc_today = float(latest['OSC'])

    today_foreign, prev_foreign = fetch_single_stock_foreign_public(stock_id)

    if today_foreign > 50 and prev_foreign <= 50:
        foreign_label = f"🔄 外資由賣轉買 (+{today_foreign} 張)"
    elif today_foreign > 200 and prev_foreign > 200:
        foreign_label = f"🔥 外資連買加碼 (+{today_foreign} 張)"
    elif today_foreign > 0:
        foreign_label = f"📈 外資買超 (+{today_foreign} 張)"
    elif today_foreign < 0:
        foreign_label = f"📉 外資賣超 ({today_foreign} 張)"
    else:
        foreign_label = "➖ 外資觀望/無變化"

    if close > ma20 and ma20 >= ma60:
        trend_status = "🟢 多頭格局 (站穩均線之上)"
    elif close < ma20 and ma20 <= ma60:
        trend_status = "🔴 空頭修正 (受壓於均線下)"
    else:
        trend_status = "🟡 震盪整理期"

    macd_status = "紅柱控盤 (偏多)" if osc_today > 0 else "綠柱控盤 (偏空)"

    report = (
        f"📊 【個股即時診斷：{stock_name} ({stock_id})】\n"
        f"--------------------\n"
        f"💵 最新收盤: {close:.2f} ({pct_change:+.2f}%)\n"
        f"📈 均線趨勢: {trend_status}\n"
        f"📊 MACD狀態: {macd_status}\n"
        f"👤 外資籌碼: {foreign_label}\n"
        f"--------------------\n"
        f"💡 5MA: {ma5:.2f} | 20MA: {ma20:.2f} | 60MA: {ma60:.2f}"
    )
    return report

@app.route("/", methods=['GET'])
def index():
    return f"LINE Bot Webhook Server is Running! (Loaded {len(STOCK_NAME_TO_ID)} stocks)"

# 對齊 cron-job.org 的自訂入口網址
@app.route("/run-cron-job-secret", methods=['GET', 'POST'])
def trigger_cron():
    """ 供 cron-job.org 呼叫的選股任務觸發點 """
    try:
        from cron_job import run_precalculation
        threading.Thread(target=run_precalculation).start()
        return "🚀 AI 選股背景任務已成功啟動！", 200
    except Exception as e:
        return f"❌ 觸發任務失敗: {e}", 500

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()

    if user_msg in ["選股", "AI", "AI選股", "今日選股"]:
        report = get_latest_report_from_db()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=report))
        return

    stock_id = find_stock_id(user_msg)

    if stock_id:
        report = analyze_stock(stock_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=report))
    else:
        reply_text = "🤖 請輸入欲查詢的股票名稱（如：台積電、緯穎、長榮航）或 4 位數代號（如：2330、2603），輸入「選股」可查看最新 AI 篩選報告！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
