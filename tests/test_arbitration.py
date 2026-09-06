"""Fixed expected choices for each priority; no score/rank-derived assertions."""

from dataclasses import replace
from itertools import permutations

import pytest
from pm_nautilus.config import Preferences
from pm_nautilus.rules import Fill, Preview, arbitrate
from test_rules import token, D


@pytest.mark.parametrize(
    "priority", ["terminal", "coverage", "budget", "spread", "net_return", "lifecycle", "stable_id"]
)
def test_lexicographic_priority_is_permutation_invariant(priority):
    fill = Fill(100000, 10_000_000, 10_000_000, 1_000_000, 0, 150000)
    other = Preview(token(market_id="a"), (fill,), 90000, 100000, 2_000_000, 5_000_000, 1_400_000)
    winner = replace(other, token=token(market_id="z"))
    if priority == "terminal":
        winner = replace(winner, fills=(replace(fill, target=140000),), exit_coverage=0)
    elif priority == "coverage":
        winner = replace(winner, exit_coverage=6_000_000, budget=4_000_000)
    elif priority == "budget":
        winner = replace(winner, budget=1_000_000, ask=200000)
    elif priority == "spread":
        winner = replace(winner, ask=95000, target_proceeds=1_100_000)
    elif priority == "net_return":
        winner = replace(winner, target_proceeds=1_500_000)
    elif priority == "lifecycle":
        winner = replace(
            winner, token=token(market_id="z", opened_ns=D // 2, ends_ns=10 * D + D // 2)
        )
    else:
        winner = replace(winner, token=token(market_id="0"))
    for order in permutations([winner, other]):
        assert arbitrate(list(order), Preferences(), D) is winner
    if priority == "lifecycle":
        assert arbitrate([winner, other], Preferences(candidateSortDirection="DESC"), D) is other


def test_no_fill_has_no_winner_and_cross_event_rejected():
    assert arbitrate([], Preferences(), D) is None
    fill = Fill(100000, 10_000_000, 10_000_000, 1_000_000, 0, 150000)
    p = Preview(token(), (fill,), 90000, 100000, 1_000_000, 0, 1_500_000)
    with pytest.raises(ValueError, match="同一Event"):
        arbitrate([p, replace(p, token=token(event_id="another"))], Preferences(), D)
