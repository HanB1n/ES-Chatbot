# backend/services/query_generator.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import re

from config import settings


class QueryGenerationError(Exception):
    """Raised when the backend cannot translate a natural-language question into an ES query."""


# Appendix A mapping (single source of truth for query generation)
FIELD_MAP: Dict[str, str] = {
    # Time
    "date": "V21Date",
    # Entities
    "persons_text": "V2Persons.V1Person",
    "persons_keyword": "V2Persons.V1Person.keyword",
    "orgs_text": "V2Orgs.V1Org",
    "orgs_keyword": "V2Orgs.V1Org.keyword",
    "all_names_text": "V21AllNames.Name",
    "all_names_keyword": "V21AllNames.Name.keyword",
    # Geo
    "country_keyword": "V2Locations.CountryCode.keyword",
    "location_text": "V2Locations.FullName",
    "location_keyword": "V2Locations.FullName.keyword",
    "geo_point": "location",
    # Themes
    "theme_text": "V2EnhancedThemes.V2Theme",
    "theme_keyword": "V2EnhancedThemes.V2Theme.keyword",
    # Sources
    "source_text": "V2SrcCmnName.V2SrcCmnName",
    "source_keyword": "V2SrcCmnName.V2SrcCmnName.keyword",
    # Article fields
    "url": "V2DocId",
    "title": "V2ExtrasXML.Title",
    # Tone
    "tone": "V15Tone.Tone",
    "positive": "V15Tone.PositiveScore",
    "negative": "V15Tone.NegativeScore",
    # Quotes
    "quote": "V21Quotations.Quote",
    "quote_verb": "V21Quotations.Verb",
}


@dataclass(frozen=True)
class ParsedTimeRange:
    gte: str
    lte: str = "now"


class QueryGenerator:
    """
    Milestone 2 implementation:
    - Minimal dependencies (no LangChain/Chroma/OpenAI SDK).
    - Deterministic rule-based NL → ES query dict using Appendix A fields.

    This is intentionally conservative: if we cannot confidently map the question,
    we raise QueryGenerationError with a clear message.
    """

    def __init__(self) -> None:
        self.max_docs_default = int(getattr(settings, "max_result_docs", 20))

    def generate(self, question: str, conversation_history: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
        if not question or not question.strip():
            raise QueryGenerationError("Empty question.")

        q = self._normalise(question)

        # 1) Aggregation: "top N <entity> ..."
        top_n = self._extract_top_n(q)
        if top_n is not None:
            return self._build_topn_aggregation(q, top_n)

        # 2) Retrieval: "show/find articles ..."
        if self._looks_like_retrieval(q):
            return self._build_retrieval_query(q)

        raise QueryGenerationError(
            "Unsupported question pattern. Try: "
            "'Who are the top 10 people mentioned this week?' or "
            "'Show all articles mentioning <person> in the last 30 days'."
        )

    # ----------------------------
    # Parsing helpers
    # ----------------------------

    def _normalise(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.strip().lower())

    def _extract_top_n(self, q: str) -> Optional[int]:
        # matches "top 10", "top10", "top ten" (limited word numbers)
        m = re.search(r"\btop\s*(\d{1,3})\b", q)
        if m:
            n = int(m.group(1))
            return max(1, min(n, 100))
        if "top ten" in q:
            return 10
        if "most " in q and ("mentioned" in q or "common" in q or "prolific" in q):
            # "most mentioned people this week" => default 10
            return 10
        return None

    def _parse_time_range(self, q: str) -> ParsedTimeRange:
        # Prefer explicit "last N days/weeks/months/years"
        m = re.search(r"\blast\s+(\d{1,3})\s*(day|days|week|weeks|month|months|year|years)\b", q)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            if unit.startswith("day"):
                return ParsedTimeRange(gte=f"now-{n}d/d")
            if unit.startswith("week"):
                return ParsedTimeRange(gte=f"now-{n}w/w")
            if unit.startswith("month"):
                return ParsedTimeRange(gte=f"now-{n}M/M")
            if unit.startswith("year"):
                return ParsedTimeRange(gte=f"now-{n}y/y")

        # Relative phrases
        if "this week" in q or "past week" in q or "past 7 days" in q or "last week" in q:
            return ParsedTimeRange(gte="now-7d/d")
        if "today" in q:
            return ParsedTimeRange(gte="now/d")
        if "yesterday" in q:
            # yesterday start to yesterday end (approx): now-1d/d to now/d
            return ParsedTimeRange(gte="now-1d/d", lte="now/d")
        if "this month" in q:
            return ParsedTimeRange(gte="now/M")
        if "this year" in q:
            return ParsedTimeRange(gte="now/y")

        # Default window for time-sensitive OSINT queries
        return ParsedTimeRange(gte="now-30d/d")

    def _entity_agg_field(self, q: str) -> Tuple[str, str]:
        """
        Returns (field, label) for aggregation based on question.
        """
        if any(w in q for w in ["people", "person", "persons"]):
            return FIELD_MAP["persons_keyword"], "people"
        if any(w in q for w in ["organisation", "organization", "org", "orgs", "organisations", "organizations"]):
            return FIELD_MAP["orgs_keyword"], "organisations"
        if any(w in q for w in ["source", "sources", "domain", "domains", "publisher", "publishers"]):
            return FIELD_MAP["source_keyword"], "sources"
        if any(w in q for w in ["theme", "themes", "topic", "topics"]):
            return FIELD_MAP["theme_keyword"], "themes"
        if any(w in q for w in ["country", "countries"]):
            return FIELD_MAP["country_keyword"], "countries"
        if any(w in q for w in ["location", "locations", "region", "regions"]):
            return FIELD_MAP["location_keyword"], "locations"

        # Fallback: entity tracking default = people
        return FIELD_MAP["persons_keyword"], "people"

    def _extract_subject_value(self, q: str) -> Optional[str]:
        """
        Tries to extract a target entity/theme/source after keywords like:
        mentioning/about/for/alongside/with/from
        Supports quoted strings: "elon musk"
        """
        # Quoted target: ... "X Y"
        m = re.search(r'["“](.+?)["”]', q)
        if m:
            val = m.group(1).strip()
            return val if val else None

        # Unquoted patterns
        for kw in ["mentioning", "about", "for", "alongside", "with", "from", "regarding", "on"]:
            m2 = re.search(rf"\b{kw}\s+([a-z0-9][a-z0-9\s\-\.\']{{1,80}})", q)
            if m2:
                val = m2.group(1).strip()
                # Stop at common trailing time phrases
                val = re.split(r"\b(last|past|this|today|yesterday|in)\b", val)[0].strip()
                # Avoid capturing generic words
                if val and val not in {"the", "a", "an"}:
                    return val
        return None

    def _looks_like_retrieval(self, q: str) -> bool:
        return any(p in q for p in ["show ", "find ", "list ", "give me ", "display "]) and "top" not in q

    # ----------------------------
    # Query builders
    # ----------------------------

    def _build_topn_aggregation(self, q: str, top_n: int) -> Dict[str, Any]:
        agg_field, _label = self._entity_agg_field(q)
        tr = self._parse_time_range(q)

        filters: List[Dict[str, Any]] = [
            {"range": {FIELD_MAP["date"]: {"gte": tr.gte, "lte": tr.lte}}}
        ]

        # Optional: if user asks "alongside/with/about X", add a filter.
        subject = self._extract_subject_value(q)
        if subject:
            # Heuristic: decide which field to filter on depending on keywords in question.
            if "theme" in q or "topic" in q:
                filters.append({"match": {FIELD_MAP["theme_text"]: subject}})
            elif "source" in q or "domain" in q:
                filters.append({"match": {FIELD_MAP["source_text"]: subject}})
            elif "organisation" in q or "organization" in q or "org" in q:
                filters.append({"match": {FIELD_MAP["orgs_text"]: subject}})
            elif "country" in q:
                # prefer keyword exact match if looks like 2-3 letter code, else match name field
                if re.fullmatch(r"[a-z]{2,3}", subject.lower()):
                    filters.append({"term": {FIELD_MAP["country_keyword"]: subject.upper()}})
                else:
                    filters.append({"match": {FIELD_MAP["location_text"]: subject}})
            else:
                filters.append({"match": {FIELD_MAP["persons_text"]: subject}})

        return {
            "size": 0,
            "query": {"bool": {"filter": filters}},
            "aggs": {
                "top_terms": {
                    "terms": {
                        "field": agg_field,
                        "size": top_n,
                    }
                }
            },
            "track_total_hits": True,
        }

    def _build_retrieval_query(self, q: str) -> Dict[str, Any]:
        tr = self._parse_time_range(q)
        subject = self._extract_subject_value(q)
        if not subject:
            raise QueryGenerationError("Retrieval query detected but no target entity/topic found (try quoting it).")

        filters: List[Dict[str, Any]] = [
            {"range": {FIELD_MAP["date"]: {"gte": tr.gte, "lte": tr.lte}}}
        ]

        # Decide the primary match field
        if any(w in q for w in ["quote", "quotes", "quoted"]):
            filters.append({"match": {FIELD_MAP["quote"]: subject}})
        elif any(w in q for w in ["theme", "themes", "topic", "topics"]):
            filters.append({"match": {FIELD_MAP["theme_text"]: subject}})
        elif any(w in q for w in ["organisation", "organization", "org", "orgs"]):
            filters.append({"match": {FIELD_MAP["orgs_text"]: subject}})
        elif any(w in q for w in ["source", "domain", "publisher"]):
            filters.append({"match": {FIELD_MAP["source_text"]: subject}})
        elif any(w in q for w in ["country", "countries", "region", "regions", "location", "locations"]):
            # country codes vs names
            if re.fullmatch(r"[a-z]{2,3}", subject.lower()):
                filters.append({"term": {FIELD_MAP["country_keyword"]: subject.upper()}})
            else:
                filters.append({"match": {FIELD_MAP["location_text"]: subject}})
        else:
            # default: person
            filters.append({"match": {FIELD_MAP["persons_text"]: subject}})

        # Small, useful _source set for retrieval
        source_includes = [
            FIELD_MAP["date"],
            FIELD_MAP["title"],
            FIELD_MAP["url"],
            FIELD_MAP["source_text"],
            FIELD_MAP["persons_text"],
            FIELD_MAP["orgs_text"],
            FIELD_MAP["location_text"],
            FIELD_MAP["theme_text"],
            FIELD_MAP["tone"],
        ]

        # Sort by date descending by default
        return {
            "size": self.max_docs_default,
            "query": {"bool": {"filter": filters}},
            "_source": {"includes": source_includes},
            "sort": [{FIELD_MAP["date"]: {"order": "desc"}}],
            "track_total_hits": True,
        }
