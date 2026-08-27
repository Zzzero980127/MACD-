import os
import datetime
import requests
import pandas as pd
import psycopg2
import re

# 自動相容兩種常見的環境變數命名方式
FINMIND_TOKEN = (os.environ.get('FINMIND_API_TOKEN') or os.environ.get('FINMIND_TOKEN', '')).strip()
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
        # A. 賣出邏輯：
        # - 每日常規：-5% 止損 / MACD 減弱
        # - 週四（weekday == 3）：清倉週一至週三買入的部位（確保週五交割款入帳）
        # - 週五（weekday == 4）：清倉週四進場的短線部位
        # -------------------------------------------------------------------------
        cursor.execute("SELECT id, stock_code, stock_name, buy_price, buy_date FROM sim_trades WHERE status = 'HOLD';")
        holding_stocks = cursor.fetchall()

        for item in holding_stocks:
            trade_id, code, name, buy_price, buy_date_str = item
            buy_price = float(buy_price)

            # 計算該持股是在週幾買進的
            buy_dt = datetime.datetime.strptime(buy_date_str, '%Y-%m-%d')
            buy_weekday = buy_dt.weekday()

            start_date = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
            
            params = {
                "dataset": "TaiwanStockPrice",
                "data_id": code,
                "start_date": start_date
            }
            if FINMIND_TOKEN:
                params["token"] = FINMIND_TOKEN

            res = requests.get("https://api.finmindtrade.com/api/v4/data", params=params).json()
            
            if res.get("data"):
                df = pd.DataFrame(res["data"])
                
                # 早上 09:00 執行時，取倒數第二筆（即昨日收盤價）做結算
                curr_price = float(df.iloc[-2]['close']) if len(df) >= 2 else float(df.iloc[-1]['close'])
                
                exp1 = pd.to_numeric(df['close']).ewm(span=12, adjust=False).mean()
                exp2 = pd.to_numeric(df['close']).ewm(span=26, adjust=False).mean()
                osc = (exp1 - exp2) - (exp1 - exp2).ewm(span=9, adjust=False).mean()
                
                osc_today, osc_p1 = float(osc.iloc[-2]), float(osc.iloc[-3]) if len(osc) >= 3 else (float(osc.iloc[-1]), float(osc.iloc[-2]))

                ret = ((curr_price - buy_price) / buy_price) * 100
                should_sell, exit_reason = False, ""

                # 判定賣出條件
                if ret <= -5.0:
                    should_sell, exit_reason = True, "🚨 大跌觸發止損 (-5%)"
                elif osc_today < osc_p1:
                    should_sell, exit_reason = True, "📉 MACD多頭減弱出場"
                elif weekday == 3 and buy_weekday in [0, 1, 2]:
                    # 週四例行清倉（出清週一～週三持股）
                    should_sell, exit_reason = True, "📅 週四結算週一至週三持股（T+2週五入帳）"
                elif weekday == 4 and buy_weekday == 3:
                    # 週五例行清倉（出清週四進場的短線股）
                    should_sell, exit_reason = True, "📅 週五週末結算週四短線持股"

                if should_sell:
                    cursor.execute('''
                        UPDATE sim_trades 
                        SET sell_date = %s, sell_price = %s, return_rate = %s, status = 'CLOSED', exit_reason = %s
                        WHERE id = %s;
                    ''', (today_str, curr_price, ret, exit_reason, trade_id))
                    print(f"💰 [模擬賣出] {code} {name} | 買價: {buy_price} -> 賣價: {curr_price} | 報酬: {ret:+.2f}% | 原因: {exit_reason}", flush=True)

        # -------------------------------------------------------------------------
        # B. 買進邏輯：
        # 週一至週三（weekday 0~2）：策略一與策略二各買入前五名 (最多 10 檔)
        # 週四（weekday 3，對應週三選股）：
        #   - 策略二需 >= 100 分（同分全買）
        #   - 若策略二無達標標的，改買策略一前 3 名
        # -------------------------------------------------------------------------
        if weekday in [0, 1, 2, 3]:
            yesterday_dt = datetime.datetime.now() - datetime.timedelta(days=3 if weekday == 0 else 1)
            yesterday_str = yesterday_dt.strftime('%Y%m%d')

            print(f"🛒 [模擬買進] 撈取前一交易日 ({yesterday_str}) 報表獲取買入標的...", flush=True)
            cursor.execute("SELECT content FROM history WHERE date = %s;", (yesterday_str,))
            row = cursor.fetchone()
            
            if not row or not row[0]:
                cursor.execute("SELECT content FROM history WHERE date = 'LATEST';")
                row = cursor.fetchone()

            if row and row[0]:
                content = row[0]
                lines = content.split('\n')
                current_strategy = "策略一"
                raw_targets = []

                for line in lines:
                    if "策略二" in line or "策略2" in line:
                        current_strategy = "策略二"
                    elif "策略一" in line or "策略1" in line:
                        current_strategy = "策略一"
                    
                    # 抓取：代號, 名稱, 價格, 分數(若有)
                    match = re.search(r'[•🔹]\s*(\d{4})\s+([\u4e00-\u9fa5A-Za-z0-9\*]+)\s*\|\s*(?:現價:\s*\$?|收:\s*)(\d+\.?\d*)', line)
                    if match:
                        code, name, price = match.group(1), match.group(2), float(match.group(3))
                        
                        # 從行內嘗試解析分數 (格式如: 100分, 105 pts, 分數:100)
                        score_match = re.search(r'(\d+)\s*(?:分|pts)', line)
                        score = int(score_match.group(1)) if score_match else 0
                        
                        raw_targets.append((code, name, price, current_strategy, score))

                buy_targets = []

                # 週一至週三買進：兩個策略各前 5 名全買 (最多 10 檔)
                if weekday in [0, 1, 2]:
                    st1_targets = [(t[0], t[1], t[2], t[3]) for t in raw_targets if t[3] == "策略一"][:5]
                    st2_targets = [(t[0], t[1], t[2], t[3]) for t in raw_targets if t[3] == "策略二"][:5]
                    buy_targets = st1_targets + st2_targets
                    print(f"🔥 [週一~週三正常建倉] 鎖定策略一 ({len(st1_targets)}檔) + 策略二 ({len(st2_targets)}檔) 準備進場！", flush=True)

                # 週四買進（週三選股）新精選邏輯：
                elif weekday == 3:
                    # 1. 篩選策略二且分數 >= 100 的標的
                    st2_qualified = [t for t in raw_targets if t[3] == "策略二" and t[4] >= 100]

                    if st2_qualified:
                        # 找出最高分數
                        max_score = max(t[4] for t in st2_qualified)
                        # 買入所有達到最高分的標的 (最高分同分者全買)
                        buy_targets = [(t[0], t[1], t[2], t[3]) for t in st2_qualified if t[4] == max_score]
                        print(f"🔥 [週四短線精選] 策略二有 {len(buy_targets)} 檔達 100 分以上 (最高分: {max_score})，同分全數進場！", flush=True)
                    else:
                        # 2. 若策略二無達 100 分標的，改買策略一前 3 名
                        st1_targets = [(t[0], t[1], t[2], t[3]) for t in raw_targets if t[3] == "策略一"]
                        buy_targets = st1_targets[:3]
                        print(f"⚠️ [週四短線精選] 策略二無標的達 100 分，啟動備案：買入策略一前 {len(buy_targets)} 名！", flush=True)

                # 寫入模擬倉資料庫 (加入當週防護 + 採用前日收盤價當做掛單買入成本)
                for code, name, price, st_type in buy_targets:
                    # 🛡️ 當週防護：若本週已買過或賣過該檔股票，則直接跳過
                    cursor.execute('''
                        SELECT id FROM sim_trades 
                        WHERE stock_code = %s 
                        AND (
                            DATE_TRUNC('week', buy_date::date) = DATE_TRUNC('week', %s::date)
                            OR (sell_date IS NOT NULL AND DATE_TRUNC('week', sell_date::date) = DATE_TRUNC('week', %s::date))
                        );
                    ''', (code, today_str, today_str))
                    
                    if cursor.fetchone():
                        print(f"🚫 [當週防護跳過] {code} {name} 本週已有買賣紀錄，不再重複建倉！", flush=True)
                        continue

                    cursor.execute('''
                        INSERT INTO sim_trades (stock_code, stock_name, strategy_type, buy_date, buy_price, status)
                        VALUES (%s, %s, %s, %s, %s, 'HOLD');
                    ''', (code, name, st_type, today_str, price))
                    print(f"🛒 [模擬買入成功] [{st_type}] {code} {name} | 掛單成交價: ${price}", flush=True)

        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"❌ [Sim Engine Error] {e}", flush=True)
    finally:
        conn.close()

if __name__ == "__main__":
    init_sim_db()
    process_simulation()
