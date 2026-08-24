import os
import requests
import pandas as pd
import datetime
import psycopg2
from concurrent.futures import ThreadPoolExecutor

FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def save_history_to_db(date_str, content_str):
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
        cursor.execute('''
            INSERT INTO history (date, content) VALUES (%s, %s)
            ON CONFLICT (date) DO UPDATE SET content = EXCLUDED.content;
        ''', (date_str, content_str))
        conn.commit()
        cursor.close()
        conn.close()
        print(f"✅ [{date_str}] AI選股數據已成功寫入 Supabase 資料庫！")
    except Exception as e:
        print(f"❌ DB Save Error: {e}")

def get_tw_stock_data(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=120)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN:
            url += f"&token={FINMIND_TOKEN}"
        
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
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

        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
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

def analyze_candidate(item):
    try:
        code = item['code']
        name = item['name']
        df = get_tw_stock_data(code)

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
            prev_close = float(prev['Close'])
            close_5d = float(five_days_ago['Close'])
            ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close

            hist_today = float(latest['Hist'])
            hist_yesterday = float(prev['Hist'])

            gain_5d = ((close - close_5d) / close_5d) * 100
            bias_pct = ((close - ma20) / ma20) * 100
            foreign_val = get_tw_foreign_investor(code)

            # --- 動態防禦評分 ---
            score = 100.0
            if foreign_val < 0:
                score -= abs(foreign_val) * 0.1
                if foreign_val < -2000:
                    score -= 50
            else:
                score += (foreign_val * 0.05)

            if close < prev_close:
                score -= 25
            if gain_5d > 12.0:
                score -= (gain_5d - 12.0) * 5
            if abs(bias_pct) > 8.0:
                score -= (abs(bias_pct) - 8.0) * 4
            if hist_today > hist_yesterday:
                score += 15
            else:
                score -= 10

            macd_status_text = "綠柱縮短 (空方衰退)" if hist_today < 0 else ("紅柱擴張" if hist_today > hist_yesterday else "紅柱縮短")

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

def run_precalculation():
    print("🚀 開始執行全台股大數據掃描 (前 100 名)...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    raw_list = []

    # 1. 抓取上市
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", headers=headers, timeout=5)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                code = str(item.get("Code", "")).strip()
                name = str(item.get("Name", "")).strip().replace(" ", "")
                raw_close = str(item.get("ClosingPrice", "")).replace(",", "").strip()
                raw_vol = str(item.get("TradeVolume", "")).replace(",", "").strip()
                if code.isdigit() and len(code) == 4 and not code.startswith("00") and raw_close and raw_close != "--":
                    try:
                        raw_list.append({'code': code, 'name': name, 'vol': float(raw_vol) / 1000.0})
                    except Exception: pass
    except Exception as e: print(f"TWSE Error: {e}")

    # 2. 抓取上櫃
    try:
        res = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes", headers=headers, timeout=5)
        if res.status_code == 200 and res.text.strip().startswith('['):
            for item in res.json():
                code = str(item.get("SecuritiesCompanyCode", "")).strip()
                name = str(item.get("CompanyName", "")).strip().replace(" ", "")
                raw_close = str(item.get("Close", "")).replace(",", "").strip()
                raw_vol = str(item.get("TradingShares", "")).replace(",", "").strip()
                if code.isdigit() and len(code) == 4 and not code.startswith("00") and raw_close and raw_close != "---":
                    try:
                        raw_list.append({'code': code, 'name': name, 'vol': float(raw_vol) / 1000.0})
                    except Exception: pass
    except Exception as e: print(f"TPEX Error: {e}")

    # 3. 取前 100 名成交量最高的大池子 (完全涵蓋航運、主流與熱門股)
    raw_list.sort(key=lambda x: x['vol'], reverse=True)
    top_100_candidates = raw_list[:100]

    # 4. 背景平行運算
    leaderboard = []
    trade_date = ""
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(analyze_candidate, top_100_candidates))

    for r in results:
        if r is not None:
            leaderboard.append(r)
            if not trade_date and r.get('trade_date'):
                trade_date = r['trade_date']

    leaderboard.sort(key=lambda x: x['score'], reverse=True)
    top_5 = leaderboard[:5]

    if top_5 and trade_date:
        # 組成文字報告
        report_cards = []
        for item in top_5:
            card = (
                f"📈 {item['name']} ({item['code']})\n"
                f"  • 收盤價: ${item['close']:.2f} (月線 ${item['ma20']:.1f})\n"
                f"  • 漲幅管控: 🛡️ 近5日 {item['gain_5d']:+.1f}%\n"
                f"  • 位階狀態: 🟢 低位階 (離月線 {item['bias_pct']:+.1f}%)\n"
                f"  • 指標狀態: 📉 MACD {item['macd_status']}\n"
                f"  • 籌碼觀察: 🎯 外資 {item['foreign_net']} 張"
            )
            report_cards.append(card)
        
        final_content = "\n\n".join(report_cards)
        
        # 存進資料庫 (以日期與 LATEST 作為 Key)
        date_key = trade_date.replace("-", "")
        save_history_to_db(date_key, final_content)
        save_history_to_db("LATEST", f"【{trade_date.replace('-', '/')} 最新計算結果】\n\n" + final_content)

if __name__ == "__main__":
    run_precalculation()
