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
    """抓取 0050 當週開盤價與最新收盤價，計算當週漲跌幅 (%)"""
    try:
        now = datetime.datetime.now()
        monday_dt = now - datetime.timedelta(days=now.weekday())
        start_date = monday_dt.strftime("%Y-%m-%d")
        
        params = {
            "dataset": "TaiwanStockPrice", 
            "data_id": "0050", 
            "start_date": start_date
        }
        if FINMIND_TOKEN: 
            params["token"] = FINMIND_TOKEN

        res = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, timeout=8).json()
        
        if res.get("data") and len(res["data"]) >= 1:
            df = pd.DataFrame(res["data"])
            week_open = float(df.iloc[0]['open'])
            week_close = float(df.iloc[-1]['close'])
            weekly_return = ((week_close - week_open) / week_open) * 100
            print(f"📈 [0050 當週績效] 週一開盤: {week_open} | 最新收盤: {week_close} | 漲跌幅: {weekly_return:+.2f}%", flush=True)
            return round(weekly_return, 2)
            
    except Exception as e:
        print(f"⚠️ [0050 API Error] 抓取 0050 績效失敗: {e}", flush=True)
        
    return 0.0

def sync_to_google_sheets(summary):
    sheets_json = os.environ.get('GOOGLE_SHEETS_JSON', '').strip()
    sheet_key = os.environ.get('SPREADSHEET_KEY', '1CrADfLGVOhfrhNB_Er-0XJCazb6onD7vjWf7QPdDpO0').strip()

    if not sheets_json:
        print("⚠️ [Google Sheets] 未偵測到 GOOGLE_SHEETS_JSON 環境變數，跳過試算表同步。", flush=True)
        return

    try:
        scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        
        # ---------------------------------------------------------------------
        # 🎯 強化 JSON 字元過濾與控制字元修復機制 (防止 Invalid control character 報錯)
        # ---------------------------------------------------------------------
        try:
            if '\\n' in sheets_json:
                sheets_json = sheets_json.replace('\\n', '\n')
            info = json.loads(sheets_json, strict=False)
        except Exception:
            clean_str = re.sub(r'[\r\n\t]+', ' ', sheets_json)
            info = json.loads(clean_str, strict=False)

        creds = Credentials.from_service_account_info(info, scopes=scope)
        client = gspread.authorize(creds)

        spreadsheet = client.open_by_key(sheet_key)
        sheet = spreadsheet.sheet1

        existing_rows = sheet.get_all_values()
        if not existing_rows:
            header = [
                "結算日期", "交易總筆數", "勝場", "敗場", "勝率 (%)", 
                "週淨損益 ($)", "累積總損益 ($)", "平均獲利 (%)", "平均虧損 (%)", 
                "風報比", "0050 同期漲跌 (%)", "是否擊敗 0050"
            ]
            sheet.append_row(header)

        target_date = summary.get("date", "")
        for row in existing_rows:
            if len(row) > 0 and row[0] == target_date:
                print(f"ℹ️ [Google Sheets] 日期 {target_date} 已存在於試算表中，跳過重複寫入。", flush=True)
                return

        # 計算當週報酬率 (%) 與是否擊敗 0050
        weekly_return_pct = (summary.get("weekly_pnl", 0) / TOTAL_CAPITAL) * 100
        benchmark_0050 = summary.get("benchmark_0050", 0.0)

        if weekly_return_pct >= benchmark_0050:
            beat_0050_str = "🟢 擊敗0050"
        else:
            beat_0050_str = "❌ 落後0050"

        # 🎯 精准對齊 A ~ L 欄位陣列
        row = [
            summary.get("date", ""),                                      # A: 結算日期
            summary.get("total", 0),                                     # B: 交易總筆數 (當週)
            summary.get("win", 0),                                       # C: 勝場 (當週)
            summary.get("loss", 0),                                      # D: 敗場 (當週)
            f"{summary.get('win_rate', 0.0):.2f}%",                      # E: 勝率 (%)
            summary.get("weekly_pnl", 0),                                # F: 週淨損益 ($)
            summary.get("total_pnl", 0),                                 # G: 累積總損益 ($)
            f"{summary.get('avg_win', 0.0):.2f}%",                       # H: 平均獲利 (%)
            f"{summary.get('avg_loss', 0.0):.2f}%",                      # I: 平均虧損 (%)
            summary.get("risk_reward_ratio", 0.0),                       # J: 風報比
            f"{benchmark_0050:+.2f}%" if benchmark_0050 != 0 else "0.00%", # K: 0050 同期漲跌 (%)
            beat_0050_str                                                # L: 是否擊敗 0050
        ]

        sheet.append_row(row)
        print(f"🎉 [Google Sheets] 成功將 {target_date} 週結算資料寫入試算表！", flush=True)
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
        # A. 賣出邏輯 (精準執行 T+2 雙軌出場)
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

                # 風控/MACD 先行判斷
                if ret <= -5.0:
                    should_sell, exit_reason = True, "🚨 大跌觸發止損 (-5%)"
                elif osc_today < osc_p1:
                    should_sell, exit_reason = True, "📉 MACD多頭減弱出場"
                
                # 精準 T+2 日期強制清空規則
                elif weekday == 3 and buy_weekday in [0, 1, 2]:
                    should_sell, exit_reason = True, "📅 週四清空週一至週三持股 (T+2)"
                elif weekday >= 4 and buy_weekday == 3:
                    should_sell, exit_reason = True, "📅 週五清空週四精選短線股 (T+2)"
                elif weekday in [5, 6]:
                    should_sell, exit_reason = True, "📅 週末強制結算殘餘持股"

                if should_sell:
                    cursor.execute('''
                        UPDATE sim_trades 
                        SET sell_date = %s, sell_price = %s, return_rate = %s, status = 'CLOSED', exit_reason = %s
                        WHERE id = %s;
                    ''', (today_str, curr_price, ret, exit_reason, trade_id))
                    print(f"💰 [模擬賣出] {code} {name} | 買價: {buy_price} -> 賣價: {curr_price} | 報酬: {ret:+.2f}% | 原因: {exit_reason}", flush=True)

        # -------------------------------------------------------------------------
        # B. 週結算與同步 (週四平倉後至週末皆可執行)
        # -------------------------------------------------------------------------
        if weekday in [3, 4, 5, 6]:
            cursor.execute("SELECT buy_price, sell_price, return_rate, sell_date FROM sim_trades WHERE status = 'CLOSED';")
            closed_trades = cursor.fetchall()
            
            if len(closed_trades) > 0:
                # 🎯 計算「本週一」與「本週五」日期字串，精準區隔當週交易
                monday_dt = now - datetime.timedelta(days=now.weekday())
                friday_dt = monday_dt + datetime.timedelta(days=4)
                
                start_str = monday_dt.strftime('%Y-%m-%d')
                end_str = friday_dt.strftime('%Y-%m-%d')
                
                # 只採計 sell_date 落在【本週一 ~ 本週五】之間的當週交易
                weekly_trades = [
                    t for t in closed_trades 
                    if t[3] and (start_str <= str(t[3])[:10] <= end_str)
                ]
                
                # 當週勝敗筆數與勝率
                weekly_win_returns = [float(t[2]) for t in weekly_trades if float(t[2]) > 0]
                weekly_loss_returns = [abs(float(t[2])) for t in weekly_trades if float(t[2]) < 0]
                
                weekly_count = len(weekly_trades)
                weekly_wins = len(weekly_win_returns)
                weekly_losses = len(weekly_loss_returns)
                weekly_win_rate = (weekly_wins / weekly_count * 100) if weekly_count > 0 else 0.0
                
                # 平均獲利/虧損與風報比
                avg_win = (sum(weekly_win_returns) / weekly_wins) if weekly_wins > 0 else 0.0
                avg_loss = (sum(weekly_loss_returns) / weekly_losses) if weekly_losses > 0 else 0.0
                rrr = round(avg_win / avg_loss, 2) if avg_loss > 0 else (round(avg_win, 2) if avg_win > 0 else 0.0)
                
                # 當週淨損益與歷史累積總損益 (歷史資料完全保護)
                weekly_pnl = sum(((float(t[1]) - float(t[0])) / float(t[0])) * 100000 for t in weekly_trades)
                total_pnl = sum(((float(t[1]) - float(t[0])) / float(t[0])) * 100000 for t in closed_trades)

                benchmark_0050 = get_0050_weekly_return()

                summary = {
                    "date": end_str,                        # 結算日 (當週五)
                    "total": weekly_count,                  # 當週交易筆數 (例如: 15)
                    "win": weekly_wins,                    # 當週勝場 (例如: 9)
                    "loss": weekly_losses,                  # 當週敗場 (例如: 6)
                    "win_rate": round(weekly_win_rate, 2), # 當週勝率 (%)
                    "weekly_pnl": int(weekly_pnl),          # 當週淨損益 ($)
                    "total_pnl": int(total_pnl),            # 歷史累積總損益 ($)
                    "avg_win": round(avg_win, 2),
                    "avg_loss": round(avg_loss, 2),
                    "risk_reward_ratio": rrr,
                    "benchmark_0050": benchmark_0050
                }
                
                print(f"📊 [週結算 Summary]: {summary}", flush=True)
                sync_to_google_sheets(summary)

        # -------------------------------------------------------------------------
        # C. 買進邏輯 (包含週四買入週三精選股條件)
        # -------------------------------------------------------------------------
        if weekday in [0, 1, 2, 3]:
            cursor.execute("SELECT content FROM history WHERE date = 'LATEST';")
            row = cursor.fetchone()

            if row and row[0]:
                content = row[0]
                st1_targets, st2_targets = [], []
                current_strategy = None
                
                for line in content.split('\n'):
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
                        
                        if current_strategy == "策略一": st1_targets.append(item)
                        elif current_strategy == "策略二": st2_targets.append(item)

                buy_targets = []

                # 週一 ~ 週三建倉
                if weekday in [0, 1, 2]:
                    buy_targets = st1_targets[:5] + st2_targets[:5]

                # 週四建倉：買入週三推薦股 (優先選擇策略二 >= 100 分第 1 名，否則買策略一第 1 名)
                elif weekday == 3:
                    st2_qualified = [t for t in st2_targets if t[4] >= 100]
                    if st2_qualified:
                        max_score = max(t[4] for t in st2_qualified)
                        buy_targets = [t for t in st2_qualified if t[4] == max_score][:1]
                        print(f"🔥 [週四精選買入] 選用策略二最高分 ({buy_targets[0][4]}分) 標的: {buy_targets[0][1]}", flush=True)
                    elif st1_targets:
                        buy_targets = st1_targets[:1]
                        print(f"🔥 [週四精選買入] 策略二未達 100 分，改選策略一第 1 名: {buy_targets[0][1]}", flush=True)

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
    """抓取 0050 當週開盤價與最新收盤價，計算當週漲跌幅 (%)"""
    try:
        now = datetime.datetime.now()
        monday_dt = now - datetime.timedelta(days=now.weekday())
        start_date = monday_dt.strftime("%Y-%m-%d")
        
        params = {
            "dataset": "TaiwanStockPrice", 
            "data_id": "0050", 
            "start_date": start_date
        }
        if FINMIND_TOKEN: 
            params["token"] = FINMIND_TOKEN

        res = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, timeout=8).json()
        
        if res.get("data") and len(res["data"]) >= 1:
            df = pd.DataFrame(res["data"])
            week_open = float(df.iloc[0]['open'])
            week_close = float(df.iloc[-1]['close'])
            weekly_return = ((week_close - week_open) / week_open) * 100
            print(f"📈 [0050 當週績效] 週一開盤: {week_open} | 最新收盤: {week_close} | 漲跌幅: {weekly_return:+.2f}%", flush=True)
            return round(weekly_return, 2)
            
    except Exception as e:
        print(f"⚠️ [0050 API Error] 抓取 0050 績效失敗: {e}", flush=True)
        
    return 0.0

def sync_to_google_sheets(summary):
    sheets_json = os.environ.get('GOOGLE_SHEETS_JSON', '').strip()
    sheet_key = os.environ.get('SPREADSHEET_KEY', '1CrADfLGVOhfrhNB_Er-0XJCazb6onD7vjWf7QPdDpO0').strip()

    if not sheets_json:
        print("⚠️ [Google Sheets] 未偵測到 GOOGLE_SHEETS_JSON 環境變數，跳過試算表同步。", flush=True)
        return

    try:
        scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        
        # ---------------------------------------------------------------------
        # 🎯 強化 JSON 字元過濾與控制字元修復機制 (防止 Invalid control character 報錯)
        # ---------------------------------------------------------------------
        try:
            if '\\n' in sheets_json:
                sheets_json = sheets_json.replace('\\n', '\n')
            info = json.loads(sheets_json, strict=False)
        except Exception:
            clean_str = re.sub(r'[\r\n\t]+', ' ', sheets_json)
            info = json.loads(clean_str, strict=False)

        creds = Credentials.from_service_account_info(info, scopes=scope)
        client = gspread.authorize(creds)

        spreadsheet = client.open_by_key(sheet_key)
        sheet = spreadsheet.sheet1

        existing_rows = sheet.get_all_values()
        if not existing_rows:
            header = [
                "結算日期", "交易總筆數", "勝場", "敗場", "勝率 (%)", 
                "週淨損益 ($)", "累積總損益 ($)", "平均獲利 (%)", "平均虧損 (%)", 
                "風報比", "0050 同期漲跌 (%)", "是否擊敗 0050"
            ]
            sheet.append_row(header)

        target_date = summary.get("date", "")
        for row in existing_rows:
            if len(row) > 0 and row[0] == target_date:
                print(f"ℹ️ [Google Sheets] 日期 {target_date} 已存在於試算表中，跳過重複寫入。", flush=True)
                return

        # 計算當週報酬率 (%) 與是否擊敗 0050
        weekly_return_pct = (summary.get("weekly_pnl", 0) / TOTAL_CAPITAL) * 100
        benchmark_0050 = summary.get("benchmark_0050", 0.0)

        if weekly_return_pct >= benchmark_0050:
            beat_0050_str = "🟢 擊敗0050"
        else:
            beat_0050_str = "❌ 落後0050"

        # 🎯 精准對齊 A ~ L 欄位陣列
        row = [
            summary.get("date", ""),                                      # A: 結算日期
            summary.get("total", 0),                                     # B: 交易總筆數 (當週)
            summary.get("win", 0),                                       # C: 勝場 (當週)
            summary.get("loss", 0),                                      # D: 敗場 (當週)
            f"{summary.get('win_rate', 0.0):.2f}%",                      # E: 勝率 (%)
            summary.get("weekly_pnl", 0),                                # F: 週淨損益 ($)
            summary.get("total_pnl", 0),                                 # G: 累積總損益 ($)
            f"{summary.get('avg_win', 0.0):.2f}%",                       # H: 平均獲利 (%)
            f"{summary.get('avg_loss', 0.0):.2f}%",                      # I: 平均虧損 (%)
            summary.get("risk_reward_ratio", 0.0),                       # J: 風報比
            f"{benchmark_0050:+.2f}%" if benchmark_0050 != 0 else "0.00%", # K: 0050 同期漲跌 (%)
            beat_0050_str                                                # L: 是否擊敗 0050
        ]

        sheet.append_row(row)
        print(f"🎉 [Google Sheets] 成功將 {target_date} 週結算資料寫入試算表！", flush=True)
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
        # A. 賣出邏輯 (精準執行 T+2 雙軌出場)
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

                # 風控/MACD 先行判斷
                if ret <= -5.0:
                    should_sell, exit_reason = True, "🚨 大跌觸發止損 (-5%)"
                elif osc_today < osc_p1:
                    should_sell, exit_reason = True, "📉 MACD多頭減弱出場"
                
                # 精準 T+2 日期強制清空規則
                elif weekday == 3 and buy_weekday in [0, 1, 2]:
                    should_sell, exit_reason = True, "📅 週四清空週一至週三持股 (T+2)"
                elif weekday >= 4 and buy_weekday == 3:
                    should_sell, exit_reason = True, "📅 週五清空週四精選短線股 (T+2)"
                elif weekday in [5, 6]:
                    should_sell, exit_reason = True, "📅 週末強制結算殘餘持股"

                if should_sell:
                    cursor.execute('''
                        UPDATE sim_trades 
                        SET sell_date = %s, sell_price = %s, return_rate = %s, status = 'CLOSED', exit_reason = %s
                        WHERE id = %s;
                    ''', (today_str, curr_price, ret, exit_reason, trade_id))
                    print(f"💰 [模擬賣出] {code} {name} | 買價: {buy_price} -> 賣價: {curr_price} | 報酬: {ret:+.2f}% | 原因: {exit_reason}", flush=True)

        # -------------------------------------------------------------------------
        # B. 週結算與同步 (週四平倉後至週末皆可執行)
        # -------------------------------------------------------------------------
        if weekday in [3, 4, 5, 6]:
            cursor.execute("SELECT buy_price, sell_price, return_rate, sell_date FROM sim_trades WHERE status = 'CLOSED';")
            closed_trades = cursor.fetchall()
            
            if len(closed_trades) > 0:
                # 🎯 計算「本週一」與「本週五」日期字串，精準區隔當週交易
                monday_dt = now - datetime.timedelta(days=now.weekday())
                friday_dt = monday_dt + datetime.timedelta(days=4)
                
                start_str = monday_dt.strftime('%Y-%m-%d')
                end_str = friday_dt.strftime('%Y-%m-%d')
                
                # 只採計 sell_date 落在【本週一 ~ 本週五】之間的當週交易
                weekly_trades = [
                    t for t in closed_trades 
                    if t[3] and (start_str <= str(t[3])[:10] <= end_str)
                ]
                
                # 當週勝敗筆數與勝率
                weekly_win_returns = [float(t[2]) for t in weekly_trades if float(t[2]) > 0]
                weekly_loss_returns = [abs(float(t[2])) for t in weekly_trades if float(t[2]) < 0]
                
                weekly_count = len(weekly_trades)
                weekly_wins = len(weekly_win_returns)
                weekly_losses = len(weekly_loss_returns)
                weekly_win_rate = (weekly_wins / weekly_count * 100) if weekly_count > 0 else 0.0
                
                # 平均獲利/虧損與風報比
                avg_win = (sum(weekly_win_returns) / weekly_wins) if weekly_wins > 0 else 0.0
                avg_loss = (sum(weekly_loss_returns) / weekly_losses) if weekly_losses > 0 else 0.0
                rrr = round(avg_win / avg_loss, 2) if avg_loss > 0 else (round(avg_win, 2) if avg_win > 0 else 0.0)
                
                # 當週淨損益與歷史累積總損益 (歷史資料完全保護)
                weekly_pnl = sum(((float(t[1]) - float(t[0])) / float(t[0])) * 100000 for t in weekly_trades)
                total_pnl = sum(((float(t[1]) - float(t[0])) / float(t[0])) * 100000 for t in closed_trades)

                benchmark_0050 = get_0050_weekly_return()

                summary = {
                    "date": end_str,                        # 結算日 (當週五)
                    "total": weekly_count,                  # 當週交易筆數 (例如: 15)
                    "win": weekly_wins,                    # 當週勝場 (例如: 9)
                    "loss": weekly_losses,                  # 當週敗場 (例如: 6)
                    "win_rate": round(weekly_win_rate, 2), # 當週勝率 (%)
                    "weekly_pnl": int(weekly_pnl),          # 當週淨損益 ($)
                    "total_pnl": int(total_pnl),            # 歷史累積總損益 ($)
                    "avg_win": round(avg_win, 2),
                    "avg_loss": round(avg_loss, 2),
                    "risk_reward_ratio": rrr,
                    "benchmark_0050": benchmark_0050
                }
                
                print(f"📊 [週結算 Summary]: {summary}", flush=True)
                sync_to_google_sheets(summary)

        # -------------------------------------------------------------------------
        # C. 買進邏輯 (包含週四買入週三精選股條件)
        # -------------------------------------------------------------------------
        if weekday in [0, 1, 2, 3]:
            cursor.execute("SELECT content FROM history WHERE date = 'LATEST';")
            row = cursor.fetchone()

            if row and row[0]:
                content = row[0]
                st1_targets, st2_targets = [], []
                current_strategy = None
                
                for line in content.split('\n'):
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
                        
                        if current_strategy == "策略一": st1_targets.append(item)
                        elif current_strategy == "策略二": st2_targets.append(item)

                buy_targets = []

                # 週一 ~ 週三建倉
                if weekday in [0, 1, 2]:
                    buy_targets = st1_targets[:5] + st2_targets[:5]

                # 週四建倉：買入週三推薦股 (優先選擇策略二 >= 100 分第 1 名，否則買策略一第 1 名)
                elif weekday == 3:
                    st2_qualified = [t for t in st2_targets if t[4] >= 100]
                    if st2_qualified:
                        max_score = max(t[4] for t in st2_qualified)
                        buy_targets = [t for t in st2_qualified if t[4] == max_score][:1]
                        print(f"🔥 [週四精選買入] 選用策略二最高分 ({buy_targets[0][4]}分) 標的: {buy_targets[0][1]}", flush=True)
                    elif st1_targets:
                        buy_targets = st1_targets[:1]
                        print(f"🔥 [週四精選買入] 策略二未達 100 分，改選策略一第 1 名: {buy_targets[0][1]}", flush=True)

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
