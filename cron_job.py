import os
import re
import time
import requests
import psycopg2
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# -----------------------------------------------------------------------------
# 1. 讀取環境變數與設定
# -----------------------------------------------------------------------------
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
FINMIND_TOKEN = os.environ.get('FINMIND_API_TOKEN', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '').strip()
LINE_USER_ID = os.environ.get('LINE_USER_ID', '').strip()

def get_db_connection():
    """建立 PostgreSQL 資料庫連線"""
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception as e:
        print(f"❌ [DB Log] 連線失敗: {e}", flush=True)
        return None

def send_line_push_message(text):
    """發送 LINE 推播訊息給指定用戶"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("⚠️ [LINE Push Log] 未設定 LINE Token 或 User ID，跳過主動推播。", flush=True)
        return

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}]
    }

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        if res.status_code == 200:
            print("✅ [LINE Log] LINE 推播成功！", flush=True)
        else:
            print(f"❌ [LINE Log] 推播失敗 ({res.status_code}): {res.text}", flush=True)
    except Exception as e:
        print(f"❌ [LINE Log] 推播異常: {e}", flush=True)

# -----------------------------------------------------------------------------
# 2. 爬取 Top 200 熱門與成交量標的
# -----------------------------------------------------------------------------
def get_top_200_stocks():
    """取得台股成交量與熱門前 200 檔股票代號與名稱"""
    stock_dict = {}
    try:
        # 上市股票成交量排行
        url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res_twse = requests.get(url_twse, timeout=8)
        if res_twse.status_code == 200:
            data = res_twse.json()
            sorted_data = sorted(data, key=lambda x: int(x.get("TradeVolume", 0) or 0), reverse=True)
            for item in sorted_data[:150]:
                code = item.get("Code", "").strip()
                name = item.get("Name", "").strip()
                if len(code) == 4 and code.isdigit():
                    stock_dict[code] = name

        # 上櫃股票成交量排行
        url_tpex = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_dailyclose_quotes"
        res_tpex = requests.get(url_tpex, timeout=8)
        if res_tpex.status_code == 200:
            data_tpex = res_tpex.json()
            sorted_tpex = sorted(data_tpex, key=lambda x: int(x.get("TradeVol", 0) or 0), reverse=True)
            for item in sorted_tpex[:100]:
                code = item.get("SecuritiesCompanyCode", "").strip()
                name = item.get("CompanyName", "").strip()
                if len(code) == 4 and code.isdigit():
                    stock_dict[code] = name
    except Exception as e:
        print(f"⚠️ [Stock List Log] 獲取 200 檔清單時發生異常: {e}", flush=True)

    # 備用保底個股清單
    default_stocks = {
        "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2308": "台達電",
        "2382": "廣達", "3231": "緯創", "2356": "英業達", "6669": "緯穎",
        "2303": "聯電", "2603": "長榮", "2609": "陽明", "2615": "萬海"
    }
    for k, v in default_stocks.items():
        if k not in stock_dict:
            stock_dict[k] = v

    print(f"📋 [Stock List Log] 成功鎖定 {len(stock_dict)} 檔精選標的進行指標演算", flush=True)
    return stock_dict

# -----------------------------------------------------------------------------
# 3. K 線數據擷取與 MACD/均線計分演算法
# -----------------------------------------------------------------------------
def analyze_stock_indicators(stock_id, stock_name):
    """計算單檔股票 Technical Indicators 與策略加分數值"""
    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if FINMIND_TOKEN else {}
    start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date={start_date}"

    try:
        res = requests.get(url, headers=headers, timeout=6)
        if res.status_code != 200 or not res.json().get("data"):
            return None
            
        data = res.json()["data"]
        if len(data) < 35:
            return None

        df = pd.DataFrame(data).rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
        df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
        df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
        df = df.dropna(subset=['Close'])

        # 指標計算: MACD (12, 26, 9)
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['OSC'] = df['DIF'] - df['MACD']

        # 指標計算: 均線
        df['MA5'] = df['Close'].rolling(5).mean()
        df['MA10'] = df['Close'].rolling(10).mean()
        df['MA20'] = df['Close'].rolling(20).mean()
        df['Vol_MA5'] = df['Volume'].rolling(5).mean()

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        osc_today = float(latest['OSC'])
        osc_prev = float(prev['OSC'])
        close = float(latest['Close'])
        prev_close = float(prev['Close'])
        pct_change = ((close - prev_close) / prev_close) * 100

        # ---- 🎯 計分機制 (Scoring System) ----
        score_s1 = 0.0  # 策略一：多頭動能擴張
        score_s2 = 0.0  # 策略二：綠柱縮短轉折/均線支撐

        # 策略一加分邏輯
        if osc_today > 0 and osc_today > osc_prev:
            score_s1 += 40  # 紅柱擴張強勢
        if latest['Close'] > latest['MA5'] > latest['MA10'] > latest['MA20']:
            score_s1 += 30  # 多頭排列
        if latest['Volume'] > latest['Vol_MA5'] * 1.2:
            score_s1 += 20  # 成交量放大
        if pct_change > 1.5:
            score_s1 += 10  # 當日漲幅佳

        # 策略二加分邏輯
        if osc_today < 0 and osc_today > osc_prev:
            score_s2 += 50  # 綠柱明顯縮短 (空方衰退)
        if osc_today > 0 and osc_prev <= 0:
            score_s2 += 40  # 紅柱第一天轉折
        if latest['Close'] >= latest['MA20']:
            score_s2 += 20  # 站上月線關卡
        if pct_change > 0:
            score_s2 += 10  # 止跌收紅

        status_desc = "🔥 動能強勁" if score_s1 >= 70 else ("📉 空方衰退/轉折" if score_s2 >= 60 else "⚖️ 觀望整理")

        return {
            "code": stock_id,
            "name": stock_name,
            "close": close,
            "pct": pct_change,
            "score_s1": score_s1,
            "score_s2": score_s2,
            "status": status_desc
        }

    except Exception as e:
        return None

# -----------------------------------------------------------------------------
# 4. 主要排程預算與 LOG 檢查流程
# -----------------------------------------------------------------------------
def run_precalculation():
    """跑完 200 檔個股預算、印出 Log 檢查，並寫入 PostgreSQL & 推播"""
    print("🚀 [Cron Job Log] 開始執行 AI 選股預估與指標分析...", flush=True)

    stocks = get_top_200_stocks()
    results = []

    success_count = 0
    fail_count = 0

    # 巡迴計算 200 檔個股與 LOG 紀錄
    for idx, (code, name) in enumerate(stocks.items(), 1):
        res = analyze_stock_indicators(code, name)
        if res:
            results.append(res)
            success_count += 1
        else:
            fail_count += 1
            print(f"❌ [{idx}/{len(stocks)}] [{code} {name}] K線讀取失敗 (HTTP Timeout)", flush=True)

    print(f"📊 [LOG 檢查] 預估算完畢！成功: {success_count} 檔 | 失敗: {fail_count} 檔", flush=True)

    if not results:
        print("⚠️ [Cron Job Log] 無法抓取任何有效的選股結果，終止寫入與推播。", flush=True)
        return

    # 排序篩選Top標的
    df_res = pd.DataFrame(results)
    top_s1 = df_res.sort_values(by="score_s1", ascending=False).head(5)
    top_s2 = df_res.sort_values(by="score_s2", ascending=False).head(5)

    today_str = datetime.now().strftime("%Y%m%d")

    # 組合日報內文
    report_lines = [
        f"📈 【AI 自動選股日報 - {today_str}】",
        "------------------------------------",
        "🎯 策略一 (多頭動能熱門精選)："
    ]
    for _, row in top_s1.iterrows():
        report_lines.append(f"• {row['code']} {row['name']} | 現價: ${row['close']:.2f} ({row['pct']:+.2f}%) | 評分: {row['score_s1']:.0f}分")

    report_lines.append("\n🎯 策略二 (綠柱縮短/低檔轉折精選)：")
    for _, row in top_s2.iterrows():
        report_lines.append(f"• {row['code']} {row['name']} | 現價: ${row['close']:.2f} ({row['pct']:+.2f}%) | 評分: {row['score_s2']:.0f}分")

    report_lines.append("------------------------------------")
    report_lines.append("💡 輸入「模擬倉」即可查看即時投資組合與戰績報表！")

    final_report = "\n".join(report_lines)

    # 寫入 PostgreSQL 資料庫
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    date VARCHAR(20) PRIMARY KEY,
                    content TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cursor.execute("""
                INSERT INTO history (date, content, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (date) DO UPDATE 
                SET content = EXCLUDED.content, updated_at = NOW();
            """, (today_str, final_report))

            cursor.execute("""
                INSERT INTO history (date, content, updated_at)
                VALUES ('LATEST', %s, NOW())
                ON CONFLICT (date) DO UPDATE 
                SET content = EXCLUDED.content, updated_at = NOW();
            """, (final_report,))

            conn.commit()
            cursor.close()
            conn.close()
            print(f"✅ [Cron Job Log] [{today_str}] 與 [LATEST] 日報已寫入 PostgreSQL！", flush=True)

        except Exception as e:
            print(f"❌ [DB Log] 寫入資料庫時失敗: {e}", flush=True)

    # 主動發送 LINE 推播
    send_line_push_message(final_report)
    print("🎉 [Cron Job Log] 排程與模擬倉運算完畢！", flush=True)

if __name__ == "__main__":
    run_precalculation()
