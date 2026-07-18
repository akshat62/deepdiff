from __future__ import annotations

import math

import pytest

from deepdiff.json_diff import (
    CollectionStrategy,
    CollectionStrategyError,
    DeepJSONDiff,
    DuplicateIdentityError,
    IdentityExtractionError,
)


@pytest.mark.parametrize(
    "path",
    ["$.", "$.users[", "$.users[-1]", "$users"],
)
def test_invalid_path_syntax_variants_are_rejected(path):
    with pytest.raises(CollectionStrategyError):
        CollectionStrategy(path=path)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"path": "$.items", "normalizers": (None,)},
        {"path": "$.items", "filter_func": "not-callable"},
        {"path": "$.items", "compare_as_set": 1},
        {"path": "$.items", "compare_as_set": True, "match_by": ("id",)},
        {"path": "$.items", "priority": "high"},
        {"path": "$.items", "name": 1},
        {"path": "$.items", "sort_by": None},
        {"path": "$.items", "exclude_fields": 1},
    ],
)
def test_additional_invalid_strategy_configuration_is_rejected(kwargs):
    with pytest.raises(CollectionStrategyError):
        CollectionStrategy(**kwargs)


def test_root_and_numeric_index_patterns_are_supported():
    root_result = DeepJSONDiff(
        [{"id": 2}, {"id": 1}],
        [{"id": 1}, {"id": 2}],
        collection_strategies=[CollectionStrategy(path="$", match_by=("id",))],
    )
    indexed_result = DeepJSONDiff(
        {"groups": [{"items": [{"id": 2}, {"id": 1}]}]},
        {"groups": [{"items": [{"id": 1}, {"id": 2}]}]},
        collection_strategies=[
            CollectionStrategy(path="$.groups[0].items", match_by=("id",))
        ],
    )

    assert not root_result
    assert not indexed_result


def test_wildcard_token_types_do_not_cross_mapping_and_list_boundaries():
    array_wildcard = DeepJSONDiff(
        {"named": [{"id": 2}, {"id": 1}]},
        {"named": [{"id": 1}, {"id": 2}]},
        collection_strategies=[CollectionStrategy(path="$[*]", match_by=("id",))],
    )
    key_wildcard = DeepJSONDiff(
        [[{"id": 2}, {"id": 1}]],
        [[{"id": 1}, {"id": 2}]],
        collection_strategies=[CollectionStrategy(path="$.*", match_by=("id",))],
    )

    assert array_wildcard
    assert key_wildcard


def test_ambiguous_equal_specificity_patterns_are_rejected_at_resolution():
    with pytest.raises(CollectionStrategyError, match="ambiguous collection strategies"):
        DeepJSONDiff(
            {"a": {"users": []}},
            {"a": {"users": []}},
            collection_strategies=[
                CollectionStrategy(path="$.*.users"),
                CollectionStrategy(path="$.a.*"),
            ],
        )


def test_non_strategy_entries_are_rejected():
    with pytest.raises(CollectionStrategyError, match="CollectionStrategy instances"):
        DeepJSONDiff({}, {}, collection_strategies=[object()])


def test_non_identifier_mapping_keys_are_rendered_in_stats():
    result = DeepJSONDiff(
        {"a-b": [{"id": 2}, {"id": 1}]},
        {"a-b": [{"id": 1}, {"id": 2}]},
        collection_strategies=[
            CollectionStrategy(path="$.*", match_by=("id",), name="hyphen-key")
        ],
    )

    assert not result
    assert result.get_stats()["$['a-b']"]["strategy"] == "hyphen-key"


def test_identity_scalar_variants_are_type_preserving():
    values = [None, False, True, 1, 1.5, "1"]
    left = {"items": [{"id": value} for value in values]}
    right = {"items": list(reversed(left["items"]))}

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[CollectionStrategy(path="$.items", match_by=("id",))],
    )

    assert not result
    assert len(result.canonical_t1["items"]) == len(values)


def test_extract_supports_list_indexes_and_invalid_index_fallback():
    result = DeepJSONDiff(
        {"items": [{"rank": [2]}, {"rank": [1]}, {"rank": []}]},
        {"items": [{"rank": []}, {"rank": [1]}, {"rank": [2]}]},
        collection_strategies=[CollectionStrategy(path="$.items", sort_by=("rank.0",))],
    )
    invalid_index = DeepJSONDiff(
        {"items": [{"rank": [2]}, {"rank": [1]}]},
        {"items": [{"rank": [1]}, {"rank": [2]}]},
        collection_strategies=[CollectionStrategy(path="$.items", sort_by=("rank.x",))],
    )

    assert not result
    assert invalid_index


def test_stable_sort_handles_non_finite_and_structured_values():
    circular = []
    circular.append(circular)
    left = {"items": [{"rank": float("inf")}, {"rank": {"b": 2, "a": 1}}, {"rank": circular}]}
    right = {"items": list(reversed(left["items"]))}

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[CollectionStrategy(path="$.items", sort_by=("rank",))],
    )

    assert not result
    assert math.isinf(result.canonical_t1["items"][0]["rank"])


def test_compare_as_set_rejects_tuple_and_set_items():
    for item in ((1, 2), {1, 2}):
        with pytest.raises(CollectionStrategyError, match="scalar items only"):
            DeepJSONDiff(
                {"items": [item]},
                {"items": [item]},
                collection_strategies=[
                    CollectionStrategy(path="$.items", compare_as_set=True)
                ],
            )


def test_missing_identity_error_on_right_input():
    with pytest.raises(IdentityExtractionError, match="right input"):
        DeepJSONDiff(
            {"items": []},
            {"items": [{"name": "anonymous"}]},
            collection_strategies=[
                CollectionStrategy(
                    path="$.items", match_by=("id",), missing_identity="error"
                )
            ],
        )


def test_duplicate_identity_error_on_right_input():
    with pytest.raises(DuplicateIdentityError, match="right input"):
        DeepJSONDiff(
            {"items": []},
            {"items": [{"id": 1}, {"id": 1}]},
            collection_strategies=[CollectionStrategy(path="$.items", match_by=("id",))],
        )


def test_facade_repr_dict_and_explicit_json_serializer_delegation():
    result = DeepJSONDiff({"value": 1}, {"value": 2})

    assert "values_changed" in repr(result)
    assert result.to_dict() == result.diff.to_dict()
    assert result.to_json(sort_keys=True, force_use_builtin_json=True)
