import os
import sqlite3
from flask import Flask, request
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from cron_job import run_precalculation

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def get_history_from_db(date_str="LATEST"):
    try:
        conn = sqlite3.connect("stock_cache.db")
        cursor = conn.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS history (date TEXT PRIMARY KEY, content TEXT);')
        cursor.execute('SELECT content FROM history WHERE date = ?;', (date_str,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

@app.route("/", methods=['GET'])
def index():
    return "OK"

@app.route('/run-cron-job-secret', methods=['GET'])
def trigger_cron():
    # 強制等算完才回應，防止 Render 冷凍程序！
    res = run_precalculation()
    return f"Execution Finished:\n{res}", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Error: {e}")
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_input = event.message.text.strip()
    if "選股" in user_input or "AI" in user_input:
        report = get_history_from_db("LATEST")
        if not report:
            report = "⚠️ 正在為您即時計算最新 Top 3，請稍等 10 秒後再傳一次「AI選股」！"
            run_precalculation()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=report))
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"請輸入「AI選股」來查看最新排行榜！"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
