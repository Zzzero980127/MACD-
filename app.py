import os
import requests
import pandas as pd
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'oG8A/4QoXPau72qWtFOcV4Hq/Ca+EgcQoJgSMHUjbNPVjtgyGkBeTwdmqfBiEjqBbZLzUn0F70JNtdTgICSrgr T+4NysH5ayUtXj4B+06J6I2DW7BT3ruJHndDuag4zjys1CO836Jwy4fR0oDq6e7wdB04t89/1O/w1cDnyilFU=')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '87cb520a332382036072d72899c94d5b')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 基礎對照字典
STOCK_NAME_MAP = {
    "台積電": "2330", "鴻海": "2317", "聯發科": "2454", "富邦金": "2881", "國泰金": "2882",
    "廣達": "2382", "緯創": "3231", "華航": "2610", "長榮航": "2618", "健策": "3653",
    "寶雅": "5904", "信驊": "5274", "鈊象": "3293", "雙鴻": "3324", "奇鋐": "3017",
    "萬潤": "6187", "台燿": "6274", "聯詠": "3034", "世芯": "3661", "創意": "3443",
    "事欣科": "4916", "雷虎": "8033", "雷科": "6207", "環球晶": "6488", "中美晶": "5483",
    "上奇": "6105", "長榮": "2603", "陽明": "2609", "萬海": "2615", "金居": "8358"
}

def load_all_taiwan_stocks():
    """啟動時自動讀取 FinMind 與 TWSE 全台股清單，載入全市場股票代碼"""
    global STOCK_NAME_MAP
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    try:
        url = "https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo"
        res = requests.get(url, headers=headers, timeout=6)
        if res.status_code == 200 and res.json().get("status") == 200:
            for item in res.json().get("data", []):
                s_id = str(item.get("stock_id", "")).strip()
                s_name = str(item.get("stock_name", "")).strip()
                if s_id and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception as e:
        print(f"FinMind Bulk Load Error: {e}")

    try:
        url_twse = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
        res = requests.get(url_twse, headers=headers, timeout=5)
        if res.status_code == 200 and isinstance(res.json(), list):
            for item in res.json():
                s_id = str(item.get("公司代號", "")).strip()
                s_name = str(item.get("公司簡稱", "")).strip()
                if s_id and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
    except Exception as e:
        print(f"TWSE Fetch Error: {e}")

load_all_taiwan_stocks()

@app.route("/", methods=['GET'])
def index():
    return 'TW Stock Bot is running alive!'

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_input = event.message.text.strip()
    clean_keyword = user_input.upper().replace(" ", "")
    
    if clean_keyword in ["AI選股", "選股", "潛力股", "AI選股推薦"]:
        reply_text = screen_undervalued_stocks()
    else:
        reply_text = analyze_stock(user_input)
        
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

def resolve_stock_symbol(user_input):
    clean_input = user_input.upper().replace(".TW", "").replace(".TWO", "").replace(" ", "").strip()
    
    if clean_input.isdigit():
        name = [k for k, v in STOCK_NAME_MAP.items() if v == clean_input]
        stock_name = name[0] if name else clean_input
        return clean_input, stock_name

    if user_input in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[user_input], user_input

    for name, code in STOCK_NAME_MAP.items():
        if user_input in name or name in user_input:
            return code, name

    return clean_input, clean_input

def get_tw_stock_data(stock_id):
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockPrice&data_id={stock_id}&start_date=2024-01-01"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=4)
        data = res.json()
        if data.get("status") == 200 and len(data.get("data", [])) >= 26:
            df = pd.DataFrame(data["data"])
            df = df.rename(columns={'close': 'Close', 'Trading_Volume': 'Volume'})
            df['Close'] = df['Close'].astype(float)
            df['Volume'] = df['Volume'].astype(float)
            return df, f"{stock_id}.TW"
    except Exception:
        pass
    return None, stock_id

def get_tw_revenue(stock_id):
    """營收計算邏輯：過濾無效與空值資料，防止 API 欄位缺失導致轉型除以零錯誤"""
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={stock_id}&start_date=2024-01-01"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=4)
        data = res.json()
        if data.get("status") == 200 and data.get("data"):
            df = pd.DataFrame(data["data"])
            
            # 清洗資料：強制轉為數值，過濾掉小於等於 0 的無效數據
            df['revenue'] = pd.to_numeric(df['revenue'], errors='coerce')
            valid_df = df[df['revenue'] > 0].copy()
            
            if len(valid_df) >= 2:
                latest = valid_df.iloc[-1]
                prev = valid_df.iloc[-2]
                
                rev_now = float(latest['revenue'])
                rev_prev = float(prev['revenue'])
                
                mom = ((rev_now - rev_prev) / rev_prev) * 100
                
                yoy = None
                if len(valid_df) >= 13:
                    last_year = valid_df.iloc[-13]
                    rev_ly = float(last_year['revenue'])
                    if rev_ly > 0:
                        yoy = ((rev_now - rev_ly) / rev_ly) * 100

                month_str = f"{latest.get('revenue_year')}/{latest.get('revenue_month')}月"
                mom_str = f"{mom:+.2f}%"
                yoy_str = f"{yoy:+.2f}%" if yoy is not None else "計算中"

                if yoy is not None and yoy > 10:
                    status = "🔥 優於預期 (年增雙位數)"
                elif yoy is not None and yoy >= 0:
                    status = "🟢 符合預期 (穩健成長)"
                elif yoy is not None:
                    status = "🟡 稍低於預期 (衰退/放緩)"
                else:
                    status = "🟢 動能持平"

                return f"{month_str} | YoY: {yoy_str} | MoM: {mom_str}\n   評價: {status}", yoy
    except Exception as e:
        print(f"Revenue Processing Error for {stock_id}: {e}")
        
    return "無即時營收數據", None

def get_tw_foreign_investor(stock_id):
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date=2024-08-01"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=4)
        data = res.json()
        if data.get("status") == 200 and data.get("data"):
            df = pd.DataFrame(data["data"])
            foreign_df = df[df['name'].str.contains('Foreign|外資|外陸資', case=False, na=False)]
            if not foreign_df.empty:
                latest_date = foreign_df.iloc[-1]['date']
                day_data = foreign_df[foreign_df['date'] == latest_date]
                net_shares = day_data['buy'].sum() - day_data['sell'].sum()
                return round(net_shares / 1000)
    except Exception:
        pass
    return None

def screen_undervalued_stocks():
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    quotes_data = {}
    try:
        url_quotes = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res_q = requests.get(url_quotes, headers=headers, timeout=5)
        if res_q.status_code == 200 and isinstance(res_q.json(), list):
            quotes_data = {item['Code']: item for item in res_q.json()}
    except Exception as e:
        print(f"Quotes Fetch Error: {e}")

    foreign_buy_map = {}
    try:
        url_t86 = "https://openapi.twse.com.tw/v1/fund/T86"
        res_f = requests.get(url_t86, headers=headers, timeout=5)
        if res_f.status_code == 200 and isinstance(res_f.json(), list):
            for item in res_f.json():
                code = item.get("Code", "").strip()
                try:
                    f_buy = int(item.get("ForeignInvestorsBuy", "0").replace(",", ""))
                    f_sell = int(item.get("ForeignInvestorsSell", "0").replace(",", ""))
                    foreign_buy_map[code] = round((f_buy - f_sell) / 1000)
                except:
                    continue
    except Exception as e:
        print(f"Foreign Buy Fetch Error: {e}")

    famous_giants = ["2330", "2317", "2454", "2382", "3231", "2603", "2609", "2615", "2881", "2882", "2886", "2002"]
    candidates = []

    for code, info in quotes_data.items():
        if not code.isdigit() or code.startswith("00") or code in famous_giants:
            continue

        try:
            close_str = str(info.get('ClosingPrice', '0')).replace(',', '')
            vol_str = str(info.get('TradeVolume', '0')).replace(',', '')
            if not close_str or close_str == '--':
                continue

            close = float(close_str)
            trade_volume = int(vol_str) / 1000 if vol_str.isdigit() else 0
            
            if 10 <= close <= 200 and trade_volume > 1000:
                foreign_net = foreign_buy_map.get(code, 0)
                if foreign_net <= 0:
                    continue

                df, _ = get_tw_stock_data(code)
                if df is None or len(df) < 60:
                    continue

                df['MA20'] = df['Close'].rolling(window=20).mean()
                df['MA60'] = df['Close'].rolling(window=60).mean()
                df['STD20'] = df['Close'].rolling(window=20).std()
                df['BB_Upper'] = df['MA20'] + (df['STD20'] * 2)

                latest_row = df.iloc[-1]
                ma20 = float(latest_row['MA20'])
                ma60 = float(latest_row['MA60'])
                bb_upper = float(latest_row['BB_Upper'])

                # 風控邏輯：跌破月線/季線，或觸及布林過熱區一律排除
                if close < ma20 or close < ma60 or close >= (bb_upper * 0.98):
                    continue

                score = 75 + min(foreign_net // 50, 20)
                name = info.get('Name', code).strip()

                item_text = (
                    f"🤫 {name} ({code}) - 安全動能評分: {int(score)}分\n"
                    f"   • 收盤價: ${close:.2f} (站穩月線 ${ma20:.1f} / 季線 ${ma60:.1f})\n"
                    f"   • 成交量: {int(trade_volume):,} 張\n"
                    f"   • 籌碼觀察: 外資卡位買超 {foreign_net:,} 張"
                )
                candidates.append((score, item_text))
        except:
            continue

    candidates.sort(key=lambda x: x[0], reverse=True)
    top_picks = [item[1] for item in candidates[:4]]

    if top_picks:
        return "🎯 【AI 全市場黑馬即時掃描】\n(已過濾破位股與過熱區，精選站穩雙均線+外資買超標的):\n\n" + "\n\n".join(top_picks)

    watchlist = [("6274", "台燿"), ("6187", "萬潤"), ("8033", "雷虎"), ("3441", "聯一光")]
    dynamic_realtime_picks = []

    for code, default_name in watchlist:
        df, _ = get_tw_stock_data(code)
        if df is not None and len(df) >= 60:
            latest = df.iloc[-1]
            real_close = float(latest['Close'])
            real_vol = int(float(latest['Volume']) / 1000)
            
            df['MA20'] = df['Close'].rolling(window=20).mean()
            df['MA60'] = df['Close'].rolling(window=60).mean()
            ma20 = float(df.iloc[-1]['MA20'])
            ma60 = float(df.iloc[-1]['MA60'])

            if real_close < ma20 or real_close < ma60:
                status_note = "⚠️ 跌破均線偏弱，建議觀望"
            else:
                status_note = "🟢 站穩均線，多頭結構完整"

            dynamic_realtime_picks.append(
                f"🤫 {default_name} ({code})\n"
                f"   • 最新收盤價: ${real_close:.2f}\n"
                f"   • 最新成交量: {real_vol:,} 張\n"
                f"   • 技術狀態: {status_note}"
            )

    if dynamic_realtime_picks:
        return "🎯 【AI 精選觀察清單】\n(備援模式 - 包含最新均線防禦狀態):\n\n" + "\n\n".join(dynamic_realtime_picks)

    return "⚠️ 目前 API 維護中，請稍後再次嘗試。"

def analyze_stock(user_input):
    try:
        stock_code, display_name = resolve_stock_symbol(user_input)

        if not stock_code.isdigit():
            return f"⚠️ 找不到「{user_input}」的台股資料。\n您可以改輸入股票名稱或代碼（如 8358 金居、2330 台積電）進行查詢。"

        df, target_symbol = get_tw_stock_data(stock_code)
        if df is None or df.empty:
            return f"找不到代碼 [{stock_code}] 的台股價格數據，請確認代碼是否正確。"

        foreign_net = get_tw_foreign_investor(stock_code)
        revenue_info, _ = get_tw_revenue(stock_code)

        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['Hist'] = df['DIF'] - df['MACD']

        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA60'] = df['Close'].rolling(window=60).mean()
        df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()
        df['STD20'] = df['Close'].rolling(window=20).std()
        df['BB_Upper'] = df['MA20'] + (df['STD20'] * 2)

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        close = float(latest['Close'])
        prev_close = float(prev['Close'])
        ma20 = float(latest['MA20']) if not pd.isna(latest['MA20']) else close
        ma60 = float(latest['MA60']) if not pd.isna(latest['MA60']) else close
        prev_ma20 = float(prev['MA20']) if not pd.isna(prev['MA20']) else prev_close
        bb_upper = float(latest['BB_Upper']) if not pd.isna(latest['BB_Upper']) else close

        hist_today = float(latest['Hist'])
        hist_yesterday = float(prev['Hist'])

        diff_pct = ((close - ma20) / ma20) * 100 if ma20 != 0 else 0

        vol_today = float(latest['Volume'])
        vol_ma5 = float(latest['Vol_MA5']) if not pd.isna(latest['Vol_MA5']) else vol_today
        
        price_change_pct = ((close - prev_close) / prev_close) * 100
        is_price_up = price_change_pct > 0
        is_price_down = price_change_pct < 0

        is_vol_expand = vol_today >= vol_ma5 * 1.15
        is_vol_shrink = vol_today <= vol_ma5 * 0.85

        is_touch_bb_upper = close >= (bb_upper * 0.98)

        if is_touch_bb_upper:
            vol_status = f"🚨 股價接近/突破布林上軌 ({close:.2f} >= {bb_upper:.2f})\n   👉 短線極端過熱，極易引發獲利賣壓，【切勿追高】！"
        elif is_price_up and is_vol_expand:
            vol_status = f"🔥 上漲放量 (+{price_change_pct:.1f}%)\n   👉 多頭攻擊強烈，追價意願高"
        elif is_price_down and is_vol_expand:
            vol_status = f"📉 下跌放量 ({price_change_pct:.1f}%)\n   👉 恐慌盤湧出/大戶拋售，注意續跌風險"
        elif is_price_up and is_vol_shrink:
            vol_status = f"⚠️ 上漲量縮 (+{price_change_pct:.1f}%)\n   👉 量價背離！買盤停滯，需防範【見頂回落】"
        elif is_price_down and is_vol_shrink:
            vol_status = f"🛡️ 下跌量縮 ({price_change_pct:.1f}%)\n   👉 賣壓沉寂/惜售，極可能接近【見底反彈】"
        else:
            vol_status = f"➡️ 價量平穩 ({price_change_pct:+.1f}%)\n   👉 量能無明顯變化"

        if foreign_net is not None:
            if foreign_net > 0:
                foreign_text = f"買超 {foreign_net:,} 張"
            elif foreign_net < 0:
                foreign_text = f"賣超 {abs(foreign_net):,} 張"
            else:
                foreign_text = "買賣超 0 張"
        else:
            foreign_text = "查無即時外資數據"

        is_break_3pct = diff_pct <= -3.0
        is_two_days_below = (close < ma20) and (prev_close < prev_ma20)
        is_below_ma60 = close < ma60

        if is_below_ma60 or is_break_3pct or is_two_days_below:
            reasons = []
            if is_below_ma60:
                reasons.append(f"跌破季線(${ma60:.1f})")
            if is_break_3pct:
                reasons.append(f"跌破月線{abs(diff_pct):.1f}%")
            if is_two_days_below:
                reasons.append("連2日低於月線")
            signal = f"🔴【建議出場/停損】{' & '.join(reasons)}，走勢轉弱破位！"

        elif is_touch_bb_upper:
            signal = "⚠️【嚴防追高 / 可擇優減碼】股價已推升至布林上軌極限，短線隨時有拉回風險！"

        elif close < ma20:
            signal = "🟡【警戒觀望】微幅低於月線，趨勢偏弱，建議先觀望。"

        elif hist_today > 0 and hist_today >= hist_yesterday:
            signal = "🔥【多頭續抱/加碼】站穩月季線且 MACD 紅柱擴大，多方強勢控盤！"

        elif hist_today > 0 and hist_today < hist_yesterday:
            signal = "🟢【偏多持有】站穩雙均線上，但多頭力道稍緩，建議續抱。"

        elif hist_today < 0 and abs(hist_today) < abs(hist_yesterday):
            signal = "🟢【試買建倉】站穩雙均線且空方力道減弱，可考慮建立分批試買單。"

        else:
            signal = "⚪【盤整觀望】多空力道均衡，建議靜待方向確立。"

        pct_text = f"高於月線 {diff_pct:.2f}%" if diff_pct >= 0 else f"跌破月線 {abs(diff_pct):.2f}%"
        title_display = f"{display_name} ({stock_code})" if display_name != stock_code else target_symbol

        return (
            f"📊 {title_display} 技術與籌碼分析：\n"
            f"-------------------\n"
            f"最新收盤價: {close:.2f}\n"
            f"20日均線(月線): {ma20:.2f} ({pct_text})\n"
            f"60日均線(季線): {ma60:.2f}\n"
            f"布林通道上軌: {bb_upper:.2f}\n"
            f"量價與通道結構:\n   {vol_status}\n"
            f"外資籌碼: {foreign_text}\n"
            f"-------------------\n"
            f"📈 基本面與營收：\n"
            f"   {revenue_info}\n"
            f"-------------------\n"
            f"💡 操作建議：\n{signal}"
        )
    except Exception as e:
        return f"分析發生錯誤: {str(e)}"

if __name__ == "__main__":
    app.run(port=5000)
