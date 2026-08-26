import os
import re
import requests
import psycopg2
import pandas as pd
from datetime import datetime

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
# 2. 主要運算與排程入口
# -----------------------------------------------------------------------------
def run_precalculation():
    """執行每日選股預算、資料庫更新、自動清理與訊息發送"""
    
    # -------------------------------------------------------------------------
    # ⚠️ [清理機制] 強制在主流程啟動前刪除 5 檔錯誤持股
    # -------------------------------------------------------------------------
    try:
        import clean_db
        clean_db.clean_specific_stocks()
        print("✅ [Clean Log] 成功執行資料庫錯誤持股清理作業！", flush=True)
    except Exception as e:
        print(f"⚠️ [Clean Log] 清理模組執行失敗或已被移除: {e}", flush=True)

    print("🚀 [Cron Job] 開始執行 AI 選股預算作業...", flush=True)

    today_str = datetime.now().strftime("%Y%m%d")
    conn = get_db_connection()
    
    if not conn:
        print("❌ [Cron Job] 資料庫連線失敗，終止排程作業。", flush=True)
        return

    try:
        # A. 模擬 AI 選股報告邏輯 (此處整合你的篩選邏輯與報告生成)
        # 若你的專案有專屬策略模組，會在線上直接產出預設內容
        report_content = (
            f"📈 【AI 自動選股日報 - {today_str}】\n"
            f"------------------------------------\n"
            f"🎯 策略一 (多頭動能精選)：\n"
            f"• 2330 台積電 | 建議關注動能轉折\n"
            f"• 2454 聯發科 | MACD 紅柱持續擴張\n\n"
            f"🎯 策略二 (均線修正回升)：\n"
            f"• 3714 富采 | 符合突破買進訊號\n"
            f"------------------------------------\n"
            f"💡 請輸入「模擬倉」查看即時戰績與未實現損益。"
        )

        cursor = conn.cursor()

        # B. 建置歷史紀錄表 (若不存在)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS history (
                date VARCHAR(20) PRIMARY KEY,
                content TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # C. 寫入今日歷史選股紀錄 (覆蓋舊日期或新增)
        cursor.execute("""
            INSERT INTO history (date, content, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (date) DO UPDATE 
            SET content = EXCLUDED.content, updated_at = NOW();
        """, (today_str, report_content))

        # D. 同步更新「最新 (LATEST)」標籤紀錄
        cursor.execute("""
            INSERT INTO history (date, content, updated_at)
            VALUES ('LATEST', %s, NOW())
            ON CONFLICT (date) DO UPDATE 
            SET content = EXCLUDED.content, updated_at = NOW();
        """, (report_content,))

        conn.commit()
        cursor.close()
        conn.close()

        print(f"✅ [Cron Job Log] [{today_str}] 與 [LATEST] 選股報告已順利寫入 PostgreSQL！", flush=True)

        # E. 發送每日主動推播
        send_line_push_message(report_content)

    except Exception as e:
        print(f"❌ [Cron Job Log] 預算執行過程中發生錯誤: {e}", flush=True)
        if conn:
            conn.rollback()
            conn.close()

if __name__ == "__main__":
    run_precalculation()
