"""
模擬倉回測：用資料庫 history 表「每天」的推薦報告 (key = YYYYMMDD) 重播 sim_portfolio 的買賣規則，
比較「RSI/KD 過濾開/關」與 MACD_WEAK_DAYS = 1 / 2 的績效。

買賣規則直接呼叫 sim_portfolio 的共用函式 (parse_recommendations / pick_buy_targets /
check_exit_signal / calendar_exit_reason / buy_overheat_reasons)，與實盤一致。
不會寫入資料庫，也不會同步 Google Sheets。

執行前需要環境變數：DATABASE_URL、FINMIND_API_TOKEN
    pip install -r requirements.txt
    python backtest.py
    python backtest.py --start 2026-07-01 --end 2026-09-30 --entry next_open

價格資料會快取在 .backtest_cache/，重跑不會重複消耗 FinMind 額度。
"""
import argparse
import datetime
import glob
import os
import time

import pandas as pd
import requests

import sim_portfolio as sp

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".backtest_cache")
TRADE_AMOUNT = 100000  # 與週報、戰報一致：每筆固定 10 萬

# 每組設定 = 對 sim_portfolio 參數的覆寫。除「原版」與「全部組合」外，一次只改一個參數 (以目前設定為基準)，
# 才看得出是哪一項真的有效，也比較不會過度擬合。
# BASE = 2026-10 調整前的舊設定，保留當比較基準
BASE = dict(USE_BUY_FILTER=True, USE_EXIT_FILTER=True, MACD_WEAK_DAYS=1, STOP_LOSS_PCT=-5.0,
            TAKE_PROFIT_PCT=None, MACD_EXIT_ONLY_PROFIT=False, MARKET_MA=None,
            MIN_BUY_SCORE=None, TOP_N_PER_STRATEGY=5, STRATEGY2_TOP_N=None,
            MAX_DAY_PCT=None, MIN_ADX=None, MAX_RET20=None, MON_WED_BUY_DAYS=(0, 1, 2))
COMBO_A = dict(BASE, MACD_EXIT_ONLY_PROFIT=True, TAKE_PROFIT_PCT=5.0)
CONFIGS = [
    ("原版 (無 RSI/KD)", dict(BASE, USE_BUY_FILTER=False, USE_EXIT_FILTER=False)),
    ("舊設定 (基準)", dict(BASE)),
    ("★ 現行 sim_portfolio 設定", {}),
    ("+ MACD 連 2 天才賣", dict(BASE, MACD_WEAK_DAYS=2)),
    ("+ MACD 只在獲利時賣", dict(BASE, MACD_EXIT_ONLY_PROFIT=True)),
    ("+ 停利 +5%", dict(BASE, TAKE_PROFIT_PCT=5.0)),
    ("+ 停利 +8%", dict(BASE, TAKE_PROFIT_PCT=8.0)),
    ("+ 停損 -4%", dict(BASE, STOP_LOSS_PCT=-4.0)),
    ("+ 大盤 0050 > MA10", dict(BASE, MARKET_MA=10)),
    ("+ 大盤 0050 > MA20", dict(BASE, MARKET_MA=20)),
    ("+ 最低 70 分", dict(BASE, MIN_BUY_SCORE=70)),
    ("+ 每策略前 3 名", dict(BASE, TOP_N_PER_STRATEGY=3)),
    ("+ 週一~三策略二只買 2 檔", dict(BASE, STRATEGY2_TOP_N=2)),
    ("組合A: MACD只獲利賣 + 停利5%", COMBO_A),
    ("組合B: A + 前3名", dict(COMBO_A, TOP_N_PER_STRATEGY=3)),
    ("組合C: A + 策略二只買2檔", dict(COMBO_A, STRATEGY2_TOP_N=2)),
    ("組合D: A + 前3名 + 策略二2檔", dict(COMBO_A, TOP_N_PER_STRATEGY=3, STRATEGY2_TOP_N=2)),
    ("組合E: A + MACD連2天", dict(COMBO_A, MACD_WEAK_DAYS=2)),
    ("組合F: 停利5% + 前3名", dict(BASE, TAKE_PROFIT_PCT=5.0, TOP_N_PER_STRATEGY=3)),
]
MARKET_CODE = "0050"


class RateLimited(Exception):
    pass


# =============================================================================
# 資料載入
# =============================================================================
REPORTS_CACHE = os.path.join(CACHE_DIR, "reports.pkl")


def load_reports(start=None, end=None, offline=False):
    """讀取 history 表中以 YYYYMMDD 為 key 的每日報告，回傳 {date: content}。
    每次從資料庫讀取後會存一份到快取；offline=True 時直接讀快取，不需 DATABASE_URL"""
    if offline:
        if not os.path.exists(REPORTS_CACHE):
            raise SystemExit("❌ 沒有報告快取，請先在有 DATABASE_URL 的環境跑一次")
        rows = pd.read_pickle(REPORTS_CACHE)
    else:
        conn = sp.get_db_connection()
        if not conn:
            raise SystemExit("❌ 無法連線資料庫，請先設定 DATABASE_URL (或加 --offline 使用快取)")
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT date, content FROM history WHERE date ~ '^[0-9]{8}$' ORDER BY date;")
            rows = cursor.fetchall()
            cursor.close()
        finally:
            conn.close()
        os.makedirs(CACHE_DIR, exist_ok=True)
        pd.to_pickle(rows, REPORTS_CACHE)

    reports = {}
    for key, content in rows:
        d = datetime.datetime.strptime(key, "%Y%m%d").date()
        if (start and d < start) or (end and d > end):
            continue
        reports[d] = content
    return reports


def to_price_df(data):
    df = pd.DataFrame(data)
    if df.empty:
        return df
    for col in ['open', 'max', 'min', 'close']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.dropna(subset=['open', 'close', 'max', 'min'])
    df = df[df['close'] > 0].copy()
    df['date'] = pd.to_datetime(df['date']).dt.date
    return df.sort_values('date').reset_index(drop=True)


def fetch_prices(code, start, end, offline=False):
    """抓 [start, end] 日 K (含快取)。遇到 FinMind 額度用完丟出 RateLimited"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{code}_{start}_{end}.pkl")
    if os.path.exists(path):
        return pd.read_pickle(path)
    if offline:
        # 離線：沿用同一檔最近一次下載的快取 (結束日可能不同)
        cached = sorted(glob.glob(os.path.join(CACHE_DIR, f"{code}_{start}_*.pkl")))
        return pd.read_pickle(cached[-1]) if cached else None

    params = {"dataset": "TaiwanStockPrice", "data_id": code,
              "start_date": str(start), "end_date": str(end)}
    if sp.FINMIND_TOKEN:
        params["token"] = sp.FINMIND_TOKEN

    for attempt in range(3):
        try:
            res = requests.get(FINMIND_URL, params=params, timeout=15)
            if res.status_code == 402:
                raise RateLimited(res.json().get("msg", "FinMind 額度已用完"))
            if res.status_code == 200:
                df = to_price_df(res.json().get("data") or [])
                df.to_pickle(path)
                return df
            print(f"⚠️ {code} HTTP {res.status_code}，重試中...", flush=True)
        except RateLimited:
            raise
        except Exception as e:
            print(f"⚠️ {code} 抓價失敗 ({e})，重試中...", flush=True)
        time.sleep(3 * (attempt + 1))
    return None


def collect_codes(reports):
    codes = set()
    for content in reports.values():
        st1, st2 = sp.parse_recommendations(content)
        codes.update(t[0] for t in st1 + st2)
    return sorted(codes)


# =============================================================================
# 模擬
# =============================================================================
def window(df, day):
    """模擬實盤當天看得到的資料：最近 PRICE_LOOKBACK_DAYS 天、且不含未來"""
    if df is None or df.empty:
        return None
    lo = day - datetime.timedelta(days=sp.PRICE_LOOKBACK_DAYS)
    w = df[(df['date'] >= lo) & (df['date'] <= day)]
    return w.reset_index(drop=True) if len(w) >= 2 else None


def week_of(d):
    return d.isocalendar()[:2]


def simulate(reports, prices, entry="report", market=None, **overrides):
    """
    逐日重播：每天先跑出場、再依當天報告買進 (與 process_simulation 順序相同)。
    overrides  暫時覆寫 sim_portfolio 參數 (例如 MACD_WEAK_DAYS=2)，結束後還原
    market     0050 日 K，大盤濾網用
    entry="report"    用報告上的收盤價買 (與實盤相同)
    entry="next_open" 用隔一個交易日開盤價買 (較貼近真實可成交價)
    回傳 (trades, stats)；trades 中 sell_date 為 None 代表資料結束時仍持有。
    """
    saved = {k: getattr(sp, k) for k in overrides}
    try:
        for k, v in overrides.items():
            setattr(sp, k, v)
        return _simulate(reports, prices, entry, market)
    finally:
        for k, v in saved.items():
            setattr(sp, k, v)


def _simulate(reports, prices, entry, market):
    trades, open_pos = [], []
    stats = {"filtered": 0, "no_price": 0, "no_next_open": 0, "dup_week": 0, "market_skip_days": 0}
    if not reports:
        return trades, stats

    last_report = max(reports)
    last_bar = max((df['date'].iloc[-1] for df in prices.values() if df is not None and not df.empty),
                   default=last_report)
    day = min(reports)

    def blocked(code, d):
        w = window(prices.get(code), d)
        return bool(w is not None and sp.buy_block_reasons(w)[0])  # 資料不足放行，同實盤

    while day <= max(last_report, last_bar):
        if day > last_report and not open_pos:
            break

        # A. 出場
        for pos in list(open_pos):
            if day <= pos['entry_day']:
                continue
            df = prices[pos['code']]
            w = window(df, day)
            if w is None:
                continue
            curr = float(w['close'].iloc[-1])
            ret = (curr - pos['buy_price']) / pos['buy_price'] * 100
            should_sell, reason = False, ""
            if w['date'].iloc[-1] == day:  # 有新 K 棒才會出現新的指標訊號
                should_sell, reason = sp.check_exit_signal(w, ret)
            if not should_sell:
                reason = sp.calendar_exit_reason(day.weekday(), pos['buy_date'].weekday())
                should_sell = bool(reason)
            if should_sell:
                pos.update(sell_date=day, sell_price=curr, ret=ret, reason=reason)
                open_pos.remove(pos)

        # B. 進場 (週一 ~ 週四)
        if day in reports and day.weekday() <= 3 and not sp.market_ok(window(market, day)):
            stats["market_skip_days"] += 1
        elif day in reports and day.weekday() <= 3:
            st1, st2 = sp.parse_recommendations(reports[day])
            before = len(st1) + len(st2)
            st1 = [t for t in st1 if not blocked(t[0], day)]
            st2 = [t for t in st2 if not blocked(t[0], day)]
            stats["filtered"] += before - len(st1) - len(st2)

            for code, name, price, strategy, score in sp.pick_buy_targets(st1, st2, day.weekday(), verbose=False):
                wk = week_of(day)
                if any(t['code'] == code and (week_of(t['buy_date']) == wk or
                                              (t['sell_date'] and week_of(t['sell_date']) == wk))
                       for t in trades):
                    stats["dup_week"] += 1
                    continue
                df = prices.get(code)
                if df is None or df.empty:
                    stats["no_price"] += 1
                    continue
                if entry == "next_open":
                    nxt = df[df['date'] > day]
                    if nxt.empty:
                        stats["no_next_open"] += 1
                        continue
                    buy_price, entry_day = float(nxt['open'].iloc[0]), nxt['date'].iloc[0]
                    entry_day = entry_day - datetime.timedelta(days=1)  # 進場當天收盤即開始檢查出場
                else:
                    buy_price, entry_day = float(price), day
                pos = dict(code=code, name=name, strategy=strategy, score=score, buy_date=day,
                           entry_day=entry_day, buy_price=buy_price,
                           sell_date=None, sell_price=None, ret=None, reason="")
                trades.append(pos)
                open_pos.append(pos)

        day += datetime.timedelta(days=1)

    return trades, stats


# =============================================================================
# 統計
# =============================================================================
def exit_category(reason):
    for key, label in [("止損", "止損"), ("停利", "停利"), ("RSI", "RSI鎖利"), ("KD", "KD鎖利"),
                       ("MACD", "MACD減弱"), ("📅", "日期出場")]:
        if key in reason:
            return label
    return "其他"


def summarize(trades, split=None):
    """split：把期間切成前後兩半各算勝率，兩半都比基準好才算穩定 (避免只是剛好吃到某段行情)"""
    closed = sorted([t for t in trades if t['sell_date']], key=lambda t: (t['sell_date'], t['buy_date']))
    rets = [t['ret'] for t in closed]
    n = len(rets)
    if n == 0:
        return {"筆數": 0}

    halves = {}
    if split:
        for name, part in (("前半", [t['ret'] for t in closed if t['buy_date'] < split]),
                           ("後半", [t['ret'] for t in closed if t['buy_date'] >= split])):
            halves[f"{name}勝率%"] = round(sum(r > 0 for r in part) / len(part) * 100, 1) if part else None
            halves[f"{name}每筆%"] = round(sum(part) / len(part), 2) if part else None
    wins = [r for r in rets if r > 0]
    losses = [-r for r in rets if r <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    cum, peak, mdd = 0.0, 0.0, 0.0
    for r in rets:
        cum += r / 100 * TRADE_AMOUNT
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)

    return {
        "筆數": n,
        "勝率%": round(len(wins) / n * 100, 1),
        "平均獲利%": round(avg_win, 2),
        "平均虧損%": round(avg_loss, 2),
        "風報比": round(avg_win / avg_loss, 2) if avg_loss > 0 else None,
        "每筆期望%": round(sum(rets) / n, 2),
        "總損益$": int(cum),
        "最大回撤$": int(mdd),
        "未平倉": len(trades) - n,
        **halves,
    }


def breakdown(trades, key):
    rows = {}
    for t in trades:
        if t['sell_date']:
            rows.setdefault(key(t), []).append(t['ret'])
    return {k: f"{len(v)}筆 勝率{sum(r > 0 for r in v) / len(v) * 100:.0f}% 均{sum(v) / len(v):+.2f}%"
            for k, v in sorted(rows.items())}


def main():
    ap = argparse.ArgumentParser(description="模擬倉回測")
    ap.add_argument("--start", type=lambda s: datetime.date.fromisoformat(s), help="起始報告日 YYYY-MM-DD")
    ap.add_argument("--end", type=lambda s: datetime.date.fromisoformat(s), help="結束報告日 YYYY-MM-DD")
    ap.add_argument("--entry", choices=["report", "next_open"], default="report",
                    help="report=報告收盤價買 (同實盤)；next_open=隔日開盤價買")
    ap.add_argument("--csv", default="backtest_trades.csv", help="逐筆交易輸出檔")
    ap.add_argument("--offline", action="store_true", help="使用上次快取的報告與價格，不連資料庫")
    args = ap.parse_args()

    reports = load_reports(args.start, args.end, args.offline)
    if not reports:
        raise SystemExit("❌ 找不到任何 YYYYMMDD 格式的歷史報告")
    print(f"📚 報告 {len(reports)} 份：{min(reports)} ~ {max(reports)}", flush=True)

    codes = collect_codes(reports)
    price_start = min(reports) - datetime.timedelta(days=sp.PRICE_LOOKBACK_DAYS + 10)
    price_end = min(max(reports) + datetime.timedelta(days=14), sp.now_tw().date())
    print(f"📈 需要 {len(codes)} 檔股票價格 ({price_start} ~ {price_end})", flush=True)

    prices = {}
    try:
        for i, code in enumerate(codes, 1):
            cached = os.path.exists(os.path.join(CACHE_DIR, f"{code}_{price_start}_{price_end}.pkl"))
            prices[code] = fetch_prices(code, price_start, price_end, args.offline)
            if not cached:
                print(f"  [{i}/{len(codes)}] {code} 下載完成", flush=True)
                time.sleep(0.3)
    except RateLimited as e:
        raise SystemExit(f"⛔ FinMind 額度用完：{e}\n已下載的價格都已快取，等額度恢復後重跑即可接續。")

    bench = fetch_prices(MARKET_CODE, price_start, price_end, args.offline)
    report_days = sorted(reports)
    split = report_days[len(report_days) // 2]
    results, all_rows = [], []
    for label, cfg in CONFIGS:
        trades, stats = simulate(reports, prices, entry=args.entry, market=bench, **cfg)
        s = summarize(trades, split)
        s["過熱擋掉"] = stats["filtered"]
        s["大盤停買天"] = stats["market_skip_days"]
        results.append((label, s, trades))
        for t in trades:
            all_rows.append(dict(設定=label, **t))

    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.width", 200)
    print("\n" + "=" * 80)
    print(f"📊 回測結果 (進場價: {'報告收盤價' if args.entry == 'report' else '隔日開盤價'}，每筆 {TRADE_AMOUNT:,} 元)")
    print("=" * 80)
    print(pd.DataFrame({label: s for label, s, _ in results}).T.to_string())
    print(f"(前半/後半以 {split} 切分)")

    if bench is not None and not bench.empty:
        b = bench[(bench['date'] >= min(reports)) & (bench['date'] <= max(reports))]
        if len(b) >= 2:
            print(f"\n🏦 0050 同期 (買進持有): {(b['close'].iloc[-1] / b['close'].iloc[0] - 1) * 100:+.2f}%")

    for label, _, trades in results:
        print(f"\n▶ {label}")
        print("   依策略:", breakdown(trades, lambda t: t['strategy']))
        print("   依出場:", breakdown(trades, lambda t: exit_category(t['reason'])))

    if all_rows:
        pd.DataFrame(all_rows).to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"\n💾 逐筆交易已輸出: {args.csv}")


if __name__ == "__main__":
    main()
