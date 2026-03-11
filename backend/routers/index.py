# backend/routers/index.py

from fastapi import APIRouter
from models.schemas import IndexStats
from services.es_client import es_client 

router = APIRouter()

@router.get("/index/stats", response_model=IndexStats)
async def get_index_stats():
    """Return summary statistics about the GKG index."""
    stats = await es_client.get_index_stats()
    return stats