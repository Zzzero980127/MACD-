import os
import datetime
import requests
import pandas as pd
import psycopg2
import re

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
    except Exception:
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
    except Exception as e:
        print(f"⚠️ [Sim DB] 初始化失敗: {e}", flush=True)

def process_simulation():
    weekday = datetime.datetime.now().weekday()
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    if not conn: return
    
    try:
        cursor = conn.cursor()
        print(f"🎯 [Sim Engine] 執行日期: {today_str} (週{weekday + 1})", flush=True)

        # -------------------------------------------------------------------------
        # A. 檢查當前持股 (止損 -5% / MACD減弱 / 週四週五清倉)
        # -------------------------------------------------------------------------
        cursor.execute("SELECT id, stock_code, stock_name, buy_price FROM sim_trades WHERE status = 'HOLD';")
        holding_stocks = cursor.fetchall()

        for item in holding_stocks:
            trade_id, code, name, buy_price = item
            buy_price = float(buy_price)

            headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if FINMIND_TOKEN else {}
            start_date = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
            res = requests.get("https://api.finmindtrade.com/api/v4/data", params={"dataset": "TaiwanStockPrice", "data_id": code, "start_date": start_date}, headers=headers).json()
            
            if res.get("data"):
                df = pd.DataFrame(res["data"])
                curr_price = float(df.iloc[-1]['close'])
                
                exp1 = pd.to_numeric(df['close']).ewm(span=12, adjust=False).mean()
                exp2 = pd.to_numeric(df['close']).ewm(span=26, adjust=False).mean()
                osc = (exp1 - exp2) - (exp1 - exp2).ewm(span=9, adjust=False).mean()
                osc_today, osc_p1 = float(osc.iloc[-1]), float(osc.iloc[-2])

                ret = ((curr_price - buy_price) / buy_price) * 100
                should_sell, exit_reason = False, ""

                if ret <= -5.0:
                    should_sell, exit_reason = True, "🚨 大跌觸發止損 (-5%)"
                elif osc_today < osc_p1:
                    should_sell, exit_reason = True, "📉 MACD多頭減弱出場"
                elif weekday in [3, 4]:
                    should_sell, exit_reason = True, f"📅 週{weekday + 1}例行清倉結算"

                if should_sell:
                    cursor.execute('''
                        UPDATE sim_trades 
                        SET sell_date = %s, sell_price = %s, return_rate = %s, status = 'CLOSED', exit_reason = %s
                        WHERE id = %s;
                    ''', (today_str, curr_price, ret, exit_reason, trade_id))
                    print(f"💰 [模擬賣出] {code} {name} | 買價: {buy_price} -> 賣價: {curr_price} | 報酬: {ret:+.2f}%", flush=True)

        # -------------------------------------------------------------------------
        # B. 週一至週三：獲利「前一交易日」歷史選股報表
        # -------------------------------------------------------------------------
        if weekday in [0, 1, 2]:
            # 計算前一個交易日的 Date Key (例如 20260825)
            yesterday_dt = datetime.datetime.now() - datetime.timedelta(days=3 if weekday == 0 else 1)
            yesterday_str = yesterday_dt.strftime('%Y%m%d')

            print(f"🛒 [模擬買進] 撈取前一交易日 ({yesterday_str}) 報表獲取買入標的...", flush=True)
            cursor.execute("SELECT content FROM history WHERE date = %s;", (yesterday_str,))
            row = cursor.fetchone()
            
            # 若撈不到前一日備份，退回讀取 LATEST
            if not row or not row[0]:
                cursor.execute("SELECT content FROM history WHERE date = 'LATEST';")
                row = cursor.fetchone()

            if row and row[0]:
                content = row[0]
                lines = content.split('\n')
                current_strategy = "策略一"
                buy_targets = []

                for line in lines:
                    if "策略二" in line:
                        current_strategy = "策略二"
                    
                    # 🎯 精確修正：匹配格式 "🔹 2489 瑞軒 | 收: 22.50 (+1.20%)"
                    match = re.search(r'🔹\s*(\d{4})\s+([\u4e00-\u9fa5A-Za-z0-9\*]+)\s*\|\s*收:\s*(\d+\.?\d*)', line)
                    if match:
                        code, name, price = match.group(1), match.group(2), float(match.group(3))
                        buy_targets.append((code, name, price, current_strategy))

                # 寫入模擬倉資料庫
                for code, name, price, st_type in buy_targets:
                    cursor.execute("SELECT id FROM sim_trades WHERE stock_code = %s AND buy_date = %s;", (code, today_str))
                    if not cursor.fetchone():
                        cursor.execute('''
                            INSERT INTO sim_trades (stock_code, stock_name, strategy_type, buy_date, buy_price, status)
                            VALUES (%s, %s, %s, %s, %s, 'HOLD');
                        ''', (code, name, st_type, today_str, price))
                        print(f"🛒 [模擬買入] [{st_type}] {code} {name} | 成本價: ${price}", flush=True)

        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"❌ [Sim Engine Error] {e}", flush=True)
    finally:
        conn.close()

if __name__ == "__main__":
    init_sim_db()
    process_simulation()
