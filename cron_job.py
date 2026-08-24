import os
import time
import requests
import pandas as pd
import datetime
import psycopg2
from concurrent.futures import ThreadPoolExecutor
from linebot import LineBotApi
from linebot.models import TextSendMessage

# ----------------------------------------------------
# 環境變數設定
# ----------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_USER_ID = os.environ.get('LINE_USER_ID', '').strip()
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None

def get_db_connection():
    if not DATABASE_URL:
        return None
    url = DATABASE_URL
    # 強制加上 sslmode=require 防範 Supabase 拒絕連線
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"
    return psycopg2.connect(url, connect_timeout=10)

def save_history_to_db(date_str, content_str):
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        if not conn:
            return
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
        print(f"✅ 已成功將 {date_str} 報告存入 Supabase 資料庫！")
    except Exception as e:
        print(f"❌ DB Save Error: {e}")

# ----------------------------------------------------
# 資料抓取 (API 延遲保護)
# ----------------------------------------------------
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

def get_tw_foreign_investor_history(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=15)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN: url += f"&token={FINMIND_TOKEN}"

        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"])
            foreign_df = df[df['name'].str.contains('Foreign|外資', case=False, na=False)].copy()
            if not foreign_df.empty:
                # ✅ 正確寫法避開 Warning
                daily_summary = foreign_df.groupby('date', as_index=False).agg({'buy': 'sum', 'sell': 'sum'})
                daily_summary['net'] = daily_summary['buy'] - daily_summary['sell']
                daily_summary = daily_summary.sort_values('date')
                
                if len(daily_summary) >= 2:
                    today_net = round(daily_summary.iloc[-1]['net'] / 1000)
                    yesterday_net = round(daily_summary.iloc[-2]['net'] / 1000)
                    return today_net, yesterday_net
                elif len(daily_summary) == 1:
                    return round(daily_summary.iloc[-1]['net'] / 1000), 0
    except Exception: pass
    return 0, 0

def analyze_candidate(item):
    try:
        code, name = item['code'], item['name']
        
        df = get_tw_stock_data(code)
        time.sleep(0.5)

        if df is not None and len(df) >= 20:
            foreign_today, foreign_yesterday = get_tw_foreign_investor_history(code)
            time.sleep(0.5)

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

            close, prev_close = float(latest['Close']), float(prev['Close'])
            close_5d = float(five_days_ago['Close'])
            ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close

            hist_today, hist_yesterday = float(latest['Hist']), float(prev['Hist'])
            gain_5d = ((close - close_5d) / close_5d) * 100
            bias_pct = ((close - ma20) / ma20) * 100

            score = 100.0

            if foreign_yesterday < 0 and foreign_today > 200:
                score += 15.0
            elif foreign_today > 3000 and gain_5d > 8.0:
                score -= 40.0
            elif foreign_today < 0:
                score -= abs(foreign_today) * 0.1

            if gain_5d > 10.0: score -= (gain_5d - 10.0) * 8.0
            if bias_pct > 6.0: score -= (bias_pct - 6.0) * 10.0

            if close < prev_close: score -= 20.0
            score += 10.0 if hist_today > hist_yesterday else -10.0

            return {
                'code': code, 'name': name, 'close': close, 'ma20': ma20,
                'bias_pct': bias_pct, 'gain_5d': gain_5d, 
                'foreign_net': foreign_today, 'foreign_prev': foreign_yesterday,
                'score': score, 'trade_date': trade_date
            }
    except Exception: pass
    return None

def run_precalculation():
    print("🚀 開始執行全台股大數據分析 (安全防超限版 Top 3)...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    raw_list = []

    for url in ["https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
                "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes"]:
        try:
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code == 200 and isinstance(res.json(), list):
                for item in res.json():
                    code = str(item.get("Code", item.get("SecuritiesCompanyCode", ""))).strip()
                    name = str(item.get("Name", item.get("CompanyName", ""))).strip().replace(" ", "")
                    raw_vol = str(item.get("TradeVolume", item.get("TradingShares", "0"))).replace(",", "").strip()
                    if code.isdigit() and len(code) == 4 and not code.startswith("00"):
                        try: raw_list.append({'code': code, 'name': name, 'vol': float(raw_vol) / 1000.0})
                        except Exception: pass
        except Exception: pass

    raw_list.sort(key=lambda x: x['vol'], reverse=True)
    top_100 = raw_list[:100]

    leaderboard = []
    trade_date = ""

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(analyze_candidate, top_100))

    for r in results:
        if r is not None:
            leaderboard.append(r)
            if not trade_date and r.get('trade_date'):
                trade_date = r['trade_date']

    leaderboard.sort(key=lambda x: x['score'], reverse=True)
    top_3 = leaderboard[:3]

    if top_3 and trade_date:
        today_str = datetime.datetime.now().strftime("%Y年%m月%d日")
        formatted_date = trade_date.replace("-", "/")
        
        header_title = f"📊【{today_str} AI選股 Top 3】\n(基於 {formatted_date} 盤後數據精選)\n" + "="*22 + "\n"
        
        report_cards = []
        for idx, item in enumerate(top_3, 1):
            chip_note = "🟢 外資轉買" if (item['foreign_prev'] < 0 and item['foreign_net'] > 0) else f"外資 {item['foreign_net']} 張"
            card = (
                f"🔥 No.{idx} {item['name']} ({item['code']})\n"
                f"  • 收盤價: ${item['close']:.2f} (月線 ${item['ma20']:.1f})\n"
                f"  • 近5日漲幅: {item['gain_5d']:+.1f}% | 乖離率: {item['bias_pct']:+.1f}%\n"
                f"  • 籌碼狀態: {chip_note}"
            )
            report_cards.append(card)

        final_content = header_title + "\n\n".join(report_cards)

        date_key = trade_date.replace("-", "")
        save_history_to_db(date_key, final_content)
        save_history_to_db("LATEST", final_content)

        if line_bot_api and LINE_USER_ID:
            try:
                line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=final_content))
                print("✅ 08:00 精選 Top 3 推播已送達！")
            except Exception as e:
                print(f"❌ LINE 推播失敗: {e}")

if __name__ == "__main__":
    run_precalculation()
