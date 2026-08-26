import os
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

def clean_specific_stocks():
    if not DATABASE_URL:
        print("⚠️ [Clean DB] 無 DATABASE_URL，跳過清理。")
        return

    # 要強制刪除的 5 檔錯誤股票代號
    target_codes = ('2303', '2615', '2301', '3231', '5871')

    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"

        conn = psycopg2.connect(url, connect_timeout=10)
        cursor = conn.cursor()

        # 強制直接依照代號刪除 sim_trades 中的紀錄
        query = "DELETE FROM sim_trades WHERE stock_code IN %s;"
        cursor.execute(query, (target_codes,))
        deleted_count = cursor.rowcount

        conn.commit()
        cursor.close()
        conn.close()

        print(f"🧹 [Clean DB] 清理完成！共刪除了 {deleted_count} 筆特定股票紀錄。")

    except Exception as e:
        print(f"❌ [Clean DB] 執行刪除時發生錯誤: {e}")

if __name__ == "__main__":
    clean_specific_stocks()
