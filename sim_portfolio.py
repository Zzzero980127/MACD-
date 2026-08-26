import os
import time
import datetime
import requests
import pandas as pd
import psycopg2

# -----------------------------------------------------------------------------
# 1. 環境變數設定
# -----------------------------------------------------------------------------
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

if not FINMIND_TOKEN:
    print("❌ [Fatal Error] 未偵測到 FINMIND_API_TOKEN！", flush=True)
    exit(1)

# -----------------------------------------------------------------------------
# 2. 資料庫初始化 (模擬倉專用資料表)
# -----------------------------------------------------------------------------
def get_db_connection():
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception as e:
        print(f"⚠️ [DB Log] 資料庫連線失敗: {e}", flush=True)
        return None

def init_sim_db():
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sim_trades (
                id SERIAL PRIMARY KEY,
                stock_code VARCHAR(10),
                stock_name VARCHAR(20),
                strategy_type VARCHAR(20),
                buy_date VARCHAR(10),
                buy_price NUMERIC,
                sell_date VARCHAR(10),
                sell_price NUMERIC,
                return_rate NUMERIC,
                status VARCHAR(20),
                exit_reason VARCHAR(50)
            );
        ''')
        conn.commit()
        cursor.close()
        conn.close()
        print("💾 [Sim DB] 模擬倉資料表初始化完成", flush=True)
    except Exception as e:
        print(f"⚠️ [Sim DB] 建立模擬倉資料表失敗: {e}", flush=True)

# -----------------------------------------------------------------------------
# 3. 核心個股分析與指標計算
# -----------------------------------------------------------------------------
def analyze_stock(stock_info, current_idx, total_count):
    stock_id = stock_info["code"]
    stock_name = stock_info["name"]
    start_date = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"}
    price_url = "https://api.finmindtrade.com/api/v4/data"

    try:
        res_p = requests.get(price_url, params={"dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start_date}, headers=headers, timeout=8.0)
        if res_p.status_code != 200 or not res_p.json().get("data"): return None

        df = pd.DataFrame(res_p.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume', 'max': 'High', 'min': 'Low', 'open': 'Open'})
        for col in ['Close', 'Volume', 'High', 'Low', 'Open']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna()
        if len(df) < 35: return None

        # MACD 計算
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['OSC'] = df['DIF'] - df['MACD']
        df['MA20'] = df['Close'].rolling(window=20).mean()

        latest, prev1, prev2, prev3 = df.iloc[-1], df.iloc[-2], df.iloc[-3], df.iloc[-4]
        dif_today = float(latest['DIF'])
        osc_today = float(latest['OSC'])
        osc_p1 = float(prev1['OSC'])
        osc_p2 = float(prev2['OSC'])
        osc_p3 = float(prev3['OSC'])

        close_price = float(latest['Close'])
        prev_close = float(prev1['Close'])
        pct_change = ((close_price - prev_close) / prev_close) * 100

        # 通用濾除門檻
        if osc_today <= osc_p1 or pct_change > 6.5 or pct_change < -5.0: return None

        # 籌碼面資料
        time.sleep(0.1)
        chip_start = (datetime.datetime.now() - datetime.timedelta(days=15)).strftime("%Y-%m-%d")
        res_c = requests.get(price_url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": stock_id, "start_date": chip_start}, headers=headers, timeout=6.0)
        
        today_foreign, prev_foreign = 0, 0
        if res_c.status_code == 200 and res_c.json().get("data"):
            df_c = pd.DataFrame(res_c.json()["data"])
            if not df_c.empty:
                df_c['net_buy'] = (pd.to_numeric(df_c['buy'], errors='coerce').fillna(0) - pd.to_numeric(df_c['sell'], errors='coerce').fillna(0)) / 1000
                foreign_df = df_c[df_c['name'].astype(str).str.contains('Foreign|外資', case=False)].groupby('date')['net_buy'].sum().reset_index(name='foreign_net').sort_values('date')
                if len(foreign_df) >= 2:
                    today_foreign = float(foreign_df.iloc[-1]['foreign_net'])
                    prev_foreign = float(foreign_df.iloc[-2]['foreign_net'])

        # 策略二洗盤起漲判定
        osc_3day_declining = (osc_p3 > osc_p2) and (osc_p2 > osc_p1)
        is_above_zero_axis = (osc_today > 0) or (dif_today > 0)
        foreign_surge_valid = (today_foreign >= prev_foreign * 3) if prev_foreign > 0 else (today_foreign > abs(prev_foreign))
        is_wash_breakout = (is_above_zero_axis and osc_3day_declining and (osc_today > osc_p1) and foreign_surge_valid and (1.0 <= pct_change <= 5.5))

        # 評分機制
        score = 50
        if osc_today < 0 and osc_p1 < 0 and (osc_today > osc_p1) and (osc_p2 > osc_p1):
            score += 30
        elif osc_today > 0 and osc_p1 <= 0:
            score += 15
        else:
            score += 20

        if close_price >= float(latest['MA20']): score += 10
        if is_wash_breakout: score += 20

        return {
            "code": stock_id, "name": stock_name, "close": close_price,
            "pct": pct_change, "score": score, "is_wash_breakout": is_wash_breakout,
            "osc_today": osc_today, "osc_p1": osc_p1
        }
    except Exception:
        return None

# -----------------------------------------------------------------------------
# 4. 模擬倉核心流程
# -----------------------------------------------------------------------------
def process_simulation():
    weekday = datetime.datetime.now().weekday()  # 0:週一, 1:週二, 2:週三, 3:週四, 4:週五
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    if not conn: return
    
    try:
        cursor = conn.cursor()
        print(f"🎯 [Sim Engine] 執行日期: {today_str} (週{weekday + 1})", flush=True)

        # -------------------------------------------------------------------------
        # A. 檢查當前持股 (止損 / MACD減弱 / 週四或週五強制結算)
        # -------------------------------------------------------------------------
        cursor.execute("SELECT id, stock_code, stock_name, buy_price, buy_date FROM sim_trades WHERE status = 'HOLD';")
        holding_stocks = cursor.fetchall()

        for item in holding_stocks:
            trade_id, code, name, buy_price, buy_date = item
            buy_price = float(buy_price)

            headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"}
            start_date = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
            res = requests.get("https://api.finmindtrade.com/api/v4/data", params={"dataset": "TaiwanStockPrice", "data_id": code, "start_date": start_date}, headers=headers).json()
            
            if res.get("data"):
                df = pd.DataFrame(res["data"])
                curr_price = float(df.iloc[-1]['close'])
                
                exp1 = pd.to_numeric(df['close']).ewm(span=12, adjust=False).mean()
                exp2 = pd.to_numeric(df['close']).ewm(span=26, adjust=False).mean()
                osc = (exp1 - exp2) - (exp1 - exp2).ewm(span=9, adjust=False).mean()
                osc_today = float(osc.iloc[-1])
                osc_p1 = float(osc.iloc[-2])

                ret = ((curr_price - buy_price) / buy_price) * 100
                should_sell = False
                exit_reason = ""

                # 出場判斷順序優化
                if ret <= -5.0:
                    should_sell = True
                    exit_reason = "🚨 大跌觸發止損 (-5%)"
                elif osc_today < osc_p1:
                    should_sell = True
                    exit_reason = "📉 MACD多頭減弱出場"
                elif weekday in [3, 4]:  # 修正：週四或週五例行清倉結算
                    should_sell = True
                    exit_reason = f"📅 週{weekday + 1}例行清倉結算"

                if should_sell:
                    cursor.execute('''
                        UPDATE sim_trades 
                        SET sell_date = %s, sell_price = %s, return_rate = %s, status = 'CLOSED', exit_reason = %s
                        WHERE id = %s;
                    ''', (today_str, curr_price, ret, exit_reason, trade_id))
                    print(f"💰 [模擬賣出] {code} {name} | 買價: {buy_price} -> 賣價: {curr_price} | 報酬: {ret:+.2f}% | 原因: {exit_reason}", flush=True)

        # -------------------------------------------------------------------------
        # B. 週一至週三：進行買入動作
        # -------------------------------------------------------------------------
        if weekday in [0, 1, 2]:
            print("🛒 [模擬買進] 開始掃描今日符合條件標的...", flush=True)
            res_twse = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", timeout=10).json()
            stocks = []
            for item in res_twse:
                code, name = item.get("Code", "").strip(), item.get("Name", "").strip()
                if len(code) == 4 and code.isdigit():
                    try: stocks.append({"code": code, "name": name, "volume": int(item.get("TradeVolume", 0))})
                    except: continue

            candidates = pd.DataFrame(stocks).sort_values(by="volume", ascending=False).head(200).to_dict('records')
            
            analyzed_results = []
            for idx, stock in enumerate(candidates, 1):
                res = analyze_stock(stock, idx, len(candidates))
                if res: analyzed_results.append(res)

            s2_list = [s for s in analyzed_results if s['is_wash_breakout']]
            s2_list.sort(key=lambda x: x['score'], reverse=True)
            top_s2 = s2_list[:5]

            s1_list = [s for s in analyzed_results if not s['is_wash_breakout']]
            s1_list.sort(key=lambda x: x['score'], reverse=True)
            top_s1 = s1_list[:5]

            buy_targets = []
            for item in top_s1: buy_targets.append((item, "策略一(底部止跌)"))
            for item in top_s2: buy_targets.append((item, "策略二(洗盤起漲)"))

            for target, st_type in buy_targets:
                # 防重複買入判定
                cursor.execute("SELECT id FROM sim_trades WHERE stock_code = %s AND buy_date = %s;", (target['code'], today_str))
                if not cursor.fetchone():
                    cursor.execute('''
                        INSERT INTO sim_trades (stock_code, stock_name, strategy_type, buy_date, buy_price, status)
                        VALUES (%s, %s, %s, %s, %s, 'HOLD');
                    ''', (target['code'], target['name'], st_type, today_str, target['close']))
                    print(f"🛒 [模擬買入] [{st_type}] {target['code']} {target['name']} | 價格: {target['close']}", flush=True)

        conn.commit()

        # -------------------------------------------------------------------------
        # C. 平倉戰績統計
        # -------------------------------------------------------------------------
        cursor.execute("SELECT buy_price, sell_price, return_rate FROM sim_trades WHERE status = 'CLOSED';")
        closed_trades = cursor.fetchall()
        if closed_trades:
            total_pnl_dollars = 0
            win_count = 0
            total_count = len(closed_trades)

            for buy_p, sell_p, ret in closed_trades:
                buy_p, sell_p = float(buy_p), float(sell_p)
                shares = int(100000 / buy_p)
                pnl = (shares * sell_p) - (shares * buy_p)
                total_pnl_dollars += pnl
                if pnl > 0: win_count += 1

            win_rate = (win_count / total_count) * 100
            print(f"\n==========================================", flush=True)
            print(f"📊 【模擬倉累計戰績 (每檔10萬)】", flush=True)
            print(f"🔹 平倉筆數: {total_count} 筆", flush=True)
            print(f"🔹 勝率: {win_rate:.1f}% ({win_count}勝 / {total_count - win_count}敗)", flush=True)
            print(f"🔹 累計總損益: ${total_pnl_dollars:+,.0f} 元", flush=True)
            print(f"==========================================\n", flush=True)

        cursor.close()
    except Exception as e:
        print(f"❌ [Sim Engine Error] 執行過程出錯: {e}", flush=True)
    finally:
        conn.close()

if __name__ == "__main__":
    init_sim_db()
    process_simulation()
