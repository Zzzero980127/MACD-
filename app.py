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

# 熱門標的硬編碼備援庫（防止政府 API 連線逾時導致空字典）
BACKUP_STOCK_MAP = {
    "台積電": "2330", "鴻海": "2317", "聯發科": "2454", "富邦金": "2881", "國泰金": "2882",
    "廣達": "2382", "緯創": "3231", "華航": "2610", "長榮航": "2618", "健策": "3653",
    "寶雅": "5904", "信驊": "5274", "鈊象": "3293", "雙鴻": "3324", "奇鋐": "3017",
    "萬潤": "6187", "台燿": "6274", "聯詠": "3034", "世芯": "3661", "創意": "3443"
}

STOCK_NAME_MAP = dict(BACKUP_STOCK_MAP)

def update_stock_name_map():
    """從上市與上櫃開放資料自動更新名稱對照表"""
    global STOCK_NAME_MAP
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    # 上市股票清單
    try:
        url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url_twse, headers=headers, timeout=5)
        if res.status_code == 200:
            for item in res.json():
                s_id = item.get("Code")
                s_name = item.get("Name")
                if s_id and s_name:
                    STOCK_NAME_MAP[s_name.strip()] = s_id.strip()
    except Exception as e:
        print(f"TWSE Fetch Error: {e}")

    # 上櫃股票清單
    try:
        url_tpex = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes"
        res = requests.get(url_tpex, headers=headers, timeout=5)
        if res.status_code == 200:
            for item in res.json():
                s_id = item.get("SecuritiesCompanyCode")
                s_name = item.get("CompanyName")
                if s_id and s_name:
                    STOCK_NAME_MAP[s_name.strip()] = s_id.strip()
    except Exception as e:
        print(f"TPEx Fetch Error: {e}")

update_stock_name_map()

@app.route("/", methods=['GET'])
def index():
    return 'TW Stock Bot is running alive!'

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
    clean_input = user_input.upper().replace(".TW", "").replace(".TWO", "").strip()
    
    # 1. 直接輸入數字代碼
    if clean_input.isdigit():
        name = [k for k, v in STOCK_NAME_MAP.items() if v == clean_input]
        stock_name = name[0] if name else clean_input
        return clean_input, stock_name

    # 2. 精確比對中文名稱
    if user_input in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[user_input], user_input

    # 3. 模糊比對中文名稱
    for name, code in STOCK_NAME_MAP.items():
        if user_input in name or name in user_input:
            return code, name

    # 4. 查無資料時，嘗試重新從線上更新字典再查一次
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
                
                mom = ((rev_now - rev_prev) / rev_prev * 100) if rev_prev > 0 else 0
                
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
                    status = "🔥 優於預期 (年增雙位數)"
                elif yoy is not None and yoy >= 0:
                    status = "🟢 符合預期 (穩健成長)"
                elif yoy is not None:
                    status = "🟡 稍低於預期 (衰退/放緩)"
                else:
                    status = "🟢 動能持平"

                return f"{month_str} | YoY: {yoy_str} | MoM: {mom_str}\n   評價: {status}"
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

def analyze_stock(user_input):
    try:
        stock_code, display_name = resolve_stock_symbol(user_input)

        if not stock_code.isdigit():
            return f"⚠️ 找不到「{user_input}」的台股資料。\n您可以改輸入代碼（如 5904 寶雅、5274 信驊）進行查詢。"

        df, target_symbol = get_tw_stock_data(stock_code)
        if df is None or df.empty:
            return f"找不到代碼 [{stock_code}] 的台股價格數據，請確認代碼是否正確。"

        foreign_net = get_tw_foreign_investor(stock_code)
        revenue_info = get_tw_revenue(stock_code)

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
        prev_close = float(prev['Close'])
        ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close
        prev_ma20 = float(prev['MA20']) if not pd.isna(prev['MA20']) else prev_close

        hist_today = float(latest['Hist'])
        hist_yesterday = float(prev['Hist'])

        diff_pct = ((close - ma20) / ma20) * 100 if ma20 != 0 else 0

        vol_today = float(latest['Volume'])
        vol_ma5 = float(latest['Vol_MA5']) if not pd.isna(latest['Vol_MA5']) else vol_today
        
        price_change_pct = ((close - prev_close) / prev_close) * 100
        is_price_up = price_change_pct > 0
        is_price_down = price_change_pct < 0

        is_vol_expand = vol_today >= vol_ma5 * 1.15
        is_vol_shrink = vol_today <= vol_ma5 * 0.85

        if is_price_up and is_vol_expand:
            vol_status = f"🔥 上漲放量 (+{price_change_pct:.1f}%)\n   👉 多頭攻擊強烈，追價意願高"
        elif is_price_down and is_vol_expand:
            vol_status = f"📉 下跌放量 ({price_change_pct:.1f}%)\n   👉 恐慌盤湧出/大戶拋售，注意續跌風險"
        elif is_price_up and is_vol_shrink:
            vol_status = f"⚠️ 上漲量縮 (+{price_change_pct:.1f}%)\n   👉 量價背離！買盤停滯，需防範【見頂回落】"
        elif is_price_down and is_vol_shrink:
            vol_status = f"🛡️ 下跌量縮 ({price_change_pct:.1f}%)\n   👉 賣壓沉寂/惜售，極可能接近【見底反彈】"
        else:
            vol_status = f"➡️ 價量平穩 ({price_change_pct:+.1f}%)\n   👉 量能無明顯變化"

        if foreign_net is not None:
            if foreign_net > 0:
                foreign_text = f"買超 {foreign_net:,} 張"
            elif foreign_net < 0:
                foreign_text = f"賣超 {abs(foreign_net):,} 張"
            else:
                foreign_text = "買賣超 0 張"
        else:
            foreign_text = "查無即時外資數據"

        is_break_3pct = diff_pct <= -3.0
        is_two_days_below = (close < ma20) and (prev_close < prev_ma20)

        if is_break_3pct or is_two_days_below:
            reasons = []
            if is_break_3pct:
                reasons.append(f"跌破月線 {abs(diff_pct):.2f}%")
            if is_two_days_below:
                reasons.append("連2日低於月線")
            signal = f"🔴【建議出場/停損】{' & '.join(reasons)}，趨勢轉弱！"

        elif close < ma20:
            signal = "🟡【警戒觀望】微幅低於月線，趨勢偏弱，建議先觀望。"

        elif hist_today > 0 and hist_today >= hist_yesterday:
            signal = "🔥【多頭續抱/加碼】強勢站穩月線且 MACD 紅柱擴大，多方控盤！"

        elif hist_today > 0 and hist_today < hist_yesterday:
            signal = "🟢【偏多持有】站穩月線上，但多頭力道稍緩，建議續抱。"

        elif hist_today < 0 and abs(hist_today) < abs(hist_yesterday):
            signal = "🟢【試買建倉】站穩月線且空方力道減弱，可考慮建立分批試買單。"

        else:
            signal = "⚪【盤整觀望】多空力道均衡，建議靜待方向確立。"

        pct_text = f"高於月線 {diff_pct:.2f}%" if diff_pct >= 0 else f"跌破月線 {abs(diff_pct):.2f}%"
        title_display = f"{display_name} ({stock_code})" if display_name != stock_code else target_symbol

        return (
            f"📊 {title_display} 技術與籌碼分析：\n"
            f"-------------------\n"
            f"最新收盤價: {close:.2f}\n"
            f"20日均線(月線): {ma20:.2f} ({pct_text})\n"
            f"量價結構:\n   {vol_status}\n"
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
