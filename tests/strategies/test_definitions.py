import copy
import json
from pathlib import Path

from familytrade.strategies.presets import get_preset
from familytrade.strategies.validation import validate_rule_definition


def _fixture() -> dict[str, object]:
    source = json.loads((Path(__file__).parents[2] / "docs/contracts-examples-v1.json").read_text())
    return copy.deepcopy(
        next(
            case
            for case in source["cases"]
            if case["id"] == "fully_serialized_rule_definition_crossover"
        )["given"]["definition"]
    )


def test_fully_serialized_rule_definition_crossover_round_trips_and_has_warmup_six() -> None:
    result = validate_rule_definition(
        _fixture(),
        owner_user_id="owner",
        execution_interval_seconds=900,
        fill_interval_seconds=60,
        calendar_versions={},
    )
    assert result.valid and result.required_warmup_bars == 6 and result.canonical_definition_sha256


def test_invalid_rule_definition_fixture_returns_all_three_typed_errors() -> None:
    value = _fixture()
    value["nodes"][0]["offset"] = -1
    value["order_policy"]["entry_type"] = "limit"
    value["nodes"].append(
        {
            "kind": "arithmetic",
            "node_id": "bad-add",
            "op": "add",
            "args": ["fast-now", "volume-now"],
            "result_type": "price",
            "unit": "contract_price",
        }
    )
    result = validate_rule_definition(
        value,
        owner_user_id="owner",
        execution_interval_seconds=900,
        fill_interval_seconds=60,
        calendar_versions={},
    )
    assert [(item.path, item.code.value) for item in result.errors] == [
        ("/nodes/0/offset", "OUT_OF_RANGE"),
        ("/order_policy/limit_price_source", "REQUIRED_FOR_LIMIT"),
        ("/nodes/7/args", "UNIT_MISMATCH"),
    ]


def test_presets_return_fresh_frozen_models() -> None:
    assert get_preset("reversal_breakout_mgc_original_v1") is not get_preset(
        "reversal_breakout_mgc_original_v1"
    )


def _validate(value: dict[str, object]):
    return validate_rule_definition(
        value,
        owner_user_id="owner",
        execution_interval_seconds=900,
        fill_interval_seconds=60,
        calendar_versions={},
    )


def test_duplicate_ids_unknown_references_cycles_and_non_boolean_roots_are_rejected() -> None:
    value = _fixture()
    nodes = value["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["node_id"] = "cycle-a"
    nodes[0]["feature_id"] = "missing"
    nodes[1].clear()
    nodes[1].update({"kind": "group", "node_id": "cycle-b", "op": "all", "children": ["cycle-a"]})
    nodes[0].clear()
    nodes[0].update({"kind": "group", "node_id": "cycle-a", "op": "all", "children": ["cycle-b"]})
    value["side_policy"] = "both"
    value["entry_rules"]["long_root"] = "cycle-a"
    value["entry_rules"]["short_root"] = "volume-now"
    value["exit_rules"]["short_root"] = "long-exit"
    result = _validate(value)
    codes = {item.code.value for item in result.errors}
    assert "CYCLE" in codes
    assert "ROOT_NOT_BOOLEAN" in codes


def test_long_short_and_both_require_only_matching_roots() -> None:
    value = _fixture()
    value["side_policy"] = "long"
    value["entry_rules"]["short_root"] = "fast-now"
    value["exit_rules"]["short_root"] = "fast-now"
    result = _validate(value)
    assert [item.path for item in result.errors if item.code.value == "INVALID_SIDE_ROOT"] == [
        "/entry_rules/short_root",
        "/exit_rules/short_root",
    ]


def test_type_unit_operator_matrix_rejects_declared_wrong_arithmetic_result() -> None:
    value = _fixture()
    nodes = value["nodes"]
    assert isinstance(nodes, list)
    nodes.append(
        {
            "kind": "arithmetic",
            "node_id": "sum",
            "op": "add",
            "args": ["fast-now", "slow-now"],
            "result_type": "volume",
            "unit": "contract_volume",
        }
    )
    result = _validate(value)
    assert ("/nodes/7/result_type", "TYPE_MISMATCH") in {
        (item.path, item.code.value) for item in result.errors
    }


def test_feature_parameters_are_closed_typed_and_bounded() -> None:
    value = _fixture()
    features = value["features"]
    assert isinstance(features, list)
    features[0]["parameters"] = {"n": True, "input": "not-a-price", "extra": 1}
    result = _validate(value)
    assert {
        ("/features/0/parameters/n", "INVALID_TYPE"),
        ("/features/0/parameters/input", "INVALID_ENUM"),
        ("/features/0/parameters/extra", "UNSUPPORTED_PARAMETER"),
    } <= {(item.path, item.code.value) for item in result.errors}


def test_raw_byte_node_depth_and_definition_byte_limits_use_exact_boundaries() -> None:
    value = _fixture()
    value["oversized"] = [0] * 8_193
    result = _validate(value)
    assert [(item.path, item.code.value) for item in result.errors] == [("/", "SIZE_LIMIT")]

    nested: dict[str, object] = _fixture()
    cursor = nested
    for _ in range(64):
        child: dict[str, object] = {}
        cursor["nested"] = child
        cursor = child
    result = _validate(nested)
    assert [(item.path, item.code.value) for item in result.errors] == [("/", "SIZE_LIMIT")]


def test_rules_setups_and_combination_modes_require_exact_inputs() -> None:
    value = _fixture()
    modules = value["setup_modules"]
    assert isinstance(modules, list)
    modules.append({"kind": "one_position_v1"})
    value["entry_combination"] = "setups_only"
    result = _validate(value)
    assert {
        ("/setup_modules/1/kind", "INVALID_MODULE_COMBINATION"),
        ("/entry_combination", "INVALID_MODULE_COMBINATION"),
    } <= {(item.path, item.code.value) for item in result.errors}


def test_setup_price_sources_require_an_enabled_emitting_setup() -> None:
    value = _fixture()
    value["order_policy"]["entry_type"] = "limit"
    value["order_policy"]["limit_price_source"] = {"kind": "setup_price", "field": "entry"}
    result = _validate(value)
    assert ("/order_policy/limit_price_source", "INVALID_MODULE_COMBINATION") in {
        (item.path, item.code.value) for item in result.errors
    }


def test_empty_entry_windows_needs_no_calendar_and_does_not_imply_flattening() -> None:
    assert _validate(_fixture()).valid


def test_entry_window_requires_ordered_local_range_and_known_calendar() -> None:
    value = _fixture()
    value["constraints"]["entry_windows"] = [
        {
            "days_of_week": [2, 1, 1],
            "start_local": "10:00:00",
            "end_local": "09:00:00",
            "calendar_id": "00000000-0000-7000-8000-000000000000",
            "calendar_version": 1,
        }
    ]
    result = _validate(value)
    assert ("/constraints/entry_windows/0", "INVALID_WINDOW") in {
        (item.path, item.code.value) for item in result.errors
    }
