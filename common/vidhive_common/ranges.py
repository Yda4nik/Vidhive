"""Parsing of the range specification formats from the task statement.

Supported inputs (see section 3 of the spec):

    "[A,B]"  -> (A, B)      process A..B inclusive
    "A"      -> (0, A)      process 0..A inclusive
    "[A,]"   -> (A, None)   process A.. open-ended
    ""/None  -> (0, None)   process 0.. open-ended

A and B are non-negative integers; for a finite range A <= B.
The upper bound is inclusive; ``None`` means open-ended.
"""

from __future__ import annotations


class RangeParseError(ValueError):
    """Raised when a range specification is malformed."""


def _to_int(text: str, field: str) -> int:
    text = text.strip()
    try:
        value = int(text)
    except ValueError as exc:
        raise RangeParseError(f"{field} must be an integer, got {text!r}") from exc
    if value < 0:
        raise RangeParseError(f"{field} must be non-negative, got {value}")
    return value


def parse_range(spec: str | None) -> tuple[int, int | None]:
    """Parse a range specification into ``(start, end)`` where end may be None."""
    if spec is None:
        return (0, None)
    spec = spec.strip()
    if spec == "":
        return (0, None)

    if spec.startswith("[") and spec.endswith("]"):
        inner = spec[1:-1]
        parts = inner.split(",")
        if len(parts) != 2:
            raise RangeParseError("bracketed range must be '[A,B]' or '[A,]'")
        left, right = parts[0].strip(), parts[1].strip()
        start = _to_int(left, "range start") if left else 0
        end = _to_int(right, "range end") if right else None
    else:
        # A bare number means 0..A inclusive.
        end = _to_int(spec, "range end")
        start = 0

    if end is not None and end < start:
        raise RangeParseError(f"range end ({end}) must be >= range start ({start})")
    return (start, end)


def format_range(start: int, end: int | None) -> str:
    """Inverse of :func:`parse_range`, for display."""
    return f"[{start},{'' if end is None else end}]"
