import os
import time
import requests
from requests.adapters import HTTPAdapter
import pandas as pd
import datetime
import psycopg2
import fcntl  # Linux / Render 防重複執行鎖
from linebot import LineBotApi
from linebot.models import TextSendMessage

# 🤖 匯入模擬倉模組
try:
    import sim_portfolio
except ImportError:
    sim_portfolio = None

# -----------------------------------------------------------------------------
# 1. 環境變數設定與 Token 檢查
# -----------------------------------------------------------------------------
FINMIND_TOKEN = (os.environ.get('FINMIND_API_TOKEN') or os.environ.get('FINMIND_TOKEN', '')).strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()

if not FINMIND_TOKEN:
    print("❌ [Fatal Error] 未偵測到 FINMIND_API_TOKEN 或 FINMIND_TOKEN！程式強制終止。", flush=True)
    exit(1)

# -----------------------------------------------------------------------------
# 🔒 防重複執行機制 (File Locking)
# -----------------------------------------------------------------------------
LOCK_FILE_PATH = "/tmp/cron_job.lock"
lock_file = open(LOCK_FILE_PATH, "w")

try:
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
except IOError:
    print("⚠️ [Lock Log] 偵測到已有另一個選股程序正在執行中，自動終止！", flush=True)
    exit(0)

# -----------------------------------------------------------------------------
# 2. 連線與工具函式
# -----------------------------------------------------------------------------
def create_robust_session():
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=1)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    return session

http = create_robust_session()

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

def save_to_db(report_text, date_str="LATEST"):
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                date VARCHAR(20) PRIMARY KEY, 
                content TEXT NOT NULL
            );
        ''')
        cursor.execute('''
            INSERT INTO history (date, content) 
            VALUES (%s, %s) 
            ON CONFLICT (date) 
            DO UPDATE SET content = EXCLUDED.content;
        ''', (date_str, report_text))
        conn.commit()
        cursor.close()
        conn.close()
        print(f"💾 [DB Log] 報告成功寫入資料庫 (Key: {date_str})", flush=True)
    except Exception as e:
        print(f"⚠️ [DB Log] 資料庫寫入失敗: {e}", flush=True)

def send_line_push(report_text):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("⚠️ [LINE Log] 未設定 LINE Token，略過推播", flush=True)
        return
    try:
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        line_bot_api.broadcast(TextSendMessage(text=report_text))
        print("✅ [LINE Log] LINE 推播成功發送！", flush=True)
    except Exception as e:
        print(f"❌ [LINE Log] LINE 推播發送失敗: {e}", flush=True)

# -----------------------------------------------------------------------------
# 3. 兩階段核心個股分析
# -----------------------------------------------------------------------------

# ⚡ 第一階段：技術面初篩 (1秒1檔)
def check_technical_pass(stock_info, current_idx, total_count):
    stock_id = stock_info["code"]
    stock_name = stock_info["name"]
    prefix = f"[{current_idx}/{total_count}]"
    
    start_date = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    api_url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": stock_id,
        "start_date": start_date,
        "token": FINMIND_TOKEN
    }
    
    res = None
    try:
        res = http.get(api_url, params=params, timeout=5.0)
    except Exception as e:
        print(f"  ⚪ {prefix} [{stock_id} {stock_name}] 連線超時，跳過", flush=True)
        return None

    if not res or res.status_code != 200 or not res.json().get("data"):
        status_msg = res.status_code if res else "No Response"
        print(f"  ⚪ {prefix} [{stock_id} {stock_name}] 無資料或 API 異常 (Status: {status_msg})，跳過", flush=True)
        return None
        
    df = pd.DataFrame(res.json()["data"]).rename(
        columns={'close': 'Close', 'Trading_Volume': 'Volume', 'max': 'High', 'min': 'Low', 'open': 'Open'}
    )
    for col in ['Close', 'Volume', 'High', 'Low', 'Open']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
        
    df = df.dropna(subset=['Close', 'Volume', 'High', 'Low', 'Open'])
    if len(df) < 35:
        print(f"  ⚪ {prefix} [{stock_id} {stock_name}] K線天數不足 35 天，跳過", flush=True)
        return None

    # MACD & 均線
    exp1 = df['Close'].ewm(span=12, adjust=False).mean()
    exp2 = df['Close'].ewm(span=26, adjust=False).mean()
    df['DIF'] = exp1 - exp2
    df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
    df['OSC'] = df['DIF'] - df['MACD']

    df['MA5'] = df['Close'].rolling(window=5).mean()
    df['MA20'] = df['Close'].rolling(window=20).mean()
    df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()

    latest = df.iloc[-1]
    prev1 = df.iloc[-2]
    prev2 = df.iloc[-3]
    prev3 = df.iloc[-4]

    osc_today = float(latest['OSC'])
    osc_p1 = float(prev1['OSC'])
    close_price = float(latest['Close'])
    prev_close = float(prev1['Close'])
    pct_change = ((close_price - prev_close) / prev_close) * 100

    if osc_today <= osc_p1:
        print(f"  🔍 {prefix} [{stock_id} {stock_name}] MACD 未轉折，淘汰", flush=True)
        return None  

    if pct_change > 6.5 or pct_change < -5.0:
        print(f"  🔍 {prefix} [{stock_id} {stock_name}] 漲跌幅過大 ({pct_change:+.2f}%)，淘汰", flush=True)
        return None  

    # 🛑 新增：上影線過長檢測（第一階段直接淘汰極端上影線）
    high_p = float(latest['High'])
    low_p = float(latest['Low'])
    open_p = float(latest['Open'])
    
    total_range = high_p - low_p
    upper_shadow = high_p - max(open_p, close_price)
    
    # 若當日上影線佔總波幅超過 60%，代表衝高大回吐，直接淘汰
    if total_range > 0 and (upper_shadow / total_range) > 0.60:
        print(f"  🔍 {prefix} [{stock_id} {stock_name}] 衝高大砸盤(上影線過長: {(upper_shadow/total_range)*100:.1f}%)，淘汰", flush=True)
        return None

    print(f"  🎯 {prefix} [{stock_id} {stock_name}] 通過初篩 (漲跌: {pct_change:+.2f}%)，入圍！", flush=True)

    return {
        "code": stock_id,
        "name": stock_name,
        "df": df,
        "latest": latest,
        "prev1": prev1,
        "prev2": prev2,
        "prev3": prev3,
        "pct_change": pct_change,
        "close_price": close_price
    }

# 🔍 第二階段：籌碼與綜合評分
def fetch_chip_and_score(tech_data):
    stock_id = tech_data["code"]
    stock_name = tech_data["name"]
    latest = tech_data["latest"]
    prev1 = tech_data["prev1"]
    prev2 = tech_data["prev2"]
    prev3 = tech_data["prev3"]
    close_price = tech_data["close_price"]
    pct_change = tech_data["pct_change"]

    api_url = "https://api.finmindtrade.com/api/v4/data"
    chip_start = (datetime.datetime.now() - datetime.timedelta(days=15)).strftime("%Y-%m-%d")
    chip_params = {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id": stock_id,
        "start_date": chip_start,
        "token": FINMIND_TOKEN
    }
    
    today_total, today_foreign, prev_foreign, today_trust = 0, 0, 0, 0

    try:
        res_c = http.get(api_url, params=chip_params, timeout=5.0)
        if res_c.status_code == 200 and res_c.json().get("data"):
            df_c = pd.DataFrame(res_c.json()["data"])
            if not df_c.empty:
                df_c['net_buy'] = (pd.to_numeric(df_c['buy'], errors='coerce').fillna(0) - pd.to_numeric(df_c['sell'], errors='coerce').fillna(0)) / 1000
                
                daily_total = df_c.groupby('date')['net_buy'].sum().reset_index(name='total_net')
                foreign_df = df_c[df_c['name'].astype(str).str.contains('Foreign|外資', case=False)].groupby('date')['net_buy'].sum().reset_index(name='foreign_net')
                trust_df = df_c[df_c['name'].astype(str).str.contains('Trust|投信', case=False)].groupby('date')['net_buy'].sum().reset_index(name='trust_net')
                
                daily_chip = daily_total.merge(foreign_df, on='date', how='left').merge(trust_df, on='date', how='left').fillna(0).sort_values('date')
                
                if len(daily_chip) >= 2:
                    today_total = float(daily_chip.iloc[-1]['total_net'])
                    today_foreign = float(daily_chip.iloc[-1]['foreign_net'])
                    prev_foreign = float(daily_chip.iloc[-2]['foreign_net'])
                    today_trust = float(daily_chip.iloc[-1]['trust_net'])
    except Exception as e:
        print(f"  ⚠️ [{stock_id} {stock_name}] 籌碼抓取失敗，以 0 計算: {e}", flush=True)

    # 策略計算與計分
    dif_today = float(latest['DIF'])
    osc_today = float(latest['OSC'])
    osc_p1 = float(prev1['OSC'])
    osc_p2 = float(prev2['OSC'])
    osc_p3 = float(prev3['OSC'])
    today_volume = float(latest['Volume'])
    vol_ma5 = float(latest['Vol_MA5'])

    osc_3day_declining = (osc_p3 > osc_p2) and (osc_p2 > osc_p1)
    is_above_zero_axis = (osc_today > 0) or (dif_today > 0)
    
    if prev_foreign > 0:
        foreign_surge_valid = (today_foreign >= prev_foreign * 3)
    else:
        foreign_surge_valid = (today_foreign > abs(prev_foreign))

    is_wash_breakout = (
        is_above_zero_axis and 
        osc_3day_declining and 
        (osc_today > osc_p1) and 
        foreign_surge_valid and 
        (1.0 <= pct_change <= 5.5)
    )

    score = 50
    tags = []

    if osc_today > 0 and osc_p1 <= 0:
        score += 15
        tags.append("💥綠轉紅第1天(金叉形)")
    elif osc_today < 0 and osc_p1 < 0:
        if (osc_today > osc_p1) and (osc_p2 > osc_p1):
            score += 30
            tags.append("📉綠柱極限止跌V轉")
        else:
            score += 20
            tags.append("📉綠柱止跌")
    elif osc_today > 0 and osc_p1 > 0:
        tags.append("🔥紅柱延伸")

    if close_price >= float(latest['MA20']):
        score += 10
        tags.append("🛡️站上月線")
    elif close_price > float(latest['MA5']):
        score += 5
        tags.append("⚡站上5日線")

    if today_volume >= vol_ma5 * 1.2:
        score += 10
        tags.append("🚀帶量攻擊")

    if today_total >= 10000:
        score += 25
        tags.append(f"⚡萬張爆買({round(today_total)}張)")
    elif today_total >= 3000:
        score += 15
        tags.append(f"🔥法人大買({round(today_total)}張)")
    elif today_total >= 800:
        score += 10
        tags.append(f"🔄法人買超({round(today_total)}張)")

    if today_foreign > 0 and today_trust > 0:
        score += 10
        tags.append("🤝土洋同買")

    if is_wash_breakout:
        score += 20
        tags.append("⚡洗盤結束起漲")

    # 🛑 新增：中度上影線扣分機制（大幅扣分 -30 分，避免高檔套牢股高居第一名）
    high_p = float(latest['High'])
    low_p = float(latest['Low'])
    open_p = float(latest['Open'])
    
    total_range = high_p - low_p
    upper_shadow = high_p - max(open_p, close_price)
    
    if total_range > 0:
        shadow_ratio = upper_shadow / total_range
        if shadow_ratio >= 0.40:
            score -= 30
            tags.append("⚠️留長上影線(-30分)")

    return {
        "code": stock_id,
        "name": stock_name,
        "close": close_price,
        "pct": pct_change,
        "score": score,
        "is_wash_breakout": is_wash_breakout,
        "status_label": " ".join(tags)
    }

# -----------------------------------------------------------------------------
# 4. 主流程
# -----------------------------------------------------------------------------
def run_precalculation():
    print("==================================================", flush=True)
    print(f"🚀 [Cron Job] 開始執行 AI 排程選股 ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M')})...", flush=True)

    twse_url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    candidates = []
    
    try:
        res = http.get(twse_url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            stocks = []
            for item in data:
                code = item.get("Code", "").strip()
                name = item.get("Name", "").strip()
                if len(code) == 4 and code.isdigit():
                    try:
                        vol = int(item.get("TradeVolume", 0))
                        stocks.append({"code": code, "name": name, "volume": vol})
                    except ValueError:
                        continue
            
            df_stocks = pd.DataFrame(stocks).sort_values(by=["volume", "code"], ascending=[False, True])
            candidates = df_stocks.head(200).to_dict('records')
            print(f"✅ [TWSE Log] 成功取得成交量前 200 大個股清單！", flush=True)
        else:
            print(f"❌ [TWSE Log] 證交所 API 拒絕存取，HTTP Code: {res.status_code}", flush=True)
            return
    except Exception as e:
        print(f"❌ [TWSE Log] 讀取失敗: {e}", flush=True)
        return

    print("--------------------------------------------------", flush=True)
    print("⚡ [Phase 1] 開始進行技術面形態極速初篩 (排除 MACD 未轉折/急漲跌/極端上影線)...", flush=True)
    tech_passed_list = []
    total_candidates = len(candidates)

    for idx, stock_info in enumerate(candidates, 1):
        pass_data = check_technical_pass(stock_info, idx, total_candidates)
        if pass_data:
            tech_passed_list.append(pass_data)
        # 1 秒 1 檔黃金節奏
        time.sleep(1.0)

    print(f"✅ [Phase 1 完成] 200 檔個股初篩完畢，共有 {len(tech_passed_list)} 檔符合型態標的！", flush=True)

    print("--------------------------------------------------", flush=True)
    print("🔍 [Phase 2] 開始對入圍個股進行法人籌碼深度分析與扣分評估...", flush=True)
    all_passed_stocks = []

    for idx, tech_data in enumerate(tech_passed_list, 1):
        scored_stock = fetch_chip_and_score(tech_data)
        all_passed_stocks.append(scored_stock)
        print(f"  ✅ [{idx}/{len(tech_passed_list)}] {scored_stock['code']} {scored_stock['name']} 評分完成: {scored_stock['score']}分", flush=True)
        time.sleep(1.0)

    wash_breakout_stocks = [s for s in all_passed_stocks if s['is_wash_breakout']]
    wash_breakout_stocks.sort(key=lambda x: x['score'], reverse=True)
    top_wash_breakout = wash_breakout_stocks[:5]

    strategy_1_candidates = [s for s in all_passed_stocks if not s['is_wash_breakout']]
    strategy_1_candidates.sort(key=lambda x: x['score'], reverse=True)
    top_bottom_turn = strategy_1_candidates[:5]

    date_display = datetime.datetime.now().strftime('%Y/%m/%d')
    today_str = datetime.datetime.now().strftime('%Y%m%d')

    lines = [
        f"📊 【AI 精選雙策略雙軌選股報告】({date_display})",
        "===================="
    ]

    lines.append("🌱 【策略一：底部止跌 + 法人合買翻多】")
    lines.append("💡 特性：空轉多拐點，低基期、獲利空間極大 (已排除策略二標的)")
    lines.append("--------------------")
    if not top_bottom_turn:
        lines.append("今日暫無符合條件之標的。")
    else:
        for idx, item in enumerate(top_bottom_turn):
            lines.append(
                f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"    👉 得分:{item['score']}分 | {item['status_label']}"
            )
            if idx < len(top_bottom_turn) - 1:
                lines.append("┈┈┈┈┈┈┈┈┈┈")

    lines.append("\n====================\n")

    lines.append("🔥 【策略二：洗盤結束 + 外資3倍/反轉暴買突破】")
    lines.append("💡 特性：OSC連3跌後翻紅 + 外資買超大於前日賣超/3倍買超 (當日漲幅<5.5%)")
    lines.append("--------------------")
    if not top_wash_breakout:
        lines.append("今日暫無符合條件之標的。")
    else:
        for idx, item in enumerate(top_wash_breakout):
            lines.append(
                f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"    👉 得分:{item['score']}分 | {item['status_label']}"
            )
            if idx < len(top_wash_breakout) - 1:
                lines.append("┈┈┈┈┈┈┈┈┈┈")

    report = "\n".join(lines)

    save_to_db(report, "LATEST")
    save_to_db(report, today_str)
    send_line_push(report)

    print("\n🎉 [Cron Job Log] 排程選股與 LINE 推播全數完畢！", flush=True)

if __name__ == "__main__":
    run_precalculation()
