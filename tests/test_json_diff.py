from copy import deepcopy

import pytest

from deepdiff import DeepDiff
from deepdiff.json_diff import (
    CollectionStrategy,
    CollectionStrategyError,
    DeepJSONDiff,
    DuplicateIdentityError,
    DuplicateIdentityPolicy,
    IdentityExtractionError,
    MissingIdentityPolicy,
)


def test_existing_deepdiff_behavior_is_unchanged():
    left = [{"id": 1}, {"id": 2}]
    right = [{"id": 2}, {"id": 1}]

    legacy = DeepDiff(left, right)

    assert legacy
    assert "values_changed" in legacy


def test_identity_matching_ignores_reordering():
    left = {"users": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]}
    right = {"users": [{"id": 2, "name": "B"}, {"id": 1, "name": "A"}]}

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[CollectionStrategy(path="$.users", match_by=("id",))],
    )

    assert not result


def test_identity_matching_reports_add_remove_and_change_without_cascade():
    left = {"users": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]}
    right = {"users": [{"id": 2, "name": "B2"}, {"id": 3, "name": "C"}]}

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[CollectionStrategy(path="$.users", match_by=("id",))],
    )
    rendered = result.to_json(sort_keys=True)

    assert "dictionary_item_removed" in result
    assert "dictionary_item_added" in result
    assert "values_changed" in result
    assert '\\"int\\",1' in rendered
    assert '\\"int\\",2' in rendered
    assert '\\"int\\",3' in rendered


def test_nested_wildcard_strategy_matches_each_collection():
    left = {
        "accounts": [
            {"users": [{"id": 1}, {"id": 2}]},
            {"users": [{"id": 3}, {"id": 4}]},
        ]
    }
    right = {
        "accounts": [
            {"users": [{"id": 2}, {"id": 1}]},
            {"users": [{"id": 4}, {"id": 3}]},
        ]
    }

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[
            CollectionStrategy(path="$.accounts[*].users", match_by=("id",))
        ],
    )

    assert not result


def test_array_wildcard_matches_exactly_one_index():
    left = {"accounts": [{"nested": [{"users": [{"id": 2}, {"id": 1}]}]}]}
    right = {"accounts": [{"nested": [{"users": [{"id": 1}, {"id": 2}]}]}]}

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[
            CollectionStrategy(path="$.accounts[*].users", match_by=("id",))
        ],
    )

    assert result
    assert "values_changed" in result


def test_key_wildcard_matches_exactly_one_key():
    left = {"a": {"users": [{"id": 2}, {"id": 1}]}}
    right = {"a": {"users": [{"id": 1}, {"id": 2}]}}

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[CollectionStrategy(path="$.*.users", match_by=("id",))],
    )

    assert not result


def test_composite_nested_identity():
    left = {
        "items": [
            {"product": {"id": "A"}, "warehouse": {"code": "IN"}, "qty": 1},
            {"product": {"id": "A"}, "warehouse": {"code": "US"}, "qty": 2},
        ]
    }
    right = {
        "items": [
            {"product": {"id": "A"}, "warehouse": {"code": "US"}, "qty": 3},
            {"product": {"id": "A"}, "warehouse": {"code": "IN"}, "qty": 1},
        ]
    }

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[
            CollectionStrategy(
                path="$.items",
                match_by=("product.id", "warehouse.code"),
            )
        ],
    )

    assert set(result) == {"values_changed"}
    assert "qty" in result.to_json()


def test_identity_encoding_is_collision_safe():
    left = {
        "items": [
            {"first": "a|str:b", "second": "", "value": 1},
            {"first": "a", "second": "b", "value": 2},
        ]
    }
    right = {
        "items": [
            {"first": "a", "second": "b", "value": 2},
            {"first": "a|str:b", "second": "", "value": 1},
        ]
    }

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[
            CollectionStrategy(path="$.items", match_by=("first", "second"))
        ],
    )

    assert not result


def test_identity_values_must_be_json_scalars():
    with pytest.raises(IdentityExtractionError, match="must resolve to a JSON scalar"):
        DeepJSONDiff(
            {"items": [{"id": {"nested": 1}}]},
            {"items": []},
            collection_strategies=[
                CollectionStrategy(path="$.items", match_by=("id",))
            ],
        )


def test_non_finite_float_identity_is_rejected():
    with pytest.raises(IdentityExtractionError, match="must be finite"):
        DeepJSONDiff(
            {"items": [{"id": float("nan")}]},
            {"items": []},
            collection_strategies=[
                CollectionStrategy(path="$.items", match_by=("id",))
            ],
        )


def test_filter_is_symmetric_observable_and_cannot_mutate_inputs():
    left = {"users": [{"id": 1, "active": True}, {"id": 2, "active": False}]}
    right = {"users": [{"id": 2, "active": False}, {"id": 1, "active": True}]}
    original_left = deepcopy(left)
    original_right = deepcopy(right)

    def mutating_filter(item):
        active = item.get("active") is True
        item["observed"] = True
        return active

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[
            CollectionStrategy(
                path="$.users",
                match_by=("id",),
                filter_func=mutating_filter,
            )
        ],
    )

    assert not result
    assert result.get_stats()["$.users"]["filtered_left"] == 1
    assert result.get_stats()["$.users"]["filtered_right"] == 1
    assert left == original_left
    assert right == original_right


def test_sort_by_is_deterministic_for_missing_null_and_mixed_values():
    left = {
        "items": [
            {"rank": "2"},
            {"rank": None},
            {"name": "missing"},
            {"rank": 1},
        ]
    }
    right = {
        "items": [
            {"rank": 1},
            {"name": "missing"},
            {"rank": "2"},
            {"rank": None},
        ]
    }

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[CollectionStrategy(path="$.items", sort_by=("rank",))],
    )

    assert not result


def test_sort_by_uses_numeric_ordering():
    result = DeepJSONDiff(
        {"items": [{"rank": 10, "value": "ten"}, {"rank": 2, "value": "two"}]},
        {"items": [{"rank": 2, "value": "two"}, {"rank": 10, "value": "ten"}]},
        collection_strategies=[CollectionStrategy(path="$.items", sort_by=("rank",))],
    )

    assert not result
    assert result.canonical_t1["items"][0]["rank"] == 2


def test_compare_as_set_for_primitives_preserves_repetition():
    result = DeepJSONDiff(
        {"roles": ["USER", "ADMIN", "USER"]},
        {"roles": ["USER", "USER", "ADMIN"]},
        collection_strategies=[CollectionStrategy(path="$.roles", compare_as_set=True)],
    )

    assert not result


def test_compare_as_set_rejects_non_scalar_items():
    with pytest.raises(CollectionStrategyError, match="supports scalar items only"):
        DeepJSONDiff(
            {"items": [{"id": 1}]},
            {"items": [{"id": 1}]},
            collection_strategies=[
                CollectionStrategy(path="$.items", compare_as_set=True)
            ],
        )


def test_missing_identity_falls_back_by_default():
    left = {"items": [{"id": 1}, {"name": "anonymous"}]}
    right = {"items": [{"name": "anonymous"}, {"id": 1}]}

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[CollectionStrategy(path="$.items", match_by=("id",))],
    )

    assert not result
    stats = result.get_stats()["$.items"]
    assert stats["missing_identity_left"] == 1
    assert stats["missing_identity_right"] == 1


def test_missing_identity_can_be_strict_with_enum_or_string():
    for policy in (MissingIdentityPolicy.ERROR, "error"):
        with pytest.raises(IdentityExtractionError, match=r"left input at \$\.items\[0\]"):
            DeepJSONDiff(
                {"items": [{"name": "anonymous"}]},
                {"items": []},
                collection_strategies=[
                    CollectionStrategy(
                        path="$.items",
                        match_by=("id",),
                        missing_identity=policy,
                    )
                ],
            )


def test_missing_identity_can_be_excluded():
    result = DeepJSONDiff(
        {"items": [{"id": 1}, {"name": "ignored-left"}]},
        {"items": [{"id": 1}, {"name": "ignored-right"}]},
        collection_strategies=[
            CollectionStrategy(
                path="$.items",
                match_by=("id",),
                missing_identity=MissingIdentityPolicy.EXCLUDE,
            )
        ],
    )

    assert not result


def test_duplicate_identity_is_strict_by_default():
    with pytest.raises(DuplicateIdentityError, match=r"left input at \$\.items"):
        DeepJSONDiff(
            {"items": [{"id": 1}, {"id": 1}]},
            {"items": []},
            collection_strategies=[CollectionStrategy(path="$.items", match_by=("id",))],
        )


def test_duplicate_identity_groups_can_be_sorted_and_compared_with_string_policy():
    strategy = CollectionStrategy(
        path="$.items",
        match_by=("id",),
        sort_by=("version",),
        duplicates="group",
    )
    left = {"items": [{"id": 1, "version": 10}, {"id": 1, "version": 2}]}
    right = {"items": [{"id": 1, "version": 2}, {"id": 1, "version": 10}]}

    result = DeepJSONDiff(left, right, collection_strategies=[strategy])

    assert not result
    assert strategy.duplicates is DuplicateIdentityPolicy.GROUP
    stats = result.get_stats()["$.items"]
    assert stats["duplicate_groups_left"] == 1
    assert stats["duplicate_groups_right"] == 1


def test_exclude_fields_does_not_mutate_inputs():
    left = {"events": [{"id": 1, "requestId": "left"}]}
    right = {"events": [{"id": 1, "requestId": "right"}]}
    original_left = deepcopy(left)
    original_right = deepcopy(right)

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[
            CollectionStrategy(
                path="$.events",
                match_by=("id",),
                exclude_fields=("requestId",),
            )
        ],
    )

    assert not result
    assert left == original_left
    assert right == original_right


def test_mutating_normalizer_does_not_mutate_inputs():
    def lowercase_region(item):
        item["region"] = item["region"].lower()
        item["nested"]["value"] = item["nested"]["value"].lower()
        return item

    left = {"items": [{"sku": "A", "region": "IN", "nested": {"value": "X"}}]}
    right = {"items": [{"sku": "A", "region": "in", "nested": {"value": "x"}}]}
    original_left = deepcopy(left)
    original_right = deepcopy(right)

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[
            CollectionStrategy(
                path="$.items",
                match_by=("sku", "region"),
                normalizers=(lowercase_region,),
            )
        ],
    )

    assert not result
    assert left == original_left
    assert right == original_right


@pytest.mark.parametrize(
    "kwargs",
    [
        {"path": "users"},
        {"path": "$.users", "match_by": "id"},
        {"path": "$.users", "sort_by": ("",)},
        {"path": "$.users", "exclude_fields": ("",)},
        {"path": "$.users", "normalizers": "normalize"},
        {"path": "$.users", "priority": True},
        {"path": "$.users", "missing_identity": "unknown"},
        {"path": "$.users", "duplicates": "unknown"},
        {"path": "$.users", "compare_as_set": True, "sort_by": ("id",)},
        {"path": "$.users[abc]"},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(CollectionStrategyError):
        CollectionStrategy(**kwargs)


def test_duplicate_strategy_configuration_is_rejected():
    duplicate = CollectionStrategy(path="$.users", match_by=("id",))
    with pytest.raises(CollectionStrategyError):
        DeepJSONDiff({}, {}, collection_strategies=[duplicate, duplicate])


def test_more_specific_or_higher_priority_strategy_wins():
    left = {"accounts": [{"users": [{"id": 2}, {"id": 1}]}]}
    right = {"accounts": [{"users": [{"id": 1}, {"id": 2}]}]}

    result = DeepJSONDiff(
        left,
        right,
        collection_strategies=[
            CollectionStrategy(path="$.accounts[*].users", sort_by=("id",), priority=1),
            CollectionStrategy(path="$.*", priority=0),
        ],
    )

    assert not result


def test_mapping_facade_exposes_standard_mapping_methods():
    result = DeepJSONDiff({"value": 1}, {"value": 2})

    assert list(result.keys()) == ["values_changed"]
    assert result.get("values_changed") is not None
    assert list(result.items())
