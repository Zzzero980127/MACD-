import os
import json
import requests
import datetime
import pandas as pd
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()

def get_db_connection():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL)

def get_latest_price(stock_id):
    """取得個股最新收盤價 (FinMind API)"""
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN:
            url += f"&token={FINMIND_TOKEN}"
        
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == 200 and data.get("data"):
                df = pd.DataFrame(data["data"])
                if not df.empty and 'close' in df.columns:
                    return float(df.iloc[-1]['close'])
    except Exception as e:
        print(f"Paper Trading Get Price Error ({stock_id}): {e}")
    return 0.0

def auto_execute_paper_buy(top3_stocks, trade_date_str, week_str):
    """
    自動記錄模擬倉買入標的 (每週/每日觸發)
    """
    if not DATABASE_URL or not top3_stocks:
        return

    try:
        conn = get_db_connection()
        if not conn:
            return
        
        cursor = conn.cursor()

        # 檢查今天或本週是否已經建立過買入紀錄
        cursor.execute("SELECT trade_date FROM paper_trades WHERE trade_date = %s;", (trade_date_str,))
        if cursor.fetchone():
            cursor.close()
            conn.close()
            return  # 今日已記錄過，自動跳過

        # 整理買入股票清單與價格
        stocks_data = []
        buy_prices = {}

        for item in top3_stocks:
            code = item.get('code')
            name = item.get('name')
            price = item.get('close', 0.0)
            
            # 如果傳入價格為 0，嘗試即時抓取
            if price == 0.0:
                price = get_latest_price(code)

            stocks_data.append({
                'code': code,
                'name': name,
                'buy_price': price
            })
            buy_prices[code] = price

        stocks_json = json.dumps(stocks_data, ensure_ascii=False)
        buy_prices_json = json.dumps(buy_prices, ensure_ascii=False)

        cursor.execute('''
            INSERT INTO paper_trades (trade_date, stocks_json, status, buy_prices_json, settlement_json)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (trade_date) DO NOTHING;
        ''', (trade_date_str, stocks_json, 'OPEN', buy_prices_json, '{}'))

        conn.commit()
        cursor.close()
        conn.close()
        print(f"✅ [模擬倉] 已成功紀錄買入組合 ({trade_date_str})！")
    except Exception as e:
        print(f"❌ auto_execute_paper_buy Error: {e}")

def get_paper_trades_status():
    """
    LINE 指令：「模擬持股」或「持倉」
    查詢最新一期的模擬倉持股與當前即時損益
    """
    if not DATABASE_URL:
        return "⚠️ 未設定資料庫，無法讀取模擬持股。"

    try:
        conn = get_db_connection()
        if not conn:
            return "⚠️ 資料庫連線失敗。"

        cursor = conn.cursor()
        cursor.execute('''
            SELECT trade_date, stocks_json, status, buy_prices_json, settlement_json
            FROM paper_trades
            ORDER BY trade_date DESC
            LIMIT 1;
        ''')
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return "📊 目前尚無任何模擬持股紀錄。"

        trade_date, stocks_json, status, buy_prices_json, settlement_json = row
        stocks = json.loads(stocks_json) if stocks_json else []

        if not stocks:
            return "📊 當前模擬倉無持股。"

        lines = [f"💼【AI 模擬倉持股狀態】({trade_date})"]
        lines.append(f"📌 狀態: {'🟢 持股中 (OPEN)' if status == 'OPEN' else '🔴 已結算 (CLOSED)'}\n")

        total_buy_val = 0.0
        total_curr_val = 0.0

        for item in stocks:
            code = item.get('code')
            name = item.get('name')
            buy_p = float(item.get('buy_price', 0.0))

            if status == 'OPEN':
                curr_p = get_latest_price(code)
                if curr_p == 0.0:
                    curr_p = buy_p
            else:
                settlement = json.loads(settlement_json) if settlement_json else {}
                curr_p = float(settlement.get(code, {}).get('sell_price', buy_p))

            ret_pct = ((curr_p - buy_p) / buy_p * 100) if buy_p > 0 else 0.0
            icon = "📈" if ret_pct >= 0 else "📉"

            lines.append(f"{icon} {name} ({code})")
            lines.append(f"   • 買入價: ${buy_p:.2f}")
            lines.append(f"   • 當前價: ${curr_p:.2f} (報酬率: {ret_pct:+.2f}%)\n")

            total_buy_val += buy_p
            total_curr_val += curr_p

        total_ret = ((total_curr_val - total_buy_val) / total_buy_val * 100) if total_buy_val > 0 else 0.0
        lines.append(f"🎯 組合總報酬率: {total_ret:+.2f}%")

        return "\n".join(lines)

    except Exception as e:
        return f"⚠️ 查詢模擬持股失敗: {str(e)}"

def execute_paper_trades_settlement():
    """
    LINE 指令：「結算」或「週結算」
    強制進行最新一期模擬持股的結算並計算最終戰績
    """
    if not DATABASE_URL:
        return "⚠️ 未設定資料庫，無法執行結算。"

    try:
        conn = get_db_connection()
        if not conn:
            return "⚠️ 資料庫連線失敗。"

        cursor = conn.cursor()
        cursor.execute('''
            SELECT trade_date, stocks_json, status, buy_prices_json
            FROM paper_trades
            WHERE status = 'OPEN'
            ORDER BY trade_date DESC
            LIMIT 1;
        ''')
        row = cursor.fetchone()

        if not row:
            cursor.close()
            conn.close()
            return "⚠️ 當前沒有待結算 (OPEN) 的模擬持股組合。"

        trade_date, stocks_json, status, buy_prices_json = row
        stocks = json.loads(stocks_json) if stocks_json else []

        settlement_dict = {}
        total_buy_val = 0.0
        total_sell_val = 0.0

        lines = [f"🏆【AI 模擬倉週結算報告】({trade_date})"]
        lines.append("--------------------")

        for item in stocks:
            code = item.get('code')
            name = item.get('name')
            buy_p = float(item.get('buy_price', 0.0))
            sell_p = get_latest_price(code)
            
            if sell_p == 0.0:
                sell_p = buy_p

            ret_pct = ((sell_p - buy_p) / buy_p * 100) if buy_p > 0 else 0.0
            settlement_dict[code] = {
                'buy_price': buy_p,
                'sell_price': sell_p,
                'return_pct': ret_pct
            }

            icon = "🟢" if ret_pct >= 0 else "🔴"
            lines.append(f"{icon} {name} ({code})")
            lines.append(f"   • 買入: ${buy_p:.2f} ➔ 賣出: ${sell_p:.2f}")
            lines.append(f"   • 戰績: {ret_pct:+.2f}%\n")

            total_buy_val += buy_p
            total_sell_val += sell_p

        total_ret = ((total_sell_val - total_buy_val) / total_buy_val * 100) if total_buy_val > 0 else 0.0
        lines.append("--------------------")
        lines.append(f"🎯 本期組合最終總戰績: {total_ret:+.2f}%")

        # 更新資料庫狀態為 CLOSED
        cursor.execute('''
            UPDATE paper_trades
            SET status = 'CLOSED', settlement_json = %s
            WHERE trade_date = %s;
        ''', (json.dumps(settlement_dict, ensure_ascii=False), trade_date))

        conn.commit()
        cursor.close()
        conn.close()

        return "\n".join(lines)

    except Exception as e:
        return f"⚠️ 執行結算失敗: {str(e)}"
