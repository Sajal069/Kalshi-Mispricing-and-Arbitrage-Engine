"""Unit, fee and price-grid arithmetic.

The fee tests pin every figure that appears in the published schedule. If Kalshi
changes the schedule these tests are supposed to fail loudly -- a silent fee
change is one of the named risks in the plan.
"""

from decimal import Decimal
from fractions import Fraction

import pytest

from kima.fees import FeeSchedule, calibrate_rounding, schedule_for
from kima.ticks import KNOWN_STRUCTURES, PriceGrid
from kima.units import (
    NOTIONAL_CC,
    complement_cc,
    contracts_to_cq,
    cost_mu,
    dollars_to_cc,
    payoff_mu,
)


class TestUnits:
    def test_dollar_parsing_is_exact(self):
        assert dollars_to_cc("0.5") == 5_000
        assert dollars_to_cc("0.0001") == 1
        assert dollars_to_cc("1.00") == NOTIONAL_CC

    def test_rejects_sub_centicent_prices(self):
        # A price finer than our unit means the unit choice is wrong; we would
        # rather crash than silently round a price we might trade on.
        with pytest.raises(ValueError):
            dollars_to_cc("0.00005")

    def test_quantity_granularity(self):
        assert contracts_to_cq("1") == 100
        assert contracts_to_cq("0.01") == 1
        with pytest.raises(ValueError):
            contracts_to_cq("0.001")

    def test_complement_identity(self):
        # YES bid at X is a NO ask at 1 - X.
        assert complement_cc(3_700) == 6_300
        assert complement_cc(complement_cc(1234)) == 1234

    def test_money_arithmetic_is_exact(self):
        # 100 contracts at $0.50 costs $50 exactly.
        assert cost_mu(5_000, contracts_to_cq(100)) == 50_000_000
        assert payoff_mu(contracts_to_cq(100)) == 100_000_000


class TestFees:
    """Every number here is quoted in the published fee schedule."""

    @pytest.mark.parametrize(
        "price_cc,expected_dollars",
        [(5_000, "1.75"), (9_000, "0.63"), (1_000, "0.63"), (9_900, "0.07"), (100, "0.07")],
    )
    def test_taker_fee_per_100_contracts(self, price_cc, expected_dollars):
        sched = FeeSchedule()
        fee = sched.taker_fee_mu(contracts_to_cq(100), price_cc)
        assert Decimal(fee) / 1_000_000 == Decimal(expected_dollars)

    def test_fee_is_symmetric_in_price(self):
        # P(1-P) is symmetric about 0.5, so a NO leg costs the same as its YES twin.
        sched = FeeSchedule()
        q = contracts_to_cq(37)
        for p in (100, 2_500, 4_900):
            assert sched.taker_fee_mu(q, p) == sched.taker_fee_mu(q, complement_cc(p))

    def test_maker_defaults_to_zero(self):
        assert FeeSchedule().maker_fee_mu(contracts_to_cq(100), 5_000) == 0

    def test_maker_is_a_quarter_of_taker_when_enabled(self):
        sched = FeeSchedule(taker_multiplier=Fraction(1), maker_multiplier=Fraction(1), rounding="none")
        q = contracts_to_cq(100)
        assert sched.maker_fee_mu(q, 5_000) * 4 == sched.taker_fee_mu(q, 5_000)

    def test_rounding_is_per_order_not_per_contract(self):
        """The key structural fact: per-contract fee decreases with order size."""
        sched = FeeSchedule()
        one = sched.taker_fee_mu(contracts_to_cq(1), 5_000)
        hundred = sched.taker_fee_mu(contracts_to_cq(100), 5_000)
        assert one == 20_000            # $0.02 on a $0.50 contract -- 4%
        assert hundred == 1_750_000     # $0.0175 per contract
        assert one * 100 > hundred      # small orders are punished

    def test_zero_fee_series(self):
        for series in ("KXBTCY", "KXETHY"):
            sched = schedule_for(series)
            assert sched.is_zero_fee
            assert sched.taker_fee_mu(contracts_to_cq(1_000), 5_000) == 0

    def test_fee_never_decreases_with_size(self):
        sched = FeeSchedule()
        prev = -1
        for c in range(1, 200):
            fee = sched.taker_fee_mu(contracts_to_cq(c), 4_200)
            assert fee >= prev
            prev = fee

    def test_calibration_recovers_the_rounding_rule(self):
        truth = FeeSchedule(rounding="centicent")
        obs = [
            (contracts_to_cq(c), p, Fraction(1), truth.taker_fee_mu(contracts_to_cq(c), p))
            for c in (1, 3, 17, 100)
            for p in (1_200, 5_000, 8_800)
        ]
        cal = calibrate_rounding(obs)
        assert cal.best_rounding == "centicent"
        assert cal.ok


class TestPriceGrid:
    def test_penny_grid(self):
        g = PriceGrid.penny()
        assert g.is_on_grid(5_000)
        assert not g.is_on_grid(5_050)
        assert g.n_levels() == 101

    def test_tapered_grid_has_fine_tails(self):
        g = KNOWN_STRUCTURES["center_deci_edge_centi_cent"]
        assert g.is_on_grid(550)      # centicent tick below $0.10
        assert not g.is_on_grid(5_005)  # decicent tick in the middle
        assert g.round_up(5_005) == 5_010
        assert g.round_down(5_005) == 5_000
        assert g.is_on_grid(9_501)    # centicent tick above $0.90

    def test_from_api_price_ranges(self):
        g = PriceGrid.from_api(
            [
                {"start": "0.00", "end": "0.10", "step": "0.0001"},
                {"start": "0.10", "end": "0.90", "step": "0.001"},
                {"start": "0.90", "end": "1.00", "step": "0.0001"},
            ]
        )
        assert g.min_step_cc == 1
        assert g.is_on_grid(1_234) is False
        assert g.is_on_grid(1_230) is True
