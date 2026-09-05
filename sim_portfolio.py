import os
import re
import json
import datetime
import requests
import pandas as pd
import psycopg2
import gspread
from google.oauth2.service_account import Credentials

FINMIND_TOKEN = (os.environ.get('FINMIND_API_TOKEN') or os.environ.get('FINMIND_TOKEN', '')).strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

TOTAL_CAPITAL = 5000000.0  # 💰 500 萬總資金池

def get_db_connection():
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception as e:
        print(f"⚠️ [DB Log] 連線失敗: {e}", flush=True)
        return None

def init_sim_db():
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sim_trades (
                id SERIAL PRIMARY KEY,
                stock_code VARCHAR(10) NOT NULL,
                stock_name VARCHAR(50) NOT NULL,
                strategy_type VARCHAR(20) NOT NULL,
                buy_date VARCHAR(20) NOT NULL,
                buy_price NUMERIC(10, 2) NOT NULL,
                sell_date VARCHAR(20),
                sell_price NUMERIC(10, 2),
                return_rate NUMERIC(10, 2),
                status VARCHAR(10) DEFAULT 'HOLD',
                exit_reason TEXT
            );
        ''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ [DB Log] 初始化資料庫失敗: {e}", flush=True)

def get_0050_weekly_return():
    # 預設基準回傳
    return 0.0

def sync_to_google_sheets(summary):
    """
    實作寫入 Google Sheets 的邏輯
    需在環境變數中設定:
    1. GOOGLE_SHEETS_JSON: Google Service Account 密鑰 JSON 字串
    2. SPREADSHEET_KEY 或 SPREADSHEET_NAME: Google 試算表 ID 或名稱
    """
    sheets_json = os.environ.get('GOOGLE_SHEETS_JSON', '').strip()
    sheet_key = os.environ.get('SPREADSHEET_KEY', '').strip()
    sheet_name = os.environ.get('SPREADSHEET_NAME', '模擬倉週結算').strip()

    if not sheets_json:
        print("⚠️ [Google Sheets] 未偵測到 GOOGLE_SHEETS_JSON 環境變數，跳過試算表同步。", flush=True)
        return

    try:
        scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        info = json.loads(sheets_json)
        creds = Credentials.from_service_account_info(info, scopes=scope)
        client = gspread.authorize(creds)

        if sheet_key:
            spreadsheet = client.open_by_key(sheet_key)
        else:
            spreadsheet = client.open(sheet_name)

        sheet = spreadsheet.sheet1

        # 檢查標頭，若空表則寫入標頭
        existing_rows = sheet.get_all_values()
        if not existing_rows:
            header = ["結算日期", "總交易數", "獲利筆數", "虧損筆數", "勝率(%)", "當週損益(元)", "累計總損益(元)", "平均獲利(%)", "平均虧損(%)", "盈虧比", "0050週報酬(%)", "當週交易數"]
            sheet.append_row(header)

        # 整理要追加寫入的數據資料列
        row = [
            summary.get("date", ""),
            summary.get("total", 0),
            summary.get("win", 0),
            summary.get("loss", 0),
            summary.get("win_rate", 0.0),
            summary.get("weekly_pnl", 0),
            summary.get("total_pnl", 0),
            summary.get("avg_win", 0.0),
            summary.get("avg_loss", 0.0),
            summary.get("risk_reward_ratio", 0.0),
            summary.get("benchmark_0050", 0.0),
            summary.get("weekly_trades_count", 0)
        ]

        sheet.append_row(row)
        print(f"✅ [Google Sheets] 成功同步週結算報告至試算表！", flush=True)
    except Exception as e:
        print(f"❌ [Google Sheets Sync Error] {e}", flush=True)

def process_simulation():
    conn = get_db_connection()
    if not conn: return
    
    now = datetime.datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    weekday = now.weekday()  # 0:週一, 1:週二, 2:週三, 3:週四, 4:週五, 5:週六, 6:週日

    try:
        cursor = conn.cursor()
        print(f"🎯 [Sim Engine] 執行日期: {today_str} (週{weekday + 1}) | 總資金設定: ${TOTAL_CAPITAL:,.0f}", flush=True)

        # -------------------------------------------------------------------------
        # A. 賣出邏輯 (保持原本正確邏輯不變)
        # -------------------------------------------------------------------------
        cursor.execute("SELECT id, stock_code, stock_name, buy_price, buy_date FROM sim_trades WHERE status = 'HOLD';")
        holding_stocks = cursor.fetchall()

        for item in holding_stocks:
            trade_id, code, name, buy_price, buy_date_str = item
            buy_price = float(buy_price)

            buy_dt = datetime.datetime.strptime(buy_date_str, '%Y-%m-%d')
            buy_weekday = buy_dt.weekday()

            start_date = (now - datetime.timedelta(days=40)).strftime("%Y-%m-%d")
            
            params = {"dataset": "TaiwanStockPrice", "data_id": code, "start_date": start_date}
            if FINMIND_TOKEN: params["token"] = FINMIND_TOKEN

            res = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, timeout=8).json()
            
            if res.get("data") and len(res["data"]) >= 2:
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
                elif weekday == 3 and buy_weekday in [0, 1, 2]:
                    should_sell, exit_reason = True, "📅 週四結算週一至週三持股"
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
        # 週五與週末結算邏輯 (修改：包含週六 5 與週日 6，避免錯過結算)
        # -------------------------------------------------------------------------
        if weekday in [4, 5, 6]:
            cursor.execute("SELECT buy_price, sell_price, return_rate, sell_date FROM sim_trades WHERE status = 'CLOSED';")
            closed_trades = cursor.fetchall()
            
            total_trades = len(closed_trades)
            if total_trades > 0:
                win_returns = [float(t[2]) for t in closed_trades if float(t[2]) > 0]
                loss_returns = [abs(float(t[2])) for t in closed_trades if float(t[2]) < 0]
                
                wins = len(win_returns)
                losses = len(loss_returns)
                win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0.0
                
                avg_win = (sum(win_returns) / wins) if wins > 0 else 0.0
                avg_loss = (sum(loss_returns) / losses) if losses > 0 else 0.0
                
                rrr = round(avg_win / avg_loss, 2) if avg_loss > 0 else (round(avg_win, 2) if avg_win > 0 else 0.0)
                
                total_pnl = sum(((float(t[1]) - float(t[0])) / float(t[0])) * 100000 for t in closed_trades)
                
                # 動態算回當週週一的日期
                week_start_dt = (now - datetime.timedelta(days=now.weekday())).strftime('%Y-%m-%d')
                weekly_trades = [t for t in closed_trades if t[3] and t[3] >= week_start_dt]
                weekly_pnl = sum(((float(t[1]) - float(t[0])) / float(t[0])) * 100000 for t in weekly_trades)

                benchmark_0050 = get_0050_weekly_return()

                summary = {
                    "date": today_str,
                    "total": total_trades,
                    "win": wins,
                    "loss": losses,
                    "win_rate": round(win_rate, 2),
                    "weekly_pnl": int(weekly_pnl),
                    "total_pnl": int(total_pnl),
                    "avg_win": round(avg_win, 2),
                    "avg_loss": round(avg_loss, 2),
                    "risk_reward_ratio": rrr,
                    "benchmark_0050": benchmark_0050,
                    "weekly_trades_count": len(weekly_trades)
                }
                
                print(f"📊 [週結算 Summary 產出成功]: {summary}", flush=True)
                sync_to_google_sheets(summary)

        # -------------------------------------------------------------------------
        # B. 買進邏輯 (修正解析 Bug + 調整買進策略)
        # -------------------------------------------------------------------------
        if weekday in [0, 1, 2, 3]:
            cursor.execute("SELECT content FROM history WHERE date = 'LATEST';")
            row = cursor.fetchone()

            if row and row[0]:
                content = row[0]
                
                st1_targets = []
                st2_targets = []
                
                current_strategy = None
                lines = content.split('\n')
                
                for line in lines:
                    line_str = line.strip()
                    if '策略一' in line_str or '策略 1' in line_str:
                        current_strategy = "策略一"
                        continue
                    elif '策略二' in line_str or '策略 2' in line_str:
                        current_strategy = "策略二"
                        continue
                    
                    code_match = re.search(r'([0-9]{4})\s+([\u4e00-\u9fa5A-Za-z0-9\*]+)', line_str)
                    price_match = re.search(r'(?:現價|收盤|收|價格)[:：\s]*\$?\s*([0-9]+\.?[0-9]*)', line_str)
                    
                    if code_match and price_match and current_strategy:
                        code = code_match.group(1)
                        name = code_match.group(2)
                        price = float(price_match.group(1))
                        
                        score_match = re.search(r'(\d+)\s*(?:分|pts)', line_str, re.IGNORECASE)
                        score = int(score_match.group(1)) if score_match else 0
                        
                        item = (code, name, price, current_strategy, score)
                        
                        if current_strategy == "策略一":
                            st1_targets.append(item)
                        elif current_strategy == "策略二":
                            st2_targets.append(item)

                buy_targets = []

                if weekday in [0, 1, 2]:
                    selected_st1 = st1_targets[:5]
                    selected_st2 = st2_targets[:5]
                    
                    buy_targets = selected_st1 + selected_st2
                    print(f"🔥 [週一~週三建倉] 策略一 ({len(selected_st1)}檔) + 策略二 ({len(selected_st2)}檔) | 總預計買入: {len(buy_targets)} 檔", flush=True)

                elif weekday == 3:
                    st2_qualified = [t for t in st2_targets if t[4] >= 100]

                    if st2_qualified:
                        max_score = max(t[4] for t in st2_qualified)
                        buy_targets = [t for t in st2_qualified if t[4] == max_score]
                        print(f"🔥 [週四短線精選] 策略二高分股 ({len(buy_targets)}檔) 進場！", flush=True)
                    else:
                        selected_st1 = st1_targets[:3]
                        buy_targets = selected_st1
                        print(f"⚠️ [週四短線精選] 備案：買入策略一前 {len(buy_targets)} 名！", flush=True)

                for item in buy_targets:
                    code, name, price, st_type = item[0], item[1], item[2], item[3]
                    
                    cursor.execute('''
                        SELECT id FROM sim_trades 
                        WHERE stock_code = %s 
                        AND (
                            DATE_TRUNC('week', buy_date::date) = DATE_TRUNC('week', %s::date)
                            OR (sell_date IS NOT NULL AND DATE_TRUNC('week', sell_date::date) = DATE_TRUNC('week', %s::date))
                        );
                    ''', (code, today_str, today_str))
                    
                    if cursor.fetchone():
                        print(f"🚫 [當週防護跳過] {code} {name} 本週已有交易紀錄！", flush=True)
                        continue

                    cursor.execute('''
                        INSERT INTO sim_trades (stock_code, stock_name, strategy_type, buy_date, buy_price, status)
                        VALUES (%s, %s, %s, %s, %s, 'HOLD');
                    ''', (code, name, st_type, today_str, price))
                    print(f"🛒 [模擬買入成功] [{st_type}] {code} {name} | 掛單成交價: ${price:.2f}", flush=True)

        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"❌ [Sim Engine Error] {e}", flush=True)
    finally:
        conn.close()

if __name__ == "__main__":
    init_sim_db()
    process_simulation()
