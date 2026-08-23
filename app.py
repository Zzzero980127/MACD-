import os
import sqlite3
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

DB_FILE = 'stock_history.db'
STOCK_NAME_MAP = {}
LEADERBOARD_CACHE = {}
CACHE_LOCK = Lock()

TOTAL_SCANNED = 0
CURRENT_INDEX = 0

# ----------------------------------------------------
# 1. SQLite 資料庫初始化與讀寫
# ----------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            date TEXT PRIMARY KEY,
            content TEXT
        )
    ''')
    conn.commit()
    conn.close()

def save_history_to_db(date_str, content_str):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO history (date, content) VALUES (?, ?)', (date_str, content_str))
        conn.commit()
        conn.close()
    except Exception: pass

def get_history_from_db(date_str):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT content FROM history WHERE date = ?', (date_str,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

init_db()

# ----------------------------------------------------
# 2. 上市 (TWSE) + 上櫃 (TPEx) 股票名稱對照庫
# ----------------------------------------------------
def load_all_taiwan_stocks():
    global STOCK_NAME_MAP
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    # 上市股票 (TWSE)
    try:
        url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url_twse, headers=headers, timeout=3)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                s_id = str(item.get("Code", "")).strip()
                s_name = str(item.get("Name", "")).strip()
                if s_id.isdigit() and len(s_id) == 4 and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception: pass

    # 上櫃股票 (TPEx)
    try:
        url_tpex = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes"
        res = requests.get(url_tpex, headers=headers, timeout=3)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                s_id = str(item.get("SecuritiesCompanyCode", "")).strip()
                s_name = str(item.get("CompanyName", "")).strip()
                if s_id.isdigit() and len(s_id) == 4 and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception: pass

load_all_taiwan_stocks()

# ----------------------------------------------------
# 3. FinMind 數據抓取 (K線、外資、營收)
# ----------------------------------------------------
def get_tw_stock_data_finmind(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=120)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
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

def get_tw_foreign_investor(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={start_date}"
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
    return 0

def get_tw_stock_revenue(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={stock_id}&start_date={start_date}"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=2.5)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == 200 and data.get("data"):
                df = pd.DataFrame(data["data"])
                if not df.empty:
                    latest = df.iloc[-1]
                    rev_date = latest.get('date', '未知')
                    yoy = latest.get('year_ov_year_growth', 0)
                    mom = latest.get('month_ov_month_growth', 0)
                    yoy_val = float(yoy) if yoy is not None else 0.0
                    mom_val = float(mom) if mom is not None else 0.0
                    
                    if yoy_val > 15:
                        eval_text = "🟢 強勁成長"
                    elif yoy_val > 0:
                        eval_text = "🟢 穩健成長"
                    else:
                        eval_text = "🔴 營收衰退"
                    return f"{rev_date} | YoY: {yoy_val:+.2f}% | MoM: {mom_val:+.2f}%\n   評價: {eval_text}"
    except Exception: pass
    return "暫無最新營收數據"

# ----------------------------------------------------
# 4. 背景慢速輪播 (30秒/檔，安全防爆)
# ----------------------------------------------------
def background_stock_scanner():
    global CURRENT_INDEX, TOTAL_SCANNED, LEADERBOARD_CACHE
    if len(STOCK_NAME_MAP) < 300:
        load_all_taiwan_stocks()

    all_stocks = sorted(list(STOCK_NAME_MAP.items()), key=lambda x: x[1])
    if not all_stocks:
        return

    name, code = all_stocks[CURRENT_INDEX]
    CURRENT_INDEX = (CURRENT_INDEX + 1) % len(all_stocks)
    TOTAL_SCANNED += 1

    if code.startswith("00") or len(code) != 4:
        return

    df = get_tw_stock_data_finmind(code)
    if df is None or len(df) < 20:
        return

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
    close_5d = float(five_days_ago['Close'])
    ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close

    hist_today = float(latest['Hist'])
    hist_yesterday = float(prev['Hist'])

    gain_5d = ((close - close_5d) / close_5d) * 100
    bias_pct = ((close - ma20) / ma20) * 100

    if (10 <= close <= 600) and (gain_5d <= 12.0) and (-8.0 <= bias_pct <= 8.0) and (hist_today > hist_yesterday):
        foreign_val = get_tw_foreign_investor(code)
        score = (foreign_val * 0.6) + ((8.0 - bias_pct) * 15) + ((hist_today - hist_yesterday) * 40)
        macd_status_text = "綠柱縮短（空方衰退）" if hist_today < 0 else "紅柱微幅擴張"

        item = {
            'code': code,
            'name': name,
            'close': close,
            'ma20': ma20,
            'bias_pct': bias_pct,
            'gain_5d': gain_5d,
            'foreign_net': foreign_val,
            'macd_status': macd_status_text,
            'score': score
        }

        with CACHE_LOCK:
            LEADERBOARD_CACHE[code] = item
            sorted_all = sorted(LEADERBOARD_CACHE.values(), key=lambda x: x['score'], reverse=True)
            LEADERBOARD_CACHE = {x['code']: x for x in sorted_all[:5]}

            report = format_ai_report(list(LEADERBOARD_CACHE.values()))
            today_str = datetime.datetime.now().strftime("%Y%m%d")
            save_history_to_db(today_str, report)

scheduler = BackgroundScheduler()
scheduler.add_job(func=background_stock_scanner, trigger="interval", seconds=30)
scheduler.start()

# ----------------------------------------------------
# 5. LINE Bot 訊息處理邏輯
# ----------------------------------------------------
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

    date_match = re.search(r'^(20\d{6})$', clean_keyword)

    if date_match:
        target_date = date_match.group(1)
        history_report = get_history_from_db(target_date)
        if history_report:
            reply_text = f"📜 【調閱 {target_date} 歷史 AI 選股紀錄】:\n\n" + history_report
        else:
            reply_text = f"⚠️ 找不到 {target_date} 的歷史紀錄，請確認日期是否正確。"
    elif "選股" in clean_keyword or "AI" in clean_keyword or "潛力股" in clean_keyword:
        reply_text = get_ai_selected_stocks()
    else:
        reply_text = analyze_stock(user_input)

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

def format_ai_report(top_stocks):
    top_stocks.sort(key=lambda x: x['score'], reverse=True)
    results = []
    for item in top_stocks[:3]:
        card = (
            f"🤫 {item['name']} ({item['code']})\n"
            f"   • 收盤價: ${item['close']:.2f} (月線 ${item['ma20']:.1f})\n"
            f"   • 漲幅控管: 🛡️ 近5日 {item['gain_5d']:+.1f}%\n"
            f"   • 位階狀態: 🟢 低位階 (離月線 {item['bias_pct']:+.1f}%)\n"
            f"   • 指標狀態: 📉 MACD {item['macd_status']}\n"
            f"   • 籌碼觀察: 🎯 外資 {item['foreign_net']} 張"
        )
        results.append(card)
    return "\n\n".join(results)

def get_ai_selected_stocks():
    global TOTAL_SCANNED
    with CACHE_LOCK:
        top_stocks = list(LEADERBOARD_CACHE.values())

    if TOTAL_SCANNED < 10 and not top_stocks:
        pct = (TOTAL_SCANNED / 30) * 100
        return (
            f"⏳ 【AI 後台數據庫暖機中】\n"
            f"-------------------\n"
            f"• 當前進度: 已掃描 {TOTAL_SCANNED} / 30 檔基本庫 ({pct:.0f}%)\n"
            f"• 安全機制: 30秒/檔 穩定運算中\n\n"
            f"💡 請再等待約 2~3 分鐘後重新點選「AI選股」！"
        )

    today_str = datetime.datetime.now().strftime("%Y/%m/%d")
    scanned_info = f"(後台已累計掃描 {TOTAL_SCANNED} 檔標的)"
    report_content = format_ai_report(top_stocks)

    if not report_content:
        return f"🎯 【{today_str} AI 選股報告】:\n{scanned_info}\n目前掃描區域暫無符合標的，背景持續輪播中！"

    return f"🎯 【{today_str} AI 全台股背景掃描 Top 3】\n{scanned_info}:\n\n" + report_content

# ----------------------------------------------------
# 6. 個股智慧解析 (含 MACD、布林通道、外資、營收、建議)
# ----------------------------------------------------
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

def analyze_stock(user_input):
    try:
        stock_code, display_name = resolve_stock_symbol(user_input)

        if not stock_code.isdigit() or len(stock_code) != 4:
            return f"⚠️ 找不到「{user_input}」的台股上市或上櫃資料。"

        df = get_tw_stock_data_finmind(stock_code)
        if df is None or df.empty:
            return f"⚠️ 暫時無法取得 [{display_name} ({stock_code})] 的技術數據，請稍後再試。"

        foreign_net = get_tw_foreign_investor(stock_code)
        revenue_info = get_tw_stock_revenue(stock_code)

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

        if close >= bb_upper * 0.98:
            vol_status = f"🚨 接近/突破布林上軌 ({close:.2f} >= {bb_upper:.2f})\n   👉 短線過熱，切勿追高"
        elif price_change_pct > 0 and vol_today >= vol_ma5 * 1.15:
            vol_status = f"🔥 上漲放量 (+{price_change_pct:.1f}%)\n   👉 多頭攻擊強烈"
        elif price_change_pct < 0 and vol_today >= vol_ma5 * 1.15:
            vol_status = f"📉 下跌放量 ({price_change_pct:.1f}%)\n   👉 注意賣壓風險"
        else:
            vol_status = f"➡️ 價量平穩 ({price_change_pct:+.1f}%)"

        foreign_text = f"{foreign_net:,} 張" if foreign_net != 0 else "0 張/結算中"

        if close < ma60 or diff_pct <= -3.0:
            signal = "🔴【建議出場/觀望】跌破關鍵支撐或均線走弱！"
        elif close >= ma20 and hist_today > hist_yesterday:
            signal = "🔥【多頭控盤】站穩均線且 MACD 柱狀體升高，可持股或分批佈局。"
        else:
            signal = "🟢【偏多觀察】站穩月線軌道，走勢穩健。"

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
            f"📈 基本面與營收：\n   {revenue_info}\n"
            f"-------------------\n"
            f"💡 操作建議：\n{signal}"
        )
    except Exception as e:
        return f"分析發生錯誤: {str(e)}"

if __name__ == "__main__":
    app.run(port=5000)
    app.run(port=5000)
