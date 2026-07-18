"""Path-scoped JSON comparison strategies built on top of :class:`DeepDiff`.

This module is intentionally opt-in. Existing ``DeepDiff`` callers are not
modified. ``DeepJSONDiff`` creates non-mutating canonical views of JSON-like
inputs, then delegates recursive comparison to ``DeepDiff``.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping as MappingABC
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
import json
import math
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .diff import DeepDiff


_MISSING = object()
_ARRAY_WILDCARD = object()
_KEY_WILDCARD = object()
JSONScalar = Optional[bool | int | float | str]
FilterFunc = Callable[[Any], bool]
Normalizer = Callable[[Any], Any]
PathToken = str | int | object


class MissingIdentityPolicy(str, Enum):
    """Behaviour when a list item does not contain all configured identity fields."""

    ERROR = "error"
    FALLBACK = "fallback"
    EXCLUDE = "exclude"


class DuplicateIdentityPolicy(str, Enum):
    """Behaviour when multiple items produce the same identity."""

    ERROR = "error"
    GROUP = "group"


class CollectionStrategyError(ValueError):
    """Base error for invalid collection strategy configuration or execution."""


class IdentityExtractionError(CollectionStrategyError):
    """Raised when a required identity field cannot be extracted."""


class DuplicateIdentityError(CollectionStrategyError):
    """Raised when an identity expected to be unique is duplicated."""


def _as_non_empty_string_tuple(value: Any, field_name: str) -> Tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise CollectionStrategyError(f"{field_name} must be a sequence of non-empty strings")
    result = tuple(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise CollectionStrategyError(f"{field_name} must contain only non-empty strings")
    return result


def _parse_pattern(pattern: str) -> Tuple[PathToken, ...]:
    """Parse the supported JSONPath-like collection selector into path tokens."""
    if not isinstance(pattern, str) or not pattern.startswith("$"):
        raise CollectionStrategyError("strategy path must be a string starting with '$'")
    if pattern == "$":
        return ()

    tokens: List[PathToken] = []
    index = 1
    while index < len(pattern):
        if pattern[index] == ".":
            index += 1
            start = index
            while index < len(pattern) and pattern[index] not in ".[":
                index += 1
            token = pattern[start:index]
            if not token:
                raise CollectionStrategyError(f"invalid strategy path {pattern!r}")
            tokens.append(_KEY_WILDCARD if token == "*" else token)
            continue

        if pattern[index] == "[":
            close = pattern.find("]", index)
            if close == -1:
                raise CollectionStrategyError(f"invalid strategy path {pattern!r}")
            token = pattern[index + 1 : close]
            if token == "*":
                tokens.append(_ARRAY_WILDCARD)
            elif token.isdigit():
                tokens.append(int(token))
            else:
                raise CollectionStrategyError(
                    "bracket selectors must contain an integer index or '*'"
                )
            index = close + 1
            continue

        raise CollectionStrategyError(f"invalid strategy path {pattern!r}")

    return tuple(tokens)


@dataclass(frozen=True)
class CollectionStrategy:
    """Rules applied to a JSON array selected by ``path``.

    ``path`` supports exact object keys and array indexes, one-key wildcards
    (``$.*.users``), and one-index array wildcards
    (``$.accounts[*].users``).

    ``match_by`` converts an array of objects into a mapping keyed by semantic
    identity. This avoids index-based cascading diffs when items are added,
    removed, or reordered. ``sort_by`` provides deterministic ordering where
    semantic matching is not configured, and also orders duplicate groups.
    ``compare_as_set`` performs order-insensitive multiset comparison, so
    duplicate values remain significant.
    """

    path: str
    match_by: Tuple[str, ...] = ()
    sort_by: Tuple[str, ...] = ()
    filter_func: Optional[FilterFunc] = None
    normalizers: Tuple[Normalizer, ...] = ()
    exclude_fields: Tuple[str, ...] = ()
    compare_as_set: bool = False
    missing_identity: MissingIdentityPolicy = MissingIdentityPolicy.FALLBACK
    duplicates: DuplicateIdentityPolicy = DuplicateIdentityPolicy.ERROR
    priority: int = 0
    name: Optional[str] = None
    _pattern_tokens: Tuple[PathToken, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        pattern_tokens = _parse_pattern(self.path)
        match_by = _as_non_empty_string_tuple(self.match_by, "match_by")
        sort_by = _as_non_empty_string_tuple(self.sort_by, "sort_by")
        exclude_fields = _as_non_empty_string_tuple(self.exclude_fields, "exclude_fields")

        if isinstance(self.normalizers, (str, bytes)) or not isinstance(
            self.normalizers, Sequence
        ):
            raise CollectionStrategyError("normalizers must be a sequence of callables")
        normalizers = tuple(self.normalizers)
        if any(not callable(item) for item in normalizers):
            raise CollectionStrategyError("all normalizers must be callable")

        if self.filter_func is not None and not callable(self.filter_func):
            raise CollectionStrategyError("filter_func must be callable")
        if not isinstance(self.compare_as_set, bool):
            raise CollectionStrategyError("compare_as_set must be a boolean")
        if self.compare_as_set and match_by:
            raise CollectionStrategyError("compare_as_set and match_by are mutually exclusive")
        if self.compare_as_set and sort_by:
            raise CollectionStrategyError("compare_as_set and sort_by are mutually exclusive")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise CollectionStrategyError("priority must be an integer")
        if self.name is not None and not isinstance(self.name, str):
            raise CollectionStrategyError("name must be a string or None")

        try:
            missing_identity = MissingIdentityPolicy(self.missing_identity)
        except (TypeError, ValueError) as exc:
            raise CollectionStrategyError(
                f"invalid missing_identity policy {self.missing_identity!r}"
            ) from exc
        try:
            duplicates = DuplicateIdentityPolicy(self.duplicates)
        except (TypeError, ValueError) as exc:
            raise CollectionStrategyError(
                f"invalid duplicates policy {self.duplicates!r}"
            ) from exc

        object.__setattr__(self, "match_by", match_by)
        object.__setattr__(self, "sort_by", sort_by)
        object.__setattr__(self, "exclude_fields", exclude_fields)
        object.__setattr__(self, "normalizers", normalizers)
        object.__setattr__(self, "missing_identity", missing_identity)
        object.__setattr__(self, "duplicates", duplicates)
        object.__setattr__(self, "_pattern_tokens", pattern_tokens)


@dataclass
class StrategyStats:
    """Execution diagnostics for one concrete collection path."""

    strategy: str
    left_items: int = 0
    right_items: int = 0
    filtered_left: int = 0
    filtered_right: int = 0
    missing_identity_left: int = 0
    missing_identity_right: int = 0
    duplicate_groups_left: int = 0
    duplicate_groups_right: int = 0


@dataclass
class _Context:
    side: str
    stats: Dict[str, StrategyStats] = field(default_factory=dict)


def _path_to_string(path: Tuple[Any, ...]) -> str:
    result = "$"
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        elif isinstance(part, str) and part.isidentifier():
            result += f".{part}"
        else:
            escaped = str(part).replace("'", "\\'")
            result += f"['{escaped}']"
    return result


def _pattern_matches(pattern: Tuple[PathToken, ...], path: Tuple[Any, ...]) -> bool:
    if len(pattern) != len(path):
        return False
    for expected, actual in zip(pattern, path):
        if expected is _ARRAY_WILDCARD:
            if not isinstance(actual, int):
                return False
        elif expected is _KEY_WILDCARD:
            if not isinstance(actual, str):
                return False
        elif expected != actual:
            return False
    return True


def _extract(value: Any, relative_path: str, default: Any = _MISSING) -> Any:
    """Extract a dotted relative path from mappings and integer-indexed lists."""
    current = value
    for token in relative_path.split("."):
        if isinstance(current, Mapping):
            if token not in current:
                return default
            current = current[token]
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return current


def _stable_value(value: Any) -> Tuple[int, Any]:
    """Return a total, deterministic ordering key for supported JSON-like values."""
    if value is _MISSING:
        return (0, "")
    if value is None:
        return (1, "")
    if isinstance(value, bool):
        return (2, value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return (3, repr(value))
        return (3, value)
    if isinstance(value, str):
        return (4, value)
    try:
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=repr)
    except (TypeError, ValueError):
        rendered = repr(value)
    return (5, f"{type(value).__name__}:{rendered}")


def _identity_component(value: Any, field_name: str, location: str) -> Tuple[str, Any]:
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise IdentityExtractionError(
                f"identity field {field_name!r} at {location} must be finite"
            )
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    raise IdentityExtractionError(
        f"identity field {field_name!r} at {location} must resolve to a JSON scalar; "
        f"got {type(value).__name__}"
    )


def _identity_label(
    identity: Tuple[Any, ...],
    fields: Tuple[str, ...],
    location: str,
) -> str:
    encoded = [
        _identity_component(value, field_name, location)
        for field_name, value in zip(fields, identity)
    ]
    return json.dumps(encoded, ensure_ascii=False, separators=(",", ":"))


class DeepJSONDiff(MappingABC[str, Any]):
    """Compare arbitrary JSON-like values with path-scoped collection semantics."""

    def __init__(
        self,
        t1: Any,
        t2: Any,
        *,
        collection_strategies: Iterable[CollectionStrategy] = (),
        **deepdiff_kwargs: Any,
    ) -> None:
        self.collection_strategies = tuple(collection_strategies)
        self._validate_strategies()
        self.stats: Dict[str, StrategyStats] = {}

        left_context = _Context(side="left", stats=self.stats)
        right_context = _Context(side="right", stats=self.stats)
        self.canonical_t1 = self._canonicalize(t1, (), left_context)
        self.canonical_t2 = self._canonicalize(t2, (), right_context)
        self.diff = DeepDiff(self.canonical_t1, self.canonical_t2, **deepdiff_kwargs)

    def _validate_strategies(self) -> None:
        seen: Dict[Tuple[str, int], CollectionStrategy] = {}
        for strategy in self.collection_strategies:
            if not isinstance(strategy, CollectionStrategy):
                raise CollectionStrategyError(
                    "collection_strategies must contain CollectionStrategy instances"
                )
            key = (strategy.path, strategy.priority)
            if key in seen:
                raise CollectionStrategyError(
                    f"ambiguous strategies for path {strategy.path!r} "
                    f"at priority {strategy.priority}"
                )
            seen[key] = strategy

    def _resolve_strategy(self, path: Tuple[Any, ...]) -> Optional[CollectionStrategy]:
        rendered = _path_to_string(path)
        matches = [
            strategy
            for strategy in self.collection_strategies
            if _pattern_matches(strategy._pattern_tokens, path)
        ]
        if not matches:
            return None
        matches.sort(
            key=lambda item: (
                item.priority,
                sum(
                    token is not _ARRAY_WILDCARD and token is not _KEY_WILDCARD
                    for token in item._pattern_tokens
                ),
            ),
            reverse=True,
        )
        if len(matches) > 1:
            first, second = matches[0], matches[1]
            first_specificity = sum(
                token is not _ARRAY_WILDCARD and token is not _KEY_WILDCARD
                for token in first._pattern_tokens
            )
            second_specificity = sum(
                token is not _ARRAY_WILDCARD and token is not _KEY_WILDCARD
                for token in second._pattern_tokens
            )
            if first.priority == second.priority and first_specificity == second_specificity:
                raise CollectionStrategyError(
                    f"ambiguous collection strategies matched concrete path {rendered!r}"
                )
        return matches[0]

    def _canonicalize(self, value: Any, path: Tuple[Any, ...], context: _Context) -> Any:
        strategy = self._resolve_strategy(path) if isinstance(value, list) else None
        if isinstance(value, Mapping):
            return {
                key: self._canonicalize(item, path + (key,), context)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return self._canonicalize_list(value, path, strategy, context)
        return value

    def _canonicalize_list(
        self,
        values: List[Any],
        path: Tuple[Any, ...],
        strategy: Optional[CollectionStrategy],
        context: _Context,
    ) -> Any:
        if strategy is None:
            return [
                self._canonicalize(item, path + (index,), context)
                for index, item in enumerate(values)
            ]

        rendered = _path_to_string(path)
        stat = context.stats.setdefault(
            rendered,
            StrategyStats(strategy=strategy.name or strategy.path),
        )
        if context.side == "left":
            stat.left_items = len(values)
        else:
            stat.right_items = len(values)

        prepared: List[Tuple[int, Any]] = []
        for index, original in enumerate(values):
            item = deepcopy(original)
            if strategy.filter_func is not None and not strategy.filter_func(item):
                if context.side == "left":
                    stat.filtered_left += 1
                else:
                    stat.filtered_right += 1
                continue

            for normalizer in strategy.normalizers:
                item = normalizer(item)
            if isinstance(item, Mapping) and strategy.exclude_fields:
                item = dict(item)
                for field_name in strategy.exclude_fields:
                    item.pop(field_name, None)
            item = self._canonicalize(item, path + (index,), context)
            prepared.append((index, item))

        if strategy.match_by:
            return self._index_by_identity(prepared, path, strategy, context, stat)

        items = [item for _, item in prepared]
        if strategy.sort_by:
            items = sorted(
                items,
                key=lambda item: tuple(
                    _stable_value(_extract(item, field)) for field in strategy.sort_by
                ),
            )
        elif strategy.compare_as_set:
            if any(isinstance(item, (Mapping, list, tuple, set)) for item in items):
                raise CollectionStrategyError(
                    f"compare_as_set at {rendered} supports scalar items only"
                )
            items = sorted(items, key=_stable_value)
        return items

    def _index_by_identity(
        self,
        prepared: List[Tuple[int, Any]],
        path: Tuple[Any, ...],
        strategy: CollectionStrategy,
        context: _Context,
        stat: StrategyStats,
    ) -> Dict[str, Any]:
        rendered = _path_to_string(path)
        grouped: Dict[str, List[Any]] = {}
        fallback: List[Any] = []

        for original_index, item in prepared:
            item_location = f"{rendered}[{original_index}]"
            identity = tuple(_extract(item, field) for field in strategy.match_by)
            if any(value is _MISSING for value in identity):
                if context.side == "left":
                    stat.missing_identity_left += 1
                else:
                    stat.missing_identity_right += 1
                if strategy.missing_identity is MissingIdentityPolicy.ERROR:
                    missing = [
                        field
                        for field, value in zip(strategy.match_by, identity)
                        if value is _MISSING
                    ]
                    raise IdentityExtractionError(
                        f"missing identity field(s) {missing!r} on {context.side} input "
                        f"at {item_location} while applying strategy {strategy.path!r}"
                    )
                if strategy.missing_identity is MissingIdentityPolicy.EXCLUDE:
                    continue
                fallback.append(item)
                continue

            label = _identity_label(identity, strategy.match_by, item_location)
            grouped.setdefault(label, []).append(item)

        result: Dict[str, Any] = {}
        for label, group in grouped.items():
            if len(group) > 1:
                if context.side == "left":
                    stat.duplicate_groups_left += 1
                else:
                    stat.duplicate_groups_right += 1
                if strategy.duplicates is DuplicateIdentityPolicy.ERROR:
                    raise DuplicateIdentityError(
                        f"duplicate identity {label!r} on {context.side} input at "
                        f"{rendered} while applying strategy {strategy.path!r}"
                    )
                if strategy.sort_by:
                    group = sorted(
                        group,
                        key=lambda item: tuple(
                            _stable_value(_extract(item, field))
                            for field in strategy.sort_by
                        ),
                    )
                result[label] = group
            else:
                result[label] = group[0]

        if fallback:
            result["__deepdiff_fallback__"] = fallback
        return result

    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        return {
            path: {
                "strategy": stat.strategy,
                "left_items": stat.left_items,
                "right_items": stat.right_items,
                "filtered_left": stat.filtered_left,
                "filtered_right": stat.filtered_right,
                "missing_identity_left": stat.missing_identity_left,
                "missing_identity_right": stat.missing_identity_right,
                "duplicate_groups_left": stat.duplicate_groups_left,
                "duplicate_groups_right": stat.duplicate_groups_right,
            }
            for path, stat in self.stats.items()
        }

    def __bool__(self) -> bool:
        return bool(self.diff)

    def __getitem__(self, key: str) -> Any:
        return self.diff[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.diff)

    def __len__(self) -> int:
        return len(self.diff)

    def __repr__(self) -> str:
        return repr(self.diff)

    def to_dict(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.diff.to_dict(*args, **kwargs)

    def to_json(self, *args: Any, **kwargs: Any) -> str:
        if kwargs.get("sort_keys") and "force_use_builtin_json" not in kwargs:
            kwargs["force_use_builtin_json"] = True
        return self.diff.to_json(*args, **kwargs)
