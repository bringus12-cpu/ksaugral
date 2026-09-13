import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.channel_analyzer import build_channel_report


class ChannelAnalyzerTests(unittest.TestCase):
    def test_groups_parser_execution_skips_and_closed_results(self) -> None:
        now = datetime.now(UTC)
        signal = {"chat_id": -1002864291293, "chat_title": "PHOENIX VIP", "message_id": 101}
        events = [
            {"timestamp_utc": now.isoformat(), "type": "signal", "signal": signal},
            {"timestamp_utc": now.isoformat(), "type": "order_attempt", "retcode": 10009, "signal": signal},
            {"timestamp_utc": now.isoformat(), "type": "skip", "reason": "late_signal", "signal": signal},
            {"timestamp_utc": now.isoformat(), "type": "unparsed_signal_candidate", "chat_id": -200, "chat_title": "TEST", "message_id": 9},
        ]
        state = {
            "adaptive_learning": {
                "closed_positions": {
                    "1": {"channel": "phoenixvip", "outcome": "win", "profit": 12.5, "closed_utc": now.isoformat()}
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
            report = build_channel_report(path, state, ["-1002864291293", "test_channel"], now=now)

        phoenix = next(item for item in report["channels"] if item["chat_id"] == -1002864291293)
        self.assertEqual(2, report["configured_count"])
        self.assertEqual(1, phoenix["recognized"])
        self.assertEqual(1, phoenix["executed_signals"])
        self.assertEqual(1, phoenix["successful_orders"])
        self.assertEqual(1, phoenix["wins"])
        self.assertEqual(12.5, phoenix["pnl"])
        self.assertEqual({"late_signal": 1}, phoenix["skip_reasons"])
        self.assertEqual(1, report["totals"]["unparsed"])


if __name__ == "__main__":
    unittest.main()
