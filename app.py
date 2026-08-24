import os
import requests
import pandas as pd
import datetime
import re
import psycopg2
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
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
    return psycopg2.connect(DATABASE_URL)

def get_history_from_db(date_str):
    if not DATABASE_URL:
        return None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT content FROM history WHERE date = %s;', (date_str,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

def load_all_taiwan_stocks():
    global STOCK_NAME_MAP
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", headers=headers, timeout=3)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                s_id = str(item.get("Code", "")).strip()
                s_name = str(item.get("Name", "")).strip().replace(" ", "")
                if s_id.isdigit() and len(s_id) == 4 and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception: pass

    try:
        res = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes", headers=headers, timeout=3)
        if res.status_code == 200 and res.text.strip().startswith('['):
            for item in res.json():
                s_id = str(item.get("SecuritiesCompanyCode", "")).strip()
                s_name = str(item.get("CompanyName", "")).strip().replace(" ", "")
                if s_id.isdigit() and len(s_id) == 4 and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception: pass

load_all_taiwan_stocks()

@app.route("/", methods=['GET'])
def index():
    return "TW Stock Bot Active - High Performance Edition!"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Callback Error: {e}")
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        user_input = event.message.text.strip()
        clean_keyword = user_input.upper().replace(" ", "")
        date_match = re.search(r'^(20\d{6})$', clean_keyword)

        if date_match:
            target_date = date_match.group(1)
            history_report = get_history_from_db(target_date)
            if history_report:
                reply_text = f"📜【查閱 ({target_date}) 歷史 AI 選股紀錄】:\n\n" + history_report
            else:
                reply_text = f"⚠️ 找不到 ({target_date}) 的歷史紀錄，請確認日期格式如 20260824"
        elif "選股" in clean_keyword or "AI" in clean_keyword or "潛力股" in clean_keyword:
            # 直接讀取後台先算好的最新數據 (0.1秒秒發)
            latest_report = get_history_from_db("LATEST")
            if latest_report:
                reply_text = "🎯【AI 全台股成交前 100 強·動態精選 Top 5】:\n\n" + latest_report
            else:
                reply_text = "⚠️ 後台尚未完成今日資料統計，請稍後再試！"
        else:
            reply_text = analyze_single_stock(user_input)

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        print(f"Handle Error: {e}")

# 個股即時單獨查詢功能
def analyze_single_stock(user_input):
    clean_input = user_input.upper().replace(".TW", "").replace(" ", "").strip()
    stock_code = STOCK_NAME_MAP.get(clean_input, clean_input)
    
    if not stock_code.isdigit() or len(stock_code) != 4:
        return f"⚠️ 找不到「{user_input}」的台股資料。"

    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=100)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_code}&start_date={start_date}"
        if FINMIND_TOKEN: url += f"&token={FINMIND_TOKEN}"
        
        res = requests.get(url, timeout=3)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"])
            close = float(df.iloc[-1]['close'])
            ma20 = df['close'].astype(float).rolling(20).mean().iloc[-1]
            bias = ((close - ma20) / ma20) * 100
            
            return (
                f"📊 [{user_input} ({stock_code})] 個股快訊 :\n"
                f"--------------------\n"
                f"最新價: ${close:.2f}\n"
                f"月線價: ${ma20:.2f}\n"
                f"乖離率: {bias:+.2f}%\n"
                f"建議: {'🟢 站穩月線，多頭結構' if close >= ma20 else '🔴 跌破月線，觀望為主'}"
            )
    except Exception: pass
    return f"⚠️ 暫時無法取得 [{stock_code}] 技術數據。"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
