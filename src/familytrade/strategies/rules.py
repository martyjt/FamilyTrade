"""Bounded three-valued evaluation for validated FT-06 rule graphs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from familytrade.simulation.state import FeatureValue, RuleResult
from familytrade.strategies.definitions import (
    ArithmeticNode,
    CompareNode,
    ConstantNode,
    FeatureNode,
    GroupNode,
    RuleDefinition,
    TemporalCompareNode,
)


@dataclass(frozen=True, slots=True)
class _Result:
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    value: Decimal | bool | str | None
    unit: str
    sources: tuple[str, ...]
    known_at: datetime
    reason: str


def _unique(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _merge(values: tuple[_Result, ...], anchor: datetime) -> tuple[tuple[str, ...], datetime]:
    sources = _unique(tuple(item for value in values for item in value.sources))
    return sources, max((anchor, *(value.known_at for value in values)))


def evaluate_rule(
    definition: RuleDefinition,
    root_id: str | None,
    feature_history: dict[str, tuple[FeatureValue, ...]],
    anchor: datetime,
    *,
    context: Literal["entry_rule", "exit_rule", "setup_filter"] = "entry_rule",
) -> tuple[str, tuple[RuleResult, ...]]:
    """Evaluate a root and return its status plus every reachable node in lexical order."""
    if root_id is None:
        return "FAIL", ()
    nodes = {node.node_id: node for node in definition.nodes}
    feature_types = {feature.feature_id: feature.output_type for feature in definition.features}
    memo: dict[str, _Result] = {}
    reachable: set[str] = set()

    def visit(node_id: str) -> _Result:
        reachable.add(node_id)
        if node_id in memo:
            return memo[node_id]
        node = nodes[node_id]
        result: _Result
        if isinstance(node, FeatureNode):
            history = feature_history.get(node.feature_id, ())
            value = history[-1 - node.offset] if len(history) > node.offset else None
            if value is None or value.status == "UNKNOWN":
                reason = value.reason_code if value is not None else "NOT_READY_WARMUP"
                result = _Result(
                    "UNKNOWN",
                    None,
                    value.unit if value else "scalar",
                    value.source_bar_record_ids if value else (),
                    value.known_at if value else anchor,
                    reason or "NOT_READY_WARMUP",
                )
            else:
                projected: Decimal | bool | str | None = (
                    Decimal(value.value)
                    if isinstance(value.value, int) and not isinstance(value.value, bool)
                    else value.value
                )
                boolean = feature_types.get(node.feature_id) == "boolean" and isinstance(
                    projected, bool
                )
                status: Literal["PASS", "FAIL", "UNKNOWN"] = (
                    "PASS" if not boolean or projected else "FAIL"
                )
                result = _Result(
                    status,
                    projected,
                    value.unit,
                    value.source_bar_record_ids,
                    value.known_at,
                    "ENTRY_RULE_PASS" if status == "PASS" else "ENTRY_RULE_FAIL",
                )
        elif isinstance(node, ConstantNode):
            constant_value: object = node.value
            if (
                isinstance(constant_value, int)
                and not isinstance(constant_value, bool)
                or isinstance(constant_value, str)
                and node.value_type
                in {
                    "decimal",
                    "price",
                    "volume",
                    "level",
                    "integer",
                }
            ):
                constant_value = Decimal(constant_value)
            constant_status: Literal["PASS", "FAIL"] = (
                "FAIL" if node.value_type == "boolean" and constant_value is False else "PASS"
            )
            result = _Result(
                constant_status,
                constant_value if isinstance(constant_value, (Decimal, bool, str)) else None,
                node.unit,
                (),
                anchor,
                "ENTRY_RULE_PASS" if constant_status == "PASS" else "ENTRY_RULE_FAIL",
            )
        elif isinstance(node, ArithmeticNode):
            args = tuple(visit(item) for item in node.args)
            sources, known = _merge(args, anchor)
            if any(
                item.status == "UNKNOWN" or not isinstance(item.value, Decimal) for item in args
            ):
                result = _Result("UNKNOWN", None, node.unit, sources, known, "RULE_CHILD_UNKNOWN")
            else:
                values = tuple(cast(Decimal, item.value) for item in args)
                try:
                    if node.op == "add":
                        arithmetic_value = sum(values, Decimal(0))
                    elif node.op == "subtract":
                        arithmetic_value = values[0] - values[1]
                    elif node.op == "multiply":
                        arithmetic_value = values[0] * values[1]
                    elif node.op == "divide":
                        if values[1] == 0:
                            raise ZeroDivisionError
                        arithmetic_value = values[0] / values[1]
                    elif node.op == "min":
                        arithmetic_value = min(values)
                    else:
                        arithmetic_value = max(values)
                    result = _Result(
                        "PASS", arithmetic_value, node.unit, sources, known, "ENTRY_RULE_PASS"
                    )
                except ArithmeticError, InvalidOperation:
                    result = _Result(
                        "UNKNOWN",
                        None,
                        node.unit,
                        sources,
                        known,
                        "ZERO_DENOMINATOR" if node.op == "divide" else "NUMERIC_DOMAIN",
                    )
        elif isinstance(node, CompareNode):
            left, right = visit(node.left), visit(node.right)
            sources, known = _merge((left, right), anchor)
            if left.status == "UNKNOWN" or right.status == "UNKNOWN":
                unknown_reason = next(
                    item.reason for item in (left, right) if item.status == "UNKNOWN"
                )
                result = _Result("UNKNOWN", None, "boolean", sources, known, unknown_reason)
            else:
                a, b = left.value, right.value
                if node.op == "lt":
                    passed = a < b  # type: ignore[operator]
                elif node.op == "lte":
                    passed = a <= b  # type: ignore[operator]
                elif node.op == "eq":
                    passed = a == b
                elif node.op == "gte":
                    passed = a >= b  # type: ignore[operator]
                elif node.op == "gt":
                    passed = a > b  # type: ignore[operator]
                else:
                    tolerance = Decimal(node.tolerance or "0")
                    passed = abs(a - b) <= tolerance  # type: ignore[operator]
                result = _Result(
                    "PASS" if passed else "FAIL",
                    passed,
                    "boolean",
                    sources,
                    known,
                    "ENTRY_RULE_PASS" if passed else "ENTRY_RULE_FAIL",
                )
        elif isinstance(node, TemporalCompareNode):
            # Temporal operands are graph node references, while runtime history is
            # keyed by FeatureInstance.feature_id.  Validation guarantees these
            # references name feature nodes; resolve that indirection explicitly.
            left_node = nodes[node.left_feature]
            right_node = nodes[node.right_feature]
            assert isinstance(left_node, FeatureNode)
            assert isinstance(right_node, FeatureNode)
            left_history = feature_history.get(left_node.feature_id, ())
            right_history = feature_history.get(right_node.feature_id, ())
            temporal_values = (*left_history[-2:], *right_history[-2:])
            sources = _unique(
                tuple(source for item in temporal_values for source in item.source_bar_record_ids)
            )
            known = max((anchor, *(item.known_at for item in temporal_values)))
            if (
                len(left_history) < 2
                or len(right_history) < 2
                or any(item.status == "UNKNOWN" for item in temporal_values)
            ):
                unknown_reason = next(
                    (
                        item.reason_code
                        for item in temporal_values
                        if item.status == "UNKNOWN" and item.reason_code is not None
                    ),
                    "RULE_CHILD_UNKNOWN",
                )
                result = _Result("UNKNOWN", None, "boolean", sources, known, unknown_reason)
            else:
                lp, lc = left_history[-2].value, left_history[-1].value
                rp, rc = right_history[-2].value, right_history[-1].value
                lp_decimal, lc_decimal = cast(Decimal, lp), cast(Decimal, lc)
                rp_decimal, rc_decimal = cast(Decimal, rp), cast(Decimal, rc)
                passed = (
                    (lp_decimal <= rp_decimal and lc_decimal > rc_decimal)
                    if node.op == "crosses_above"
                    else (lp_decimal >= rp_decimal and lc_decimal < rc_decimal)
                )
                result = _Result(
                    "PASS" if passed else "FAIL",
                    passed,
                    "boolean",
                    sources,
                    known,
                    "ENTRY_RULE_PASS" if passed else "ENTRY_RULE_FAIL",
                )
        else:
            assert isinstance(node, GroupNode)
            children = tuple(visit(item) for item in node.children)
            sources, known = _merge(children, anchor)
            statuses = tuple(item.status for item in children)
            if node.op == "all":
                status = (
                    "FAIL"
                    if "FAIL" in statuses
                    else "PASS"
                    if all(x == "PASS" for x in statuses)
                    else "UNKNOWN"
                )
            elif node.op == "any":
                status = (
                    "PASS"
                    if "PASS" in statuses
                    else "FAIL"
                    if all(x == "FAIL" for x in statuses)
                    else "UNKNOWN"
                )
            else:
                status = (
                    "FAIL"
                    if "PASS" in statuses
                    else "PASS"
                    if all(x == "FAIL" for x in statuses)
                    else "UNKNOWN"
                )
            result = _Result(
                status,
                status == "PASS" if status != "UNKNOWN" else None,
                "boolean",
                sources,
                known,
                "RULE_CHILD_UNKNOWN"
                if status == "UNKNOWN"
                else "ENTRY_RULE_PASS"
                if status == "PASS"
                else "ENTRY_RULE_FAIL",
            )
        memo[node_id] = result
        return result

    root = visit(root_id)
    leaf_nodes = {
        node.node_id
        for node in definition.nodes
        if (
            (isinstance(node, FeatureNode) and feature_types.get(node.feature_id) == "boolean")
            or (isinstance(node, ConstantNode) and node.value_type == "boolean")
            or isinstance(node, (CompareNode, TemporalCompareNode))
        )
    }

    def project_reason(item: _Result) -> str:
        if item.status == "UNKNOWN":
            return "FILTER_UNKNOWN" if context == "setup_filter" else item.reason
        if item.status == "PASS":
            return "EXIT_RULE_PASS" if context == "exit_rule" else "ENTRY_RULE_PASS"
        return (
            "FILTER_FAIL"
            if context == "setup_filter"
            else "EXIT_RULE_FAIL"
            if context == "exit_rule"
            else "ENTRY_RULE_FAIL"
        )

    evidence = tuple(
        RuleResult(
            node_id=node_id,
            result=memo[node_id].status,
            value=memo[node_id].value,
            unit=memo[node_id].unit,
            source_bar_record_ids=memo[node_id].sources,
            known_at=memo[node_id].known_at,
            reason_code=project_reason(memo[node_id]),
        )
        for node_id in sorted(reachable & leaf_nodes)
    )
    return root.status, evidence
