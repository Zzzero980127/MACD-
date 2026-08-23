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

STOCK_NAME_MAP = {}
STOCK_INDUSTRY_MAP = {}

def load_all_taiwan_stocks():
    global STOCK_NAME_MAP, STOCK_INDUSTRY_MAP
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        url = "https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInfo"
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200 and res.json().get("status") == 200:
            for item in res.json().get("data", []):
                s_id = str(item.get("stock_id", "")).strip()
                s_name = str(item.get("stock_name", "")).strip()
                s_ind = str(item.get("industry_category", "")).strip()
                if s_id and s_name:
                    STOCK_NAME_MAP[s_name] = s_id
                    STOCK_INDUSTRY_MAP[s_id] = s_ind
    except Exception as e:
        print(f"FinMind Bulk Load Error: {e}")

load_all_taiwan_stocks()

@app.route("/", methods=['GET'])
def index():
    return 'TW Stock Bot Active!'

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
        res = requests.get(url, headers=headers, timeout=3)
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
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockMonthRevenue&data_id={stock_id}&start_date=2024-01-01"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=3)
        data = res.json()
        if data.get("status") == 200 and data.get("data"):
            df = pd.DataFrame(data["data"])
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
                else:
                    status = "🟡 稍低於預期 (放緩/整理)"

                return f"{month_str} | YoY: {yoy_str} | MoM: {mom_str}\n   評價: {status}", yoy
    except Exception:
        pass
    return "無即時營收數據", None

def get_tw_foreign_investor(stock_id):
    url = f"https://api.finmindtrade.com/api/v4/data?dataset=TaiwanStockInstitutionalInvestorsBuySell&data_id={stock_id}&start_date=2024-08-01"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = requests.get(url, headers=headers, timeout=3)
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
        url_twse = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        res_q = requests.get(url_twse, headers=headers, timeout=3)
        if res_q.status_code == 200 and isinstance(res_q.json(), list):
            for item in res_q.json():
                quotes_data[item['Code']] = {
                    'name': item.get('Name', '').strip(),
                    'close': str(item.get('ClosingPrice', '0')).replace(',', ''),
                    'vol': str(item.get('TradeVolume', '0')).replace(',', '')
                }
    except Exception as e:
        print(f"TWSE Fetch Failed: {e}")

    tech_keywords = ["半導體", "電子", "電腦", "光電", "通訊", "網通", "資訊服務", "電子零組件"]
    candidates = []

    for code, info in quotes_data.items():
        if not code.isdigit() or code.startswith("00"):
            continue
            
        industry = STOCK_INDUSTRY_MAP.get(code, "")
        if any(tech in industry for tech in tech_keywords):
            continue

        try:
            close_val = float(info['close']) if info['close'] != '--' else 0
            vol_val = int(info['vol']) / 1000 if info['vol'].isdigit() else 0

            if 10 <= close_val <= 300 and vol_val >= 500:
                candidates.append({
                    'code': code,
                    'name': info['name'] or STOCK_NAME_MAP.get(code, code),
                    'close': close_val,
                    'volume': vol_val,
                    'industry': industry or "非科技傳產"
                })
        except: continue

    candidates.sort(key=lambda x: x['volume'], reverse=True)
    # 縮小掃描範圍至 20 檔以利於快速回應，避免 LINE 逾時
    top_targets = candidates[:20]

    valid_candidates = []
    
    for item in top_targets:
        code = item['code']
        df, _ = get_tw_stock_data(code)
        if df is None or len(df) < 26:
            continue

        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['DIF'] = exp1 - exp2
        df['MACD'] = df['DIF'].ewm(span=9, adjust=False).mean()
        df['Hist'] = df['DIF'] - df['MACD']

        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['STD20'] = df['Close'].rolling(window=20).std()
        df['BB_Upper'] = df['MA20'] + (df['STD20'] * 2)
        
        latest_row = df.iloc[-1]
        prev_row = df.iloc[-2]
        
        close = float(latest_row['Close'])
        ma20 = float(latest_row['MA20']) if not pd.isna(latest_row['MA20']) else close
        bb_upper = float(latest_row['BB_Upper']) if not pd.isna(latest_row['BB_Upper']) else close

        hist_today = float(latest_row['Hist'])
        hist_yesterday = float(prev_row['Hist'])

        foreign_net = get_tw_foreign_investor(code)

        # 外資必須買超 > 0
        is_foreign_buy = (foreign_net is not None and foreign_net > 0)
        
        # MACD 綠柱縮短 或 紅柱剛發動
        is_macd_rebound = (hist_today < 0 and abs(hist_today) <= abs(hist_yesterday)) or \
                          (hist_today > 0 and hist_today >= hist_yesterday)

        if is_foreign_buy and is_macd_rebound and close < (bb_upper * 0.98):
            macd_status = "📉 綠柱收斂 (空方減弱)" if hist_today < 0 else "🔥 紅柱擴大 (多頭發動)"
            valid_candidates.append({
                'code': code,
                'name': item['name'],
                'industry': item['industry'],
                'close': close,
                'ma20': ma20,
                'foreign_net': foreign_net,
                'macd_status': macd_status
            })

    valid_candidates.sort(key=lambda x: x['foreign_net'], reverse=True)
    final_top5 = valid_candidates[:5]

    results = []
    for item in final_top5:
        card = (
            f"🤫 {item['name']} ({item['code']}) - [{item['industry']}]\n"
            f"   • 收盤價: ${item['close']:.2f} (月線 ${item['ma20']:.1f})\n"
            f"   • MACD訊號: {item['macd_status']}\n"
            f"   • 籌碼動態: 🔥 外資買超 {item['foreign_net']:,} 張"
        )
        results.append(card)

    if results:
        return "🎯 【AI 盤後轉折/外資加碼/非科技 Top 5】\n(MACD 空方衰退 + 外資轉買卡位):\n\n" + "\n\n".join(results)
    
    return "⚠️ 今日無符合「MACD空方衰退/轉折 + 外資買超」之非科技標的，建議觀望。"

def analyze_stock(user_input):
    try:
        stock_code, display_name = resolve_stock_symbol(user_input)

        if not stock_code.isdigit():
            return f"⚠️ 找不到「{user_input}」的台股資料。\n您可以輸入名稱或代碼（如 2603 長榮、8358 金居）查詢。"

        df, target_symbol = get_tw_stock_data(stock_code)
        if df is None or df.empty:
            return f"找不到代碼 [{stock_code}] 的數據，請確認輸入是否正確。"

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
        bb_upper = float(latest['BB_Upper']) if not pd.isna(latest['BB_Upper']) else close

        hist_today = float(latest['Hist'])
        hist_yesterday = float(prev['Hist'])

        diff_pct = ((close - ma20) / ma20) * 100 if ma20 != 0 else 0

        vol_today = float(latest['Volume'])
        vol_ma5 = float(latest['Vol_MA5']) if not pd.isna(latest['Vol_MA5']) else vol_today
        
        price_change_pct = ((close - prev_close) / prev_close) * 100
        is_vol_expand = vol_today >= vol_ma5 * 1.15
        is_vol_shrink = vol_today <= vol_ma5 * 0.85
        is_touch_bb_upper = close >= (bb_upper * 0.98)

        if is_touch_bb_upper:
            vol_status = f"🚨 接近/突破布林上軌 ({close:.2f} >= {bb_upper:.2f})\n   👉 短線極端過熱，切勿盲目追高！"
        elif price_change_pct > 0 and is_vol_expand:
            vol_status = f"🔥 上漲放量 (+{price_change_pct:.1f}%)\n   👉 多頭攻擊強烈"
        elif price_change_pct < 0 and is_vol_expand:
            vol_status = f"📉 下跌放量 ({price_change_pct:.1f}%)\n   👉 注意大戶賣壓與續跌風險"
        elif price_change_pct > 0 and is_vol_shrink:
            vol_status = f"⚠️ 上漲量縮 (+{price_change_pct:.1f}%)\n   👉 量價背離，提防高位拉回"
        elif price_change_pct < 0 and is_vol_shrink:
            vol_status = f"🛡️ 下跌量縮 ({price_change_pct:.1f}%)\n   👉 賣壓沉寂，容易迎來止跌反彈"
        else:
            vol_status = f"➡️ 價量平穩 ({price_change_pct:+.1f}%)"

        if foreign_net is not None:
            foreign_text = f"買超 {foreign_net:,} 張" if foreign_net > 0 else (f"賣超 {abs(foreign_net):,} 張" if foreign_net < 0 else "買賣超 0 張")
        else:
            foreign_text = "查無即時外資數據"

        if close < ma60 or diff_pct <= -3.0:
            signal = "🔴【建議出場/觀望】跌破關鍵支撐或均線走弱！"
        elif is_touch_bb_upper:
            signal = "⚠️【擇優減碼】股價推升至布林上軌過熱區，注意拉回。"
        elif close >= ma20 and hist_today > 0 and hist_today >= hist_yesterday:
            signal = "🔥【多頭控盤】站穩均線且 MACD 紅柱擴力，可持股或分批佈局。"
        elif close >= ma20:
            signal = "🟢【偏多觀察】站穩月線軌道，走勢穩健。"
        else:
            signal = "⚪【觀望為主】多空方向未定。"

        pct_text = f"高於月線 {diff_pct:.2f}%" if diff_pct >= 0 else f"跌破月線 {abs(diff_pct):.2f}%"
        title_display = f"{display_name} ({stock_code})" if display_name != stock_code else target_symbol

        return (
            f"📊 {title_display} 技術與籌碼分析：\n"
            f"-------------------\n"
            f"最新收盤價: {close:.2f}\n"
            f"20日均線(月線): {ma20:.2f} ({pct_text})\n"
            f"60日均線(季線): {ma60:.2f}\n"
            f"布林通道上軌: {bb_upper:.2f}\n"
            f"量價結構:\n   {vol_status}\n"
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
