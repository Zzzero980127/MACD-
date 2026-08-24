import psycopg2
import datetime
import os
import pandas as pd
import requests

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()

# 🎯 每檔股票固定投入金額 (可自行修改，例如 100000 代表十萬元)
BUDGET_PER_STOCK = 100000  

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_paper_db():
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS paper_portfolio (
                id SERIAL PRIMARY KEY,
                stock_id VARCHAR(10),
                stock_name VARCHAR(50),
                buy_price NUMERIC,
                shares INT,
                buy_date VARCHAR(20),
                week_str VARCHAR(20),
                status VARCHAR(10) DEFAULT 'HOLDING',
                sell_price NUMERIC,
                return_pct NUMERIC,
                profit_loss NUMERIC,
                CONSTRAINT unique_stock_week UNIQUE (stock_id, week_str)
            );
        ''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Paper DB Init Error: {e}")

init_paper_db()

# 抓取最新即時價格
def get_latest_price(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN:
            url += f"&token={FINMIND_TOKEN}"
        
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=3)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == 200 and data.get("data"):
                df = pd.DataFrame(data["data"])
                return float(df.iloc[-1]['close'])
    except Exception:
        pass
    return None

# 自動寫入買進 (根據固定預算算股數)
def auto_execute_paper_buy(top3_list, today_str, week_str):
    if not DATABASE_URL or not top3_list:
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for item in top3_list:
            stock_id = item['code']
            stock_name = item['name']
            buy_price = float(item['close'])
            
            # 🎯 根據固定預算動態計算股數 (向下取整數)
            shares = int(BUDGET_PER_STOCK // buy_price) if buy_price > 0 else 0

            if shares > 0:
                cursor.execute('''
                    INSERT INTO paper_portfolio (stock_id, stock_name, buy_price, shares, buy_date, week_str, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'HOLDING')
                    ON CONFLICT (stock_id, week_str) DO NOTHING;
                ''', (stock_id, stock_name, buy_price, shares, today_str, week_str))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Auto Buy Error: {e}")

# 查詢目前持倉
def get_paper_trades_status():
    if not DATABASE_URL:
        return "⚠️ 未連結資料庫，無法查詢模擬持股。"
    
    now_dt = datetime.datetime.now()
    week_str = f"{now_dt.isocalendar()[0]}_W{now_dt.isocalendar()[1]}"
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT stock_id, stock_name, buy_price, shares, buy_date
            FROM paper_portfolio
            WHERE week_str = %s AND status = 'HOLDING';
        ''', (week_str,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        if not rows:
            return f"📦 本週 ({week_str}) 目前尚無模擬持股紀錄。"

        report_lines = [f"💼【本週模擬持倉即時狀態】({week_str})\n• 單檔預算: ${BUDGET_PER_STOCK:,} 元\n"]
        total_pnl = 0

        for r in rows:
            s_id, s_name, buy_p, shares, b_date = r[0], r[1], float(r[2]), int(r[3]), r[4]
            cur_p = get_latest_price(s_id) or buy_p
            
            pnl = (cur_p - buy_p) * shares
            ret_pct = ((cur_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0
            total_pnl += pnl

            icon = "🟢" if pnl >= 0 else "🔴"
            report_lines.append(
                f"{icon} {s_name} ({s_id})\n"
                f"  • 持有股數: {shares:,} 股\n"
                f"  • 進場價格: ${buy_p:.2f} ({b_date})\n"
                f"  • 當前現價: ${cur_p:.2f}\n"
                f"  • 未實現損益: {pnl:+,.0f} 元 ({ret_pct:+.2f}%)\n"
            )

        tot_icon = "🟢" if total_pnl >= 0 else "🔴"
        report_lines.append(f"--------------------\n{tot_icon} 本週持倉總未實現損益: {total_pnl:+,.0f} 元")
        return "\n".join(report_lines)

    except Exception as e:
        return f"⚠️ 查詢持股失敗: {e}"

# 週五尾盤/假日執行平倉結算
def execute_paper_trades_settlement():
    if not DATABASE_URL:
        return "⚠️ 未連結資料庫，無法執行結算。"
    
    now_dt = datetime.datetime.now()
    week_str = f"{now_dt.isocalendar()[0]}_W{now_dt.isocalendar()[1]}"

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, stock_id, stock_name, buy_price, shares
            FROM paper_portfolio
            WHERE week_str = %s AND status = 'HOLDING';
        ''', (week_str,))
        rows = cursor.fetchall()

        if not rows:
            return f"⚠️ 本週 ({week_str}) 沒有進行中的持股可供結算。"

        report_lines = [f"🏆【本週模擬交易平倉結算】({week_str})\n"]
        total_pnl = 0

        for r in rows:
            p_id, s_id, s_name, buy_p, shares = r[0], r[1], r[2], float(r[3]), int(r[4])
            sell_p = get_latest_price(s_id) or buy_p
            pnl = (sell_p - buy_p) * shares
            ret_pct = ((sell_p - buy_p) / buy_p) * 100 if buy_p > 0 else 0
            total_pnl += pnl

            cursor.execute('''
                UPDATE paper_portfolio
                SET status = 'CLOSED', sell_price = %s, return_pct = %s, profit_loss = %s
                WHERE id = %s;
            ''', (sell_p, ret_pct, pnl, p_id))

            icon = "🟢" if pnl >= 0 else "🔴"
            report_lines.append(
                f"{icon} {s_name} ({s_id})\n"
                f"  • 買入價: ${buy_p:.2f} | 賣出價: ${sell_p:.2f}\n"
                f"  • 結算損益: {pnl:+,.0f} 元 ({ret_pct:+.2f}%)\n"
            )

        conn.commit()
        cursor.close()
        conn.close()

        tot_icon = "🟢" if total_pnl >= 0 else "🔴"
        report_lines.append(f"--------------------\n{tot_icon} 本週平倉總獲利: {total_pnl:+,.0f} 元")
        return "\n".join(report_lines)

    except Exception as e:
        return f"⚠️ 結算失敗: {e}"
