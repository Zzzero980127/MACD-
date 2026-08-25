import os
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import pandas as pd
import datetime
import psycopg2
from linebot import LineBotApi
from linebot.models import TextSendMessage

# -----------------------------------------------------------------------------
# 1. 環境變數設定（專用 Token 機制）
# -----------------------------------------------------------------------------
FINMIND_TOKEN = (
    os.environ.get('FINMIND_API_TOKEN', '').strip() or 
    os.environ.get('FINMIND_TOKEN', '').strip()
)

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()

# -----------------------------------------------------------------------------
# 2. 連線與工具函式
# -----------------------------------------------------------------------------
def create_robust_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http = create_robust_session()

def get_db_connection():
    if not DATABASE_URL:
        return None
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
    if not conn:
        return
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
# 3. 核心個股分析 (Cron Job：使用 Token 獲取穩定高額度)
# -----------------------------------------------------------------------------
def fetch_finmind_data(stock_info):
    stock_id = stock_info["code"]
    stock_name = stock_info["name"]
    
    start_date = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    
    headers = {}
    if FINMIND_TOKEN:
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"

    price_url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockPrice",
        "data_id": stock_id,
        "start_date": start_date
    }
    
    try:
        res_p = http.get(price_url, params=params, headers=headers, timeout=8.0)
        if res_p.status_code != 200 or not res_p.json().get("data"):
            print(f"  ❌ [{stock_id} {stock_name}] K線 API 失敗 (HTTP {res_p.status_code})", flush=True)
            return None
        
        df = pd.DataFrame(res_p.json()["data"]).rename(
            columns={'close': 'Close', 'Trading_Volume': 'Volume', 'max': 'High', 'min': 'Low', 'open': 'Open'}
        )
        for col in ['Close', 'Volume', 'High', 'Low', 'Open']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            
        df = df.dropna(subset=['Close', 'Volume', 'High', 'Low', 'Open'])
        if len(df) < 30:
            print(f"  ⚠️ [{stock_id} {stock_name}] K線資料不足 30 天", flush=True)
            return None

        # --- MACD 指標計算 ---
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

        osc_today = float(latest['OSC'])
        osc_p1 = float(prev1['OSC'])
        osc_p2 = float(prev2['OSC'])

        close_price = float(latest['Close'])
        prev_close = float(prev1['Close'])
        today_volume = float(latest['Volume'])
        vol_ma5 = float(latest['Vol_MA5'])
        pct_change = ((close_price - prev_close) / prev_close) * 100

        # ---------------------------------------------------------------------
        # 🎯 唯一硬性篩選門檻：MACD 空方力道下降 (今天 OSC > 昨天 OSC)
        # ---------------------------------------------------------------------
        if osc_today <= osc_p1:
            print(f"  🚫 [{stock_id} {stock_name}] 跳過: MACD 未見轉折 (今日OSC {osc_today:.3f} <= 昨天 {osc_p1:.3f})", flush=True)
            return None

        if pct_change > 9.8 or pct_change < -7.0:
            print(f"  🚫 [{stock_id} {stock_name}] 跳過: 漲跌幅過大 ({pct_change:.2f}%)", flush=True)
            return None

        # ---------------------------------------------------------------------
        # 💯 計分與標籤系統
        # ---------------------------------------------------------------------
        score = 50
        tags = []

        # 1. MACD 加分
        if osc_today > 0 and osc_p1 <= 0:
            score += 15
            tags.append("💥綠轉紅第1天")
        elif osc_today < 0:
            score += 10
            tags.append("📉綠柱止跌")
        else:
            score += 10
            tags.append("🔥紅柱延伸")

        # 2. 均線加分
        ma5_today = float(latest['MA5'])
        ma20_today = float(latest['MA20'])
        if close_price >= ma20_today:
            score += 10
            tags.append("🛡️站上月線")
        elif close_price > ma5_today:
            score += 5
            tags.append("⚡站上5日線")

        # 3. 量能加分
        is_volume_breakout = (today_volume >= vol_ma5 * 1.2)
        if is_volume_breakout:
            score += 10
            tags.append("🚀帶量攻擊")

        # 4. 籌碼面（三大法人）
        time.sleep(0.3)
        chip_start = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        chip_params = {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": stock_id,
            "start_date": chip_start
        }
        
        today_total = 0
        today_foreign = 0
        today_trust = 0

        res_c = http.get(price_url, params=chip_params, headers=headers, timeout=6.0)
        if res_c.status_code == 200 and res_c.json().get("data"):
            df_c = pd.DataFrame(res_c.json()["data"])
            if not df_c.empty:
                df_c['net_buy'] = (pd.to_numeric(df_c['buy'], errors='coerce').fillna(0) - pd.to_numeric(df_c['sell'], errors='coerce').fillna(0)) / 1000
                
                daily_total = df_c.groupby('date')['net_buy'].sum().reset_index(name='total_net')
                foreign_df = df_c[df_c['name'].astype(str).str.contains('Foreign|外資', case=False)].groupby('date')['net_buy'].sum().reset_index(name='foreign_net')
                trust_df = df_c[df_c['name'].astype(str).str.contains('Trust|投信', case=False)].groupby('date')['net_buy'].sum().reset_index(name='trust_net')
                
                daily_chip = daily_total.merge(foreign_df, on='date', how='left').merge(trust_df, on='date', how='left').fillna(0).sort_values('date')
                
                if len(daily_chip) >= 1:
                    today_total = float(daily_chip.iloc[-1]['total_net'])
                    today_foreign = float(daily_chip.iloc[-1]['foreign_net'])
                    today_trust = float(daily_chip.iloc[-1]['trust_net'])

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

        # ---------------------------------------------------------------------
        # 🔍 洗盤後起漲點 獨立判定
        # ---------------------------------------------------------------------
        is_wash_breakout = False
        if (osc_p1 < osc_p2 or float(prev1['Close']) <= float(prev2['Close'])) and (pct_change >= 0.5) and (is_volume_breakout or today_total >= 1000):
            is_wash_breakout = True
            score += 15
            tags.append("⚡洗盤結束起漲")

        # 終端機詳細 Log 輸出
        print(f"  ✅ [選中標的] {stock_id} {stock_name} | 收盤:{close_price} ({pct_change:+.2f}%) | 得分:{score} | 法人買超:{round(today_total)}張 | 洗盤突破:{is_wash_breakout}", flush=True)

        return {
            "code": stock_id,
            "name": stock_name,
            "close": close_price,
            "pct": pct_change,
            "score": score,
            "is_wash_breakout": is_wash_breakout,
            "status_label": " ".join(tags)
        }
    except Exception as e:
        print(f"  ❌ [{stock_id} {stock_name}] 運算異常: {e}", flush=True)
        return None

# -----------------------------------------------------------------------------
# 4. 主流程與 Top 5 + Top 5 輸出
# -----------------------------------------------------------------------------
def run_precalculation():
    print("==================================================", flush=True)
    print(f"🚀 [Cron Job Log] 開始執行 AI 排程選股 ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M')})...", flush=True)
    
    if not FINMIND_TOKEN:
        print("❌ [Fatal Error] FINMIND_API_TOKEN 未設定或讀取失敗！", flush=True)
        return
    else:
        masked_token = FINMIND_TOKEN[:4] + "..." + FINMIND_TOKEN[-4:] if len(FINMIND_TOKEN) > 8 else "***"
        print(f"🔑 [Token Check] 成功載入 FinMind Token ({masked_token})", flush=True)

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
            
            df_stocks = pd.DataFrame(stocks).sort_values(by="volume", ascending=False)
            candidates = df_stocks.head(200).to_dict('records')
            print(f"✅ [TWSE Log] 成功取得成交量前 200 大個股清單！開始進行逐一分析...", flush=True)
    except Exception as e:
        print(f"❌ [TWSE Log] 證交所 API 讀取失敗: {e}", flush=True)
        return

    all_passed_stocks = []
    wash_breakout_stocks = []

    for idx, stock_info in enumerate(candidates, 1):
        print(f"📊 [{idx}/{len(candidates)}] 分析中: {stock_info['code']} {stock_info['name']}", flush=True)
        res = fetch_finmind_data(stock_info)
        if res:
            all_passed_stocks.append(res)
            if res['is_wash_breakout']:
                wash_breakout_stocks.append(res)
            
        time.sleep(0.3)

    # 依得分高低排序
    all_passed_stocks.sort(key=lambda x: x['score'], reverse=True)
    wash_breakout_stocks.sort(key=lambda x: x['score'], reverse=True)

    # 兩邊各取前 5 名
    top_bottom_turn = all_passed_stocks[:5]
    top_wash_breakout = wash_breakout_stocks[:5]

    print(f"📈 [Log 統計] 滿足 MACD 轉折標的共 {len(all_passed_stocks)} 檔，符合洗盤突破標的共 {len(wash_breakout_stocks)} 檔", flush=True)

    date_display = datetime.datetime.now().strftime('%Y/%m/%d')
    today_str = datetime.datetime.now().strftime('%Y%m%d')

    lines = [
        f"📊 【AI 精選雙策略雙軌選股報告】({date_display})",
        "===================="
    ]

    lines.append("🌱 【策略一：底部止跌 + 法人合買翻多】")
    lines.append("💡 特性：空轉多拐點，低基期、獲利空間極大")
    lines.append("--------------------")
    if not top_bottom_turn:
        lines.append("今日暫無符合條件之標的。")
    else:
        for idx, item in enumerate(top_bottom_turn):
            lines.append(
                f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"   👉 得分:{item['score']}分 | {item['status_label']}"
            )
            if idx < len(top_bottom_turn) - 1:
                lines.append("┈┈┈┈┈┈┈┈┈┈")

    lines.append("\n====================\n")

    lines.append("🔥 【策略二：洗盤結束 + 法人暴買突破】")
    lines.append("💡 特性：主力洗盤完成，短線發動拉升即戰力")
    lines.append("--------------------")
    if not top_wash_breakout:
        lines.append("今日暫無符合條件之標的。")
    else:
        for idx, item in enumerate(top_wash_breakout):
            lines.append(
                f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"   👉 得分:{item['score']}分 | {item['status_label']}"
            )
            if idx < len(top_wash_breakout) - 1:
                lines.append("┈┈┈┈┈┈┈┈┈┈")

    report = "\n".join(lines)

    save_to_db(report, "LATEST")
    save_to_db(report, today_str)
    send_line_push(report)
    
    print("🎉 [Cron Job Log] 排程選股與推播全數完畢！", flush=True)

if __name__ == "__main__":
    run_precalculation()
