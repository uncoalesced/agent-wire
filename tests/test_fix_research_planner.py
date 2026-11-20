"""Regression tests for audited planner / scoring bugs (offline)."""

from datetime import UTC, datetime, timedelta

import pytest
from test_research import hit

from agent_wire.research.engine import mentions
from agent_wire.research.planner import make_plan, terms
from agent_wire.research.scoring import Item, relevance, score_items

NOW = datetime.now(UTC)


def test_terms_keep_non_ascii_words():
    assert terms("東京 vs 大阪") == ["東京", "vs", "大阪"]
    assert terms("нейросети") == ["нейросети"]
    assert terms("café résumé") == ["café", "résumé"]


def test_non_latin_topic_is_scored_and_matched_not_waved_through():
    assert relevance(hit("Новости про нейросети", "u"), "нейросети") > 0.9
    assert relevance(hit("Cooking pasta", "u"), "нейросети") == 0
    on, off = hit("Tokyo 東京 guide", "u"), hit("Cooking pasta", "v")
    assert mentions(Item(on), ["東京"]) == ["東京"] and mentions(Item(off), ["東京"]) == []


def test_topic_without_searchable_words_matches_nothing_and_says_so():
    p = make_plan("the of")
    assert any("no searchable words" in n for n in p.notes)
    assert mentions(Item(hit("the of", "u")), ["the of"]) == []
    items = [Item(hit("Anything", "u", when=NOW - timedelta(hours=1), score=5), query="the of")]
    assert score_items(items, "the of", NOW - timedelta(days=7), NOW, 7) == []
    assert not make_plan("").notes  # discover mode has no topic and no complaint


@pytest.mark.parametrize("topic", ["rust", "東京"])
def test_normal_topics_get_no_note(topic):
    assert not make_plan(topic).notes
