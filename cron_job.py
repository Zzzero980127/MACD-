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

# 🔐 環境變數設定
ENV_TOKEN = os.environ.get('FINMIND_TOKEN', '').strip()
HARDCODED_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9..." # ⚠️ 備用 Token
FINMIND_TOKEN = ENV_TOKEN if len(ENV_TOKEN) > 20 else HARDCODED_TOKEN
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()

def create_robust_session():
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
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception: return None

def save_to_db(report_text, date_str="LATEST"):
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
    """ 📢 自動發送 LINE 訊息給所有加好友的使用者 """
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("⚠️ [LINE Log] 未設定 LINE_CHANNEL_ACCESS_TOKEN，跳過主動推播！", flush=True)
        return
    try:
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        # 使用廣播功能推播給所有追蹤者
        line_bot_api.broadcast(TextSendMessage(text=report_text))
        print("📣 [LINE Log] 已成功將選股結果推播至 LINE！", flush=True)
    except Exception as e:
        print(f"❌ [LINE Log] LINE 推播發送失敗: {e}", flush=True)

def fetch_finmind_data(stock_info):
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

        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['OSC'] = df['DIF'] - df['MACD']

        osc_today = float(df.iloc[-1]['OSC'])
        osc_prev = float(df.iloc[-2]['OSC'])
        close_price = float(df.iloc[-1]['Close'])
        prev_close = float(df.iloc[-2]['Close'])
        pct_change = ((close_price - prev_close) / prev_close) * 100

        if pct_change > 6.0: return None # 🛡️ 防追高：漲超過 6% 直接不考慮

        score = 0
        tags = []

        is_green_shrinking = (osc_today < 0) and (osc_today > osc_prev)
        is_first_red = (osc_today > 0) and (osc_prev <= 0)
        is_macd_expanding = (osc_today > 0) and (osc_today > osc_prev)

        if is_green_shrinking:
            score += 20
            macd_status = "📉綠柱縮短"
        elif is_first_red:
            score += 25
            macd_status = "💥紅柱第1天"
        elif is_macd_expanding:
            score += 15
            macd_status = "🔥紅柱擴大"
        else:
            return None

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
                    
                    if today_foreign > 50 and prev_foreign <= 50:
                        score += 20
                        tags.append("🔄外資轉買")
                    elif today_foreign > 200 and prev_foreign > 200:
                        score += 25
                        tags.append("🔥外資連買")

                    # ⚡ 裕民加分邏輯：外資買超比昨日暴增 3 倍
                    if (prev_foreign > 0) and (today_foreign >= prev_foreign * 3) and (today_foreign >= 500) and is_macd_expanding:
                        score += 35
                        tags.append("⚡外資爆買3倍")

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
    print("==================================================", flush=True)
    print("🚀 [Cron Job] 開始執行 AI 排程選股與自動推播...", flush=True)
    if FINMIND_TOKEN and len(FINMIND_TOKEN) > 20:
        print(f"🔑 [Token Log] 成功載入 FinMind Token (前5碼: {FINMIND_TOKEN[:5]}...)", flush=True)
    else:
        print("⚠️ [Token Log] 未檢測到有效 Token，將以無密鑰模式運行！", flush=True)
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
        lines.append("💡 策略說明：採用評分架構，優先推薦兼具「外資急煞暴買、洗盤再起漲」之標的。")
        report = "\n".join(lines)

    # 1. 儲存資料庫
    save_to_db(report, "LATEST")
    save_to_db(report, today_str)

    # 2. 📢 執行 LINE 主動推播！
    send_line_push(report)

    print("🎉 [Cron Job Log] 排程選股與 LINE 推播發送完畢！", flush=True)

if __name__ == "__main__":
    run_precalculation()
