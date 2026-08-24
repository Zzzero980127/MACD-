import psycopg2
import datetime
import requests
import pandas as pd
import os

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_paper_db():
    if not DATABASE_URL:
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS paper_trades (
                id SERIAL PRIMARY KEY,
                buy_date VARCHAR(20),
                buy_week VARCHAR(20),
                code VARCHAR(10),
                name VARCHAR(50),
                buy_price REAL,
                sell_date VARCHAR(20),
                sell_price REAL,
                status VARCHAR(20) DEFAULT 'OPEN',
                exit_reason VARCHAR(100),
                return_pct REAL
            );
        ''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Paper DB Init Error: {e}")

init_paper_db()

def get_stock_kline_data(stock_id):
    try:
        start_date = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
        url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"
        if FINMIND_TOKEN:
            url += f"&token={FINMIND_TOKEN}"
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=4)
        if res.status_code == 200 and res.json().get("data"):
            df = pd.DataFrame(res.json()["data"])
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df = df.dropna(subset=['close'])
            if len(df) >= 20:
                exp1 = df['close'].ewm(span=12, adjust=False).mean()
                exp2 = df['close'].ewm(span=26, adjust=False).mean()
                df['DIF'] = exp1 - exp2
                df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
                df['Hist'] = df['DIF'] - df['MACD']
                return df
    except Exception:
        pass
    return None

def auto_execute_paper_buy(top_stocks, buy_date_str, week_str):
    """
    掃描完 1,800 檔瞬間觸發：自動建立前 3 名模擬持股
    """
    if not DATABASE_URL or not top_stocks:
        return
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for item in top_stocks:
            cursor.execute('''
                INSERT INTO paper_trades (buy_date, buy_week, code, name, buy_price, status)
                VALUES (%s, %s, %s, %s, %s, 'OPEN');
            ''', (buy_date_str, week_str, item['code'], item['name'], item['close']))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Auto Paper Buy Error: {e}")

def check_and_update_daily_exits():
    """
    每日盤後風控檢測：
    1. 跌破 -3% 硬停損
    2. MACD 多方力道下降
    """
    if not DATABASE_URL:
        return []
    alerts = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, buy_date, code, name, buy_price FROM paper_trades WHERE status = 'OPEN';")
        rows = cursor.fetchall()
        today_str = datetime.datetime.now().strftime("%Y%m%d")

        for r in rows:
            t_id, b_date, code, name, b_price = r
            df = get_stock_kline_data(code)
            if df is None or len(df) < 2:
                continue

            latest = df.iloc[-1]
            prev = df.iloc[-2]

            curr_price = float(latest['close'])
            hist_today = float(latest['Hist'])
            hist_prev = float(prev['Hist'])

            # 扣除單趟買賣預估手續費與證交稅共 0.5%
            ret_pct = ((curr_price - b_price) / b_price - 0.005) * 100

            if ret_pct <= -3.0:
                cursor.execute('''
                    UPDATE paper_trades
                    SET sell_date = %s, sell_price = %s, status = 'CLOSED_STOP_LOSS', 
                        exit_reason = '🛑 觸發 -3%% 硬性停損', return_pct = %s
                    WHERE id = %s;
                ''', (today_str, curr_price, ret_pct, t_id))
                alerts.append(f"🛑【停損賣出提醒】{name}({code}) 跌破 3% 停損 ({ret_pct:.2f}%)，已模擬賣出！")

            elif hist_today < hist_prev:
                cursor.execute('''
                    UPDATE paper_trades
                    SET sell_date = %s, sell_price = %s, status = 'CLOSED_MACD_DROP', 
                        exit_reason = '📉 MACD 多方力道下降 (擬傳簡訊)', return_pct = %s
                    WHERE id = %s;
                ''', (today_str, curr_price, ret_pct, t_id))
                alerts.append(f"📱【簡訊平倉提醒】{name}({code}) MACD多方力道減弱，建議賣出 ({ret_pct:+.2f}%)！")

        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Daily Exit Check Error: {e}")
    return alerts

def get_paper_trades_status():
    """
    LINE 指令：「模擬持股」呼叫
    """
    if not DATABASE_URL:
        return "⚠️ 未連接資料庫"
    try:
        check_and_update_daily_exits()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT buy_date, code, name, buy_price FROM paper_trades WHERE status = 'OPEN';")
        open_rows = cursor.fetchall()

        cursor.execute("SELECT name, code, sell_date, exit_reason, return_pct FROM paper_trades WHERE status LIKE 'CLOSED%%' ORDER BY id DESC LIMIT 5;")
        closed_rows = cursor.fetchall()
        cursor.close()
        conn.close()

        msg = "📝 【當前模擬持股與風控監控】\n--------------------\n"

        if not open_rows:
            msg += "目前無持股中標的 (等待掃描完畢或已全數平倉)。\n"
        else:
            for r in open_rows:
                df = get_stock_kline_data(r[1])
                curr_price = float(df.iloc[-1]['close']) if df is not None else r[3]
                ret = ((curr_price - r[3]) / r[3] - 0.005) * 100
                msg += f"📌 {r[2]} ({r[1]})\n  • 買價: ${r[3]:.2f} | 現價: ${curr_price:.2f}\n  • 即時損益: {ret:+.2f}%\n  • 風控: -3%停損 | MACD下降賣出\n\n"

        if closed_rows:
            msg += "\n📜 【近期平倉賣出紀錄】:\n--------------------\n"
            for cr in closed_rows:
                msg += f"🏁 {cr[0]} ({cr[1]}) | {cr[2]}\n  • 原因: {cr[3]}\n  • 損益: {cr[4]:+.2f}%\n"

        return msg.strip()
    except Exception as e:
        return f"查閱失敗: {e}"

def execute_paper_trades_settlement():
    """
    LINE 指令：「結算」呼叫（週五尾盤平倉與雙維度統計報告）
    """
    if not DATABASE_URL:
        return "⚠️ 未連接資料庫"
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. 將尚未平倉的持股在週五強制進行收盤價結算
        cursor.execute("SELECT id, buy_date, code, name, buy_price FROM paper_trades WHERE status = 'OPEN';")
        open_rows = cursor.fetchall()
        today_str = datetime.datetime.now().strftime("%Y%m%d")

        for r in open_rows:
            t_id, b_date, code, name, b_price = r
            df = get_stock_kline_data(code)
            s_price = float(df.iloc[-1]['close']) if df is not None else b_price
            ret = ((s_price - b_price) / b_price - 0.005) * 100

            cursor.execute('''
                UPDATE paper_trades
                SET sell_date = %s, sell_price = %s, status = 'CLOSED_FRIDAY', 
                    exit_reason = '📅 週五尾盤定時結算', return_pct = %s
                WHERE id = %s;
            ''', (today_str, s_price, ret, t_id))

        conn.commit()

        # 2. 抓取歷史所有已平倉資料進行統計
        cursor.execute("SELECT return_pct FROM paper_trades WHERE status LIKE 'CLOSED%%';")
        all_closed = cursor.fetchall()
        cursor.close()
        conn.close()

        if not all_closed:
            return "⚠️ 目前尚無任何已結算的平倉交易資料！"

        returns = [r[0] for r in all_closed]
        win_trades = [r for r in returns if r > 0]

        total_trades = len(returns)
        win_rate = (len(win_trades) / total_trades) * 100
        avg_ret = sum(returns) / total_trades

        # 計算等權重（假設每檔固定投入 10 萬元）的實際獲利金額
        fixed_capital_per_trade = 100000
        total_pnl_dollars = sum([fixed_capital_per_trade * (r / 100.0) for r in returns])

        return (
            f"📊 【策略模擬總體檢測報告】\n"
            f"--------------------\n"
            f"🎯 體檢一：選股邏輯純勝率\n"
            f"  • 累積總交易筆數: {total_trades} 筆\n"
            f"  • 勝率 (Win Rate): {win_rate:.1f}% ({len(win_trades)}勝 / {total_trades - len(win_trades)}敗)\n"
            f"  • 單筆平均淨報酬率: {avg_ret:+.2f}% (已扣0.5%稅費)\n"
            f"--------------------\n"
            f"💰 體檢二：等權重實戰金額 (每檔固定 $10 萬)\n"
            f"  • 累積總獲利台幣: ${total_pnl_dollars:+,.0f} 元\n"
            f"--------------------\n"
            f"💡 風控設定: -3%硬停損 | MACD下降賣出 | 週五定時結算"
        )
    except Exception as e:
        return f"結算失敗: {e}"
