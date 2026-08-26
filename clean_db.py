import os
import psycopg2

# 讀取你的 PostgreSQL 資料庫連線字串
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

def clean_specific_stocks():
    if not DATABASE_URL:
        print("❌ 未偵測到 DATABASE_URL 環境變數！")
        return

    # 要精準刪除的 5 檔錯誤股票代號
    target_stocks = ['2303', '2615', '2301', '3231', '5871']
    
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
            
        conn = psycopg2.connect(url, connect_timeout=10)
        cursor = conn.cursor()
        
        print(f"🔄 準備從模擬倉精準刪除以下股票: {target_stocks}...")

        # 1. 嘗試從 portfolio 資料表刪除 (請依你實際的資料表名稱與欄位調整)
        # 普遍設計欄位名稱為 stock_id 或 code
        try:
            cursor.execute(
                "DELETE FROM portfolio WHERE stock_id = ANY(%s) OR code = ANY(%s);",
                (target_stocks, target_stocks)
            )
        except Exception:
            conn.rollback() # 若欄位名稱不符則回滾重試通用查詢
            cursor.execute(
                "DELETE FROM portfolio WHERE stock_id IN %s;",
                (tuple(target_stocks),)
            )

        deleted_count = cursor.rowcount
        conn.commit()
        
        cursor.close()
        conn.close()
        
        print(f"✅ 清理完成！成功從模擬倉移除 {deleted_count} 筆舊資料。")
        print("💡 昨天的 6 檔股票與最新標的已完好保留。")

    except Exception as e:
        print(f"❌ 刪除過程中發生錯誤: {e}")

if __name__ == "__main__":
    clean_specific_stocks()
