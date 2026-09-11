"""Unit tests for the range-specification parser."""

import pytest

from vidhive_common.ranges import RangeParseError, parse_range


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("[0,500000]", (0, 500000)),
        ("[10,20]", (10, 20)),
        ("100", (0, 100)),
        ("[42,]", (42, None)),
        ("", (0, None)),
        (None, (0, None)),
        ("  [5, 9] ", (5, 9)),
    ],
)
def test_parse_range_valid(spec, expected):
    assert parse_range(spec) == expected


@pytest.mark.parametrize("spec", ["[10,5]", "[-1,5]", "abc", "[a,b]", "[1,2,3]"])
def test_parse_range_invalid(spec):
    with pytest.raises(RangeParseError):
        parse_range(spec)
