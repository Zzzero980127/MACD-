import os
import time
import requests
import pandas as pd
import datetime
import re
import psycopg2
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ----------------------------------------------------
# 環境變數與 LINE Bot 設定
# ----------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '').strip()
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

STOCK_NAME_MAP = {}

# ----------------------------------------------------
# 1. Supabase (PostgreSQL) 資料庫
# ----------------------------------------------------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                date VARCHAR(20) PRIMARY KEY,
                content TEXT
            );
        ''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Supabase Init Error: {e}")

def save_history_to_db(date_str, content_str):
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO history (date, content) VALUES (%s, %s)
            ON CONFLICT (date) DO UPDATE SET content = EXCLUDED.content;
        ''', (date_str, content_str))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Save History Error: {e}")

def get_history_from_db(date_str):
    if not DATABASE_URL:
        return None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT content FROM history WHERE date = %s;', (date_str,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None

init_db()

# ----------------------------------------------------
# 2. 上市 / 上櫃 股票名稱地圖
# ----------------------------------------------------
def load_all_taiwan_stocks():
    global STOCK_NAME_MAP
    headers = {'User-Agent': 'Mozilla/5.0'}

    try:
        url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url_twse, headers=headers, timeout=3)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                s_id = str(item.get("Code", "")).strip()
                s_name = str(item.get("Name", "")).strip().replace(" ", "")
                if s_id.isdigit() and len(s_id) == 4 and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception:
        pass

    try:
        url_tpex = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes"
        res = requests.get(url_tpex, headers=headers, timeout=3)
        if res.status_code == 200 and res.text.strip().startswith('['):
            for item in res.json():
                s_id = str(item.get("SecuritiesCompanyCode", "")).strip()
                s_name = str(item.get("CompanyName", "")).strip().replace(" ", "")
                if s_id.isdigit() and len(s_id) == 4 and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception:
        pass

load_all_taiwan_stocks()

# ----------------------------------------------------
# 3. FinMind 技術面與籌碼 API
# ----------------------------------------------------
def get_tw_stock_data_finmind(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=120)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN:
            url += f"&token={FINMIND_TOKEN}"
        
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
    except Exception:
        pass
    return None

def get_tw_foreign_investor(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN:
            url += f"&token={FINMIND_TOKEN}"

        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=2.5)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == 200 and data.get("data"):
                df = pd.DataFrame(data["data"])
                foreign_df = df[df['name'].str.contains('Foreign|外資', case=False, na=False)]
                if not foreign_df.empty:
                    latest_date = foreign_df.iloc[-1]['date']
                    day_data = foreign_df[foreign_df['date'] == latest_date]
                    net_shares = day_data['buy'].sum() - day_data['sell'].sum()
                    return round(net_shares / 1000)
    except Exception:
        pass
    return 0

def get_tw_stock_revenue(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=400)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN:
            url += f"&token={FINMIND_TOKEN}"

        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=2.5)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == 200 and data.get("data"):
                df = pd.DataFrame(data["data"])
                if 'revenue' in df.columns and len(df) >= 13:
                    df['revenue'] = pd.to_numeric(df['revenue'], errors='coerce')
                    df = df.dropna(subset=['revenue'])
                    
                    latest = df.iloc[-1]
                    prev_month = df.iloc[-2]
                    last_year = df.iloc[-13]

                    rev_latest = float(latest['revenue'])
                    rev_prev = float(prev_month['revenue'])
                    rev_ly = float(last_year['revenue'])

                    rev_date = f"{latest.get('revenue_year', '')}/{str(latest.get('revenue_month', '')).zfill(2)}"

                    yoy_val = ((rev_latest - rev_ly) / rev_ly * 100) if rev_ly > 0 else 0.0
                    mom_val = ((rev_latest - rev_prev) / rev_prev * 100) if rev_prev > 0 else 0.0

                    eval_text = "🟢 強勁成長" if yoy_val > 15 else ("🟢 穩健成長" if yoy_val > 0 else "🔴 營收衰退")
                    return f"{rev_date}月營收 | YoY: {yoy_val:+.2f}% | MoM: {mom_val:+.2f}%\n    評價: {eval_text}"
    except Exception:
        pass
    return "暫無最新月營收資料"

# ----------------------------------------------------
# 4. 單檔股票分析作業（多線程平行計算）
# ----------------------------------------------------
def analyze_candidate(item):
    try:
        code = item['code']
        name = item['name']
        df = get_tw_stock_data_finmind(code)

        if df is not None and len(df) >= 20:
            latest = df.iloc[-1]
            trade_date = str(latest.get('date', '')).strip()

            df['MA20'] = df['Close'].rolling(window=20).mean()
            exp1 = df['Close'].ewm(span=12, adjust=False).mean()
            exp2 = df['Close'].ewm(span=26, adjust=False).mean()
            df['DIF'] = exp1 - exp2
            df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
            df['Hist'] = df['DIF'] - df['MACD']

            prev = df.iloc[-2]
            five_days_ago = df.iloc[-6] if len(df) >= 6 else prev

            close = float(latest['Close'])
            close_5d = float(five_days_ago['Close'])
            ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close

            hist_today = float(latest['Hist'])
            hist_yesterday = float(prev['Hist'])

            gain_5d = ((close - close_5d) / close_5d) * 100
            bias_pct = ((close - ma20) / ma20) * 100

            # 防追高與低位階指標卡關條件
            if (gain_5d <= 15.0) and (-10.0 <= bias_pct <= 10.0) and (hist_today > hist_yesterday):
                foreign_val = get_tw_foreign_investor(code)
                score = (foreign_val * 0.6) + ((10.0 - bias_pct) * 15) + ((hist_today - hist_yesterday) * 40)
                macd_status_text = "綠柱縮短 (空方衰退)" if hist_today < 0 else "紅柱微幅擴張"

                return {
                    'code': code,
                    'name': name,
                    'close': close,
                    'ma20': ma20,
                    'bias_pct': bias_pct,
                    'gain_5d': gain_5d,
                    'foreign_net': foreign_val,
                    'macd_status': macd_status_text,
                    'score': score,
                    'trade_date': trade_date
                }
    except Exception:
        pass
    return None

# ----------------------------------------------------
# 5. 多線程超高速選股引擎 (帶完全防空值機制)
# ----------------------------------------------------
def fast_scan_all_stocks():
    headers = {'User-Agent': 'Mozilla/5.0'}
    candidates = []

    # 1. 抓取上市快照
    try:
        url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res = requests.get(url_twse, headers=headers, timeout=3)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                code = str(item.get("Code", "")).strip()
                name = str(item.get("Name", "")).strip().replace(" ", "")
                raw_close = str(item.get("ClosingPrice", "")).replace(",", "").strip()
                raw_vol = str(item.get("TradeVolume", "")).replace(",", "").strip()

                if code.isdigit() and len(code) == 4 and not code.startswith("00") and raw_close and raw_close != "--":
                    try:
                        close = float(raw_close)
                        vol = float(raw_vol) / 1000
                        if 10 <= close <= 600 and vol >= 1000:
                            candidates.append({'code': code, 'name': name, 'close': close, 'vol': vol})
                    except Exception:
                        pass
    except Exception as e:
        print(f"TWSE Fetch Error: {e}")

    # 2. 抓取上櫃快照
    try:
        url_tpex = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes"
        res = requests.get(url_tpex, headers=headers, timeout=3)
        if res.status_code == 200 and res.text.strip().startswith('['):
            for item in res.json():
                code = str(item.get("SecuritiesCompanyCode", "")).strip()
                name = str(item.get("CompanyName", "")).strip().replace(" ", "")
                raw_close = str(item.get("Close", "")).replace(",", "").strip()
                raw_vol = str(item.get("TradingShares", "")).replace(",", "").strip()

                if code.isdigit() and len(code) == 4 and not code.startswith("00") and raw_close and raw_close != "---":
                    try:
                        close = float(raw_close)
                        vol = float(raw_vol) / 1000
                        if 10 <= close <= 600 and vol >= 1000:
                            candidates.append({'code': code, 'name': name, 'close': close, 'vol': vol})
                    except Exception:
                        pass
    except Exception as e:
        print(f"TPEX Fetch Error: {e}")

    # 3. 備援防空值機制：若全無資料，切換為熱門指標權值庫
    if not candidates:
        top_candidates = [
            {'code': '2330', 'name': '台積電'},
            {'code': '2317', 'name': '鴻海'},
            {'code': '2454', 'name': '聯發科'},
            {'code': '2382', 'name': '廣達'},
            {'code': '3231', 'name': '緯創'},
            {'code': '2308', 'name': '台達電'},
            {'code': '2356', 'name': '英業達'},
            {'code': '6669', 'name': '緯穎'},
            {'code': '3017', 'name': '奇鋐'},
            {'code': '2376', 'name': '技嘉'}
        ]
    else:
        candidates.sort(key=lambda x: x['vol'], reverse=True)
        top_candidates = candidates[:15]

    leaderboard = []
    trade_date = ""

    # 4. 8 個 Thread 多線程平行計算
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(analyze_candidate, top_candidates))

    for r in results:
        if r is not None:
            leaderboard.append(r)
            if not trade_date and r.get('trade_date'):
                trade_date = r['trade_date']

    leaderboard.sort(key=lambda x: x['score'], reverse=True)
    top_5 = leaderboard[:5]

    if top_5 and trade_date:
        history_key = trade_date.replace("-", "")
        save_history_to_db(history_key, format_ai_report(top_5))

    return top_5, trade_date

# ----------------------------------------------------
# 6. LINE Bot 路由與事件
# ----------------------------------------------------
@app.route("/", methods=['GET'])
def index():
    return "TW Stock Bot Active with Multithreaded Engine!"

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Callback Error: {e}")
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        user_input = event.message.text.strip()
        clean_keyword = user_input.upper().replace(" ", "")

        date_match = re.search(r'^(20\d{6})$', clean_keyword)

        if date_match:
            target_date = date_match.group(1)
            history_report = get_history_from_db(target_date)
            if history_report:
                reply_text = f"📜【查閱 ({target_date}) 歷史 AI 選股紀錄】:\n\n" + history_report
            else:
                reply_text = f"⚠️ 找不到 ({target_date}) 的歷史紀錄，請確認日期格式如 20260824"
        elif "選股" in clean_keyword or "AI" in clean_keyword or "潛力股" in clean_keyword:
            top_stocks, trade_date = fast_scan_all_stocks()
            display_date = trade_date.replace("-", "/") if trade_date else datetime.datetime.now().strftime("%Y/%m/%d")
            
            if top_stocks:
                reply_text = f"🎯【{display_date} AI 全台股極速動態 Top 5 總排名】:\n\n" + format_ai_report(top_stocks)
            else:
                reply_text = f"🎯【{display_date} AI 全台股極速動態排名】:\n目前今日無符合嚴格爆發指標的標的，建議觀望！"
        else:
            reply_text = analyze_stock(user_input)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        print(f"Handle Error: {e}")
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 系統選股計算逾時或發生錯誤，請再試一次！")
            )
        except Exception:
            pass

def format_ai_report(top_stocks):
    top_stocks.sort(key=lambda x: x['score'], reverse=True)
    results = []
    for item in top_stocks[:5]:
        card = (
            f"📈 {item['name']} ({item['code']})\n"
            f"  • 收盤價: ${item['close']:.2f} (月線 ${item['ma20']:.1f})\n"
            f"  • 漲幅管控: 🛡️ 近5日 {item['gain_5d']:+.1f}%\n"
            f"  • 位階狀態: 🟢 低位階 (離月線 {item['bias_pct']:+.1f}%)\n"
            f"  • 指標狀態: 📉 MACD {item['macd_status']}\n"
            f"  • 籌碼觀察: 🎯 外資 {item['foreign_net']} 張"
        )
        results.append(card)
    return "\n\n".join(results)

# ----------------------------------------------------
# 7. 個股單獨查詢
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
        df['STD20'] = df['Close'].rolling(window=20).std(ddof=0)
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
            vol_status = f"💥 接近/突破布林上軌 ({close:.2f} >= {bb_upper:.2f})\n  👉 短線過熱，切勿追高"
        elif price_change_pct > 0 and vol_today >= vol_ma5 * 1.15:
            vol_status = f"👆 上漲放量 (+{price_change_pct:.1f}%)\n  👉 多頭攻擊強烈"
        elif price_change_pct < 0 and vol_today >= vol_ma5 * 1.15:
            vol_status = f"👇 下跌放量 ({price_change_pct:.1f}%)\n  👉 注意賣壓風險"
        else:
            vol_status = f"➡️ 價量平穩 ({price_change_pct:+.1f}%)"

        foreign_text = f"{foreign_net:} 張" if foreign_net != 0 else "0 張/估算中"

        if close < ma60 or diff_pct < -3.0:
            signal = "🔴 【建議出場/觀望】跌破關鍵支撐或空頭走勢！"
        elif close >= ma20 and hist_today > hist_yesterday:
            signal = "🔥 【多頭控盤】站穩均線且 MACD 柱狀體升高，可持股或分批佈局。"
        else:
            signal = "🟡 【多短觀望】超越月線軌道，走勢偏溫。"

        pct_text = f"高於月線 {diff_pct:.2f}%" if diff_pct >= 0 else f"低於月線 {abs(diff_pct):.2f}%"

        return (
            f"📊 [{display_name} ({stock_code})] 技術與籌碼分析 :\n"
            f"--------------------\n"
            f"最新收盤價: {close:.2f}\n"
            f"20日均線(月線): {ma20:.2f} ({pct_text})\n"
            f"60日均線(季線): {ma60:.2f}\n"
            f"布林通道上軌: {bb_upper:.2f}\n"
            f"量價結構:\n  {vol_status}\n"
            f"外資籌碼: {foreign_text}\n"
            f"--------------------\n"
            f"📈 基本面與營收 :\n  {revenue_info}\n"
            f"--------------------\n"
            f"💡 操作建議 :\n{signal}"
        )
    except Exception as e:
        return f"⚠️ 分析發生錯誤: {str(e)}"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
