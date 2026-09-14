"""Bounded raw parsing and static validation for FT-06 definitions."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from familytrade.market_data.models import CalendarVersion
from familytrade.strategies.definitions import (
    DefinitionValidationResult,
    RuleDefinition,
    RuleNode,
    ValidationIssue,
    ValidationIssueCode,
)

CalendarKey = tuple[str, int]
_DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_FEATURES: dict[str, tuple[str, str, set[str]]] = {
    **{
        name: ("price", "contract_price", set())
        for name in ("open", "high", "low", "close", "hl2", "typical")
    },
    "volume": ("volume", "contract_volume", set()),
    "sma_v1": ("variable", "variable", {"n", "input"}),
    "ema_v1": ("variable", "variable", {"n", "input"}),
    "rsi_wilder_v1": ("decimal", "ratio_0_100", {"n", "input"}),
    "atr_wilder_v1": ("price", "contract_price", {"n"}),
    "relative_volume_v1": ("decimal", "ratio", {"n"}),
    "session_vwap_v1": ("price", "contract_price", set()),
    "confirmed_pivot_v1": ("level", "contract_price", {"pivot_kind", "left", "right"}),
    "swing_regime_v1": ("regime", "regime", {"left", "right"}),
    "prior_session_high_v1": ("level", "contract_price", set()),
    "prior_session_low_v1": ("level", "contract_price", set()),
    "rolling_high_v1": ("level", "contract_price", {"n"}),
    "rolling_low_v1": ("level", "contract_price", {"n"}),
    "level_touch_v1": ("boolean", "boolean", {"level_feature_id", "tolerance_ticks"}),
    "level_cross": ("boolean", "boolean", {"level_feature_id", "direction"}),
}

_FEATURE_PARAMETER_RULES: dict[str, dict[str, tuple[str, object]]] = {
    "sma_v1": {
        "n": ("integer", (2, 500)),
        "input": ("enum", {"open", "high", "low", "close", "hl2", "typical", "volume"}),
    },
    "ema_v1": {
        "n": ("integer", (2, 500)),
        "input": ("enum", {"open", "high", "low", "close", "hl2", "typical", "volume"}),
    },
    "rsi_wilder_v1": {"n": ("integer", (2, 500)), "input": ("literal", "close")},
    "atr_wilder_v1": {"n": ("integer", (2, 500))},
    "relative_volume_v1": {"n": ("integer", (2, 500))},
    "confirmed_pivot_v1": {
        "pivot_kind": ("enum", {"high", "low"}),
        "left": ("integer", (1, 50)),
        "right": ("integer", (1, 50)),
    },
    "swing_regime_v1": {"left": ("integer", (1, 50)), "right": ("integer", (1, 50))},
    "rolling_high_v1": {"n": ("integer", (2, 500))},
    "rolling_low_v1": {"n": ("integer", (2, 500))},
    "level_touch_v1": {
        "level_feature_id": ("reference", None),
        "tolerance_ticks": ("integer", (0, 100)),
    },
    "level_cross": {
        "level_feature_id": ("reference", None),
        "direction": ("enum", {"above", "below"}),
    },
}

_FEATURE_KINDS: dict[str, str] = {
    **{name: "input" for name in ("open", "high", "low", "close", "hl2", "typical", "volume")},
    **{
        name: "indicator"
        for name in (
            "sma_v1",
            "ema_v1",
            "rsi_wilder_v1",
            "atr_wilder_v1",
            "relative_volume_v1",
            "session_vwap_v1",
        )
    },
    **{name: "structure" for name in ("confirmed_pivot_v1", "swing_regime_v1")},
    **{
        name: "level"
        for name in (
            "prior_session_high_v1",
            "prior_session_low_v1",
            "rolling_high_v1",
            "rolling_low_v1",
            "level_touch_v1",
            "level_cross",
        )
    },
}


def _pointer(parts: tuple[object, ...]) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _issue(path: str, code: ValidationIssueCode, message: str) -> ValidationIssue:
    return ValidationIssue(path=path, code=code, message=message)


def _issue_sort_key(issue: ValidationIssue) -> tuple[int, str, str]:
    if issue.code in {
        ValidationIssueCode.REQUIRED_FOR_LIMIT,
        ValidationIssueCode.FORBIDDEN_FOR_MARKET,
        ValidationIssueCode.NAME_MISMATCH,
        ValidationIssueCode.INVALID_SIDE_ROOT,
    }:
        return (2, issue.path, issue.code.value)
    if issue.code in {
        ValidationIssueCode.SIZE_LIMIT,
        ValidationIssueCode.LOOKBACK_LIMIT,
        ValidationIssueCode.INVALID_MODULE_COMBINATION,
        ValidationIssueCode.ISSUE_LIMIT,
    }:
        return (4, issue.path, issue.code.value)
    return (
        1
        if issue.code
        in {
            ValidationIssueCode.UNKNOWN_FIELD,
            ValidationIssueCode.INVALID_TYPE,
            ValidationIssueCode.REQUIRED,
            ValidationIssueCode.INVALID_ENUM,
            ValidationIssueCode.OUT_OF_RANGE,
            ValidationIssueCode.INVALID_DECIMAL,
            ValidationIssueCode.NONFINITE,
            ValidationIssueCode.DUPLICATE_KEY,
        }
        else 3,
        issue.path,
        issue.code.value,
    )


def _canonical(value: object, *, definition: bool = False) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise TypeError("object keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized:
                raise ValueError("duplicate normalized object key")
            item = _canonical(raw_value, definition=definition)
            if (
                definition
                and key
                in {
                    "multiple",
                    "merge_multiple",
                    "max_width_multiple",
                    "approach_multiple",
                    "stop_buffer_multiple",
                    "measured_move_multiple",
                    "r_multiple",
                    "break_multiple",
                    "pullback_multiple",
                    "tolerance",
                }
                and _decimal(item)
            ):
                decimal = Decimal(cast(str, item))
                item = format(decimal.normalize(), "f")
                if "." in item:
                    item = item.rstrip("0").rstrip(".")
                if item in {"", "-0"}:
                    item = "0"
            normalized[key] = item
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonical(item, definition=definition) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError("not a JSON value")


def _bytes(value: object, *, definition: bool = False) -> bytes:
    return json.dumps(
        _canonical(value, definition=definition),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _raw_safety_exceeded(value: object) -> bool:
    """Apply the bounded direct-Mapping gate before any recursive parser work.

    Counting intentionally mirrors the contract: the root plus every object key,
    object value, and array item is a JSON node.  Container depth starts at one
    for the supplied root mapping.
    """
    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    seen_containers: set[int] = set()
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > 8192 or depth > 64:
            return True
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen_containers:
                return True
            seen_containers.add(identity)
            for key, child in item.items():
                # keys are nodes too, and a JSON object's values are one depth
                # below the object that contains them.
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
        elif isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in seen_containers:
                return True
            seen_containers.add(identity)
            stack.extend((child, depth + 1) for child in item)
    try:
        return len(_bytes(value)) > 524_288
    except TypeError, ValueError, RecursionError:
        return False


def canonical_operation_request_bytes(value: Mapping[str, object]) -> bytes:
    return _bytes(value)


def canonical_definition_bytes(definition: RuleDefinition) -> bytes:
    return _bytes(definition.model_dump(mode="json"), definition=True)


def canonical_definition_sha256(definition: RuleDefinition) -> str:
    return hashlib.sha256(canonical_definition_bytes(definition)).hexdigest()


def _as_model_input(value: object) -> object:
    """JSON arrays are represented as tuples by the immutable public models."""
    if isinstance(value, Mapping):
        return {key: _as_model_input(item) for key, item in value.items()}
    if isinstance(value, list):
        return tuple(_as_model_input(item) for item in value)
    return value


def _pydantic_issues(value: Mapping[str, object]) -> list[ValidationIssue]:
    try:
        RuleDefinition.model_validate(_as_model_input(value))
    except ValidationError as exc:
        result: list[ValidationIssue] = []
        for error in exc.errors(include_input=False):
            kind = str(error["type"])
            code = (
                ValidationIssueCode.UNKNOWN_FIELD
                if kind == "extra_forbidden"
                else ValidationIssueCode.REQUIRED
                if kind == "missing"
                else ValidationIssueCode.INVALID_ENUM
                if "literal" in kind or "tag" in kind
                else ValidationIssueCode.INVALID_TYPE
                if kind.endswith("_type")
                else ValidationIssueCode.OUT_OF_RANGE
            )
            loc = tuple(
                part
                for part in error["loc"]
                if not isinstance(part, str)
                or part
                not in {"feature", "constant", "arithmetic", "compare", "temporal_compare", "group"}
            )
            result.append(_issue(_pointer(loc), code, "Invalid strategy definition field."))
        return result
    return []


def _decimal(value: object) -> bool:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        return False
    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def _independent_raw_rules(value: Mapping[str, object]) -> list[ValidationIssue]:
    """Rules whose operands remain observable despite unrelated shape errors."""
    issues: list[ValidationIssue] = []
    policy = value.get("order_policy")
    if (
        isinstance(policy, Mapping)
        and policy.get("entry_type") == "limit"
        and policy.get("limit_price_source") is None
    ):
        issues.append(
            _issue(
                "/order_policy/limit_price_source",
                ValidationIssueCode.REQUIRED_FOR_LIMIT,
                "Limit orders require a price source.",
            )
        )
    features = value.get("features")
    signatures: dict[str, tuple[object, object]] = {}
    if isinstance(features, list):
        for feature in features:
            if isinstance(feature, Mapping) and isinstance(feature.get("feature_id"), str):
                signatures[feature["feature_id"]] = (
                    feature.get("output_type"),
                    feature.get("unit"),
                )
    nodes = value.get("nodes")
    feature_nodes: dict[str, str] = {}
    if isinstance(nodes, list):
        for node in nodes:
            if (
                isinstance(node, Mapping)
                and node.get("kind") == "feature"
                and isinstance(node.get("node_id"), str)
                and isinstance(node.get("feature_id"), str)
            ):
                feature_nodes[node["node_id"]] = node["feature_id"]
        for index, node in enumerate(nodes):
            if (
                not isinstance(node, Mapping)
                or node.get("kind") != "arithmetic"
                or node.get("op") not in {"add", "subtract", "min", "max"}
            ):
                continue
            args = node.get("args")
            if isinstance(args, list):
                pairs = [
                    signatures[feature_nodes[arg]]
                    for arg in args
                    if isinstance(arg, str)
                    and arg in feature_nodes
                    and feature_nodes[arg] in signatures
                ]
                if len(set(pairs)) > 1:
                    issues.append(
                        _issue(
                            f"/nodes/{index}/args",
                            ValidationIssueCode.UNIT_MISMATCH,
                            "Arithmetic units must match.",
                        )
                    )
    return issues


def _node_references(node: RuleNode) -> tuple[str, ...]:
    """Return node edges without interpreting an unknown or partial node."""
    if node.kind == "arithmetic":
        return node.args
    if node.kind == "group":
        return node.children
    if node.kind == "compare":
        return (node.left, node.right)
    if node.kind == "temporal_compare":
        return (node.left_feature, node.right_feature)
    return ()


def _graph_issues(definition: RuleDefinition) -> list[ValidationIssue]:
    """Validate graph reachability and the closed operand algebra.

    This deliberately operates only after the strict record parser succeeded: a
    malformed union must not be guessed into a graph variant.
    """
    nodes = {node.node_id: node for node in definition.nodes}
    issues: list[ValidationIssue] = []
    index_by_id = {node.node_id: index for index, node in enumerate(definition.nodes)}
    for node_id, node in nodes.items():
        index = index_by_id[node_id]
        if node.kind == "group":
            for item, ref in enumerate(node.children):
                if ref not in nodes:
                    issues.append(
                        _issue(
                            f"/nodes/{index}/children/{item}",
                            ValidationIssueCode.UNKNOWN_REFERENCE,
                            "Node is unknown.",
                        )
                    )
        elif node.kind == "temporal_compare":
            for field, ref in (
                ("left_feature", node.left_feature),
                ("right_feature", node.right_feature),
            ):
                if ref not in nodes:
                    issues.append(
                        _issue(
                            f"/nodes/{index}/{field}",
                            ValidationIssueCode.UNKNOWN_REFERENCE,
                            "Node is unknown.",
                        )
                    )

    # A DFS identifies every cyclic component.  Reporting its lexical minimum is
    # stable regardless of source array order.
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []
    cycles: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            cycles.add(min(stack[stack.index(node_id) :]))
            return
        visiting.add(node_id)
        stack.append(node_id)
        for ref in _node_references(nodes[node_id]):
            if ref in nodes:
                visit(ref)
        stack.pop()
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in sorted(nodes):
        visit(node_id)
    for node_id in sorted(cycles):
        issues.append(
            _issue(
                f"/nodes/{index_by_id[node_id]}/node_id",
                ValidationIssueCode.CYCLE,
                "Node dependency graph contains a cycle.",
            )
        )

    features = {
        feature.feature_id: (feature.output_type, feature.unit) for feature in definition.features
    }
    for node_id, node in nodes.items():
        if node.kind != "constant":
            continue
        index = index_by_id[node_id]
        path = f"/nodes/{index}/value"
        value = node.value
        expected_unit = {
            "price": "contract_price",
            "volume": "contract_volume",
            "integer": "count",
            "boolean": "boolean",
        }.get(node.value_type)
        if expected_unit is not None and node.unit != expected_unit:
            issues.append(
                _issue(path, ValidationIssueCode.UNIT_MISMATCH, "Constant type and unit disagree.")
            )
        decimal_types = {"decimal", "price", "volume", "level"}
        if node.value_type in decimal_types:
            if not _decimal(value):
                issues.append(
                    _issue(
                        path,
                        ValidationIssueCode.INVALID_DECIMAL,
                        "Constant must be a canonical decimal string.",
                    )
                )
            else:
                decimal_value = Decimal(cast(str, value))
                if node.value_type == "volume" and decimal_value < 0:
                    issues.append(
                        _issue(path, ValidationIssueCode.OUT_OF_RANGE, "Volume cannot be negative.")
                    )
                elif node.unit == "ratio_0_100" and not (0 <= decimal_value <= 100):
                    issues.append(
                        _issue(path, ValidationIssueCode.OUT_OF_RANGE, "Ratio is out of range.")
                    )
        elif (
            node.value_type == "integer" and (not isinstance(value, int) or isinstance(value, bool))
        ) or (node.value_type == "boolean" and not isinstance(value, bool)):
            issues.append(
                _issue(path, ValidationIssueCode.INVALID_TYPE, "Constant has an invalid type.")
            )
        elif node.value_type == "timestamp":
            try:
                parsed_timestamp = datetime.fromisoformat(value) if isinstance(value, str) else None
            except ValueError:
                parsed_timestamp = None
            if (
                not isinstance(value, str)
                or not value.endswith("Z")
                or parsed_timestamp is None
                or parsed_timestamp.utcoffset() != timedelta(0)
            ):
                issues.append(
                    _issue(path, ValidationIssueCode.INVALID_TYPE, "Timestamp must be UTC RFC3339.")
                )
        elif node.value_type == "side" and value not in {"long", "short"}:
            issues.append(
                _issue(path, ValidationIssueCode.INVALID_ENUM, "Side constant is invalid.")
            )
        elif node.value_type == "regime" and value not in {
            "bullish",
            "bearish",
            "sideways",
            "unknown",
        }:
            issues.append(
                _issue(path, ValidationIssueCode.INVALID_ENUM, "Regime constant is invalid.")
            )
    signatures: dict[str, tuple[str, str] | None] = {}
    resolving: set[str] = set()

    def signature(node_id: str) -> tuple[str, str] | None:
        if node_id in signatures:
            return signatures[node_id]
        if node_id in resolving or node_id not in nodes:
            return None
        resolving.add(node_id)
        node = nodes[node_id]
        result: tuple[str, str] | None
        if node.kind == "feature":
            result = features.get(node.feature_id)
        elif node.kind == "constant":
            result = (node.value_type, node.unit)
        elif node.kind in {"compare", "temporal_compare", "group"}:
            result = ("boolean", "boolean")
        elif node.kind == "arithmetic":
            result = (node.result_type, node.unit)
        else:  # pragma: no cover - RuleNode is a closed discriminated union.
            result = None
        resolving.remove(node_id)
        signatures[node_id] = result
        return result

    numeric = {"decimal", "integer", "price", "volume", "level"}
    for node_id, node in nodes.items():
        index = index_by_id[node_id]
        if node.kind == "group":
            if any(
                signature(ref) != ("boolean", "boolean") for ref in node.children if ref in nodes
            ):
                issues.append(
                    _issue(
                        f"/nodes/{index}/children",
                        ValidationIssueCode.TYPE_MISMATCH,
                        "Group children must be boolean.",
                    )
                )
        elif node.kind == "compare":
            left, right = signature(node.left), signature(node.right)
            if left is not None and right is not None:
                if node.op == "within":
                    tolerance_ok = (
                        node.tolerance is not None
                        and _decimal(node.tolerance)
                        and Decimal(node.tolerance) >= 0
                    )
                    if left[0] not in numeric or left != right:
                        issues.append(
                            _issue(
                                f"/nodes/{index}/right",
                                ValidationIssueCode.TYPE_MISMATCH
                                if left[0] != right[0]
                                else ValidationIssueCode.UNIT_MISMATCH,
                                "Comparison operands are incompatible.",
                            )
                        )
                    if not tolerance_ok:
                        issues.append(
                            _issue(
                                f"/nodes/{index}/tolerance",
                                ValidationIssueCode.INVALID_DECIMAL,
                                "Tolerance must be a nonnegative decimal.",
                            )
                        )
                elif left != right:
                    issues.append(
                        _issue(
                            f"/nodes/{index}/right",
                            ValidationIssueCode.TYPE_MISMATCH
                            if left[0] != right[0]
                            else ValidationIssueCode.UNIT_MISMATCH,
                            "Comparison operands are incompatible.",
                        )
                    )
                elif node.op in {"lt", "lte", "gt", "gte"} and left[0] not in numeric | {
                    "timestamp"
                }:
                    issues.append(
                        _issue(
                            f"/nodes/{index}/left",
                            ValidationIssueCode.TYPE_MISMATCH,
                            "Ordered comparison requires numeric values.",
                        )
                    )
        elif node.kind == "temporal_compare":
            left_node, right_node = nodes.get(node.left_feature), nodes.get(node.right_feature)
            if left_node is not None and right_node is not None:
                if (
                    left_node.kind != "feature"
                    or right_node.kind != "feature"
                    or left_node.offset != 0
                    or right_node.offset != 0
                ):
                    issues.append(
                        _issue(
                            f"/nodes/{index}",
                            ValidationIssueCode.TYPE_MISMATCH,
                            "Temporal comparison requires offset-zero feature nodes.",
                        )
                    )
                else:
                    left_signature = signature(left_node.node_id)
                    right_signature = signature(right_node.node_id)
                    if (
                        left_signature is None
                        or right_signature is None
                        or left_signature != right_signature
                        or left_signature[0] not in numeric
                    ):
                        issues.append(
                            _issue(
                                f"/nodes/{index}",
                                ValidationIssueCode.TYPE_MISMATCH,
                                "Temporal comparison operands are incompatible.",
                            )
                        )
        elif node.kind == "arithmetic":
            args = [signature(ref) for ref in node.args]
            known = [item for item in args if item is not None]
            derived: tuple[str, str] | None = None
            if len(known) == len(args) and all(item[0] in numeric for item in known):
                if node.op in {"add", "subtract", "min", "max"} and len(set(known)) == 1:
                    derived = known[0]
                elif node.op == "multiply" and len(known) == 2:
                    scalars = [item for item in known if item == ("decimal", "scalar")]
                    non_scalars = [item for item in known if item != ("decimal", "scalar")]
                    if (
                        scalars
                        and len(non_scalars) <= 1
                        and all(item[0] != "integer" for item in known)
                    ):
                        derived = non_scalars[0] if non_scalars else ("decimal", "scalar")
                elif node.op == "divide" and len(known) == 2:
                    if known[0] == known[1]:
                        derived = ("decimal", "scalar")
                    elif known[1] == ("decimal", "scalar") and known[0][0] != "integer":
                        derived = known[0]
            if derived is None:
                issues.append(
                    _issue(
                        f"/nodes/{index}/args",
                        ValidationIssueCode.TYPE_MISMATCH,
                        "Arithmetic operands are incompatible.",
                    )
                )
            elif derived != (node.result_type, node.unit):
                issues.append(
                    _issue(
                        f"/nodes/{index}/result_type",
                        ValidationIssueCode.TYPE_MISMATCH
                        if derived[0] != node.result_type
                        else ValidationIssueCode.UNIT_MISMATCH,
                        "Arithmetic result declaration is incompatible.",
                    )
                )

    entry_roots_required = definition.entry_combination in {"rules_only", "setup_and_rules"}
    roots: list[tuple[str, str | None, bool]] = [
        ("/entry_rules/long_root", definition.entry_rules.long_root, entry_roots_required),
        ("/entry_rules/short_root", definition.entry_rules.short_root, entry_roots_required),
        ("/exit_rules/long_root", definition.exit_rules.long_root, False),
        ("/exit_rules/short_root", definition.exit_rules.short_root, False),
    ]
    for path, root, required in roots:
        side = "long" if "/long_" in path else "short"
        if definition.side_policy != "both" and definition.side_policy != side:
            if root is not None:
                issues.append(
                    _issue(
                        path,
                        ValidationIssueCode.INVALID_SIDE_ROOT,
                        "Disabled side root must be null.",
                    )
                )
        elif root is None and required:
            issues.append(
                _issue(
                    path, ValidationIssueCode.INVALID_SIDE_ROOT, "Enabled side root is required."
                )
            )
        elif root is not None and root not in nodes:
            issues.append(
                _issue(path, ValidationIssueCode.UNKNOWN_REFERENCE, "Root node is unknown.")
            )
        elif root is not None and signature(root) != ("boolean", "boolean"):
            issues.append(
                _issue(path, ValidationIssueCode.ROOT_NOT_BOOLEAN, "Root must resolve to boolean.")
            )

    for module_index, module in enumerate(definition.setup_modules):
        for field in ("filter_root", "arm_filter_root", "entry_filter_root"):
            root = getattr(module, field, None)
            if root is not None and root in nodes and signature(root) != ("boolean", "boolean"):
                issues.append(
                    _issue(
                        f"/setup_modules/{module_index}/{field}",
                        ValidationIssueCode.ROOT_NOT_BOOLEAN,
                        "Setup filter root must resolve to boolean.",
                    )
                )

    def expression_depth(node_id: str, seen: set[str]) -> int:
        if node_id in seen or node_id not in nodes:
            return 0
        node = nodes[node_id]
        if node.kind not in {"group", "arithmetic"}:
            return 0
        children = node.children if node.kind == "group" else node.args
        return 1 + max((expression_depth(child, seen | {node_id}) for child in children), default=0)

    over_depth = [
        node_id
        for node_id in sorted(nodes)
        if nodes[node_id].kind in {"group", "arithmetic"} and expression_depth(node_id, set()) > 4
    ]
    if over_depth:
        node_id = over_depth[0]
        issues.append(
            _issue(
                f"/nodes/{index_by_id[node_id]}",
                ValidationIssueCode.SIZE_LIMIT,
                "Expression nesting exceeds four levels.",
            )
        )
    if sum(node.kind in {"compare", "temporal_compare"} for node in nodes.values()) > 64:
        issues.append(
            _issue("/nodes", ValidationIssueCode.SIZE_LIMIT, "Condition leaf limit exceeded.")
        )
    return issues


def _module_issues(definition: RuleDefinition) -> list[ValidationIssue]:
    """Check the static setup dependency and price-source contract.

    The check deliberately models availability only.  It never attempts to
    calculate a setup, select a zone, resolve a price, or create an order.
    """
    issues: list[ValidationIssue] = []
    seen: set[str] = set()
    kinds: set[str] = set()
    enabled_emitting = False
    feature_ids = {feature.feature_id for feature in definition.features}
    for index, module in enumerate(definition.setup_modules):
        if module.kind in seen:
            issues.append(
                _issue(
                    f"/setup_modules/{index}/kind",
                    ValidationIssueCode.INVALID_MODULE_COMBINATION,
                    "Setup kind is duplicated.",
                )
            )
        seen.add(module.kind)
        kinds.add(module.kind)
        if module.kind in {"reversal_setup_v1", "breakout_retest_v1"} and module.enabled:
            enabled_emitting = True
        for field in ("filter_root", "arm_filter_root", "entry_filter_root"):
            root = getattr(module, field, None)
            if root is not None and root not in {node.node_id for node in definition.nodes}:
                issues.append(
                    _issue(
                        f"/setup_modules/{index}/{field}",
                        ValidationIssueCode.UNKNOWN_REFERENCE,
                        "Setup filter root is unknown.",
                    )
                )
        for field in (
            "merge_multiple",
            "max_width_multiple",
            "approach_multiple",
            "stop_buffer_multiple",
            "measured_move_multiple",
            "r_multiple",
            "break_multiple",
            "pullback_multiple",
        ):
            multiple = getattr(module, field, None)
            if multiple is not None and (
                not _decimal(multiple) or not (Decimal(0) < Decimal(multiple) <= Decimal(100))
            ):
                issues.append(
                    _issue(
                        f"/setup_modules/{index}/{field}",
                        ValidationIssueCode.OUT_OF_RANGE,
                        "Multiple must be in (0, 100].",
                    )
                )
    if enabled_emitting and not {"one_position_v1", "confirmed_pivot_zones_v1"} <= kinds:
        issues.append(
            _issue(
                "/setup_modules",
                ValidationIssueCode.INVALID_MODULE_COMBINATION,
                "Enabled setup requires zones and one-position modules.",
            )
        )
    required_roots = (
        definition.entry_rules.long_root is not None
        or definition.entry_rules.short_root is not None
    )
    combination = definition.entry_combination
    invalid_combination = (
        (combination == "rules_only" and (not required_roots or enabled_emitting))
        or (combination == "setups_only" and (required_roots or not enabled_emitting))
        or (combination == "setup_and_rules" and (not required_roots or not enabled_emitting))
        or (combination == "setup_or_rules" and not (required_roots or enabled_emitting))
    )
    if invalid_combination:
        issues.append(
            _issue(
                "/entry_combination",
                ValidationIssueCode.INVALID_MODULE_COMBINATION,
                "Entry combination does not match configured rules and setups.",
            )
        )

    def setup_price_is_backed(source: object) -> bool:
        return getattr(source, "kind", None) != "setup_price" or enabled_emitting

    if not setup_price_is_backed(definition.order_policy.limit_price_source):
        issues.append(
            _issue(
                "/order_policy/limit_price_source",
                ValidationIssueCode.INVALID_MODULE_COMBINATION,
                "Setup price source has no enabled emitting setup.",
            )
        )
    if not setup_price_is_backed(definition.exit_policy.stop):
        issues.append(
            _issue(
                "/exit_policy/stop",
                ValidationIssueCode.INVALID_MODULE_COMBINATION,
                "Setup price source has no enabled emitting setup.",
            )
        )
    if not setup_price_is_backed(definition.exit_policy.target):
        issues.append(
            _issue(
                "/exit_policy/target",
                ValidationIssueCode.INVALID_MODULE_COMBINATION,
                "Setup price source has no enabled emitting setup.",
            )
        )
    for path, multiple in (
        ("/exit_policy/stop/multiple", getattr(definition.exit_policy.stop, "multiple", None)),
        ("/exit_policy/target/multiple", getattr(definition.exit_policy.target, "multiple", None)),
    ):
        if multiple is not None and (
            not _decimal(multiple) or not (Decimal(0) < Decimal(multiple) <= Decimal(100))
        ):
            issues.append(
                _issue(path, ValidationIssueCode.OUT_OF_RANGE, "Multiple must be in (0, 100].")
            )
    for path, source in (
        ("/order_policy/limit_price_source", definition.order_policy.limit_price_source),
        ("/exit_policy/stop", definition.exit_policy.stop),
    ):
        feature_id = getattr(source, "feature_id", None)
        if feature_id is not None and feature_id not in feature_ids:
            issues.append(
                _issue(
                    path + "/feature_id",
                    ValidationIssueCode.UNKNOWN_REFERENCE,
                    "Feature is unknown.",
                )
            )
    return issues


def _window_issues(
    definition: RuleDefinition,
    owner_user_id: str,
    calendar_versions: Mapping[CalendarKey, CalendarVersion],
) -> list[ValidationIssue]:
    """Validate local-window shape and its explicitly supplied calendar binding."""
    issues: list[ValidationIssue] = []
    for index, window in enumerate(definition.constraints.entry_windows):
        path = f"/constraints/entry_windows/{index}"
        invalid_days = (
            len(window.days_of_week) not in range(1, 8)
            or tuple(sorted(window.days_of_week)) != window.days_of_week
            or len(set(window.days_of_week)) != len(window.days_of_week)
            or any(day not in range(1, 8) for day in window.days_of_week)
        )
        invalid = invalid_days
        times_valid = True
        try:
            start = time.fromisoformat(window.start_local)
            end = time.fromisoformat(window.end_local)
            invalid = invalid or start >= end
        except ValueError:
            invalid = True
            times_valid = False
        calendar = calendar_versions.get((window.calendar_id, window.calendar_version))
        if calendar is None:
            invalid = True
        else:
            try:
                zone = ZoneInfo(calendar.exchange_timezone)
                if (
                    calendar.calendar_id != window.calendar_id
                    or calendar.calendar_version != window.calendar_version
                    or calendar.owner_user_id != owner_user_id
                ):
                    invalid = True
                elif times_valid:
                    first = calendar.coverage_start.astimezone(zone).date()
                    last = calendar.coverage_end.astimezone(zone).date()
                    day = first
                    while day <= last:
                        if day.isoweekday() in window.days_of_week:
                            start_at = datetime.combine(day, start, zone).astimezone(UTC)
                            end_at = datetime.combine(day, end, zone).astimezone(UTC)
                            containing = [
                                segment
                                for segment in calendar.windows
                                if segment.start_at <= start_at and end_at <= segment.end_at
                            ]
                            if (
                                start_at < calendar.coverage_start
                                or end_at > calendar.coverage_end
                                or not any(segment.kind == "open" for segment in containing)
                                and not any(
                                    segment.kind == "scheduled_closed" for segment in containing
                                )
                            ):
                                invalid = True
                        day += timedelta(days=1)
            except ZoneInfoNotFoundError:
                invalid = True
        if invalid:
            issues.append(
                _issue(
                    path + "/days_of_week" if invalid_days else path,
                    ValidationIssueCode.OUT_OF_RANGE
                    if invalid_days
                    else ValidationIssueCode.INVALID_WINDOW,
                    "Entry-window weekdays are invalid."
                    if invalid_days
                    else "Entry window is invalid.",
                )
            )
    return issues


def _warmup(definition: RuleDefinition, execution_interval_seconds: int) -> int:
    spans: dict[str, int] = {}
    for feature in definition.features:
        params = feature.parameters
        n = params.get("n", 1)
        n_int = n if isinstance(n, int) and not isinstance(n, bool) else 1
        bars = n_int
        if feature.name in {
            "rsi_wilder_v1",
            "relative_volume_v1",
            "rolling_high_v1",
            "rolling_low_v1",
        }:
            bars += 1
        if feature.name == "swing_regime_v1":
            left, right = params.get("left", 1), params.get("right", 1)
            bars = (
                (left if isinstance(left, int) else 1)
                + 2 * (right if isinstance(right, int) else 1)
                + 2
            )
        elif feature.name == "confirmed_pivot_v1":
            left, right = params.get("left", 1), params.get("right", 1)
            bars = (
                (left if isinstance(left, int) else 1)
                + (right if isinstance(right, int) else 1)
                + 1
            )
        spans[feature.feature_id] = bars * feature.interval_seconds
    nodes = {node.node_id: node for node in definition.nodes}
    cache: dict[str, int] = {}

    def span(node_id: str) -> int:
        if node_id in cache:
            return cache[node_id]
        node = nodes[node_id]
        if node.kind == "feature":
            answer = spans.get(node.feature_id, 0) + node.offset * next(
                f.interval_seconds for f in definition.features if f.feature_id == node.feature_id
            )
        elif node.kind == "constant":
            answer = 0
        elif node.kind == "temporal_compare":
            left_node, right_node = nodes.get(node.left_feature), nodes.get(node.right_feature)
            if (
                left_node is None
                or right_node is None
                or left_node.kind != "feature"
                or right_node.kind != "feature"
            ):
                return 0
            answer = max(span(node.left_feature), span(node.right_feature)) + max(
                next(
                    f.interval_seconds
                    for f in definition.features
                    if f.feature_id == left_node.feature_id
                ),
                next(
                    f.interval_seconds
                    for f in definition.features
                    if f.feature_id == right_node.feature_id
                ),
            )
        else:
            refs = (
                node.args
                if node.kind == "arithmetic"
                else node.children
                if node.kind == "group"
                else (node.left, node.right)
                if node.kind == "compare"
                else ()
            )
            answer = max((span(ref) for ref in refs if ref in nodes), default=0)
        cache[node_id] = answer
        return answer

    roots = [
        root
        for pair in (definition.entry_rules, definition.exit_rules)
        for root in (pair.long_root, pair.short_root)
        if root in nodes
    ]
    for module in definition.setup_modules:
        for field in ("filter_root", "arm_filter_root", "entry_filter_root"):
            root = getattr(module, field, None)
            if root in nodes:
                roots.append(root)
    for source in (
        definition.order_policy.limit_price_source,
        definition.exit_policy.stop,
    ):
        feature_id = getattr(source, "feature_id", None)
        if feature_id is not None:
            for node in definition.nodes:
                if node.kind == "feature" and node.feature_id == feature_id:
                    roots.append(node.node_id)
    return max((math.ceil(span(root) / execution_interval_seconds) for root in roots), default=0)


def _module_warmup(definition: RuleDefinition, execution_interval_seconds: int) -> int:
    """Return only stateful setup readiness, converted once at the boundary."""
    spans: list[int] = []
    for module in definition.setup_modules:
        if module.kind == "confirmed_pivot_zones_v1":
            atr_span = (
                module.atr_length * module.zone_interval_seconds
                if module.use_atr
                else module.zone_interval_seconds
            )
            touches_span = (
                module.pivot_left
                + module.pivot_right
                + 1
                + (module.minimum_touches - 1) * (module.pivot_right + 1)
            ) * module.zone_interval_seconds
            spans.append(max(atr_span, touches_span))
        elif module.kind == "reversal_setup_v1" and module.enabled:
            bars = 2 if module.require_directional_approach else 1
            if module.recent_peak_stop:
                bars = max(bars, module.peak_lookback)
            spans.append(bars * execution_interval_seconds)
        elif module.kind == "breakout_retest_v1" and module.enabled:
            spans.append(
                (2 if module.confirmation_mode == "strict_cross" else 1)
                * execution_interval_seconds
            )
    return max((math.ceil(span / execution_interval_seconds) for span in spans), default=0)


def validate_rule_definition(
    value: Mapping[str, object],
    *,
    owner_user_id: str,
    execution_interval_seconds: int,
    fill_interval_seconds: int,
    calendar_versions: Mapping[CalendarKey, CalendarVersion],
) -> DefinitionValidationResult:
    """Validate configuration statically; this intentionally never evaluates it."""
    if _raw_safety_exceeded(value):
        return DefinitionValidationResult(
            valid=False,
            definition=None,
            errors=(
                _issue("/", ValidationIssueCode.SIZE_LIMIT, "Input exceeds the safety limit."),
            ),
            canonical_definition_sha256=None,
            required_warmup_bars=None,
        )
    issues = _pydantic_issues(value)
    issues.extend(_independent_raw_rules(value))
    try:
        raw_size = len(_bytes(value))
    except TypeError, ValueError:
        return DefinitionValidationResult(
            valid=False,
            definition=None,
            errors=(_issue("/", ValidationIssueCode.INVALID_TYPE, "Input is not JSON."),),
            canonical_definition_sha256=None,
            required_warmup_bars=None,
        )
    if raw_size > 65536:
        issues.append(
            _issue("/", ValidationIssueCode.SIZE_LIMIT, "Definition exceeds its byte limit.")
        )
    if issues:
        return DefinitionValidationResult(
            valid=False,
            definition=None,
            errors=tuple(
                sorted(
                    {(x.path, x.code.value): x for x in issues}.values(),
                    key=_issue_sort_key,
                )
            ),
            canonical_definition_sha256=None,
            required_warmup_bars=None,
        )
    definition = RuleDefinition.model_validate(_as_model_input(value))
    if definition.name != unicodedata.normalize("NFC", definition.name):
        issues.append(
            _issue("/name", ValidationIssueCode.OUT_OF_RANGE, "Name must be NFC normalized.")
        )
    if not (
        fill_interval_seconds > 0
        and fill_interval_seconds <= execution_interval_seconds
        and execution_interval_seconds % fill_interval_seconds == 0
    ):
        issues.append(
            _issue(
                "/",
                ValidationIssueCode.INTERVAL_NOT_DIVISIBLE,
                "Fill interval must divide execution interval.",
            )
        )
    features = {item.feature_id: item for item in definition.features}
    for index, item in enumerate(definition.features):
        path = f"/features/{index}"
        expected = _FEATURES.get(item.name)
        if expected is None:
            issues.append(
                _issue(
                    path + "/name",
                    ValidationIssueCode.UNSUPPORTED_FEATURE,
                    "Feature is unsupported.",
                )
            )
            continue
        expected_type, expected_unit, allowed = expected
        if item.kind != _FEATURE_KINDS[item.name]:
            issues.append(
                _issue(
                    path + "/kind",
                    ValidationIssueCode.TYPE_MISMATCH,
                    "Feature kind differs from catalogue.",
                )
            )
        if expected_type != "variable" and (
            item.output_type != expected_type or item.unit != expected_unit
        ):
            issues.append(
                _issue(
                    path + "/output_type",
                    ValidationIssueCode.TYPE_MISMATCH,
                    "Feature signature differs from catalogue.",
                )
            )
        if item.name in {"sma_v1", "ema_v1"}:
            input_name = item.parameters.get("input")
            wanted = "volume" if input_name == "volume" else "price"
            unit = "contract_volume" if input_name == "volume" else "contract_price"
            if item.output_type != wanted or item.unit != unit:
                issues.append(
                    _issue(
                        path + "/output_type",
                        ValidationIssueCode.TYPE_MISMATCH,
                        "Feature signature differs from input.",
                    )
                )
        for parameter in item.parameters:
            if parameter not in allowed:
                issues.append(
                    _issue(
                        path + "/parameters/" + parameter,
                        ValidationIssueCode.UNSUPPORTED_PARAMETER,
                        "Parameter is unsupported.",
                    )
                )
        if (
            item.interval_seconds not in {60, 300, 900, 1800, 3600}
            or item.interval_seconds > execution_interval_seconds
        ):
            issues.append(
                _issue(
                    path + "/interval_seconds",
                    ValidationIssueCode.INVALID_INTERVAL,
                    "Feature interval is invalid.",
                )
            )
        parameter_rules = _FEATURE_PARAMETER_RULES.get(item.name, {})
        for parameter, (rule, bound) in parameter_rules.items():
            parameter_path = f"{path}/parameters/{parameter}"
            if parameter not in item.parameters:
                issues.append(
                    _issue(
                        parameter_path,
                        ValidationIssueCode.REQUIRED,
                        "Feature parameter is required.",
                    )
                )
                continue
            parameter_value = item.parameters[parameter]
            if rule == "integer":
                lower, upper = cast(tuple[int, int], bound)
                if not isinstance(parameter_value, int) or isinstance(parameter_value, bool):
                    issues.append(
                        _issue(
                            parameter_path,
                            ValidationIssueCode.INVALID_TYPE,
                            "Feature parameter has an invalid type.",
                        )
                    )
                elif not (lower <= parameter_value <= upper):
                    issues.append(
                        _issue(
                            parameter_path,
                            ValidationIssueCode.OUT_OF_RANGE,
                            "Feature parameter is out of range.",
                        )
                    )
            elif (rule == "enum" and parameter_value not in cast(set[str], bound)) or (
                rule == "literal" and parameter_value != bound
            ):
                issues.append(
                    _issue(
                        parameter_path,
                        ValidationIssueCode.INVALID_ENUM,
                        "Feature parameter enum is invalid.",
                    )
                )
            elif rule == "reference" and (
                not isinstance(parameter_value, str) or parameter_value not in features
            ):
                issues.append(
                    _issue(
                        parameter_path,
                        ValidationIssueCode.UNKNOWN_REFERENCE,
                        "Feature reference is unknown.",
                    )
                )
        if (
            item.interval_seconds in {60, 300, 900, 1800, 3600}
            and item.interval_seconds <= execution_interval_seconds
            and execution_interval_seconds % item.interval_seconds
        ):
            issues.append(
                _issue(
                    path + "/interval_seconds",
                    ValidationIssueCode.INTERVAL_NOT_DIVISIBLE,
                    "Feature interval does not divide execution interval.",
                )
            )
    if len(features) != len(definition.features):
        seen: set[str] = set()
        for index, feature in enumerate(definition.features):
            if feature.feature_id in seen:
                issues.append(
                    _issue(
                        f"/features/{index}/feature_id",
                        ValidationIssueCode.DUPLICATE_ID,
                        "Feature ID is duplicated.",
                    )
                )
            seen.add(feature.feature_id)
    nodes = {node.node_id: node for node in definition.nodes}
    if len(nodes) != len(definition.nodes):
        seen_nodes: set[str] = set()
        for index, node in enumerate(definition.nodes):
            if node.node_id in seen_nodes:
                issues.append(
                    _issue(
                        f"/nodes/{index}/node_id",
                        ValidationIssueCode.DUPLICATE_ID,
                        "Node ID is duplicated.",
                    )
                )
            seen_nodes.add(node.node_id)
    for index, node in enumerate(definition.nodes):
        path = f"/nodes/{index}"
        if node.kind == "feature":
            if not 0 <= node.offset <= 2000:
                issues.append(
                    _issue(
                        path + "/offset",
                        ValidationIssueCode.OUT_OF_RANGE,
                        "Offset is out of range.",
                    )
                )
            if node.feature_id not in features:
                issues.append(
                    _issue(
                        path + "/feature_id",
                        ValidationIssueCode.UNKNOWN_REFERENCE,
                        "Feature is unknown.",
                    )
                )
        elif node.kind == "arithmetic":
            if not 2 <= len(node.args) <= 16:
                issues.append(
                    _issue(
                        path + "/args",
                        ValidationIssueCode.OUT_OF_RANGE,
                        "Arithmetic arity is invalid.",
                    )
                )
            for arg_index, ref in enumerate(node.args):
                if ref not in nodes:
                    issues.append(
                        _issue(
                            f"{path}/args/{arg_index}",
                            ValidationIssueCode.UNKNOWN_REFERENCE,
                            "Node is unknown.",
                        )
                    )
        elif node.kind == "group":
            if not 1 <= len(node.children) <= 16:
                issues.append(
                    _issue(
                        path + "/children",
                        ValidationIssueCode.OUT_OF_RANGE,
                        "Group arity is invalid.",
                    )
                )
        elif node.kind == "compare":
            for field, ref in (("left", node.left), ("right", node.right)):
                if ref not in nodes:
                    issues.append(
                        _issue(
                            f"{path}/{field}",
                            ValidationIssueCode.UNKNOWN_REFERENCE,
                            "Node is unknown.",
                        )
                    )
    issues.extend(_graph_issues(definition))
    issues.extend(_module_issues(definition))
    issues.extend(_window_issues(definition, owner_user_id, calendar_versions))
    if (
        definition.order_policy.entry_type == "limit"
        and definition.order_policy.limit_price_source is None
    ):
        issues.append(
            _issue(
                "/order_policy/limit_price_source",
                ValidationIssueCode.REQUIRED_FOR_LIMIT,
                "Limit orders require a price source.",
            )
        )
    if (
        definition.order_policy.entry_type == "market"
        and definition.order_policy.limit_price_source is not None
    ):
        issues.append(
            _issue(
                "/order_policy/limit_price_source",
                ValidationIssueCode.FORBIDDEN_FOR_MARKET,
                "Market orders do not use a limit source.",
            )
        )
    if len(definition.features) > 32:
        issues.append(_issue("/features", ValidationIssueCode.OUT_OF_RANGE, "Too many features."))
    if len(definition.nodes) > 128:
        issues.append(_issue("/nodes", ValidationIssueCode.OUT_OF_RANGE, "Too many nodes."))
    # The fixture's incompatible add is the normative observable unit mismatch.
    for index, node in enumerate(definition.nodes):
        if (
            node.kind == "arithmetic"
            and node.op in {"add", "subtract", "min", "max"}
            and len(node.args) >= 2
        ):
            pairs: list[tuple[str, str]] = []
            for ref in node.args:
                candidate = nodes.get(ref)
                if (
                    candidate is not None
                    and candidate.kind == "feature"
                    and candidate.feature_id in features
                ):
                    f = features[candidate.feature_id]
                    pairs.append((f.output_type, f.unit))
            if len(set(pairs)) > 1:
                issues.append(
                    _issue(
                        f"/nodes/{index}/args",
                        ValidationIssueCode.UNIT_MISMATCH,
                        "Arithmetic units must match.",
                    )
                )
    if issues:
        ordered = tuple(
            sorted(
                {(x.path, x.code.value): x for x in issues}.values(),
                key=_issue_sort_key,
            )
        )
        return DefinitionValidationResult(
            valid=False,
            definition=None,
            errors=(
                ordered[:255]
                + (
                    _issue(
                        "/",
                        ValidationIssueCode.ISSUE_LIMIT,
                        "Additional validation issues were truncated.",
                    ),
                )
                if len(ordered) > 256
                else ordered
            ),
            canonical_definition_sha256=None,
            required_warmup_bars=None,
        )
    historical_warmup = _warmup(definition, execution_interval_seconds)
    if historical_warmup > 2000:
        return DefinitionValidationResult(
            valid=False,
            definition=None,
            errors=(_issue("/", ValidationIssueCode.LOOKBACK_LIMIT, "Lookback limit exceeded."),),
            canonical_definition_sha256=None,
            required_warmup_bars=None,
        )
    warmup = max(historical_warmup, _module_warmup(definition, execution_interval_seconds))
    if warmup > 5_100_050:
        return DefinitionValidationResult(
            valid=False,
            definition=None,
            errors=(
                _issue("/", ValidationIssueCode.SIZE_LIMIT, "Setup warm-up exceeds its limit."),
            ),
            canonical_definition_sha256=None,
            required_warmup_bars=None,
        )
    return DefinitionValidationResult(
        valid=True,
        definition=definition,
        errors=(),
        canonical_definition_sha256=canonical_definition_sha256(definition),
        required_warmup_bars=warmup,
    )
