import os
import requests
import pandas as pd
import psycopg2
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# -----------------------------------------------------------------------------
# 1. 環境變數
# -----------------------------------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# -----------------------------------------------------------------------------
# 2. 模擬倉查詢與報表產生邏輯
# -----------------------------------------------------------------------------
def get_sim_portfolio_report():
    if not DATABASE_URL:
        return "❌ 資料庫連線失敗，無法讀取模擬倉資料。"

    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        conn = psycopg2.connect(url, connect_timeout=10)
        cursor = conn.cursor()

        # A. 抓取【持倉中】股票
        cursor.execute("SELECT id, stock_code, stock_name, strategy_type, buy_date, buy_price FROM sim_trades WHERE status = 'HOLD' ORDER BY buy_date ASC;")
        holds = cursor.fetchall()

        lines = [
            "📊 【AI 模擬倉即時戰報】",
            "💰 測試設定：每檔固定投入 10 萬元",
            "===================="
        ]

        lines.append("🛒 【當前持股明細】")
        if not holds:
            lines.append("目前空倉中（無持股）")
        else:
            headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if FINMIND_TOKEN else {}
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
                except:
                    pass

                # 每檔 10 萬元計算
                shares = int(100000 / buy_price)
                cost_actual = shares * buy_price
                curr_val = shares * curr_price
                pnl_dollars = curr_val - cost_actual
                pnl_pct = ((curr_price - buy_price) / buy_price) * 100

                emoji = "🔺" if pnl_dollars >= 0 else "🔻"
                lines.append(
                    f"🔹 {code} {name} ({st_type[:3]})\n"
                    f"   📅 買入: {buy_date} | 成本: ${buy_price:.2f}\n"
                    f"   📈 現價: ${curr_price:.2f} | 股數: {shares:,}股\n"
                    f"   👉 損益: {emoji} ${pnl_dollars:+,.0f} ({pnl_pct:+.2f}%)"
                )
                lines.append("┈┈┈┈┈┈┈┈┈┈")

        lines.append("\n====================\n")

        # B. 抓取【歷史平倉】統計
        cursor.execute("SELECT buy_price, sell_price, return_rate FROM sim_trades WHERE status = 'CLOSED';")
        closed = cursor.fetchall()

        lines.append("📈 【歷史回測戰績彙整】")
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
            lines.append(f"🏆 策略勝率 (Win Rate): {win_rate:.1f}% ({wins}勝 / {total_trades - wins}敗)")
            lines.append(f"{pnl_emoji} 累計淨損益: ${total_pnl:+,.0f} 元")

        cursor.close()
        conn.close()
        return "\n".join(lines)

    except Exception as e:
        return f"⚠️ 讀取模擬倉報表時發生錯誤: {e}"

# -----------------------------------------------------------------------------
# 3. LINE Webhook 路由與事件接聽
# -----------------------------------------------------------------------------
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

    # 觸發模擬倉查詢關鍵字
    if user_msg in ["模擬倉", "持倉", "模擬倉持倉", "勝率", "戰績"]:
        report = get_sim_portfolio_report()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=report))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
