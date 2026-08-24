import os
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import datetime
import psycopg2

FINMIND_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjo2t5bGdkc0BnWFpc5jb20iLCJlbWFpbCI6InRewXnZHNAZ21haWWuY29tIwidG9rZW5_fdmVyc2lvbiI6MH0.ebdFVr_Wfwo_Cm3ZnxZolvZGxfmXkywJJv8Y19gngCk".strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

# 自動重試 Session（僅防止 API 逾時/限流斷線，不變動任何選股資料）
def create_robust_session():
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http = create_robust_session()

def get_top_100_volume_stocks():
    """ 保持原本邏輯：動態抓取證交所 (TWSE) 當日成交量前 100 名 """
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    print("🔍 正在從臺灣證券交易所抓取今日成交量前 100 名個股...", flush=True)
    try:
        res = http.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            stocks = []
            for item in data:
                code = item.get("Code", "").strip()
                if len(code) == 4 and code.isdigit():
                    try:
                        trade_volume = int(item.get("TradeVolume", 0))
                        stocks.append({"code": code, "volume": trade_volume})
                    except ValueError:
                        continue
            
            df_stocks = pd.DataFrame(stocks).sort_values(by="volume", ascending=False)
            top_100 = df_stocks.head(100)["code"].tolist()
            print(f"✅ 成功獲取今日成交量前 100 名個股！", flush=True)
            return top_100
    except Exception as e:
        print(f"⚠️ 抓取證交所全市場數據失敗: {e}", flush=True)
    
    return []

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

def fetch_stock_price_with_retry(stock_id):
    """ 保持原本邏輯：抓日 K 價量數據（僅加入 HTTP 重試機制） """
    start_date = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    url_token = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
    try:
        res = http.get(url_token, timeout=4.0)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
            df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
            df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
            df = df.dropna(subset=['Close'])
            if len(df) >= 35: return df
    except Exception: pass

    url_public = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
    try:
        res = http.get(url_public, timeout=4.0)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
            df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
            df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
            df = df.dropna(subset=['Close'])
            if len(df) >= 35: return df
    except Exception: pass

    return None

def fetch_foreign_investor_with_retry(stock_id):
    """ 保持原本邏輯：抓取外資籌碼，精準判斷『由賣轉買』 """
    start_date = (datetime.datetime.now() - datetime.timedelta(days=12)).strftime("%Y-%m-%d")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
    
    try:
        res = http.get(url, timeout=4.0)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"])
            foreign_df = df[df['name'].str.contains('Foreign|外資', case=False, na=False)].copy()
            if not foreign_df.empty:
                foreign_df['net_buy'] = (foreign_df['buy'] - foreign_df['sell']) / 1000
                daily_summary = foreign_df.groupby('date')['net_buy'].sum().reset_index()
                daily_summary = daily_summary.sort_values('date')
                
                if len(daily_summary) >= 2:
                    today_foreign = float(daily_summary.iloc[-1]['net_buy'])
                    prev_foreign = float(daily_summary.iloc[-2]['net_buy'])
                    
                    is_turn_to_buy = (today_foreign > 50) and (prev_foreign <= 50)
                    is_continuous_buy = (today_foreign > 200) and (prev_foreign > 200)

                    if is_turn_to_buy or is_continuous_buy:
                        status_label = "🔄 外資由賣轉買" if is_turn_to_buy else "🔥 外資連買加碼"
                        return True, round(today_foreign), status_label
                    else:
                        return False, round(today_foreign), ""
    except Exception: pass

    return False, 0, ""

def analyze_single_stock(stock_id):
    """ 保持原本邏輯：選股條件濾網 """
    df = fetch_stock_price_with_retry(stock_id)
    if df is None: return None

    df['MA5'] = df['Close'].rolling(5).mean()
    df['MA20'] = df['Close'].rolling(20).mean()
    df['MA60'] = df['Close'].rolling(min(60, len(df))).mean()
    df['Vol_MA5'] = df['Volume'].rolling(5).mean()
    
    df['STD20'] = df['Close'].rolling(20).std(ddof=0)
    df['BB_Upper'] = df['MA20'] + (df['STD20'] * 2)

    exp1 = df['Close'].ewm(span=12, adjust=False).mean()
    exp2 = df['Close'].ewm(span=26, adjust=False).mean()
    df['DIF'] = exp1 - exp2
    df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
    df['OSC'] = df['DIF'] - df['MACD']

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(latest['Close'])
    ma20, ma60 = float(latest['MA20']), float(latest['MA60'])
    vol_today, vol_ma5 = float(latest['Volume']), float(latest['Vol_MA5'])
    osc_today = float(latest['OSC'])
    bb_upper = float(latest['BB_Upper'])

    is_bull_trend = (close > ma20) and (ma20 >= ma60)
    is_macd_good = (osc_today > 0)
    is_vol_surge = (vol_today >= vol_ma5 * 1.1)
    not_overheated = (close < bb_upper * 0.99)

    if is_bull_trend and is_macd_good and is_vol_surge and not_overheated:
        time.sleep(0.15)
        has_foreign_signal, foreign_shares, foreign_label = fetch_foreign_investor_with_retry(stock_id)
        
        if has_foreign_signal:
            pct_change = ((close - float(prev['Close'])) / float(prev['Close'])) * 100
            return {
                "code": stock_id,
                "close": close,
                "pct": pct_change,
                "foreign_shares": foreign_shares,
                "foreign_label": foreign_label
            }
    return None

def run_precalculation():
    print("🚀 開始執行「成交量 Top 100 動態抓取 + 外資由賣轉買」選股任務...", flush=True)
    
    target_stocks = get_top_100_volume_stocks()
    selected_stocks = []
    
    for i, stock_id in enumerate(target_stocks, 1):
        print(f"[{i}/{len(target_stocks)}] 正在分析熱門股 {stock_id}...", flush=True)
        res = analyze_single_stock(stock_id)
        if res:
            selected_stocks.append(res)
        time.sleep(0.25)

    selected_stocks.sort(key=lambda x: x['foreign_shares'], reverse=True)

    today_str = datetime.datetime.now().strftime('%Y%m%d')
    date_display = datetime.datetime.now().strftime('%Y/%m/%d')

    if not selected_stocks:
        report = f"📅 【AI 今日熱門股外資轉買精選】({date_display})\n--------------------\n今日成交量前100熱門股中，未有符合「多頭技術面 + 外資由賣轉買」之個股，建議多看少做。"
    else:
        lines = [f"🔥 【AI 精選：成交量爆量 + 外資轉買股】({date_display})", "--------------------"]
        for item in selected_stocks:
            lines.append(
                f"🔹 {item['code']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"   👉 {item['foreign_label']}: {item['foreign_shares']:+} 張"
            )
        lines.append("--------------------")
        lines.append("💡 篩選核心：當日成交量 Top100 + 站穩月線 + MACD紅柱 + 外資突破性買超。")
        report = "\n".join(lines)

    save_to_db(report, "LATEST")
    save_to_db(report, today_str)
    print("🎉 動態成交量選股運算完畢並已成功寫入 DB！", flush=True)

if __name__ == "__main__":
    run_precalculation()
