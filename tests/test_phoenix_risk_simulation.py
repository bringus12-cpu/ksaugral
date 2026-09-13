import unittest
from datetime import datetime, timedelta

from scripts.simulate_phoenix_per_leg_risk import simulate


class PhoenixRiskSimulationTests(unittest.TestCase):
    def test_sizes_each_leg_from_current_balance(self):
        opened = datetime(2026, 1, 1, 10, 0)
        trades = [
            {
                "module": "phoenix_full",
                "message_id": 1,
                "entry_time": opened,
                "exit_time": opened + timedelta(minutes=1),
                "entry": 4000.0,
                "sl": 3995.0,
                "pnl_001": 50.0,
                "status": "target",
                "target_index": 1,
            },
            {
                "module": "phoenix_full",
                "message_id": 2,
                "entry_time": opened + timedelta(minutes=2),
                "exit_time": opened + timedelta(minutes=3),
                "entry": 4000.0,
                "sl": 3995.0,
                "pnl_001": -5.06,
                "status": "stop",
                "target_index": 1,
            },
        ]
        report = simulate(trades, 1000.0, 1.5, 0.06)
        self.assertEqual(report["trades"][0]["lot"], 0.02)
        self.assertEqual(report["trades"][1]["lot"], 0.03)
        self.assertAlmostEqual(report["final_balance"], 1000.0 + 100.0 - 15.18, places=2)


if __name__ == "__main__":
    unittest.main()
