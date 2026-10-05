import os
import re
import json
import datetime
import requests
import pandas as pd
import psycopg2
import gspread
from google.oauth2.service_account import Credentials

FINMIND_TOKEN = (os.environ.get('FINMIND_API_TOKEN') or os.environ.get('FINMIND_TOKEN', '')).strip()
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()

TOTAL_CAPITAL = 5000000.0  # 💰 500 萬總資金池

# =============================================================================
# 🔧 RSI / KD 過熱過濾參數 (集中在這裡，方便調整與回測比較)
# =============================================================================
USE_BUY_FILTER = True      # 買進前檢查：過熱就不追高
USE_EXIT_FILTER = True     # 持股中檢查：過熱就提前出場

RSI_PERIOD = 14
KD_N = 9                   # 台股常用 KD(9,3,3)

# --- 追強勢股版本：強勢股 RSI>70、K 高檔鈍化是常態，所以只擋「極端過熱」 ---
# 買進過濾：RSI 與 K 「同時」極端才視為過熱 (AND)，單一指標偏高不擋
RSI_BUY_MAX = 82.0
K_BUY_MAX = 90.0
# 短線追高保護：收盤價高於 5 日均線超過 N% (乖離過大，隔天容易拉回)；設 None 可關閉
BIAS5_BUY_MAX = 10.0

# 出場輔助 (🆕 只在「已經有獲利」時才啟動，當作鎖利，不會在虧損時提前砍)
RSI_EXIT = 85.0            # RSI 衝過 85 且有獲利 → 鎖利
KD_HIGH_ZONE = 85.0        # K 在 85 以上向下穿越 D (高檔死叉) 且有獲利 → 鎖利
EXIT_ONLY_WHEN_PROFIT = True

# MACD 柱狀體連續縮小幾天才出場 (原程式為 1 天，容易在飆股洗盤時被洗出去；想維持原樣就設 1)
MACD_WEAK_DAYS = 1

# =============================================================================
# 🧪 週結算短線參數 (2026-10 回測 2026/08/24~10/04 共 42 份報告後選定「組合D」)
#    舊設定：隔日開盤價進場 勝率 36.9%、每筆 -0.17%、最大回撤 56,144
#    組合D ：隔日開盤價進場 勝率 46.7%、每筆 +0.32%、最大回撤 17,649 (前後半段皆為正)
#    樣本僅約 6 週，請持續用 backtest.py 追蹤；想退回舊設定見每行註解
# =============================================================================
STOP_LOSS_PCT = -5.0          # 停損 (%)
TAKE_PROFIT_PCT = 5.0         # 停利 (%)；舊設定 None
MACD_EXIT_ONLY_PROFIT = True  # MACD 減弱只在有獲利時才出場 (MACD 1 天就賣的單勝率僅 6%)；舊設定 False
MARKET_MA = None              # 大盤濾網：0050 站上 N 日均線才買 (回測無效，維持關閉)
MIN_BUY_SCORE = None          # 週一~三買進的最低分數 (回測無差異，維持關閉)
TOP_N_PER_STRATEGY = 3        # 週一~三每個策略最多買幾檔；舊設定 5
STRATEGY2_TOP_N = 2           # 週一~三策略二另外限制檔數 (策略二整體勝率偏低)；舊設定 None

PRICE_LOOKBACK_DAYS = 90   # 指標暖機用，約 60 個交易日

# 🕗 一律用台灣時間判斷日期與星期 (Render 等雲端主機預設 UTC，台灣早上 8 點前會被算成前一天)
TW_TZ = datetime.timezone(datetime.timedelta(hours=8))

def now_tw():
    return datetime.datetime.now(TW_TZ).replace(tzinfo=None)

# =============================================================================
# 📐 技術指標計算
# =============================================================================
def fetch_price_df(code, days=PRICE_LOOKBACK_DAYS):
    """從 FinMind 抓日 K，並轉成數值型態。FinMind 的最高/最低價欄位為 max / min"""
    start_date = (now_tw() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    params = {"dataset": "TaiwanStockPrice", "data_id": code, "start_date": start_date}
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN
    res = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, timeout=8).json()
    data = res.get("data")
    if not data:
        return None
    df = pd.DataFrame(data)
    for col in ['open', 'max', 'min', 'close']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    # 排除停牌 / 異常 (價格為 0 或缺值)
    df = df.dropna(subset=['close', 'max', 'min'])
    df = df[df['close'] > 0].reset_index(drop=True)
    return df

def calc_rsi(close, period=RSI_PERIOD):
    """Wilder's RSI"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.where(avg_loss != 0)
    rsi = 100 - 100 / (1 + rs)
    # 完全沒有下跌時 avg_loss=0，RSI 視為 100
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    return rsi

def calc_kd(df, n=KD_N):
    """台股標準 KD：RSV 算法，K = 2/3*前K + 1/3*RSV，D = 2/3*前D + 1/3*K，初值 50"""
    low_n = df['min'].rolling(n, min_periods=n).min()
    high_n = df['max'].rolling(n, min_periods=n).max()
    rng = (high_n - low_n).where((high_n - low_n) != 0)
    rsv = (df['close'] - low_n) / rng * 100

    k_list, d_list = [], []
    k_prev, d_prev = 50.0, 50.0
    for v in rsv:
        if pd.notna(v):
            k_prev = k_prev * 2 / 3 + float(v) / 3
            d_prev = d_prev * 2 / 3 + k_prev / 3
            k_list.append(k_prev)
            d_list.append(d_prev)
        else:
            k_list.append(float('nan'))
            d_list.append(float('nan'))
    return pd.Series(k_list, index=df.index), pd.Series(d_list, index=df.index)

def get_indicator_status(df):
    """回傳最新一天的 RSI / K / D 與是否高檔死叉；資料不足回傳 None"""
    if df is None or len(df) < max(RSI_PERIOD, KD_N) + 5:
        return None
    rsi = calc_rsi(df['close'])
    k, d = calc_kd(df)
    rsi_now, k_now, d_now = rsi.iloc[-1], k.iloc[-1], d.iloc[-1]
    k_prev, d_prev = k.iloc[-2], d.iloc[-2]
    if pd.isna(rsi_now) or pd.isna(k_now) or pd.isna(d_now):
        return None
    dead_cross = bool(
        pd.notna(k_prev) and pd.notna(d_prev)
        and k_prev >= d_prev and k_now < d_now and k_prev > KD_HIGH_ZONE
    )
    ma5 = df['close'].rolling(5).mean().iloc[-1]
    bias5 = float((df['close'].iloc[-1] / ma5 - 1) * 100) if pd.notna(ma5) and ma5 > 0 else 0.0
    return {"rsi": float(rsi_now), "k": float(k_now), "d": float(d_now),
            "kd_dead_cross": dead_cross, "bias5": bias5}

def buy_overheat_reasons(st):
    """依指標狀態回傳過熱原因清單；空清單 = 可以買 (實盤與回測共用)"""
    reasons = []
    # RSI 與 K 同時極端才算過熱 (強勢股單一指標偏高屬正常)
    if st['rsi'] >= RSI_BUY_MAX and st['k'] >= K_BUY_MAX:
        reasons.append(f"RSI {st['rsi']:.1f} 且 K {st['k']:.1f} 同時極端")
    # 短線乖離過大 (追高風險)
    if BIAS5_BUY_MAX is not None and st['bias5'] >= BIAS5_BUY_MAX:
        reasons.append(f"5日乖離 {st['bias5']:.1f}% ≥ {BIAS5_BUY_MAX:.0f}%")
    return reasons

def is_overheated_for_buy(code, name, cache):
    """買進前過熱檢查。回傳 True = 過熱 (不買)。資料取得失敗時不阻擋 (沿用原本行為)"""
    if not USE_BUY_FILTER:
        return False
    if code in cache:
        return cache[code]
    overheated = False
    try:
        st = get_indicator_status(fetch_price_df(code))
        if st is None:
            print(f"⚠️ [過熱檢查] {code} {name} 指標資料不足，略過過濾直接放行", flush=True)
        else:
            reasons = buy_overheat_reasons(st)
            info = f"RSI {st['rsi']:.1f} | K {st['k']:.1f} | D {st['d']:.1f} | 5日乖離 {st['bias5']:.1f}%"
            if reasons:
                overheated = True
                print(f"🧊 [過熱略過] {code} {name} | {' / '.join(reasons)} | {info}", flush=True)
            else:
                print(f"✅ [過熱檢查通過] {code} {name} | {info}", flush=True)
    except Exception as e:
        print(f"⚠️ [過熱檢查] {code} {name} 失敗，略過過濾直接放行: {e}", flush=True)
    cache[code] = overheated
    return overheated

def check_exit_signal(df, ret, macd_weak_days=None, use_exit_filter=None):
    """
    指標面出場判斷 (實盤與回測共用)，回傳 (should_sell, exit_reason)。
    判斷順序：停損 → 停利 → RSI/KD 鎖利 (有獲利才啟動) → MACD 柱狀體連續縮小。日期規則不在這裡。
    """
    if macd_weak_days is None:
        macd_weak_days = MACD_WEAK_DAYS
    if use_exit_filter is None:
        use_exit_filter = USE_EXIT_FILTER

    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    osc = (exp1 - exp2) - (exp1 - exp2).ewm(span=9, adjust=False).mean()
    osc_tail = [float(x) for x in osc.iloc[-(macd_weak_days + 1):]]
    macd_weak = len(osc_tail) == macd_weak_days + 1 and all(
        osc_tail[i + 1] < osc_tail[i] for i in range(macd_weak_days)
    )

    ind = get_indicator_status(df) if use_exit_filter else None
    can_lock_profit = (ret > 0) or (not EXIT_ONLY_WHEN_PROFIT)

    if ret <= STOP_LOSS_PCT:
        return True, f"🚨 大跌觸發止損 ({STOP_LOSS_PCT:g}%)"
    if TAKE_PROFIT_PCT is not None and ret >= TAKE_PROFIT_PCT:
        return True, f"🎯 達停利出場 (+{TAKE_PROFIT_PCT:g}%)"
    if ind and can_lock_profit and ind['rsi'] > RSI_EXIT:
        return True, f"🔥 RSI 過熱鎖利出場 (RSI {ind['rsi']:.1f})"
    if ind and can_lock_profit and ind['kd_dead_cross']:
        return True, f"🔥 KD 高檔死叉鎖利出場 (K {ind['k']:.1f} < D {ind['d']:.1f})"
    if macd_weak and (ret > 0 or not MACD_EXIT_ONLY_PROFIT):
        return True, "📉 MACD多頭減弱出場"
    return False, ""

def market_ok(df_market):
    """大盤濾網：0050 收盤在 MARKET_MA 日均線之上才允許買進；未啟用或資料不足時放行"""
    if not MARKET_MA or df_market is None or len(df_market) < MARKET_MA:
        return True
    ma = df_market['close'].rolling(MARKET_MA).mean().iloc[-1]
    return float(df_market['close'].iloc[-1]) >= float(ma)

def calendar_exit_reason(weekday, buy_weekday):
    """T+2 / 週五日期強制出場規則，回傳出場原因；不需出場回傳空字串 (實盤與回測共用)"""
    if weekday == 3 and buy_weekday in [0, 1, 2]:
        return "📅 週四清空週一至週三持股 (T+2)"
    if weekday >= 4 and buy_weekday == 3:
        return "📅 週五清空週四精選短線股 (T+2)"
    if weekday >= 4:
        # 週五起清空所有殘餘持股 (例如週四排程沒跑到)，讓週五結算涵蓋整週
        return "📅 週五/週末強制結算殘餘持股"
    return ""

def pick_buy_targets(st1_targets, st2_targets, weekday, verbose=True):
    """依星期決定買進標的 (輸入為已過濾過熱後的清單，實盤與回測共用)"""
    # 週一 ~ 週三建倉：兩策略各取前 N 名 (可設最低分數)
    if weekday in [0, 1, 2]:
        if MIN_BUY_SCORE is not None:
            st1_targets = [t for t in st1_targets if t[4] >= MIN_BUY_SCORE]
            st2_targets = [t for t in st2_targets if t[4] >= MIN_BUY_SCORE]
        st2_n = TOP_N_PER_STRATEGY if STRATEGY2_TOP_N is None else STRATEGY2_TOP_N
        return st1_targets[:TOP_N_PER_STRATEGY] + st2_targets[:st2_n]

    # 週四建倉：精選 1 檔 (優先選擇策略二 >= 100 分最高分，否則買策略一第 1 名)
    if weekday == 3:
        st2_qualified = [t for t in st2_targets if t[4] >= 100]
        if st2_qualified:
            max_score = max(t[4] for t in st2_qualified)
            picked = [t for t in st2_qualified if t[4] == max_score][:1]
            if verbose:
                print(f"🔥 [週四精選買入] 選用策略二最高分 ({picked[0][4]}分) 標的: {picked[0][1]}", flush=True)
            return picked
        if st1_targets:
            if verbose:
                print(f"🔥 [週四精選買入] 策略二未達 100 分，改選策略一第 1 名: {st1_targets[0][1]}", flush=True)
            return st1_targets[:1]
    return []

# =============================================================================
# 📝 解析 cron_job 產生的推薦報告
# =============================================================================
STRATEGY_HEADER_RE = re.compile(r'【\s*策略\s*(一|二|1|2)')
CODE_RE = re.compile(r'([0-9]{4})\s+([一-龥A-Za-z0-9\*\-]+)')
PRICE_RE = re.compile(r'(?:現價|收盤|收|價格)[:：\s]*\$?\s*([0-9]+\.?[0-9]*)')
SCORE_RE = re.compile(r'(-?\d+)\s*(?:分|pts)', re.IGNORECASE)

def parse_recommendations(content):
    """
    解析推薦報告，回傳 (策略一清單, 策略二清單)，每筆為 (code, name, price, strategy, score)。
    - 只認「【策略一」「【策略二」標題切換策略 (說明行「已排除策略二標的」不可誤判)
    - 分數在代號下一行 (👉 得分:85分)，要往下一行補上
    """
    st1_targets, st2_targets = [], []
    current_strategy = None
    last_item = None  # 最近一檔尚未取得分數的標的 (list，方便補分數)

    for line in content.split('\n'):
        line_str = line.strip()
        header = STRATEGY_HEADER_RE.search(line_str)
        if header:
            current_strategy = "策略一" if header.group(1) in ("一", "1") else "策略二"
            last_item = None
            continue

        code_match = CODE_RE.search(line_str)
        price_match = PRICE_RE.search(line_str)
        if code_match and price_match and current_strategy:
            score_match = SCORE_RE.search(line_str)
            item = [code_match.group(1), code_match.group(2), float(price_match.group(1)),
                    current_strategy, int(score_match.group(1)) if score_match else None]
            (st1_targets if current_strategy == "策略一" else st2_targets).append(item)
            last_item = item if item[4] is None else None
            continue

        if last_item is not None:
            score_match = SCORE_RE.search(line_str)
            if score_match:
                last_item[4] = int(score_match.group(1))
                last_item = None

    def finalize(items):
        return [(c, n, p, s, sc if sc is not None else 0) for c, n, p, s, sc in items]
    return finalize(st1_targets), finalize(st2_targets)

# =============================================================================
# 🗄️ 資料庫
# =============================================================================
def get_db_connection():
    if not DATABASE_URL: return None
    try:
        url = DATABASE_URL
        if "sslmode" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}sslmode=require"
        return psycopg2.connect(url, connect_timeout=10)
    except Exception as e:
        print(f"⚠️ [DB Log] 連線失敗: {e}", flush=True)
        return None

def init_sim_db():
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sim_trades (
                id SERIAL PRIMARY KEY,
                stock_code VARCHAR(10) NOT NULL,
                stock_name VARCHAR(50) NOT NULL,
                strategy_type VARCHAR(20) NOT NULL,
                buy_date VARCHAR(20) NOT NULL,
                buy_price NUMERIC(10, 2) NOT NULL,
                sell_date VARCHAR(20),
                sell_price NUMERIC(10, 2),
                return_rate NUMERIC(10, 2),
                status VARCHAR(10) DEFAULT 'HOLD',
                exit_reason TEXT
            );
        ''')
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ [DB Log] 初始化資料庫失敗: {e}", flush=True)

def get_0050_weekly_return():
    """抓取 0050 當週開盤價與最新收盤價，計算當週漲跌幅 (%) [加入週末/假期回溯強化]"""
    try:
        now = now_tw()
        monday_dt = now - datetime.timedelta(days=now.weekday())

        # 往前多抓 3 天，避免週末或連假時 API 查無當週資料
        start_date = (monday_dt - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
        
        params = {
            "dataset": "TaiwanStockPrice", 
            "data_id": "0050", 
            "start_date": start_date
        }
        if FINMIND_TOKEN: 
            params["token"] = FINMIND_TOKEN

        res = requests.get("https://api.finmindtrade.com/api/v4/data", params=params, timeout=10).json()
        data = res.get("data", [])
        
        if data:
            df = pd.DataFrame(data)
            monday_str = monday_dt.strftime("%Y-%m-%d")
            week_df = df[df['date'] >= monday_str]
            
            # 若當週資料受假日影響未出，則使用回溯區間最後資料
            if week_df.empty:
                week_df = df

            week_open = float(week_df.iloc[0]['open'])
            week_close = float(week_df.iloc[-1]['close'])
            weekly_return = ((week_close - week_open) / week_open) * 100
            
            print(f"📈 [0050 當週績效] 週一開盤({week_df.iloc[0]['date']}): {week_open} | 最新收盤({week_df.iloc[-1]['date']}): {week_close} | 漲跌幅: {weekly_return:+.2f}%", flush=True)
            return round(weekly_return, 2)
        else:
            print(f"⚠️ [0050 API Warning] FinMind 未回傳 0050 資料: {res}", flush=True)
            
    except Exception as e:
        print(f"⚠️ [0050 API Error] 抓取 0050 績效失敗: {e}", flush=True)
        
    return 0.0

def sync_to_google_sheets(summary):
    sheets_json = os.environ.get('GOOGLE_SHEETS_JSON', '').strip()
    sheet_key = os.environ.get('SPREADSHEET_KEY', '1CrADfLGVOhfrhNB_Er-0XJCazb6onD7vjWf7QPdDpO0').strip()

    if not sheets_json:
        print("⚠️ [Google Sheets] 未偵測到 GOOGLE_SHEETS_JSON 環境變數，跳過試算表同步。", flush=True)
        return

    try:
        scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        
        # 🎯 1. JSON 控制字元過濾容錯修復
        try:
            if '\\n' in sheets_json:
                sheets_json = sheets_json.replace('\\n', '\n')
            info = json.loads(sheets_json, strict=False)
        except Exception:
            clean_str = re.sub(r'[\r\n\t]+', ' ', sheets_json)
            info = json.loads(clean_str, strict=False)

        creds = Credentials.from_service_account_info(info, scopes=scope)
        client = gspread.authorize(creds)

        spreadsheet = client.open_by_key(sheet_key)
        sheet = spreadsheet.sheet1

        existing_rows = sheet.get_all_values()
        
        # 🎯 2. 表頭加入「策略期望報酬 (%)」，擴充至 13 欄
        if not existing_rows:
            header = [
                "結算日期", "交易總筆數", "勝場", "敗場", "勝率 (%)", 
                "週淨損益 ($)", "累積總損益 ($)", "平均獲利 (%)", "平均虧損 (%)", 
                "風報比", "策略期望報酬 (%)", "0050 同期漲跌 (%)", "是否擊敗 0050"
            ]
            sheet.append_row(header)

        target_date = summary.get("date", "")
        for row in existing_rows:
            if len(row) > 0 and row[0] == target_date:
                print(f"ℹ️ [Google Sheets] 日期 {target_date} 已存在於試算表中，跳過重複寫入。", flush=True)
                return

        # 🎯 3. 安全強制轉型，精準計算「策略期望報酬 (%)」
        avg_win = float(summary.get("avg_win", 0.0))
        avg_loss = float(summary.get("avg_loss", 0.0))
        win_rate = float(summary.get("win_rate", 0.0)) / 100.0
        loss_rate = 1.0 - win_rate
        
        strategy_avg_return_pct = (win_rate * avg_win) - (loss_rate * avg_loss)
        benchmark_0050 = float(summary.get("benchmark_0050", 0.0))

        if strategy_avg_return_pct >= benchmark_0050:
            beat_0050_str = "🟢 擊敗0050"
        else:
            beat_0050_str = "❌ 落後0050"

        # 🎯 4. 對齊 A ~ M 欄位陣列 (共 13 欄)
        row = [
            summary.get("date", ""),                                # A: 結算日期
            summary.get("total", 0),                               # B: 交易總筆數 (當週)
            summary.get("win", 0),                                 # C: 勝場 (當週)
            summary.get("loss", 0),                                # D: 敗場 (當週)
            f"{float(summary.get('win_rate', 0.0)):.2f}%",         # E: 勝率 (%)
            summary.get("weekly_pnl", 0),                          # F: 週淨損益 ($)
            summary.get("total_pnl", 0),                           # G: 累積總損益 ($)
            f"{avg_win:.2f}%",                                     # H: 平均獲利 (%)
            f"{avg_loss:.2f}%",                                    # I: 平均虧損 (%)
            summary.get("risk_reward_ratio", 0.0),                 # J: 風報比
            f"{strategy_avg_return_pct:+.2f}%",                    # K: 策略期望報酬 (%)
            f"{benchmark_0050:+.2f}%",                             # L: 0050 同期漲跌 (%)
            beat_0050_str                                          # M: 是否擊敗 0050
        ]

        sheet.append_row(row)
        print(f"🎉 [Google Sheets] 成功寫入！策略期望報酬: {strategy_avg_return_pct:+.2f}% vs 0050: {benchmark_0050:+.2f}%", flush=True)
    except Exception as e:
        print(f"❌ [Google Sheets Sync Error] {e}", flush=True)

def process_simulation():
    conn = get_db_connection()
    if not conn: return
    
    now = now_tw()
    today_str = now.strftime('%Y-%m-%d')
    weekday = now.weekday()  # 0:週一, 1:週二, 2:週三, 3:週四, 4:週五, 5:週六, 6:週日

    try:
        cursor = conn.cursor()
        print(f"🎯 [Sim Engine] 執行日期: {today_str} (週{weekday + 1}) | 總資金設定: ${TOTAL_CAPITAL:,.0f}", flush=True)

        # -------------------------------------------------------------------------
        # A. 賣出邏輯 (精準執行 T+2 雙軌出場 + 🆕 RSI/KD 過熱出場)
        # -------------------------------------------------------------------------
        cursor.execute("SELECT id, stock_code, stock_name, buy_price, buy_date FROM sim_trades WHERE status = 'HOLD';")
        holding_stocks = cursor.fetchall()

        for item in holding_stocks:
            trade_id, code, name, buy_price, buy_date_str = item
            buy_price = float(buy_price)
            buy_dt = datetime.datetime.strptime(buy_date_str, '%Y-%m-%d')
            buy_weekday = buy_dt.weekday()

            # 🆕 改用 fetch_price_df (回溯 90 天，讓 RSI/KD 有足夠暖機資料，並取得 max/min 欄位)
            df = fetch_price_df(code)

            if df is not None and len(df) >= 2:
                curr_price = float(df.iloc[-1]['close'])
                ret = ((curr_price - buy_price) / buy_price) * 100

                # 風控 / RSI·KD 鎖利 / MACD 減弱 先行判斷，再套用 T+2 日期強制清空規則
                should_sell, exit_reason = check_exit_signal(df, ret)
                if not should_sell:
                    exit_reason = calendar_exit_reason(weekday, buy_weekday)
                    should_sell = bool(exit_reason)

                if should_sell:
                    cursor.execute('''
                        UPDATE sim_trades 
                        SET sell_date = %s, sell_price = %s, return_rate = %s, status = 'CLOSED', exit_reason = %s
                        WHERE id = %s;
                    ''', (today_str, curr_price, ret, exit_reason, trade_id))
                    print(f"💰 [模擬賣出] {code} {name} | 買價: {buy_price} -> 賣價: {curr_price} | 報酬: {ret:+.2f}% | 原因: {exit_reason}", flush=True)

        # -------------------------------------------------------------------------
        # B. 週結算與同步 (🎯 修正區塊：SQL 去重並限定本週平倉，確保當週精準 15 筆)
        # -------------------------------------------------------------------------
        # 🆕 週五起才結算：原本週四就以「週五日期」寫入試算表，週五平倉的週四精選股會因日期重複被跳過、永遠沒算進週報
        if weekday in [4, 5, 6]:
            monday_dt = now - datetime.timedelta(days=now.weekday())
            friday_dt = monday_dt + datetime.timedelta(days=4)
            sunday_dt = monday_dt + datetime.timedelta(days=6)
            start_str = monday_dt.strftime('%Y-%m-%d')
            end_str = friday_dt.strftime('%Y-%m-%d')
            query_end_str = sunday_dt.strftime('%Y-%m-%d')  # 週末強制結算的平倉也算本週

            # 使用 DISTINCT ON + 精準日期過濾，防止歷史/重複紀錄污染
            cursor.execute("""
                SELECT DISTINCT ON (stock_code) buy_price, sell_price, return_rate, sell_date
                FROM sim_trades 
                WHERE status = 'CLOSED' 
                  AND sell_date >= %s 
                  AND sell_date <= %s
                ORDER BY stock_code, id DESC;
            """, (start_str, query_end_str))
            
            weekly_trades = cursor.fetchall()
            
            if len(weekly_trades) > 0:
                # 當週勝敗筆數與勝率 (大於 0 為勝，小於等於 0 為敗)
                weekly_win_returns = [float(t[2]) for t in weekly_trades if float(t[2]) > 0]
                weekly_loss_returns = [abs(float(t[2])) for t in weekly_trades if float(t[2]) <= 0]
                
                weekly_wins = len(weekly_win_returns)
                weekly_losses = len(weekly_loss_returns)
                weekly_count = weekly_wins + weekly_losses  # 🎯 筆數精準對齊真實當週交易檔數 (15 筆)
                weekly_win_rate = (weekly_wins / weekly_count * 100) if weekly_count > 0 else 0.0
                
                # 平均獲利/虧損與風報比
                avg_win = (sum(weekly_win_returns) / weekly_wins) if weekly_wins > 0 else 0.0
                avg_loss = (sum(weekly_loss_returns) / weekly_losses) if weekly_losses > 0 else 0.0
                rrr = round(avg_win / avg_loss, 2) if avg_loss > 0 else (round(avg_win, 2) if avg_win > 0 else 0.0)
                
                # 當週淨損益與歷史累積總損益
                weekly_pnl = sum(((float(t[1]) - float(t[0])) / float(t[0])) * 100000 for t in weekly_trades)
                
                cursor.execute("SELECT buy_price, sell_price FROM sim_trades WHERE status = 'CLOSED';")
                all_closed = cursor.fetchall()
                total_pnl = sum(((float(t[1]) - float(t[0])) / float(t[0])) * 100000 for t in all_closed)

                benchmark_0050 = get_0050_weekly_return()

                summary = {
                    "date": end_str,                        # 結算日 (當週五)
                    "total": weekly_count,                  # 當週真實交易筆數
                    "win": weekly_wins,                    # 當週勝場
                    "loss": weekly_losses,                  # 當週敗場
                    "win_rate": round(weekly_win_rate, 2), # 當週勝率 (%)
                    "weekly_pnl": int(weekly_pnl),          # 當週淨損益 ($)
                    "total_pnl": int(total_pnl),            # 歷史累積總損益 ($)
                    "avg_win": round(avg_win, 2),
                    "avg_loss": round(avg_loss, 2),
                    "risk_reward_ratio": rrr,
                    "benchmark_0050": benchmark_0050
                }
                
                print(f"📊 [週結算 Summary]: {summary}", flush=True)
                sync_to_google_sheets(summary)

        # -------------------------------------------------------------------------
        # C. 買進邏輯 (包含週四買入週三精選股條件 + 🆕 RSI/KD 過熱過濾)
        # -------------------------------------------------------------------------
        if weekday in [0, 1, 2, 3]:
            cursor.execute("SELECT content FROM history WHERE date = 'LATEST';")
            row = cursor.fetchone()

            market_pass = True
            if MARKET_MA:
                try:
                    market_pass = market_ok(fetch_price_df("0050"))
                except Exception as e:
                    print(f"⚠️ [大盤濾網] 0050 資料取得失敗，略過濾網: {e}", flush=True)
                if not market_pass:
                    print(f"🛑 [大盤濾網] 0050 跌破 {MARKET_MA} 日均線，今日不建倉", flush=True)

            if market_pass and row and row[0]:
                st1_targets, st2_targets = parse_recommendations(row[0])
                print(f"📋 [推薦解析] 策略一 {len(st1_targets)} 檔 | 策略二 {len(st2_targets)} 檔", flush=True)

                # 🆕 先過濾過熱標的，再取前幾名 (過熱的剔除後，遞補下一名合格標的)
                overheat_cache = {}
                st1_targets = [t for t in st1_targets if not is_overheated_for_buy(t[0], t[1], overheat_cache)]
                st2_targets = [t for t in st2_targets if not is_overheated_for_buy(t[0], t[1], overheat_cache)]

                buy_targets = pick_buy_targets(st1_targets, st2_targets, weekday)

                for item in buy_targets:
                    code, name, price, st_type = item[0], item[1], item[2], item[3]
                    
                    cursor.execute('''
                        SELECT id FROM sim_trades 
                        WHERE stock_code = %s 
                        AND (
                            DATE_TRUNC('week', buy_date::date) = DATE_TRUNC('week', %s::date)
                            OR (sell_date IS NOT NULL AND DATE_TRUNC('week', sell_date::date) = DATE_TRUNC('week', %s::date))
                        );
                    ''', (code, today_str, today_str))
                    
                    if cursor.fetchone():
                        continue

                    cursor.execute('''
                        INSERT INTO sim_trades (stock_code, stock_name, strategy_type, buy_date, buy_price, status)
                        VALUES (%s, %s, %s, %s, %s, 'HOLD');
                    ''', (code, name, st_type, today_str, price))
                    print(f"🛒 [模擬買入成功] [{st_type}] {code} {name} | 掛單成交價: ${price:.2f}", flush=True)

        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"❌ [Sim Engine Error] {e}", flush=True)
    finally:
        conn.close()

if __name__ == "__main__":
    init_sim_db()
    process_simulation()
