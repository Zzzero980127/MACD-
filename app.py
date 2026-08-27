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

# 匯入模擬倉模組
from sim_portfolio import init_sim_db, process_simulation

app = Flask(__name__)

# -----------------------------------------------------------------------------
# 1. 讀取環境變數與 LINE 設定
# -----------------------------------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

STOCK_MAP = {}

def update_stock_map():
    """從證交所 API 載入全台股代號與名稱對照表"""
    global STOCK_MAP
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url, timeout=8)
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
# 2. 模擬倉戰報生成邏輯 (固定投入 10 萬元測試，升級加入當前持倉總損益與勝率)
# -----------------------------------------------------------------------------
def get_sim_portfolio_report():
    """計算並組合模擬倉當前持股明細與歷史勝率戰報"""
    conn = get_db_connection()
    if not conn:
        return "❌ 資料庫連線失敗，無法讀取模擬倉資料。"

    try:
        cursor = conn.cursor()

        # A. 抓取目前【持倉中】股票
        cursor.execute("SELECT id, stock_code, stock_name, strategy_type, buy_date, buy_price FROM sim_trades WHERE status = 'HOLD' ORDER BY buy_date ASC;")
        holds = cursor.fetchall()

        lines = [
            "📊 【AI 模擬倉即時戰報】",
            "💰 測試設定：每檔固定投入 10 萬元",
            "===================="
        ]

        if not holds:
            lines.append("🛒 【當前持股明細】\n目前空倉中（無持股）")
        else:
            headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if FINMIND_TOKEN else {}
            
            # 用於計算當前持倉總體戰力
            total_hold_cost = 0.0
            total_hold_market_val = 0.0
            hold_winning_count = 0
            total_hold_stocks = len(holds)
            
            hold_lines_temp = []

            for item in holds:
                trade_id, code, name, st_type, buy_date, buy_price = item
                buy_price = float(buy_price)

                curr_price = buy_price
                try:
                    price_url = "https://api.finmindtrade.com/api/v4/data"
                    res = requests.get(price_url, params={"dataset": "TaiwanStockPrice", "data_id": code, "start_date": buy_date}, headers=headers, timeout=5)
                    if res.status_code == 200 and res.json().get("data"):
                        df_p = pd.DataFrame(res.json()["data"])
                        curr_price = float(df_p.iloc[-1]['close'])
                except Exception:
                    pass

                # 計算 10 萬元買入的股數與即時損益
                shares = int(100000 / buy_price)
                cost_actual = shares * buy_price
                curr_val = shares * curr_price
                pnl_dollars = curr_val - cost_actual
                pnl_pct = ((curr_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0.0

                total_hold_cost += cost_actual
                total_hold_market_val += curr_val
                if pnl_dollars > 0:
                    hold_winning_count += 1

                emoji = "🔺" if pnl_dollars >= 0 else "🔻"
                sign = "+" if pnl_dollars >= 0 else ""

                hold_lines_temp.append(
                    f"🔹 {code} {name} ({st_type[:3]})\n"
                    f"   📅 買入: {buy_date} | 成本: ${buy_price:.2f}\n"
                    f"   📈 現價: ${curr_price:.2f} | 股數: {shares:,}股\n"
                    f"   👉 損益: {emoji} {sign}${pnl_dollars:,.0f} ({sign}{pnl_pct:.2f}%)"
                )

            # 計算當前持股統計
            hold_total_pnl = total_hold_market_val - total_hold_cost
            hold_total_return = (hold_total_pnl / total_hold_cost * 100) if total_hold_cost > 0 else 0.0
            hold_win_rate = (hold_winning_count / total_hold_stocks * 100) if total_hold_stocks > 0 else 0.0

            total_emoji = "🔺" if hold_total_pnl >= 0 else "🔻"
            total_sign = "+" if hold_total_pnl >= 0 else ""

            # 組合頂部總計看板
            lines.append(f"🏆 當前未實現勝率：{hold_win_rate:.1f}% ({hold_winning_count}/{total_hold_stocks} 檔獲利)")
            lines.append(f"💵 當前持股總成本：${total_hold_cost:,.0f}")
            lines.append(f"💼 當前持股總市值：${total_hold_market_val:,.0f}")
            lines.append(f"📈 未實現總損益：{total_emoji} {total_sign}${hold_total_pnl:,.0f} ({total_sign}{hold_total_return:.2f}%)")
            lines.append("--------------------")
            lines.append("🛒 【當前持股明細】")
            lines.append("\n┈┈┈┈┈┈┈┈┈┈\n".join(hold_lines_temp))

        lines.append("\n====================\n")

        # B. 抓取【歷史平倉】戰績統計
        cursor.execute("SELECT buy_price, sell_price, return_rate FROM sim_trades WHERE status = 'CLOSED';")
        closed = cursor.fetchall()

        lines.append("📈 【歷史已平倉戰績彙整】")
        if not closed:
            lines.append("尚無已平倉交易紀錄")
        else:
            total_trades = len(closed)
            wins = 0
            total_pnl = 0

            for buy_p, sell_p, ret_rate in closed:
                buy_p, sell_p = float(buy_p), float(sell_p)
                shares = int(100000 / buy_p)
                pnl = (shares * sell_p) - (shares * buy_p)
                total_pnl += pnl
                if pnl > 0: wins += 1

            win_rate = (wins / total_trades) * 100
            pnl_emoji = "🎉" if total_pnl >= 0 else "📉"

            lines.append(f"🔹 總已平倉筆數: {total_trades} 筆")
            lines.append(f"🏆 平倉勝率 (Win Rate): {win_rate:.1f}% ({wins}勝 / {total_trades - wins}敗)")
            lines.append(f"{pnl_emoji} 累計淨損益: ${total_pnl:+,.0f} 元")

        cursor.close()
        conn.close()
        return "\n".join(lines)

    except Exception as e:
        return f"⚠️ 讀取模擬倉報表時發生錯誤: {e}"

# -----------------------------------------------------------------------------
# 3. 查詢單檔股票 (公開無 Token 請求，避免占用配額)
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
                    macd_status = "🔥 紅柱擴態強 (多頭動態強)"
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
# 4. Flask 路由與事件處理
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
    """在背景執行選股任務與模擬倉運算的封裝函式"""
    # 1. 執行每日 AI 選股與 LINE 推播
    try:
        from cron_job import run_precalculation
        run_precalculation()
        print("✅ [Background] 後台 AI 選股與推播順利完成！", flush=True)
    except Exception as e:
        print(f"❌ [Background] 後台選股執行失敗: {e}", flush=True)

    # 2. 執行模擬倉更新與計算 (獨立隔離，避免互相干擾)
    try:
        init_sim_db()
        process_simulation()
        print("✅ [Background] 後台模擬倉結算與掃描順利完成！", flush=True)
    except Exception as e:
        print(f"❌ [Background] 後台模擬倉執行失敗: {e}", flush=True)

@app.route("/run-job", methods=['GET', 'POST'])
def trigger_job():
    """cron-job.org 呼叫的端點"""
    print("⏰ [Cron Endpoint] 收到觸發請求，立即響應並丟至後台執行...", flush=True)
    thread = threading.Thread(target=background_job)
    thread.start()
    return "✅ 任務已在後台啟動！", 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    # 🎯 模擬倉與勝率戰報查詢 (支援多種常見關鍵字)
    if user_msg in ["模擬倉", "持倉", "模擬倉持倉", "勝率", "戰績", "戰報"]:
        report = get_sim_portfolio_report()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=report))
    
    elif "選股" in user_msg or "推薦" in user_msg:
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
            "2. 輸入「戰報」或「模擬倉」：查看持倉明細與回測勝率\n"
            "3. 輸入「代號或中文名」(如 2606 或 裕民)：即時解析單檔動態\n"
            "4. 輸入「8位西元年日期」(如 20260825)：查詢歷史選股紀錄"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=hint))

if __name__ == "__main__":
    
from test import test_sync

@app.route('/test')
def trigger_test():
    test_sync()
    return "✅ 測試指令已發送！請去 Google 試算表確認是否有新資料。", 200
    update_stock_map()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
