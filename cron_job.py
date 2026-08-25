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
# 1. 環境變數與設定
# -----------------------------------------------------------------------------
FINMIND_TOKEN = (
    os.environ.get('FINMIND_TOKEN', '').strip() or 
    os.environ.get('FINMIND_API_TOKEN', '').strip()
)

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()

# -----------------------------------------------------------------------------
# 2. 建立連線 Session 與 API 重試機制
# -----------------------------------------------------------------------------
def create_robust_session():
    session = requests.Session()
    retries = Retry(
        total=3, 
        backoff_factor=1.0, 
        status_forcelist=[429, 500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http = create_robust_session()

# -----------------------------------------------------------------------------
# 3. 資料庫連線與歷史寫入
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# 4. LINE 廣播推播
# -----------------------------------------------------------------------------
def send_line_push(report_text):
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("⚠️ [LINE Log] 未設定 LINE Channel Access Token，略過發送", flush=True)
        return
    try:
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        line_bot_api.broadcast(TextSendMessage(text=report_text))
        print("✅ [LINE Log] 廣播推播成功發送！", flush=True)
    except Exception as e:
        print(f"❌ [LINE Log] LINE 推播發送失敗: {e}", flush=True)

# -----------------------------------------------------------------------------
# 5. 個股 K 線與籌碼分析核心 Logic
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
        if len(df) < 60:
            return None

        # --- 計算 MACD 技術指標 ---
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['OSC'] = df['DIF'] - df['MACD']

        # --- 計算均線與布林通道 ---
        df['MA5'] = df['Close'].rolling(window=5).mean()
        df['MA10'] = df['Close'].rolling(window=10).mean()
        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA60'] = df['Close'].rolling(window=60).mean()
        df['STD20'] = df['Close'].rolling(window=20).std()
        df['Boll_Upper'] = df['MA20'] + (df['STD20'] * 2)
        df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()
        df['Vol_MA20'] = df['Volume'].rolling(window=20).mean()

        latest = df.iloc[-1]
        prev1 = df.iloc[-2]
        prev2 = df.iloc[-3]
        prev3 = df.iloc[-4]

        osc_today = float(latest['OSC'])
        osc_p1 = float(prev1['OSC'])
        osc_p2 = float(prev2['OSC'])
        osc_p3 = float(prev3['OSC'])

        close_price = float(latest['Close'])
        open_price = float(latest['Open'])
        high_price = float(latest['High'])
        prev_close = float(prev1['Close'])
        today_volume = float(latest['Volume'])
        vol_ma5 = float(latest['Vol_MA5'])
        vol_ma20 = float(latest['Vol_MA20'])
        pct_change = ((close_price - prev_close) / prev_close) * 100

        # --- 技術面嚴格過濾條款 ---
        # 1. 🛡️ 允許良性洗盤，但大跌（<-2.0%）或長黑K（實體跌幅>2.0%）過濾
        body_pct = ((close_price - open_price) / open_price) * 100
        if pct_change < -2.0 or body_pct < -2.0:
            return None

        # 2. 漲幅過高防追高
        if pct_change > 7.0:
            return None
            
        # 3. 上影線避雷針過濾
        upper_shadow = high_price - max(open_price, close_price)
        body_length = max(abs(close_price - open_price), 0.01)
        if (upper_shadow / body_length) > 1.8 and upper_shadow > (close_price * 0.02):
            return None
        
        # 4. 🛡️ 防自由落體：近 3 天不能創近 60 天新低
        min_60d = df['Low'].tail(60).min()
        if float(latest['Low']) <= min_60d or float(prev1['Low']) <= min_60d:
            return None

        # 5. 🛡️ 動態均線適應：未站上月線者，必須站上 5 日線且 5 日線向上（V 轉機制）
        ma5_today = float(latest['MA5'])
        ma20_today = float(latest['MA20'])
        is_above_ma20 = (close_price >= ma20_today)
        is_v_turn_rebound = (close_price > ma5_today) and (ma5_today >= float(prev1['MA5']))
        
        if not is_above_ma20 and not is_v_turn_rebound:
            return None

        # 6. 布林通道過濾
        boll_upper = float(latest['Boll_Upper'])
        is_volume_breakout = (today_volume >= vol_ma5 * 1.2) or (today_volume >= vol_ma20 * 1.1)
        if (close_price > boll_upper * 1.005) and not is_volume_breakout:
            return None

        # --- MACD 轉折狀態判定 ---
        is_green_shrinking = (osc_today < -0.001) and (osc_today > osc_p1)
        is_first_red = (osc_today > 0.001) and (osc_p1 <= 0.001)
        is_macd_expanding = (osc_today > 0.001) and (osc_today > osc_p1)
        
        is_red_shrinking_2days = (osc_p1 > 0.001) and (osc_p2 > osc_p1)
        is_red_shrinking_3days = (osc_p1 > 0.001) and (osc_p3 > osc_p2 > osc_p1)

        # --- 抓取籌碼面資料 ---
        time.sleep(0.8)
        chip_start = (datetime.datetime.now() - datetime.timedelta(days=12)).strftime("%Y-%m-%d")
        chip_url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date={chip_start}&token={FINMIND_TOKEN}"
        
        res_c = http.get(chip_url, timeout=8.0)
        if res_c.status_code == 200 and res_c.json().get("data"):
            df_c = pd.DataFrame(res_c.json()["data"])
            if not df_c.empty:
                df_c['net_buy'] = (df_c['buy'] - df_c['sell']) / 1000
                
                daily_total = df_c.groupby('date')['net_buy'].sum().reset_index(name='total_net')
                foreign_df = df_c[df_c['name'].str.contains('Foreign|外資', case=False)].groupby('date')['net_buy'].sum().reset_index(name='foreign_net')
                trust_df = df_c[df_c['name'].str.contains('Trust|投信', case=False)].groupby('date')['net_buy'].sum().reset_index(name='trust_net')
                
                daily_chip = daily_total.merge(foreign_df, on='date', how='left').merge(trust_df, on='date', how='left').fillna(0).sort_values('date')
                
                if len(daily_chip) >= 2:
                    if str(daily_chip.iloc[-1]['date']) != str(latest['date']):
                        return None

                    today_total = float(daily_chip.iloc[-1]['total_net'])
                    prev_total = float(daily_chip.iloc[-2]['total_net'])
                    today_foreign = float(daily_chip.iloc[-1]['foreign_net'])
                    today_trust = float(daily_chip.iloc[-1]['trust_net'])

                    # 投信大拋售保護
                    if today_trust <= -1000:
                        return None

                    # 🛡️ 籌碼急凍過濾：前天暴買今日縮水 60% 且收黑者
                    if prev_total > 2000 and today_total < (prev_total * 0.4) and pct_change < 0:
                        return None

                    is_chip_buy = (today_total >= 1000)

                    # 籌碼集中度計算
                    total_vol_shares = (today_volume / 1000) if today_volume > 0 else 1
                    chip_ratio = (today_total / total_vol_shares) if total_vol_shares > 0 else 0
                    is_day_trading_risk = (chip_ratio >= 0.40)

                    score = 0
                    strategy_type = None
                    tags = []

                    # ---------------------------------------------------------
                    # 【策略一：底部止跌】
                    # ---------------------------------------------------------
                    if (is_green_shrinking or is_first_red) and is_chip_buy:
                        strategy_type = "BOTTOM_TURN"
                        score = 70
                        
                        if is_first_red:
                            tags.append("💥綠轉紅第1天")
                        else:
                            tags.append("📉綠柱止跌")
                        
                        if not is_above_ma20 and is_v_turn_rebound:
                            tags.append("⚡崩盤V轉站上5日線")
                            score += 5

                        if is_day_trading_risk:
                            score += 10
                            tags.append(f"🔄法人合買({round(today_total)}張)")
                            tags.append("⚠️籌碼過度集中")
                        elif today_total >= 10000:
                            score += 20
                            tags.append(f"⚡萬張爆買({round(today_total)}張)")
                        elif today_total >= 5000:
                            score += 15
                            tags.append(f"🔥法人大買({round(today_total)}張)")
                        else:
                            score += 10
                            tags.append(f"🔄法人合買({round(today_total)}張)")

                    # ---------------------------------------------------------
                    # 【策略二：洗盤突破】
                    # ---------------------------------------------------------
                    elif is_macd_expanding and is_chip_buy:
                        if today_total < 2000:
                            return None

                        strategy_type = "WASH_BREAKOUT"
                        score = 65
                        
                        if is_red_shrinking_3days:
                            score += 15
                            tags.append("⚡3日洗盤突破")
                        elif is_red_shrinking_2days:
                            score += 10
                            tags.append("⚡2日洗盤突破")
                        
                        if is_day_trading_risk:
                            score += 10
                            tags.append(f"🔥法人買超({round(today_total)}張)")
                            tags.append("⚠️籌碼過度集中")
                        elif today_total >= 10000:
                            score += 20
                            tags.append(f"⚡萬張爆買({round(today_total)}張)")
                        else:
                            score += 10
                            tags.append(f"🔥法人買超({round(today_total)}張)")

                    # ---------------------------------------------------------
                    # 附加指標加分項
                    # ---------------------------------------------------------
                    if strategy_type:
                        if is_volume_breakout and (close_price >= boll_upper * 0.985):
                            score += 10
                            tags.append("🚀帶量強勢突破")
                            
                        if today_foreign > 0 and today_trust > 0:
                            score += 10
                            tags.append("🤝土洋同買")
                            
                        if 0.5 <= pct_change <= 3.5:
                            score += 10
                            tags.append("🛡️黃金位階")
                        elif pct_change < 0:
                            tags.append("🔍拉回洗盤")

                        return {
                            "code": stock_id,
                            "name": stock_name,
                            "close": close_price,
                            "pct": pct_change,
                            "score": score,
                            "type": strategy_type,
                            "status_label": " ".join(tags)
                        }
    except Exception as e:
        print(f"  └─ ⚠️ [{stock_id} {stock_name}] 分析過程異常: {e}", flush=True)
        
    return None

# -----------------------------------------------------------------------------
# 6. 主排程與掃描流程
# -----------------------------------------------------------------------------
def run_precalculation():
    print("==================================================", flush=True)
    print("🚀 [Cron Job] 開始執行 AI 排程選股與自動推播...", flush=True)
    
    if not FINMIND_TOKEN:
        print("❌ [Fatal Error] FINMIND_TOKEN 未設定，無法執行抓取！", flush=True)
        return

    # 從證交所取得交易量前 200 大個股
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
            print(f"✅ [TWSE Log] 成功取得 Top 200 熱門個股清單！開始逐一精準分析...", flush=True)
    except Exception as e:
        print(f"❌ [TWSE Log] 證交所 API 抓取失敗: {e}", flush=True)
        return

    bottom_turn_stocks = []
    wash_breakout_stocks = []
    total_count = len(candidates)

    # 執行掃描邏輯
    for idx, stock_info in enumerate(candidates, 1):
        print(f"📊 [{idx}/{total_count}] 分析中: {stock_info['code']} {stock_info['name']}", flush=True)
        res = fetch_finmind_data(stock_info)
        
        if res:
            if res['type'] == 'BOTTOM_TURN':
                bottom_turn_stocks.append(res)
            elif res['type'] == 'WASH_BREAKOUT':
                wash_breakout_stocks.append(res)
            print(f"  └─ 🎯 [選中標的] [{res['code']} {res['name']}] 類型: {res['type']} 得分: {res['score']}", flush=True)
            
        time.sleep(0.5)

    # 依分數高低排序
    bottom_turn_stocks.sort(key=lambda x: x['score'], reverse=True)
    wash_breakout_stocks.sort(key=lambda x: x['score'], reverse=True)

    today_str = datetime.datetime.now().strftime('%Y%m%d')
    date_display = datetime.datetime.now().strftime('%Y/%m/%d')

    # -------------------------------------------------------------------------
    # 組合訊息（與 LINE 截圖格式 100% 吻合）
    # -------------------------------------------------------------------------
    lines = [
        f"📊 【AI 精選雙策略雙軌選股報告】({date_display})",
        "===================="
    ]

    lines.append("🌱 【策略一：底部止跌 + 法人合買翻多】")
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
            if idx < len(bottom_turn_stocks) - 1:
                lines.append("┈┈┈┈┈┈┈┈┈┈")

    lines.append("\n====================\n")

    lines.append("🔥 【策略二：洗盤結束 + 法人暴買突破】")
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
            if idx < len(wash_breakout_stocks) - 1:
                lines.append("┈┈┈┈┈┈┈┈┈┈")

    report = "\n".join(lines)

    # 儲存歷史與推播
    save_to_db(report, "LATEST")
    save_to_db(report, today_str)
    send_line_push(report)
    
    print("🎉 [Cron Job Log] 排程選股與 LINE 推播發送完畢！", flush=True)

# -----------------------------------------------------------------------------
# 7. 腳本進入點
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    run_precalculation()
