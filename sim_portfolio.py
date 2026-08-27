import os
import datetime
import requests
import pandas as pd
import psycopg2
import re
import json
import gspread

FINMIND_TOKEN = (os.environ.get('FINMIND_API_TOKEN') or os.environ.get('FINMIND_TOKEN', '')).strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDS_JSON', '').strip()

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

def get_0050_weekly_return():
    """抓取 0050 當週漲跌幅 (%) 做為對照基準"""
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        params = {"dataset": "TaiwanStockPrice", "data_id": "0050", "start_date": start_date}
        if FINMIND_TOKEN: params["token"] = FINMIND_TOKEN
        
        res = requests.get("https://api.finmindtrade.com/api/v4/data", params=params).json()
        if res.get("data") and len(res["data"]) >= 2:
            df = pd.DataFrame(res["data"])
            p_start = float(df.iloc[-5]['close']) if len(df) >= 5 else float(df.iloc[0]['close'])
            p_end = float(df.iloc[-1]['close'])
            return round(((p_end - p_start) / p_start) * 100, 2)
    except Exception as e:
        print(f"⚠️ 抓取 0050 價格失敗: {e}", flush=True)
    return 0.0

def sync_to_google_sheets(summary_data):
    """將每週五結算戰報（含風報比與 0050 對照）寫入 Google Sheets"""
    if not GOOGLE_CREDS_JSON:
        print("⚠️ 未設定 GOOGLE_CREDS_JSON 環境變數，跳過雲端同步", flush=True)
        return
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        gc = gspread.service_account_from_dict(creds_dict)
        sh = gc.open("AI模擬倉週績效紀錄表").sheet1
        
        # 寫入欄位：[日期, 總筆數, 勝, 敗, 勝率, 週損益, 累計損益, 均賺%, 均賠%, 風報比, 0050漲跌%]
        row = [
            summary_data['date'],
            summary_data['total'],
            summary_data['win'],
            summary_data['loss'],
            f"{summary_data['win_rate']:.1f}%",
            summary_data['weekly_pnl'],
            summary_data['total_pnl'],
            f"{summary_data['avg_win']:.2f}%",
            f"{summary_data['avg_loss']:.2f}%",
            summary_data['risk_reward_ratio'],
            f"{summary_data['benchmark_0050']:+.2f}%"
        ]
        sh.append_row(row)
        print("📊 [Google Sheets] 已成功將週結算與0050對照數據同步至雲端！", flush=True)
    except Exception as e:
        print(f"❌ [Google Sheets API 錯誤] {e}", flush=True)

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
        # A. 賣出邏輯
        # -------------------------------------------------------------------------
        cursor.execute("SELECT id, stock_code, stock_name, buy_price, buy_date FROM sim_trades WHERE status = 'HOLD';")
        holding_stocks = cursor.fetchall()

        for item in holding_stocks:
            trade_id, code, name, buy_price, buy_date_str = item
            buy_price = float(buy_price)

            buy_dt = datetime.datetime.strptime(buy_date_str, '%Y-%m-%d')
            buy_weekday = buy_dt.weekday()

            start_date = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
            
            params = {"dataset": "TaiwanStockPrice", "data_id": code, "start_date": start_date}
            if FINMIND_TOKEN: params["token"] = FINMIND_TOKEN

            res = requests.get("https://api.finmindtrade.com/api/v4/data", params=params).json()
            
            if res.get("data"):
                df = pd.DataFrame(res["data"])
                curr_price = float(df.iloc[-2]['close']) if len(df) >= 2 else float(df.iloc[-1]['close'])
                
                exp1 = pd.to_numeric(df['close']).ewm(span=12, adjust=False).mean()
                exp2 = pd.to_numeric(df['close']).ewm(span=26, adjust=False).mean()
                osc = (exp1 - exp2) - (exp1 - exp2).ewm(span=9, adjust=False).mean()
                
                osc_today, osc_p1 = float(osc.iloc[-2]), float(osc.iloc[-3]) if len(osc) >= 3 else (float(osc.iloc[-1]), float(osc.iloc[-2]))

                ret = ((curr_price - buy_price) / buy_price) * 100
                should_sell, exit_reason = False, ""

                if ret <= -5.0:
                    should_sell, exit_reason = True, "🚨 大跌觸發止損 (-5%)"
                elif osc_today < osc_p1:
                    should_sell, exit_reason = True, "📉 MACD多頭減弱出場"
                elif weekday == 3 and buy_weekday in [0, 1, 2]:
                    should_sell, exit_reason = True, "📅 週四結算週一至週三持股（T+2週五入帳）"
                elif weekday == 4 and buy_weekday == 3:
                    should_sell, exit_reason = True, "📅 週五週末結算週四短線持股"

                if should_sell:
                    cursor.execute('''
                        UPDATE sim_trades 
                        SET sell_date = %s, sell_price = %s, return_rate = %s, status = 'CLOSED', exit_reason = %s
                        WHERE id = %s;
                    ''', (today_str, curr_price, ret, exit_reason, trade_id))
                    print(f"💰 [模擬賣出] {code} {name} | 買價: {buy_price} -> 賣價: {curr_price} | 報酬: {ret:+.2f}% | 原因: {exit_reason}", flush=True)

        # -------------------------------------------------------------------------
        # 週五結算邏輯：計算勝率、風報比與 0050 對照
        # -------------------------------------------------------------------------
        if weekday == 4:
            cursor.execute("SELECT buy_price, sell_price, return_rate FROM sim_trades WHERE status = 'CLOSED';")
            closed_trades = cursor.fetchall()
            
            total_trades = len(closed_trades)
            if total_trades > 0:
                win_returns = [float(t[2]) for t in closed_trades if float(t[2]) > 0]
                loss_returns = [abs(float(t[2])) for t in closed_trades if float(t[2]) < 0]
                
                wins = len(win_returns)
                losses = len(loss_returns)
                win_rate = (wins / total_trades) * 100
                
                avg_win = (sum(win_returns) / wins) if wins > 0 else 0.0
                avg_loss = (sum(loss_returns) / losses) if losses > 0 else 0.0
                
                # 風報比 = 平均獲利% / 平均虧損%
                rrr = round(avg_win / avg_loss, 2) if avg_loss > 0 else (round(avg_win, 2) if avg_win > 0 else 0.0)
                
                total_pnl = sum(((float(t[1]) - float(t[0])) / float(t[0])) * 100000 for t in closed_trades)
                benchmark_0050 = get_0050_weekly_return()

                summary = {
                    "date": today_str,
                    "total": total_trades,
                    "win": wins,
                    "loss": losses,
                    "win_rate": win_rate,
                    "weekly_pnl": int(total_pnl),
                    "total_pnl": int(total_pnl),
                    "avg_win": avg_win,
                    "avg_loss": avg_loss,
                    "risk_reward_ratio": rrr,
                    "benchmark_0050": benchmark_0050
                }
                sync_to_google_sheets(summary)

        # -------------------------------------------------------------------------
        # B. 買進邏輯
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
                raw_targets = []

                st1_pos = re.search(r'策略[一1]', content)
                st2_pos = re.search(r'策略[二2]', content)

                st1_idx = st1_pos.start() if st1_pos else -1
                st2_idx = st2_pos.start() if st2_pos else -1

                pattern = r'(?:[•🔹]\s*)?(\d{4})\s+([\u4e00-\u9fa5A-Za-z0-9\*]+)[\s\S]*?(?:現價:\s*\$?|收:\s*)(\d+\.?\d*)'
                
                for match in re.finditer(pattern, content):
                    start_pos = match.start()
                    code = match.group(1)
                    name = match.group(2)
                    price = float(match.group(3))

                    if st2_idx != -1 and start_pos >= st2_idx:
                        strategy = "策略二"
                    else:
                        strategy = "策略一"

                    snippet = content[start_pos:start_pos + 200]
                    score_match = re.search(r'(?:得分|分數|pts|分)\s*[:：\s]*(\d+)', snippet, re.IGNORECASE)
                    if not score_match:
                        score_match = re.search(r'(\d+)\s*(?:分|pts)', snippet, re.IGNORECASE)
                    
                    score = int(score_match.group(1)) if score_match else 0
                    raw_targets.append((code, name, price, strategy, score))

                buy_targets = []

                if weekday in [0, 1, 2]:
                    st1_targets = [(t[0], t[1], t[2], t[3]) for t in raw_targets if t[3] == "策略一"][:5]
                    st2_targets = [(t[0], t[1], t[2], t[3]) for t in raw_targets if t[3] == "策略二"][:5]
                    buy_targets = st1_targets + st2_targets
                    print(f"🔥 [週一~週三正常建倉] 鎖定策略一 ({len(st1_targets)}檔) + 策略二 ({len(st2_targets)}檔) 準備進場！", flush=True)

                elif weekday == 3:
                    st2_qualified = [t for t in raw_targets if t[3] == "策略二" and t[4] >= 100]

                    if st2_qualified:
                        max_score = max(t[4] for t in st2_qualified)
                        buy_targets = [(t[0], t[1], t[2], t[3]) for t in st2_qualified if t[4] == max_score]
                        print(f"🔥 [週四短線精選] 策略二有 {len(buy_targets)} 檔達 100 分以上 (最高分: {max_score})，同分全數進場！", flush=True)
                    else:
                        st1_targets = [(t[0], t[1], t[2], t[3]) for t in raw_targets if t[3] == "策略一"]
                        buy_targets = st1_targets[:3]
                        print(f"⚠️ [週四短線精選] 策略二無標的達 100 分，啟動備案：買入策略一前 {len(buy_targets)} 名！", flush=True)

                for code, name, price, st_type in buy_targets:
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
