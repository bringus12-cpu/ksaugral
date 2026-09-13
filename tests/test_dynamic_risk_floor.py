from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app import xau_scalp_bot


class DynamicRiskFloorTests(TestCase):
    def test_scalper_account_risk_base_can_use_equity(self):
        account = SimpleNamespace(balance=1000.0, equity=1450.0)
        with patch.dict("os.environ", {"POSITION_RISK_BASE": "equity"}):
            self.assertEqual(xau_scalp_bot._account_risk_base(account), 1450.0)

    def _lot(self, balance: float) -> float:
        cfg = SimpleNamespace(min_lot=0.01, max_lot=999.0)
        signal = {"entry": 4000.0, "sl": 3997.0, "side": "buy"}
        with (
            patch.dict(
                "os.environ",
                {"XAU_SCALP_RISK_PCT": "3", "XAU_SCALP_MIN_LEG_LOT": "0.10"},
            ),
            patch.object(xau_scalp_bot, "calc_loss_per_lot", return_value=300.0),
            patch.object(
                xau_scalp_bot,
                "normalize_volume",
                side_effect=lambda _symbol, value, _minimum, _maximum: value,
            ),
        ):
            return xau_scalp_bot._xau_scalp_risk_leg_lot(
                "XAUUSD",
                signal,
                balance,
                cfg,
                6,
            )

    def test_scalper_risk_lot_keeps_requested_minimum(self):
        self.assertEqual(self._lot(1000.0), 0.10)

    def test_scalper_risk_lot_grows_above_minimum(self):
        self.assertEqual(round(self._lot(10000.0), 4), 0.1667)

    def test_explicit_per_leg_risk_is_not_split_across_legs(self):
        cfg = SimpleNamespace(min_lot=0.01, max_lot=999.0)
        signal = {"entry": 4000.0, "sl": 3997.0, "side": "buy"}
        with (
            patch.dict(
                "os.environ",
                {
                    "XAU_SCALP_RISK_PCT": "0",
                    "XAU_SCALP_RISK_PCT_PER_LEG": "1.5",
                    "XAU_SCALP_MIN_LEG_LOT": "0.01",
                },
            ),
            patch.object(xau_scalp_bot, "calc_loss_per_lot", return_value=300.0),
            patch.object(
                xau_scalp_bot,
                "normalize_volume",
                side_effect=lambda _symbol, value, _minimum, _maximum: value,
            ),
        ):
            lot = xau_scalp_bot._xau_scalp_risk_leg_lot("XAUUSD", signal, 1000.0, cfg, 3)
        self.assertEqual(round(float(lot), 4), 0.05)
