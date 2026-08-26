from decimal import Decimal

from src.ewma_twap_fair import (
    EwmaTwapFairValue,
    EwmaTwapSettings,
    choose_ewma_twap_decision,
    ewma_twap_fair_value,
)


def sample_prices() -> list[tuple[float, Decimal]]:
    return [
        (0.0, Decimal("100")),
        (1.0, Decimal("100.02")),
        (3.0, Decimal("99.99")),
        (6.0, Decimal("100.03")),
    ]


def test_ewma_twap_uses_terminal_average_variance_clock() -> None:
    result = ewma_twap_fair_value(
        sample_prices(),
        Decimal("100"),
        Decimal("100"),
        Decimal("300"),
        EwmaTwapSettings(),
        Decimal("0.00005"),
    )

    assert result is not None
    assert result.effective_variance_seconds == Decimal("260")
    assert Decimal("0.49") < result.probability_up < Decimal("0.51")


def test_ewma_twap_buys_discounted_side_with_fee_aware_kelly_cap() -> None:
    fair = EwmaTwapFairValue(
        probability_up=Decimal("0.50"),
        probability_down=Decimal("0.50"),
        sigma_per_sqrt_second=Decimal("0.0001"),
        effective_variance_seconds=Decimal("260"),
        z_score=Decimal("0"),
    )

    decision = choose_ewma_twap_decision(
        fair,
        Decimal("0.42"),
        Decimal("0.59"),
        Decimal("296"),
        EwmaTwapSettings(),
    )

    assert decision is not None
    assert decision.side == "UP"
    assert decision.notional <= Decimal("25")
    assert decision.net_model_edge >= Decimal("0.015")
    assert decision.fee_per_share == Decimal("0.017052")


def test_ewma_twap_rejects_edge_that_does_not_cover_real_taker_fee() -> None:
    fair = EwmaTwapFairValue(
        probability_up=Decimal("0.50"),
        probability_down=Decimal("0.50"),
        sigma_per_sqrt_second=Decimal("0.0001"),
        effective_variance_seconds=Decimal("260"),
        z_score=Decimal("0"),
    )

    assert (
        choose_ewma_twap_decision(
            fair,
            Decimal("0.4775"),
            Decimal("0.53"),
            Decimal("296"),
            EwmaTwapSettings(),
        )
        is None
    )


def test_ewma_twap_stops_before_final_twap_interval() -> None:
    fair = EwmaTwapFairValue(
        probability_up=Decimal("0.60"),
        probability_down=Decimal("0.40"),
        sigma_per_sqrt_second=Decimal("0.0001"),
        effective_variance_seconds=Decimal("35"),
        z_score=Decimal("1"),
    )

    assert (
        choose_ewma_twap_decision(
            fair,
            Decimal("0.40"),
            Decimal("0.61"),
            Decimal("75"),
            EwmaTwapSettings(),
        )
        is None
    )
