import os
import requests
import psycopg2
import re
import threading
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

STOCK_MAP = {}

def update_stock_map():
    """ 加上 User-Agent 偽裝，安全抓取上市 + 上櫃全台股對照表 """
    global STOCK_MAP
    new_map = {}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    # 1. 抓取上市股票 (TWSE)
    try:
        url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res_twse = requests.get(url_twse, headers=headers, timeout=8)
        if res_twse.status_code == 200:
            for item in res_twse.json():
                code = item.get("Code", "").strip()
                name = item.get("Name", "").strip()
                if len(code) == 4 and code.isdigit():
                    new_map[code] = name
                    new_map[name] = code
    except Exception as e:
        print(f"⚠️ 抓取上市名稱失敗: {e}", flush=True)

    # 2. 抓取上櫃股票 (TPEx) - 加上備援機制
    try:
        url_tpex = "https://www.tpex.org.tw/openapi/v1/mopsfront/t187ap03_R_otc"
        res_tpex = requests.get(url_tpex, headers=headers, timeout=8)
        if res_tpex.status_code == 200:
            data = res_tpex.json()
            if isinstance(data, list):
                for item in data:
                    code = str(item.get("SecuritiesCompanyCode", "")).strip()
                    name = str(item.get("CompanyName", "")).strip()
                    if len(code) == 4 and code.isdigit() and name:
                        new_map[code] = name
                        new_map[name] = code
    except Exception as e:
        print(f"⚠️ 抓取上櫃名稱失敗: {e}", flush=True)

    if new_map:
        STOCK_MAP = new_map
        print(f"✅ 股票名稱對照表已自動更新（上市+上櫃），共載入 {len(STOCK_MAP)//2} 檔個股！", flush=True)

def get_db_connection():
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception: return None

def get_report_by_date(date_key="LATEST"):
    conn = get_db_connection()
    if not conn: return "⚠️ 資料庫未連線，請檢查 DATABASE_URL 設定。"
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT content FROM history WHERE date = %s;", (date_key,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row: return row[0]
        return f"ℹ️ 找不到 [{date_key}] 的歷史選股紀錄。"
    except Exception as e:
        return f"❌ 讀取報告失敗: {e}"

def query_single_stock(user_input):
    """ 個股即時查：直接使用背景載入好的 STOCK_MAP，不觸發重複 API 請求 """
    global STOCK_MAP

    stock_id = user_input
    stock_name = ""

    if user_input in STOCK_MAP and not user_input.isdigit():
        stock_id = STOCK_MAP[user_input]
        stock_name = user_input
    elif user_input in STOCK_MAP and user_input.isdigit():
        stock_name = STOCK_MAP[user_input]

    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}"
    print(f"🔍 [個股即時查] 標的: {stock_id} {stock_name} | 🆓 [無Token模式]", flush=True)

    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200 and res.json().get("data"):
            data = res.json()["data"]
            if len(data) >= 35:
                import pandas as pd
                df = pd.DataFrame(data).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
                df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
                df = df.dropna(subset=['Close'])

                exp1 = df['Close'].ewm(span=12, adjust=False).mean()
                exp2 = df['Close'].ewm(span=26, adjust=False).mean()
                df['DIF'] = exp1 - exp2
                df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
                df['OSC'] = df['DIF'] - df['MACD']

                latest = df.iloc[-1]
                prev = df.iloc[-2]
                close = float(latest['Close'])
                prev_close = float(prev['Close'])
                pct = ((close - prev_close) / prev_close) * 100
                osc_today = float(latest['OSC'])
                osc_prev = float(prev['OSC'])

                if osc_today < 0 and osc_today > osc_prev:
                    macd_status = "📉 綠柱縮短 (空方衰退/轉折中)"
                elif osc_today > 0 and osc_prev <= 0:
                    macd_status = "💥 紅柱第1天 (剛起漲轉折)"
                elif osc_today > 0:
                    macd_status = "🔥 多頭控盤中 (紅柱)"
                else:
                    macd_status = "❄️ 空頭控盤中 (綠柱)"

                display_title = f"{stock_id} {stock_name}".strip()
                return f"📊 【個股即時解析 - {display_title}】\n--------------------\n🔹 最新收盤: {close:.2f} ({pct:+.2f}%)\n🔹 MACD狀態: {macd_status}\n🔹 當日成交量: {int(latest['Volume'])/1000:.0f} 張"
    except Exception as e:
        print(f"  └─ ❌ 查詢發生異常: {e}", flush=True)

    return f"⚠️ 無法取得個股 [{user_input}] 的數據，請確認名稱或代號是否正確。"

@app.route("/", methods=['GET'])
def home():
    return "OK - LINE Bot Running!", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK', 200

@app.route("/run-cron-job-secret", methods=['GET', 'POST'])
@app.route("/run-job", methods=['GET', 'POST'])
def trigger_job():
    try:
        from cron_job import run_precalculation
        thread = threading.Thread(target=run_precalculation)
        thread.start()
        return "OK", 200
    except Exception as e:
        return f"❌ 觸發失敗: {e}", 500

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    if "選股" in user_msg or "推薦" in user_msg:
        report = get_report_by_date("LATEST")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=report))
    
    elif "歷史" in user_msg or re.match(r'^\d{8}$', user_msg):
        date_key = user_msg if re.match(r'^\d{8}$', user_msg) else "LATEST"
        report = get_report_by_date(date_key)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📜 歷史選股查詢結果 ({date_key}):\n\n" + report))
        
    elif re.match(r'^\d{4}$', user_msg) or (len(user_msg) >= 2 and not user_msg.isdigit()):
        reply_msg = query_single_stock(user_msg)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        
    else:
        hint = (
            "🤖 AI 選股機器人使用指南：\n"
            "1. 輸入「選股」：查看今日 Top 200 轉折強勢股\n"
            "2. 輸入「代號或中文名」(如 2330 或 台積電)：即時查詢單檔狀態\n"
            "3. 輸入「8位西元年日期」(如 20260825)：查詢歷史選股紀錄"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=hint))

# 🎯 在服務啟動前先預載入股票地圖
update_stock_map()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
