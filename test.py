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


if __name__ == "__main__":
    unittest.main()
