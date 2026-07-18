"""Path-scoped JSON comparison strategies built on top of :class:`DeepDiff`.

This module is intentionally opt-in. Existing ``DeepDiff`` callers are not
modified. ``DeepJSONDiff`` creates non-mutating canonical views of JSON-like
inputs, then delegates recursive comparison to ``DeepDiff``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .diff import DeepDiff


_MISSING = object()
JSONScalar = Optional[bool | int | float | str]
FilterFunc = Callable[[Any], bool]
Normalizer = Callable[[Any], Any]


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


@dataclass(frozen=True)
class CollectionStrategy:
    """Rules applied to a JSON array selected by ``path``.

    ``path`` uses a small, deterministic JSONPath-like dialect. Supported
    patterns include exact paths (``$.users``), single-level wildcards
    (``$.accounts[*].users``), and recursive wildcards (``$.*.items`` through
    standard ``fnmatch`` semantics).

    ``match_by`` converts an array of objects into a mapping keyed by semantic
    identity. This avoids index-based cascading diffs when items are added,
    removed, or reordered. ``sort_by`` provides deterministic ordering where
    semantic matching is not configured, and also orders duplicate groups.
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

    def __post_init__(self) -> None:
        if not self.path.startswith("$"):
            raise CollectionStrategyError("strategy path must start with '$'")
        if self.compare_as_set and self.match_by:
            raise CollectionStrategyError("compare_as_set and match_by are mutually exclusive")
        if self.filter_func is not None and not callable(self.filter_func):
            raise CollectionStrategyError("filter_func must be callable")
        if any(not callable(item) for item in self.normalizers):
            raise CollectionStrategyError("all normalizers must be callable")


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


def _normalise_pattern(pattern: str) -> str:
    # Make array wildcards compatible with fnmatch without treating brackets as
    # a character class.
    return pattern.replace("[*]", "[[]*[]]")


def _extract(value: Any, relative_path: str, default: Any = _MISSING) -> Any:
    """Extract a dotted relative path from mappings and integer-indexed lists."""

    current = value
    if not relative_path:
        return current
    for token in relative_path.split("."):
        if isinstance(current, Mapping):
            if token not in current:
                return default
            current = current[token]
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            try:
                current = current[int(token)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return current


def _stable_value(value: Any) -> Tuple[str, str]:
    """Return a total, deterministic ordering key for heterogeneous JSON values."""

    if value is _MISSING:
        return ("0-missing", "")
    if value is None:
        return ("1-null", "")
    if isinstance(value, bool):
        return ("2-bool", repr(value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ("3-number", repr(value))
    if isinstance(value, str):
        return ("4-string", value)
    return (f"5-{type(value).__name__}", repr(value))


def _identity_label(identity: Tuple[Any, ...]) -> str:
    return "|".join(f"{kind}:{text}" for kind, text in (_stable_value(v) for v in identity))


class DeepJSONDiff:
    """Compare arbitrary JSON-like values with path-scoped collection semantics.

    The resulting object delegates mapping-like behaviour and serialization to
    the underlying :class:`DeepDiff` instance through ``diff``.
    """

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
                    f"ambiguous strategies for path {strategy.path!r} at priority {strategy.priority}"
                )
            seen[key] = strategy

    def _resolve_strategy(self, path: Tuple[Any, ...]) -> Optional[CollectionStrategy]:
        rendered = _path_to_string(path)
        matches = [
            strategy
            for strategy in self.collection_strategies
            if fnmatchcase(rendered, _normalise_pattern(strategy.path))
        ]
        if not matches:
            return None
        matches.sort(key=lambda item: (item.priority, len(item.path)), reverse=True)
        if len(matches) > 1:
            first, second = matches[0], matches[1]
            if first.priority == second.priority and len(first.path) == len(second.path):
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
            return [self._canonicalize(item, path + (index,), context) for index, item in enumerate(values)]

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
            if strategy.filter_func is not None and not strategy.filter_func(original):
                if context.side == "left":
                    stat.filtered_left += 1
                else:
                    stat.filtered_right += 1
                continue

            item = original
            for normalizer in strategy.normalizers:
                item = normalizer(item)
            if isinstance(item, Mapping) and strategy.exclude_fields:
                item = dict(item)
                for field_name in strategy.exclude_fields:
                    item.pop(field_name, None)
            item = self._canonicalize(item, path + (index,), context)
            prepared.append((index, item))

        if strategy.match_by:
            return self._index_by_identity(prepared, strategy, context, stat)

        items = [item for _, item in prepared]
        if strategy.sort_by:
            items = sorted(
                items,
                key=lambda item: tuple(_stable_value(_extract(item, field)) for field in strategy.sort_by),
            )
        elif strategy.compare_as_set:
            items = sorted(items, key=_stable_value)
        return items

    def _index_by_identity(
        self,
        prepared: List[Tuple[int, Any]],
        strategy: CollectionStrategy,
        context: _Context,
        stat: StrategyStats,
    ) -> Dict[str, Any]:
        grouped: Dict[str, List[Any]] = {}
        fallback: List[Any] = []

        for original_index, item in prepared:
            identity = tuple(_extract(item, field) for field in strategy.match_by)
            if any(value is _MISSING for value in identity):
                if context.side == "left":
                    stat.missing_identity_left += 1
                else:
                    stat.missing_identity_right += 1
                if strategy.missing_identity is MissingIdentityPolicy.ERROR:
                    missing = [
                        field for field, value in zip(strategy.match_by, identity) if value is _MISSING
                    ]
                    raise IdentityExtractionError(
                        f"missing identity field(s) {missing!r} at item index {original_index}"
                    )
                if strategy.missing_identity is MissingIdentityPolicy.EXCLUDE:
                    continue
                fallback.append(item)
                continue

            label = _identity_label(identity)
            grouped.setdefault(label, []).append(item)

        result: Dict[str, Any] = {}
        for label, group in grouped.items():
            if len(group) > 1:
                if context.side == "left":
                    stat.duplicate_groups_left += 1
                else:
                    stat.duplicate_groups_right += 1
                if strategy.duplicates is DuplicateIdentityPolicy.ERROR:
                    raise DuplicateIdentityError(f"duplicate identity {label!r}")
                if strategy.sort_by:
                    group = sorted(
                        group,
                        key=lambda item: tuple(
                            _stable_value(_extract(item, field)) for field in strategy.sort_by
                        ),
                    )
                result[label] = group
            else:
                result[label] = group[0]

        if fallback:
            # Retain fallback items under an explicit namespace. Their original
            # relative order remains meaningful and deterministic.
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

    def __iter__(self):
        return iter(self.diff)

    def __len__(self) -> int:
        return len(self.diff)

    def __repr__(self) -> str:
        return repr(self.diff)

    def to_dict(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.diff.to_dict(*args, **kwargs)

    def to_json(self, *args: Any, **kwargs: Any) -> str:
        return self.diff.to_json(*args, **kwargs)
