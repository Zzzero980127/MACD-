import os
import time
import requests
import pandas as pd
import datetime
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from linebot import LineBotApi
from linebot.models import TextSendMessage

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_USER_ID = os.environ.get('LINE_USER_ID', '').strip()
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None

def save_history_to_db(date_str, content_str):
    try:
        conn = sqlite3.connect("stock_cache.db")
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                date TEXT PRIMARY KEY,
                content TEXT
            );
        ''')
        cursor.execute('''
            INSERT OR REPLACE INTO history (date, content) VALUES (?, ?);
        ''', (date_str, content_str))
        conn.commit()
        conn.close()
        print(f"✅ 已成功將 {date_str} 本地資料庫寫入完成！")
    except Exception as e:
        print(f"❌ SQLite Save Error: {e}")

def get_tw_stock_data(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN: url += f"&token={FINMIND_TOKEN}"
        
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
            df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
            df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
            df = df.dropna(subset=['Close'])
            if len(df) >= 15: return df
    except Exception: pass
    return None

def analyze_candidate(item):
    try:
        code, name = item['code'], item['name']
        df = get_tw_stock_data(code)
        if df is None or len(df) < 15: return None

        latest = df.iloc[-1]
        trade_date = str(latest.get('date', '')).strip()

        df['MA20'] = df['Close'].rolling(window=15, min_periods=1).mean()
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['Hist'] = df['DIF'] - df['MACD']

        prev = df.iloc[-2]
        close, prev_close = float(latest['Close']), float(prev['Close'])
        ma20 = float(latest['MA20'])
        hist_today, hist_yesterday = float(latest['Hist']), float(prev['Hist'])

        score = 100.0
        if close > ma20: score += 20.0
        if hist_today > hist_yesterday: score += 15.0
        if close > prev_close: score += 10.0

        return {
            'code': code, 'name': name, 'close': close, 'ma20': ma20,
            'score': score, 'trade_date': trade_date
        }
    except Exception: pass
    return None

def run_precalculation():
    print("🚀 開始執行台股精選運算...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    raw_list = []

    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", headers=headers, timeout=4)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                code = str(item.get("Code", "")).strip()
                name = str(item.get("Name", "")).strip().replace(" ", "")
                raw_vol = str(item.get("TradeVolume", "0")).replace(",", "").strip()
                if code.isdigit() and len(code) == 4 and not code.startswith("00"):
                    try: raw_list.append({'code': code, 'name': name, 'vol': float(raw_vol)})
                    except Exception: pass
    except Exception: pass

    raw_list.sort(key=lambda x: x['vol'], reverse=True)
    top_30 = raw_list[:30] # 縮減至精準 30 檔，確保 10 秒內跑完

    leaderboard = []
    trade_date = ""

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(analyze_candidate, top_30))

    for r in results:
        if r is not None:
            leaderboard.append(r)
            if not trade_date and r.get('trade_date'): trade_date = r['trade_date']

    leaderboard.sort(key=lambda x: x['score'], reverse=True)
    top_3 = leaderboard[:3]

    if top_3:
        today_str = datetime.datetime.now().strftime("%Y年%m月%d日")
        formatted_date = trade_date.replace("-", "/") if trade_date else today_str
        
        header_title = f"📊【{today_str} AI選股 Top 3】\n(基於 {formatted_date} 數據精選)\n" + "="*22 + "\n"
        report_cards = []
        for idx, item in enumerate(top_3, 1):
            card = (
                f"🔥 No.{idx} {item['name']} ({item['code']})\n"
                f"  • 收盤價: ${item['close']:.2f}\n"
                f"  • 月線支撐: ${item['ma20']:.1f}\n"
                f"  • 狀態: 強勢多頭動能"
            )
            report_cards.append(card)

        final_content = header_title + "\n\n".join(report_cards)

        save_history_to_db("LATEST", final_content)

        if line_bot_api and LINE_USER_ID:
            try:
                line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=final_content))
                print("✅ 成功發送 LINE 推播！")
            except Exception as e:
                print(f"❌ LINE 推播失敗: {e}")
        return final_content
    return "計算失敗"

if __name__ == "__main__":
    run_precalculation()
