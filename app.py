import os
import requests
import psycopg2
import re
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 從環境變數獲取 LINE Channel 與 FinMind 金鑰
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
FINMIND_TOKEN = os.environ.get('FINMIND_TOKEN', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def get_db_connection():
    if not DATABASE_URL:
        return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception as e:
        print(f"❌ 資料庫連線失敗: {e}")
        return None

def get_report_by_date(date_key="LATEST"):
    """ 查詢最新或特定日期的選股報告 """
    conn = get_db_connection()
    if not conn:
        return "⚠️ 資料庫未連線，請檢查 DATABASE_URL 設定。"
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT content FROM history WHERE date = %s;", (date_key,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            return row[0]
        return f"ℹ️ 找不到 [{date_key}] 的歷史選股紀錄。"
    except Exception as e:
        return f"❌ 讀取報告失敗: {e}"

def query_single_stock(stock_id):
    """ 個股即時查：抓取 FinMind 最新價量與 MACD 轉折狀態 """
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&token={FINMIND_TOKEN}"
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

                return f"📊 【個股即時解析 - {stock_id}】\n--------------------\n🔹 最新收盤: {close:.2f} ({pct:+.2f}%)\n🔹 MACD狀態: {macd_status}\n🔹 當日成交量: {int(latest['Volume'])/1000:.0f} 張"
    except Exception as e:
        print(f"查詢個股失敗: {e}")
    return f"⚠️ 無法取得個股 [{stock_id}] 的最新數據，請確認代號是否正確。"

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

@app.route("/run-job", methods=['GET', 'POST'])
def trigger_job():
    try:
        from cron_job import run_precalculation
        run_precalculation()
        return "✅ 選股排程順利執行完成！", 200
    except Exception as e:
        return f"❌ 執行選股排程失敗: {e}", 500

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    # 1. 查詢最新選股結果
    if "選股" in user_msg or "推薦" in user_msg:
        report = get_report_by_date("LATEST")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=report))
    
    # 2. 查詢歷史紀錄 (例如輸入：歷史選股 或 20260825)
    elif "歷史" in user_msg or re.match(r'^\d{8}$', user_msg):
        date_key = user_msg if re.match(r'^\d{8}$', user_msg) else "LATEST"
        report = get_report_by_date(date_key)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"📜 歷史選股查詢結果 ({date_key}):\n\n" + report))
        
    # 3. 個股代號單獨查詢 (4位數字，例如：2330)
    elif re.match(r'^\d{4}$', user_msg):
        reply_msg = query_single_stock(user_msg)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        
    # 4. 預設提示
    else:
        hint = (
            "🤖 AI 選股機器人使用指南：\n"
            "1. 輸入「選股」：查看今日 Top 200 轉折強勢股\n"
            "2. 輸入「4位股票代號」(如 2330)：即時查詢單檔 MACD 轉折狀態\n"
            "3. 輸入「8位西元年日期」(如 20260825)：查詢歷史選股紀錄"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=hint))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
