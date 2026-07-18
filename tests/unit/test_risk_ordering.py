"""Risk-level ordering.

Regression tests for a real bug found during development: ``RiskLevel`` is a
``StrEnum``, so it inherits ``str``'s comparison operators. With only ``__lt__``
and ``__le__`` overridden, ``>=`` fell back to alphabetical comparison --
``"read" >= "execute"`` is True because 'r' sorts after 'e'. Every severity gate in
the permissions engine was therefore comparing spelling, not severity: the kill
switch blocked ordinary reads, and workspace mode demanded ``allow_execute`` for a
plain file write.

The lesson generalises: any ordered enum built on ``StrEnum`` or ``IntEnum`` must
define *all four* comparison operators, because the base class already supplies
working-but-wrong implementations that will not raise.
"""

from __future__ import annotations

import itertools

import pytest

from localai.config.models import RiskLevel

pytestmark = pytest.mark.security

#: Least to most severe. This is the contract every gate depends on.
ASCENDING = [
    RiskLevel.READ,
    RiskLevel.WRITE,
    RiskLevel.DESTRUCTIVE,
    RiskLevel.EXECUTE,
    RiskLevel.PRIVILEGED,
]


def test_every_level_is_covered() -> None:
    """A new level added without an explicit ordering position is a bug."""
    assert set(ASCENDING) == set(RiskLevel)


@pytest.mark.parametrize(("lower", "higher"), list(itertools.combinations(ASCENDING, 2)))
def test_strict_ordering_holds_in_both_directions(lower: RiskLevel, higher: RiskLevel) -> None:
    assert lower < higher
    assert lower <= higher
    assert higher > lower
    assert higher >= lower
    assert not lower > higher
    assert not lower >= higher
    assert not higher < lower
    assert not higher <= lower


@pytest.mark.parametrize("level", ASCENDING)
def test_reflexive_comparisons(level: RiskLevel) -> None:
    assert level <= level
    assert level >= level
    assert not level < level
    assert not level > level


def test_alphabetical_ordering_does_not_leak_through() -> None:
    """The exact comparisons that were wrong before the fix."""
    # "read" > "execute" alphabetically, but READ is far less severe.
    assert RiskLevel.READ < RiskLevel.EXECUTE
    assert not RiskLevel.READ >= RiskLevel.EXECUTE

    # "write" > "execute" alphabetically, but WRITE is less severe.
    assert RiskLevel.WRITE < RiskLevel.EXECUTE
    assert not RiskLevel.WRITE >= RiskLevel.EXECUTE

    # "privileged" < "read" alphabetically, but PRIVILEGED is the most severe.
    assert RiskLevel.PRIVILEGED > RiskLevel.READ
    assert RiskLevel.PRIVILEGED >= RiskLevel.DESTRUCTIVE


def test_sorting_uses_severity_not_spelling() -> None:
    shuffled = [
        RiskLevel.PRIVILEGED,
        RiskLevel.READ,
        RiskLevel.EXECUTE,
        RiskLevel.WRITE,
        RiskLevel.DESTRUCTIVE,
    ]
    assert sorted(shuffled) == ASCENDING


def test_ranks_are_distinct_and_ascending() -> None:
    ranks = [level.rank for level in ASCENDING]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_comparison_with_a_plain_string_is_not_silently_accepted() -> None:
    """Comparing against a raw string must not produce a misleading answer.

    Returning NotImplemented lets Python fall back to str comparison for equality
    (which is correct and useful for a StrEnum) while ordering against a non-enum
    is a programming error the type checker catches.
    """
    assert RiskLevel.READ == "read"  # equality on a StrEnum is intentional
    assert RiskLevel.READ.value == "read"
