"""Path-scoped JSON comparison strategies built on top of :class:`DeepDiff`.

The implementation is intentionally opt-in so existing ``DeepDiff`` callers keep
exactly the same behaviour.  ``DeepJSONDiff`` creates non-mutating canonical
views of JSON-compatible inputs and delegates the actual recursive comparison to
``DeepDiff``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatchcase
from typing import Any, Callable, Iterable