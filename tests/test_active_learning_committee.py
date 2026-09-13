from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.active_learning_committee import analyze_phoenix, build_snapshot


class ActiveLearningCommitteeTests(TestCase):
    def test_analyze_phoenix_flags_correlated_pre_signal_exposure(self) -> None:
        events = []
        for leg in range(1, 10):
            events.append(
                {
                    "timestamp_utc": "2026-08-06T15:13:45+00:00",
                    "type": "phoenix_range_trigger_attempt" if leg <= 6 else "phoenix_range_optimized_extra_attempt",
                    "chat_id": -1002864291293,
                    "message_id": 10844,
                    "side": "buy",
                    "order_kind": "market" if leg <= 3 else "limit",
                    "volume": 0.12,
                    "retcode": 10009,
                }
            )
        events.extend(
            [
                {
                    "timestamp_utc": "2026-08-06T15:27:14+00:00",
                    "type": "signal",
                    "signal": {
                        "chat_id": -1002864291293,
                        "chat_title": "PHOENIX VIP",
                        "message_id": 10846,
                        "side": "buy",
                        "entries": [4250.0, 4253.5, 4257.0],
                        "sl": 4245.0,
                        "tps": [4259.0, 4260.0],
                    },
                },
                {
                    "timestamp_utc": "2026-08-06T15:27:14+00:00",
                    "type": "phoenix_entry_brain",
                    "signal": {"chat_id": -1002864291293, "message_id": 10846},
                    "brain": {"decision": "fresh_zone"},
                },
            ]
        )

        result = analyze_phoenix(events)

        self.assertEqual(result["latest_signal"]["message_id"], 10846)
        self.assertEqual(result["latest_signal"]["successful_range_legs"], 9)
        self.assertEqual(result["latest_signal"]["successful_range_volume"], 1.08)
        self.assertEqual(result["anomalies"][0]["code"], "phoenix_correlated_pre_signal_exposure")

    def test_build_snapshot_is_observation_only(self) -> None:
        with TemporaryDirectory() as temporary:
            tmp_path = Path(temporary)
            data = tmp_path / "data_vantage"
            data.mkdir()
            now = datetime(2026, 8, 6, 16, 0, tzinfo=UTC)
            state = data / "telegram_signal_state.json"
            state.write_text('{"adaptive_learning":{"channels":{}}}', encoding="utf-8")
            status = data / "xau_scalp_status.json"
            status.write_text(
                '{"heartbeat_utc":"2026-08-06T16:00:00+00:00","strategy":"XAU-SCALP","day_realized_profit":10}',
                encoding="utf-8",
            )

            snapshot = build_snapshot(tmp_path, now=now)

            self.assertEqual(snapshot["mode"], "observation_only")
            self.assertEqual(snapshot["automatic_changes_applied"], [])
            self.assertTrue(any(member["key"] == "learning_chair" for member in snapshot["members"]))
