"""
sim_portfolio 單元測試 (不連 FinMind / 資料庫 / Google Sheets)
執行方式: python -m unittest test -v
"""
import sys
import types
import unittest
from unittest import mock

import pandas as pd

# 本機沒裝雲端用的套件也能跑測試：缺什麼就塞一個空殼模組
for _name in ["requests", "psycopg2", "gspread", "google", "google.oauth2", "google.oauth2.service_account"]:
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = types.ModuleType(_name)
if not hasattr(sys.modules["google.oauth2.service_account"], "Credentials"):
    sys.modules["google.oauth2.service_account"].Credentials = object

import sim_portfolio as sp


# 與 cron_job.run_precalculation 產生的格式一致
SAMPLE_REPORT = "\n".join([
    "📊 【AI 精選雙策略雙軌選股報告】(2026/10/01)",
    "====================",
    "🌱 【策略一：底部止跌 + 法人合買翻多】",
    "💡 特性：空轉多拐點，低基期、獲利空間極大 (已排除策略二標的)",
    "--------------------",
    "🔹 2330 台積電 | 收: 1025.00 (+1.23%)\n    👉 得分:95分 | 🛡️站上月線 🚀帶量攻擊",
    "┈┈┈┈┈┈┈┈┈┈",
    "🔹 6230 雙鴻-KY | 收: 88.50 (-0.40%)\n    👉 得分:-10分 | ⚠️主力倒貨(-30分) ⚠️留長上影線(-30分)",
    "\n====================\n",
    "🔥 【策略二：洗盤結束 + 外資3倍/反轉暴買突破】",
    "💡 特性：OSC連3跌後翻紅 + 外資買超大於前日賣超/3倍買超 (當日漲幅<5.5%)",
    "--------------------",
    "🔹 2603 長榮 | 收: 210.50 (+2.10%)\n    👉 得分:120分 | ⚡洗盤結束起漲",
    "┈┈┈┈┈┈┈┈┈┈",
    "🔹 2317 鴻海 | 收: 180.00 (+1.50%)\n    👉 得分:105分 | 🤝土洋同買",
])


def make_df(closes, spread=0.5):
    return pd.DataFrame({
        "close": closes,
        "max": [c + spread for c in closes],
        "min": [c - spread for c in closes],
    })


class TestParseRecommendations(unittest.TestCase):
    def test_both_strategies_parsed(self):
        st1, st2 = sp.parse_recommendations(SAMPLE_REPORT)
        self.assertEqual([t[0] for t in st1], ["2330", "6230"])
        self.assertEqual([t[0] for t in st2], ["2603", "2317"])
        self.assertTrue(all(t[3] == "策略一" for t in st1))
        self.assertTrue(all(t[3] == "策略二" for t in st2))

    def test_description_line_does_not_switch_strategy(self):
        # 「(已排除策略二標的)」不能把策略一切成策略二
        st1, _ = sp.parse_recommendations(SAMPLE_REPORT)
        self.assertEqual(len(st1), 2)

    def test_score_read_from_next_line(self):
        st1, st2 = sp.parse_recommendations(SAMPLE_REPORT)
        self.assertEqual(st1[0][4], 95)
        self.assertEqual(st1[1][4], -10)  # 負分也要讀到，且不能被「(-30分)」標籤覆蓋
        self.assertEqual([t[4] for t in st2], [120, 105])

    def test_price_and_name(self):
        st1, st2 = sp.parse_recommendations(SAMPLE_REPORT)
        self.assertEqual(st1[0][1], "台積電")
        self.assertEqual(st1[0][2], 1025.0)
        self.assertEqual(st1[1][1], "雙鴻-KY")
        self.assertEqual(st2[0][2], 210.5)

    def test_empty_strategy(self):
        report = "\n".join([
            "🌱 【策略一：底部止跌】", "今日暫無符合條件之標的。",
            "🔥 【策略二：洗盤結束】", "🔹 2603 長榮 | 收: 210.50 (+2.10%)", "    👉 得分:120分 | x",
        ])
        st1, st2 = sp.parse_recommendations(report)
        self.assertEqual(st1, [])
        self.assertEqual(st2, [("2603", "長榮", 210.5, "策略二", 120)])

    def test_missing_score_defaults_to_zero(self):
        report = "🔥 【策略二：洗盤結束】\n🔹 2603 長榮 | 收: 210.50"
        _, st2 = sp.parse_recommendations(report)
        self.assertEqual(st2[0][4], 0)


class TestRSI(unittest.TestCase):
    def test_only_gains_is_100(self):
        rsi = sp.calc_rsi(pd.Series(range(1, 31), dtype=float))
        self.assertAlmostEqual(rsi.iloc[-1], 100.0)

    def test_only_losses_is_0(self):
        rsi = sp.calc_rsi(pd.Series(range(30, 0, -1), dtype=float))
        self.assertAlmostEqual(rsi.iloc[-1], 0.0)

    def test_warmup_is_nan(self):
        rsi = sp.calc_rsi(pd.Series(range(1, 31), dtype=float), period=14)
        self.assertTrue(rsi.iloc[:14].isna().all())
        self.assertFalse(pd.isna(rsi.iloc[14]))

    def test_alternating_is_near_50(self):
        closes = pd.Series([10 + (i % 2) for i in range(60)], dtype=float)
        self.assertAlmostEqual(sp.calc_rsi(closes).iloc[-1], 50.0, delta=5)

    def test_flat_is_nan(self):
        rsi = sp.calc_rsi(pd.Series([10.0] * 30))
        self.assertTrue(pd.isna(rsi.iloc[-1]))


class TestKD(unittest.TestCase):
    def test_hand_calculated(self):
        df = pd.DataFrame({"max": [10, 11, 12, 12], "min": [8, 9, 10, 9], "close": [9, 10, 12, 9]}, dtype=float)
        k, d = sp.calc_kd(df, n=3)
        self.assertTrue(pd.isna(k.iloc[0]) and pd.isna(k.iloc[1]))
        # RSV=100 → K=50*2/3+100/3, D=50*2/3+K/3
        self.assertAlmostEqual(k.iloc[2], 66.6667, places=3)
        self.assertAlmostEqual(d.iloc[2], 55.5556, places=3)
        # RSV=0
        self.assertAlmostEqual(k.iloc[3], 44.4444, places=3)
        self.assertAlmostEqual(d.iloc[3], 51.8519, places=3)

    def test_zero_range_is_nan(self):
        df = make_df([10.0] * 12, spread=0)
        k, _ = sp.calc_kd(df)
        self.assertTrue(k.isna().all())


class TestIndicatorStatus(unittest.TestCase):
    def test_insufficient_data(self):
        self.assertIsNone(sp.get_indicator_status(None))
        self.assertIsNone(sp.get_indicator_status(make_df([10.0 + i for i in range(10)])))

    def test_uptrend_values(self):
        st = sp.get_indicator_status(make_df([10.0 + i for i in range(40)]))
        self.assertAlmostEqual(st["rsi"], 100.0)
        self.assertGreater(st["k"], 90)
        self.assertFalse(st["kd_dead_cross"])
        # 最後收盤 49，MA5=47 → 乖離 4.255%
        self.assertAlmostEqual(st["bias5"], (49 / 47 - 1) * 100, places=6)

    def test_high_zone_dead_cross(self):
        closes = [10.0 + i for i in range(40)] + [44.0]  # 長紅後一根大跌
        st = sp.get_indicator_status(make_df(closes))
        self.assertTrue(st["kd_dead_cross"])
        self.assertLess(st["k"], st["d"])


class TestBuyFilter(unittest.TestCase):
    def check(self, status):
        with mock.patch.object(sp, "fetch_price_df", return_value="df"), \
             mock.patch.object(sp, "get_indicator_status", return_value=status):
            return sp.is_overheated_for_buy("2330", "台積電", {})

    def st(self, rsi=60, k=60, bias5=2.0):
        return {"rsi": rsi, "k": k, "d": 50, "kd_dead_cross": False, "bias5": bias5}

    def test_normal_passes(self):
        self.assertFalse(self.check(self.st()))

    def test_both_extreme_blocked(self):
        self.assertTrue(self.check(self.st(rsi=sp.RSI_BUY_MAX, k=sp.K_BUY_MAX)))

    def test_only_one_extreme_passes(self):
        self.assertFalse(self.check(self.st(rsi=95, k=60)))
        self.assertFalse(self.check(self.st(rsi=60, k=99)))

    def test_bias5_blocked(self):
        self.assertTrue(self.check(self.st(bias5=sp.BIAS5_BUY_MAX)))
        self.assertFalse(self.check(self.st(bias5=sp.BIAS5_BUY_MAX - 0.1)))

    def test_no_data_passes(self):
        self.assertFalse(self.check(None))

    def test_exception_passes(self):
        with mock.patch.object(sp, "fetch_price_df", side_effect=RuntimeError("boom")):
            self.assertFalse(sp.is_overheated_for_buy("2330", "台積電", {}))

    def test_cache_avoids_refetch(self):
        cache = {}
        with mock.patch.object(sp, "fetch_price_df", return_value="df") as f, \
             mock.patch.object(sp, "get_indicator_status", return_value=self.st()):
            sp.is_overheated_for_buy("2330", "台積電", cache)
            sp.is_overheated_for_buy("2330", "台積電", cache)
        self.assertEqual(f.call_count, 1)

    def test_filter_disabled(self):
        with mock.patch.object(sp, "USE_BUY_FILTER", False):
            self.assertFalse(self.check(self.st(rsi=99, k=99, bias5=20)))


class TestTradingRules(unittest.TestCase):
    def test_calendar_exit(self):
        self.assertIn("週四", sp.calendar_exit_reason(3, 0))
        self.assertEqual(sp.calendar_exit_reason(3, 3), "")      # 週四買的週四不賣
        self.assertIn("週四精選", sp.calendar_exit_reason(4, 3))
        self.assertIn("強制結算", sp.calendar_exit_reason(4, 1))
        self.assertEqual(sp.calendar_exit_reason(1, 0), "")

    def test_pick_mon_to_wed_takes_both_strategies(self):
        st1, st2 = sp.parse_recommendations(SAMPLE_REPORT)
        picks = sp.pick_buy_targets(st1, st2, 0, verbose=False)
        self.assertEqual([t[0] for t in picks], ["2330", "6230", "2603", "2317"])

    def test_pick_thursday(self):
        st1, st2 = sp.parse_recommendations(SAMPLE_REPORT)
        self.assertEqual([t[0] for t in sp.pick_buy_targets(st1, st2, 3, verbose=False)], ["2603"])
        low_st2 = [t[:4] + (90,) for t in st2]
        self.assertEqual([t[0] for t in sp.pick_buy_targets(st1, low_st2, 3, verbose=False)], ["2330"])
        self.assertEqual(sp.pick_buy_targets(st1, st2, 4, verbose=False), [])

    def test_min_score_and_top_n(self):
        st1, st2 = sp.parse_recommendations(SAMPLE_REPORT)
        with mock.patch.object(sp, "MIN_BUY_SCORE", 100), mock.patch.object(sp, "TOP_N_PER_STRATEGY", 1):
            self.assertEqual([t[0] for t in sp.pick_buy_targets(st1, st2, 1, verbose=False)], ["2603"])

    def test_macd_exit_only_profit(self):
        df = make_df([10.0 + i for i in range(40)])  # MACD 柱狀體縮小中
        self.assertTrue(sp.check_exit_signal(df, -1.0, 1, False)[0])
        with mock.patch.object(sp, "MACD_EXIT_ONLY_PROFIT", True):
            self.assertFalse(sp.check_exit_signal(df, -1.0, 1, False)[0])
            self.assertTrue(sp.check_exit_signal(df, 1.0, 1, False)[0])

    def test_market_ok(self):
        self.assertTrue(sp.market_ok(None))               # 未啟用
        with mock.patch.object(sp, "MARKET_MA", 5):
            self.assertTrue(sp.market_ok(make_df([10, 11, 12, 13, 14])))
            self.assertFalse(sp.market_ok(make_df([14, 13, 12, 11, 10])))
            self.assertTrue(sp.market_ok(make_df([10, 11])))  # 資料不足放行

    def test_exit_signal_stop_loss(self):
        df = make_df([10.0] * 40)
        self.assertEqual(sp.check_exit_signal(df, -5.0, 1, False), (True, "🚨 大跌觸發止損 (-5%)"))
        self.assertEqual(sp.check_exit_signal(df, 0.0, 1, False), (False, ""))

    def test_exit_rsi_lock_only_with_profit(self):
        df = make_df([10.0 + i for i in range(40)])  # RSI = 100
        sell, reason = sp.check_exit_signal(df, 3.0, 1, True)
        self.assertTrue(sell and "RSI" in reason)
        # 虧損時不啟動 RSI 鎖利 (這組資料 MACD 柱狀體在縮小，所以會改由 MACD 減弱出場)
        self.assertNotIn("RSI", sp.check_exit_signal(df, -1.0, 1, True)[1])


class TestBacktest(unittest.TestCase):
    """用合成價格驗證回測的進出場機制"""

    @classmethod
    def setUpClass(cls):
        import datetime
        import backtest
        cls.bt = backtest
        cls.dt = datetime
        # 找一個週一當報告日
        d = datetime.date(2026, 9, 7)
        cls.monday = d - datetime.timedelta(days=d.weekday())

    def prices(self, closes_after, flat=10.0):
        """報告日前 120 個交易日平盤，報告日 (含) 之後依序為 closes_after"""
        days = pd.bdate_range(end=self.monday, periods=120).date.tolist()
        after = pd.bdate_range(start=self.monday + self.dt.timedelta(days=1), periods=len(closes_after)).date.tolist()
        closes = [flat] * len(days) + list(closes_after)
        df = pd.DataFrame({"date": days + after, "close": closes,
                           "max": [c + 0.2 for c in closes], "min": [c - 0.2 for c in closes]})
        df["open"] = df["close"]
        return df

    def report(self, code="1111", strategy="一", score=80):
        return f"【策略{strategy}】\n🔹 {code} 測試 | 收: 10.00 (+0.00%)\n    👉 得分:{score}分 | x"

    def run_bt(self, reports, prices, **kw):
        cfg = dict(USE_BUY_FILTER=False, USE_EXIT_FILTER=False, MACD_WEAK_DAYS=1)
        cfg.update(kw)
        return self.bt.simulate(reports, prices, **cfg)

    def test_take_profit(self):
        trades, _ = self.run_bt({self.monday: self.report()}, {"1111": self.prices([10.6] * 10)},
                                TAKE_PROFIT_PCT=5.0)
        self.assertIn("停利", trades[0]["reason"])
        self.assertEqual(sp.TAKE_PROFIT_PCT, None)  # 回測結束後參數要還原

    def test_market_filter_blocks_buys(self):
        market = self.prices([9.0] * 10)            # 0050 在報告日前一路平盤，報告日當天跌破
        market.loc[market["date"] == self.monday, "close"] = 9.0
        trades, stats = self.run_bt({self.monday: self.report()}, {"1111": self.prices([10.0] * 10)},
                                    market=market, MARKET_MA=20)
        self.assertEqual(trades, [])
        self.assertEqual(stats["market_skip_days"], 1)

    def test_thursday_forced_exit(self):
        trades, _ = self.run_bt({self.monday: self.report()}, {"1111": self.prices([10.0] * 10)})
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["sell_date"], self.monday + self.dt.timedelta(days=3))
        self.assertAlmostEqual(t["ret"], 0.0)
        self.assertIn("週四", t["reason"])

    def test_stop_loss(self):
        trades, _ = self.run_bt({self.monday: self.report()}, {"1111": self.prices([9.4] + [9.4] * 9)})
        t = trades[0]
        self.assertEqual(t["sell_date"], self.monday + self.dt.timedelta(days=1))
        self.assertIn("止損", t["reason"])

    def test_same_week_not_bought_twice(self):
        tue = self.monday + self.dt.timedelta(days=1)
        reports = {self.monday: self.report(), tue: self.report()}
        trades, stats = self.run_bt(reports, {"1111": self.prices([10.0] * 10)})
        self.assertEqual(len(trades), 1)
        self.assertEqual(stats["dup_week"], 1)

    def test_next_open_entry(self):
        prices = self.prices([10.0] * 10)
        prices.loc[prices["date"] > self.monday, "open"] = 10.5
        trades, _ = self.run_bt({self.monday: self.report()}, {"1111": prices}, entry="next_open")
        self.assertEqual(trades[0]["buy_price"], 10.5)
        self.assertIn("週四", trades[0]["reason"])

    def test_summarize_drawdown(self):
        d = self.monday
        trades = [dict(sell_date=d, buy_date=d, ret=r) for r in (5.0, -3.0, -4.0, 2.0)]
        s = self.bt.summarize(trades + [dict(sell_date=None, buy_date=d, ret=None)])
        self.assertEqual(s["筆數"], 4)
        self.assertEqual(s["勝率%"], 50.0)
        self.assertEqual(s["總損益$"], 0)
        self.assertEqual(s["最大回撤$"], 7000)   # 5000 → -2000
        self.assertEqual(s["未平倉"], 1)


if __name__ == "__main__":
    unittest.main()
