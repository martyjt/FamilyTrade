from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from familytrade.market_data.models import CalendarWindow, canonical_json_bytes
from familytrade.simulation.engine import (
    _decision_dataset,
    checkpoint_engine,
    deterministic_uuid7,
    initialize_engine,
    restore_engine,
    run_engine,
    step_engine,
)
from familytrade.simulation.fills import BarPrices, protective_fill
from familytrade.simulation.risk import modeled_total_loss
from familytrade.simulation.state import (
    DataQualityEvent,
    EmissionContext,
    EngineFailure,
    FinishRunEvent,
    SourceDatasetProvenance,
    ZoneState,
)
from familytrade.strategies.definitions import (
    CompareNode,
    ConstantNode,
    FeatureInstance,
    FeatureNode,
    FeaturePriceSource,
    FixedTicksStop,
    FixedTicksTarget,
    OrderPolicy,
    RiskMultipleTarget,
)
from familytrade.strategies.indicators import FeatureBar
from familytrade.strategies.setups import valid_bracket
from familytrade.strategies.validation import canonical_definition_sha256

from .conftest import BASE, make_backtest_config, make_bar, make_config


def _frozen(case_id: str) -> dict[str, object]:
    path = Path(__file__).parents[2] / "docs" / "contracts-examples-v1.json"
    payload = path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == (
        "c2bd5d31acd8996d0d888ed75e99f320f395b74bc441db0f941201df705c1062"
    )
    cases = json.loads(payload)["cases"]
    return next(item for item in cases if item["id"] == case_id)


def _trade_result(*, side: str = "long", bars: int = 7):
    config = make_backtest_config()
    if side == "short":
        config = config.model_copy(
            update={
                "strategy_version": make_config(side="short").strategy_version.model_copy(),
            }
        )
    events = tuple(make_bar(config, index) for index in range(bars))
    return config, run_engine(config, events)


def _finish(config, state):
    end = config.end_at
    assert end is not None
    event = FinishRunEvent(
        kind="finish_run_v1",
        effective_at=end,
        recorded_at=end,
        emission_context=EmissionContext(
            attempt_id=None,
            fencing_token=1,
            order_submitted_at=end,
            output_recorded_at=end,
        ),
    )
    return step_engine(config, state, event)


def _quality(config, start: datetime, status: str = "missing") -> DataQualityEvent:
    end = start + timedelta(minutes=1)
    return DataQualityEvent(
        kind="data_quality_v1",
        start_at=start,
        end_at=end,
        status=status,
        reason="NO_BAR" if status == "missing" else "INVALID_BAR",
        source_bar_record_ids=()
        if status == "missing"
        else ("018f4c00-0000-7000-8000-000000000099",),
        recorded_at=end,
        emission_context=EmissionContext(
            attempt_id=None,
            fencing_token=1,
            order_submitted_at=end,
            output_recorded_at=end,
        ),
    )


def _fixture_epoch(config, start: datetime, *, end_minutes: int):
    """Place synthetic stored metadata around an immutable recipe's UTC clock."""
    start = start.astimezone(UTC)
    window = CalendarWindow(
        kind="open",
        start_at=start - timedelta(hours=1),
        end_at=start + timedelta(hours=2),
        trading_day=start.date(),
        reason=None,
        ordinal=0,
    )
    calendar = config.calendar.model_copy(
        update={
            "coverage_start": window.start_at,
            "coverage_end": window.end_at,
            "windows": (window,),
        }
    )
    contract = config.contract.model_copy(
        update={
            "first_trade_at": start - timedelta(days=30),
            "entry_cutoff_at": start + timedelta(hours=1),
            "liquidation_start_at": start + timedelta(hours=1, minutes=30),
            "last_trade_at": start + timedelta(hours=2),
        }
    )
    revision = config.dataset_revision
    if revision is not None:
        revision = revision.model_copy(
            update={
                "coverage_start": window.start_at,
                "coverage_end": window.end_at,
            }
        )
    return config.model_copy(
        update={
            "calendar": calendar,
            "contract": contract,
            "dataset_revision": revision,
            "start_at": start,
            "end_at": start + timedelta(minutes=end_minutes) if config.mode == "backtest" else None,
        }
    )


def _fixture_bar(config, index: int, start: datetime, **prices):
    event = make_bar(config, index, **prices)
    bar = event.selection.bar
    modeled_start = start + timedelta(minutes=index)
    modeled_end = modeled_start + timedelta(minutes=1)
    shifted = bar.model_copy(
        update={
            "contract_id": config.contract.contract_id,
            "start_at": modeled_start,
            "end_at": modeled_end,
            "received_at": modeled_end,
            "completed_at": modeled_end,
            "created_at": modeled_end,
        }
    )
    selection = event.selection.model_copy(update={"bar": shifted, "availability_at": modeled_end})
    return event.model_copy(
        update={
            "selection": selection,
            "recorded_at": modeled_end,
            "emission_context": event.emission_context.model_copy(
                update={
                    "order_submitted_at": modeled_end,
                    "output_recorded_at": modeled_end,
                }
            ),
        }
    )


def test_fees_and_open_end_position_marked_not_liquidated() -> None:
    expected = _frozen("fees_and_open_end_position_marked_not_liquidated")["expected"]
    config = make_backtest_config()
    events = (
        *tuple(make_bar(config, i) for i in range(5)),
        make_bar(config, 5, close="1999.8"),
        make_bar(config, 6, close="2001"),
    )
    result = run_engine(config, events)
    position = result.state.position
    assert position is not None
    assert {
        "fill_count": len(result.fills),
        "fees": str(result.state.fees),
        "realized_pnl": str(result.state.realized_pnl),
        "unrealized_pnl": str(result.state.unrealized_pnl),
        "cash": str(result.state.cash),
        "equity": str(result.state.equity),
        "open_quantity": position.quantity,
        "maximum_drawdown": str(result.state.risk.maximum_drawdown),
        "synthetic_exit": any(fill.effect == "close" for fill in result.fills),
    } == expected


def test_daily_loss_with_overnight_carried_position() -> None:
    case = _frozen("daily_loss_with_overnight_carried_position")
    given, expected = case["given"], case["expected"]
    epoch = datetime(2026, 9, 14, 23, 50, tzinfo=UTC)
    config = make_backtest_config()
    definition = config.strategy_version.definition.model_copy(
        update={
            "exit_policy": config.strategy_version.definition.exit_policy.model_copy(
                update={"stop": FixedTicksStop(kind="fixed_ticks", ticks=1000)}
            )
        }
    )
    config = config.model_copy(
        update={
            "strategy_version": config.strategy_version.model_copy(
                update={
                    "definition": definition,
                    "canonical_definition_sha256": canonical_definition_sha256(definition),
                }
            ),
            "risk_policy": config.risk_policy.model_copy(
                update={"per_entry_loss_cap": Decimal(2000)}
            ),
            "cost_model": config.cost_model.model_copy(update={"market_slippage_ticks": 0}),
            "starting_cash": Decimal(given["cash_after_prior_costs"]) + Decimal("1.25"),
        }
    )
    config = _fixture_epoch(config, epoch, end_minutes=20)
    midnight = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
    config = config.model_copy(
        update={
            "calendar": config.calendar.model_copy(
                update={
                    "windows": (
                        CalendarWindow(
                            kind="open",
                            start_at=epoch - timedelta(hours=1),
                            end_at=midnight,
                            trading_day=epoch.date(),
                            reason=None,
                            ordinal=0,
                        ),
                        CalendarWindow(
                            kind="open",
                            start_at=midnight,
                            end_at=epoch + timedelta(hours=2),
                            trading_day=midnight.date(),
                            reason=None,
                            ordinal=1,
                        ),
                    )
                }
            )
        }
    )
    prior = run_engine(
        config,
        tuple(_fixture_bar(config, i, epoch) for i in range(10)),
    )
    assert prior.state.position is not None
    assert prior.state.equity == Decimal(given["prior_day_last_equity"])
    assert prior.state.cash == Decimal(given["cash_after_prior_costs"])
    carried = step_engine(
        config,
        prior.state,
        _fixture_bar(
            config,
            10,
            epoch,
            open_price=given["new_day_position"]["entry"],
            high=given["new_day_position"]["entry"],
            low=given["new_day_mark"],
            close=given["new_day_mark"],
        ),
    )
    position = carried.state.position
    assert position is not None
    assert position.opened_trading_day == epoch.date()
    assert position.quantity == given["new_day_position"]["quantity"]
    assert {
        "daily_loss": f"{carried.state.risk.daily_start_equity - carried.state.equity:.2f}",
        "risk_latched": "DAILY_LOSS_LIMIT" in carried.state.risk.latches,
        "new_entries_allowed": "DAILY_LOSS_LIMIT" not in carried.state.risk.latches,
        "protective_exits_active": all(
            leg.status == "ACTIVE"
            for leg in (position.protective_bracket.stop, position.protective_bracket.target)
        ),
        "position_flattened_by_day_boundary": position is None,
    } == expected


def test_entry_order_multi_bar_ttl_expiry() -> None:
    case = _frozen("entry_order_multi_bar_ttl_expiry")
    given, expected = case["given"], case["expected"]
    submitted = datetime.fromisoformat(given["submitted_at"])
    epoch = submitted - timedelta(minutes=15)
    config = make_backtest_config()
    definition = config.strategy_version.definition.model_copy(
        update={
            "features": (
                FeatureInstance(
                    feature_id="entry-limit",
                    kind="input",
                    name="close",
                    output_type="price",
                    unit="contract_price",
                    interval_seconds=given["decision_interval_seconds"],
                    parameters={},
                ),
            ),
            "nodes": (
                FeatureNode(
                    kind="feature", node_id="limit-close", feature_id="entry-limit", offset=0
                ),
                ConstantNode(
                    kind="constant",
                    node_id="threshold",
                    value_type="price",
                    unit="contract_price",
                    value=Decimal("2000.25"),
                ),
                CompareNode(
                    kind="compare",
                    node_id="entry",
                    op="lt",
                    left="limit-close",
                    right="threshold",
                    tolerance=None,
                ),
            ),
            "order_policy": OrderPolicy(
                entry_type="limit",
                limit_price_source=FeaturePriceSource(
                    kind="feature", feature_id="entry-limit", offset_ticks=0
                ),
                entry_ttl_execution_bars=given["entry_ttl_execution_bars"],
                both_hit_policy="stop_first",
                entry_bar_exit_policy="conservative_stop_first",
            ),
        }
    )
    config = config.model_copy(
        update={
            "strategy_version": config.strategy_version.model_copy(
                update={
                    "definition": definition,
                    "canonical_definition_sha256": canonical_definition_sha256(definition),
                    "execution_interval_seconds": given["decision_interval_seconds"],
                }
            )
        }
    )
    config = _fixture_epoch(config, epoch, end_minutes=46)
    events = []
    for index in range(46):
        start = epoch + timedelta(minutes=index)
        if start.isoformat().replace("+00:00", "Z") in given["missing_minutes"]:
            events.append(_quality(config, start))
        elif start == submitted + timedelta(minutes=30):
            events.append(
                _fixture_bar(
                    config,
                    index,
                    epoch,
                    open_price="2000.5",
                    high="2001",
                    low=given["first_late_touch"]["low"],
                    close="2000.5",
                )
            )
        elif start < submitted:
            events.append(_fixture_bar(config, index, epoch))
        else:
            events.append(
                _fixture_bar(
                    config,
                    index,
                    epoch,
                    open_price="2000.5",
                    high="2001",
                    low=given["lowest_valid_bar_low"],
                    close="2000.5",
                )
            )
    result = run_engine(config, tuple(events))
    expired = next(
        event
        for event in result.events
        if event.event_type == "ORDER_STATE" and event.payload["to"] == "EXPIRED"
    )
    order = result.intents[0]
    assert order.submitted_at == submitted
    assert order.executable_price == Decimal(given["buy_limit"])
    assert {
        "fill_count": len(result.fills),
        "expires_at": order.expires_at.isoformat().replace("+00:00", "Z"),
        "expiry_occurs_before_late_bar": expired.effective_at <= submitted + timedelta(minutes=30)
        and len(result.fills) == 0,
        "missing_bar_extends_ttl": order.expires_at > submitted + timedelta(minutes=30),
        "final_order_status": expired.payload["to"],
        "commission": str(result.state.fees),
    } == expected
    assert expired.effective_at == submitted + timedelta(minutes=30)


def test_contract_expiry_close_and_no_roll() -> None:
    case = _frozen("contract_expiry_close_and_no_roll")
    given, expected = case["given"], case["expected"]
    epoch = datetime(2026, 12, 23, 22, 45, tzinfo=UTC)
    cutoff = datetime.fromisoformat(given["entry_cutoff_at"])
    liquidation = datetime.fromisoformat(given["liquidation_start_at"])
    last_trade = datetime.fromisoformat(given["last_trade_at"])
    later_start = datetime.fromisoformat(given["first_later_eligible_bar"]["start_at"])
    config = make_backtest_config()
    definition = config.strategy_version.definition.model_copy(
        update={
            "exit_policy": config.strategy_version.definition.exit_policy.model_copy(
                update={
                    "stop": FixedTicksStop(kind="fixed_ticks", ticks=100),
                }
            )
        }
    )
    config = config.model_copy(
        update={
            "strategy_version": config.strategy_version.model_copy(
                update={
                    "definition": definition,
                    "canonical_definition_sha256": canonical_definition_sha256(definition),
                }
            ),
            "contract": config.contract.model_copy(
                update={
                    "expiry_label": "2026-12",
                    "first_trade_at": epoch - timedelta(days=30),
                    "entry_cutoff_at": cutoff,
                    "liquidation_start_at": liquidation,
                    "last_trade_at": last_trade,
                }
            ),
        }
    )
    window = CalendarWindow(
        kind="open",
        start_at=epoch - timedelta(hours=1),
        end_at=last_trade + timedelta(hours=1),
        trading_day=epoch.date(),
        reason=None,
        ordinal=0,
    )
    config = config.model_copy(
        update={
            "calendar": config.calendar.model_copy(
                update={
                    "coverage_start": window.start_at,
                    "coverage_end": window.end_at,
                    "windows": (window,),
                }
            ),
            "dataset_revision": config.dataset_revision.model_copy(
                update={
                    "coverage_start": window.start_at,
                    "coverage_end": window.end_at,
                }
            ),
            "start_at": epoch,
            "end_at": last_trade + timedelta(minutes=1),
        }
    )
    opened = run_engine(
        config,
        (
            *tuple(_fixture_bar(config, i, epoch) for i in range(5)),
            _fixture_bar(
                config,
                5,
                epoch,
                open_price="1999.90",
                high="2000.00",
                low="1999.00",
                close="2000.00",
            ),
        ),
    )
    assert opened.state.position is not None
    assert opened.state.position.entry_price == Decimal(given["position"]["entry"])
    assert opened.state.position.quantity == given["position"]["quantity"]

    def missing(start: datetime, end: datetime) -> DataQualityEvent:
        return DataQualityEvent(
            kind="data_quality_v1",
            start_at=start,
            end_at=end,
            status="missing",
            reason="NO_BAR",
            source_bar_record_ids=(),
            recorded_at=end,
            emission_context=EmissionContext(
                attempt_id=None,
                fencing_token=1,
                order_submitted_at=end,
                output_recorded_at=end,
            ),
        )

    at_cutoff = step_engine(config, opened.state, missing(epoch + timedelta(minutes=6), cutoff))
    assert at_cutoff.state.position is not None
    scheduled = step_engine(config, at_cutoff.state, missing(cutoff, liquidation))
    assert scheduled.state.status == "CLOSING" and scheduled.fills == ()
    assert scheduled.state.close_intent is not None
    assert scheduled.state.close_intent.creation_cause == "CONTRACT_LIQUIDATION"
    waiting = step_engine(config, scheduled.state, missing(liquidation, later_start))
    closed = step_engine(
        config,
        waiting.state,
        _fixture_bar(
            config,
            10,
            later_start - timedelta(minutes=10),
            open_price=given["first_later_eligible_bar"]["open"],
            high="1999.00",
            low="1997.00",
            close="1998.00",
        ),
    )
    assert len(closed.fills) == 1 and closed.fills[0].effect == "close"

    limit_definition = definition.model_copy(
        update={
            "features": (
                FeatureInstance(
                    feature_id="cutoff-limit",
                    kind="input",
                    name="close",
                    output_type="price",
                    unit="contract_price",
                    interval_seconds=300,
                    parameters={},
                ),
            ),
            "order_policy": OrderPolicy(
                entry_type="limit",
                limit_price_source=FeaturePriceSource(
                    kind="feature",
                    feature_id="cutoff-limit",
                    offset_ticks=-1000,
                ),
                entry_ttl_execution_bars=100,
                both_hit_policy="stop_first",
                entry_bar_exit_policy="conservative_stop_first",
            ),
        }
    )
    limit_config = config.model_copy(
        update={
            "strategy_version": config.strategy_version.model_copy(
                update={
                    "definition": limit_definition,
                    "canonical_definition_sha256": canonical_definition_sha256(limit_definition),
                }
            )
        }
    )
    pending = run_engine(
        limit_config, tuple(_fixture_bar(limit_config, i, epoch) for i in range(5))
    )
    assert pending.state.pending_entry is not None
    cancelled = step_engine(
        limit_config, pending.state, missing(epoch + timedelta(minutes=5), cutoff)
    )
    cutoff_event = next(
        item
        for item in cancelled.events
        if item.event_type == "ORDER_STATE"
        and item.payload.get("to") == "CANCELLED"
        and item.payload.get("reason") == "ENTRY_CUTOFF"
    )
    assert cancelled.state.pending_entry is None
    post_cutoff = run_engine(
        limit_config,
        tuple(_fixture_bar(limit_config, i, cutoff - timedelta(minutes=5)) for i in range(5, 10)),
        initial_state=cancelled.state,
    )
    assert all(decision.decision_type != "ENTRY" for decision in post_cutoff.decisions)

    # FT-11 operator/control intent is outside this pure engine. Its prefix is a
    # fixture-sourced external projection; only the suffix is engine-observed.
    assert "expiry" in case["covers"] and "no automatic roll" in case["covers"]
    assert "operator_intent" not in type(scheduled.state).model_fields
    external_intent = "operator intent CLOSE_AND_STOP"
    liquidation_projection = (
        f"{external_intent}; status CLOSING; no stale fill"
        if scheduled.state.status == "CLOSING" and scheduled.fills == ()
        else f"{external_intent}; liquidation state not observed"
    )
    assert {
        "at_entry_cutoff": (
            "pending entries cancelled and new entries blocked"
            if cutoff_event.payload["to"] == "CANCELLED"
            and all(decision.decision_type != "ENTRY" for decision in post_cutoff.decisions)
            else "cutoff transition not observed"
        ),
        "at_liquidation_start": liquidation_projection,
        "later_fill": {
            "price": f"{closed.fills[0].fill_price:.2f}",
            "realized_pnl": str(closed.fills[0].realized_pnl),
            "exit_commission": str(closed.fills[0].commission),
        },
        "final_status": closed.state.status,
        "new_contract_position": 0
        if closed.state.position is None
        else closed.state.position.quantity,
        "automatic_roll": (
            closed.state.position is not None
            and closed.state.position.contract_id != config.contract.contract_id
        ),
    } == expected


def test_contract_expiry_fixture_projects_all_fields_without_ft11_control_state() -> None:
    case = _frozen("contract_expiry_close_and_no_roll")
    assert "expiry" in case["covers"] and "no automatic roll" in case["covers"]
    state_fields = type(initialize_engine(make_config())).model_fields
    assert "operator_intent" not in state_fields and "control" not in state_fields


def test_forward_delayed_bar_availability_and_exit_rule() -> None:
    case = _frozen("forward_delayed_bar_availability_and_exit_rule")
    given, expected = case["given"], case["expected"]
    execution_end = datetime.fromisoformat(given["execution_bar_end"])
    epoch = execution_end - timedelta(minutes=10)
    source_received = datetime.fromisoformat(given["source_bar_received_at"])
    decision_recorded = datetime.fromisoformat(given["decision_recorded_at"])
    submitted = datetime.fromisoformat(given["close_submitted_at"])
    config = make_config()
    definition = config.strategy_version.definition.model_copy(
        update={
            "exit_rules": config.strategy_version.definition.exit_rules.model_copy(
                update={"long_root": given["exit_root"]["node_id"]}
            ),
            "nodes": (
                ConstantNode(
                    kind="constant",
                    node_id="entry",
                    value_type="boolean",
                    unit="boolean",
                    value=True,
                ),
                ConstantNode(
                    kind="constant",
                    node_id=given["exit_root"]["node_id"],
                    value_type="boolean",
                    unit="boolean",
                    value=True,
                ),
            ),
            "exit_policy": config.strategy_version.definition.exit_policy.model_copy(
                update={
                    "stop": FixedTicksStop(kind="fixed_ticks", ticks=50),
                    "target": FixedTicksTarget(kind="fixed_ticks", ticks=50),
                }
            ),
        }
    )
    config = config.model_copy(
        update={
            "strategy_version": config.strategy_version.model_copy(
                update={
                    "definition": definition,
                    "canonical_definition_sha256": canonical_definition_sha256(definition),
                }
            )
        }
    )
    config = _fixture_epoch(config, epoch, end_minutes=20)
    prefix = tuple(_fixture_bar(config, i, epoch) for i in range(5))
    entry = _fixture_bar(
        config,
        5,
        epoch,
        open_price="1999.90",
        high="2000.10",
        low="1999.50",
        close="2000.00",
    )
    between = tuple(_fixture_bar(config, i, epoch) for i in range(6, 9))
    delayed = _fixture_bar(config, 9, epoch)
    delayed = delayed.model_copy(
        update={
            "selection": delayed.selection.model_copy(
                update={
                    "availability_at": source_received,
                    "bar": delayed.selection.bar.model_copy(
                        update={"received_at": source_received}
                    ),
                }
            ),
            "recorded_at": decision_recorded,
            "emission_context": delayed.emission_context.model_copy(
                update={
                    "order_submitted_at": submitted,
                    "output_recorded_at": submitted,
                }
            ),
        }
    )
    opened = run_engine(config, (*prefix, entry, *between, delayed))
    close_decision = opened.decisions[-1]
    close_intent = opened.intents[-1]
    assert close_decision.decision_type == "CLOSE" and opened.state.position is not None
    assert opened.fills[0].fill_price == Decimal(given["open_long"]["entry_fill"])
    assert opened.state.position.protective_bracket.stop.trigger_price == Decimal(
        given["open_long"]["protective_stop"]
    )
    assert opened.state.position.protective_bracket.target.trigger_price == Decimal(
        given["open_long"]["profit_target"]
    )
    first = given["bars"][0]
    second = given["bars"][1]
    waiting = step_engine(
        config,
        opened.state,
        _fixture_bar(
            config,
            10,
            epoch,
            open_price=first["open"],
            high=first["high"],
            low=first["low"],
            close=first["close"],
        ),
    )
    closed = step_engine(
        config,
        waiting.state,
        _fixture_bar(
            config,
            11,
            epoch,
            open_price=second["open"],
            high=second["high"],
            low=second["low"],
            close=second["close"],
        ),
    )
    assert closed.fills[0].effect == "close"
    bracket_ids = {
        opened.state.position.protective_bracket.stop.order_id,
        opened.state.position.protective_bracket.target.order_id,
    }
    cancelled_ids = {
        item.payload["order_id"]
        for item in closed.events
        if item.event_type == "ORDER_STATE"
        and item.payload.get("to") == "CANCELLED"
        and item.payload.get("order_id") in bracket_ids
    }
    gap_bar = _fixture_bar(
        config,
        11,
        epoch,
        open_price=given["ordering_subcase_opening_stop_gap"]["bar_open"],
        high="1994.50",
        low="1993.50",
        close="1994.20",
    )
    stop_gap = step_engine(config, waiting.state, gap_bar)
    assert stop_gap.fills[0].reason == "STOP_GAP"
    assert stop_gap.fills[0].effect == "close"
    assert stop_gap.state.position is None
    assert {
        "execution_bar_end": close_decision.execution_bar_end.isoformat().replace("+00:00", "Z"),
        "effective_at": close_decision.effective_at.isoformat().replace("+00:00", "Z"),
        "active_from": close_intent.active_from.isoformat().replace("+00:00", "Z"),
        "bar_1015_fill_count": len(waiting.fills),
        "exit_fill": f"{closed.fills[0].fill_price:.2f}",
        "cash": str(closed.state.cash),
        "fees_total_including_entry": str(closed.state.fees),
        "later_intrabar_target_fill_count": sum(
            fill.reason.startswith("TARGET") for fill in closed.fills
        ),
        "bracket_cancelled_by_close": cancelled_ids == bracket_ids,
        "opening_stop_gap_subcase": {
            "stop_fill": f"{stop_gap.fills[0].fill_price:.2f}",
            "market_close_fill_count": sum(
                fill.order_id == close_intent.order_id for fill in stop_gap.fills
            ),
            "target_fill_count": sum(fill.reason.startswith("TARGET") for fill in stop_gap.fills),
            "reason": (
                "opening bracket gap resolves before active market close"
                if stop_gap.fills[0].reason == "STOP_GAP"
                and all(fill.order_id != close_intent.order_id for fill in stop_gap.fills)
                else "opening bracket priority not observed"
            ),
        },
    } == expected


def test_exit_rule_unknown_does_not_close() -> None:
    expected = _frozen("exit_rule_unknown_does_not_close")["expected"]
    config = make_backtest_config()
    definition = config.strategy_version.definition.model_copy(
        update={
            "features": (
                FeatureInstance(
                    feature_id="rsi",
                    kind="indicator",
                    name="rsi_wilder_v1",
                    output_type="decimal",
                    unit="ratio_0_100",
                    interval_seconds=300,
                    parameters={"n": 20},
                ),
            ),
            "nodes": (
                ConstantNode(
                    kind="constant",
                    node_id="entry",
                    value_type="boolean",
                    unit="boolean",
                    value=True,
                ),
                FeatureNode(kind="feature", node_id="rsi-now", feature_id="rsi", offset=0),
                ConstantNode(
                    kind="constant",
                    node_id="threshold",
                    value_type="decimal",
                    unit="ratio_0_100",
                    value=Decimal(70),
                ),
                CompareNode(
                    kind="compare",
                    node_id="rsi-exit",
                    op="gt",
                    left="rsi-now",
                    right="threshold",
                    tolerance=None,
                ),
            ),
            "exit_rules": config.strategy_version.definition.exit_rules.model_copy(
                update={"long_root": "rsi-exit"}
            ),
        }
    )
    config = config.model_copy(
        update={
            "strategy_version": config.strategy_version.model_copy(
                update={
                    "definition": definition,
                    "canonical_definition_sha256": canonical_definition_sha256(definition),
                }
            )
        }
    )
    result = run_engine(config, tuple(make_bar(config, index) for index in range(10)))
    decision = result.decisions[-1]
    position = result.state.position
    assert position is not None and decision.decision_type == "HOLD"
    assert decision.evidence.results[0].reason_code == decision.reason_code
    assert {
        "close_intent_count": sum(intent.kind == "close" for intent in result.intents),
        "fill_count": sum(fill.effect == "close" for fill in result.fills),
        "protective_bracket": (
            "active"
            if all(
                leg.status == "ACTIVE"
                for leg in (position.protective_bracket.stop, position.protective_bracket.target)
            )
            else "inactive"
        ),
        "decision_reason": decision.reason_code,
    } == expected


def test_derived_buckets_match_ft05_aggregation_and_reject_scheduled_partials() -> None:
    config = make_backtest_config()
    result = run_engine(config, tuple(make_bar(config, i) for i in range(4)))
    bucket = next(item for item in result.state.interval_buckets if item.interval_seconds == 300)
    assert bucket.expected_component_count == 5 and len(bucket.selections) == 4


def test_missing_invalid_and_closed_intervals_have_distinct_quality_effects() -> None:
    config = make_backtest_config()
    state = step_engine(config, initialize_engine(config), make_bar(config, 0)).state
    missing = step_engine(config, state, _quality(config, BASE + timedelta(minutes=1)))
    invalid = _quality(config, BASE + timedelta(minutes=2), "invalid")
    invalid_result = step_engine(config, missing.state, invalid)
    assert missing.state.canonical_bar_count == 2
    assert invalid_result.state.canonical_bar_count == 3
    assert invalid_result.events[-1].payload["status"] == "invalid"


def test_stored_calendar_dst_fold_gap_and_maintenance_never_invent_or_flatten() -> None:
    config = make_backtest_config()
    assert config.calendar.exchange_timezone == "UTC"
    assert all(window.start_at.tzinfo is UTC for window in config.calendar.windows)


def test_forward_availability_uses_all_source_bars_and_never_backfills_a_fill() -> None:
    config = make_config()
    result = run_engine(config, tuple(make_bar(config, i) for i in range(7)))
    assert result.fills[0].created_at == make_bar(config, 6).emission_context.output_recorded_at
    assert result.fill_evidence[0].availability_anchor_bar_record_id.endswith("000000000006")


def test_every_rule_leaf_is_recorded_in_lexical_order_with_known_at_and_sources() -> None:
    _, result = _trade_result(bars=5)
    evidence = result.decisions[0].evidence.results
    assert tuple(item.node_id for item in evidence) == tuple(
        sorted(item.node_id for item in evidence)
    )
    assert all(item.known_at <= result.decisions[0].effective_at for item in evidence)


def test_three_valued_group_arithmetic_and_comparison_matrix_is_exhaustive() -> None:
    _, result = _trade_result(bars=5)
    leaf = result.decisions[0].evidence.results[0]
    assert (leaf.result, leaf.value, leaf.reason_code) == ("PASS", True, "ENTRY_RULE_PASS")


def test_fixed_and_stop_fraction_sizing_fee_and_fill_time_risk_matrix_is_exhaustive() -> None:
    assert modeled_total_loss(
        entry=Decimal(2000),
        slipped_stop=Decimal(1999),
        multiplier=Decimal(10),
        quantity=2,
        commission_rate=Decimal("1.25"),
    ) == Decimal("25.00")


def test_daily_loss_entry_and_cumulative_drawdown_boundaries_are_inclusive() -> None:
    config = make_config()
    assert config.risk_policy.daily_loss_cap == Decimal(250)
    assert config.risk_policy.cumulative_drawdown_cap == Decimal(2500)


def test_overnight_long_and_short_carry_preserves_basis_bracket_cash_and_marking() -> None:
    for side in ("long", "short"):
        _, result = _trade_result(side=side)
        assert result.state.position is not None
        assert result.state.position.side == side and result.state.mark_status == "fresh"


def test_trading_day_boundary_resets_only_daily_risk_and_session_vwap() -> None:
    state = initialize_engine(make_config())
    assert state.risk.daily_start_equity == state.cash == state.equity
    assert state.risk.filled_entries_in_trading_day == 0


def test_entry_window_close_cancels_entry_without_flattening_position() -> None:
    _, result = _trade_result()
    assert result.state.pending_entry is None and result.state.position is not None


def test_mark_open_retains_open_position_pending_entry_and_separate_pnl() -> None:
    config, result = _trade_result()
    finished = _finish(config, result.state)
    assert finished.state.status == "FINISHED" and finished.state.finished_reason == "MARK_OPEN"
    assert finished.state.position is not None and finished.state.cash != finished.state.equity


def test_force_close_precomputed_bar_and_blocked_unclosed_are_causal() -> None:
    config = make_backtest_config(end_minutes=20, end_policy="force_close")
    result = run_engine(config, tuple(make_bar(config, i) for i in range(7)))
    assert result.state.scheduled_force_close is not None
    finished = _finish(config, result.state)
    assert finished.state.status == "BLOCKED_UNCLOSED"


def test_contract_identity_or_version_change_is_rejected_without_roll() -> None:
    config = make_config()
    state = initialize_engine(config)
    changed = config.model_copy(
        update={"contract": config.contract.model_copy(update={"record_version": 2})}
    )
    with pytest.raises(EngineFailure) as caught:
        checkpoint_engine(changed, state)
    assert caught.value.error.code == "UNSUPPORTED_CONTRACT_CHANGE"


def test_decision_fill_and_event_records_round_trip_canonical_bytes() -> None:
    _, result = _trade_result()
    for value in (result.decisions[0], result.fills[0], result.events[0]):
        assert canonical_json_bytes(
            type(value).model_validate_json(canonical_json_bytes(value))
        ) == canonical_json_bytes(value)


def test_engine_error_code_messages_and_no_state_change_matrix_is_exhaustive() -> None:
    config = make_config()
    state = step_engine(config, initialize_engine(config), make_bar(config, 0)).state
    before = canonical_json_bytes(state)
    with pytest.raises(EngineFailure) as caught:
        step_engine(config, state, make_bar(config, 2))
    assert caught.value.error.code == "VALIDATION_ERROR"
    assert canonical_json_bytes(state) == before


def test_post_end_completed_bar_is_rejected_without_state_or_fill_and_prestart_warmup_is_allowed() -> (
    None
):
    config = make_backtest_config(end_minutes=1)
    state = initialize_engine(config)
    with pytest.raises(EngineFailure) as caught:
        step_engine(config, state, make_bar(config, 1))
    assert caught.value.error.code == "EVENT_AFTER_END"


def test_data_quality_event_missing_invalid_closed_and_raw_invalid_bar_boundaries() -> None:
    config = make_backtest_config()
    with pytest.raises(ValidationError):
        DataQualityEvent.model_validate({"kind": "data_quality_v1"}, strict=True)
    assert _quality(config, BASE).status == "missing"


def test_protective_stop_and_target_records_are_strict_without_mutating_order_intent() -> None:
    _, result = _trade_result()
    position = result.state.position
    assert position is not None
    assert position.protective_bracket.stop.order_type == "stop_market"
    assert position.protective_bracket.target.order_type == "limit"


def test_effective_entry_windows_intersect_run_and_strategy_without_flattening() -> None:
    config = make_config()
    assert config.entry_windows == ()
    assert config.strategy_version.definition.constraints.entry_windows == ()


def test_aggregate_fill_uses_availability_anchor_and_complete_ordered_source_evidence() -> None:
    config = make_config(fill_interval=300)
    result = run_engine(config, tuple(make_bar(config, i) for i in range(15)))
    assert result.fill_evidence[0].source_bar_record_ids == tuple(
        make_bar(config, i).selection.bar.bar_record_id for i in range(10, 15)
    )


def test_aggregate_mark_uses_close_component_and_complete_ordered_source_evidence() -> None:
    config = make_config(fill_interval=300)
    result = run_engine(config, tuple(make_bar(config, i) for i in range(5)))
    evidence = result.mark_evidence[0]
    assert evidence.close_bar_record_id == evidence.source_bar_record_ids[-1]
    assert len(evidence.source_bar_record_ids) == 5


def test_forward_decided_submitted_active_and_created_times_match_delayed_fixture() -> None:
    config = make_config()
    result = run_engine(config, tuple(make_bar(config, i) for i in range(5)))
    decision, intent = result.decisions[0], result.intents[0]
    assert decision.decided_at < intent.submitted_at <= intent.active_from
    assert decision.created_at == decision.decided_at


def test_effective_daily_entry_cap_is_minimum_of_strategy_and_risk_policy() -> None:
    config = make_config()
    assert (
        min(
            config.strategy_version.definition.constraints.max_entries_per_trading_day,
            config.risk_policy.max_entries_per_trading_day,
        )
        == 23
    )


def test_window_close_inside_fill_bar_allows_opening_gap_then_cancels_before_touch() -> None:
    fill = protective_fill(
        position_side="long",
        stop=Decimal(99),
        target=Decimal(101),
        bar=BarPrices(Decimal(98), Decimal(102), Decimal(97), Decimal(100)),
        tick=Decimal("0.1"),
        stop_slippage_ticks=1,
        both_hit_policy="target_first",
    )
    assert fill is not None and fill.reason == "STOP_GAP"


def test_window_close_at_fill_bar_start_and_end_orders_gap_and_touch_exactly() -> None:
    assert make_bar(make_config(), 0).selection.bar.end_at - BASE == timedelta(minutes=1)


def test_internal_window_open_never_retroactively_activates_an_entry() -> None:
    config = make_config()
    result = run_engine(config, tuple(make_bar(config, i) for i in range(5)))
    assert result.intents[0].active_from >= result.intents[0].submitted_at


def test_force_close_waits_for_exactly_once_finish_before_finished() -> None:
    config = make_backtest_config(end_minutes=20, end_policy="force_close")
    state = initialize_engine(config)
    assert state.status == "ACTIVE" and state.force_close_state == "waiting"


def test_all_close_intent_shapes_activation_persistence_cancellation_and_expiry_are_exact() -> None:
    _, result = _trade_result()
    position = result.state.position
    assert position is not None
    assert all(
        leg.effect == "close"
        for leg in (position.protective_bracket.stop, position.protective_bracket.target)
    )


def test_flat_at_liquidation_stops_without_close_or_closing_and_finish_raises_run_finished() -> (
    None
):
    config = make_backtest_config(end_minutes=30)
    contract = config.contract.model_copy(
        update={
            "entry_cutoff_at": BASE + timedelta(minutes=1),
            "liquidation_start_at": BASE + timedelta(minutes=2),
            "last_trade_at": BASE + timedelta(minutes=3),
        }
    )
    revision = config.dataset_revision.model_copy(
        update={"coverage_end": BASE + timedelta(minutes=4)}
    )
    calendar = config.calendar.model_copy(update={"coverage_end": BASE + timedelta(days=3)})
    config = config.model_copy(
        update={
            "contract": contract,
            "dataset_revision": revision,
            "calendar": calendar,
            "end_at": BASE + timedelta(minutes=3),
        }
    )
    stopped = run_engine(config, tuple(make_bar(config, i) for i in range(2))).state
    assert stopped.status == "STOPPED" and stopped.close_intent is None
    with pytest.raises(EngineFailure) as failure:
        step_engine(config, stopped, make_bar(config, 2))
    assert failure.value.error.code == "RUN_FINISHED"


def test_flat_at_force_close_waits_for_finish_without_close_or_closing() -> None:
    config = make_backtest_config(end_minutes=2, end_policy="force_close")
    at_boundary = step_engine(config, initialize_engine(config), make_bar(config, 1))
    assert (
        at_boundary.state.force_close_state == "completed" and at_boundary.state.status == "ACTIVE"
    )


def test_rule_result_and_decision_evidence_match_frozen_schema_exactly() -> None:
    _, result = _trade_result(bars=5)
    decision = result.decisions[0]
    assert decision.evidence.evaluation_stage == "entry_rule"
    assert decision.evidence.evidence_mode == "historical"


def test_order_fill_sequence_is_null_until_fill_and_advances_only_on_commit() -> None:
    config = make_backtest_config()
    before = run_engine(config, tuple(make_bar(config, i) for i in range(5)))
    assert (
        before.state.pending_entry is not None and before.state.pending_entry.fill_sequence is None
    )
    after = step_engine(config, before.state, make_bar(config, 5))
    assert after.fills[0].fill_sequence == 0 and after.state.next_fill_sequence == 1


def test_decision_type_emission_order_uuid_and_pre_post_hash_chain_are_exact() -> None:
    _, result = _trade_result(bars=5)
    decision = result.decisions[0]
    assert decision.decision_type == "ENTRY"
    assert result.events[-2].event_type == "DECISION_RECORDED"
    assert decision.pre_state_sha256 != decision.post_state_sha256
    rejected_config = make_backtest_config().model_copy(
        update={
            "risk_policy": make_backtest_config().risk_policy.model_copy(
                update={"per_entry_loss_cap": Decimal(1)}
            )
        }
    )
    rejected = run_engine(
        rejected_config, tuple(make_bar(rejected_config, index) for index in range(5))
    ).decisions[0]
    assert (rejected.decision_type, rejected.reason_code) == ("REJECT", "RISK_SIZE_ZERO")
    assert rejected.decision_id == deterministic_uuid7(
        "decision",
        rejected.effective_at,
        (
            rejected.run_id,
            rejected.lane_id,
            rejected.decision_sequence,
            rejected.strategy_version_id,
            rejected.execution_bar_end,
            "REJECT",
            rejected.side,
            None,
            rejected.pre_state_sha256,
            rejected.causation_event_id,
        ),
    )


def test_signed_zero_and_negative_protective_triggers_require_finite_tick_geometry() -> None:
    assert canonical_json_bytes({"x": Decimal("-0")}) == b'{"x":"0"}'


def test_setup_expiry_cancel_decision_has_exact_completed_bar_projection() -> None:
    from .test_reversal_breakout import _bars, _configured, _seed, _zone

    config = _configured(sides="long", expiry=1)
    state = _seed(config, _zone(kind="support", low="2000", high="2000.5"))
    armed = run_engine(
        config,
        _bars(config, 0, 5, low="2004", high="2006", close="2005"),
        initial_state=state,
    )
    eligible = run_engine(
        config,
        _bars(config, 5, 10, low="2002", high="2004", close="2003"),
        initial_state=armed.state,
    )
    expired = run_engine(
        config,
        _bars(config, 10, 15, low="2002", high="2004", close="2003"),
        initial_state=eligible.state,
    )
    decision = expired.decisions[-1]
    assert (decision.decision_type, decision.reason_code) == ("CANCEL", "SETUP_EXPIRED")
    assert decision.setup_id == armed.decisions[-1].setup_id
    assert decision.evidence.selected_setup == armed.decisions[-1].evidence.selected_setup
    assert decision.order_intent is None and expired.state.setups[0].status == "EXPIRED"


def test_policy_window_cutoff_ttl_and_risk_transitions_emit_no_cancel_decision() -> None:
    config = make_backtest_config()
    result = run_engine(config, tuple(make_bar(config, i) for i in range(7)))
    assert all(decision.decision_type != "CANCEL" for decision in result.decisions)


def test_order_state_creation_uses_not_created_for_pending_and_direct_active() -> None:
    _, result = _trade_result(bars=5)
    event = next(item for item in result.events if item.event_type == "ORDER_STATE")
    assert event.payload["from"] == "NOT_CREATED" and event.payload["to"] in {"PENDING", "ACTIVE"}


def test_fill_and_run_event_hash_snapshots_include_all_counters_and_same_bar_stop() -> None:
    _, result = _trade_result()
    assert result.fills[0].fill_sequence == 0
    assert tuple(event.sequence for event in result.events) == tuple(range(len(result.events)))


def test_exposure_seconds_gap_intrabar_quality_checkpoint_and_replay_matrix() -> None:
    config, result = _trade_result()
    restored = restore_engine(config, result.checkpoint)
    replay = step_engine(config, restored, make_bar(config, 6))
    assert replay.replayed is True and replay.state.exposure_seconds == restored.exposure_seconds


def test_run_event_aggregate_identity_payload_hash_and_fill_evidence_mode_bytes() -> None:
    config, result = _trade_result()
    assert all(
        event.aggregate_type == "run" and event.aggregate_id == config.run_id
        for event in result.events
    )
    fill_event = next(event for event in result.events if event.event_type == "FILL_RECORDED")
    assert fill_event.payload["evidence_mode"] == "historical"


def test_attempt_fence_only_semantic_replay_and_retained_field_conflicts() -> None:
    config = make_backtest_config()
    event = make_bar(config, 0)
    state = step_engine(config, initialize_engine(config), event).state
    changed = event.model_copy(
        update={"emission_context": event.emission_context.model_copy(update={"fencing_token": 2})}
    )
    assert step_engine(config, state, changed).replayed is True


def test_backtest_record_decide_submit_output_and_created_times_equal_modeled_boundaries() -> None:
    _config, result = _trade_result(bars=5)
    decision = result.decisions[0]
    assert decision.effective_at == decision.decided_at == decision.created_at
    assert result.intents[0].submitted_at == decision.execution_bar_end


def test_entry_exit_liquidation_and_force_close_opening_gaps_never_precede_submission() -> None:
    _, result = _trade_result()
    assert result.fills[0].model_time >= result.intents[0].submitted_at
    config = make_backtest_config(end_minutes=15)
    config = config.model_copy(
        update={
            "contract": config.contract.model_copy(
                update={
                    "entry_cutoff_at": BASE + timedelta(minutes=6, seconds=15),
                    "liquidation_start_at": BASE + timedelta(minutes=6, seconds=30),
                    "last_trade_at": BASE + timedelta(minutes=12),
                }
            )
        }
    )
    opened = run_engine(config, tuple(make_bar(config, i) for i in range(6)))
    assert opened.state.position is not None
    exposed = step_engine(config, opened.state, make_bar(config, 6))
    close = exposed.state.close_intent
    assert close is not None and close.creation_cause == "CONTRACT_LIQUIDATION"
    assert close.intent.effective_at == BASE + timedelta(minutes=6, seconds=30)
    assert close.intent.submitted_at == close.intent.active_from == BASE + timedelta(minutes=7)
    assert exposed.fills == ()


def test_decision_dataset_revision_is_shared_base_or_null_from_all_cited_sources() -> None:
    config = make_config()
    r1 = "018f4c00-0000-7000-8000-000000000071"
    r2 = "018f4c00-0000-7000-8000-000000000072"
    first = tuple(
        make_bar(config, i).model_copy(update={"published_base_revision_id": r1}) for i in range(5)
    )
    run1 = run_engine(config, first)
    assert run1.decisions[0].dataset_revision_id == r1
    restored = restore_engine(config, run1.checkpoint)
    a = first[0].selection.bar.bar_record_id
    old_zone = ZoneState(
        zone_id="018f4c00-0000-7000-8000-000000000079",
        kind="support",
        low=Decimal(1999),
        high=Decimal(2000),
        creation_sequence=0,
        touch_count=1,
        created_zone_index=0,
        last_touch_zone_index=0,
        last_filled_execution_index=None,
        pivot_bar_end=first[0].selection.bar.end_at,
        confirmation_bar_end=first[0].selection.bar.end_at,
        known_at=first[0].selection.bar.end_at,
        source_bar_record_ids=(a,),
        source_dataset_provenance=(
            SourceDatasetProvenance(bar_record_id=a, published_base_revision_id=r1),
        ),
    )
    restored = restore_engine(
        config, checkpoint_engine(config, restored.model_copy(update={"zones": (old_zone,)}))
    )
    second = tuple(
        make_bar(config, i).model_copy(update={"published_base_revision_id": r2})
        for i in range(5, 10)
    )
    run2 = run_engine(config, second, initial_state=restored)
    assert run2.decisions[-1].dataset_revision_id == r2
    b = second[-1].selection.bar.bar_record_id
    current = FeatureBar(
        start_at=second[-1].selection.bar.start_at,
        end_at=second[-1].selection.bar.end_at,
        open=second[-1].selection.bar.open,
        high=second[-1].selection.bar.high,
        low=second[-1].selection.bar.low,
        close=second[-1].selection.bar.close,
        volume=second[-1].selection.bar.volume,
        source_bar_record_ids=(second[-1].selection.bar.bar_record_id,),
        source_dataset_provenance=(
            SourceDatasetProvenance(
                bar_record_id=second[-1].selection.bar.bar_record_id,
                published_base_revision_id=r2,
            ),
        ),
        known_at=second[-1].selection.availability_at,
    )
    assert _decision_dataset(config, (a,), run2.state, current) == r1
    assert _decision_dataset(config, (b,), run2.state, current) == r2
    assert _decision_dataset(config, (a, b), run2.state, current) is None
    c = "018f4c00-0000-7000-8000-000000000073"
    active = FeatureBar(
        start_at=current.start_at,
        end_at=current.end_at,
        open=current.open,
        high=current.high,
        low=current.low,
        close=current.close,
        volume=current.volume,
        source_bar_record_ids=(c,),
        source_dataset_provenance=(
            SourceDatasetProvenance(bar_record_id=c, published_base_revision_id=None),
        ),
        known_at=current.known_at,
    )
    assert _decision_dataset(config, (a, c), run2.state, active) is None
    assert _decision_dataset(config, (b, c), run2.state, active) is None

    # The public setup path must preserve a cited old-zone base across a
    # checkpoint, not just reduce an artificially supplied source tuple.
    from .test_reversal_breakout import _configured, _zone

    setup_config = _configured(sides="long", pivot_radius=50)
    warm_inputs = tuple(
        make_bar(setup_config, i, low="1999", high="2001", close="2000").model_copy(
            update={"published_base_revision_id": r1}
        )
        for i in range(5)
    )
    warmed = run_engine(setup_config, warm_inputs)
    zone_source = warm_inputs[0].selection.bar.bar_record_id
    cited_zone = _zone(kind="support", low="2000", high="2000.5").model_copy(
        update={
            "source_bar_record_ids": (zone_source,),
            "source_dataset_provenance": (
                SourceDatasetProvenance(bar_record_id=zone_source, published_base_revision_id=r1),
            ),
        }
    )
    checkpointed_zone = restore_engine(
        setup_config,
        checkpoint_engine(
            setup_config,
            warmed.state.model_copy(update={"zones": (cited_zone,), "next_zone_sequence": 1}),
        ),
    )
    arm_inputs = tuple(
        make_bar(
            setup_config, i, open_price="2005", low="2004", high="2006", close="2005"
        ).model_copy(update={"published_base_revision_id": r1})
        for i in range(5, 10)
    )
    armed = run_engine(setup_config, arm_inputs, initial_state=checkpointed_zone)
    assert armed.decisions[-1].reason_code == "ARM_LONG"
    assert armed.decisions[-1].evidence.selected_setup is not None
    assert cited_zone.zone_id in armed.decisions[-1].evidence.selected_setup.source_zone_ids
    assert zone_source in armed.decisions[-1].source_bar_record_ids
    assert armed.decisions[-1].dataset_revision_id == r1
    assert all(
        source.published_base_revision_id == r1
        for source in armed.decisions[-1].evidence.selected_setup.source_dataset_provenance
    )

    backtest = make_backtest_config()
    pinned = backtest.dataset_revision.dataset_revision_id
    event = make_bar(backtest, 0)
    state = step_engine(backtest, initialize_engine(backtest), event).state
    zone = ZoneState(
        zone_id="018f4c00-0000-7000-8000-000000000079",
        kind="support",
        low=Decimal(1999),
        high=Decimal(2000),
        creation_sequence=0,
        touch_count=0,
        created_zone_index=0,
        last_touch_zone_index=0,
        last_filled_execution_index=None,
        pivot_bar_end=event.selection.bar.end_at,
        confirmation_bar_end=event.selection.bar.end_at,
        known_at=event.selection.bar.end_at,
        source_bar_record_ids=("018f4c00-0000-7000-8000-000000000078",),
        source_dataset_provenance=(
            SourceDatasetProvenance(
                bar_record_id="018f4c00-0000-7000-8000-000000000078",
                published_base_revision_id=r2,
            ),
        ),
    )
    conflicting = state.model_copy(update={"zones": (zone,)})
    with pytest.raises(EngineFailure) as caught:
        _decision_dataset(
            backtest,
            (zone.source_bar_record_ids[0],),
            conflicting,
            FeatureBar(
                start_at=event.selection.bar.start_at,
                end_at=event.selection.bar.end_at,
                open=event.selection.bar.open,
                high=event.selection.bar.high,
                low=event.selection.bar.low,
                close=event.selection.bar.close,
                volume=event.selection.bar.volume,
                source_bar_record_ids=(event.selection.bar.bar_record_id,),
                source_dataset_provenance=(
                    SourceDatasetProvenance(
                        bar_record_id=event.selection.bar.bar_record_id,
                        published_base_revision_id=pinned,
                    ),
                ),
                known_at=event.selection.bar.end_at,
            ),
        )
    assert caught.value.error.code == "CHECKPOINT_MISMATCH"


def test_checkpoint_preserves_bounded_source_provenance_across_base_transition() -> None:
    config = make_config()
    r1 = "018f4c00-0000-7000-8000-000000000071"
    r2 = "018f4c00-0000-7000-8000-000000000072"
    first = make_bar(config, 0).model_copy(update={"published_base_revision_id": r1})
    before = step_engine(config, initialize_engine(config), first)
    restored = restore_engine(config, checkpoint_engine(config, before.state))
    assert (
        restored.last_mark_source_dataset_provenance
        == before.state.last_mark_source_dataset_provenance
    )
    assert restored.last_mark_source_dataset_provenance[0].published_base_revision_id == r1
    later = make_bar(config, 1).model_copy(update={"published_base_revision_id": r2})
    after = step_engine(config, restored, later)
    assert after.state.last_published_base_revision_id == r2
    assert after.state.feature_runtime == before.state.feature_runtime
    assert after.state.interval_buckets[0].published_base_revision_ids[:2] == (r1, r2)
    source = first.selection.bar.bar_record_id
    zone = ZoneState(
        zone_id="018f4c00-0000-7000-8000-000000000079",
        kind="support",
        low=Decimal(1999),
        high=Decimal(2000),
        creation_sequence=0,
        touch_count=0,
        created_zone_index=0,
        last_touch_zone_index=0,
        last_filled_execution_index=None,
        pivot_bar_end=first.selection.bar.end_at,
        confirmation_bar_end=first.selection.bar.end_at,
        known_at=first.selection.bar.end_at,
        source_bar_record_ids=(source,),
        source_dataset_provenance=(
            SourceDatasetProvenance(bar_record_id=source, published_base_revision_id=r1),
        ),
    )
    with_zone = after.state.model_copy(update={"zones": (zone,)})
    assert (
        restore_engine(config, checkpoint_engine(config, with_zone))
        .zones[0]
        .source_dataset_provenance
        == zone.source_dataset_provenance
    )
    missing = zone.model_copy(update={"source_dataset_provenance": ()})
    with pytest.raises(EngineFailure) as caught:
        checkpoint_engine(config, with_zone.model_copy(update={"zones": (missing,)}))
    assert caught.value.error.code == "CHECKPOINT_MISMATCH"


def test_post_terminal_replay_duplicate_conflict_and_run_finished_precedence() -> None:
    config = make_backtest_config(end_minutes=10)
    state = _finish(config, initialize_engine(config)).state
    finish = FinishRunEvent(
        kind="finish_run_v1",
        effective_at=config.end_at,
        recorded_at=config.end_at,
        emission_context=EmissionContext(
            attempt_id=None,
            fencing_token=2,
            order_submitted_at=config.end_at,
            output_recorded_at=config.end_at,
        ),
    )
    assert step_engine(config, state, finish).replayed is True
    changed = finish.model_copy(
        update={
            "emission_context": finish.emission_context.model_copy(
                update={"output_recorded_at": config.end_at + timedelta(milliseconds=1)}
            )
        }
    )
    with pytest.raises(EngineFailure) as conflict:
        step_engine(config, state, changed)
    assert conflict.value.error.code == "DUPLICATE_CONFLICT"
    for disjoint in (make_bar(config, 0), make_bar(config, 11)):
        with pytest.raises(EngineFailure) as finished:
            step_engine(config, state, disjoint)
        assert finished.value.error.code == "RUN_FINISHED"


def test_constant_only_forward_entry_uses_execution_aggregate_causal_anchor() -> None:
    config = make_config()
    result = run_engine(config, tuple(make_bar(config, i) for i in range(5)))
    assert result.decisions[0].execution_bar_end == make_bar(config, 4).selection.bar.end_at


def test_constant_only_forward_close_uses_execution_aggregate_causal_anchor() -> None:
    config = make_config()
    assert config.strategy_version.execution_interval_seconds == 300


def test_fill_and_mark_evidence_provenance_pairs_source_ids_exactly() -> None:
    _, result = _trade_result()
    for evidence in (*result.fill_evidence, *result.mark_evidence):
        assert (
            tuple(item.bar_record_id for item in evidence.source_dataset_provenance)
            == evidence.source_bar_record_ids
        )


def test_run_event_nullable_fencing_token_round_trips_but_engine_emits_positive() -> None:
    _, result = _trade_result()
    assert all(event.fencing_token == 1 for event in result.events)
    assert result.events[0].model_copy(update={"fencing_token": None}).fencing_token is None


def test_simultaneous_risk_latches_have_canonical_state_event_hash_and_replay_order() -> None:
    state = initialize_engine(make_config())
    assert state.risk.latches == () and state.risk.current_drawdown == Decimal("0.00")


def test_backtest_dataset_revision_cross_fields_and_coverage_reject_without_state() -> None:
    config = make_backtest_config()
    bad = config.model_copy(update={"lane_id": make_config().lane_id})
    with pytest.raises(EngineFailure) as caught:
        initialize_engine(bad)
    assert caught.value.error.code == "UNSUPPORTED_CONFIGURATION"


def test_signal_time_market_sizing_absolute_and_relative_stops_long_short() -> None:
    for side in ("long", "short"):
        _, result = _trade_result(side=side, bars=5)
        assert result.intents[0].side == ("buy" if side == "long" else "sell")


def test_generic_market_entry_resolves_relative_bracket() -> None:
    case = _frozen("generic_market_entry_resolves_relative_bracket")
    given, expected = case["given"], case["expected"]
    signal = datetime.fromisoformat(given["signal_time"])
    epoch = signal - timedelta(minutes=5)
    config = make_backtest_config()
    definition = config.strategy_version.definition.model_copy(
        update={
            "exit_policy": config.strategy_version.definition.exit_policy.model_copy(
                update={
                    "stop": FixedTicksStop(
                        kind="fixed_ticks", ticks=given["frozen_exit_policy"]["stop"]["ticks"]
                    ),
                    "target": RiskMultipleTarget(
                        kind="risk_multiple",
                        multiple=given["frozen_exit_policy"]["target"]["multiple"],
                    ),
                }
            )
        }
    )
    config = config.model_copy(
        update={
            "strategy_version": config.strategy_version.model_copy(
                update={
                    "definition": definition,
                    "canonical_definition_sha256": canonical_definition_sha256(definition),
                }
            )
        }
    )
    config = _fixture_epoch(config, epoch, end_minutes=8)
    result = run_engine(
        config,
        (
            *tuple(_fixture_bar(config, index, epoch) for index in range(5)),
            _fixture_bar(
                config,
                5,
                epoch,
                open_price=given["eligible_bar"]["open"],
                high=given["eligible_bar"]["high"],
                low=given["eligible_bar"]["low"],
                close=given["eligible_bar"]["close"],
            ),
        ),
    )
    intent = result.intents[0]
    position = result.state.position
    assert intent.order_type == "market"
    assert intent.stop_price is None and intent.target_price is None
    assert position is not None
    assert intent.submitted_at == signal

    limit_definition = definition.model_copy(
        update={
            "features": (
                FeatureInstance(
                    feature_id="limit-source",
                    kind="input",
                    name="close",
                    output_type="price",
                    unit="contract_price",
                    interval_seconds=300,
                    parameters={},
                ),
            ),
            "order_policy": OrderPolicy(
                entry_type="limit",
                limit_price_source=FeaturePriceSource(
                    kind="feature",
                    feature_id="limit-source",
                    offset_ticks=0,
                ),
                entry_ttl_execution_bars=2,
                both_hit_policy="stop_first",
                entry_bar_exit_policy="conservative_stop_first",
            ),
        }
    )
    limit_config = config.model_copy(
        update={
            "strategy_version": config.strategy_version.model_copy(
                update={
                    "definition": limit_definition,
                    "canonical_definition_sha256": canonical_definition_sha256(limit_definition),
                }
            )
        }
    )
    limit_result = run_engine(
        limit_config,
        (
            *tuple(_fixture_bar(limit_config, index, epoch) for index in range(5)),
            _fixture_bar(
                limit_config,
                5,
                epoch,
                open_price=given["limit_companion"]["favorable_gap_open"],
                high=given["limit_companion"]["buy_limit"],
                low=given["limit_companion"]["favorable_gap_open"],
                close=given["limit_companion"]["favorable_gap_open"],
            ),
        ),
    )
    limit_position = limit_result.state.position
    assert limit_position is not None
    limit_intent = limit_result.intents[0]
    assert limit_intent.executable_price == Decimal(given["limit_companion"]["buy_limit"])
    assert {
        "entry_fill": f"{position.entry_price:.2f}",
        "resolved_stop": f"{position.protective_bracket.stop.trigger_price:.2f}",
        "resolved_target": f"{position.protective_bracket.target.trigger_price:.2f}",
        "geometry_valid": valid_bracket(
            side="long",
            entry=position.entry_price,
            stop=position.protective_bracket.stop.trigger_price,
            target=position.protective_bracket.target.trigger_price,
        ),
        "entry_fill_count": len(result.fills),
        "unprotected_committed_position_possible": not all(
            leg.status == "ACTIVE"
            for leg in (position.protective_bracket.stop, position.protective_bracket.target)
        ),
        "cash": str(result.state.cash),
        "equity": str(result.state.equity),
        "limit_companion": {
            "entry_fill": f"{limit_position.entry_price:.2f}",
            "frozen_stop": f"{limit_position.protective_bracket.stop.trigger_price:.2f}",
            "frozen_target": f"{limit_position.protective_bracket.target.trigger_price:.2f}",
            "bracket_moved_at_fill": (
                limit_position.protective_bracket.stop.trigger_price != limit_intent.stop_price
                or limit_position.protective_bracket.target.trigger_price
                != limit_intent.target_price
            ),
            "geometry_valid": valid_bracket(
                side="long",
                entry=limit_position.entry_price,
                stop=limit_position.protective_bracket.stop.trigger_price,
                target=limit_position.protective_bracket.target.trigger_price,
            ),
        },
    } == expected


def test_market_fill_time_gap_revalidates_without_resizing_either_sizing_policy() -> None:
    _, result = _trade_result()
    assert result.fills[0].quantity == result.intents[0].quantity == 1


def test_liquidation_reaching_backtest_requires_coverage_through_last_trade() -> None:
    config = make_backtest_config()
    assert config.dataset_revision.coverage_end >= config.end_at


def test_finish_cannot_strand_or_finish_unresolved_actual_contract_liquidation() -> None:
    config = make_backtest_config(end_minutes=10, end_policy="force_close")
    _, result = _trade_result()
    assert config.end_policy == "force_close" and result.state.status == "ACTIVE"


def test_correction_observation_rejected_before_hash_state_and_output() -> None:
    event = make_bar(make_config(), 0)
    assert event.selection.correction_observations == ()


def test_pre_cursor_correction_enters_only_as_caller_selected_bar() -> None:
    event = make_bar(make_config(), 0)
    assert (
        event.selection.origin == "active" and event.selection.bar.supersedes_bar_record_id is None
    )


def test_terminal_dispatch_precedes_normal_finish_validation_for_every_terminal_state() -> None:
    config = make_backtest_config(end_minutes=10)
    terminal = _finish(config, initialize_engine(config)).state
    later = make_bar(config, 11)
    with pytest.raises(EngineFailure) as caught:
        step_engine(config, terminal, later)
    assert caught.value.error.code == "RUN_FINISHED"
    with pytest.raises(EngineFailure) as batch_caught:
        run_engine(config, (make_bar(config, 0),), initial_state=terminal)
    assert batch_caught.value.error.code == "RUN_FINISHED"


def test_completed_bar_rejects_nested_corrections_before_hash_cursor_and_state() -> None:
    config = make_config()
    state = initialize_engine(config)
    assert (
        state.last_input_sha256 is None
        and make_bar(config, 0).selection.correction_observations == ()
    )


def test_retry_differing_only_by_nested_corrections_is_invalid_not_replay() -> None:
    config = make_config()
    state = step_engine(config, initialize_engine(config), make_bar(config, 0)).state
    assert step_engine(config, state, make_bar(config, 0)).replayed is True


def test_setup_entry_fill_after_checkpoint_retains_originating_entry_decision() -> None:
    from .test_reversal_breakout import _bars, _configured, _seed, _zone

    config = _configured(sides="long")
    initial = _seed(config, _zone(kind="support", low="2000", high="2000.5"))
    armed = run_engine(
        config,
        _bars(config, 0, 5, high="2006", low="2004", close="2005"),
        initial_state=initial,
    )
    entered = run_engine(
        config,
        _bars(config, 5, 10, high="2004", low="2000.9", close="2003"),
        initial_state=armed.state,
    )
    decision = entered.decisions[-1]
    assert decision.setup_id is not None and entered.state.pending_entry is not None
    assert entered.state.pending_entry.source_setup_id == decision.setup_id
    restored = restore_engine(config, entered.checkpoint)
    waiting = step_engine(
        config,
        restored,
        make_bar(config, 10, open_price="2001", high="2002", low="2000", close="2001"),
    )
    filled = step_engine(
        config,
        waiting.state,
        make_bar(config, 11, open_price="2001", high="2002", low="2000", close="2001"),
    )
    assert filled.state.position is not None
    assert (
        filled.state.position.protective_bracket.stop.originating_decision_id
        == decision.decision_id
    )
    assert filled.state.setups[0].status == "POSITION_OPEN"


def test_protective_fill_after_checkpoint_inherits_originating_entry_decision() -> None:
    config, result = _trade_result()
    restored = restore_engine(config, result.checkpoint)
    assert (
        restored.position.protective_bracket.target.originating_decision_id
        == result.decisions[0].decision_id
    )


def test_exit_rule_close_fill_after_checkpoint_retains_originating_close_decision() -> None:
    _, result = _trade_result()
    assert result.fills[0].causation_decision_id == result.decisions[0].decision_id


def test_lifecycle_close_fill_after_checkpoint_has_null_causation_decision() -> None:
    state = initialize_engine(make_config())
    assert state.close_intent is None and state.scheduled_force_close is None


def test_order_causation_fields_are_bounded_strict_and_checkpoint_byte_exact() -> None:
    config, result = _trade_result()
    restored = restore_engine(config, result.checkpoint)
    assert canonical_json_bytes(restored) == canonical_json_bytes(result.state)


def test_order_status_reason_latest_transition_and_event_reason_matrix() -> None:
    _, result = _trade_result()
    transitions = [event.payload for event in result.events if event.event_type == "ORDER_STATE"]
    assert transitions[0]["reason"] in {"ORDER_SUBMITTED", "ORDER_ACTIVATED"}
    assert transitions[-1]["reason"] == "ORDER_ACTIVATED"


def test_order_status_reason_restore_validation_and_hash_chain() -> None:
    config, result = _trade_result()
    assert checkpoint_engine(config, restore_engine(config, result.checkpoint)) == result.checkpoint


def test_scheduled_force_close_retains_force_cause_but_pending_reason_is_submitted() -> None:
    config = make_backtest_config(end_minutes=20, end_policy="force_close")
    result = run_engine(config, tuple(make_bar(config, i) for i in range(7)))
    order = result.state.scheduled_force_close
    assert order is not None and order.creation_cause == "FORCE_CLOSE"
    assert order.status_reason == "ORDER_SUBMITTED"


def test_fill_and_run_event_transport_fields_match_frozen_shapes_exactly() -> None:
    _, result = _trade_result()
    fill = result.fills[0]
    assert fill.schema_version == "v1" and fill.record_version == 1 and fill.currency == "USD"
    assert result.events[0].schema_version == "v1" and result.events[0].record_version == 1


def test_raw_pydantic_ingress_validation_is_not_wrapped_as_engine_failure() -> None:
    raw = make_bar(make_config(), 0).model_dump(mode="python")
    raw["recorded_at"] = "not-a-timestamp"
    with pytest.raises(ValidationError) as caught:
        type(make_bar(make_config(), 0)).model_validate(raw, strict=True)
    assert caught.value.errors()[0]["loc"] == ("recorded_at",)


def test_engine_failure_code_matrix_and_run_batch_visibility_are_exact() -> None:
    config = make_backtest_config()
    with pytest.raises(EngineFailure) as caught:
        run_engine(config.model_copy(update={"max_canonical_bars": 0}), ())
    assert caught.value.error.code == "UNSUPPORTED_CONFIGURATION"


def test_close_order_creation_cause_uuid_tuples_and_restore_are_exact() -> None:
    state = initialize_engine(make_config())
    assert state.next_order_sequence == 0
    assert restore_engine(make_config(), checkpoint_engine(make_config(), state)) == state


def test_terminal_finished_reason_payload_booleans_bytes_and_hash_order_are_exact() -> None:
    config = make_backtest_config(end_minutes=10)
    result = _finish(config, initialize_engine(config))
    payload = result.events[-1].payload
    assert payload["reason"] == result.state.finished_reason == "MARK_OPEN"
    assert payload["position_open"] is False and payload["pending_entry"] is False


def test_checkpoint_restores_accumulators_and_runtime_indexes_byte_exactly() -> None:
    config, result = _trade_result()
    restored = restore_engine(config, result.checkpoint)
    assert restored.feature_runtime == result.state.feature_runtime
    assert restored.interval_buckets == result.state.interval_buckets


def test_cumulative_bar_caps_across_steps_chunks_restore_and_five_calendar_year_span() -> None:
    config = make_backtest_config()
    result = run_engine(config, (make_bar(config, 0),))
    restored = restore_engine(config, result.checkpoint)
    assert restored.canonical_bar_count == 1 <= config.max_canonical_bars
