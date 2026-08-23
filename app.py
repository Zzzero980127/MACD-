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

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

STOCK_NAME_MAP = {}
STOCK_INDUSTRY_MAP = {}

def load_all_taiwan_stocks():
    global STOCK_NAME_MAP, STOCK_INDUSTRY_MAP
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        # 使用證交所開放資料，避免 API 封鎖
        url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                s_id = str(item.get("Code", "")).strip()
                s_name = str(item.get("Name", "")).strip()
                if s_id and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
                    
        # 補充櫃買中心（上櫃）資料
        url_tpex = "https://www.tpex.org.tw/openapi/v1/mopsfront/t187ap03_O"
        res_tpex = requests.get(url_tpex, headers=headers, timeout=5)
        if res_tpex.status_code == 200 and isinstance(res_tpex.json(), list):
            for item in res_tpex.json():
                s_id = str(item.get("SecuritiesCompanyCode", "")).strip()
                s_name = str(item.get("CompanyName", "")).strip()
                if s_id and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception as e:
        print(f"Stock Info Load Error: {e}")

load_all_taiwan_stocks()

@app.route("/", methods=['GET'])
def index():
    return 'TW Stock Bot Active!'

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
    user_input = event.message.text.strip()
    clean_keyword = user_input.upper().replace(" ", "")
    
    if clean_keyword in ["AI選股", "選股", "潛力股", "AI選股推薦"]:
        reply_text = screen_undervalued_stocks()
    else:
        reply_text = analyze_stock(user_input)
        
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

def resolve_stock_symbol(user_input):
    clean_input = user_input.upper().replace(".TW", "").replace(".TWO", "").replace(" ", "").strip()
    if clean_input.isdigit():
        name = [k for k, v in STOCK_NAME_MAP.items() if v == clean_input]
        stock_name = name[0] if name else clean_input
        return clean_input, stock_name

    if user_input in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[user_input], user_input

    for name, code in STOCK_NAME_MAP.items():
        if user_input in name or name in user_input:
            return code, name

    return clean_input, clean_input

def get_tw_stock_data(stock_id):
    # 改為抓取最少需要的日數 (start_date 取最近兩個月即可計算 MA20/MACD)
    import datetime
    start_date = (datetime.datetime.now() - datetime.timedelta(days=70)).strftime("%Y-%m-%d")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=4)
        data = res.json()
        if data.get("status") == 200 and len(data.get("data", [])) >= 15:
            df = pd.DataFrame(data["data"])
            df = df.rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
            df['Close'] = df['Close'].astype(float)
            df['Volume'] = df['Volume'].astype(float)
            return df, f"{stock_id}.TW"
    except Exception:
        pass
    return None, stock_id

def get_tw_revenue(stock_id):
    import datetime
    start_date = (datetime.datetime.now() - datetime.timedelta(days=400)).strftime("%Y-%m-%d")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={stock_id}&start_date={start_date}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=3)
        data = res.json()
        if data.get("status") == 200 and data.get("data"):
            df = pd.DataFrame(data["data"])
            df['revenue'] = pd.to_numeric(df['revenue'], errors='coerce')
            valid_df = df[df['revenue'] > 0].copy()
            
            if len(valid_df) >= 2:
                latest = valid_df.iloc[-1]
                prev = valid_df.iloc[-2]
                rev_now = float(latest['revenue'])
                rev_prev = float(prev['revenue'])
                
                mom = ((rev_now - rev_prev) / rev_prev) * 100
                yoy = None
                if len(valid_df) >= 12:
                    last_year = valid_df.iloc[-12]
                    rev_ly = float(last_year['revenue'])
                    if rev_ly > 0:
                        yoy = ((rev_now - rev_ly) / rev_ly) * 100

                month_str = f"{latest.get('revenue_year')}/{latest.get('revenue_month')}月"
                mom_str = f"{mom:+.2f}%"
                yoy_str = f"{yoy:+.2f}%" if yoy is not None else "計算中"

                if yoy is not None and yoy > 10:
                    status = "🔥 優於預期 (年增雙位數)"
                elif yoy is not None and yoy >= 0:
                    status = "🟢 符合預期 (穩健成長)"
                else:
                    status = "🟡 稍低於預期 (放緩/整理)"

                return f"{month_str} | YoY: {yoy_str} | MoM: {mom_str}\n   評價: {status}", yoy
    except Exception:
        pass
    return "無即時營收數據", None

def get_tw_foreign_investor(stock_id):
    import datetime
    start_date = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={start_date}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=3)
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

def screen_undervalued_stocks():
    headers = {'User-Agent': 'Mozilla/5.0'}
    all_quotes = []

    # 1. 直接從證交所開放資料抓取今日所有台股交易資料（零 API 限制）
    try:
        url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url_twse, headers=headers, timeout=5)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                code = item.get('Code', '')
                if code.isdigit() and not code.startswith("00"):
                    try:
                        close_p = float(str(item.get('ClosingPrice', '0')).replace(',', ''))
                        vol = int(str(item.get('TradeVolume', '0')).replace(',', '')) // 1000
                        if close_p > 0:
                            all_quotes.append({
                                'code': code,
                                'name': item.get('Name', '').strip(),
                                'close': close_p,
                                'volume': vol
                            })
                    except: continue
    except Exception as e:
        print(f"TWSE OpenData Error: {e}")

    if not all_quotes:
        return "⚠️ 目前證交所資料庫維護中，請稍後再試。"

    # 依成交量排序，取熱門前 15 檔以極速計算，避免 LINE 逾時
    all_quotes.sort(key=lambda x: x['volume'], reverse=True)
    top_targets = all_quotes[:15]

    results = []
    for item in top_targets:
        code = item['code']
        df, _ = get_tw_stock_data(code)
        if df is None or len(df) < 15:
            continue

        close = float(df.iloc[-1]['Close'])
        ma20 = float(df['Close'].tail(20).mean())
        
        foreign_net = get_tw_foreign_investor(code)
        foreign_str = f"外資買超 {foreign_net:,} 張" if (foreign_net and foreign_net > 0) else "外資觀望/賣超"

        status = "🔥 強勢多頭 (站穩月線)" if close >= ma20 else "📉 震盪整理"

        results.append(
            f"🤫 {item['name']} ({code})\n"
            f"   • 收盤價: ${close:.2f} (月線 ${ma20:.1f})\n"
            f"   • 走勢狀態: {status}\n"
            f"   • 籌碼動態: {foreign_str}"
        )

    if results:
        return "🎯 【今日熱門人氣/籌碼精選 Top 5】:\n\n" + "\n\n".join(results[:5])
    
    return "⚠️ 資料更新中，請稍後再試。"

def analyze_stock(user_input):
    try:
        stock_code, display_name = resolve_stock_symbol(user_input)

        if not stock_code.isdigit():
            return f"⚠️ 找不到「{user_input}」的台股資料。\n您可以輸入名稱或代碼（如 2915 潤泰全、8463 潤泰材）查詢。"

        df, target_symbol = get_tw_stock_data(stock_code)
        if df is None or df.empty:
            return f"⚠️ 暫時無法取得 [{display_name} ({stock_code})] 的即時行情，請稍後 1 分鐘再試。"

        foreign_net = get_tw_foreign_investor(stock_code)
        revenue_info, _ = get_tw_revenue(stock_code)

        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['Hist'] = df['DIF'] - df['MACD']

        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()
        df['STD20'] = df['Close'].rolling(window=20).std()
        df['BB_Upper'] = df['MA20'] + (df['STD20'] * 2)

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        close = float(latest['Close'])
        prev_close = float(prev['Close'])
        ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close
        bb_upper = float(latest['BB_Upper']) if not pd.isna(latest['BB_Upper']) else close

        diff_pct = ((close - ma20) / ma20) * 100 if ma20 != 0 else 0
        price_change_pct = ((close - prev_close) / prev_close) * 100

        if foreign_net is not None:
            foreign_text = f"買超 {foreign_net:,} 張" if foreign_net > 0 else (f"賣超 {abs(foreign_net):,} 張" if foreign_net < 0 else "買賣超 0 張")
        else:
            foreign_text = "籌碼結算中"

        if close >= ma20:
            signal = "🟢【偏多觀察】站穩月線軌道，走勢穩健。"
        else:
            signal = "🔴【建議觀望】位於月線下方，短線偏弱。"

        pct_text = f"高於月線 {diff_pct:.2f}%" if diff_pct >= 0 else f"跌破月線 {abs(diff_pct):.2f}%"

        return (
            f"📊 {display_name} ({stock_code}) 技術與籌碼分析：\n"
            f"-------------------\n"
            f"最新收盤價: {close:.2f}\n"
            f"20日均線(月線): {ma20:.2f} ({pct_text})\n"
            f"布林通道上軌: {bb_upper:.2f}\n"
            f"當日漲跌: {price_change_pct:+.2f}%\n"
            f"外資籌碼: {foreign_text}\n"
            f"-------------------\n"
            f"📈 基本面與營收：\n"
            f"   {revenue_info}\n"
            f"-------------------\n"
            f"💡 操作建議：\n{signal}"
        )
    except Exception as e:
        return f"分析發生錯誤: {str(e)}"

if __name__ == "__main__":
    app.run(port=5000)
