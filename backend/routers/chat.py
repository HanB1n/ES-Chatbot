# backend/routers/chat.py

from fastapi import APIRouter
from models.schemas import ChatRequest, ChatResponse
from services.query_generator import QueryGenerator, QueryGenerationError

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Milestone 2: Query Generation Pipeline
    - Translate a plain-English question into an Elasticsearch query dict.
    - Return the generated query in query_metadata.es_query.
    - No safety layer yet, and NO execution against Elasticsearch.
    """
    query_generator = QueryGenerator()
    try:
        es_query = query_generator.generate(request.message, [h.model_dump() for h in request.history])
    except QueryGenerationError as e:
        return ChatResponse(
            response=f"I couldn't generate an Elasticsearch query for that request. Reason: {str(e)}",
            query_metadata={
                "es_query": None,
                "total_hits": None,
                "execution_time_ms": None,
                "safety_status": "blocked",
                "blocked_reason": "query_generation_error",
            },
            session_id=request.session_id,
        )

    return ChatResponse(
        response="Generated an Elasticsearch query for your request.",
        query_metadata={
            "es_query": es_query,
            "total_hits": None,
            "execution_time_ms": None,
            "safety_status": "allowed",
            "blocked_reason": None,
        },
        session_id=request.session_id,
    )
