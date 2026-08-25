import os
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import datetime
import psycopg2
from linebot import LineBotApi
from linebot.models import TextSendMessage

FINMIND_TOKEN = (
    os.environ.get('FINMIND_TOKEN', '').strip() or 
    os.environ.get('FINMIND_API_TOKEN', '').strip()
)

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()

def create_robust_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504])
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
        cursor.execute('''CREATE TABLE IF NOT EXISTS history (date VARCHAR(20) PRIMARY KEY, content TEXT NOT NULL);''')
        cursor.execute('''INSERT INTO history (date, content) VALUES (%s, %s) ON CONFLICT (date) DO UPDATE SET content = EXCLUDED.content;''', (date_str, report_text))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception: pass

def send_line_push(report_text):
    if not LINE_CHANNEL_ACCESS_TOKEN: return
    try:
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        line_bot_api.broadcast(TextSendMessage(text=report_text))
    except Exception: pass

def fetch_finmind_data(stock_info):
    stock_id = stock_info["code"]
    stock_name = stock_info["name"]
    
    start_date = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    price_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
    
    try:
        res_p = http.get(price_url, timeout=8.0)
        if res_p.status_code != 200 or not res_p.json().get("data"): return None
        
        df = pd.DataFrame(res_p.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume', 'max': 'High', 'min': 'Low', 'open': 'Open'})
        for col in ['Close', 'Volume', 'High', 'Low', 'Open']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=['Close', 'Volume', 'High', 'Low', 'Open'])
        if len(df) < 35: return None

        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['OSC'] = df['DIF'] - df['MACD']

        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['STD20'] = df['Close'].rolling(window=20).std()
        df['Boll_Upper'] = df['MA20'] + (df['STD20'] * 2)
        df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()

        latest = df.iloc[-1]
        prev1 = df.iloc[-2]
        prev2 = df.iloc[-3]
        prev3 = df.iloc[-4]

        osc_today = float(latest['OSC'])
        osc_p1 = float(prev1['OSC'])
        osc_p2 = float(prev2['OSC'])
        osc_p3 = float(prev3['OSC'])

        close_price = float(latest['Close'])
        open_price = float(latest['Open'])
        high_price = float(latest['High'])
        prev_close = float(prev1['Close'])
        pct_change = ((close_price - prev_close) / prev_close) * 100

        # 技術面過濾
        if pct_change > 6.0: return None
        upper_shadow = high_price - max(open_price, close_price)
        body_length = max(abs(close_price - open_price), 0.01)
        if (upper_shadow / body_length) > 1.5 and upper_shadow > (close_price * 0.015): return None
        ma20_val = float(latest['MA20'])
        if ma20_val > 0 and (close_price / ma20_val) > 1.20: return None
        if (close_price >= float(latest['Boll_Upper']) * 0.985) and (float(latest['Volume']) < float(latest['Vol_MA5'])): return None

        is_green_shrinking = (osc_today < 0) and (osc_today > osc_p1)
        is_first_red = (osc_today > 0) and (osc_p1 <= 0)
        is_macd_expanding = (osc_today > 0) and (osc_today > osc_p1)
        is_red_shrinking_2days = (osc_p1 > 0) and (osc_p2 > osc_p1)
        is_red_shrinking_3days = (osc_p1 > 0) and (osc_p3 > osc_p2 > osc_p1)

        time.sleep(0.8)
        chip_start = (datetime.datetime.now() - datetime.timedelta(days=12)).strftime("%Y-%m-%d")
        chip_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={chip_start}&token={FINMIND_TOKEN}"
        
        res_c = http.get(chip_url, timeout=8.0)
        if res_c.status_code == 200 and res_c.json().get("data"):
            df_c = pd.DataFrame(res_c.json()["data"])
            if not df_c.empty:
                df_c['net_buy'] = (df_c['buy'] - df_c['sell']) / 1000
                
                daily_total = df_c.groupby('date')['net_buy'].sum().reset_index(name='total_net')
                foreign_df = df_c[df_c['name'].str.contains('Foreign|外資', case=False)].groupby('date')['net_buy'].sum().reset_index(name='foreign_net')
                trust_df = df_c[df_c['name'].str.contains('Trust|投信', case=False)].groupby('date')['net_buy'].sum().reset_index(name='trust_net')
                
                daily_chip = daily_total.merge(foreign_df, on='date', how='left').merge(trust_df, on='date', how='left').fillna(0).sort_values('date')
                
                if len(daily_chip) >= 2:
                    if str(daily_chip.iloc[-1]['date']) != str(latest['date']): return None

                    today_total = float(daily_chip.iloc[-1]['total_net'])
                    prev_total = float(daily_chip.iloc[-2]['total_net'])
                    today_foreign = float(daily_chip.iloc[-1]['foreign_net'])
                    today_trust = float(daily_chip.iloc[-1]['trust_net'])
                    
                    if (prev_total >= 3000) and (today_total < prev_total * 0.85): return None
                    if today_trust <= -1000: return None

                    is_heavy_sell_yesterday = (prev_total <= -2000)
                    is_strong_rebound_cover = is_heavy_sell_yesterday and (today_total >= abs(prev_total) * 0.8)
                    is_normal_turn_buy = (prev_total > -2000) and (today_total >= 2000)
                    is_3x_surge = (prev_total > 0) and (today_total >= prev_total * 3) and (today_total >= 1000)

                    score = 0
                    strategy_type = None
                    tags = []

                    if (is_green_shrinking or is_first_red):
                        if is_strong_rebound_cover:
                            strategy_type = "BOTTOM_TURN"
                            score = 90
                            tags.append("📉綠柱止跌" if is_green_shrinking else "💥綠轉紅第1天")
                            tags.append(f"⚡法人強勢吞噬({round(today_total)}張)")
                        elif is_normal_turn_buy:
                            strategy_type = "BOTTOM_TURN"
                            score = 80
                            tags.append("📉綠柱止跌" if is_green_shrinking else "💥綠轉紅第1天")
                            tags.append(f"🔄法人合買{round(today_total)}張")

                    elif is_macd_expanding and (is_strong_rebound_cover or is_normal_turn_buy or is_3x_surge):
                        strategy_type = "WASH_BREAKOUT"
                        score = 70
                        if is_red_shrinking_3days:
                            score += 20
                            tags.append("⚡3日洗盤突破")
                        elif is_red_shrinking_2days:
                            score += 15
                            tags.append("⚡2日洗盤突破")
                        tags.append(f"🔥法人暴買{round(today_total)}張")

                    if strategy_type:
                        if today_foreign > 0 and today_trust > 0:
                            score += 15
                            tags.append("🤝土洋同買")
                        if 1.0 <= pct_change <= 4.0:
                            score += 10
                            tags.append("🛡️黃金位階")

                        return {
                            "code": stock_id, "name": stock_name, "close": close_price,
                            "pct": pct_change, "score": score, "type": strategy_type,
                            "status_label": " ".join(tags)
                        }
    except Exception as e:
        print(f"  └─ ⚠️ [{stock_id} {stock_name}] 分析異常: {e}", flush=True)
    return None

def run_precalculation():
    print("==================================================", flush=True)
    print("🚀 [Cron Job] 開始執行 AI 排程選股與自動推播...", flush=True)
    
    if not FINMIND_TOKEN: return

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
            candidates = df_stocks.head(200).to_dict('records')
            print(f"✅ [TWSE Log] 成功取得 Top 200 熱門個股！開始分析...", flush=True)
    except Exception as e:
        print(f"❌ [TWSE Log] 證交所 API 抓取失敗: {e}", flush=True)
        return

    bottom_turn_stocks = []
    wash_breakout_stocks = []
    total_count = len(candidates)

    # 🔍 補回每一檔的掃描進度 Log
    for idx, stock_info in enumerate(candidates, 1):
        print(f"📊 [{idx}/{total_count}] 分析中: {stock_info['code']} {stock_info['name']}", flush=True)
        res = fetch_finmind_data(stock_info)
        if res:
            if res['type'] == 'BOTTOM_TURN':
                bottom_turn_stocks.append(res)
            elif res['type'] == 'WASH_BREAKOUT':
                wash_breakout_stocks.append(res)
            print(f"  └─ 🎯 [選中標的] [{res['code']} {res['name']}] 類型:{res['type']} 得分:{res['score']}", flush=True)
        time.sleep(0.5)

    bottom_turn_stocks.sort(key=lambda x: x['score'], reverse=True)
    wash_breakout_stocks.sort(key=lambda x: x['score'], reverse=True)

    today_str = datetime.datetime.now().strftime('%Y%m%d')
    date_display = datetime.datetime.now().strftime('%Y/%m/%d')

    lines = [f"📊 【AI 精選雙策略雙軌選股報告】({date_display})", "===================="]

    lines.append("🌱 【策略一：底部止跌 + 法人合買翻多】")
    lines.append("💡 特性：空轉多拐點，低基期、獲利空間極大")
    lines.append("--------------------")
    if not bottom_turn_stocks:
        lines.append("今日暫無符合條件之標的。")
    else:
        for idx, item in enumerate(bottom_turn_stocks):
            lines.append(f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n   👉 得分:{item['score']}分 | {item['status_label']}")
            if idx < len(bottom_turn_stocks) - 1: lines.append("┈┈┈┈┈┈┈┈┈┈")

    lines.append("\n====================\n")

    lines.append("🔥 【策略二：洗盤結束 + 法人暴買突破】")
    lines.append("💡 特性：主力洗盤完成，短線發動拉升即戰力")
    lines.append("--------------------")
    if not wash_breakout_stocks:
        lines.append("今日暫無符合條件之標的。")
    else:
        for idx, item in enumerate(wash_breakout_stocks):
            lines.append(f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n   👉 得分:{item['score']}分 | {item['status_label']}")
            if idx < len(wash_breakout_stocks) - 1: lines.append("┈┈┈┈┈┈┈┈┈┈")

    report = "\n".join(lines)
    save_to_db(report, "LATEST")
    save_to_db(report, today_str)
    send_line_push(report)
    print("🎉 [Cron Job Log] 排程選股與 LINE 推播發送完畢！", flush=True)

if __name__ == "__main__":
    run_precalculation()
