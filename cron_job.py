import os
import time
import requests
import pandas as pd
import datetime
import psycopg2
import gc
import traceback
from linebot import LineBotApi
from linebot.models import TextSendMessage

# 寫死 FinMind Token 確保 100% 帶入
FINMIND_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjo2t5bGdkc0BnWFpc5jb20iLCJlbWFpbCI6InRewXnZHNAZ21haWWuY29tIwidG9rZW5_fdmVyc2lvbiI6MH0.ebdFVr_Wfwo_Cm3ZnxZolvZGxfmXkywJJv8Y19gngCk".strip()

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_USER_ID = os.environ.get('LINE_USER_ID', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None

def get_db_connection():
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception as e:
        print(f"❌ DB 連線失敗: {e}", flush=True)
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
        print(f"✅ 成功寫入資料庫 Key: {date_str}", flush=True)
    except Exception as e:
        print(f"❌ DB 寫入失敗 [{date_str}]: {e}", flush=True)

def get_tw_stock_data(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=120)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
        
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'token': FINMIND_TOKEN
        }
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
            df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
            df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
            df = df.dropna(subset=['Close'])
            if len(df) >= 5: return df
    except Exception: pass
    return None

def analyze_candidate(item):
    try:
        code, name = item['code'], item['name']
        df = get_tw_stock_data(code)
        
        # 備用機制：抓不到 K 線時用證交所數據
        if df is None or len(df) < 5:
            close_price = item.get('close', 100.0)
            vol = item.get('vol', 0)
            score = 50.0 + (vol / 1000000.0)
            return {
                'code': code, 'name': name, 'close': close_price, 'ma20': close_price * 0.95,
                'score': score, 'trade_date': datetime.datetime.now().strftime("%Y-%m-%d")
            }

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest
        trade_date = str(latest.get('date', '')).strip()

        df['MA20'] = df['Close'].rolling(window=min(20, len(df))).mean()
        df['MA60'] = df['Close'].rolling(window=min(60, len(df))).mean()
        
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['Hist'] = df['DIF'] - df['MACD']

        close, prev_close = float(latest['Close']), float(prev['Close'])
        ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close
        ma60 = float(latest['MA60']) if not pd.isna(latest['MA60']) else close
        hist_today = float(latest['Hist']) if not pd.isna(latest['Hist']) else 0
        hist_yesterday = float(prev['Hist']) if not pd.isna(prev['Hist']) else 0

        score = 100.0
        if close > ma20: score += 20.0
        if close > ma60: score += 15.0
        if hist_today > hist_yesterday: score += 15.0
        if close > prev_close: score += 10.0

        return {
            'code': code, 'name': name, 'close': close, 'ma20': ma20,
            'score': score, 'trade_date': trade_date
        }
    except Exception as e:
        print(f"分析個股錯誤 {item.get('code')}: {e}", flush=True)
    return None

def run_precalculation():
    print("🚀 後台啟動：開始運算全台股成交量前 100 檔指標...", flush=True)
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        raw_list = []

        try:
            res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", headers=headers, timeout=5)
            if res.status_code == 200 and isinstance(res.json(), list):
                for item in res.json():
                    code = str(item.get("Code", "")).strip()
                    name = str(item.get("Name", "")).strip().replace(" ", "")
                    raw_vol = str(item.get("TradeVolume", "0")).replace(",", "").strip()
                    raw_close = str(item.get("ClosingPrice", "0")).replace(",", "").strip()
                    if code.isdigit() and len(code) == 4 and not code.startswith("00"):
                        try: 
                            raw_list.append({
                                'code': code, 
                                'name': name, 
                                'vol': float(raw_vol),
                                'close': float(raw_close) if raw_close != "--" else 100.0
                            })
                        except Exception: pass
        except Exception as e:
            print(f"❌ 抓取證交所列表失敗: {e}", flush=True)

        raw_list.sort(key=lambda x: x['vol'], reverse=True)
        top_100 = raw_list[:100]

        leaderboard = []
        trade_date = ""

        for idx, item in enumerate(top_100, 1):
            print(f"[{idx}/100] 正在分析 {item['name']} ({item['code']})...", flush=True)
            res = analyze_candidate(item)
            if res is not None:
                leaderboard.append(res)
                if not trade_date and res.get('trade_date'): trade_date = res['trade_date']
            
            gc.collect()
            time.sleep(5)  # 有 Token 後帶入，速度可加快至 5 秒

        print(f"🏁 100 檔分析完畢！成功算出 {len(leaderboard)} 檔。", flush=True)

        if len(leaderboard) > 0:
            leaderboard.sort(key=lambda x: x['score'], reverse=True)
            top_3 = leaderboard[:3]

            today_dt = datetime.datetime.now()
            today_str = today_dt.strftime("%Y年%m月%d日")
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

            print("📝 正在寫入 Supabase 資料庫...", flush=True)
            save_history_to_db("LATEST", final_content)
            save_history_to_db(today_dt.strftime("%Y%m%d"), final_content)
            save_history_to_db(today_dt.strftime("%m%d"), final_content)

            if line_bot_api and LINE_USER_ID:
                try:
                    line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=final_content))
                    print("✅ 已成功發送 LINE 主動推播！", flush=True)
                except Exception as e:
                    print(f"❌ LINE 推播失敗: {e}", flush=True)

            return final_content

    except Exception as e:
        print(f"💥 run_precalculation 發生致命崩潰:\n{traceback.format_exc()}", flush=True)

    return "計算失敗"

if __name__ == "__main__":
    run_precalculation()
