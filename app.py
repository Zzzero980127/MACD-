import os
import random
import requests
import pandas as pd
import datetime
import yfinance as yf
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'oG8A/4QoXPau72qWtFOcV4Hq/Ca+EgcQoJgSMHUjbNPVjtgyGkBeTwdmqfBiEjqBbZLzUn0F70JNtdTgICSrgr T+4NysH5ayUtXj4B+06J6I2DW7BT3ruJHndDuag4zjys1CO836Jwy4fR0oDq6e7wdB04t89/1O/w1cDnyilFU=')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '87cb520a332382036072d72899c94d5b')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 常備對照表
STOCK_NAME_MAP = {}

def load_all_taiwan_stocks():
    """直連 TWSE 與 TPEx 官方 OpenAPI 建立全台股對照表"""
    global STOCK_NAME_MAP
    headers = {'User-Agent': 'Mozilla/5.0'}

    try:
        url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url_twse, headers=headers, timeout=5)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                s_id = str(item.get("Code", "")).strip()
                s_name = str(item.get("Name", "")).strip()
                if s_id and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception: pass

    try:
        url_tpex = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes"
        res_tpex = requests.get(url_tpex, headers=headers, timeout=5)
        if res_tpex.status_code == 200 and isinstance(res_tpex.json(), list):
            for item in res_tpex.json():
                s_id = str(item.get("SecuritiesCompanyCode") or item.get("SecuritiesCode") or "").strip()
                s_name = str(item.get("CompanyName") or item.get("SecuritiesName") or "").strip()
                if s_id and s_name and len(s_id) == 4 and s_id.isdigit():
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception: pass

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

    try:
        search_url = f"https://api.cnyes.com/media/api/v1/search?keyword={user_input}"
        res = requests.get(search_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
        if res.status_code == 200:
            items = res.json().get('items', {}).get('data', [])
            for item in items:
                code = str(item.get('code', '')).strip()
                if code.isdigit() and len(code) == 4:
                    STOCK_NAME_MAP[user_input] = code
                    return code, user_input
    except Exception: pass

    return clean_input, clean_input

def get_tw_stock_data(stock_id):
    if not stock_id.isdigit():
        return None, stock_id

    for suffix in [".TWO", ".TW"]:
        try:
            ticker = f"{stock_id}{suffix}"
            yf_obj = yf.Ticker(ticker)
            df = yf_obj.history(period="3m")
            if not df.empty and len(df) >= 20:
                df = df.reset_index()
                df = df.rename(columns={'Close': 'Close', 'Volume': 'Volume'})
                df['Close'] = df['Close'].astype(float)
                df['Volume'] = df['Volume'].astype(float)
                return df, ticker
        except Exception: continue

    return None, stock_id

def get_tw_revenue(stock_id):
    start_date = (datetime.datetime.now() - datetime.timedelta(days=400)).strftime("%Y-%m-%d")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={stock_id}&start_date={start_date}"
    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
        if res.status_code == 200:
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

                    status = "🟢 穩健成長" if (yoy and yoy > 0) else "🟡 整理/調整中"
                    return f"{month_str} | YoY: {yoy_str} | MoM: {mom_str}\n   評價: {status}", yoy
    except Exception: pass
    return "數據更新中", None

def get_tw_foreign_investor(stock_id):
    start_date = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={start_date}"
    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == 200 and data.get("data"):
                df = pd.DataFrame(data["data"])
                foreign_df = df[df['name'].str.contains('Foreign|外資|外陸資', case=False, na=False)]
                if not foreign_df.empty:
                    latest_date = foreign_df.iloc[-1]['date']
                    day_data = foreign_df[foreign_df['date'] == latest_date]
                    net_shares = day_data['buy'].sum() - day_data['sell'].sum()
                    return round(net_shares / 1000)
    except Exception: pass
    return None

def screen_undervalued_stocks():
    """【真・全市場動態掃描】徹底刪除固定備援清單，採用隨機抽樣確保每日/每次結果完全不同"""
    headers = {'User-Agent': 'Mozilla/5.0'}
    quotes_data = []

    # 1. 取得當前記憶體中的全台股代碼，並進行隨機洗牌 (Shuffle)
    all_codes = list(STOCK_NAME_MAP.items())
    random.shuffle(all_codes)

    # 限制每次取 150 檔進行即時精密運算（避免 Line 機器人回應逾時）
    sample_pool = all_codes[:150]

    tier1_candidates = []

    for name, code in sample_pool:
        # 過濾掉 ETF (00開頭)
        if code.startswith("00") or len(code) != 4:
            continue

        df, _ = get_tw_stock_data(code)
        if df is None or len(df) < 25:
            continue

        # 計算均線、布林通道與 MACD
        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['STD20'] = df['Close'].rolling(window=20).std()
        df['BB_Upper'] = df['MA20'] + (df['STD20'] * 2)

        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['Hist'] = df['DIF'] - df['MACD']

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        five_days_ago = df.iloc[-6] if len(df) >= 6 else prev

        close = float(latest['Close'])
        prev_close = float(prev['Close'])
        close_5d = float(five_days_ago['Close'])
        ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close
        bb_upper = float(latest['BB_Upper']) if not pd.isna(latest['BB_Upper']) else close

        hist_today = float(latest['Hist'])
        hist_yesterday = float(prev['Hist'])

        # --- 真・低位階防追高門檻 ---
        # 1. 股價落在 15 ~ 200 元之間（過濾垃圾股與超高價股）
        if not (15 <= close <= 200):
            continue

        # 2. 5日內累積漲幅 <= 3.0%（嚴禁噴出爆漲股票）
        gain_5d = ((close - close_5d) / close_5d) * 100
        if gain_5d > 3.0:
            continue

        # 3. 當天漲幅 <= 1.5%（不追當天拉長紅）
        gain_1d = ((close - prev_close) / prev_close) * 100
        if gain_1d > 1.5:
            continue

        # 4. 離月線乖離率在 -4.0% ~ +1.5%（底部位階）
        bias_pct = ((close - ma20) / ma20) * 100
        if not (-4.0 <= bias_pct <= 1.5):
            continue

        # 5. 離布林上軌相當遠（低於上軌 90% 價格）
        if close > bb_upper * 0.90:
            continue

        # 6. MACD 柱狀體升高（綠柱縮短或紅柱生成，空方衰退）
        if hist_today <= hist_yesterday:
            continue

        # 7. 外資籌碼買超 (> 0 張)
        foreign_net = get_tw_foreign_investor(code)
        if foreign_net is not None and foreign_net > 0:
            macd_status_text = "綠柱縮短（空方衰退）" if hist_today < 0 else "紅柱初生/微擴張"

            tier1_candidates.append({
                'code': code,
                'name': name,
                'close': close,
                'ma20': ma20,
                'bias_pct': bias_pct,
                'gain_5d': gain_5d,
                'foreign_net': foreign_net,
                'macd_status': macd_status_text
            })

        if len(tier1_candidates) >= 5:
            break

    results = []
    for item in tier1_candidates:
        card = (
            f"🤫 {item['name']} ({item['code']})\n"
            f"   • 收盤價: ${item['close']:.2f} (月線 ${item['ma20']:.1f})\n"
            f"   • 漲幅控管: 🛡️ 近5日微幅 {item['gain_5d']:+.1f}% (徹底未爆發/低位打底)\n"
            f"   • 位階狀態: 🟢 低位階 (離月線 {item['bias_pct']:+.1f}%)\n"
            f"   • 指標狀態: 📉 MACD {item['macd_status']}\n"
            f"   • 籌碼觀察: 🎯 外資進場買超 {item['foreign_net']:,} 張"
        )
        results.append(card)

    if results:
        return "🎯 【全市場隨機精選：未爆發低位股 + MACD空方衰退 + 外資買超 Top 5】:\n\n" + "\n\n".join(results)

    return "⚠️ 隨機抽樣池中暫未符合條件標的，請再輸入一次「AI選股」進行重新掃描。"

def analyze_stock(user_input):
    try:
        stock_code, display_name = resolve_stock_symbol(user_input)

        if not stock_code.isdigit():
            return f"⚠️ 找不到「{user_input}」的台股資料。\n您可以嘗試直接輸入 4 位數代碼查詢。"

        df, target_symbol = get_tw_stock_data(stock_code)

        if df is None or df.empty:
            return f"⚠️ 暫時無法取得 [{display_name} ({stock_code})] 的技術數據，請稍後再試。"

        foreign_net = get_tw_foreign_investor(stock_code)
        revenue_info, _ = get_tw_revenue(stock_code)

        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['Hist'] = df['DIF'] - df['MACD']

        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA60'] = df['Close'].rolling(window=60).mean()
        df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()
        df['STD20'] = df['Close'].rolling(window=20).std()
        df['BB_Upper'] = df['MA20'] + (df['STD20'] * 2)

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        close = float(latest['Close'])
        prev_close = float(prev['Close'])
        ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close
        ma60 = float(latest['MA60']) if not pd.isna(latest['MA60']) else close
        bb_upper = float(latest['BB_Upper']) if not pd.isna(latest['BB_Upper']) else close

        hist_today = float(latest['Hist'])
        hist_yesterday = float(prev['Hist'])

        diff_pct = ((close - ma20) / ma20) * 100 if ma20 != 0 else 0

        vol_today = float(latest['Volume'])
        vol_ma5 = float(latest['Vol_MA5']) if not pd.isna(latest['Vol_MA5']) else vol_today

        price_change_pct = ((close - prev_close) / prev_close) * 100
        is_vol_expand = vol_today >= vol_ma5 * 1.15
        is_vol_shrink = vol_today <= vol_ma5 * 0.85
        is_touch_bb_upper = close >= (bb_upper * 0.98)

        if is_touch_bb_upper:
            vol_status = f"🚨 接近/突破布林上軌 ({close:.2f} >= {bb_upper:.2f})\n   👉 短線過熱，切勿盲目追高！"
        elif price_change_pct > 0 and is_vol_expand:
            vol_status = f"🔥 上漲放量 (+{price_change_pct:.1f}%)\n   👉 多頭攻擊強烈"
        elif price_change_pct < 0 and is_vol_expand:
            vol_status = f"📉 下跌放量 ({price_change_pct:.1f}%)\n   👉 注意大戶賣壓與續跌風險"
        elif price_change_pct > 0 and is_vol_shrink:
            vol_status = f"⚠️ 上漲量縮 (+{price_change_pct:.1f}%)\n   👉 量價背離，提防高位拉回"
        elif price_change_pct < 0 and is_vol_shrink:
            vol_status = f"🛡️ 下跌量縮 ({price_change_pct:.1f}%)\n   👉 賣壓沉寂，容易迎來止跌反彈"
        else:
            vol_status = f"➡️ 價量平穩 ({price_change_pct:+.1f}%)"

        if foreign_net is not None:
            foreign_text = f"買超 {foreign_net:,} 張" if foreign_net > 0 else (f"賣超 {abs(foreign_net):,} 張" if foreign_net < 0 else "買賣超 0 張")
        else:
            foreign_text = "籌碼結算中"

        if close < ma60 or diff_pct <= -3.0:
            signal = "🔴【建議出場/觀望】跌破關鍵支撐或均線走弱！"
        elif is_touch_bb_upper:
            signal = "⚠️【擇優減碼】股價推升至布林上軌過熱區，注意拉回。"
        elif close >= ma20 and hist_today > hist_yesterday:
            signal = "🔥【多頭控盤】站穩均線且 MACD 柱狀體升高，可持股或分批佈局。"
        elif close >= ma20:
            signal = "🟢【偏多觀察】站穩月線軌道，走勢穩健。"
        else:
            signal = "⚪【觀望為主】多空方向未定。"

        pct_text = f"高於月線 {diff_pct:.2f}%" if diff_pct >= 0 else f"跌破月線 {abs(diff_pct):.2f}%"
        title_display = f"{display_name} ({stock_code})" if display_name != stock_code else target_symbol

        return (
            f"📊 {title_display} 技術與籌碼分析：\n"
            f"-------------------\n"
            f"最新收盤價: {close:.2f}\n"
            f"20日均線(月線): {ma20:.2f} ({pct_text})\n"
            f"60日均線(季線): {ma60:.2f}\n"
            f"布林通道上軌: {bb_upper:.2f}\n"
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
