import os
import json
import datetime
import gspread

GOOGLE_CREDS_JSON = os.environ.get('GOOGLE_CREDS_JSON', '').strip()

def test_sync():
    if not GOOGLE_CREDS_JSON:
        msg = "❌ [測試失敗] 未設定 GOOGLE_CREDS_JSON 環境變數！"
        print(msg)
        return msg

    try:
        # 1. 金鑰解析
        creds_raw = GOOGLE_CREDS_JSON.replace('\\n', '\n')
        creds_dict = json.loads(creds_raw, strict=False)

        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace('\\n', '\n')

        # 2. 連線 Google Sheets
        gc = gspread.service_account_from_dict(creds_dict)
        sh = gc.open_by_key("1CrADfLGVOhfrhNB_Er-0XJCazb6onD7vjWf7QpDpO0").sheet1

        # 3. 純粹寫入固定測試資料
        test_row = [
            datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), # 結算日期
            10,       # 交易總筆數
            6,        # 勝場
            4,        # 敗場
            "60.0%",  # 勝率
            12500,    # 週淨損益
            35000,    # 累積總損益
            "3.50%",  # 平均獲利
            "-1.80%", # 平均虧損
            1.94,     # 風報比
            "+1.20%"  # 0050 同期漲跌
        ]
        sh.append_row(test_row)
        
        success_msg = "🎉 [測試成功] 已成功寫入一筆測試資料至 Google 試算表！"
        print(success_msg)
        return success_msg

    except Exception as e:
        error_msg = f"❌ [測試失敗] 錯誤原因: {e}"
        print(error_msg)
        return error_msg

if __name__ == "__main__":
    test_sync()
