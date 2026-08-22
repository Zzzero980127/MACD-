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

# 全域變數：快取全台股中文對照表
STOCK_NAME_MAP = {}

def update_stock_name_map():
    """向 FinMind / 證交所 抓取全台股中文名稱與代號對照"""
    global STOCK_NAME_MAP
    try:
        url = "https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo"
        res = requests.get(url, timeout=10)
        data = res.json()
        if data.get("status") == 200:
            for item in data.get("data", []):
                s_id = item.get("stock_id")
                s_name = item.get("stock_name")
                if s_id and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception as e:
        print(f"Update stock map error: {e}")

# 初始化載入一次對照表
update_stock_name_map()

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
    symbol = event.message.text.strip()
    reply_text = analyze_stock(symbol)
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

def resolve_stock_symbol(user_input):
    """判斷輸入是代號還是中文，並轉換為股票代號與顯示名稱"""
    clean_input = user_input.upper().replace(".TW", "").replace(".TWO", "")
    
    # 若輸入純數字，直接作為代碼
    if clean_input.isdigit():
        # 嘗試從對照表反查中文名
        name = [k for k, v in STOCK_NAME_MAP.items() if v == clean_input]
        stock_name = name[0] if name else clean_input
        return clean_input, stock_name

    # 若對照表為空，再更新一次
    if not STOCK_NAME_MAP:
        update_stock_name_map()

    # 精確比對中文名稱
    if user_input in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[user_input], user_input

    # 模糊比對（例如輸入「台積」匹配「台積電」）
    for name, code in STOCK_NAME_MAP.items():
        if user_input in name or name in user_input:
            return code, name

    # 美股代號或無法識別的字詞，原樣傳回
    return clean_input, clean_input

def get_tw_stock_data(stock_id):
    """抓取台股日 K 線與成交量"""
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date=2024-01-01"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        if data.get("status") == 200 and len(data.get("data", [])) >= 26:
            df = pd.DataFrame(data["data"])
            df = df.rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
            df['Close'] = df['Close'].astype(float)
            df['Volume'] = df['Volume'].astype(float)
            return df, f"{stock_id}.TW"
    except Exception:
        pass
    return None, stock_id

def get_tw_foreign_investor(stock_id):
    """抓取台股外資買賣超資料 (張數)"""
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date=2024-08-01"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        if data.get("status") == 200 and data.get("data"):
            df = pd.DataFrame(data["data"])
            foreign_df = df[df['name'].str.contains('Foreign', case=False, na=False)]
            if not foreign_df.empty:
                latest_net = foreign_df.iloc[-1]['buy'] - foreign_df.iloc[-1]['sell']
                return round(latest_net / 1000)
    except Exception:
        pass
    return None

def get_us_stock_data(symbol):
    url = f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={symbol}&apikey={ALPHA_VANTAGE_API_KEY}"
    try:
        res = requests.get(url, timeout=10)
        data = res.json()
        if "Time Series (Daily)" in data:
            ts = data["Time Series (Daily)"]
            df = pd.DataFrame.from_dict(ts, orient='index')
            df = df.rename(columns={'4. close': 'Close', '5. volume': 'Volume'}).astype(float)
            df = df.sort_index()
            return df, symbol
    except Exception:
        pass
    return None, symbol

def analyze_stock(user_input):
    try:
        stock_code, display_name = resolve_stock_symbol(user_input)
        foreign_net = None

        if stock_code.isdigit():
            df, target_symbol = get_tw_stock_data(stock_code)
            foreign_net = get_tw_foreign_investor(stock_code)
        else:
            df, target_symbol = get_us_stock_data(stock_code)

        if df is None or df.empty:
            return f"找不到 [{user_input}] 的股票資料，請確認名稱或代碼是否正確。"

        # 1. 均線與 MACD 計算
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['Hist'] = df['DIF'] - df['MACD']
        df['MA20'] = df['Close'].rolling(window=20).mean()

        # 2. 成交量 5 日均量計算
        df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        close = float(latest['Close'])
        ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close
        hist_today = float(latest['Hist'])
        hist_yesterday = float(prev['Hist'])

        prev_close = float(prev['Close'])
        prev_ma20 = float(prev['MA20']) if not pd.isna(prev['MA20']) else prev_close

        diff_pct = ((close - ma20) / ma20) * 100 if ma20 != 0 else 0

        # 成交量狀態判斷
        vol_today = float(latest['Volume'])
        vol_ma5 = float(latest['Vol_MA5']) if not pd.isna(latest['Vol_MA5']) else vol_today
        
        if vol_today >= vol_ma5 * 1.5:
            vol_status = "🔥 顯著放量 (大於5日均量50%)"
        elif vol_today >= vol_ma5 * 1.2:
            vol_status = "📈 溫和放量 (大於5日均量20%)"
        elif vol_today <= vol_ma5 * 0.8:
            vol_status = "📉 明顯量縮 (低於5日均量20%)"
        else:
            vol_status = "➡️ 量能平穩 (與5日均量相當)"

        # 外資買賣超字串
        if foreign_net is not None:
            if foreign_net > 0:
                foreign_text = f"買超 {foreign_net} 張"
            elif foreign_net < 0:
                foreign_text = f"賣超 {abs(foreign_net)} 張"
            else:
                foreign_text = "買賣超 0 張"
        else:
            foreign_text = "無數據 (或美股無此指標)"

        # 防洗盤與出場條件
        is_break_3pct = diff_pct <= -3.0
        is_two_days_below = (close < ma20) and (prev_close < prev_ma20)

        # 訊號判斷
        if is_break_3pct or is_two_days_below:
            reasons = []
            if is_break_3pct:
                reasons.append(f"跌破月線 {abs(diff_pct):.2f}%（超 3%）")
            if is_two_days_below:
                reasons.append("連 2 日低於月線")
            signal = f"🔴【建議出場/停損】{' & '.join(reasons)}，趨勢轉弱，防範持續下探！"

        elif close < ma20:
            signal = "🟡【警戒觀望】股價微幅低於月線，趨勢偏弱，建議先觀望或適度減碼。"

        elif hist_today > 0 and hist_today >= hist_yesterday:
            signal = "🔥【多頭續抱/加碼】強勢站穩月線且 MACD 紅柱擴大，多方控盤可持續持有或逢低加碼！"

        elif hist_today > 0 and hist_today < hist_yesterday:
            signal = "🟢【偏多持有】站穩月線上，但多頭力道稍緩，建議續抱並關注月線支撐。"

        elif hist_today < 0 and abs(hist_today) < abs(hist_yesterday):
            signal = "🟢【試買建倉】股價在月線上，且空方力道開始減弱，可考慮建立分批試買單。"

        else:
            signal = "⚪【盤整觀望】多空力道均衡，建議靜待方向確立再操作。"

        pct_text = f"高於月線 {diff_pct:.2f}%" if diff_pct >= 0 else f"跌破月線 {abs(diff_pct):.2f}%"
        title_display = f"{display_name} ({stock_code})" if display_name != stock_code else target_symbol

        return (
            f"📊 {title_display} 分析結果：\n"
            f"-------------------\n"
            f"最新收盤價: {close:.2f}\n"
            f"20日均線(月線): {ma20:.2f}\n"
            f"月線偏離度: {pct_text}\n"
            f"MACD柱狀體: {hist_today:.3f}\n"
            f"成交量狀態: {vol_status}\n"
            f"外資籌碼動向: {foreign_text}\n"
            f"-------------------\n"
            f"💡 操作建議：\n{signal}"
        )
    except Exception as e:
        return f"分析發生錯誤: {str(e)}"

if __name__ == "__main__":
    app.run(port=5000)
    
