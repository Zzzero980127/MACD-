import os
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import datetime
import psycopg2

# 🎯 FinMind Token (環境變數優先，否則使用預設)
HARDCODED_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoic2t5bGdkc0BnbWFpbC5jb20iLCJlbWFpbCI6InNreWxnZHNAZ21haWwuY29tIiwidG9rZW5fdmVyc2lvbiI6Mn0.QZb8bF7wtOVTB4GKr0gjm90pBagTHU4J7DMMLRNPu0E"

ENV_TOKEN = os.environ.get('FINMIND_TOKEN', '').strip()
FINMIND_TOKEN = ENV_TOKEN if len(ENV_TOKEN) > 20 else HARDCODED_TOKEN

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_USER_ID = os.environ.get('LINE_USER_ID', '').strip()

def create_robust_session():
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http = create_robust_session()

def get_db_connection():
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception: return None

def save_to_db(report_text, date_str="LATEST"):
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                date VARCHAR(20) PRIMARY KEY,
                content TEXT NOT NULL
            );
        ''')
        cursor.execute('''
            INSERT INTO history (date, content)
            VALUES (%s, %s)
            ON CONFLICT (date) DO UPDATE SET content = EXCLUDED.content;
        ''', (date_str, report_text))
        conn.commit()
        cursor.close()
        conn.close()
        print(f"✅ 已成功將 {date_str} 的選股報告寫入 PostgreSQL！", flush=True)
    except Exception as e:
        print(f"❌ 寫入資料庫失敗: {e}", flush=True)

def push_line_message(report_text):
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("⚠️ 未設定 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID，跳過主動推播。", flush=True)
        return
    
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": report_text}]
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        if res.status_code == 200:
            print("✅ 成功發送 LINE 主動推播訊息！", flush=True)
        else:
            print(f"❌ LINE 推播失敗 ({res.status_code}): {res.text}", flush=True)
    except Exception as e:
        print(f"❌ 發送 LINE 推播發生異常: {e}", flush=True)

def safe_get_finmind(dataset, stock_id, start_date):
    """ 100% 純 Token 請求，遇 402 等待重試 """
    url = f"https://api.finmindtrade.com/api/v4/data?dataset={dataset}&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
    
    for attempt in range(3):
        try:
            res = http.get(url, timeout=8.0)
            if res.status_code == 200 and res.json().get("data"):
                return res
            elif res.status_code == 402:
                print(f"  └─ ⚠️ Token 額度超限或觸發頻率限制(HTTP 402)，冷卻 6 秒重試 ({attempt+1}/3)...", flush=True)
                time.sleep(6.0)
            else:
                print(f"  └─ ⚠️ API 異常 (HTTP {res.status_code})", flush=True)
                break
        except Exception as e:
            print(f"  └─ ⚠️ 連線異常: {e}", flush=True)
            time.sleep(2.0)

    return None

def fetch_finmind_data(stock_info, current_idx, total_count):
    stock_id = stock_info["code"]
    stock_name = stock_info["name"]
    
    start_date = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    
    # 1. 抓取 K 線資料 (純 Token)
    res_p = safe_get_finmind("TaiwanStockPrice", stock_id, start_date)

    if not res_p or not res_p.json().get("data"):
        print(f"⌛ [{current_idx}/{total_count}] {stock_id} {stock_name} | ❌ [Token模式] K線抓取失敗", flush=True)
        return None
    
    print(f"⌛ [{current_idx}/{total_count}] {stock_id} {stock_name} | 🔑 [Token模式] K線資料取得成功！", flush=True)

    df = pd.DataFrame(res_p.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
    df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
    df = df.dropna(subset=['Close'])
    
    if len(df) < 35: return None

    exp1 = df['Close'].ewm(span=12, adjust=False).mean()
    exp2 = df['Close'].ewm(span=26, adjust=False).mean()
    df['DIF'] = exp1 - exp2
    df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
    df['OSC'] = df['DIF'] - df['MACD']

    osc_today = float(df.iloc[-1]['OSC'])
    osc_prev = float(df.iloc[-2]['OSC'])

    is_green_shrinking = (osc_today < 0) and (osc_today > osc_prev)
    is_first_red = (osc_today > 0) and (osc_prev <= 0)

    if not (is_green_shrinking or is_first_red):
        return None
    
    macd_status = "📉 綠柱縮短(空退)" if is_green_shrinking else "💥 紅柱第1天(起漲)"

    time.sleep(1.0)

    chip_start = (datetime.datetime.now() - datetime.timedelta(days=12)).strftime("%Y-%m-%d")
    
    # 2. 抓取外資籌碼 (純 Token)
    res_c = safe_get_finmind("TaiwanStockInstitutionalInvestorsBuySell", stock_id, chip_start)

    if res_c and res_c.json().get("data"):
        df_c = pd.DataFrame(res_c.json()["data"])
        foreign_df = df_c[df_c['name'].str.contains('Foreign|外資', case=False, na=False)].copy()
        if not foreign_df.empty:
            foreign_df['net_buy'] = (foreign_df['buy'] - foreign_df['sell']) / 1000
            daily_summary = foreign_df.groupby('date')['net_buy'].sum().reset_index().sort_values('date')
            
            if len(daily_summary) >= 2:
                today_foreign = float(daily_summary.iloc[-1]['net_buy'])
                prev_foreign = float(daily_summary.iloc[-2]['net_buy'])
                
                is_turn_to_buy = (today_foreign > 50) and (prev_foreign <= 50)
                is_continuous_buy = (today_foreign > 200) and (prev_foreign > 200)

                if is_turn_to_buy or is_continuous_buy:
                    status_label = "🔄 外資由賣轉買" if is_turn_to_buy else "🔥 外資連買加碼"
                    close_price = float(df.iloc[-1]['Close'])
                    prev_close = float(df.iloc[-2]['Close'])
                    pct_change = ((close_price - prev_close) / prev_close) * 100

                    print(f"  └─ 🎯 符合標的: [{stock_id} {stock_name}] {macd_status} | {status_label}", flush=True)
                    return {
                        "code": stock_id,
                        "name": stock_name,
                        "close": close_price,
                        "pct": pct_change,
                        "foreign_shares": round(today_foreign),
                        "foreign_label": status_label,
                        "macd_status": macd_status
                    }

    return None

def run_precalculation():
    print("🚀 開始 Top 200 選股與自動推播任務...", flush=True)
    
    twse_url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    candidates = []
    try:
        res = http.get(twse_url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            stocks = []
            for item in data:
                code = item.get("Code", "").strip()
                name = item.get("Name", "").strip()
                if len(code) == 4 and code.isdigit():
                    try:
                        vol = int(item.get("TradeVolume", 0))
                        stocks.append({"code": code, "name": name, "volume": vol})
                    except ValueError: continue
            
            df_stocks = pd.DataFrame(stocks).sort_values(by="volume", ascending=False)
            top_200 = df_stocks.head(200).to_dict('records')
            print(f"✅ 第一階段完成，鎖定 Top 200 成交量個股！", flush=True)
            candidates = top_200
    except Exception as e:
        print(f"❌ 證交所 API 抓取失敗: {e}", flush=True)
        return

    print(f"🔍 第二階段：開始針對 Top 200 進行 MACD 轉折與籌碼驗證...", flush=True)
    selected_stocks = []
    total_count = len(candidates)
    
    for i, stock_info in enumerate(candidates, 1):
        res = fetch_finmind_data(stock_info, i, total_count)
        if res:
            selected_stocks.append(res)
        time.sleep(1.2)

    selected_stocks.sort(key=lambda x: x['foreign_shares'], reverse=True)

    today_str = datetime.datetime.now().strftime('%Y%m%d')
    date_display = datetime.datetime.now().strftime('%Y/%m/%d')

    if not selected_stocks:
        report = f"📅 【AI 今日 Top200 轉折起漲精選】({date_display})\n--------------------\n今日 Top200 熱門股中，未有符合「MACD轉折/起漲 + 外資進場」之個股。"
    else:
        lines = [f"🔥 【AI 精選：Top200 轉折起漲股】({date_display})", "════════════════════"]
        for item in selected_stocks:
            lines.append(
                f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"   👉 {item['macd_status']}\n"
                f"   👉 {item['foreign_label']}: {item['foreign_shares']:+} 張\n"
                f"────────────────────"
            )
        lines.append("💡 篩選核心：Top200 成交量 + MACD綠柱縮短/剛轉紅柱 + 外資突破性買超。")
        report = "\n".join(lines)

    save_to_db(report, "LATEST")
    save_to_db(report, today_str)

    push_line_message(report)

    print("🎉 200 檔選股、寫入 DB 與 LINE 推播皆順利完成！", flush=True)

if __name__ == "__main__":
    run_precalculation()
