from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.xau_scalp_bot import (
    _manage_martingale_step,
    _martingale_triggered,
    _protect_positions,
    _scaled_initial_sl_distance,
    _scaled_tp_distance,
    _strategy_comment,
    _update_daily_profit_lock,
    _xau_scalp_legs,
    _xau_scalp_target_r_plan,
)


class XauScalpProtectionTests(TestCase):
    def test_reward_plan_builds_independent_targets_for_buy(self):
        env = {
            "XAU_SCALP_LEG_COUNT": "3",
            "XAU_SCALP_TARGET_R_PLAN": "1,2,3.2",
        }
        signal = {"side": "buy", "entry": 4000.0, "sl": 3995.0, "tp1": 4005.0, "tp2": 4010.0}
        with patch.dict("os.environ", env, clear=False):
            self.assertEqual(_xau_scalp_target_r_plan(), [1.0, 2.0, 3.2])
            self.assertEqual(
                _xau_scalp_legs(signal),
                [("r1_1", 4005.0), ("r2_2", 4010.0), ("r3_3.2", 4016.0)],
            )

    def test_reward_plan_builds_independent_targets_for_sell(self):
        env = {
            "XAU_SCALP_LEG_COUNT": "2",
            "XAU_SCALP_TARGET_R_PLAN": "1.5,4",
        }
        signal = {"side": "sell", "entry": 4000.0, "sl": 4004.0, "tp1": 3998.0, "tp2": 3996.0}
        with patch.dict("os.environ", env, clear=False):
            self.assertEqual(_xau_scalp_legs(signal), [("r1_1.5", 3994.0), ("r2_4", 3984.0)])

    def test_initial_sl_multiplier_changes_requested_distance(self):
        with patch.dict("os.environ", {"XAU_SCALP_SL_DISTANCE_MULTIPLIER": "1.5"}, clear=False):
            distance, multiplier = _scaled_initial_sl_distance(4.0)

        self.assertEqual(distance, 6.0)
        self.assertEqual(multiplier, 1.5)

    def test_initial_sl_multiplier_is_safely_bounded(self):
        with patch.dict("os.environ", {"XAU_SCALP_SL_DISTANCE_MULTIPLIER": "20"}, clear=False):
            distance, multiplier = _scaled_initial_sl_distance(4.0)

        self.assertEqual(distance, 12.0)
        self.assertEqual(multiplier, 3.0)

    def test_tp_multiplier_shortens_requested_distance(self):
        with patch.dict("os.environ", {"XAU_SCALP_TP_DISTANCE_MULTIPLIER": "0.85"}, clear=False):
            distance, multiplier = _scaled_tp_distance(4.0)

        self.assertEqual(distance, 3.4)
        self.assertEqual(multiplier, 0.85)

    def test_tp1_leg_is_protected_after_be_trigger(self):
        position = SimpleNamespace(
            ticket=123,
            type=0,
            price_open=4000.0,
            price_current=4001.1,
            sl=3997.0,
            tp=4001.5,
        )
        cfg = SimpleNamespace(
            xau_scalp_magic=994242,
            xau_scalp_be_trigger_usd=1.0,
            xau_scalp_be_buffer_usd=0.25,
        )
        meta = {
            "123": {
                "leg": "tp1",
                "tp1": 4001.5,
                "min_hold_until_utc": "",
            }
        }
        result = SimpleNamespace(retcode=10009)

        with (
            patch("app.xau_scalp_bot.positions_by_magic", return_value=[position]),
            patch("app.xau_scalp_bot._backfill_position_meta", return_value=False),
            patch("app.xau_scalp_bot._hold_remaining_seconds", return_value=0.0),
            patch("app.xau_scalp_bot._trailing_stop_candidate", return_value=None),
            patch("app.xau_scalp_bot.modify_position", return_value=result) as modify,
            patch("app.xau_scalp_bot._append_jsonl"),
        ):
            changed = _protect_positions("XAUUSD+", cfg, meta, Path("events.jsonl"))

        self.assertTrue(changed)
        modify.assert_called_once_with(position, 4000.25, 4001.5)
        self.assertTrue(meta["123"]["be_done"])

    def test_martingale_trigger_is_direction_aware(self):
        self.assertTrue(_martingale_triggered("buy", 4000.0, 3998.5, 1.5))
        self.assertFalse(_martingale_triggered("buy", 4000.0, 3998.51, 1.5))
        self.assertTrue(_martingale_triggered("sell", 4000.0, 4001.5, 1.5))
        self.assertFalse(_martingale_triggered("sell", 4000.0, 4001.49, 1.5))

    def test_martingale_step_cannot_repeat_for_same_signal(self):
        runtime = {
            "last_opened_signal_bar_utc": "2026-07-20T12:00:00+00:00",
            "martingale_armed_signal_bar_utc": "2026-07-20T12:00:00+00:00",
            "martingale_step_signal_bar_utc": "2026-07-20T12:00:00+00:00",
        }
        cfg = SimpleNamespace(xau_scalp_magic=994242)
        with patch.dict("os.environ", {"XAU_SCALP_MARTINGALE_ENABLED": "true"}):
            changed = _manage_martingale_step(
                "XAUUSD+",
                cfg,
                {},
                runtime,
                Path("runtime.json"),
                Path("events.jsonl"),
            )
        self.assertFalse(changed)

    def test_accuracy_setup_tag_is_used_in_mt5_comment(self):
        self.assertEqual(_strategy_comment("tp1", {"setup_tag": "SC-LiqSweep"}), "SC-LiqSweep:tp1")

    def test_daily_profit_lock_uses_day_start_balance_percentages(self):
        runtime = {}
        env = {
            "XAU_SCALP_DAILY_PROFIT_LOCK_ENABLED": "true",
            "XAU_SCALP_DAILY_PROFIT_LOCK_TRIGGER_USD": "0",
            "XAU_SCALP_DAILY_PROFIT_LOCK_TRIGGER_BALANCE_PCT": "1.0",
            "XAU_SCALP_DAILY_PROFIT_LOCK_GIVEBACK_USD": "0",
            "XAU_SCALP_DAILY_PROFIT_LOCK_GIVEBACK_PCT": "0",
            "XAU_SCALP_DAILY_PROFIT_LOCK_GIVEBACK_BALANCE_PCT": "0.25",
            "XAU_SCALP_DAILY_PROFIT_STOP_USD": "0",
        }
        with patch.dict("os.environ", env, clear=False), patch("app.xau_scalp_bot._append_jsonl") as append:
            locked, status = _update_daily_profit_lock(runtime, "2026-07-22", 100.0, 10_000.0, Path("events.jsonl"))
            self.assertFalse(locked)
            self.assertEqual(status["trigger_usd"], 100.0)
            self.assertEqual(status["giveback_usd"], 25.0)

            locked, status = _update_daily_profit_lock(runtime, "2026-07-22", 74.0, 10_000.0, Path("events.jsonl"))

        self.assertTrue(locked)
        self.assertEqual(status["reason"], "daily_profit_giveback_lock")
        append.assert_called_once()
