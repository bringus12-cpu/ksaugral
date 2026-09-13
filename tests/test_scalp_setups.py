from unittest import TestCase
from unittest.mock import patch

from app.scalp_setups import (
    ACCURACY_SETUP_TAGS,
    EXTRA_SETUP_MODES,
    INDICATOR_SETUP_PROFILES,
    SMC_SETUP_TAGS,
    evaluate_extra_setup,
)


class AccuracyScalpSetupTests(TestCase):
    def test_accuracy_modes_and_dispatcher_are_registered(self):
        self.assertTrue(set(ACCURACY_SETUP_TAGS).issubset(EXTRA_SETUP_MODES))
        self.assertIn("accuracy_ensemble", EXTRA_SETUP_MODES)
        self.assertIn("accuracy_two_bar_continue", EXTRA_SETUP_MODES)
        self.assertIn("accuracy_ema_rejection", EXTRA_SETUP_MODES)
        self.assertIn("accuracy_adx_breakout", EXTRA_SETUP_MODES)
        self.assertIn("accuracy_multi_trigger", EXTRA_SETUP_MODES)
        self.assertIn("smc_liquidity_sweep", EXTRA_SETUP_MODES)
        self.assertIn("smc_bos_retest", EXTRA_SETUP_MODES)
        self.assertIn("smc_fvg_retest", EXTRA_SETUP_MODES)
        self.assertIn("smc_order_block", EXTRA_SETUP_MODES)
        self.assertIn("smc_mss_retest", EXTRA_SETUP_MODES)
        self.assertIn("smc_breaker_retest", EXTRA_SETUP_MODES)
        self.assertIn("smc_discount_reclaim", EXTRA_SETUP_MODES)
        self.assertIn("smc_displacement", EXTRA_SETUP_MODES)
        self.assertIn("smc_ensemble", EXTRA_SETUP_MODES)
        self.assertIn("smc_selective_ensemble", EXTRA_SETUP_MODES)
        self.assertIn("smc_adaptive_ensemble", EXTRA_SETUP_MODES)
        self.assertIn("gold_multi_strategy", EXTRA_SETUP_MODES)
        self.assertIn("gold_multi_strategy_v2", EXTRA_SETUP_MODES)
        self.assertIn("bb_keltner_macd_squeeze", EXTRA_SETUP_MODES)

    def test_research_indicator_profiles_are_registered_and_have_short_tags(self):
        self.assertEqual(len(INDICATOR_SETUP_PROFILES), 6)
        self.assertTrue(set(INDICATOR_SETUP_PROFILES).issubset(EXTRA_SETUP_MODES))
        self.assertTrue(all(len(str(profile["tag"])) <= 16 for profile in INDICATOR_SETUP_PROFILES.values()))
        self.assertTrue(all(float(profile["sl_atr"]) > 0 for profile in INDICATOR_SETUP_PROFILES.values()))
        self.assertTrue(all(float(profile["tp_r"]) > 0 for profile in INDICATOR_SETUP_PROFILES.values()))

    def test_comment_tags_fit_mt5_limit(self):
        self.assertTrue(all(len(tag) <= 16 for tag in ACCURACY_SETUP_TAGS.values()))
        self.assertTrue(all(len(tag) <= 16 for tag in SMC_SETUP_TAGS.values()))

    def test_bb_macd_exit_profile_honors_research_overrides(self):
        last1 = {
            "close": 101.0, "open": 100.0, "high": 101.2, "low": 99.8,
            "ema20": 100.0, "rsi14": 55.0, "tick_volume": 100.0, "volume_ma20": 100.0,
        }
        prev1 = {
            "close": 100.0, "open": 99.8, "high": 100.2, "low": 99.6,
            "ema20": 99.9, "rsi14": 52.0, "tick_volume": 100.0, "volume_ma20": 100.0,
        }
        last5 = {
            "close": 101.0, "open": 100.0, "high": 101.2, "low": 99.8,
            "ema20": 100.5, "ema50": 100.0, "tick_volume": 120.0, "volume_ma20": 100.0,
            "macd_hist": 0.3, "bb_upper": 100.8, "bb_lower": 99.2,
            "bb_width": 1.6, "bb_width_median": 2.0, "close_location": 0.8,
        }
        prev5 = {
            "close": 100.0, "open": 99.8, "high": 100.2, "low": 99.6,
            "ema20": 100.1, "ema50": 100.0, "tick_volume": 100.0, "volume_ma20": 100.0,
            "macd_hist": 0.1, "bb_upper": 100.2, "bb_lower": 99.0,
            "bb_width": 1.5, "bb_width_median": 2.0, "close_location": 0.7,
        }
        last15 = {"ema20": 100.5, "ema50": 100.0}
        overrides = {
            "SC_BBMACD_EXIT_SL_ATR": "2.5",
            "SC_BBMACD_EXIT_TP_R": "3.2",
            "SC_BBMACD_EXIT_BE_R": "0.75",
            "SC_BBMACD_EXIT_BE_BUFFER": "0.1",
            "SC_BBMACD_EXIT_HOLD_MINUTES": "480",
        }
        with patch.dict("os.environ", overrides, clear=False):
            buy, sell, diagnostics = evaluate_extra_setup(
                "bb_macd_breakout", last1, prev1, last5, prev5, last15, 1.0, 2.0, 0.5
            )
        self.assertTrue(buy)
        self.assertFalse(sell)
        self.assertEqual(
            diagnostics["indicator_exit_profile"],
            {
                "tag": "SC-BBMACD-BRK", "timeframe": "M5", "sl_atr": 2.5,
                "tp_r": 3.2, "be_r": 0.75, "be_buffer": 0.1,
                "hold_minutes": 480.0, "cooldown_seconds": 1800,
            },
        )
