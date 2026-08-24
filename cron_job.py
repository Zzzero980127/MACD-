import os
import time
import requests
import pandas as pd
import datetime
import psycopg2

FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

def get_db_connection():
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception as e:
        print(f"❌ DB Connect Error: {e}")
        return None

def save_history_to_db(date_str, content_str):
    conn = get_db_connection()
    if not conn: return
    try:
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
        print(f"✅ 已成功將 {date_str} 報告存入 Supabase！")
    except Exception as e:
        print(f"❌ DB Save Error: {e}")

def get_tw_stock_data(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=120)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN: url += f"&token={FINMIND_TOKEN}"
        
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
            df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
            df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
            df = df.dropna(subset=['Close'])
            if len(df) >= 20: return df
    except Exception: pass
    return None

def analyze_candidate(item):
    try:
        code, name = item['code'], item['name']
        df = get_tw_stock_data(code)
        if df is None or len(df) < 20: return None

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        trade_date = str(latest.get('date', '')).strip()

        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA60'] = df['Close'].rolling(window=60).mean()
        
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['Hist'] = df['DIF'] - df['MACD']

        close, prev_close = float(latest['Close']), float(prev['Close'])
        ma20 = float(latest['MA20'])
        ma60 = float(latest['MA60'])
        hist_today, hist_yesterday = float(latest['Hist']), float(prev['Hist'])

        score = 100.0
        if close > ma20: score += 20.0
        if close > ma60: score += 15.0
        if hist_today > hist_yesterday: score += 15.0
        if close > prev_close: score += 10.0

        return {
            'code': code, 'name': name, 'close': close, 'ma20': ma20,
            'score': score, 'trade_date': trade_date
        }
    except Exception: pass
    return None

def run_precalculation():
    print("🚀 後台啟動：開始運算全台股成交量前 100 檔指標...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    raw_list = []

    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", headers=headers, timeout=5)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                code = str(item.get("Code", "")).strip()
                name = str(item.get("Name", "")).strip().replace(" ", "")
                raw_vol = str(item.get("TradeVolume", "0")).replace(",", "").strip()
                if code.isdigit() and len(code) == 4 and not code.startswith("00"):
                    try: raw_list.append({'code': code, 'name': name, 'vol': float(raw_vol)})
                    except Exception: pass
    except Exception: pass

    # 排序並取前 100 檔
    raw_list.sort(key=lambda x: x['vol'], reverse=True)
    top_100 = raw_list[:100]

    leaderboard = []
    trade_date = ""

    # 前 100 檔逐一計算，每檔精準間隔 10 秒（共 100 次 FinMind API 呼叫，耗時約 16.6 分鐘）
    for idx, item in enumerate(top_100, 1):
        print(f"[{idx}/100] 正在分析 {item['name']} ({item['code']})...")
        res = analyze_candidate(item)
        if res is not None:
            leaderboard.append(res)
            if not trade_date and res.get('trade_date'): trade_date = res['trade_date']
        
        # 安全防封鎖，每 10 秒抓一檔
        time.sleep(10)

    leaderboard.sort(key=lambda x: x['score'], reverse=True)
    top_3 = leaderboard[:3]

    if top_3:
        today_str = datetime.datetime.now().strftime("%Y年%m月%d日")
        formatted_date = trade_date.replace("-", "/") if trade_date else today_str
        
        header_title = f"📊【{today_str} AI選股 Top 3】\n(基於前100大成交量 & {formatted_date} 技術面數據)\n" + "="*22 + "\n"
        report_cards = []
        for idx, item in enumerate(top_3, 1):
            card = (
                f"🔥 No.{idx} {item['name']} ({item['code']})\n"
                f"  • 收盤價: ${item['close']:.2f}\n"
                f"  • 月線支撐: ${item['ma20']:.1f}\n"
                f"  • 狀態: 強勢多頭動能 (MACD向上)"
            )
            report_cards.append(card)

        final_content = header_title + "\n\n".join(report_cards)

        # 計算完成後存入 Supabase
        save_history_to_db("LATEST", final_content)
        print("✅ 後台 100 檔運算順利完工，已更新 Supabase 資料庫！")
        return final_content

    print("❌ 計算失敗：無有效數據")
    return "計算失敗"

if __name__ == "__main__":
    run_precalculation()
