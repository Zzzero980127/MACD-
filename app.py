import os
import requests
import psycopg2
import re
import datetime
import threading
import pandas as pd
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# -----------------------------------------------------------------------------
# 1. 讀取環境變數與 LINE 設定
# -----------------------------------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

STOCK_MAP = {}

def update_stock_map():
    """從證交所 API 載入全台股代號與名稱對照表"""
    global STOCK_MAP
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            for item in res.json():
                code = item.get("Code", "").strip()
                name = item.get("Name", "").strip()
                if len(code) == 4 and code.isdigit():
                    STOCK_MAP[code] = name
                    STOCK_MAP[name] = code
            print(f"✅ [App Log] 股票對照表載入完成，共 {len(STOCK_MAP)//2} 檔個股！", flush=True)
    except Exception as e:
        print(f"⚠️ [App Log] 股票對照表更新失敗: {e}", flush=True)

def get_db_connection():
    """建立 PostgreSQL 資料庫連線"""
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception: return None

def get_report_by_date(date_key="LATEST"):
    """從資料庫讀取特定日期或最新版本的選股報告"""
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

# -----------------------------------------------------------------------------
# 2. 查詢單檔股票 (公開無 Token 請求，消耗 300次/小時 配額，避免占用 Token)
# -----------------------------------------------------------------------------
def query_single_stock(user_input):
    """查詢單檔股票的即時 MACD 動能狀態"""
    global STOCK_MAP
    if not STOCK_MAP: update_stock_map()

    stock_id = user_input
    stock_name = ""

    if user_input in STOCK_MAP and not user_input.isdigit():
        stock_id = STOCK_MAP[user_input]
        stock_name = user_input
    elif user_input in STOCK_MAP and user_input.isdigit():
        stock_name = STOCK_MAP[user_input]

    start_date = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"

    try:
        # 注意：此處故意完全不帶 headers 或 token 參數，走免費公開配額
        res = requests.get(url, timeout=5)
        if res.status_code == 200 and res.json().get("data"):
            data = res.json()["data"]
            if len(data) >= 30:
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
                elif osc_today > 0 and osc_today > osc_prev:
                    macd_status = "🔥 紅柱擴大中 (多頭動能強)"
                elif osc_today > 0:
                    macd_status = "⚠️ 紅柱縮短中 (多頭力道減弱)"
                else:
                    macd_status = "❄️ 空頭控盤中 (綠柱)"

                display_title = f"{stock_id} {stock_name}".strip()
                return (
                    f"📊 【個股即時解析 - {display_title}】\n"
                    f"--------------------\n"
                    f"🔹 最新收盤: {close:.2f} ({pct:+.2f}%)\n"
                    f"🔹 MACD狀態: {macd_status}\n"
                    f"🔹 當日成交量: {int(latest['Volume'])/1000:.0f} 張"
                )
    except Exception as e:
        print(f"❌ [Query Log] 查詢失敗: {e}", flush=True)
        
    return f"⚠️ 無法取得個股 [{user_input}] 的數據，請確認名稱或代號是否正確。"

# -----------------------------------------------------------------------------
# 3. Flask 路由與事件處理
# -----------------------------------------------------------------------------
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

def background_job():
    """在背景執行選股任務的封裝函式"""
    try:
        from cron_job import run_precalculation
        run_precalculation()
        print("✅ [Background] 後台 AI 選股與推播順利完成！", flush=True)
    except Exception as e:
        print(f"❌ [Background] 後台執行失敗: {e}", flush=True)

@app.route("/run-job", methods=['GET', 'POST'])
def trigger_job():
    """cron-job.org 呼叫的端點：秒回 200 OK 防止逾時，並在後台進行運算與推播"""
    print("⏰ [Cron Endpoint] 收到觸發請求，立即響應並丟至後台執行...", flush=True)
    
    # 建立獨立執行緒於背景執行選股任務
    thread = threading.Thread(target=background_job)
    thread.start()
    
    return "✅ 任務已在後台啟動！", 200

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
            "1. 輸入「選股」：查看今日 Top 200 加分精選強勢股\n"
            "2. 輸入「代號或中文名」(如 2606 或 裕民)：即時解析單檔動能狀態\n"
            "3. 輸入「8位西元年日期」(如 20260825)：查詢歷史選股紀錄"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=hint))

if __name__ == "__main__":
    update_stock_map()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
