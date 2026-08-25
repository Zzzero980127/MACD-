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
# 1. 環境變數設定 (精準對齊你的控制台 Key 名稱)
# -----------------------------------------------------------------------------
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()
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
    # 帶上標準瀏覽器標頭，防 TWSE/FinMind 阻擋 Python requests
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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
# 3. 核心個股分析
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
        
        # Token 異常自動退回匿名請求
        if res_p.status_code in [401, 403]:
            res_p = http.get(price_url, params=params, timeout=8.0)

        if res_p.status_code != 200 or not res_p.json().get("data"):
            print(f"  ❌ [{stock_id} {stock_name}] K線 API 請求失敗 (HTTP {res_p.status_code})", flush=True)
            return None
        
        df = pd.DataFrame(res_p.json()["data"]).rename(
            columns={'close': 'Close', 'Trading_Volume': 'Volume', 'max': 'High', 'min': 'Low', 'open': 'Open'}
        )
        for col in ['Close', 'Volume', 'High', 'Low', 'Open']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            
        df = df.dropna(subset=['Close', 'Volume', 'High', 'Low', 'Open'])
        if len(df) < 35:
            print(f"  ⚠️ [{stock_id} {stock_name}] K線資料不足 35 天", flush=True)
            return None

        # --- MACD 與均線計算 ---
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
        osc_p2 = float(prev2['OSC'])
        osc_p3 = float(prev3['OSC'])

        close_price = float(latest['Close'])
        prev_close = float(prev1['Close'])
        today_volume = float(latest['Volume'])
        vol_ma5 = float(latest['Vol_MA5'])
        pct_change = ((close_price - prev_close) / prev_close) * 100

        # 通用初始門檻 (當日 OSC 轉折 + 防追高)
        if osc_today <= osc_p1:
            print(f"  🚫 [{stock_id} {stock_name}] 濾除: MACD未轉折 (OSC {osc_today:.3f} <= 昨天 {osc_p1:.3f})", flush=True)
            return None

        if pct_change > 6.5 or pct_change < -5.0:
            print(f"  🚫 [{stock_id} {stock_name}] 濾除: 防追高/急跌門檻 ({pct_change:+.2f}%)", flush=True)
            return None

        # ---------------------------------------------------------------------
        # 籌碼面資料（三大法人與外資買超）
        # ---------------------------------------------------------------------
        time.sleep(0.2)
        chip_start = (datetime.datetime.now() - datetime.timedelta(days=15)).strftime("%Y-%m-%d")
        chip_params = {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": stock_id,
            "start_date": chip_start
        }
        
        today_total = 0
        today_foreign = 0
        prev_foreign = 0
        today_trust = 0

        res_c = http.get(price_url, params=chip_params, headers=headers, timeout=6.0)
        if res_c.status_code in [401, 403]:
            res_c = http.get(price_url, params=chip_params, timeout=6.0)

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

        # ---------------------------------------------------------------------
        # 🎯 策略二專屬：防死貓跳與洗盤結束判定
        # ---------------------------------------------------------------------
        osc_3day_declining = (osc_p3 > osc_p2) and (osc_p2 > osc_p1)
        
        # 外資買超反轉量能判定 (防死貓跳)
        if prev_foreign > 0:
            foreign_surge_valid = (today_foreign >= prev_foreign * 3)
        else:
            # 若前日賣超，今日買超量必須大於前日賣超的絕對值
            foreign_surge_valid = (today_foreign > abs(prev_foreign))

        is_wash_breakout = (
            osc_3day_declining and 
            (osc_today > osc_p1) and 
            foreign_surge_valid and 
            (1.0 <= pct_change <= 5.5)
        )

        # ---------------------------------------------------------------------
        # 4. 基礎計分與 Tag 標記
        # ---------------------------------------------------------------------
        score = 50
        tags = []

        # MACD 狀態精確比對
        if osc_today > 0 and osc_p1 <= 0:
            score += 15
            tags.append("💥綠轉紅第1天(金叉形成)")
        elif osc_today < 0 and osc_p1 < 0:
            score += 20
            tags.append("📉綠柱止跌")
        elif osc_today > 0 and osc_p1 > 0:
            tags.append("🔥紅柱延伸") # 不加分

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

        print(f"  ✅ [{stock_id} {stock_name}] 得分:{score} | 洗盤起漲:{is_wash_breakout} | 外資今日:{round(today_foreign)}張 (前日:{round(prev_foreign)}張)", flush=True)

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
# 4. 主流程 (策略池互斥分流)
# -----------------------------------------------------------------------------
def run_precalculation():
    print("==================================================", flush=True)
    print(f"🚀 [Cron Job] 開始執行 AI 排程選股 ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M')})...", flush=True)

    if FINMIND_TOKEN:
        masked_token = FINMIND_TOKEN[:4] + "..." + FINMIND_TOKEN[-4:] if len(FINMIND_TOKEN) > 8 else "***"
        print(f"🔑 [Token Check] 載入 FINMIND_API_TOKEN 成功 ({masked_token})", flush=True)
    else:
        print("⚠️ [Token Check] 未偵測到 FINMIND_API_TOKEN，將以匿名模式執行", flush=True)

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
            print(f"✅ [TWSE Log] 成功取得成交量前 200 大個股清單！", flush=True)
        else:
            print(f"❌ [TWSE Log] 證交所 API 拒絕存取，HTTP Code: {res.status_code}", flush=True)
            return
    except Exception as e:
        print(f"❌ [TWSE Log] 讀取失敗: {e}", flush=True)
        return

    all_passed_stocks = []

    for idx, stock_info in enumerate(candidates, 1):
        res = fetch_finmind_data(stock_info)
        if res:
            all_passed_stocks.append(res)
        time.sleep(0.4)

    # -------------------------------------------------------------------------
    # 🎯 雙策略分流與互斥篩選
    # -------------------------------------------------------------------------
    wash_breakout_stocks = [s for s in all_passed_stocks if s['is_wash_breakout']]
    wash_breakout_stocks.sort(key=lambda x: x['score'], reverse=True)
    top_wash_breakout = wash_breakout_stocks[:5]

    strategy_1_candidates = [s for s in all_passed_stocks if not s['is_wash_breakout']]
    strategy_1_candidates.sort(key=lambda x: x['score'], reverse=True)
    top_bottom_turn = strategy_1_candidates[:5]

    print(f"📈 [Log 統計] 策略一可選標的: {len(strategy_1_candidates)} 檔 | 策略二洗盤起漲標的: {len(wash_breakout_stocks)} 檔", flush=True)

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
                f"   👉 得分:{item['score']}分 | {item['status_label']}"
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
