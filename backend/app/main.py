"""Artha FastAPI application."""

import logging
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config import get_settings, Settings
from app.query.mysql_engine import MySQLQueryEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()
app = FastAPI(title="Artha", version="0.1.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global engine (lazy-loaded)
_engine: MySQLQueryEngine | None = None


def get_engine() -> MySQLQueryEngine:
    """Get or create the query engine."""
    global _engine
    if _engine is None:
        _engine = MySQLQueryEngine(settings.ARTHA_DATABASE_URL)
    return _engine


@app.on_event("startup")
async def startup():
    """Validate database connectivity."""
    engine = get_engine()
    if not await engine.ping():
        logger.warning("Failed to connect to database on startup")


class HealthResponse(BaseModel):
    status: str
    database: str
    backend: str = "mysql"


@app.get("/api/health", response_model=HealthResponse)
async def health():
    """Health check."""
    engine = get_engine()
    db_ok = await engine.ping()
    return HealthResponse(
        status="healthy" if db_ok else "degraded",
        database="ok" if db_ok else "error",
    )


class ChatRequest(BaseModel):
    question: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    conversation_id: str
    meta: dict


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Chat endpoint (minimal MVP)."""
    import uuid
    conv_id = req.conversation_id or str(uuid.uuid4())
    
    # TODO: full implementation with understanding + query execution
    return ChatResponse(
        answer="Feature coming soon",
        conversation_id=conv_id,
        meta={"engine": "mysql", "llm_calls": 0},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
