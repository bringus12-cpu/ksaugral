import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from app.telegram_signal_bot import (
    _account_risk_base,
    _channel_lot_override,
    _channel_asset_allowed,
    _channel_update_target_message_ids,
    _defer_ghp_currency_provider_be,
    _channel_strategy,
    _comment_source_tag,
    _dynamic_lot_from_balance,
    _ensure_asset_symbol,
    _is_hold_message,
    _is_dany_signals_source,
    _is_phoenix_source,
    _is_secure_message,
    _market_near_entry_zone,
    _market_order_allowed_for_strategy,
    _minimum_safe_stop,
    _manual_exit_reason_after_hold,
    _managed_source_message_id,
    _managed_source_message_ids,
    _normalize_gold_provider_quote_basis,
    _ghp_gold_sanity_reason,
    _pending_expiry_minutes,
    _pending_price_cancellation_allowed,
    _planned_market_reward_risk,
    _parse_signal,
    _relay_content_signature,
    _phoenix_direction_hint,
    _phoenix_direction_pullback_confirmed,
    _phoenix_entries_for_target_plan,
    _is_phoenix_direction_runner_announcement,
    _phoenix_continuation_market_allowed,
    _phoenix_deepest_runner_target,
    _phoenix_extra_market_runner_target,
    _phoenix_full_signal_stage_allowed,
    _phoenix_followup_revision_match,
    _phoenix_pending_stage_allowed,
    _signal_per_leg_risk_pct,
    _phoenix_profit_module_plan,
    _phoenix_levels_plausible_against_market,
    _phoenix_market_runner_allowed,
    _phoenix_numeric_range,
    _phoenix_range_be_candidate,
    _phoenix_confirmed_range_legs,
    _phoenix_limit_pending_levels,
    _phoenix_range_pending_levels,
    _phoenix_range_market_entry_state,
    _phoenix_preliminary_reconcile_reason,
    _phoenix_provider_stop_with_cap,
    _phoenix_tp_ladder_is_strict,
    _phoenix_lot_from_balance,
    _phoenix_runner_matches_range,
    _phoenix_runner_timeout_due,
    _position_matches_managed,
    _tp_one_runner_target_index,
    _tp_one_runner_requested_volume,
    _phoenix_progressive_stop,
    _phoenix_wait_for_zone_retrace,
    _repair_tps_for_entry,
    _repair_gold_hundred_digit_typo,
    _repair_phoenix_truncated_stop,
    _risk_usd_per_leg,
    _select_live_tps,
    _sl_increases_risk,
    _split_total_volume,
    _strict_live_tps_for_entry,
    _stale_profit_exit_update,
    _signal_content_signature,
    _should_execute_fresh_edited_signal,
    _split_target_plan_for_strategy,
    _tfxc_momentum_signal,
    _tfxc_premium_tight_signal,
    _tp_hit_level,
    _with_nearest_runner_leg,
)


class TelegramSignalParserTests(unittest.TestCase):
    def test_phoenix_profit_module_uses_middle_entry_and_tp1_tp5_tp6(self):
        self.assertEqual(
            _phoenix_profit_module_plan([4438.0, 4442.0]),
            [
                (911, 4440.0, 1, "none", 911),
                (912, 4440.0, 5, "none", 912),
                (913, 4440.0, 6, "none", 913),
            ],
        )

    def test_phoenix_profit_module_assigns_runner_protection_per_target(self):
        self.assertEqual(
            _phoenix_profit_module_plan(
                [4438.0, 4442.0],
                (5, 6),
                ("phoenix_ladder", "phoenix_ladder"),
            ),
            [
                (911, 4440.0, 5, "phoenix_ladder", 911),
                (912, 4440.0, 6, "phoenix_ladder", 912),
            ],
        )

    def test_account_risk_base_uses_lower_balance_and_equity(self):
        with patch.dict("os.environ", {"POSITION_RISK_BASE": "lower"}):
            self.assertEqual(_account_risk_base(SimpleNamespace(balance=1000.0, equity=940.0)), 940.0)
            self.assertEqual(_account_risk_base(SimpleNamespace(balance=1000.0, equity=1040.0)), 1000.0)

    def test_account_risk_base_can_use_equity(self):
        with patch.dict("os.environ", {"POSITION_RISK_BASE": "equity"}):
            self.assertEqual(_account_risk_base(SimpleNamespace(balance=1000.0, equity=1450.0)), 1450.0)

    def test_position_matching_does_not_attach_new_phoenix_leg_to_stale_signal(self):
        position = SimpleNamespace(
            ticket=1736524951,
            comment="tg:phoenixvip:517279d2",
            type=0,
            tp=4357.0,
            price_open=4355.0,
        )
        stale = {
            "signal_id": "b4c6687ff82b-e1-t1",
            "order_ticket": 1736137465,
            "side": "buy",
            "execution_tp": 4357.0,
            "entry": 4355.0,
        }
        current = {
            "signal_id": "517279d21523-e1-t1",
            "order_ticket": 1736524951,
            "side": "buy",
            "execution_tp": 4357.0,
            "entry": 4355.0,
        }

        self.assertFalse(_position_matches_managed(position, stale))
        self.assertTrue(_position_matches_managed(position, current))

    def test_position_matching_uses_signal_token_and_tp_for_legacy_record_without_ticket(self):
        position = SimpleNamespace(ticket=9002, comment="tg:phoenixvip:517279d2", tp=4359.0)
        managed = {"signal_id": "517279d21523-e3-t3", "execution_tp": 4359.0}

        self.assertTrue(_position_matches_managed(position, managed))

    def test_position_matching_does_not_confuse_sibling_legs_with_same_comment_token(self):
        position = SimpleNamespace(ticket=9002, comment="tg:phoenixvip:517279d2", tp=4359.0)
        sibling = {
            "signal_id": "517279d21523-e1-t1",
            "order_ticket": 9001,
            "execution_tp": 4357.0,
        }

        self.assertFalse(_position_matches_managed(position, sibling))

    def test_min_hold_does_not_manually_close_levels_already_armed_at_broker(self):
        self.assertEqual(
            _manual_exit_reason_after_hold(
                side="buy",
                current_price=4343.95,
                intended_sl=4344.0,
                intended_tp=4357.0,
                broker_sl=4343.0,
                broker_tp=4357.0,
                deferred_channel_tp_hit=False,
            ),
            "",
        )
        self.assertEqual(
            _manual_exit_reason_after_hold(
                side="buy",
                current_price=4343.95,
                intended_sl=4344.0,
                intended_tp=4357.0,
                broker_sl=0.0,
                broker_tp=0.0,
                deferred_channel_tp_hit=False,
            ),
            "sl",
        )

    def test_deferred_channel_tp_can_close_even_when_broker_levels_are_armed(self):
        self.assertEqual(
            _manual_exit_reason_after_hold(
                side="sell",
                current_price=4350.0,
                intended_sl=4360.0,
                intended_tp=4340.0,
                broker_sl=4360.0,
                broker_tp=4340.0,
                deferred_channel_tp_hit=True,
            ),
            "channel_tp",
        )

    def test_phoenix_near_entry_mapping_is_symmetric_for_buy_and_sell(self):
        entries = [4284.0, 4287.5, 4291.0]

        self.assertEqual(
            _phoenix_entries_for_target_plan("buy", entries, 6, "near"),
            [4291.0] * 6,
        )
        self.assertEqual(
            _phoenix_entries_for_target_plan("sell", entries, 6, "near"),
            [4284.0] * 6,
        )

    def test_phoenix_cycle_mapping_distributes_six_legs_across_range(self):
        entries = [4284.0, 4291.0]

        self.assertEqual(
            _phoenix_entries_for_target_plan("buy", entries, 6, "cycle"),
            [4291.0, 4287.5, 4284.0, 4291.0, 4287.5, 4284.0],
        )
        self.assertEqual(
            _phoenix_entries_for_target_plan("sell", entries, 6, "cycle"),
            [4284.0, 4287.5, 4291.0, 4284.0, 4287.5, 4291.0],
        )

    def test_phoenix_side_deep_mapping_uses_far_edge_for_deep_targets(self):
        entries = [4284.0, 4287.5, 4291.0]

        self.assertEqual(
            _phoenix_entries_for_target_plan("buy", entries, 6, "side_deep"),
            [4291.0, 4287.5, 4284.0, 4284.0, 4284.0, 4284.0],
        )
        self.assertEqual(
            _phoenix_entries_for_target_plan("sell", entries, 6, "side_deep"),
            [4284.0, 4287.5, 4291.0, 4291.0, 4291.0, 4291.0],
        )

    def test_channel_update_reply_targets_only_the_replied_signal(self):
        rows = [
            {"chat_id": -1002864291293, "chat_title": "PHOENIX VIP", "signal_uid": "-1002864291293:10878:1"},
            {"chat_id": -1002864291293, "chat_title": "PHOENIX VIP", "signal_uid": "-1002864291293:10888:1"},
        ]

        self.assertEqual(
            _channel_update_target_message_ids(rows, -1002864291293, "PHOENIX VIP", 10890, 10878),
            {10878},
        )
        self.assertEqual(_managed_source_message_id(rows[1]), 10888)

    def test_channel_update_without_reply_targets_only_latest_preceding_signal(self):
        rows = [
            {"chat_id": -1002864291293, "chat_title": "PHOENIX VIP", "signal_uid": "-1002864291293:10878:1"},
            {"chat_id": -1002864291293, "chat_title": "PHOENIX VIP", "signal_uid": "-1002864291293:10888:1"},
        ]

        self.assertEqual(
            _channel_update_target_message_ids(rows, -1002864291293, "PHOENIX VIP", 10891),
            {10888},
        )

    def test_channel_update_accepts_phoenix_revision_message_alias(self):
        rows = [
            {
                "chat_id": -1002864291293,
                "chat_title": "PHOENIX VIP",
                "signal_uid": "-1002864291293:11029:1",
                "provider_revision_message_ids": [11029, 11030],
            },
        ]

        self.assertEqual(_managed_source_message_ids(rows[0]), {11029, 11030})
        self.assertEqual(
            _channel_update_target_message_ids(rows, -1002864291293, "PHOENIX VIP", 11031, 11030),
            {11030},
        )

    def test_channel_update_accepts_any_message_from_phoenix_signal_cycle(self):
        rows = [
            {
                "chat_id": -1002864291293,
                "chat_title": "PHOENIX VIP",
                "signal_uid": "-1002864291293:12072:1",
                "provider_revision_message_ids": [12070, 12071, 12072],
            },
        ]

        self.assertEqual(
            _channel_update_target_message_ids(rows, -1002864291293, "PHOENIX VIP", 12073, 12070),
            {12070},
        )
        self.assertEqual(
            _channel_update_target_message_ids(rows, -1002864291293, "PHOENIX VIP", 12074, 12071),
            {12071},
        )

    def test_phoenix_followup_revision_matches_only_rapid_same_setup(self):
        now = datetime(2026, 8, 10, 14, 20, 48, tzinfo=UTC)
        signal = _parse_signal(
            "XAUUSD\nENTRY 4335/4344\nSL 4348\nTP 4333\nTP 4332\nTP 4331",
            "-1002864291293:11030",
            -1002864291293,
            "PHOENIX VIP",
            "",
            11030,
            side_hint="sell",
        )
        self.assertIsNotNone(signal)
        managed = {
            "chat_id": -1002864291293,
            "chat_title": "PHOENIX VIP",
            "signal_uid": "-1002864291293:11029:abc-e1-t1",
            "side": "sell",
            "entry": 4335.0,
            "tp1": 4333.0,
            "created_utc": (now - timedelta(seconds=41)).isoformat(),
        }

        self.assertTrue(_phoenix_followup_revision_match(managed, signal, now=now))
        self.assertFalse(
            _phoenix_followup_revision_match(
                {**managed, "created_utc": (now - timedelta(minutes=3)).isoformat()},
                signal,
                now=now,
            )
        )
        self.assertFalse(_phoenix_followup_revision_match({**managed, "tp1": 4332.0}, signal, now=now))

    def test_channel_update_with_unknown_reply_does_not_touch_other_signal(self):
        rows = [
            {"chat_id": -1002864291293, "chat_title": "PHOENIX VIP", "signal_uid": "-1002864291293:10888:1"},
        ]

        self.assertEqual(
            _channel_update_target_message_ids(rows, -1002864291293, "PHOENIX VIP", 10891, 10000),
            set(),
        )

    def test_tp_one_runner_volume_uses_ordinary_leg_multiplier(self):
        self.assertAlmostEqual(_tp_one_runner_requested_volume(0.03, 1.0, 10.0), 0.30)
        self.assertAlmostEqual(_tp_one_runner_requested_volume(0.10, 1.0, 10.0), 1.00)
        self.assertAlmostEqual(_tp_one_runner_requested_volume(0.10, 1.0, 0.0), 1.00)

    def test_phoenix_reconcile_closes_only_materially_worse_losing_entry(self):
        self.assertEqual(
            _phoenix_preliminary_reconcile_reason("sell", 4047.0, -20.0, "sell", [4049.5, 4053.0, 4056.5], 2.0),
            "entry_worse_than_full_zone",
        )
        self.assertIsNone(
            _phoenix_preliminary_reconcile_reason("sell", 4047.6, -20.0, "sell", [4049.5, 4053.0, 4056.5], 2.0)
        )
        self.assertIsNone(
            _phoenix_preliminary_reconcile_reason("sell", 4045.0, 5.0, "sell", [4049.5, 4053.0, 4056.5], 2.0)
        )

    def test_phoenix_reconcile_closes_conflicting_direction(self):
        self.assertEqual(
            _phoenix_preliminary_reconcile_reason("buy", 4050.0, 4.0, "sell", [4049.5, 4053.0, 4056.5], 2.0),
            "opposite_full_signal",
        )

    def test_phoenix_runner_timeout_only_closes_losing_runner(self):
        self.assertTrue(_phoenix_runner_timeout_due(600.0, -1.0, 10.0))
        self.assertFalse(_phoenix_runner_timeout_due(599.0, -1.0, 10.0))
        self.assertFalse(_phoenix_runner_timeout_due(900.0, 1.0, 10.0))

    def test_phoenix_direction_pullback_requires_trend_and_short_retrace(self):
        rising_then_pullback = [100.0 + index for index in range(30)] + [128.8, 128.6, 128.4]
        falling_then_pullback = [140.0 - index for index in range(30)] + [111.2, 111.4, 111.6]
        rising_without_pullback = [100.0 + index for index in range(33)]

        self.assertTrue(_phoenix_direction_pullback_confirmed("buy", rising_then_pullback))
        self.assertTrue(_phoenix_direction_pullback_confirmed("sell", falling_then_pullback))
        self.assertFalse(_phoenix_direction_pullback_confirmed("buy", rising_without_pullback))
        self.assertFalse(_phoenix_direction_pullback_confirmed("sell", rising_then_pullback))

    def test_phoenix_runner_is_compared_directionally_with_later_range(self):
        self.assertTrue(_phoenix_runner_matches_range("buy", 4118.0, [4111.0, 4118.0], 2.0))
        self.assertTrue(_phoenix_runner_matches_range("buy", 4108.0, [4111.0, 4118.0], 2.0))
        self.assertFalse(_phoenix_runner_matches_range("buy", 4120.01, [4111.0, 4118.0], 2.0))
        self.assertTrue(_phoenix_runner_matches_range("sell", 4111.0, [4111.0, 4118.0], 2.0))
        self.assertTrue(_phoenix_runner_matches_range("sell", 4122.0, [4111.0, 4118.0], 2.0))
        self.assertFalse(_phoenix_runner_matches_range("sell", 4108.99, [4111.0, 4118.0], 2.0))

    def test_phoenix_range_be_waits_for_trigger_and_locks_buffer(self):
        self.assertIsNone(_phoenix_range_be_candidate("buy", 4000.0, 4000.49, 3994.0, 0.50, 0.25))
        self.assertEqual(_phoenix_range_be_candidate("buy", 4000.0, 4000.50, 3994.0, 0.50, 0.25), 4000.25)
        self.assertEqual(_phoenix_range_be_candidate("sell", 4000.0, 3999.50, 4006.0, 0.50, 0.25), 3999.75)

    def test_phoenix_range_be_never_worsens_existing_stop(self):
        self.assertIsNone(_phoenix_range_be_candidate("buy", 4000.0, 4001.0, 4000.30, 0.50, 0.25))
        self.assertIsNone(_phoenix_range_be_candidate("sell", 4000.0, 3999.0, 3999.70, 0.50, 0.25))

    def test_per_leg_risk_overrides_total_signal_split(self):
        self.assertEqual(_risk_usd_per_leg(1000.0, 9.0, 6, 1.5), 15.0)
        self.assertEqual(_risk_usd_per_leg(1000.0, 9.0, 6, 0.0), 15.0)
        self.assertEqual(_risk_usd_per_leg(2000.0, 0.0, 9, 1.5), 30.0)

    def test_phoenix_tp_ladder_requires_unique_directional_targets(self):
        self.assertTrue(_phoenix_tp_ladder_is_strict("buy", [4056.0, 4057.0, 4058.0]))
        self.assertTrue(_phoenix_tp_ladder_is_strict("sell", [4056.0, 4055.0, 4054.0]))
        self.assertFalse(_phoenix_tp_ladder_is_strict("sell", [4056.0, 4055.0, 4055.0, 4054.0]))
        self.assertFalse(_phoenix_tp_ladder_is_strict("buy", [4056.0, 4055.0]))

    def test_phoenix_range_pending_levels_use_only_valid_limit_side(self):
        self.assertEqual(
            _phoenix_range_pending_levels("buy", [4111.0, 4119.0], 4120.0, 0.01),
            [4111.0, 4115.0, 4119.0],
        )

    def test_phoenix_pending_expiry_overrides_strategy_default(self):
        cfg = SimpleNamespace(signal_pending_expiry_minutes=5.0)
        managed = {
            "strategy": "phoenix_zone_tp1_tp2_tp4_signal_sl_delayed_be",
            "chat_title": "PHOENIX VIP",
            "strategy_pending_expiry_minutes": 60.0,
        }
        with patch.dict(
            "os.environ",
            {"PHOENIX_PENDING_EXPIRY_MINUTES": "15"},
            clear=False,
        ):
            self.assertEqual(_pending_expiry_minutes(cfg, managed), 15.0)

    def test_phoenix_pre_range_allows_one_small_favorable_market_chase(self):
        self.assertEqual(
            _phoenix_range_market_entry_state("sell", 4331.97, [4335.0, 4341.0], 0.5, 4.0),
            (True, True),
        )
        self.assertEqual(
            _phoenix_range_market_entry_state("sell", 4330.0, [4335.0, 4341.0], 0.5, 4.0),
            (False, False),
        )
        self.assertEqual(
            _phoenix_range_market_entry_state("buy", 4338.0, [4335.0, 4341.0], 0.5, 4.0),
            (True, False),
        )

    def test_confirmed_phoenix_range_can_open_one_market_leg_per_target(self):
        self.assertEqual(
            _phoenix_confirmed_range_legs(True, 4118.5, [], 3, True),
            [("market", 4118.5), ("market", 4118.5), ("market", 4118.5)],
        )
        self.assertEqual(
            _phoenix_confirmed_range_legs(True, 4118.5, [], 3, False),
            [("market", 4118.5)],
        )

    def test_phoenix_pre_signal_keeps_only_deepest_pending_levels(self):
        self.assertEqual(
            _phoenix_limit_pending_levels("buy", [4242.0, 4244.5, 4247.0], 2),
            [4242.0, 4244.5],
        )
        self.assertEqual(
            _phoenix_limit_pending_levels("sell", [4242.0, 4244.5, 4247.0], 2),
            [4247.0, 4244.5],
        )
        self.assertEqual(_phoenix_limit_pending_levels("buy", [4242.0], 0), [])

    def test_phoenix_provider_stop_uses_source_up_to_twelve_dollars(self):
        self.assertEqual(_phoenix_provider_stop_with_cap("buy", 4118.0, 4107.0, 12.0), 4107.0)
        self.assertEqual(_phoenix_provider_stop_with_cap("buy", 4119.32, 4107.0, 12.0), 4107.32)
        self.assertEqual(_phoenix_provider_stop_with_cap("sell", 4100.0, 4115.0, 12.0), 4112.0)
        self.assertEqual(_phoenix_provider_stop_with_cap("buy", 4119.32, 4107.0, 0.0), 4107.0)
        self.assertEqual(
            _phoenix_range_pending_levels("buy", [4111.0, 4119.0], 4115.5, 0.01),
            [4111.0, 4115.0],
        )
        self.assertEqual(
            _phoenix_range_pending_levels("sell", [4111.0, 4119.0], 4110.0, 0.01),
            [4111.0, 4115.0, 4119.0],
        )

    def test_free_signals_channel_is_not_mislabeled_as_saeal(self):
        signal = SimpleNamespace(chat_id=-1003576763534, chat_title="FX GOLD XAUUSD FREE SIGNALS")
        self.assertEqual(_comment_source_tag(signal), "fxgoldfree")

    def test_stale_profit_exit_waits_for_tp1_and_resets_on_progress(self):
        start = datetime(2026, 7, 20, 8, 0, tzinfo=UTC)
        managed = {"tps": [4001.0, 4002.0, 4003.0]}

        due, changed, reached = _stale_profit_exit_update(
            managed,
            side="buy",
            current_price=4001.2,
            floating_profit=10.0,
            now=start,
            stale_minutes=30.0,
        )
        self.assertFalse(due)
        self.assertTrue(changed)
        self.assertEqual(reached, 1)

        due, changed, reached = _stale_profit_exit_update(
            managed,
            side="buy",
            current_price=4002.2,
            floating_profit=15.0,
            now=start + timedelta(minutes=29),
            stale_minutes=30.0,
        )
        self.assertFalse(due)
        self.assertTrue(changed)
        self.assertEqual(reached, 2)

        due, _, reached = _stale_profit_exit_update(
            managed,
            side="buy",
            current_price=4001.4,
            floating_profit=4.0,
            now=start + timedelta(minutes=60),
            stale_minutes=30.0,
        )
        self.assertTrue(due)
        self.assertEqual(reached, 1)

    def test_stale_profit_exit_never_closes_a_losing_leg(self):
        now = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
        managed = {
            "tps": [3999.0, 3998.0],
            "live_exit_reached_tp_level": 1,
            "live_exit_last_progress_utc": (now - timedelta(minutes=45)).isoformat(),
        }
        due, _, _ = _stale_profit_exit_update(
            managed,
            side="sell",
            current_price=4000.0,
            floating_profit=-1.0,
            now=now,
            stale_minutes=30.0,
        )
        self.assertFalse(due)

    def test_phoenix_numeric_range_requires_a_bare_gold_range(self):
        self.assertEqual(_phoenix_numeric_range("4010/4017"), [4010.0, 4017.0])
        self.assertEqual(_phoenix_numeric_range("4010,5 - 4017,5"), [4010.5, 4017.5])
        self.assertEqual(_phoenix_numeric_range("TP 4010/4017"), [])

    def test_complete_levels_override_a_mistyped_explicit_side(self):
        signal = self.parse(
            "XAUUSD BUY NOW 4007\nTP1 4005\nTP2 4003\nTP3 4001\nSL 4019"
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "sell")

    def test_fresh_phoenix_edit_can_be_executed_as_initial_signal(self):
        now = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
        self.assertTrue(
            _should_execute_fresh_edited_signal(
                is_phoenix=True,
                updated_positions=0,
                message_date=now - timedelta(seconds=2),
                now=now,
            )
        )
        self.assertFalse(
            _should_execute_fresh_edited_signal(
                is_phoenix=True,
                updated_positions=0,
                message_date=now - timedelta(minutes=5),
                now=now,
            )
        )
        self.assertFalse(
            _should_execute_fresh_edited_signal(
                is_phoenix=True,
                updated_positions=1,
                message_date=now - timedelta(seconds=2),
                now=now,
            )
        )

    def test_fresh_ghp_gold_edit_can_recover_corrected_levels(self):
        now = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
        self.assertTrue(
            _should_execute_fresh_edited_signal(
                is_phoenix=False,
                is_ghp_gold=True,
                updated_positions=0,
                message_date=now - timedelta(seconds=10),
                now=now,
            )
        )
        self.assertFalse(
            _should_execute_fresh_edited_signal(
                is_phoenix=False,
                is_ghp_gold=True,
                updated_positions=0,
                message_date=now - timedelta(minutes=5),
                now=now,
            )
        )

    def test_profit_dynamic_lot_adds_to_base_every_completed_step(self):
        cfg = SimpleNamespace(
            signal_fixed_lot=0.09,
            signal_dynamic_lot_step_usd=500.0,
            signal_dynamic_lot_add=0.01,
            signal_dynamic_lot_max=999.0,
        )

        self.assertEqual(_dynamic_lot_from_balance(cfg, 1000.0, 1499.99), (0.09, 0, 499.99))
        self.assertEqual(_dynamic_lot_from_balance(cfg, 1000.0, 1500.0), (0.10, 1, 500.0))
        self.assertEqual(_dynamic_lot_from_balance(cfg, 1000.0, 2000.0), (0.11, 2, 1000.0))

    def test_phoenix_lot_scales_every_completed_500_balance_step(self):
        env = {
            "PHOENIX_LOT_BASE_BALANCE_USD": "1000",
            "PHOENIX_LOT_BASE_PER_POSITION": "0.10",
            "PHOENIX_LOT_BALANCE_STEP_USD": "500",
            "PHOENIX_LOT_STEP_ADD": "0.01",
            "PHOENIX_LOT_MAX_PER_POSITION": "999",
        }
        with patch.dict("os.environ", env, clear=False):
            self.assertEqual(_phoenix_lot_from_balance(1000.0), (0.10, 0))
            self.assertEqual(_phoenix_lot_from_balance(1499.99), (0.10, 0))
            self.assertEqual(_phoenix_lot_from_balance(1500.0), (0.11, 1))
            self.assertEqual(_phoenix_lot_from_balance(3000.0), (0.14, 4))

    def test_total_signal_lot_is_distributed_without_rounding_up(self):
        self.assertEqual(_split_total_volume(0.09, 5), [0.02, 0.02, 0.02, 0.02, 0.01])
        self.assertEqual(sum(_split_total_volume(0.09, 5)), 0.09)

    def parse(self, text: str):
        return _parse_signal(text, "sample", 1, "chat", "", 1)

    def test_hold_message_is_recognized(self):
        self.assertTrue(_is_hold_message("keep holding gold"))
        self.assertTrue(_is_hold_message("trzymaj pozycje"))
        self.assertFalse(_is_hold_message("cancel this setup"))
        self.assertFalse(_is_hold_message("secure and move SL to BE"))

    def test_secure_message_is_distinct_from_hold(self):
        self.assertTrue(_is_secure_message("secure and move SL to BE"))
        self.assertTrue(_is_secure_message("zabezpieczamy pozycje"))
        self.assertTrue(_is_secure_message("ustaw SL na BE"))
        self.assertTrue(_is_secure_message("BE"))
        self.assertFalse(
            _is_secure_message(
                "Grajcie dziś ostrożnie! Zalecam ustawiać breakeven, piątki ostatnio lubią być manipulacyjne."
            )
        )
        self.assertFalse(_is_secure_message("keep holding gold"))

    def test_full_signal_with_update_word_is_not_discarded_as_noise(self):
        signal = self.parse("UPDATE XAUUSD BUY 4200 SL 4194 TP1 4202 TP2 4205")

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "buy")
        self.assertEqual(signal.tps, [4202.0, 4205.0])

    def test_generic_side_range_is_parsed_for_any_channel(self):
        signal = self.parse("XAUUSD SELL 4210-4214 SL 4218 TP1 4207 TP2 4204")

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entries, [4210.0, 4212.0, 4214.0])

    def test_generic_side_range_accepts_unicode_dash(self):
        signal = self.parse("Gold Sell 4010.5 — 4015.5 TP1 4005.5 TP2 4000.5 SL 4025.5")

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entries, [4010.5, 4013.0, 4015.5])

    def test_single_tp_label_can_contain_multiple_targets(self):
        signal = self.parse("XAUUSD BUY 4200 SL 4194 TP 4202 4205 4208")

        self.assertIsNotNone(signal)
        self.assertEqual(signal.tps, [4202.0, 4205.0, 4208.0])

    def test_market_stop_is_moved_to_broker_safe_distance(self):
        info = SimpleNamespace(point=0.01, digits=2, trade_stops_level=20, trade_freeze_level=0)
        with patch("app.telegram_signal_bot.mt5.symbol_info", return_value=info):
            self.assertEqual(_minimum_safe_stop("XAUUSD", "buy", 4026.22, 4026.0, 12.0, 50), 4025.72)
            self.assertEqual(_minimum_safe_stop("XAUUSD", "sell", 4026.22, 4026.4, 12.0, 50), 4026.72)

    def test_tp_hit_messages_are_recognized(self):
        self.assertEqual(_tp_hit_level("TP1✅"), 1)
        self.assertEqual(_tp_hit_level("TP4 ✅"), 4)
        self.assertEqual(_tp_hit_level("TP10✅"), 10)
        self.assertEqual(_tp_hit_level("WSZYSTKIE TP ZALICZONE!!!"), 99)
        self.assertEqual(_tp_hit_level("TP 4084\nTP 4083\nSL 4099"), 0)
        self.assertEqual(
            _tp_hit_level("TP1 : 3985\nTP2 : 3990\nTP3 : 3995\nTP4 : open✅\nSL : PREMIUM"),
            0,
        )

    def test_channel_lot_override_matches_public_username(self):
        signal = self.parse("XAUUSD BUY 4200 TP1 4205 SL 4195")
        cfg = SimpleNamespace(channel_lot_sizes={"https://t.me/Gold_btcusd_xauusd/4455": 0.01})

        self.assertEqual(_channel_lot_override(cfg, signal, "Gold_btcusd_xauusd"), 0.01)

    def test_channel_lot_override_matches_telegram_chat_id(self):
        signal = _parse_signal("XAUUSD BUY 4200 TP1 4205 SL 4195", "sample", -1003991839723, "Gold", "", 1)
        cfg = SimpleNamespace(channel_lot_sizes={"3991839723": 0.03})

        self.assertEqual(_channel_lot_override(cfg, signal), 0.03)

    def test_phoenix_source_is_recognized(self):
        self.assertTrue(_is_phoenix_source(-1002864291293, "PHOENIX VIP"))
        self.assertTrue(_is_phoenix_source(None, "Phoenix VIP"))
        self.assertFalse(_is_phoenix_source(-1001914224843, "XAUUSD GOLD SIGNAL"))

    def test_dany_signals_parses_implicit_gold_signal(self):
        signal = _parse_signal(
            "Buy At 4355.92 with love\nSL 4335.5\nTP 4375\nTP 4400\nTP 4433",
            "-1004410781005:63",
            -1004410781005,
            "Dany Signals",
            "",
            63,
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.asset, "gold")
        self.assertEqual(signal.side, "buy")
        self.assertEqual(signal.entries, [4355.92])
        self.assertEqual(signal.tps, [4375.0, 4400.0, 4433.0])
        self.assertEqual(_channel_strategy(signal).name, "ghp_gold_tp1_tp2_deep_original_sl_be_60m")

    def test_dany_signals_keeps_phoenix_range_strategy(self):
        signal = _parse_signal(
            "XAUUSD\nENTRY 4391/4385\nSL 4377\nTP 4393\nTP 4394\nTP 4395\nMaterial ma charakter edukacyjny!",
            "-1004410781005:62",
            -1004410781005,
            "Dany Signals",
            "",
            62,
        )
        self.assertIsNotNone(signal)
        self.assertEqual(_channel_strategy(signal).name, "phoenix_zone_tp1_tp2_tp4_signal_sl_delayed_be")

    def test_dany_signals_parses_bare_target_lines(self):
        signal = _parse_signal(
            "Buy at 4392.63\nSL 4377.63\n4403\n4413\n4423",
            "-1004410781005:59",
            -1004410781005,
            "Dany Signals",
            "",
            59,
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.asset, "gold")
        self.assertEqual(signal.tps, [4403.0, 4413.0, 4423.0])

    def test_dany_relay_signature_matches_original_source(self):
        source = _parse_signal(
            "XAUUSD BUY 4355.92 SL 4335.5 TP 4375 TP 4400 TP 4433",
            "source:1",
            -1002033681012,
            "Goldhunter Paul",
            "",
            1,
        )
        relay = _parse_signal(
            "XAUUSD BUY 4355.92 SL 4335.5 TP 4375 TP 4400 TP 4433",
            "relay:1",
            -1004410781005,
            "Dany Signals",
            "",
            1,
        )
        self.assertTrue(_is_dany_signals_source(relay.chat_id, relay.chat_title))
        self.assertEqual(_relay_content_signature(source), _relay_content_signature(relay))

    def test_phoenix_waits_for_retrace_when_market_is_beyond_zone_before_tp1(self):
        self.assertTrue(
            _phoenix_wait_for_zone_retrace(
                {
                    "decision": "pending_only",
                    "zone_state": "after_zone",
                    "reached_level": 0,
                    "zone_distance": 2.8,
                }
            )
        )
        self.assertFalse(
            _phoenix_wait_for_zone_retrace(
                {
                    "decision": "fresh_zone",
                    "zone_state": "after_zone",
                    "reached_level": 0,
                    "zone_distance": 1.45,
                }
            )
        )
        self.assertFalse(
            _phoenix_wait_for_zone_retrace(
                {"zone_state": "inside_zone", "reached_level": 0, "zone_distance": 0.0}
            )
        )

    def test_phoenix_11348_distributes_range_and_allows_market_runner(self):
        entries = [4345.0, 4347.5, 4350.0]
        tps = [4352.0, 4353.0, 4354.0, 4355.0, 4356.0, 4356.5, 4357.0, 4358.0]

        self.assertEqual(
            _phoenix_entries_for_target_plan("buy", entries, 3, "cycle"),
            [4350.0, 4347.5, 4345.0],
        )
        with patch.dict(
            "os.environ",
            {
                "PHOENIX_MARKET_ENTRY_TOLERANCE": "2.0",
                "PHOENIX_MIN_MARKET_TP1_DISTANCE": "0.5",
            },
            clear=False,
        ):
            self.assertTrue(_phoenix_market_runner_allowed("buy", entries, 4351.45, tps))
        self.assertFalse(
            _phoenix_wait_for_zone_retrace(
                {"zone_state": "after_zone", "reached_level": 1, "zone_distance": 2.0}
            )
        )

    def test_phoenix_full_signal_stages_only_through_tp1(self):
        env = {
            "PHOENIX_ALWAYS_STAGE_RANGE_PENDING": "true",
            "PHOENIX_FULL_SIGNAL_STAGE_MAX_REACHED_TP_LEVEL": "1",
        }
        with patch.dict("os.environ", env, clear=False):
            self.assertTrue(
                _phoenix_full_signal_stage_allowed(
                    {"reached_level": 1},
                    retrace_pending=True,
                    entry_count=2,
                )
            )
            self.assertFalse(
                _phoenix_full_signal_stage_allowed(
                    {"reached_level": 2},
                    retrace_pending=True,
                    entry_count=2,
                )
            )

    def test_explicit_phoenix_limit_can_stage_even_when_market_is_already_past_tps(self):
        signal = self.parse(
            "XAUUSD SELL LIMIT! ENTRY 4320/4328 SL 4340 TP 4318 TP 4317 TP 4316"
        )
        self.assertTrue(
            _phoenix_pending_stage_allowed(
                signal,
                {"reached_level": 3},
                retrace_pending=True,
                entry_count=3,
            )
        )

    def test_full_phoenix_signal_after_matching_pre_range_can_wait_for_retrace(self):
        signal = self.parse(
            "XAUUSD SELL ENTRY 4290/4297 SL 4302 TP 4288 TP 4287 TP 4286"
        )
        with patch.dict("os.environ", {"PHOENIX_PLAY_FULL_SIGNAL_AFTER_PRE_RANGE": "true"}, clear=False):
            self.assertTrue(
                _phoenix_pending_stage_allowed(
                    signal,
                    {"reached_level": 3},
                    retrace_pending=True,
                    entry_count=3,
                    preliminary_range_matched=True,
                )
            )

    def test_phoenix_strategy_uses_calibrated_three_leg_plan(self):
        signal = _parse_signal(
            """XAUUSD

ENTRY 4215-4208
SL 4203
TP 4217
TP 4219
TP 4220
TP 4221

Material ma charakter edukacyjny!""",
            "phoenix:1",
            -1002864291293,
            "PHOENIX VIP",
            "",
            1,
        )

        self.assertIsNotNone(signal)
        strategy = _channel_strategy(signal)
        self.assertEqual(strategy.name, "phoenix_zone_tp1_tp2_tp4_signal_sl_delayed_be")
        self.assertEqual(strategy.split_target_indices, (1, 2, 4))
        self.assertEqual(strategy.protect_mode, "be_after_tp3")
        self.assertTrue(strategy.force_all_entries)
        self.assertTrue(strategy.limit_only_ranges)
        self.assertTrue(strategy.allow_market_runner)
        self.assertEqual(strategy.pending_expiry_minutes, 15.0)
        self.assertEqual(
            _split_target_plan_for_strategy(strategy, is_phoenix_signal=True, is_tfxc_signal=False),
            [(1, "none", 1), (2, "be_after_tp3", 2), (4, "be_after_tp3", 3)],
        )

    def test_phoenix_ladder_stop_moves_one_tp_at_a_time(self):
        tps = [4179.0, 4180.0, 4181.0, 4182.0, 4183.0, 4183.5]

        self.assertEqual(_phoenix_progressive_stop("buy", 4178.4, tps, 1, 4164.0), 4178.5)
        self.assertEqual(_phoenix_progressive_stop("buy", 4178.4, tps, 2, 4178.5), 4179.0)
        self.assertEqual(_phoenix_progressive_stop("buy", 4178.4, tps, 3, 4179.0), 4180.0)

    def test_phoenix_extra_tp6_runner_requires_market_entry_and_six_live_targets(self):
        entries = [4177.0, 4178.0, 4179.0]
        tps = [4180.0, 4181.0, 4182.0, 4183.0, 4184.0, 4185.0]

        with patch.dict(
            "os.environ",
            {
                "PHOENIX_MARKET_ENTRY_TOLERANCE": "2",
                "PHOENIX_MIN_MARKET_TP1_DISTANCE": "0",
            },
        ):
            self.assertEqual(
                _phoenix_extra_market_runner_target("buy", entries, 4178.5, tps, 6),
                6,
            )
            self.assertEqual(
                _phoenix_extra_market_runner_target("buy", entries, 4178.5, tps[:5], 6),
                0,
            )
            self.assertEqual(
                _phoenix_extra_market_runner_target("buy", entries, 4186.0, tps, 6),
                0,
            )

    def test_phoenix_extra_runner_before_tp1_gate_accepts_price_outside_zone(self):
        entries = [4177.0, 4178.0, 4179.0]
        tps = [4180.0, 4181.0, 4182.0, 4183.0, 4184.0, 4185.0]

        self.assertEqual(
            _phoenix_extra_market_runner_target(
                "buy", entries, 4175.0, tps, 5, "before_tp1"
            ),
            5,
        )
        self.assertEqual(
            _phoenix_extra_market_runner_target(
                "buy", entries, 4181.5, tps, 5, "before_tp1"
            ),
            0,
        )

    def test_phoenix_signal_keeps_live_tps_when_market_is_inside_entry_zone(self):
        signal = _parse_signal(
            """XAUUSD

ENTRY 3980/3988
SL 3995
TP 3978
TP 3977
TP 3976
TP 3975
TP 3974
TP 3973.5

Material ma charakter edukacyjny!""",
            "phoenix:8998",
            -1002864291293,
            "PHOENIX VIP",
            "",
            8998,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entries, [3980.0, 3984.0, 3988.0])
        self.assertEqual(_strict_live_tps_for_entry(signal.side, 3987.05, signal.tps), signal.tps)

    def test_phoenix_buy_repairs_hundred_digit_typo_against_market(self):
        signal = _parse_signal(
            """XAUUSD

ENTRY 4074/4066
SL 4060
TP 4076
TP 4077
TP 4078
TP 4079
TP 4080
TP 4080.5""",
            "phoenix:9387",
            -1002864291293,
            "PHOENIX VIP",
            "",
            9387,
        )

        repaired = _repair_gold_hundred_digit_typo(signal, 4176.99)

        self.assertEqual(repaired.entries, [4166.0, 4170.0, 4174.0])
        self.assertEqual(repaired.sl, 4160.0)
        self.assertEqual(repaired.tps[0], 4176.0)

    def test_phoenix_sell_repairs_hundred_digit_typo_against_market(self):
        signal = _parse_signal(
            """XAUUSD

ENTRY 4086/4094
SL 4099
TP 4084
TP 4083
TP 4082
TP 4081
TP 4080
TP 4079.5""",
            "phoenix:9410",
            -1002864291293,
            "PHOENIX VIP",
            "",
            9410,
        )

        repaired = _repair_gold_hundred_digit_typo(signal, 4183.94)

        self.assertEqual(repaired.entries, [4186.0, 4190.0, 4194.0])
        self.assertEqual(repaired.sl, 4199.0)
        self.assertEqual(repaired.tps[0], 4184.0)
        self.assertTrue(_phoenix_levels_plausible_against_market(repaired, 4183.94))

    def test_phoenix_repairs_truncated_provider_stop_from_entry_zone(self):
        signal = _parse_signal(
            """XAUUSD
ENTRY 4371/4377
SL 438
TP 4369
TP 4368
TP 4367""",
            "phoenix:truncated-sl",
            -1002864291293,
            "PHOENIX VIP",
            "",
            12351,
            side_hint="sell",
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.sl, 0.0)
        repaired = _repair_phoenix_truncated_stop(signal, 4368.54)
        self.assertEqual(repaired.sl, 4380.0)

    def test_phoenix_implausible_unrepaired_levels_are_rejected(self):
        signal = _parse_signal(
            """XAUUSD

ENTRY 4086/4094
SL 4099
TP 4084
TP 4083
TP 4082""",
            "phoenix:bad",
            -1002864291293,
            "PHOENIX VIP",
            "",
            9411,
        )

        self.assertFalse(_phoenix_levels_plausible_against_market(signal, 4183.94))

    def test_phoenix_repairs_other_digit_typos_against_market(self):
        signal = _parse_signal(
            """XAUUSD

ENTRY 4146/4154
SL 4159
TP 4144
TP 4143
TP 4142""",
            "phoenix:digit",
            -1002864291293,
            "PHOENIX VIP",
            "",
            9420,
        )

        repaired = _repair_gold_hundred_digit_typo(signal, 4183.94)

        self.assertEqual(repaired.entries, [4186.0, 4190.0, 4194.0])
        self.assertEqual(repaired.sl, 4199.0)
        self.assertEqual(repaired.tps[:3], [4184.0, 4183.0, 4182.0])

    def test_phoenix_repairs_extra_digit_in_stop_before_side_inference(self):
        signal = _parse_signal(
            """XAUUSD
ENTRY 4753-4746
SL 47838
TP 4755
TP 4757
TP 4758
TP 4759
TP 4760
TP 4761""",
            "phoenix:bad-sl",
            -1002864291293,
            "PHOENIX VIP",
            "",
            5962,
            side_hint="buy",
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "buy")
        self.assertEqual(signal.sl, 4738.0)

    def test_phoenix_repair_tps_rebuilds_bad_ladder_against_entry(self):
        repaired = _repair_tps_for_entry("sell", 3987.05, [3990.0, 3991.0], 4)

        self.assertEqual(len(repaired), 4)
        self.assertTrue(all(tp < 3987.05 for tp in repaired))
        self.assertEqual(repaired[:2], [3986.05, 3985.05])

    def test_market_near_entry_zone_allows_two_dollar_tolerance(self):
        self.assertTrue(_market_near_entry_zone([3980.0, 3984.0, 3988.0], 3989.9))
        self.assertTrue(_market_near_entry_zone([3980.0, 3984.0, 3988.0], 3976.1))
        self.assertFalse(_market_near_entry_zone([3980.0, 3984.0, 3988.0], 3992.5))

    def test_phoenix_continuation_allows_market_before_tp3(self):
        self.assertTrue(
            _phoenix_continuation_market_allowed(
                "sell",
                [4038.0, 4042.0, 4046.0],
                4035.5,
                [4036.0, 4035.0, 4034.0, 4033.0],
            )
        )
        self.assertFalse(
            _phoenix_continuation_market_allowed(
                "sell",
                [4038.0, 4042.0, 4046.0],
                4033.5,
                [4036.0, 4035.0, 4034.0, 4033.0],
            )
        )

    def test_nearest_runner_leg_uses_market_leg_first(self):
        plan = _with_nearest_runner_leg(
            [
                (0, 3986.37, 1, "none", 0),
                (1, 3988.0, 1, "none", 1),
                (2, 3992.0, 1, "be", 2),
            ],
            3986.37,
        )

        self.assertEqual(plan[0], (0, 3986.37, 4, "atr", 0))

    def test_nearest_runner_leg_can_use_phoenix_be_protection(self):
        plan = _with_nearest_runner_leg(
            [
                (0, 4037.6, 4, "atr", 0),
                (1, 4038.0, 1, "none", 1),
            ],
            4037.6,
            2,
            "be",
        )

        self.assertEqual(plan[0], (0, 4037.6, 2, "be", 0))

    def test_phoenix_market_runner_allows_fast_tp1_scalp_near_zone(self):
        entries = [4053.0, 4057.0, 4061.0]
        tps = [4063.0, 4064.0, 4065.0]

        self.assertFalse(_phoenix_market_runner_allowed("buy", entries, 4062.72, tps))
        self.assertFalse(_phoenix_market_runner_allowed("buy", entries, 4060.8, [4061.2, 4062.0]))
        self.assertTrue(_phoenix_market_runner_allowed("buy", entries, 4060.8, [4062.4, 4064.0]))

    def test_tp_one_runner_moves_to_next_live_target_when_tp1_is_too_close(self):
        self.assertEqual(
            _tp_one_runner_target_index(
                "sell",
                3969.1,
                [3969.0, 3968.0, 3967.0, 3966.0],
                0.12,
            ),
            2,
        )

    def test_tp_one_runner_requires_a_live_target(self):
        self.assertEqual(_tp_one_runner_target_index("sell", 3960.0, [3969.0, 3968.0], 0.12), 0)

    def test_phoenix_deep_runner_uses_deepest_real_target_capped_at_tp8(self):
        tps = [4201.0, 4202.0, 4203.0, 4204.0, 4205.0, 4206.0, 4207.0, 4208.0, 4209.0]

        self.assertEqual(_phoenix_deepest_runner_target("buy", 4200.0, tps), 8)
        self.assertEqual(_phoenix_deepest_runner_target("buy", 4200.0, tps[:6]), 6)
        self.assertEqual(_phoenix_deepest_runner_target("buy", 4200.0, tps[:4]), 0)

    def test_sl_widen_detection_blocks_more_risk(self):
        self.assertTrue(_sl_increases_risk("buy", 4014.0, 4010.0))
        self.assertFalse(_sl_increases_risk("buy", 4014.0, 4016.0))
        self.assertTrue(_sl_increases_risk("sell", 4025.0, 4030.0))
        self.assertFalse(_sl_increases_risk("sell", 4025.0, 4020.0))

    def test_nearest_runner_leg_uses_closest_entry_without_market_leg(self):
        plan = _with_nearest_runner_leg(
            [
                (1, 3987.0, 1, "none", 1),
                (2, 3991.0, 1, "be", 2),
                (3, 3995.0, 2, "be", 3),
            ],
            3989.5,
        )

        self.assertEqual(plan[1], (2, 3991.0, 4, "atr", 2))

    def test_tfxc_premium_uses_tight_tp1_strategy(self):
        signal = _parse_signal(
            """SIGNAL ALERT

SELL XAUUSD 4197.3

TP1: 4195.5
TP2: 4193.7
TP3: 4185.3
SL: 4209.3""",
            "tfx:1",
            -1001220837618,
            "TFXC PREMIUM",
            "",
            1,
        )

        self.assertIsNotNone(signal)
        strategy = _channel_strategy(signal)
        self.assertEqual(strategy.name, "tfxc_low_lot_tp1_tp1_be")
        self.assertEqual(strategy.split_target_indices, (1, 1))
        self.assertEqual(
            _split_target_plan_for_strategy(strategy, is_phoenix_signal=False, is_tfxc_signal=True),
            [(1, "be", 1), (1, "be", 2)],
        )
        adjusted = _tfxc_premium_tight_signal(signal)
        self.assertLess(adjusted.sl, signal.sl)
        self.assertEqual(adjusted.tps, [4195.5, 4194.6])
        self.assertIsNone(_tfxc_momentum_signal(signal, 4196.0))
        momentum = _tfxc_momentum_signal(signal, 4195.0)
        self.assertIsNotNone(momentum)
        self.assertEqual(momentum.entry, 4195.0)
        self.assertEqual(momentum.sl, 4195.5)
        self.assertEqual(momentum.tps[0], 4193.7)

    def test_safe_gold_channels_use_tp1_tp2_be_and_no_chase(self):
        for chat_id, title in [
            (-1001704634655, "Gold Hunter FX"),
            (-1002528249483, "GHPTrading"),
            (-1001914224843, "XAUUSD GOLD SIGNAL"),
        ]:
            signal = _parse_signal("XAUUSD BUY 4200 TP1 4203 TP2 4206 SL 4194", "safe:1", chat_id, title, "", 1)
            self.assertIsNotNone(signal)
            strategy = _channel_strategy(signal)
            self.assertEqual(strategy.split_target_indices, (1, 1, 2))
            self.assertEqual(strategy.protect_mode, "be")
            self.assertFalse(_market_order_allowed_for_strategy(strategy, "buy", 4200.0, 4203.2, [4203.0, 4206.0]))
            self.assertTrue(_market_order_allowed_for_strategy(strategy, "buy", 4200.0, 4200.8, [4203.0, 4206.0]))

    def test_100d_selected_channels_use_calibrated_strategies(self):
        cases = [
            (-1003576763534, "FX GOLD XAUUSD FREE SIGNALS", "fxgold_100d_tp1_all_no_be_wide_market", (1, 1, 1)),
            (-1001996608301, "XAUUSD PIPS KILLERS", "xau_pips_100d_tp1_all_no_be_wide_market", (1, 1, 1)),
            (-1003212344580, "GOLD PRO TRADER", "goldpro_100d_tp1_tp2_tp3_no_be_wide_pending", (1, 2, 3)),
            (-1001826528649, "GOLD BTC XAUUSD FOREX VIP", "goldbtcxauvip_100d_tp1_tp1_tp2_no_be_wide_pending", (1, 1, 2)),
        ]
        for chat_id, title, expected_name, expected_targets in cases:
            signal = _parse_signal("XAUUSD BUY 4200 TP1 4203 TP2 4206 TP3 4209 SL 4194", f"cal:{chat_id}", chat_id, title, "", 1)
            self.assertIsNotNone(signal)
            strategy = _channel_strategy(signal)
            self.assertEqual(strategy.name, expected_name)
            self.assertEqual(strategy.split_target_indices, expected_targets)

    def test_nas_channel_uses_safe_tp1_tp2_be_strategy(self):
        signal = _parse_signal("NAS100 BUY 18000 TP1 18040 TP2 18080 SL 17940", "nas:1", -1001232813229, "VipNas100 Pro", "", 1)

        self.assertIsNotNone(signal)
        strategy = _channel_strategy(signal)
        self.assertEqual(strategy.name, "vipnas100_60d_tp1_tp1_tp2_be_after_tp1_wide_market")
        self.assertEqual(strategy.split_target_indices, (1, 1, 2))
        self.assertTrue(_market_order_allowed_for_strategy(strategy, "buy", 18000.0, 18030.0, [18040.0, 18080.0]))
        self.assertTrue(_market_order_allowed_for_strategy(strategy, "buy", 18000.0, 18010.0, [18040.0, 18080.0]))

    def test_buy_with_emoji_take_profit_labels(self):
        signal = self.parse(
            """🟢 XAUUSD buy at🔤4177.00

✔️ Take Profit 1️⃣🔤4180.00

✔️ Take Profit 2️⃣🔤4183.00

✔️ Take Profit 3️⃣🔤4187.00

✔️ Take Profit 4️⃣🔤4192.00"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "buy")
        self.assertEqual(signal.order_kind, "market")
        self.assertEqual(signal.entry, 4177.00)
        self.assertEqual(signal.sl, 0.0)
        self.assertEqual(signal.tps, [4180.00, 4183.00, 4187.00, 4192.00])

    def test_xauusd_sell_with_compact_tp_and_sl(self):
        signal = self.parse(
            """XAUUSD 📉 SELL 4092.00

💰TP1 4090.00
💰TP2 4087.00
💰TP3 4082.00
🚫SL 4100.00"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entry, 4092.00)
        self.assertEqual(signal.sl, 4100.00)
        self.assertEqual(signal.tps, [4090.00, 4087.00, 4082.00])

    def test_gold_sell_alias(self):
        signal = self.parse(
            """🔰GOLD SELL 4241

🔳TP1 4236.00

🔳TP2 4231.00

🔳TP3 4226.00

❌SL 4246.00"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entry, 4241.00)
        self.assertEqual(signal.sl, 4246.00)
        self.assertEqual(signal.tps, [4236.00, 4231.00, 4226.00])

    def test_btcusd_signal_is_supported(self):
        signal = self.parse(
            """BTCUSD BUY 64244.00
TP1 64323.00
SL 64000.00"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.asset, "btc")
        self.assertEqual(signal.side, "buy")
        self.assertEqual(signal.entry, 64244.0)
        self.assertEqual(signal.tps, [64323.0])

    def test_bitcoin_alias_signal_is_supported(self):
        signal = self.parse(
            """Bitcoin sell 59650
TP1 59500
TP2 59350
SL 59800"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.asset, "btc")
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entry, 59650.0)

    def test_nas100_sell_signal_sorts_targets_by_distance(self):
        signal = self.parse(
            """NAS100 SELL 30450
SL 30580
TP 29420
TP 30350
TP 30250
TP 29950"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.asset, "nas100")
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entry, 30450.0)
        self.assertEqual(signal.sl, 30580.0)
        self.assertEqual(signal.tps, [30350.0, 30250.0, 29950.0, 29420.0])

    def test_us30_signal_uses_us30_asset(self):
        signal = self.parse(
            """US30 SELL 52280
SL 52410
TP 52250
TP 52180
TP 52080
TP 51780"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.asset, "us30")
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entry, 52280.0)
        self.assertEqual(signal.sl, 52410.0)
        self.assertEqual(signal.tps, [52250.0, 52180.0, 52080.0, 51780.0])

    def test_same_signal_content_gets_same_signature(self):
        first = _parse_signal(
            """GOLD Buy 4319-4320

TP 4326
TP 4332
TP 4360

SL 4313""",
            "chat:17714",
            -1001838220681,
            "Gold Pro Trader",
            "",
            17714,
        )
        second = _parse_signal(
            """GOLD Buy 4319-4320

TP 4326
TP 4332
TP 4360

SL 4313""",
            "chat:17713",
            -1001838220681,
            "Gold Pro Trader",
            "",
            17713,
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.uid, second.uid)
        self.assertEqual(_signal_content_signature(first), _signal_content_signature(second))

    def test_live_tp_selection_skips_passed_tp1(self):
        tp1, execution_tp, live_index, live_tps = _select_live_tps("buy", 4314.49, [4314.0, 4317.0, 4320.0], 2)

        self.assertEqual(tp1, 4317.0)
        self.assertEqual(execution_tp, 4320.0)
        self.assertEqual(live_index, 2)
        self.assertEqual(live_tps, [4317.0, 4320.0])

    def test_tagsignals_zone_without_tp_or_sl(self):
        signal = _parse_signal(
            """4332-4347
Sell / venta
XAUUSD""",
            "tags:4995",
            -1002717527369,
            "Free Tag Signals",
            "",
            4995,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.asset, "gold")
        self.assertEqual(signal.entries, [4332.0, 4339.5, 4347.0])
        self.assertEqual(signal.sl, 4354.5)
        self.assertEqual(signal.tps, [4324.5, 4317.0, 4309.5])
        strategy = _channel_strategy(signal)
        self.assertEqual(strategy.name, "tagsignals_wide_zone_3_limits_tp1_tp2_tp3_be")
        self.assertTrue(strategy.force_all_entries)
        self.assertEqual(strategy.split_target_indices, (1, 2, 3))

    def test_xauusd_gold_signal_unlabeled_zone_levels(self):
        signal = _parse_signal(
            """ Sell 4350.4355

 4345

 4340

 4335

TP 4330

TP 4320

 4367""",
            "xau:34299",
            -1001914224843,
            "XAUUSD GOLD SIGNAL",
            "",
            34299,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.asset, "gold")
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entries, [4350.0, 4352.5, 4355.0])
        self.assertEqual(signal.sl, 4367.0)
        self.assertEqual(signal.tps, [4345.0, 4340.0, 4335.0, 4330.0, 4320.0])

    def test_xauusd_gold_signal_space_separated_sell_zone(self):
        signal = _parse_signal(
            """XAUUSD sell 4363 4367
SL 4373
TP 4355
TP 4345
TP 4335
TP 4325""",
            "xau:34359",
            -1001914224843,
            "XAUUSD GOLD SIGNAL",
            "",
            34359,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.asset, "gold")
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entries, [4363.0, 4365.0, 4367.0])
        self.assertEqual(signal.sl, 4373.0)
        self.assertEqual(signal.tps, [4355.0, 4345.0, 4335.0, 4325.0])

    def test_dollar_xauusd_sell_with_entry_stop_loss_and_many_tps(self):
        signal = self.parse(
            """$XAUUSD - SELL 👑

ENTRY: 4468.70
STOP LOSS: 4486.00


TP1: 4463.00
TP2: 4456.00
TP3: 4450.00
TP4: 4445.00
TP5: 4436.00"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entry, 4468.70)
        self.assertEqual(signal.sl, 4486.00)
        self.assertEqual(signal.tps, [4463.00, 4456.00, 4450.00, 4445.00, 4436.00])

    def test_phoenix_vip_buy_without_explicit_side(self):
        signal = self.parse(
            """XAUUSD

ENTRY 4215-4208
SL 4203
TP 4217
TP 4219
TP 4220
TP 4221
TP 4222
TP 4222.5

Material ma charakter edukacyjny!"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "buy")
        self.assertEqual(signal.entry, 4215.00)
        self.assertEqual(signal.sl, 4203.00)
        self.assertEqual(signal.tps[:4], [4217.00, 4219.00, 4220.00, 4221.00])

    def test_phoenix_vip_sell_without_explicit_side(self):
        signal = self.parse(
            """XAUUSD

ENTRY 4217-4226
SL 4230
TP 4215
TP 4213
TP 4212
TP 4211
TP 4210
TP 4209.5

Material ma charakter edukacyjny!"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entry, 4217.00)
        self.assertEqual(signal.sl, 4230.00)
        self.assertEqual(signal.tps[:4], [4215.00, 4213.00, 4212.00, 4211.00])

    def test_phoenix_polish_direction_announcements(self):
        self.assertEqual(_phoenix_direction_hint("Wrzucę teraz, góra"), "buy")
        self.assertEqual(_phoenix_direction_hint("Wrzucę teraz, dół"), "sell")
        self.assertEqual(_phoenix_direction_hint("wrzuce teraz gora"), "buy")
        self.assertEqual(_phoenix_direction_hint("wrzuce teraz dol"), "sell")
        self.assertTrue(_is_phoenix_direction_runner_announcement("Wrzucę teraz, góra"))
        self.assertTrue(_is_phoenix_direction_runner_announcement("wrzuce teraz dol"))
        self.assertFalse(_is_phoenix_direction_runner_announcement("Rynek może iść w dół"))

    def test_phoenix_direction_hint_must_agree_with_levels(self):
        text = """XAUUSD

ENTRY 4030/4038
SL 4045
TP 4028
TP 4027
TP 4026"""
        accepted = _parse_signal(text, "phoenix:hint", -1002864291293, "PHOENIX VIP", "", 1, side_hint="sell")
        rejected = _parse_signal(text, "phoenix:wrong", -1002864291293, "PHOENIX VIP", "", 2, side_hint="buy")

        self.assertIsNotNone(accepted)
        self.assertEqual(accepted.side, "sell")
        self.assertIsNone(rejected)

    def test_blue_pips_now_range_without_explicit_side(self):
        signal = _parse_signal(
            """✅XAU/USD 🔤🔤 NOW 4142 - 4138

📊TP¹ _ 4147

📊TP² _ 4152

📊TP³ _ 4156

📊TP⁴ _ 4160🫣(OPEN)


🚨 STOP LOSS 4130""",
            "blue:20746",
            -1001982510222,
            "𝐁𝐋𝐔𝐄 𝐏𝐈𝐏'𝐒 𝐗𝐀𝐔-𝐔𝐒𝐃 𝐒𝐈𝐆𝐍𝐀𝐋",
            "",
            20746,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "buy")
        self.assertEqual(signal.entries, [4138.0, 4140.0, 4142.0])
        self.assertEqual(signal.sl, 4130.0)
        self.assertEqual(signal.tps[:4], [4147.0, 4152.0, 4156.0, 4160.0])
        self.assertEqual(_channel_strategy(signal).name, "blue_pips_zone_3_limits_tp1_tp2_tp4_be")

    def test_blue_pips_entry_point_take_profit_format(self):
        signal = _parse_signal(
            """XAU/USD SELL ENTRY POINT 4530 TO 4534

📊 ¹/TAKE PROFIT      4526 ✅
📊 ²/TAKE PROFIT      4522 ✅
📊 ³/TAKE PROFIT      4518 ✅
📊 ⁴/TAKE PROFIT      4514 OPEN

❌STOP LOSS             4540

 ✅ USE PROPER LOT SIZE 😄😄""",
            "blue:20452",
            -1001982510222,
            "𝐁𝐋𝐔𝐄 𝐏𝐈𝐏'𝐒 𝐗𝐀𝐔-𝐔𝐒𝐃 𝐒𝐈𝐆𝐍𝐀𝐋",
            "",
            20452,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entries, [4530.0, 4532.0, 4534.0])
        self.assertEqual(signal.sl, 4540.0)
        self.assertEqual(signal.tps[:4], [4526.0, 4522.0, 4518.0, 4514.0])

    def test_gold_signal_provide_slash_range(self):
        signal = _parse_signal(
            """Gold Buy Now 4315 // 4311

Tp Level¹ :   4317
Tp Level² :   4319
Tp Level³ :   4321
Tp Level⁴ :   4323
Tp Level⁵ :   4325


SL Level      4306""",
            "fffdos:1933",
            -1003772508199,
            "𝗚𝗢𝗟𝗗 𝗦𝗜𝗚𝗡𝗔𝗟 𝗣𝗥𝗢𝗩𝗜𝗗𝗘",
            "",
            1933,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "buy")
        self.assertEqual(signal.entries, [4311.0, 4313.0, 4315.0])
        self.assertEqual(signal.sl, 4306.0)
        self.assertEqual(signal.tps[:4], [4317.0, 4319.0, 4321.0, 4323.0])
        self.assertEqual(_channel_strategy(signal).name, "gold_signal_provide_zone_3_limits_tp1_tp2_tp4_be")

    def test_gold_signal_provide_rejects_fat_finger_gold_entry(self):
        signal = _parse_signal(
            """Gold Sell Now 43147 // 4352

Tp Level¹ :   4344
Tp Level² :   4342
Tp Level³ :   4340
Tp Level⁴ :   4338
Tp Level⁵ :   4336


SL Level      4355""",
            "fffdos:1867",
            -1003772508199,
            "𝗚𝗢𝗟𝗗 𝗦𝗜𝗚𝗡𝗔𝗟 𝗣𝗥𝗢𝗩𝗜𝗗𝗘",
            "",
            1867,
        )

        self.assertIsNone(signal)

    def test_us30_text_overrides_nas100_channel_title(self):
        signal = _parse_signal(
            """US30 SELL 52280
SL 52410
TP 52250
TP 52180
TP 52080
TP 51780""",
            "-1001232813229:6923",
            -1001232813229,
            "Vip Nas100 Pro",
            "",
            6923,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.asset, "us30")
        self.assertEqual(_channel_strategy(signal).name, "vipnas100_60d_tp1_tp1_tp2_be_after_tp1_wide_market")

    def test_phoenix_vip_infers_buy_from_reversed_entry_range(self):
        signal = self.parse(
            """XAUUSD

ENTRY 4208-4215
SL 4203
TP 4217
TP 4219
TP 4220

Material ma charakter edukacyjny!"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "buy")
        self.assertEqual(signal.entries, [4208.0, 4211.5, 4215.0])

    def test_phoenix_vip_infers_sell_from_reversed_entry_range(self):
        signal = self.parse(
            """XAUUSD

ENTRY 4226-4217
SL 4230
TP 4215
TP 4213
TP 4212

Material ma charakter edukacyjny!"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "sell")
        self.assertEqual(signal.entries, [4217.0, 4221.5, 4226.0])

    def test_entry_at_symbol_and_numbered_take_profits(self):
        signal = self.parse(
            """XAUUSD BUY

ENTRY: @4285

S.L.4273

TAKE PROFITS

1 4290.00
2 4294.00
3 4298.00
4 4302.00
5 4306.00
6 4310.00
7 4330.00"""
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "buy")
        self.assertEqual(signal.entry, 4285.0)
        self.assertEqual(signal.sl, 4273.0)
        self.assertEqual(signal.tps, [4290.0, 4294.0, 4298.0, 4302.0, 4306.0, 4310.0, 4330.0])

    def test_market_gold_levels_are_shifted_to_broker_quote_basis(self):
        signal = self.parse("XAUUSD SELL 4048/4051 SL 4061 TP1 4045 TP2 4042 TP3 4039")

        normalized, shift = _normalize_gold_provider_quote_basis(signal, 4019.49)

        self.assertAlmostEqual(shift, -28.51)
        self.assertEqual(normalized.entries, [4019.49, 4020.99, 4022.49])
        self.assertAlmostEqual(normalized.sl, 4032.49)
        self.assertEqual(normalized.tps, [4016.49, 4013.49, 4010.49])

    def test_ghp_gold_rejects_implausible_120_usd_stop(self):
        signal = _parse_signal(
            "Gold buy Entry 4126 SL 4006 TP 4136 TP 4146",
            "-1001958009741:1",
            -1001958009741,
            "GHP VIP-JACKPOT FX",
            "",
            1,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(
            _ghp_gold_sanity_reason(signal, 4126.0),
            "ghp_gold_implausible_stop_distance",
        )

    def test_ghp_gold_accepts_near_market_scalp_levels(self):
        signal = _parse_signal(
            "Gold sell Entry 4126 SL 4140 TP 4117 TP 4110",
            "-1001958009741:2",
            -1001958009741,
            "GHP VIP-JACKPOT FX",
            "",
            2,
        )

        self.assertIsNotNone(signal)
        self.assertIsNone(_ghp_gold_sanity_reason(signal, 4125.5))

    def test_ghp_gold_waits_for_edit_when_stop_is_suspiciously_tight(self):
        signal = _parse_signal(
            "Gold buy Entry 4355.92 SL 4355.5 TP 4375 TP 4400 TP 4433",
            "-1001958009741:3",
            -1001958009741,
            "GHP VIP-JACKPOT FX",
            "",
            3,
        )

        self.assertIsNotNone(signal)
        self.assertEqual(
            _ghp_gold_sanity_reason(signal, 4356.0),
            "ghp_gold_suspicious_tight_stop_wait_for_edit",
        )

        corrected = signal.__class__(**{**signal.__dict__, "sl": 4335.5})
        self.assertIsNone(_ghp_gold_sanity_reason(corrected, 4356.0))

    def test_explicit_gold_limit_is_not_shifted(self):
        signal = self.parse("XAUUSD SELL LIMIT 4048 SL 4061 TP1 4045 TP2 4042")

        normalized, shift = _normalize_gold_provider_quote_basis(signal, 4019.49)

        self.assertEqual(shift, 0.0)
        self.assertEqual(normalized, signal)

    def test_explicit_phoenix_limit_is_not_repaired_against_current_market(self):
        signal = self.parse(
            "XAUUSD SELL LIMIT! ENTRY 4320/4328 SL 4340 TP 4318 TP 4317 TP 4316"
        )

        repaired = _repair_gold_hundred_digit_typo(signal, 4297.54)

        self.assertEqual(repaired, signal)

    def test_explicit_phoenix_pending_uses_long_expiry_and_no_pre_entry_tp_cancel(self):
        cfg = SimpleNamespace(signal_pending_expiry_minutes=5.0)
        managed = {
            "strategy": "phoenix_zone_tp1_tp2_tp4_signal_sl_delayed_be",
            "chat_title": "PHOENIX VIP",
            "provider_explicit_pending": True,
            "strategy_pending_expiry_minutes": 5.0,
        }
        with patch.dict("os.environ", {"PHOENIX_EXPLICIT_PENDING_EXPIRY_MINUTES": "120"}, clear=False):
            self.assertEqual(_pending_expiry_minutes(cfg, managed), 120.0)
        self.assertFalse(_pending_price_cancellation_allowed(managed))
        self.assertTrue(
            _pending_price_cancellation_allowed(
                {"strategy": "generic", "chat_title": "OTHER", "provider_explicit_pending": False}
            )
        )

    def test_implausible_gold_reward_is_not_shifted(self):
        signal = self.parse("XAUUSD SELL 4048 SL 4061 TP1 3940")

        normalized, shift = _normalize_gold_provider_quote_basis(signal, 4019.49)

        self.assertEqual(shift, 0.0)
        self.assertEqual(normalized, signal)

    def test_ghp_gold_uses_tp1_tp2_and_deep_runner_calibration(self):
        signal = _parse_signal(
            "Gold buy at 4470 SL 4455 TP 4480 TP 4490 TP 4510",
            "-1001958009741:1",
            -1001958009741,
            "GHP VIP-JACKPOT FX",
            "",
            1,
        )
        self.assertIsNotNone(signal)
        strategy = _channel_strategy(signal)
        self.assertEqual(strategy.name, "ghp_gold_tp1_tp2_deep_original_sl_be_60m")
        self.assertTrue(strategy.force_all_entries)
        self.assertFalse(strategy.allow_market_runner)
        self.assertEqual(strategy.split_target_indices, (1, 2, 99))
        self.assertEqual(strategy.split_protect_modes, ("none", "be", "be"))

    def test_ghp_indices_uses_tp1_original_sl_calibration(self):
        signal = _parse_signal(
            "BTCUSD BUY NOW ENTRY 81150 SL 79500 TP 81400 TP 81700",
            "-1003306025363:1",
            -1003306025363,
            "GHP VIP-JACKPOT INDICES & CRYPTO",
            "",
            1,
        )
        self.assertIsNotNone(signal)
        strategy = _channel_strategy(signal)
        self.assertEqual(strategy.name, "ghp_tp1_original_sl_90s")
        self.assertEqual(strategy.split_target_indices, (1,))
        self.assertEqual(strategy.split_protect_modes, ("none",))

    def test_ghp_currency_uses_tp1_only_with_original_sl(self):
        signal = _parse_signal(
            "EURJPY SELL 184.970 SL 185.300 TP1 184.720 TP2 184.550",
            "-1003495213392:2321",
            -1003495213392,
            "GHP VIP-JACKPOT CURRENCY FX",
            "",
            2321,
        )
        self.assertIsNotNone(signal)
        strategy = _channel_strategy(signal)
        self.assertEqual(strategy.name, "ghp_currency_tp1_original_sl_60m")
        self.assertEqual(strategy.split_target_indices, (1,))
        self.assertEqual(strategy.split_protect_modes, ("none",))

    def test_ghp_explicit_levels_are_never_shifted_to_market(self):
        signal = _parse_signal(
            "Gold sell Entry 4415/4416 SL 4430 TP 4400 TP 4385 TP 4365",
            "-1001958009741:99",
            -1001958009741,
            "GHP VIP-JACKPOT FX",
            "",
            99,
        )
        self.assertIsNotNone(signal)
        normalized, shift = _normalize_gold_provider_quote_basis(signal, 4397.0)
        self.assertEqual(shift, 0.0)
        self.assertEqual(normalized, signal)

    def test_ghp_currency_defers_provider_be_before_tp1(self):
        managed = {"side": "sell", "tps": [184.72, 184.55], "protected_to_tp1": False}
        self.assertTrue(_defer_ghp_currency_provider_be(-1003495213392, managed, 184.90))
        self.assertFalse(_defer_ghp_currency_provider_be(-1003495213392, managed, 184.70))
        self.assertFalse(_defer_ghp_currency_provider_be(-1001958009741, managed, 184.90))

    def test_channel_asset_allowlist_only_filters_configured_channel(self):
        mapping = '{"-1003306025363":["ger40"]}'
        with patch.dict("os.environ", {"SIGNAL_CHANNEL_ASSET_ALLOWLIST": mapping}, clear=False):
            self.assertTrue(_channel_asset_allowed(-1003306025363, "ger40"))
            self.assertFalse(_channel_asset_allowed(-1003306025363, "btc"))
            self.assertTrue(_channel_asset_allowed(-1001958009741, "gold"))

    def test_planned_market_reward_risk_uses_absolute_stop_distance(self):
        self.assertAlmostEqual(_planned_market_reward_risk(4100.0, 4110.0, 4097.5), 0.25)
        self.assertEqual(_planned_market_reward_risk(4100.0, 4100.0, 4097.5), 0.0)

    def test_profile_symbol_map_has_priority_over_generic_candidates(self):
        mapping = '{"wti":"USOUSD.s","ger40":"GER40.s"}'
        with patch.dict("os.environ", {"SIGNAL_ASSET_SYMBOL_MAP": mapping}, clear=False):
            with patch("app.telegram_signal_bot.ensure_symbol", side_effect=lambda value: value) as resolver:
                self.assertEqual(_ensure_asset_symbol("wti"), "USOUSD.s")
                resolver.assert_called_once_with("USOUSD.s")

    def test_wti_generic_candidates_never_use_axti_stock(self):
        with patch.dict("os.environ", {"SIGNAL_ASSET_SYMBOL_MAP": ""}, clear=False):
            with patch("app.telegram_signal_bot.ensure_symbol", side_effect=lambda value: value) as resolver:
                self.assertEqual(_ensure_asset_symbol("wti"), "USOUSD")
                tried = [call.args[0] for call in resolver.call_args_list]
                self.assertNotIn("AXTIUSD", tried)

    def test_generic_signal_uses_configured_risk_per_leg(self):
        with patch.dict("os.environ", {"SIGNAL_RISK_PCT_PER_LEG": "1.50"}, clear=False):
            self.assertEqual(_signal_per_leg_risk_pct(False), 1.5)

    def test_phoenix_can_override_generic_risk_per_leg(self):
        with patch.dict(
            "os.environ",
            {"SIGNAL_RISK_PCT_PER_LEG": "1.50", "PHOENIX_RISK_PCT_PER_LEG": "1.25"},
            clear=False,
        ):
            self.assertEqual(_signal_per_leg_risk_pct(True), 1.25)


if __name__ == "__main__":
    unittest.main()
