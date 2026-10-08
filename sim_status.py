"""
模擬倉狀態檢查 (只讀，不寫入資料庫、不推播)
- LATEST 推薦報告中每一檔的買進檢查結果 (過熱 / 進場品質過濾)
- 目前持股的即時損益與出場訊號
- 本週已平倉交易
執行前需要環境變數：DATABASE_URL、FINMIND_API_TOKEN
    python sim_status.py
"""
import datetime

import sim_portfolio as sp


def main():
    conn = sp.get_db_connection()
    if not conn:
        raise SystemExit("❌ 無法連線資料庫，請先設定 DATABASE_URL")
    now = sp.now_tw()
    monday = (now - datetime.timedelta(days=now.weekday())).strftime('%Y-%m-%d')
    try:
        cur = conn.cursor()
        print(f"🕗 台灣時間 {now:%Y-%m-%d %H:%M} (週{now.weekday() + 1})\n")

        # 1. LATEST 報告與買進檢查
        cur.execute("SELECT content FROM history WHERE date = 'LATEST';")
        row = cur.fetchone()
        if not row:
            print("⚠️ 找不到 LATEST 報告")
        else:
            print("📋 LATEST 報告:", row[0].split('\n')[0])
            st1, st2 = sp.parse_recommendations(row[0])
            passed1, passed2 = [], []
            for targets, passed in ((st1, passed1), (st2, passed2)):
                for t in targets:
                    df = sp.fetch_price_df(t[0])
                    last = df['date'].iloc[-1] if df is not None and len(df) else "無資料"
                    reasons, info = sp.buy_block_reasons(df) if df is not None and len(df) >= 2 else ([], "")
                    adx = sp.calc_adx(df)
                    mark = "🧊 不買" if reasons else "✅ 可買"
                    if not reasons:
                        passed.append(t)
                    print(f"  {mark} [{t[3]}] {t[0]} {t[1]} {t[4]}分 | K棒日 {last} | ADX "
                          f"{adx if adx is None else round(adx, 1)} | {info} {('| ' + ' / '.join(reasons)) if reasons else ''}")
            wd = now.weekday()
            picks = sp.pick_buy_targets(passed1, passed2, wd, verbose=False) if wd <= 3 else []
            print(f"👉 今天 (週{wd + 1}) 依規則會買: {[f'{t[0]} {t[1]}' for t in picks] or '無'}\n")

        # 2. 持股
        cur.execute("SELECT stock_code, stock_name, strategy_type, buy_date, buy_price FROM sim_trades "
                    "WHERE status = 'HOLD' ORDER BY buy_date;")
        holds = cur.fetchall()
        print(f"💼 目前持股 {len(holds)} 檔")
        for code, name, st_type, buy_date, buy_price in holds:
            df = sp.fetch_price_df(code)
            if df is None or len(df) < 2:
                print(f"  {code} {name} 價格資料不足")
                continue
            curr = float(df['close'].iloc[-1])
            ret = (curr - float(buy_price)) / float(buy_price) * 100
            sell, reason = sp.check_exit_signal(df, ret)
            if not sell:
                bd = datetime.datetime.strptime(buy_date, '%Y-%m-%d')
                reason = sp.calendar_exit_reason(now.weekday(), bd.weekday(),
                                                 bd.isocalendar()[:2] != now.isocalendar()[:2])
            print(f"  {code} {name} [{st_type}] 買 {buy_date} @{float(buy_price):.2f} → {curr:.2f} "
                  f"({ret:+.2f}%) | 下次執行: {reason or '續抱'}")

        cur.execute("SELECT stock_code, stock_name, strategy_type, buy_date FROM sim_trades "
                    "WHERE status = 'PENDING' ORDER BY id;")
        pendings = cur.fetchall()
        print(f"\n📝 待成交掛單 {len(pendings)} 檔 (下次執行以推薦隔日開盤價成交)")
        for code, name, st_type, buy_date in pendings:
            print(f"  {code} {name} [{st_type}] 推薦日 {buy_date}")

        # 3. 本週平倉
        cur.execute("SELECT stock_code, stock_name, buy_date, sell_date, return_rate, exit_reason FROM sim_trades "
                    "WHERE status = 'CLOSED' AND sell_date >= %s ORDER BY sell_date, id;", (monday,))
        closed = cur.fetchall()
        print(f"\n📈 本週已平倉 {len(closed)} 筆")
        for code, name, bd, sd, ret, reason in closed:
            print(f"  {code} {name} {bd} → {sd} {float(ret):+.2f}% | {reason}")
        if closed:
            rets = [float(c[4]) for c in closed]
            print(f"  勝率 {sum(r > 0 for r in rets) / len(rets) * 100:.0f}% | 平均 {sum(rets) / len(rets):+.2f}%")
        cur.close()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
