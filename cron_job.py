import os
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import datetime
import psycopg2

ENV_TOKEN = os.environ.get('FINMIND_TOKEN', '').strip()
HARDCODED_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..." # ⚠️ 確保此處或環境變數有填入真 Token
FINMIND_TOKEN = ENV_TOKEN if len(ENV_TOKEN) > 20 else HARDCODED_TOKEN
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

def create_robust_session():
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
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

def fetch_finmind_data(stock_info):
    """ stock_info 為字典 {"code": "2330", "name": "台積電"} """
    stock_id = stock_info["code"]
    stock_name = stock_info["name"]
    
    start_date = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    price_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
    
    try:
        res_p = http.get(price_url, timeout=8.0)
        if res_p.status_code != 200 or not res_p.json().get("data"):
            return None
        
        df = pd.DataFrame(res_p.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
        df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
        df = df.dropna(subset=['Close'])
        
        if len(df) < 35: return None

        # 計算 MACD
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

        # 外資籌碼驗證
        time.sleep(0.8)
        chip_start = (datetime.datetime.now() - datetime.timedelta(days=12)).strftime("%Y-%m-%d")
        chip_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={chip_start}&token={FINMIND_TOKEN}"
        
        res_c = http.get(chip_url, timeout=8.0)
        if res_c.status_code == 200 and res_c.json().get("data"):
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

                        return {
                            "code": stock_id,
                            "name": stock_name,
                            "close": close_price,
                            "pct": pct_change,
                            "foreign_shares": round(today_foreign),
                            "foreign_label": status_label,
                            "macd_status": macd_status
                        }
    except Exception as e:
        print(f"  └─ ⚠️ [{stock_id} {stock_name}] 分析異常: {e}", flush=True)

    return None

def run_precalculation():
    print("🚀 開始 200 檔安全選股任務 (含中文名稱對照)...", flush=True)
    
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
            top_200 = df_stocks.head(200).to_dict('records') # 包含 code 與 name 的字典串列
            print(f"✅ 第一階段完成，鎖定 Top 200 個股及其中文名稱！", flush=True)
            candidates = top_200
    except Exception as e:
        print(f"❌ 證交所 API 抓取失敗: {e}", flush=True)
        return

    print(f"🔍 第二階段：開始針對 Top 200 進行深度驗證...", flush=True)
    selected_stocks = []
    
    for i, stock_info in enumerate(candidates, 1):
        res = fetch_finmind_data(stock_info)
        if res:
            print(f"  └─ 🎯 符合標的: [{res['code']} {res['name']}] {res['macd_status']} | {res['foreign_label']}", flush=True)
            selected_stocks.append(res)
        
        time.sleep(1.2)

    selected_stocks.sort(key=lambda x: x['foreign_shares'], reverse=True)

    today_str = datetime.datetime.now().strftime('%Y%m%d')
    date_display = datetime.datetime.now().strftime('%Y/%m/%d')

    if not selected_stocks:
        report = f"📅 【AI 今日 Top200 轉折起漲精選】({date_display})\n--------------------\n今日 Top200 熱門股中，未有符合「MACD轉折/起漲 + 外資進場」之個股。"
    else:
        lines = [f"🔥 【AI 精選：Top200 底部轉折 + 外資加碼股】({date_display})", "--------------------"]
        for item in selected_stocks:
            lines.append(
                f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"   👉 {item['macd_status']} | {item['foreign_label']}: {item['foreign_shares']:+} 張"
            )
        lines.append("--------------------")
        lines.append("💡 篩選核心：Top200 成交量 + MACD綠柱縮短/剛轉紅柱 + 外資突破性買超。")
        report = "\n".join(lines)

    save_to_db(report, "LATEST")
    save_to_db(report, today_str)
    print("🎉 200 檔轉折選股分析完畢，已成功帶中文名稱寫入 DB！", flush=True)

if __name__ == "__main__":
    run_precalculation()
