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
# 1. 環境變數設定
# -----------------------------------------------------------------------------
FINMIND_TOKEN = (
    os.environ.get('FINMIND_TOKEN', '').strip() or 
    os.environ.get('FINMIND_API_TOKEN', '').strip()
)

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()

# -----------------------------------------------------------------------------
# 2. Session 與 DB / LINE 工具
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
        print("✅ [LINE Log] 推播發送成功！", flush=True)
    except Exception as e:
        print(f"❌ [LINE Log] LINE 推播發送失敗: {e}", flush=True)

# -----------------------------------------------------------------------------
# 3. 核心選股與加分機制
# -----------------------------------------------------------------------------
def fetch_finmind_data(stock_info):
    stock_id = stock_info["code"]
    stock_name = stock_info["name"]
    
    start_date = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    price_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}&token={FINMIND_TOKEN}"
    
    try:
        res_p = http.get(price_url, timeout=8.0)
        if res_p.status_code != 200 or not res_p.json().get("data"):
            return None
        
        df = pd.DataFrame(res_p.json()["data"]).rename(
            columns={'close': 'Close', 'Trading_Volume': 'Volume', 'max': 'High', 'min': 'Low', 'open': 'Open'}
        )
        for col in ['Close', 'Volume', 'High', 'Low', 'Open']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            
        df = df.dropna(subset=['Close', 'Volume', 'High', 'Low', 'Open'])
        if len(df) < 30:
            return None

        # --- 技術指標計算 ---
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['OSC'] = df['DIF'] - df['MACD']

        df['MA5'] = df['Close'].rolling(window=5).mean()
        df['MA10'] = df['Close'].rolling(window=10).mean()
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

        # =========================================================================
        # 🎯 【唯一硬性篩選門檻】：MACD 空方力道下降 (今天 OSC > 昨天 OSC)
        # =========================================================================
        if osc_today <= osc_p1:
            return None

        # 基本基礎過濾：避開極端跌停或高檔大洗盤噴出 (>9.5% 或 <-5%)
        if pct_change > 9.5 or pct_change < -5.0:
            return None

        # =========================================================================
        # 💯 【全加分演算法 (Scoring)】
        # =========================================================================
        score = 50  # 基礎分（只要滿足 MACD 空方力道下降就有 50 分）
        tags = []

        # 1. MACD 型態加分
        is_first_red = (osc_today > 0) and (osc_p1 <= 0)
        if is_first_red:
            score += 15
            tags.append("💥綠轉紅第1天")
        elif osc_today < 0:
            score += 10
            tags.append("📉綠柱止跌")
        else:
            score += 10
            tags.append("🔥紅柱延伸")

        # 2. 均線位階加分
        ma5_today = float(latest['MA5'])
        ma20_today = float(latest['MA20'])
        if close_price >= ma20_today:
            score += 10
            tags.append("🛡️站上月線")
        elif close_price > ma5_today:
            score += 5
            tags.append("⚡站上5日線")

        # 3. 量能加分
        is_volume_breakout = (today_volume >= vol_ma5 * 1.3)
        if is_volume_breakout:
            score += 10
            tags.append("🚀帶量攻擊")

        # 4. 抓取籌碼面加分 (外資/投信/法人)
        time.sleep(0.5)
        chip_start = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        chip_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={chip_start}&token={FINMIND_TOKEN}"
        
        today_total = 0
        res_c = http.get(chip_url, timeout=6.0)
        if res_c.status_code == 200 and res_c.json().get("data"):
            df_c = pd.DataFrame(res_c.json()["data"])
            if not df_c.empty:
                df_c['net_buy'] = (df_c['buy'] - df_c['sell']) / 1000
                daily_total = df_c.groupby('date')['net_buy'].sum().reset_index(name='total_net')
                foreign_df = df_c[df_c['name'].str.contains('Foreign|外資', case=False)].groupby('date')['net_buy'].sum().reset_index(name='foreign_net')
                trust_df = df_c[df_c['name'].str.contains('Trust|投信', case=False)].groupby('date')['net_buy'].sum().reset_index(name='trust_net')
                
                daily_chip = daily_total.merge(foreign_df, on='date', how='left').merge(trust_df, on='date', how='left').fillna(0).sort_values('date')
                
                if len(daily_chip) >= 1:
                    today_total = float(daily_chip.iloc[-1]['total_net'])
                    today_foreign = float(daily_chip.iloc[-1]['foreign_net'])
                    today_trust = float(daily_chip.iloc[-1]['trust_net'])

                    if today_total >= 10000:
                        score += 20
                        tags.append(f"⚡萬張爆買({round(today_total)}張)")
                    elif today_total >= 3000:
                        score += 15
                        tags.append(f"🔥法人大買({round(today_total)}張)")
                    elif today_total >= 1000:
                        score += 10
                        tags.append(f"🔄法人買超({round(today_total)}張)")

                    if today_foreign > 0 and today_trust > 0:
                        score += 10
                        tags.append("🤝土洋同買")

        # =========================================================================
        # 🔍 【洗盤後起漲點 獨立判定（加分/標籤）】
        # 判定條件：短線洗盤（前1~2天柱狀圖微幅回檔或小黑K），今日轉折發動漲幅 > 1% 且有量
        # =========================================================================
        is_wash_breakout = False
        if (osc_p1 < osc_p2 or float(prev1['Close']) < float(prev2['Close'])) and (pct_change >= 1.0) and is_volume_breakout:
            is_wash_breakout = True
            score += 15
            tags.append("⚡洗盤結束起漲")

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
        print(f"  └─ ⚠️ [{stock_id} {stock_name}] 異常: {e}", flush=True)
        
    return None

# -----------------------------------------------------------------------------
# 4. 主排程與輸出 (Top 5 + Top 5 控管)
# -----------------------------------------------------------------------------
def run_precalculation():
    print("==================================================", flush=True)
    print("🚀 [Cron Job] 開始執行 AI 排程選股...", flush=True)
    
    if not FINMIND_TOKEN:
        print("❌ [Fatal Error] FINMIND_TOKEN 未設定！", flush=True)
        return

    # 前 200 大成交量
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
            print(f"✅ 取得 Top 200 熱門股，開始進行 MACD 轉折與籌碼掃描...", flush=True)
    except Exception as e:
        print(f"❌ 證交所 API 抓取失敗: {e}", flush=True)
        return

    all_passed_stocks = []
    wash_breakout_stocks = []

    for idx, stock_info in enumerate(candidates, 1):
        res = fetch_finmind_data(stock_info)
        if res:
            all_passed_stocks.append(res)
            if res['is_wash_breakout']:
                wash_breakout_stocks.append(res)
            print(f"  └─ 🎯 [選中] {res['code']} {res['name']} 得分: {res['score']}", flush=True)
            
        time.sleep(0.3)

    # 排序
    all_passed_stocks.sort(key=lambda x: x['score'], reverse=True)
    wash_breakout_stocks.sort(key=lambda x: x['score'], reverse=True)

    # 取前 5 名
    top_bottom_turn = all_passed_stocks[:5]
    top_wash_breakout = wash_breakout_stocks[:5]

    date_display = datetime.datetime.now().strftime('%Y/%m/%d')
    today_str = datetime.datetime.now().strftime('%Y%m%d')

    # 組合訊息
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
    
    print("🎉 排程選股與 LINE 推播發送完畢！", flush=True)

if __name__ == "__main__":
    run_precalculation()
