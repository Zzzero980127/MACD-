import os
import requests
import pandas as pd
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'oG8A/4QoXPau72qWtFOcV4Hq/Ca+EgcQoJgSMHUjbNPVjtgyGkBeTwdmqfBiEjqBbZLzUn0F70JNtdTgICSrgr T+4NysH5ayUtXj4B+06J6I2DW7BT3ruJHndDuag4zjys1CO836Jwy4fR0oDq6e7wdB04t89/1O/w1cDnyilFU=')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '87cb520a332382036072d72899c94d5b')
ALPHA_VANTAGE_API_KEY = os.environ.get('ALPHA_VANTAGE_API_KEY', 'USVKF1GK6PIWA0CZ')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

@app.route("/", methods=['GET'])
def index():
    return 'Stock Bot is running alive!'

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
    symbol = event.message.text.strip().upper()
    reply_text = analyze_stock(symbol)
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

def get_tw_stock_data(stock_id):
    """透過 FinMind 免費 API 抓取台股資料 (已有 Token 授權)"""
    token = 'EyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoic2t5bGdkc0BnbWFpbC5jb20iLCJlbWFpbCI6InNreWxnZHNAZ21haWwuY29tIiwidG9rZW5fdmVyc2lvbiI6MH0.ebdFVr_Wfwo_Cm3ZnxZolvZGxfmXkywJJv8Y19gngCk'
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date=2024-01-01&token={token}"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        if data.get("status") == 200 and data.get("data"):
            df = pd.DataFrame(data["data"])
            df = df.rename(columns={'close': 'Close'})
            df['Close'] = df['Close'].astype(float)
            if len(df) >= 26:
                return df, f"{stock_id}.TW"
    except Exception:
        pass
    return None, stock_id

def get_us_stock_data(symbol):
    """美股使用 Alpha Vantage API"""
    url = f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={symbol}&apikey={ALPHA_VANTAGE_API_KEY}"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        if "Time Series (Daily)" in data:
            ts = data["Time Series (Daily)"]
            df = pd.DataFrame.from_dict(ts, orient='index')
            df = df.rename(columns={'4. close': 'Close'}).astype(float)
            df = df.sort_index()
            return df, symbol
    except Exception:
        pass
    return None, symbol

def analyze_stock(user_input):
    try:
        clean_input = user_input.replace(".TW", "").replace(".TWO", "")

        if clean_input.isdigit():
            df, target_symbol = get_tw_stock_data(clean_input)
        else:
            df, target_symbol = get_us_stock_data(user_input)

        if df is None or df.empty:
            return f"找不到代號 [{user_input}] 的資料，請確認代碼是否正確。"

        # 指標計算
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['Hist'] = df['DIF'] - df['MACD']
        df['MA20'] = df['Close'].rolling(window=20).mean()

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        close = float(latest['Close'])
        ma20 = float(latest['MA20'])
        hist_today = float(latest['Hist'])
        hist_yesterday = float(prev['Hist'])

        prev_close = float(prev['Close'])
        prev_ma20 = float(prev['MA20'])

        diff_pct = ((close - ma20) / ma20) * 100

        # 防洗盤與出場條件
        is_break_3pct = diff_pct <= -3.0
        is_two_days_below = (close < ma20) and (prev_close < prev_ma20)

        if is_break_3pct or is_two_days_below:
            reasons = []
            if is_break_3pct:
                reasons.append(f"跌破月線幅度達 {abs(diff_pct):.2f}%（超過 3% 門檻）")
            if is_two_days_below:
                reasons.append("已連續 2 個交易日收盤於月線下方")
            
            reason_str = " & ".join(reasons)
            signal = f"🔴【建議出場】{reason_str}，真跌破機率高，建議離場避險！"
        elif close < ma20:
            signal = "🟡【警戒預警】目前收盤價微幅低於月線（尚未達3%且未滿2天），建議密切觀察或先減碼部分部位。"
        elif hist_today < 0 and abs(hist_today) < abs(hist_yesterday) and close > ma20:
            signal = "🟢【試買訊號】空方力道減弱且在月線上，可建立小量試買單！"
        elif hist_yesterday < 0 and hist_today >= 0 and close > ma20:
            signal = "🔥【加碼訊號】MACD 轉多（金叉），可順勢加碼！"
        else:
            signal = "⚪【觀望】目前無明確交易訊號"

        if diff_pct < 0:
            pct_text = f"跌破月線 {abs(diff_pct):.2f}%"
        else:
            pct_text = f"高於月線 {diff_pct:.2f}%"

        return (
            f"📊 {target_symbol} 分析結果：\n"
            f"-------------------\n"
            f"最新收盤價: {close:.2f}\n"
            f"20日均線(月線): {ma20:.2f}\n"
            f"月線偏離度: {pct_text}\n"
            f"MACD柱狀體: {hist_today:.3f}\n"
            f"-------------------\n"
            f"診斷結果：\n{signal}"
        )
    except Exception as e:
        return f"分析發生錯誤: {str(e)}"

if __name__ == "__main__":
    app.run(port=5000)
  
