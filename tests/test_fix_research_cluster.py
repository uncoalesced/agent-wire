"""Regression tests for audited clustering / term bugs (offline)."""

import pytest
from test_research import hit

from agent_wire.research.cluster import cluster
from agent_wire.research.scoring import Item


def items(*titles):
    return [
        Item(hit(t, f"https://e.example/{n}", source="news"), score=0.5)
        for n, t in enumerate(titles)
    ]


@pytest.mark.parametrize(
    "a,b",
    [
        ("Python 3.12 released", "Python 3.13 released"),
        ("Bitcoin price hits $90k today", "Bitcoin price hits $95k today"),
    ],
)
def test_headlines_differing_only_by_number_stay_separate(a, b):
    assert len(cluster(items(a, b))) == 2


def test_true_duplicates_with_numbers_still_merge():
    a, b = "Python 3.13 released with a faster JIT", "Python 3.13 released with a faster JIT today"
    assert len(cluster(items(a, b))) == 1


def test_non_latin_titles_cluster():
    a, b = (
        "Токио и Осака выбрали новый нейросети проект",
        "Токио и Осака выбрали новый нейросети проект сегодня",
    )
    assert len(cluster(items(a, b))) == 1
