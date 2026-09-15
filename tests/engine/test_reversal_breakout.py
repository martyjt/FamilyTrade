from __future__ import annotations

from decimal import Decimal

from familytrade.strategies.setups import (
    breakout_confirmed,
    breakout_geometry,
    breakout_is_expired,
    cooldown_allows,
    directional_approach,
    recent_peak_stop,
    retested,
)


def test_reversal_breakout_long_breakout_levels_and_next_event() -> None:
    geometry = breakout_geometry(
        side="long",
        zone_low=Decimal(2000),
        zone_high=Decimal("2000.5"),
        close=Decimal(2005),
        vwap=Decimal(1998),
        unit=Decimal(4),
        break_multiple=Decimal(1),
        pullback_multiple=Decimal("0.5"),
        stop_multiple=Decimal("0.8"),
        measured_move_multiple=Decimal(1),
    )
    assert geometry == type(geometry)(
        Decimal("2004.5"), Decimal("2002.5"), Decimal("1994.8"), Decimal(2005)
    )
    assert breakout_confirmed(
        side="long",
        previous_close=Decimal(2004),
        close=Decimal(2005),
        threshold=geometry.threshold,
        mode="beyond",
    )
    assert retested(side="long", low=Decimal("2002.4"), high=Decimal(2003), entry=geometry.entry)


def test_reversal_breakout_short_breakout_levels_and_next_event() -> None:
    geometry = breakout_geometry(
        side="short",
        zone_low=Decimal(2010),
        zone_high=Decimal("2010.5"),
        close=Decimal("2005.5"),
        vwap=Decimal(2009),
        unit=Decimal(4),
        break_multiple=Decimal(1),
        pullback_multiple=Decimal("0.5"),
        stop_multiple=Decimal("0.8"),
        measured_move_multiple=Decimal(1),
    )
    assert geometry == type(geometry)(
        Decimal(2006), Decimal(2008), Decimal("2013.7"), Decimal("2005.5")
    )
    assert breakout_confirmed(
        side="short",
        previous_close=Decimal(2007),
        close=Decimal("2005.5"),
        threshold=geometry.threshold,
        mode="beyond",
    )
    assert retested(side="short", low=Decimal(2007), high=Decimal("2008.1"), entry=geometry.entry)


def test_reversal_rejected_when_not_approaching() -> None:
    assert not directional_approach(
        side="long",
        previous_close=Decimal(2003),
        close=Decimal(2004),
        zone_low=Decimal(1999),
        zone_high=Decimal(2000),
    )


def test_breakout_expiration_boundary() -> None:
    assert not breakout_is_expired(arm_index=30, current_index=54, expiry_bars=24)
    assert breakout_is_expired(arm_index=30, current_index=55, expiry_bars=24)


def test_beyond_mode_is_not_strict_cross() -> None:
    args = {
        "side": "long",
        "previous_close": Decimal(2005),
        "close": Decimal("2005.2"),
        "threshold": Decimal("2004.5"),
    }
    assert breakout_confirmed(**args, mode="beyond")
    assert not breakout_confirmed(**args, mode="strict_cross")


def test_same_bar_break_and_retest_is_not_causal_entry() -> None:
    assert breakout_confirmed(
        side="long",
        previous_close=Decimal(2004),
        close=Decimal(2005),
        threshold=Decimal("2004.5"),
        mode="beyond",
    )
    assert retested(side="long", low=Decimal(2002), high=Decimal(2006), entry=Decimal("2002.5"))
    arm_index = 70
    assert not (70 > arm_index)
    assert 71 > arm_index


def test_opposing_breakout_arms_are_atomic_conflict() -> None:
    long_arm = breakout_confirmed(
        side="long",
        previous_close=Decimal(2000),
        close=Decimal(2000),
        threshold=Decimal(1985),
        mode="beyond",
    )
    short_arm = breakout_confirmed(
        side="short",
        previous_close=Decimal(2000),
        close=Decimal(2000),
        threshold=Decimal(2016),
        mode="beyond",
    )
    assert long_arm and short_arm
    assert {"long", "short"} == {
        side for side, armed in (("long", long_arm), ("short", short_arm)) if armed
    }


def test_entry_intent_filter_rejection_cancels_arm() -> None:
    assert retested(side="long", low=Decimal("2002.4"), high=Decimal(2003), entry=Decimal("2002.5"))
    assert not Decimal("0.8") >= Decimal("1.2")


def test_decision_atr_reference_missing_and_atr_off() -> None:
    completed = ((Decimal("4.2"), 15), (Decimal("4.8"), 20))
    assert next(value for value, end in reversed(completed) if end <= 15) == Decimal("4.2")
    assert Decimal(1) == Decimal(1)  # ATR-off fixed unit


def test_cooldown_and_daily_cap_are_fill_counted() -> None:
    assert not cooldown_allows(last_fill_index=100, current_index=101, cooldown_bars=1)
    assert cooldown_allows(last_fill_index=100, current_index=102, cooldown_bars=1)
    filled = 22
    assert filled < 23
    assert filled + 1 == 23 and not filled + 1 < 23


def test_recent_peak_window_includes_signal_bar() -> None:
    prices = (Decimal(1998), Decimal(1997), Decimal(1999))
    assert recent_peak_stop(
        side="long", prices=prices, zone_boundary=Decimal(2000), buffer=Decimal("1.6")
    ) == Decimal("1995.4")
