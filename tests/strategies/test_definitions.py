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
