import os
import json
import datetime
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_paper_db():
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # 建立模擬交易紀錄表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS paper_trades (
                week_id VARCHAR(20) PRIMARY KEY,
                trade_date VARCHAR(20),
                stocks_json TEXT,
                status VARCHAR(10) DEFAULT 'HOLDING',
                buy_budget_per_stock NUMERIC DEFAULT 100000,
                settle_date VARCHAR(20),
                total_pnl NUMERIC DEFAULT 0,
                total_return_pct NUMERIC DEFAULT 0
            );
        ''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Paper DB Init Error: {e}")

init_paper_db()

# ----------------------------------------------------
# 1. 自動執行模擬買入 (週一至週三，且全台股掃完一輪後)
# ----------------------------------------------------
def auto_execute_paper_buy(top3_stocks, today_str, week_str):
    if not DATABASE_URL or not top3_stocks:
        return

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 檢查本週是否已經建立過模擬買單
        cursor.execute('SELECT week_id FROM paper_trades WHERE week_id = %s;', (week_str,))
        if cursor.fetchone():
            cursor.close()
            conn.close()
            return  # 本週已有紀錄，不重複建倉

        buy_records = []
        budget = 100000  # 每一檔預算 10 萬台幣

        for item in top3_stocks[:3]:
            close_price = float(item['close'])
            if close_price > 0:
                shares = int((budget / close_price) // 1000) * 1000  # 優先買整張 (1,000股)
                if shares == 0:
                    shares = int(budget // close_price)  # 若不滿一張則買零股
                
                cost = round(shares * close_price)
                buy_records.append({
                    'code': item['code'],
                    'name': item['name'],
                    'buy_price': close_price,
                    'shares': shares,
                    'cost': cost
                })

        if buy_records:
            cursor.execute('''
                INSERT INTO paper_trades (week_id, trade_date, stocks_json, status, buy_budget_per_stock)
                VALUES (%s, %s, %s, 'HOLDING', %s);
            ''', (week_str, today_str, json.dumps(buy_records), budget))
            conn.commit()
            print(f"✅ [模擬交易] 已成功自動為本週 ({week_str}) 建倉 Top 3 標的！")

        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Auto Paper Buy Error: {e}")

# ----------------------------------------------------
# 2. 查詢當前模擬持倉狀態 (LINE 指令: 持股 / PAPER)
# ----------------------------------------------------
def get_paper_trades_status():
    if not DATABASE_URL:
        return "⚠️ 未連接資料庫，無法查詢模擬持倉。"

    try:
        # 取得當前週別
        now_dt = datetime.datetime.now()
        week_str = f"{now_dt.isocalendar()[0]}_W{now_dt.isocalendar()[1]}"

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT trade_date, stocks_json, status FROM paper_trades WHERE week_id = %s;', (week_str,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return f"📂 【本週 ({week_str}) 模擬持倉】\n目前尚無建倉紀錄。(系統需在週一至週三掃描完成後自動執行建倉)"

        trade_date, stocks_json, status = row[0], json.loads(row[1]), row[2]
        
        status_tag = "🟢 持有中" if status == 'HOLDING' else "🔴 已平倉結算"
        msg = [f"💼 【本週模擬持倉明細】 ({status_tag})", f"📅 進場日期: {trade_date}\n"]

        from app import get_tw_stock_data_finmind  # 動態抓取最新價格

        total_cost = 0
        total_current_val = 0

        for stock in stocks_json:
            code = stock['code']
            name = stock['name']
            buy_price = stock['buy_price']
            shares = stock['shares']
            cost = stock['cost']

            # 抓取最新收盤價
            df = get_tw_stock_data_finmind(code)
            current_price = float(df.iloc[-1]['Close']) if df is not None and not df.empty else buy_price

            current_val = round(shares * current_price)
            pnl = current_val - cost
            ret_pct = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0.0

            total_cost += cost
            total_current_val += current_val

            msg.append(
                f"📌 {name} ({code})\n"
                f"  • 進場價: ${buy_price:.2f} | 現價: ${current_price:.2f}\n"
                f"  • 持有股數: {shares:,} 股 (成本 ${cost:,})\n"
                f"  • 未實現損益: {pnl:+,.0f} 元 ({ret_pct:+.2f}%)"
            )

        total_pnl = total_current_val - total_cost
        total_ret = ((total_current_val - total_cost) / total_cost) * 100 if total_cost > 0 else 0.0

        msg.append("--------------------")
        msg.append(f"📊 預估總市值: ${total_current_val:,} 元")
        msg.append(f"💰 預估總損益: {total_pnl:+,.0f} 元 ({total_ret:+.2f}%)")

        return "\n\n".join(msg)

    except Exception as e:
        return f"⚠️ 查詢模擬持倉失敗: {str(e)}"

# ----------------------------------------------------
# 3. 執行模擬平倉結算 (LINE 指令: 結算 / CLOSE)
# ----------------------------------------------------
def execute_paper_trades_settlement():
    if not DATABASE_URL:
        return "⚠️ 未連接資料庫，無法執行結算。"

    try:
        now_dt = datetime.datetime.now()
        week_str = f"{now_dt.isocalendar()[0]}_W{now_dt.isocalendar()[1]}"
        settle_date_str = now_dt.strftime("%Y-%m-%d")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT trade_date, stocks_json, status FROM paper_trades WHERE week_id = %s;', (week_str,))
        row = cursor.fetchone()

        if not row:
            cursor.close()
            conn.close()
            return f"⚠️ 本週 ({week_str}) 尚無模擬持倉紀錄，無法進行結算。"

        trade_date, stocks_json, status = row[0], json.loads(row[1]), row[2]

        if status == 'SETTLED':
            cursor.close()
            conn.close()
            return f"⚠️ 本週 ({week_str}) 持倉先前已完成平倉結算，請勿重複結算！"

        from app import get_tw_stock_data_finmind

        total_cost = 0
        total_settle_val = 0
        msg = [f"🏁 【本週 ({week_str}) 模擬持倉平倉結算報告】", f"📅 進場日: {trade_date} ➔ 結算日: {settle_date_str}\n"]

        for stock in stocks_json:
            code = stock['code']
            name = stock['name']
            buy_price = stock['buy_price']
            shares = stock['shares']
            cost = stock['cost']

            df = get_tw_stock_data_finmind(code)
            settle_price = float(df.iloc[-1]['Close']) if df is not None and not df.empty else buy_price

            settle_val = round(shares * settle_price)
            pnl = settle_val - cost
            ret_pct = ((settle_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0.0

            total_cost += cost
            total_settle_val += settle_val

            msg.append(
                f"📌 {name} ({code})\n"
                f"  • 買入價: ${buy_price:.2f} ➔ 結算價: ${settle_price:.2f}\n"
                f"  • 最終損益: {pnl:+,.0f} 元 ({ret_pct:+.2f}%)"
            )

        total_pnl = total_settle_val - total_cost
        total_ret = ((total_settle_val - total_cost) / total_cost) * 100 if total_cost > 0 else 0.0

        # 更新資料庫平倉狀態
        cursor.execute('''
            UPDATE paper_trades
            SET status = 'SETTLED', settle_date = %s, total_pnl = %s, total_return_pct = %s
            WHERE week_id = %s;
        ''', (settle_date_str, total_pnl, total_ret, week_str))

        conn.commit()
        cursor.close()
        conn.close()

        msg.append("--------------------")
        msg.append(f"💰 本週最終實現總損益: {total_pnl:+,.0f} 元")
        msg.append(f"📈 本週總報酬率: {total_ret:+.2f}%")
        msg.append("🎉 結算完成！狀態已更新為【已平倉】。")

        return "\n\n".join(msg)

    except Exception as e:
        return f"⚠️ 執行結算發生錯誤: {str(e)}"
