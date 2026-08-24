import os
import threading
import requests
import pandas as pd
import datetime
import psycopg2
from flask import Flask, request
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

STOCK_NAME_MAP = {}

def get_db_connection():
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception: return None

def get_history_from_db(date_str="LATEST"):
    conn = get_db_connection()
    if not conn: return None
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT content FROM history WHERE date = %s;', (date_str,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row[0] if row else None
    except Exception: return None

def load_all_taiwan_stocks():
    global STOCK_NAME_MAP
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", headers=headers, timeout=3)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                s_id = str(item.get("Code", "")).strip()
                s_name = str(item.get("Name", "")).strip().replace(" ", "")
                if s_id.isdigit() and len(s_id) == 4 and s_name: STOCK_NAME_MAP[s_name] = s_id
    except Exception: pass

    try:
        res = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes", headers=headers, timeout=3)
        if res.status_code == 200 and res.text.strip().startswith('['):
            for item in res.json():
                s_id = str(item.get("SecuritiesCompanyCode", "")).strip()
                s_name = str(item.get("CompanyName", "")).strip().replace(" ", "")
                if s_id.isdigit() and len(s_id) == 4 and s_name: STOCK_NAME_MAP[s_name] = s_id
    except Exception: pass

load_all_taiwan_stocks()

def get_tw_stock_data_finmind(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=120)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN: url += f"&token={FINMIND_TOKEN}"
        
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=4.0)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
            df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
            df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
            df = df.dropna(subset=['Close'])
            if len(df) >= 20: return df
    except Exception: pass
    return None

def get_tw_foreign_investor(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN: url += f"&token={FINMIND_TOKEN}"

        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3.0)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"])
            foreign_df = df[df['name'].str.contains('Foreign|外資', case=False, na=False)]
            if not foreign_df.empty:
                latest_date = foreign_df.iloc[-1]['date']
                day_data = foreign_df[foreign_df['date'] == latest_date]
                return round((day_data['buy'].sum() - day_data['sell'].sum()) / 1000)
    except Exception: pass
    return 0

def get_tw_stock_revenue(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=400)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN: url += f"&token={FINMIND_TOKEN}"

        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3.0)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"])
            if 'revenue' in df.columns and len(df) >= 13:
                df['revenue'] = pd.to_numeric(df['revenue'], errors='coerce')
                df = df.dropna(subset=['revenue'])
                
                latest, prev_month, last_year = df.iloc[-1], df.iloc[-2], df.iloc[-13]
                rev_latest, rev_prev, rev_ly = float(latest['revenue']), float(prev_month['revenue']), float(last_year['revenue'])
                rev_date = f"{latest.get('revenue_year', '')}/{str(latest.get('revenue_month', '')).zfill(2)}"

                yoy_val = ((rev_latest - rev_ly) / rev_ly * 100) if rev_ly > 0 else 0.0
                mom_val = ((rev_latest - rev_prev) / rev_prev * 100) if rev_prev > 0 else 0.0
                eval_text = "🟢 強勁成長" if yoy_val > 15 else ("🟢 穩健成長" if yoy_val > 0 else "🔴 營收衰退")
                return f"{rev_date}月營收 | YoY: {yoy_val:+.2f}% | MoM: {mom_val:+.2f}%\n    評價: {eval_text}"
    except Exception: pass
    return "暫無最新月營收資料"

def resolve_stock_symbol(user_input):
    if len(STOCK_NAME_MAP) < 300: load_all_taiwan_stocks()
    clean_input = user_input.upper().replace(".TW", "").replace(".TWO", "").replace(" ", "").strip()
    if clean_input.isdigit() and len(clean_input) == 4:
        name = [k for k, v in STOCK_NAME_MAP.items() if v == clean_input]
        return clean_input, name[0] if name else clean_input
    if clean_input in STOCK_NAME_MAP: return STOCK_NAME_MAP[clean_input], clean_input
    for name, code in STOCK_NAME_MAP.items():
        if clean_input in name or name in clean_input: return code, name
    return clean_input, clean_input

def analyze_stock(user_input):
    try:
        stock_code, display_name = resolve_stock_symbol(user_input)
        if not stock_code.isdigit() or len(stock_code) != 4:
            return f"⚠️ 找不到「{user_input}」的台股資料，請確認名稱或代號是否正確。"

        df = get_tw_stock_data_finmind(stock_code)
        if df is None or df.empty:
            return f"⚠️ 暫時無法取得 [{display_name} ({stock_code})] 的技術數據，請稍後重試。"

        foreign_net = get_tw_foreign_investor(stock_code)
        revenue_info = get_tw_stock_revenue(stock_code)

        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA60'] = df['Close'].rolling(window=60).mean()
        df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()
        df['STD20'] = df['Close'].rolling(window=20).std(ddof=0)
        df['BB_Upper'] = df['MA20'] + (df['STD20'] * 2)

        latest, prev = df.iloc[-1], df.iloc[-2]
        close, prev_close = float(latest['Close']), float(prev['Close'])
        ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close
        ma60 = float(latest['MA60']) if not pd.isna(latest['MA60']) else close
        bb_upper = float(latest['BB_Upper']) if not pd.isna(latest['BB_Upper']) else close

        diff_pct = ((close - ma20) / ma20) * 100 if ma20 != 0 else 0
        vol_today = float(latest['Volume'])
        vol_ma5 = float(latest['Vol_MA5']) if not pd.isna(latest['Vol_MA5']) else vol_today
        price_change_pct = ((close - prev_close) / prev_close) * 100

        if close >= bb_upper * 0.98:
            vol_status = f"💥 接近/突破布林上軌 ({close:.2f} >= {bb_upper:.2f})\n  👉 短線過熱，切勿追高"
        elif price_change_pct > 0 and vol_today >= vol_ma5 * 1.15:
            vol_status = f"👆 上漲放量 (+{price_change_pct:.1f}%)\n  👉 多頭攻擊強烈"
        elif price_change_pct < 0 and vol_today >= vol_ma5 * 1.15:
            vol_status = f"👇 下跌放量 ({price_change_pct:.1f}%)\n  👉 注意賣壓風險"
        else:
            vol_status = f"➡️ 價量平穩 ({price_change_pct:+.1f}%)"

        foreign_text = f"{foreign_net:} 張" if foreign_net != 0 else "0 張/估算中"
        signal = "🔴 【建議出場/觀望】跌破支撐" if (close < ma60 or diff_pct < -3.0) else ("🔥 【多頭控盤】可持股或觀察" if close >= ma20 else "🟡 【多短觀望】偏溫運作")
        pct_text = f"高於月線 {diff_pct:.2f}%" if diff_pct >= 0 else f"低於月線 {abs(diff_pct):.2f}%"

        return (
            f"📊 [{display_name} ({stock_code})] 技術與籌碼分析 :\n"
            f"--------------------\n"
            f"最新收盤價: {close:.2f}\n"
            f"20日均線(月線): {ma20:.2f} ({pct_text})\n"
            f"60日均線(季線): {ma60:.2f}\n"
            f"布林通道上軌: {bb_upper:.2f}\n"
            f"量價結構:\n  {vol_status}\n"
            f"外資籌碼: {foreign_text}\n"
            f"--------------------\n"
            f"📈 基本面與營收 :\n  {revenue_info}\n"
            f"--------------------\n"
            f"💡 操作建議 :\n{signal}"
        )
    except Exception as e:
        return f"⚠️ 分析發生錯誤: {str(e)}"

@app.route("/", methods=['GET'])
def index(): return "OK"

@app.route('/run-cron-job-secret', methods=['GET'])
def trigger_cron():
    from cron_job import run_precalculation
    threading.Thread(target=run_precalculation).start()
    return "🚀 前 100 檔選股運算已在背景啟動！每 10 秒處理一檔（總耗時約 16 分鐘），完成後會自動存入 Supabase 並推播到 LINE。", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except Exception as e: print(f"Error: {e}")
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_input = event.message.text.strip()
    clean_keyword = user_input.upper().replace(" ", "")

    # 1. AI 選股（回傳最新資料）
    if "選股" in clean_keyword or "AI" in clean_keyword:
        report = get_history_from_db("LATEST")
        reply_text = report if report else "⚠️ 後台尚未完成今日統計，請稍後再試！"

    # 2. 歷史日期查詢（如 20260824 或 0824）
    elif user_input.replace("/", "").replace("-", "").isdigit() and len(user_input.replace("/", "").replace("-", "")) in [4, 8]:
        clean_date = user_input.replace("/", "").replace("-", "")
        query_date = f"{datetime.datetime.now().strftime('%Y')}{clean_date}" if len(clean_date) == 4 else clean_date

        report = get_history_from_db(query_date)
        if report:
            reply_text = report
        else:
            # 若不是歷史選股日期，自動轉去查詢個股（例如輸入 2330）
            reply_text = analyze_stock(user_input)

    # 3. 個股即時查詢（輸入股票名稱或代號，如 台積電、2317）
    else:
        reply_text = analyze_stock(user_input)

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
