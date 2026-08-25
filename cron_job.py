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
    """分析單檔股票，分類為『底部轉折』或『洗盤突破』"""
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
        df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
        df = df.dropna(subset=['Close', 'Volume'])
        if len(df) < 35: return None

        # 計算 MACD 技術指標
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['OSC'] = df['DIF'] - df['MACD']

        # 計算布林通道 (20日 MA ± 2倍標準差)
        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['STD20'] = df['Close'].rolling(window=20).std()
        df['Boll_Upper'] = df['MA20'] + (df['STD20'] * 2)
        df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()

        # 抓取技術指標最新數據
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

        # 🛡️ 條件過濾：漲幅過高 (> 6%) 避免追高
        if pct_change > 6.0: return None

        # 🛡️ 防追高機制 1：價頂布林上軌且無量衝高 (收盤 > 上軌*0.985 且 當日量 < 5日均量)
        boll_upper = float(latest['Boll_Upper'])
        vol_today = float(latest['Volume'])
        vol_ma5 = float(latest['Vol_MA5'])
        
        if (close_price >= boll_upper * 0.985) and (vol_today < vol_ma5):
            return None  # 高檔無量過頂，極易拉回，直接排除

        # MACD 型態定義
        is_green_shrinking = (osc_today < 0) and (osc_today > osc_p1) # 📉 綠柱縮短（止跌）
        is_first_red = (osc_today > 0) and (osc_p1 <= 0)              # 💥 綠轉紅第1天
        is_macd_expanding = (osc_today > 0) and (osc_today > osc_p1) # 🔥 紅柱擴大

        # 洗盤型態判定
        is_red_shrinking_2days = (osc_p1 > 0) and (osc_p2 > osc_p1)
        is_red_shrinking_3days = (osc_p1 > 0) and (osc_p3 > osc_p2 > osc_p1)

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
                    # 🛡️ 日期安全校驗：確認 API 最後一筆數據包含最新價格交易日
                    last_chip_date = str(daily_summary.iloc[-1]['date'])
                    last_price_date = str(latest['date'])
                    
                    if last_chip_date != last_price_date:
                        return None # API 籌碼未更新完畢，跳過防舊資料誤判

                    today_foreign = float(daily_summary.iloc[-1]['net_buy'])
                    prev_foreign = float(daily_summary.iloc[-2]['net_buy'])
                    
                    # 🛡️ 防追高機制 2：外資連買後力道明顯衰退 (昨日買超 > 3000張，今日買超減少 > 15%)
                    if (prev_foreign >= 3000) and (today_foreign < prev_foreign * 0.85):
                        return None # 外資高檔買盤力道收縮，排除

                    # 🎯 條件 1：籌碼強勢吞噬 (昨日大賣 <= -2000張，今日買超補回 80% 以上)
                    is_heavy_sell_yesterday = (prev_foreign <= -2000)
                    is_strong_rebound_cover = is_heavy_sell_yesterday and (today_foreign >= abs(prev_foreign) * 0.8)

                    # 🎯 條件 2：標準翻多買超 (昨日小賣或微買，今日買超 >= 2000 張)
                    is_normal_turn_buy = (prev_foreign > -2000) and (today_foreign >= 2000)

                    # 🎯 條件 3：外資暴買突破 (買超為昨日 3 倍以上且 >= 1000 張)
                    is_foreign_3x_surge = (prev_foreign > 0) and (today_foreign >= prev_foreign * 3) and (today_foreign >= 1000)

                    score = 0
                    strategy_type = None
                    tags = []

                    # 🎯 【策略 A：底部轉折起漲區】 (空轉多拐點)
                    if (is_green_shrinking or is_first_red):
                        if is_strong_rebound_cover:
                            strategy_type = "BOTTOM_TURN"
                            score = 90
                            tags.append("📉綠柱止跌" if is_green_shrinking else "💥綠轉紅第1天")
                            tags.append(f"⚡籌碼強勢吞噬({round(today_foreign)}張)")
                        elif is_normal_turn_buy:
                            strategy_type = "BOTTOM_TURN"
                            score = 80
                            tags.append("📉綠柱止跌" if is_green_shrinking else "💥綠轉紅第1天")
                            tags.append(f"🔄外資買超{round(today_foreign)}張")

                    # 🎯 【策略 B：洗盤突破爆發區】 (即戰力飆股)
                    elif is_macd_expanding and (is_strong_rebound_cover or is_normal_turn_buy or is_foreign_3x_surge):
                        strategy_type = "WASH_BREAKOUT"
                        score = 70
                        if is_red_shrinking_3days:
                            score += 20
                            tags.append("⚡3日洗盤突破")
                        elif is_red_shrinking_2days:
                            score += 15
                            tags.append("⚡2日洗盤突破")
                        tags.append(f"🔥外資暴買{round(today_foreign)}張")

                    # 位階加分 (1% ~ 4%)
                    if strategy_type and (1.0 <= pct_change <= 4.0):
                        score += 10
                        tags.append("🛡️黃金位階")

                    if strategy_type:
                        return {
                            "code": stock_id, "name": stock_name, "close": close_price,
                            "pct": pct_change, "foreign_shares": round(today_foreign),
                            "score": score, "type": strategy_type,
                            "status_label": " ".join(tags)
                        }
    except Exception as e:
        print(f"  └─ ⚠️ [{stock_id} {stock_name}] 分析異常: {e}", flush=True)
    return None

def run_precalculation():
    """主執行函式：抓取 Top 200 個股進行篩選與分區推播"""
    print("==================================================", flush=True)
    print("🚀 [Cron Job] 開始執行 AI 排程選股與自動推播...", flush=True)
    
    if not FINMIND_TOKEN:
        print("❌ [Token Error] 未檢測到 FINMIND_TOKEN 環境變數！程式中止！", flush=True)
        print("==================================================", flush=True)
        return

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
            print(f"✅ [TWSE Log] 成功取得 Top 200 熱門個股！開始分析...", flush=True)
    except Exception as e:
        print(f"❌ [TWSE Log] 證交所 API 抓取失敗: {e}", flush=True)
        return

    bottom_turn_stocks = []
    wash_breakout_stocks = []
    total_count = len(candidates)

    for idx, stock_info in enumerate(candidates, 1):
        print(f"📊 [{idx}/{total_count}] 分析中: {stock_info['code']} {stock_info['name']}", flush=True)
        res = fetch_finmind_data(stock_info)
        if res:
            if res['type'] == 'BOTTOM_TURN':
                bottom_turn_stocks.append(res)
            elif res['type'] == 'WASH_BREAKOUT':
                wash_breakout_stocks.append(res)
            print(f"  └─ 🎯 [選中標的] [{res['code']} {res['name']}] 類型:{res['type']} 得分:{res['score']}", flush=True)
        time.sleep(0.5)

    # 依得分高低進行排序
    bottom_turn_stocks.sort(key=lambda x: x['score'], reverse=True)
    wash_breakout_stocks.sort(key=lambda x: x['score'], reverse=True)

    today_str = datetime.datetime.now().strftime('%Y%m%d')
    date_display = datetime.datetime.now().strftime('%Y/%m/%d')

    lines = [f"📊 【AI 精選雙策略雙軌選股報告】({date_display})", "===================="]

    # 1. 🌱 底部轉折起漲區
    lines.append("🌱 【策略一：底部止跌 + 外資籌碼吞噬翻多】")
    lines.append("💡 特性：空轉多拐點，低基期、獲利空間極大")
    lines.append("--------------------")
    if not bottom_turn_stocks:
        lines.append("今日暫無符合條件之標的。")
    else:
        for idx, item in enumerate(bottom_turn_stocks):
            lines.append(
                f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"   👉 得分:{item['score']}分 | {item['status_label']}"
            )
            # 💡 只有非最後一檔時才插入卡片分隔線
            if idx < len(bottom_turn_stocks) - 1:
                lines.append("┈┈┈┈┈┈┈┈┈┈")

    lines.append("\n====================\n")

    # 2. 🔥 洗盤突破爆發區
    lines.append("🔥 【策略二：洗盤結束 + 外資暴買突破】")
    lines.append("💡 特性：主力洗盤完成，短線發動拉升即戰力")
    lines.append("--------------------")
    if not wash_breakout_stocks:
        lines.append("今日暫無符合條件之標的。")
    else:
        for idx, item in enumerate(wash_breakout_stocks):
            lines.append(
                f"🔹 {item['code']} {item['name']} | 收: {item['close']:.2f} ({item['pct']:+.2f}%)\n"
                f"   👉 得分:{item['score']}分 | {item['status_label']}"
            )
            # 💡 只有非最後一檔時才插入卡片分隔線
            if idx < len(wash_breakout_stocks) - 1:
                lines.append("┈┈┈┈┈┈┈┈┈┈")

    report = "\n".join(lines)

    # 儲存與發送推播
    save_to_db(report, "LATEST")
    save_to_db(report, today_str)
    send_line_push(report)

    print("🎉 [Cron Job Log] 排程選股與 LINE 推播發送完畢！", flush=True)

if __name__ == "__main__":
    run_precalculation()
