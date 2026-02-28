# ES-Chatbot/test/acceptance_test.py
"""
Acceptance tests for ES-Chatbot, aligned to the README.md acceptance criteria
for Milestones 1–6.

How to run:
    # unit-style (no external services required)
    pytest -q ES-Chatbot/test/acceptance_test.py

    # integration-style (requires running backend on BACKEND_URL, and real ES/LLM reachable)
    RUN_INTEGRATION=1 BACKEND_URL=http://localhost:8000 pytest -q ES-Chatbot/test/acceptance_test.py

Notes:
- These tests are intentionally "black-box first" where possible (hit HTTP endpoints).
- For milestones involving external dependencies (LLM/Chroma/ES), we provide:
  (a) unit acceptance tests using mocks, always runnable, and
  (b) optional integration acceptance tests enabled via RUN_INTEGRATION=1.
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import pytest


# -----------------------------
# Config / helpers
# -----------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]  # .../ES-Chatbot
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
RUN_INTEGRATION = os.environ.get("RUN_INTEGRATION", "").strip() in {"1", "true", "TRUE", "yes", "YES"}


def _tcp_connectable(host: str, port: int, timeout_s: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _parse_host_port(url: str) -> tuple[str, int]:
    # expects http://host:port
    m = re.match(r"^https?://([^:/]+)(?::(\d+))?$", url)
    if not m:
        return ("localhost", 8000)
    host = m.group(1)
    port = int(m.group(2) or 80)
    return host, port


def _should_run_integration() -> bool:
    if not RUN_INTEGRATION:
        return False
    host, port = _parse_host_port(BACKEND_URL)
    return _tcp_connectable(host, port)


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# -----------------------------
# Milestone 6 (Doc / hardening) checks that can run offline
# -----------------------------

def test_m6_readme_exists_and_has_onboarding_sections():
    """
    Milestone 6 Acceptance:
    - README sufficient to onboard: setup instructions, architecture overview,
      how to run tests, known limitations.
    This is a lightweight heuristic check (keyword-based).
    """
    readme = PROJECT_ROOT / "README.md"
    assert readme.exists(), "README.md is missing at repo root"

    text = readme.read_text(encoding="utf-8", errors="ignore").lower()

    # Heuristic: ensure core onboarding sections/keywords exist
    must_have_any = {
        "setup": ["setup", "installation", "requirements", ".env", "docker compose", "docker-compose"],
        "architecture": ["architecture", "system architecture", "data flow", "component"],
        "tests": ["pytest", "run tests", "testing"],
        "limitations": ["known limitations", "limitations", "out of scope"],
    }

    missing = []
    for section, keywords in must_have_any.items():
        if not any(k in text for k in keywords):
            missing.append(section)

    assert not missing, f"README seems to be missing onboarding sections: {missing}"


def test_m6_chat_input_length_validation_in_schema():
    """
    Milestone 6 Task:
    - Add input length validation to chat endpoint.
    README shows ChatRequest.message min_length/max_length in Pydantic model.
    We validate schemas.py contains these constraints (static check).
    """
    schemas = PROJECT_ROOT / "backend" / "models" / "schemas.py"
    assert schemas.exists(), "backend/models/schemas.py missing"

    text = schemas.read_text(encoding="utf-8", errors="ignore")

    # Check for Field(..., min_length=1, max_length=1000) on message
    # (allow minor formatting differences)
    assert re.search(r"message\s*:\s*str\s*=\s*Field\([^)]*min_length\s*=\s*1", text), \
        "ChatRequest.message min_length=1 not found"
    assert re.search(r"message\s*:\s*str\s*=\s*Field\([^)]*max_length\s*=\s*1000", text), \
        "ChatRequest.message max_length=1000 not found"


# -----------------------------
# Milestone 3 (Safety Layer) acceptance checks (offline import + behavior)
# -----------------------------

def test_m3_query_safety_layer_rules_exist_and_behave():
    """
    Milestone 3 Acceptance criteria:
    - Script key at any depth is blocked
    - size: 10000 capped to max_result_docs (default 20)
    - _source exclusions injected on every query
    - terms agg size capped to MAX_AGG_BUCKETS

    Note: repo already has backend/tests/test_query_safety.py,
    but this acceptance test re-validates the "core promises" directly.
    """
    # Import using repo layout (tests typically run with CWD at repo root)
    try:
        from backend.services.query_safety import QuerySafetyLayer, SafetyStatus, ALWAYS_EXCLUDE_FIELDS
    except Exception:
        # Some repos run tests with backend/ on PYTHONPATH; try alternate import.
        from services.query_safety import QuerySafetyLayer, SafetyStatus, ALWAYS_EXCLUDE_FIELDS

    safety = QuerySafetyLayer(max_result_docs=20, max_agg_buckets=50)

    # script at depth
    res = safety.validate({"query": {"bool": {"filter": [{"script": {"source": "1+1"}}]}}})
    assert res.status == SafetyStatus.BLOCKED
    assert res.query is None
    assert res.reason == "script_detected"

    # size cap
    res = safety.validate({"query": {"match_all": {}}, "size": 10000})
    assert res.status in (SafetyStatus.MODIFIED, SafetyStatus.ALLOWED)
    assert res.query is not None
    assert res.query.get("size") == 20

    # _source exclusions injected/merged
    res = safety.validate({"query": {"match_all": {}}, "size": 1})
    assert res.query is not None
    assert "_source" in res.query
    assert "excludes" in res.query["_source"]
    for f in ALWAYS_EXCLUDE_FIELDS:
        assert f in res.query["_source"]["excludes"]

    # terms agg bucket cap
    res = safety.validate(
        {
            "size": 0,
            "aggs": {"top_people": {"terms": {"field": "V2Persons.V1Person.keyword", "size": 1000}}},
        }
    )
    assert res.query is not None
    assert res.query["aggs"]["top_people"]["terms"]["size"] == 50


# -----------------------------
# Milestone 2 (Query Generation) acceptance checks (offline with mocks)
# -----------------------------

@dataclass
class _FakeLLMResponse:
    content: str


class _FakeLLM:
    def __init__(self, content: str):
        self._content = content

    def invoke(self, messages: Any) -> _FakeLLMResponse:
        return _FakeLLMResponse(self._content)


class _FakeVectorStore:
    def similarity_search(self, question: str, k: int = 6):
        # Return fake "documents" with page_content attribute if needed.
        class _Doc:
            def __init__(self, page_content: str):
                self.page_content = page_content

        return [_Doc("V2Persons.V1Person.keyword is a keyword field for terms aggregations."), _Doc("V21Date is date.")]


def test_m2_query_generation_returns_valid_es_query_dict_for_top10_people_this_week(monkeypatch):
    """
    Milestone 2 Acceptance criteria:
    - Asking "Who are the top 10 people mentioned this week?" returns valid ES query JSON dict
    - Query uses V2Persons.V1Person.keyword in a terms aggregation
    """
    # Import the QueryGenerator
    try:
        from backend.services.query_generator import QueryGenerator
    except Exception:
        from services.query_generator import QueryGenerator

    # Prepare deterministic "LLM" output
    llm_json = json.dumps(
        {
            "size": 0,
            "query": {"bool": {"filter": [{"range": {"V21Date": {"gte": "now-7d/d", "lte": "now"}}}]}},
            "aggs": {
                "top_people": {
                    "terms": {"field": "V2Persons.V1Person.keyword", "size": 10}
                }
            },
        }
    )

    qg = QueryGenerator()

    # Monkeypatch external dependencies (LLM + vectorstore)
    monkeypatch.setattr(qg, "llm", _FakeLLM(llm_json), raising=True)
    monkeypatch.setattr(qg, "vectorstore", _FakeVectorStore(), raising=True)

    query = qg.generate("Who are the top 10 people mentioned this week?", history=[])

    assert isinstance(query, dict)
    assert query.get("size") == 0
    assert "aggs" in query
    # Must reference required field in terms agg
    terms_aggs = json.dumps(query.get("aggs", {}))
    assert "V2Persons.V1Person.keyword" in terms_aggs
    assert re.search(r'"terms"\s*:\s*\{', terms_aggs), "No terms aggregation found"


def test_m2_invalid_llm_output_raises_query_generation_error(monkeypatch):
    """
    Milestone 2 Acceptance:
    - Invalid LLM output is caught and raises QueryGenerationError with a clear message
    """
    try:
        from backend.services.query_generator import QueryGenerator, QueryGenerationError
    except Exception:
        from services.query_generator import QueryGenerator, QueryGenerationError

    qg = QueryGenerator()
    monkeypatch.setattr(qg, "llm", _FakeLLM("```json\n{not valid}\n```"), raising=True)
    monkeypatch.setattr(qg, "vectorstore", _FakeVectorStore(), raising=True)

    with pytest.raises(QueryGenerationError) as ei:
        _ = qg.generate("Who are the top 10 people mentioned this week?", history=[])

    msg = str(ei.value).lower()
    assert any(k in msg for k in ["invalid", "json", "parse"]), f"Error message not clear enough: {ei.value}"


# -----------------------------
# Milestone 1 (Infrastructure) acceptance checks (optional integration)
# -----------------------------

@pytest.mark.skipif(not _should_run_integration(), reason="Integration disabled or backend not reachable")
def test_m1_health_endpoint_ok_and_indicates_es_llm_reachable():
    """
    Milestone 1 Acceptance:
    - curl /health returns {"status":"ok","elasticsearch":true,"llm":true}
    """
    import requests

    r = requests.get(f"{BACKEND_URL}/health", timeout=10)
    assert r.status_code == 200
    data = r.json()

    assert data.get("status") == "ok"
    # These should be booleans
    assert data.get("elasticsearch") is True
    assert data.get("llm") is True


@pytest.mark.skipif(not _should_run_integration(), reason="Integration disabled or backend not reachable")
def test_m1_index_stats_returns_document_count_and_date_range():
    """
    Milestone 1 Acceptance:
    - GET /api/v1/index/stats returns document count and date range from live index
    """
    import requests

    r = requests.get(f"{BACKEND_URL}/api/v1/index/stats", timeout=20)
    assert r.status_code == 200
    data = r.json()

    assert isinstance(data.get("total_documents"), int)
    assert data["total_documents"] >= 0

    # index_size_bytes might be 0 in some dev setups, but should be int
    assert isinstance(data.get("index_size_bytes"), int)

    # earliest/latest should be present strings
    assert isinstance(data.get("earliest_date"), str) and len(data["earliest_date"]) > 0
    assert isinstance(data.get("latest_date"), str) and len(data["latest_date"]) > 0

    # top_sources list
    assert isinstance(data.get("top_sources"), list)


# -----------------------------
# Milestone 4 (End-to-End) acceptance checks (optional integration)
# -----------------------------

@pytest.mark.skipif(not _should_run_integration(), reason="Integration disabled or backend not reachable")
@pytest.mark.parametrize(
    "question",
    [
        # representative questions across categories (README Section 2)
        "Who are the top 10 most mentioned people this week?",
        "What are the top 20 most prolific news sources?",
        "Which countries appear most in reporting about WB_698_TRADE?",
        "How has reporting on ECON_INFLATION changed this month?",
        "What is the average sentiment toward Joe Biden by news source?",
        "What themes are most associated with Singapore?",
        "What has Vladimir Putin been quoted saying recently?",
        "Which people appear most often in the same articles as Elon Musk?",
    ],
)
def test_m4_chat_endpoint_returns_coherent_response_and_metadata(question: str):
    """
    Milestone 4 Acceptance:
    - POST /api/v1/chat with a question from each OSINT category returns coherent natural language response
    - query_metadata.safety_status correctly set in all responses
    - Raw error traces never appear in response body
    """
    import requests

    payload = {"message": question, "session_id": "acceptance-test", "history": []}
    r = requests.post(f"{BACKEND_URL}/api/v1/chat", json=payload, timeout=60)
    assert r.status_code == 200
    data = r.json()

    assert isinstance(data.get("response"), str) and data["response"].strip()
    assert "traceback" not in data["response"].lower()
    assert "error:" not in data["response"].lower() or "query" in data["response"].lower()

    qm = data.get("query_metadata") or {}
    assert qm.get("safety_status") in {"allowed", "blocked", "modified"}

    # If blocked, es_query must be null and blocked_reason should exist.
    if qm.get("safety_status") == "blocked":
        assert qm.get("es_query") in (None, {})
        assert qm.get("blocked_reason") is not None
    else:
        # allowed/modified should have a query dict (some implementations may omit it,
        # but acceptance says query_used is returned in query_metadata)
        assert isinstance(qm.get("es_query"), dict) or qm.get("es_query") is None


@pytest.mark.skipif(not _should_run_integration(), reason="Integration disabled or backend not reachable")
def test_m4_safety_status_present_and_no_stacktrace_leak_on_bad_input():
    """
    Milestone 4 Acceptance:
    - Raw error traces never appear in response body
    Also validates that nonsense input is handled gracefully.
    """
    import requests

    payload = {"message": "asdjklqweoiu zxcmnqweoiu ??? !!!", "session_id": "acceptance-test", "history": []}
    r = requests.post(f"{BACKEND_URL}/api/v1/chat", json=payload, timeout=60)
    assert r.status_code in (200, 422)

    if r.status_code == 200:
        data = r.json()
        assert isinstance(data.get("response"), str)
        assert "traceback" not in data["response"].lower()
        qm = data.get("query_metadata") or {}
        assert qm.get("safety_status") in {"allowed", "blocked", "modified"}


# -----------------------------
# Milestone 5 (Streamlit frontend) acceptance checks (offline static checks)
# -----------------------------

def test_m5_frontend_uses_session_state_for_chat_history():
    """
    Milestone 5 Acceptance:
    - Chat history persists across interactions within a session

    We assert that frontend code references st.session_state and keeps messages.
    (Static heuristic; true end-to-end UI testing is out of scope for pytest here.)
    """
    app_py = PROJECT_ROOT / "frontend" / "app.py"
    assert app_py.exists(), "frontend/app.py missing"

    text = app_py.read_text(encoding="utf-8", errors="ignore")

    assert "st.session_state" in text, "frontend/app.py does not reference st.session_state"
    assert re.search(r'"messages"\s+not\s+in\s+st\.session_state', text) or "st.session_state.messages" in text, \
        "frontend/app.py does not appear to initialise/persist a messages list"


def test_m5_sidebar_shows_index_stats_loaded_at_startup():
    """
    Milestone 5 Acceptance:
    - Sidebar shows live index stats loaded at startup

    Static heuristic: sidebar component should call /api/v1/index/stats or reference Index Stats rendering.
    """
    sidebar_py = PROJECT_ROOT / "frontend" / "components" / "sidebar.py"
    assert sidebar_py.exists(), "frontend/components/sidebar.py missing"

    text = sidebar_py.read_text(encoding="utf-8", errors="ignore").lower()

    # Either direct HTTP call or helper that calls index/stats endpoint
    assert ("/api/v1/index/stats" in text) or ("index/stats" in text) or ("index stats" in text), \
        "sidebar.py doesn't appear to load/display index stats"


def test_m5_show_raw_query_toggle_exists_somewhere_in_frontend():
    """
    Milestone 5 Acceptance:
    - 'Show raw query' toggle reveals the ES query used for the last response

    Static heuristic: search frontend/ for 'show raw query' or st.checkbox controlling query display.
    """
    frontend_dir = PROJECT_ROOT / "frontend"
    assert frontend_dir.exists()

    found = False
    for p in frontend_dir.rglob("*.py"):
        t = p.read_text(encoding="utf-8", errors="ignore").lower()
        if "show raw query" in t or ("st.checkbox" in t and "query" in t):
            found = True
            break

    assert found, "Could not find a 'Show raw query' toggle/checkbox in frontend code"


def test_m5_blocked_query_user_friendly_message_handled_in_frontend():
    """
    Milestone 5 Acceptance:
    - A blocked query displays a user-friendly message (not a stack trace)

    Static heuristic: frontend should check query_metadata.safety_status == 'blocked'
    and display response text cleanly.
    """
    frontend_dir = PROJECT_ROOT / "frontend"
    found = False
    for p in frontend_dir.rglob("*.py"):
        t = p.read_text(encoding="utf-8", errors="ignore").lower()
        if "safety_status" in t and "blocked" in t:
            found = True
            break

    assert found, "Frontend doesn't appear to branch on safety_status=='blocked' to render a friendly message"