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

# 🔐 強制讀取環境變數 (同時相容 FINMIND_TOKEN 與 FINMIND_API_TOKEN)
FINMIND_TOKEN = (
    os.environ.get('FINMIND_TOKEN', '').strip() or 
    os.environ.get('FINMIND_API_TOKEN', '').strip()
)

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()

def create_robust_session():
    """建立帶有自動重試機制的 HTTP Session，提升 API 連線穩定度"""
    session = requests.Session()
    retries = Retry(
        total=3, backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504], raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http = create_robust_session()

def get_db_connection():
    """建立 PostgreSQL 資料庫連線"""
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception: return None

def save_to_db(report_text, date_str="LATEST"):
    """將產生的選股報告存入資料庫歷史紀錄"""
    conn = get_db_connection()
    if not conn:
        print("❌ [DB Log] 資料庫未連線，無法存入報告！", flush=True)
        return
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS history (
                date VARCHAR(20) PRIMARY KEY, content TEXT NOT NULL
            );
        ''')
        cursor.execute('''
            INSERT INTO history (date, content) VALUES (%s, %s)
            ON CONFLICT (date) DO UPDATE SET content = EXCLUDED.content;
        ''', (date_str, report_text))
        conn.commit()
        cursor.close()
        conn.close()
        print(f"✅ [DB Log] 寫入成功！紀錄日期: {date_str}", flush=True)
    except Exception as e:
        print(f"❌ [DB Log] 寫入資料庫失敗: {e}", flush=True)

def send_line_push(report_text):
    """將選股結果主動推播至 LINE"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("⚠️ [LINE Log] 未設定 LINE_CHANNEL_ACCESS_TOKEN，跳過主動推播！", flush=True)
        return
    try:
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        line_bot_api.broadcast(TextSendMessage(text=report_text))
        print("📣 [LINE Log] 已成功將選股結果推播至 LINE！", flush=True)
    except Exception as e:
        print(f"❌ [LINE Log] LINE 推播發送失敗: {e}", flush=True)

def fetch_finmind_data(stock_info):
    """分析單檔股票的 MACD 與籌碼條件並進行評分"""
    stock_id = stock_info["code"]
    stock_name = stock_info["name"]
    
    start_date = (datetime.datetime.now() - datetime.timedelta(days=60)).strftime("%Y-%m-%d")
    price_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
    
    try:
        res_p = http.get(price_url, timeout=8.0)
        if res_p.status_code != 200 or not res_p.json().get("data"):
            return None
        
        df = pd.DataFrame(res_p.json()["data"]).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
        df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
        df = df.dropna(subset=['Close'])
        if len(df) < 35: return None

        # 計算 MACD 技術指標
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['OSC'] = df['DIF'] - df['MACD']

        # 抓取近 4 天的 MACD 柱狀體 (OSC)
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
        pct_change = ((close_price - prev_close) / prev_close) * 100

        # 過濾當天漲幅過高 (> 6%) 的股票，避免追高
        if pct_change > 6.0: return None

        score = 0
        tags = []

        # --- MACD 狀態判定 ---
        is_green_shrinking = (osc_today < 0) and (osc_today > osc_p1)
        is_first_red = (osc_today > 0) and (osc_p1 <= 0)
        is_macd_expanding = (osc_today > 0) and (osc_today > osc_p1)

        # 🎯 洗盤判定：前幾天紅柱「連續遞減」
        is_red_shrinking_2days = (osc_p1 > 0) and (osc_p2 > osc_p1)
        is_red_shrinking_3days = (osc_p1 > 0) and (osc_p3 > osc_p2 > osc_p1)

        if is_green_shrinking:
            score += 30
            macd_status = "📉綠柱縮短"
        elif is_first_red:
            score += 20
            macd_status = "💥紅柱第1天"
        elif is_macd_expanding:
            score += 0   # 🎯 純紅柱擴大不給基礎分
            macd_status = "🔥紅柱擴大"
        else:
            return None  # 綠柱擴大或紅柱縮短直接排除

        time.sleep(0.8)
        chip_start = (datetime.datetime.now() - datetime.timedelta(days=12)).strftime("%Y-%m-%d")
        chip_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={chip_start}&token={FINMIND_TOKEN}"
        
        res_c = http.get(chip_url, timeout=8.0)
        if res_c.status_code == 200 and res_c.json().get("data"):
            df_c = pd.DataFrame(res_c.json()["data"])
            foreign_df = df_c[df_c['name'].str.contains('Foreign|外資', case=False, na=False)].copy()
            if not foreign_df.empty:
                foreign_df['net_buy'] = (foreign_df['buy'] - foreign_df['sell']) / 1000
                daily_summary = foreign_df.groupby('date')['net_buy'].sum().reset_index().sort_values('date')
                
                if len(daily_summary) >= 2:
                    today_foreign = float(daily_summary.iloc[-1]['net_buy'])
                    prev_foreign = float(daily_summary.iloc[-2]['net_buy'])
                    
                    # 🎯 【外資爆量/轉買核心判定】(門檻提升至 1000 張)
                    is_foreign_turn_buy_surge = (prev_foreign <= 0) and (today_foreign >= 1000)
                    is_foreign_3x_surge = (prev_foreign > 0) and (today_foreign >= prev_foreign * 3) and (today_foreign >= 1000)
                    is_foreign_surge = is_foreign_turn_buy_surge or is_foreign_3x_surge

                    if today_foreign > 50 and prev_foreign <= 50:
                        tags.append("🔄外資轉買")

                    # 🎯 【組合條件大額加分】
                    if is_macd_expanding and is_red_shrinking_3days and is_foreign_surge:
                        score += 45
                        tags.append("⚡3日洗盤突破+外資爆買")
                    elif is_macd_expanding and is_red_shrinking_2days and is_foreign_surge:
                        score += 40
                        tags.append("⚡2日洗盤突破+外資爆買")
                    elif is_macd_expanding and is_foreign_surge:
                        score += 30
                        tags.append("⚡紅柱擴大+外資爆買")

                    if 1.0 <= pct_change <= 4.0:
                        score += 15
                        tags.append("🛡️黃金位階")

                    if score >= 40:
                        return {
                            "code": stock_id, "name": stock_name, "close": close_price,
                            "pct": pct_change, "foreign_shares": round(today_foreign),
                            "score": score, "status_label": " ".join(tags) if tags else "籌碼轉佳",
                            "macd_status": macd_status
                        }
    except Exception as e:
        print(f"  └─ ⚠️ [{stock_id} {stock_name}] 分析異常: {e}", flush=True)
    return None

def run_precalculation():
    """主執行函式：抓取 Top 200 個股進行篩選與推播"""
    print("==================================================", flush=True)
    print("🚀 [Cron Job] 開始執行 AI 排程選股與自動推播...", flush=True)
    
    if not FINMIND_TOKEN:
        print("❌ [Token Error] 未檢測到 FINMIND_TOKEN 或 FINMIND_API_TOKEN 環境變數！程式中止！", flush=True)
        print("==================================================", flush=True)
        return
    else:
        print(f"🔑 [Token Log] 成功載入 FinMind Token (前5碼: {FINMIND_TOKEN[:5]}...)", flush=True)
        
    print("==================================================", flush=True)

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
                    except ValueError: continue
            
            df_stocks = pd.DataFrame(stocks).sort_values(by="volume", ascending=False)
            candidates = df_stocks.head(200).to_dict('records')
            print(f"✅ [TWSE Log] 成功取得 Top 200 熱門個股！", flush=True)
    except Exception as e:
        print(f"❌ [TWSE Log] 證交所 API 抓取失敗: {e}", flush=True)
        return

    selected_stocks = []
    for stock_info in candidates:
        res = fetch_finmind_data(stock_info)
        if res:
            print(f"  └─ 🎯 [選中標的] [{res['code']} {res['name']}] 得分:{res['score']} | {res['macd_status']} | {res['status_label']}", flush=True)
            selected_stocks.append(res)
        time.sleep(1.2)

    selected_stocks.sort(key=lambda x: x['score'], reverse=True)

    today_str = datetime.datetime.now().strftime('%Y%m%d')
    date_display = datetime.datetime.now().strftime('%Y/%m/%d')

    if not selected_stocks:
        report = f"📅 【AI 今日 Top200 轉折起漲精選】({date_display})\n--------------------\n今日未有符合高分標準之標的。"
    else:
        lines = [f"🔥 【AI 精選：Top200 底部轉折 + 洗盤爆發股】({date_display})", "--------------------"]
        for item in selected_stocks:
            lines.append(
                f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"   👉 得分: {item['score']}分 | {item['macd_status']} | {item['status_label']}"
            )
        lines.append("--------------------")
        lines.append("💡 策略說明：優先推薦經連日洗盤後，今日突破且外資暴買（>=1000張或前日3倍）之起漲標的。")
        report = "\n".join(lines)

    save_to_db(report, "LATEST")
    save_to_db(report, today_str)
    send_line_push(report)

    print("🎉 [Cron Job Log] 排程選股與 LINE 推播發送完畢！", flush=True)

if __name__ == "__main__":
    run_precalculation()
