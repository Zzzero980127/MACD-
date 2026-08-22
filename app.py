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

STOCK_NAME_MAP = {}

# 產業與旺季庫
STOCK_CONCEPT_INFO = {
    "2330": {"concept": "晶圓代工 / AI晶片 / CoWoS", "season": "10月~隔年2月 (Q4~Q1 科技旺季)"},
    "2610": {"concept": "航空客運 / 觀光暑假 / 客貨運", "season": "6月~8月 (暑假客運) / 11月~12月 (年底貨運旺季)"},
    "2618": {"concept": "航空客運 / 貨運雙雄", "season": "6月~8月 (暑假旅遊) / 11月~12月 (年底貨運旺季)"},
    "2317": {"concept": "鴻海家族 / AI伺服器 / 電動車", "season": "10月~12月 (iPhone發布與Q4出貨旺季)"},
    "2454": {"concept": "IC設計 / 手機晶片 / 邊緣AI", "season": "11月~3月 (新機拉貨與展覽題材)"},
    "6770": {"concept": "成熟製程 / 車用半導體", "season": "3月~7月 (半導體復甦與股東會題材)"},
    "2059": {"concept": "伺服器導軌 / AI伺服器 / 高價股", "season": "11月~3月 (伺服器新平台拉貨旺季)"},
    "3231": {"concept": "AI伺服器代工 / 緯創", "season": "11月~2月 (集團作帳與AI出貨旺季)"},
    "2382": {"concept": "AI伺服器代工 / 廣達", "season": "11月~2月 (AI浪潮與歲末行情)"}
}

def update_stock_name_map():
    global STOCK_NAME_MAP
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            for item in data:
                s_id = item.get("Code")
                s_name = item.get("Name")
                if s_id and s_name:
                    STOCK_NAME_MAP[s_name.strip()] = s_id.strip()
    except Exception as e:
        print(f"Update stock map error: {e}")

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
    clean_input = user_input.upper().replace(".TW", "").replace(".TWO", "")
    
    if clean_input.isdigit():
        name = [k for k, v in STOCK_NAME_MAP.items() if v == clean_input]
        stock_name = name[0] if name else clean_input
        return clean_input, stock_name

    if not STOCK_NAME_MAP:
        update_stock_name_map()

    if user_input in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[user_input], user_input

    for name, code in STOCK_NAME_MAP.items():
        if user_input in name or name in user_input:
            return code, name

    return clean_input, clean_input

def get_tw_stock_data(stock_id):
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

def get_tw_revenue(stock_id):
    """抓取並精準計算台股營收 MoM / YoY"""
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={stock_id}&start_date=2024-01-01"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        if data.get("status") == 200 and data.get("data"):
            df = pd.DataFrame(data["data"])
            if len(df) >= 2:
                latest = df.iloc[-1]
                prev = df.iloc[-2]
                
                rev_now = float(latest.get('revenue', 0))
                rev_prev = float(prev.get('revenue', 0))
                
                # 計算 MoM
                mom = ((rev_now - rev_prev) / rev_prev * 100) if rev_prev > 0 else 0
                
                # 計算 YoY (若有去年同月數據)
                yoy = None
                if len(df) >= 13:
                    last_year = df.iloc[-13]
                    rev_ly = float(last_year.get('revenue', 0))
                    if rev_ly > 0:
                        yoy = (rev_now - rev_ly) / rev_ly * 100

                month_str = f"{latest.get('revenue_year')}/{latest.get('revenue_month')}月"
                mom_str = f"{mom:+.2f}%"
                yoy_str = f"{yoy:+.2f}%" if yoy is not None else "資料計算中"

                if yoy is not None and yoy > 10:
                    status = "🔥 優於預期 (年增雙位數成長)"
                elif yoy is not None and yoy >= 0:
                    status = "🟢 符合預期 (穩健正成長)"
                elif yoy is not None:
                    status = "🟡 稍低於預期 (年增放緩/衰退)"
                else:
                    status = "🟢 營收動能持平"

                return f"{month_str} | 年增(YoY): {yoy_str} | 月增(MoM): {mom_str}\n   表現評價: {status}"
    except Exception:
        pass
    return "無即時營收數據"

def get_tw_foreign_investor(stock_id):
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date=2024-08-01"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        if data.get("status") == 200 and data.get("data"):
            df = pd.DataFrame(data["data"])
            foreign_df = df[df['name'].str.contains('Foreign|外資|外陸資', case=False, na=False)]
            if not foreign_df.empty:
                latest_date = foreign_df.iloc[-1]['date']
                day_data = foreign_df[foreign_df['date'] == latest_date]
                net_shares = day_data['buy'].sum() - day_data['sell'].sum()
                return round(net_shares / 1000)
    except Exception:
        pass
    return None

def get_us_stock_data(symbol):
    """美股抓取備援機制"""
    # 方案 1: Alpha Vantage
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

    # 方案 2: Stooq / Yahoo 備援
    try:
        stooq_url = f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&i=d"
        df = pd.read_csv(stooq_url)
        if not df.empty and 'Close' in df.columns:
            df = df.sort_values(by='Date').reset_index(drop=True)
            df['Close'] = df['Close'].astype(float)
            df['Volume'] = df['Volume'].astype(float)
            return df, symbol
    except Exception:
        pass

    return None, symbol

def analyze_stock(user_input):
    try:
        stock_code, display_name = resolve_stock_symbol(user_input)
        foreign_net = None
        revenue_info = "美股ETF/股票，無台股營收"
        concept_text = "海外股票 / ETF"
        season_text = "11月~隔年4月 (美股歷史最佳表現月份)"

        if stock_code.isdigit():
            df, target_symbol = get_tw_stock_data(stock_code)
            foreign_net = get_tw_foreign_investor(stock_code)
            revenue_info = get_tw_revenue(stock_code)

            if stock_code in STOCK_CONCEPT_INFO:
                concept_text = STOCK_CONCEPT_INFO[stock_code]["concept"]
                season_text = STOCK_CONCEPT_INFO[stock_code]["season"]
            else:
                concept_text = "上市櫃一般產業股"
                season_text = "11月~3月 (歷史作帳與財報空窗期行情)"
        else:
            df, target_symbol = get_us_stock_data(stock_code)

        if df is None or df.empty:
            return f"找不到 [{user_input}] 的股票資料，請確認名稱或代碼是否正確。"

        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['Hist'] = df['DIF'] - df['MACD']
        df['MA20'] = df['Close'].rolling(window=20).mean()
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

        if foreign_net is not None:
            if foreign_net > 0:
                foreign_text = f"買超 {foreign_net:,} 張"
            elif foreign_net < 0:
                foreign_text = f"賣超 {abs(foreign_net):,} 張"
            else:
                foreign_text = "買賣超 0 張"
        else:
            foreign_text = "無數據 (美股/海外標的)"

        is_break_3pct = diff_pct <= -3.0
        is_two_days_below = (close < ma20) and (prev_close < prev_ma20)

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
            f"📊 {title_display} 全方位分析：\n"
            f"-------------------\n"
            f"最新收盤價: {close:.2f}\n"
            f"20日均線(月線): {ma20:.2f} ({pct_text})\n"
            f"成交量狀態: {vol_status}\n"
            f"外資籌碼動向: {foreign_text}\n"
            f"-------------------\n"
            f"📈 基本面與營收：\n"
            f"   {revenue_info}\n"
            f"🏷️ 概念題材: {concept_text}\n"
            f"🗓️ 歷史旺季行情: {season_text}\n"
            f"-------------------\n"
            f"💡 操作建議：\n{signal}"
        )
    except Exception as e:
        return f"分析發生錯誤: {str(e)}"

if __name__ == "__main__":
    app.run(port=5000)
