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

# -----------------------------------------------------------------------------
# 1. 環境變數與設定
# -----------------------------------------------------------------------------
FINMIND_TOKEN = (
    os.environ.get('FINMIND_TOKEN', '').strip() or 
    os.environ.get('FINMIND_API_TOKEN', '').strip()
)

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()

# -----------------------------------------------------------------------------
# 2. 建立連線 Session 與 API 重試機制
# -----------------------------------------------------------------------------
def create_robust_session():
    session = requests.Session()
    retries = Retry(
        total=3, 
        backoff_factor=1.0, 
        status_forcelist=[429, 500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http = create_robust_session()

# -----------------------------------------------------------------------------
# 3. 資料庫連線與歷史寫入
# -----------------------------------------------------------------------------
def get_db_connection():
    if not DATABASE_URL:
        return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception as e:
        print(f"⚠️ [DB Log] 資料庫連線失敗: {e}", flush=True)
        return None

def save_to_db(report_text, date_str="LATEST"):
    conn = get_db_connection()
    if not conn:
        return
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
            ON CONFLICT (date) 
            DO UPDATE SET content = EXCLUDED.content;
        ''', (date_str, report_text))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ [DB Log] 資料庫寫入失敗: {e}", flush=True)

# -----------------------------------------------------------------------------
# 4. LINE 廣播推播
# -----------------------------------------------------------------------------
def send_line_push(report_text):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("⚠️ [LINE Log] 未設定 LINE Channel Access Token，略過發送", flush=True)
        return
    try:
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        line_bot_api.broadcast(TextSendMessage(text=report_text))
        print("✅ [LINE Log] 廣播推播成功發送！", flush=True)
    except Exception as e:
        print(f"❌ [LINE Log] LINE 推播發送失敗: {e}", flush=True)

# -----------------------------------------------------------------------------
# 5. 個股 K 線與籌碼分析核心 Logic
# -----------------------------------------------------------------------------
def fetch_finmind_data(stock_info):
    stock_id = stock_info["code"]
    stock_name = stock_info["name"]
    is_debug_target = (stock_id == "2884")  # 針對玉山金輸出調試 Log
    
    start_date = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    price_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
    
    try:
        res_p = http.get(price_url, timeout=8.0)
        if res_p.status_code != 200 or not res_p.json().get("data"):
            if is_debug_target: print(f"  └─ ❌ [{stock_id}] K線 API 取得失敗", flush=True)
            return None
        
        df = pd.DataFrame(res_p.json()["data"]).rename(
            columns={'close': 'Close', 'Trading_Volume': 'Volume', 'max': 'High', 'min': 'Low', 'open': 'Open'}
        )
        for col in ['Close', 'Volume', 'High', 'Low', 'Open']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            
        df = df.dropna(subset=['Close', 'Volume', 'High', 'Low', 'Open'])
        if len(df) < 35:
            if is_debug_target: print(f"  └─ ❌ [{stock_id}] K線資料筆數不足 35 筆", flush=True)
            return None

        # --- 計算 MACD 指標 ---
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['OSC'] = df['DIF'] - df['MACD']

        # --- 計算均線與布林通道 ---
        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['STD20'] = df['Close'].rolling(window=20).std()
        df['Boll_Upper'] = df['MA20'] + (df['STD20'] * 2)
        df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()
        df['Vol_MA20'] = df['Volume'].rolling(window=20).mean()

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
        today_volume = float(latest['Volume'])
        vol_ma5 = float(latest['Vol_MA5'])
        vol_ma20 = float(latest['Vol_MA20'])
        pct_change = ((close_price - prev_close) / prev_close) * 100

        # --- 技術面防衛過濾 ---
        if pct_change > 6.0:
            if is_debug_target: print(f"  └─ ❌ [{stock_id}] 漲幅過高 ({pct_change:.2f}%)", flush=True)
            return None
            
        upper_shadow = high_price - max(open_price, close_price)
        body_length = max(abs(close_price - open_price), 0.01)
        if (upper_shadow / body_length) > 1.5 and upper_shadow > (close_price * 0.015):
            if is_debug_target: print(f"  └─ ❌ [{stock_id}] 上影線過長避雷針", flush=True)
            return None
        
        ma20_val = float(latest['MA20'])
        if ma20_val > 0:
            if (close_price / ma20_val) > 1.20:
                if is_debug_target: print(f"  └─ ❌ [{stock_id}] 乖離過大", flush=True)
                return None
            if (close_price / ma20_val) < 0.95:
                if is_debug_target: print(f"  └─ ❌ [{stock_id}] 低於月線 5% 空頭格局", flush=True)
                return None

        boll_upper = float(latest['Boll_Upper'])
        is_volume_breakout = (today_volume >= vol_ma5 * 1.2) or (today_volume >= vol_ma20 * 1.1)
        if (close_price > boll_upper * 1.005) and not is_volume_breakout:
            if is_debug_target: print(f"  └─ ❌ [{stock_id}] 突破布林上軌但縮量", flush=True)
            return None

        # --- MACD 狀態判定 ---
        is_green_shrinking = (osc_today <= 0.005) and (osc_today > osc_p1)
        is_first_red = (osc_today > 0.0) and (osc_p1 <= 0.0)
        is_macd_expanding = (osc_today > 0.0) and (osc_today > osc_p1)
        
        is_red_shrinking_2days = (osc_p1 > 0.0) and (osc_p2 > osc_p1)
        is_red_shrinking_3days = (osc_p1 > 0.0) and (osc_p3 > osc_p2 > osc_p1)

        # --- 抓取籌碼面資料 ---
        time.sleep(0.5)
        chip_start = (datetime.datetime.now() - datetime.timedelta(days=12)).strftime("%Y-%m-%d")
        chip_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={chip_start}&token={FINMIND_TOKEN}"
        
        res_c = http.get(chip_url, timeout=8.0)
        if res_c.status_code == 200 and res_c.json().get("data"):
            df_c = pd.DataFrame(res_c.json()["data"])
            if not df_c.empty:
                # 安全解析買賣超股數並換算為張數
                buy_col = 'buy' if 'buy' in df_c.columns else ('Buy' if 'Buy' in df_c.columns else None)
                sell_col = 'sell' if 'sell' in df_c.columns else ('Sell' if 'Sell' in df_c.columns else None)
                
                if buy_col and sell_col:
                    df_c['net_buy'] = (pd.to_numeric(df_c[buy_col]) - pd.to_numeric(df_c[sell_col])) / 1000.0
                else:
                    df_c['net_buy'] = 0.0

                daily_total = df_c.groupby('date')['net_buy'].sum().reset_index(name='total_net')
                foreign_df = df_c[df_c['name'].str.contains('Foreign|外資', case=False)].groupby('date')['net_buy'].sum().reset_index(name='foreign_net')
                trust_df = df_c[df_c['name'].str.contains('Trust|投信', case=False)].groupby('date')['net_buy'].sum().reset_index(name='trust_net')
                
                daily_chip = daily_total.merge(foreign_df, on='date', how='left').merge(trust_df, on='date', how='left').fillna(0).sort_values('date')
                
                if len(daily_chip) >= 1:
                    today_total = float(daily_chip.iloc[-1]['total_net'])
                    prev_total = float(daily_chip.iloc[-2]['total_net']) if len(daily_chip) >= 2 else 0.0
                    today_foreign = float(daily_chip.iloc[-1]['foreign_net'])
                    today_trust = float(daily_chip.iloc[-1]['trust_net'])

                    if is_debug_target:
                        print(f"  🔍 [2884 除錯資訊] K線日期: {latest['date']} | 籌碼日期: {daily_chip.iloc[-1]['date']}", flush=True)
                        print(f"  🔍 [2884 除錯資訊] 收盤: {close_price} | OSC: {osc_today:.4f} (前日: {osc_p1:.4f})", flush=True)
                        print(f"  🔍 [2884 除錯資訊] 法人買超張數: {today_total:.0f}張 (外資: {today_foreign:.0f}, 投信: {today_trust:.0f})", flush=True)

                    if today_trust <= -1000:
                        if is_debug_target: print("  └─ ❌ [2884] 投信賣超過大", flush=True)
                        return None

                    if (prev_total <= -2000) and (today_total < abs(prev_total) * 0.8):
                        if is_debug_target: print("  └─ ❌ [2884] 沒能完全吞噬前日大賣", flush=True)
                        return None

                    is_chip_buy = (today_total >= 1000)

                    total_vol_shares = (today_volume / 1000) if today_volume > 0 else 1
                    chip_ratio = (today_total / total_vol_shares) if total_vol_shares > 0 else 0
                    is_day_trading_risk = (chip_ratio >= 0.40)

                    score = 0
                    strategy_type = None
                    tags = []

                    # 【策略一：底部止跌】
                    if (is_green_shrinking or is_first_red) and is_chip_buy:
                        strategy_type = "BOTTOM_TURN"
                        score = 70
                        tags.append("💥綠轉紅第1天" if is_first_red else "📉綠柱止跌")
                        
                        if is_day_trading_risk:
                            score += 10
                            tags.append(f"🔄法人合買({round(today_total)}張) ⚠️籌碼過度集中")
                        elif today_total >= 10000:
                            score += 20
                            tags.append(f"⚡萬張爆買({round(today_total)}張)")
                        elif today_total >= 5000:
                            score += 15
                            tags.append(f"🔥法人大買({round(today_total)}張)")
                        else:
                            score += 10
                            tags.append(f"🔄法人合買({round(today_total)}張)")

                    # 【策略二：洗盤突破】
                    elif is_macd_expanding and is_chip_buy:
                        if (prev_total >= 5000) and (today_total < prev_total * 0.5):
                            if is_debug_target: print("  └─ ❌ [2884] 法人買超連動衰退", flush=True)
                            return None

                        if today_total < 2000:
                            if is_debug_target: print("  └─ ❌ [2884] 洗盤突破買超未達 2000 張", flush=True)
                            return None

                        strategy_type = "WASH_BREAKOUT"
                        score = 65
                        if is_red_shrinking_3days:
                            score += 15
                            tags.append("⚡3日洗盤突破")
                        elif is_red_shrinking_2days:
                            score += 10
                            tags.append("⚡2日洗盤突破")
                        
                        tags.append(f"🔥法人買超({round(today_total)}張)")

                    # 加分項
                    if strategy_type:
                        if is_volume_breakout and (close_price >= boll_upper * 0.985):
                            score += 10
                            tags.append("🚀帶量強勢突破")
                        if today_foreign > 0 and today_trust > 0:
                            score += 10
                            tags.append("🤝土洋同買")
                        if 1.0 <= pct_change <= 4.0:
                            score += 10
                            tags.append("🛡️黃金位階")

                        return {
                            "code": stock_id,
                            "name": stock_name,
                            "close": close_price,
                            "pct": pct_change,
                            "score": score,
                            "type": strategy_type,
                            "status_label": " ".join(tags)
                        }
                    else:
                        if is_debug_target:
                            print(f"  └─ ❌ [2884] 未符合策略條件：is_green_shrinking={is_green_shrinking}, is_first_red={is_first_red}, is_chip_buy={is_chip_buy}", flush=True)

    except Exception as e:
        print(f"  └─ ⚠️ [{stock_id} {stock_name}] 分析過程異常: {e}", flush=True)
        
    return None

# -----------------------------------------------------------------------------
# 6. 主排程與掃描流程
# -----------------------------------------------------------------------------
def run_precalculation():
    print("==================================================", flush=True)
    print("🚀 [Cron Job] 開始執行 AI 排程選股與自動推播...", flush=True)
    
    if not FINMIND_TOKEN:
        print("❌ [Fatal Error] FINMIND_TOKEN 未設定，無法執行抓取！", flush=True)
        return

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
                    except ValueError:
                        continue
            
            df_stocks = pd.DataFrame(stocks).sort_values(by="volume", ascending=False)
            candidates = df_stocks.head(200).to_dict('records')
            print(f"✅ [TWSE Log] 成功取得 Top 200 熱門個股清單！開始逐一精準分析...", flush=True)
    except Exception as e:
        print(f"❌ [TWSE Log] 證交所 API 抓取失敗: {e}", flush=True)
        return

    bottom_turn_stocks = []
    wash_breakout_stocks = []
    total_count = len(candidates)

    for idx, stock_info in enumerate(candidates, 1):
        print(f"📊 [{idx}/{total_count}] 分析中: {stock_info['code']} {stock_info['name']}", flush=True)
        res = fetch_finmind_data(stock_info)
        
        if res:
            if res['type'] == 'BOTTOM_TURN':
                bottom_turn_stocks.append(res)
            elif res['type'] == 'WASH_BREAKOUT':
                wash_breakout_stocks.append(res)
            print(f"  └─ 🎯 [選中標的] [{res['code']} {res['name']}] 類型: {res['type']} 得分: {res['score']}", flush=True)
            
        time.sleep(0.3)

    bottom_turn_stocks.sort(key=lambda x: x['score'], reverse=True)
    wash_breakout_stocks.sort(key=lambda x: x['score'], reverse=True)

    today_str = datetime.datetime.now().strftime('%Y%m%d')
    date_display = datetime.datetime.now().strftime('%Y/%m/%d')

    lines = [
        f"📊 【AI 精選雙策略雙軌選股報告】({date_display})",
        "===================="
    ]

    lines.append("🌱 【策略一：底部止跌 + 法人合買翻多】")
    lines.append("💡 特性：空轉多拐點，低基期、獲利空間極大")
    lines.append("--------------------")
    if not bottom_turn_stocks:
        lines.append("今日暫無符合條件之標的。")
    else:
        for idx, item in enumerate(bottom_turn_stocks):
            lines.append(
                f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"   👉 得分:{item['score']}分 | {item['status_label']}"
            )
            if idx < len(bottom_turn_stocks) - 1:
                lines.append("┈┈┈┈┈┈┈┈┈┈")

    lines.append("\n====================\n")

    lines.append("🔥 【策略二：洗盤結束 + 法人暴買突破】")
    lines.append("💡 特性：主力洗盤完成，短線發動拉升即戰力")
    lines.append("--------------------")
    if not wash_breakout_stocks:
        lines.append("今日暫無符合條件之標的。")
    else:
        for idx, item in enumerate(wash_breakout_stocks):
            lines.append(
                f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"   👉 得分:{item['score']}分 | {item['status_label']}"
            )
            if idx < len(wash_breakout_stocks) - 1:
                lines.append("┈┈┈┈┈┈┈┈┈┈")

    report = "\n".join(lines)

    save_to_db(report, "LATEST")
    save_to_db(report, today_str)
    send_line_push(report)
    
    print("🎉 [Cron Job Log] 排程選股與 LINE 推播發送完畢！", flush=True)

if __name__ == "__main__":
    run_precalculation()
