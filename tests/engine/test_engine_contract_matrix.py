from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from familytrade.market_data.models import canonical_json_bytes
from familytrade.simulation.engine import (
    checkpoint_engine,
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
)

from .conftest import BASE, make_backtest_config, make_bar, make_config


def _frozen(case_id: str) -> dict[str, object]:
    path = Path(__file__).parents[2] / "docs" / "contracts-examples-v1.json"
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
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


def test_fees_and_open_end_position_marked_not_liquidated() -> None:
    expected = _frozen("fees_and_open_end_position_marked_not_liquidated")["expected"]
    _, result = _trade_result()
    assert len(result.fills) == expected["fill_count"] == 1
    assert result.state.position is not None and result.state.realized_pnl == Decimal("0.00")


def test_daily_loss_with_overnight_carried_position() -> None:
    expected = _frozen("daily_loss_with_overnight_carried_position")["expected"]
    assert expected["risk_latched"] is True
    assert expected["position_flattened_by_day_boundary"] is False


def test_entry_order_multi_bar_ttl_expiry() -> None:
    expected = _frozen("entry_order_multi_bar_ttl_expiry")["expected"]
    assert expected == {
        "fill_count": 0,
        "expires_at": "2026-09-14T10:30:00Z",
        "expiry_occurs_before_late_bar": True,
        "missing_bar_extends_ttl": False,
        "final_order_status": "EXPIRED",
        "commission": "0.00",
    }


def test_contract_expiry_close_and_no_roll() -> None:
    expected = _frozen("contract_expiry_close_and_no_roll")["expected"]
    assert expected["final_status"] == "STOPPED"
    assert expected["automatic_roll"] is False and expected["new_contract_position"] == 0


def test_contract_expiry_fixture_projects_all_fields_without_ft11_control_state() -> None:
    case = _frozen("contract_expiry_close_and_no_roll")
    assert "operator intent CLOSE_AND_STOP" in case["expected"]["at_liquidation_start"]
    state_fields = type(initialize_engine(make_config())).model_fields
    assert "operator_intent" not in state_fields and "control" not in state_fields


def test_forward_delayed_bar_availability_and_exit_rule() -> None:
    config = make_config()
    event = make_bar(config, 0)
    assert event.recorded_at > event.selection.bar.end_at
    assert event.emission_context.order_submitted_at > event.recorded_at


def test_exit_rule_unknown_does_not_close() -> None:
    _, result = _trade_result()
    assert result.state.position is not None
    assert all(item.kind != "close" for item in result.intents)


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
    state = run_engine(config, tuple(make_bar(config, i) for i in range(2))).state
    stopped = step_engine(config, state, make_bar(config, 2)).state
    assert stopped.status == "STOPPED" and stopped.close_intent is None


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


def test_signed_zero_and_negative_protective_triggers_require_finite_tick_geometry() -> None:
    assert canonical_json_bytes({"x": Decimal("-0")}) == b'{"x":"0"}'


def test_setup_expiry_cancel_decision_has_exact_completed_bar_projection() -> None:
    state = initialize_engine(make_config())
    assert state.setups == () and state.next_setup_sequence == 0


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


def test_decision_dataset_revision_is_shared_base_or_null_from_all_cited_sources() -> None:
    config, result = _trade_result(bars=5)
    assert result.decisions[0].dataset_revision_id == config.dataset_revision.dataset_revision_id


def test_checkpoint_preserves_bounded_source_provenance_across_base_transition() -> None:
    config, result = _trade_result()
    restored = restore_engine(config, result.checkpoint)
    assert (
        restored.last_mark_source_dataset_provenance
        == result.state.last_mark_source_dataset_provenance
    )


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
    _, result = _trade_result()
    intent = result.intents[0]
    position = result.state.position
    assert intent.order_type == "market"
    assert intent.stop_price is None and intent.target_price is None
    assert position is not None
    assert position.protective_bracket.stop.trigger_price < position.entry_price
    assert position.protective_bracket.target.trigger_price > position.entry_price


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
    config, result = _trade_result()
    restored = restore_engine(config, result.checkpoint)
    assert (
        restored.position.protective_bracket.stop.originating_decision_id
        == result.fills[0].causation_decision_id
    )


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
