import unittest

from app.dashboard import _clean_channel_lot_sizes, _parse_channel_lot_sizes


class DashboardChannelLotTests(unittest.TestCase):
    def test_parse_channel_lot_sizes_keeps_valid_values(self):
        raw = '{"Gold_btcusd_xauusd":0.01,"bad":"x","too_large":101}'

        self.assertEqual(_parse_channel_lot_sizes(raw), {"Gold_btcusd_xauusd": 0.01})

    def test_clean_channel_lot_sizes_uses_canonical_channel_name(self):
        result = _clean_channel_lot_sizes(
            {"https://t.me/gold_btcusd_xauusd/4455": "0.02", "removed": 0.5},
            ["Gold_btcusd_xauusd"],
        )

        self.assertEqual(result, {"Gold_btcusd_xauusd": 0.02})

    def test_clean_channel_lot_sizes_rejects_broker_invalid_value(self):
        with self.assertRaises(ValueError):
            _clean_channel_lot_sizes({"Gold_btcusd_xauusd": 0.001}, ["Gold_btcusd_xauusd"])


if __name__ == "__main__":
    unittest.main()
