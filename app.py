import os
import random
import requests
import pandas as pd
import datetime
import re
from threading import Lock
from flask import Flask, request, abort
from apscheduler.schedulers.background import BackgroundScheduler
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# LINE 憑證設定
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'oG8A/4QoXPau72qWtFOcV4Hq/Ca+EgcQoJgSMHUjbNPVjtgyGkBeTwdmqfBiEjqBbZLzUn0F70JNtdTgICSrgr T+4NysH5ayUtXj4B+06J6I2DW7BT3ruJHndDuag4zjys1CO836Jwy4fR0oDq6e7wdB04t89/1O/w1cDnyilFU=').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '87cb520a332382036072d72899c94d5b').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

STOCK_NAME_MAP = {}
LEADERBOARD_CACHE = {}
CACHE_LOCK = Lock()
CURRENT_CHECK_INDEX = 0

def load_all_taiwan_stocks():
    global STOCK_NAME_MAP
    headers = {'User-Agent': 'Mozilla/5.0'}

    # 1. FinMind
    try:
        url = "https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo"
        res = requests.get(url, headers=headers, timeout=3)
        if res.status_code == 200:
            for item in res.json().get("data", []):
                s_id = str(item.get("stock_id", "")).strip()
                s_name = str(item.get("stock_name", "")).strip()
                if s_id.isdigit() and len(s_id) == 4 and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception: pass

    # 2. TWSE
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url, headers=headers, timeout=3)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                s_id = str(item.get("Code", "")).strip()
                s_name = str(item.get("Name", "")).strip()
                if s_id.isdigit() and len(s_id) == 4 and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception: pass

    # 3. TPEx
    try:
        url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes"
        res = requests.get(url, headers=headers, timeout=3)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                s_id = str(item.get("SecuritiesCompanyCode", "")).strip()
                s_name = str(item.get("CompanyName", "")).strip()
                if s_id.isdigit() and len(s_id) == 4 and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception: pass

load_all_taiwan_stocks()

def get_tw_stock_data_finmind(stock_id, end_date_str=None):
    if end_date_str:
        end_dt = datetime.datetime.strptime(end_date_str, "%Y%m%d")
        start_date = (end_dt - datetime.timedelta(days=120)).strftime("%Y-%m-%d")
        end_date = end_dt.strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}&end_date={end_date}"
    else:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=120)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"

    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=2.5)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == 200 and data.get("data"):
                df = pd.DataFrame(data["data"])
                df = df.rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
                df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
                df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
                df = df.dropna(subset=['Close'])
                if len(df) >= 20:
                    return df
    except Exception: pass
    return None

def get_tw_foreign_investor(stock_id, end_date_str=None):
    if end_date_str:
        end_dt = datetime.datetime.strptime(end_date_str, "%Y%m%d")
        start_date = (end_dt - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        end_date = end_dt.strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={start_date}&end_date={end_date}"
    else:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={start_date}"

    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=2)
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

# 背景定時全台股掃描任務
def background_stock_scanner():
    global CURRENT_CHECK_INDEX, LEADERBOARD_CACHE
    if len(STOCK_NAME_MAP) < 300:
        load_all_taiwan_stocks()

    all_stocks = sorted(list(STOCK_NAME_MAP.items()), key=lambda x: x[1])
    if not all_stocks:
        return

    batch_size = 20
    batch = all_stocks[CURRENT_CHECK_INDEX:CURRENT_CHECK_INDEX + batch_size]
    CURRENT_CHECK_INDEX = (CURRENT_CHECK_INDEX + batch_size) % len(all_stocks)

    batch_candidates = []
    for name, code in batch:
        if code.startswith("00") or len(code) != 4:
            continue

        df = get_tw_stock_data_finmind(code)
        if df is None or len(df) < 20:
            continue

        df['MA20'] = df['Close'].rolling(window=20).mean()
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

        hist_today = float(latest['Hist'])
        hist_yesterday = float(prev['Hist'])

        gain_5d = ((close - close_5d) / close_5d) * 100
        bias_pct = ((close - ma20) / ma20) * 100

        if (10 <= close <= 600) and (gain_5d <= 10.0) and (-8.0 <= bias_pct <= 6.0) and (hist_today > hist_yesterday):
            foreign_net = get_tw_foreign_investor(code)
            foreign_val = foreign_net if foreign_net is not None else 0
            
            score = (foreign_val * 0.6) + ((6.0 - bias_pct) * 15) + ((hist_today - hist_yesterday) * 40)
            macd_status_text = "綠柱縮短（空方衰退）" if hist_today < 0 else "紅柱微幅擴張"
            
            batch_candidates.append({
                'code': code,
                'name': name,
                'close': close,
                'ma20': ma20,
                'bias_pct': bias_pct,
                'gain_5d': gain_5d,
                'foreign_net': foreign_val,
                'macd_status': macd_status_text,
                'score': score
            })

    with CACHE_LOCK:
        for item in batch_candidates:
            LEADERBOARD_CACHE[item['code']] = item

        sorted_all = sorted(LEADERBOARD_CACHE.values(), key=lambda x: x['score'], reverse=True)
        LEADERBOARD_CACHE = {x['code']: x for x in sorted_all[:5]}

scheduler = BackgroundScheduler()
scheduler.add_job(func=background_stock_scanner, trigger="interval", seconds=25)
scheduler.start()

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

    # 檢查是否含有 8 位數日期 (如 20260815)
    date_match = re.search(r'20\d{6}', clean_keyword)
    target_date = date_match.group(0) if date_match else None

    if "選股" in clean_keyword or "AI" in clean_keyword or "潛力股" in clean_keyword or target_date:
        if target_date:
            reply_text = get_historical_ai_stocks(target_date)
        else:
            reply_text = get_ai_selected_stocks()
    else:
        reply_text = analyze_stock(user_input)

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

# 1. 未帶日期：秒回當前全台股掃描 Top 5
def get_ai_selected_stocks():
    with CACHE_LOCK:
        top_stocks = list(LEADERBOARD_CACHE.values())

    if not top_stocks:
        return "⚡ AI 系統正在背景掃描全台股資料庫，預計 30 秒內完成首輪統計，請稍後再次點選！"

    top_stocks.sort(key=lambda x: x['score'], reverse=True)

    results = []
    for item in top_stocks:
        card = (
            f"🤫 {item['name']} ({item['code']})\n"
            f"   • 收盤價: ${item['close']:.2f} (月線 ${item['ma20']:.1f})\n"
            f"   • 漲幅控管: 🛡️ 近5日 {item['gain_5d']:+.1f}%\n"
            f"   • 位階狀態: 🟢 低位階 (離月線 {item['bias_pct']:+.1f}%)\n"
            f"   • 指標狀態: 📉 MACD {item['macd_status']}\n"
            f"   • 籌碼觀察: 🎯 外資 {item['foreign_net']} 張"
        )
        results.append(card)

    today_str = datetime.datetime.now().strftime("%Y/%m/%d")
    return f"🎯 【{today_str} 全台股背景連掃即時 Top {len(results)}】:\n\n" + "\n\n".join(results)

# 2. 帶歷史日期：進行歷史時間膠囊運算
def get_historical_ai_stocks(query_date):
    if len(STOCK_NAME_MAP) < 300:
        load_all_taiwan_stocks()

    all_stocks = sorted(list(STOCK_NAME_MAP.items()), key=lambda x: x[1])
    if not all_stocks:
        return "⚠️ 資料庫初始化中，請稍後再試。"

    # 使用查詢日期作為亂數種子，進行抽樣運算
    rng = random.Random(query_date)
    shuffled_pool = list(all_stocks)
    rng.shuffle(shuffled_pool)

    candidates = []
    for name, code in shuffled_pool[:25]:
        if code.startswith("00") or len(code) != 4:
            continue

        df = get_tw_stock_data_finmind(code, query_date)
        if df is None or len(df) < 20:
            continue

        df['MA20'] = df['Close'].rolling(window=20).mean()
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

        hist_today = float(latest['Hist'])
        hist_yesterday = float(prev['Hist'])

        gain_5d = ((close - close_5d) / close_5d) * 100
        bias_pct = ((close - ma20) / ma20) * 100

        if (10 <= close <= 600) and (gain_5d <= 10.0) and (-8.0 <= bias_pct <= 6.0) and (hist_today > hist_yesterday):
            foreign_net = get_tw_foreign_investor(code, query_date)
            foreign_val = foreign_net if foreign_net is not None else 0
            
            score = (foreign_val * 0.6) + ((6.0 - bias_pct) * 15) + ((hist_today - hist_yesterday) * 40)
            macd_status_text = "綠柱縮短（空方衰退）" if hist_today < 0 else "紅柱微幅擴張"
            
            candidates.append({
                'code': code,
                'name': name,
                'close': close,
                'ma20': ma20,
                'bias_pct': bias_pct,
                'gain_5d': gain_5d,
                'foreign_net': foreign_val,
                'macd_status': macd_status_text,
                'score': score
            })

    if not candidates:
        return f"⚠️ 基準日 [{query_date}] 盤面無符合篩選標準的標的，請更換日期嘗試。"

    candidates.sort(key=lambda x: x['score'], reverse=True)
    top_candidates = candidates[:3]

    results = []
    for item in top_candidates:
        card = (
            f"🤫 {item['name']} ({item['code']})\n"
            f"   • 收盤價: ${item['close']:.2f} (月線 ${item['ma20']:.1f})\n"
            f"   • 漲幅控管: 🛡️ 近5日 {item['gain_5d']:+.1f}%\n"
            f"   • 位階狀態: 🟢 低位階 (離月線 {item['bias_pct']:+.1f}%)\n"
            f"   • 指標狀態: 📉 MACD {item['macd_status']}\n"
            f"   • 籌碼觀察: 🎯 外資 {item['foreign_net']} 張"
        )
        results.append(card)

    return f"📜 【{query_date} 歷史選股回測選單 Top {len(top_candidates)}】:\n\n" + "\n\n".join(results)

def resolve_stock_symbol(user_input):
    if len(STOCK_NAME_MAP) < 300:
        load_all_taiwan_stocks()

    clean_input = user_input.upper().replace(".TW", "").replace(".TWO", "").replace(" ", "").strip()

    if clean_input.isdigit() and len(clean_input) == 4:
        name = [k for k, v in STOCK_NAME_MAP.items() if v == clean_input]
        return clean_input, name[0] if name else clean_input

    if clean_input in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[clean_input], clean_input

    for name, code in STOCK_NAME_MAP.items():
        if clean_input in name or name in clean_input:
            return code, name

    return clean_input, clean_input

def get_tw_revenue(stock_id):
    start_date = (datetime.datetime.now() - datetime.timedelta(days=400)).strftime("%Y-%m-%d")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={stock_id}&start_date={start_date}"
    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=2)
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
                    return f"{month_str} | YoY: {yoy_str} | MoM: {mom_str}\n   評價: {status}"
    except Exception: pass
    return "數據更新中"

def analyze_stock(user_input):
    try:
        stock_code, display_name = resolve_stock_symbol(user_input)

        if not stock_code.isdigit() or len(stock_code) != 4:
            return f"⚠️ 找不到「{user_input}」的台股資料。"

        df = get_tw_stock_data_finmind(stock_code)
        if df is None or df.empty:
            return f"⚠️ 暫時無法取得 [{display_name} ({stock_code})] 的技術數據。"

        foreign_net = get_tw_foreign_investor(stock_code)
        revenue_info = get_tw_revenue(stock_code)

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

        return (
            f"📊 {display_name} ({stock_code}) 技術與籌碼分析：\n"
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
