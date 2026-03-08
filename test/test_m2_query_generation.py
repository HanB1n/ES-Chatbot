"""
Milestone 2 acceptance tests for the newer async, schema-aware QueryGenerator.

What changed compared to the older test:
- The newer QueryGenerator uses async generation via `agenerate(...)`
- Schema/context is no longer retrieved by directly monkeypatching `vectorstore`
- We patch `_get_schema_context_async(...)` instead, which is the cleaner contract
- The LLM is still monkeypatched so the test remains deterministic

These tests still validate the same milestone intent:
1. Natural language -> valid Elasticsearch query dict
2. Correct use of V2Persons.V1Person.keyword for top-people aggregation
3. Invalid LLM JSON raises QueryGenerationError cleanly
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


class _FakeLLM:
    """Simple fake LLM that returns a fixed `.content` payload."""

    def __init__(self, content: str):
        self._content = content

    def invoke(self, messages):
        # Keep messages accessible for optional debugging/assertion
        self.last_messages = messages
        return SimpleNamespace(content=self._content)


@pytest.mark.asyncio
async def test_m2_query_generation_returns_valid_es_query_dict_for_top10_people_this_week(monkeypatch):
    """
    Acceptance criteria:
    - Asking "Who are the top 10 people mentioned this week?"
      returns a valid ES query JSON dict
    - Query uses V2Persons.V1Person.keyword in a terms aggregation
    - Includes a time range filter
    """
    from services.query_generator import QueryGenerator

    llm_json = json.dumps(
        {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {
                            "range": {
                                "V21Date": {
                                    "gte": "now-7d/d",
                                    "lte": "now",
                                }
                            }
                        }
                    ]
                }
            },
            "aggs": {
                "top_people": {
                    "terms": {
                        "field": "V2Persons.V1Person.keyword",
                        "size": 10,
                    }
                }
            },
        }
    )

    qg = QueryGenerator()
    monkeypatch.setattr(qg, "llm", _FakeLLM(llm_json), raising=True)

    async def _fake_get_schema_context_async(question: str, k: int = 8) -> str:
        return (
            "Field: V21Date. Type: date. Usage: Use for date filtering.\n"
            "Field: V2Persons.V1Person.keyword. Type: keyword. "
            "Usage: Use for exact matches and terms aggregations."
        )

    monkeypatch.setattr(
        qg,
        "_get_schema_context_async",
        _fake_get_schema_context_async,
        raising=True,
    )

    result = await qg.agenerate("Who are the top 10 people mentioned this week?")

    assert isinstance(result, dict)
    assert "aggs" in result
    assert "top_people" in result["aggs"]

    terms = result["aggs"]["top_people"]["terms"]
    assert terms["field"] == "V2Persons.V1Person.keyword"
    assert terms["size"] == 10

    assert "query" in result
    filters = result["query"]["bool"]["filter"]
    assert any("range" in item and "V21Date" in item["range"] for item in filters)


@pytest.mark.asyncio
async def test_m2_invalid_llm_output_raises_query_generation_error(monkeypatch):
    """
    Acceptance criteria:
    - If the LLM returns invalid JSON, QueryGenerator raises QueryGenerationError
    - The error should be clean and intentional, not a crash from later stages
    """
    from services.query_generator import QueryGenerator, QueryGenerationError

    qg = QueryGenerator()
    monkeypatch.setattr(qg, "llm", _FakeLLM("```json\n{not valid}\n```"), raising=True)

    async def _fake_get_schema_context_async(question: str, k: int = 8) -> str:
        return (
            "Field: V21Date. Type: date.\n"
            "Field: V2Persons.V1Person.keyword. Type: keyword."
        )

    monkeypatch.setattr(
        qg,
        "_get_schema_context_async",
        _fake_get_schema_context_async,
        raising=True,
    )

    with pytest.raises(QueryGenerationError) as exc_info:
        await qg.agenerate("Who are the top 10 people mentioned this week?")

    assert "Invalid JSON" in str(exc_info.value) or "valid JSON" in str(exc_info.value)


@pytest.mark.asyncio
async def test_m2_schema_context_is_included_in_prompt(monkeypatch):
    """
    Extra regression test for the newer schema-aware design.

    This verifies that retrieved schema context is actually passed into the LLM prompt.
    It is useful because the new architecture depends on schema retrieval, not hardcoded
    Appendix A mappings alone.
    """
    from services.query_generator import QueryGenerator

    llm_json = json.dumps(
        {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {
                            "range": {
                                "V21Date": {
                                    "gte": "now-7d/d",
                                    "lte": "now",
                                }
                            }
                        }
                    ]
                }
            },
            "aggs": {
                "top_people": {
                    "terms": {
                        "field": "V2Persons.V1Person.keyword",
                        "size": 10,
                    }
                }
            },
        }
    )

    fake_llm = _FakeLLM(llm_json)
    qg = QueryGenerator()
    monkeypatch.setattr(qg, "llm", fake_llm, raising=True)

    schema_context = (
        "Field: V2Persons.V1Person.keyword. Type: keyword. "
        "Usage: Use for exact matches and terms aggregations."
    )

    async def _fake_get_schema_context_async(question: str, k: int = 8) -> str:
        return schema_context

    monkeypatch.setattr(
        qg,
        "_get_schema_context_async",
        _fake_get_schema_context_async,
        raising=True,
    )

    await qg.agenerate("Who are the top 10 people mentioned this week?")

    sent_messages = fake_llm.last_messages
    assert isinstance(sent_messages, list)
    assert len(sent_messages) >= 2

    system_message = sent_messages[0]
    assert system_message["role"] == "system"
    assert schema_context in system_message["content"]


@pytest.mark.asyncio
async def test_m2_generate_can_use_empty_schema_context_without_crashing(monkeypatch):
    """
    Extra resilience test for the newer design.

    Even if schema retrieval fails or returns nothing, query generation should still
    parse valid LLM JSON instead of crashing inside the schema retrieval path.
    """
    from services.query_generator import QueryGenerator

    llm_json = json.dumps(
        {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {
                            "range": {
                                "V21Date": {
                                    "gte": "now-7d/d",
                                    "lte": "now",
                                }
                            }
                        }
                    ]
                }
            },
            "aggs": {
                "top_people": {
                    "terms": {
                        "field": "V2Persons.V1Person.keyword",
                        "size": 10,
                    }
                }
            },
        }
    )

    qg = QueryGenerator()
    monkeypatch.setattr(qg, "llm", _FakeLLM(llm_json), raising=True)

    async def _fake_get_schema_context_async(question: str, k: int = 8) -> str:
        return "(no schema context available)"

    monkeypatch.setattr(
        qg,
        "_get_schema_context_async",
        _fake_get_schema_context_async,
        raising=True,
    )

    result = await qg.agenerate("Who are the top 10 people mentioned this week?")
    assert isinstance(result, dict)
    assert result["aggs"]["top_people"]["terms"]["field"] == "V2Persons.V1Person.keyword"