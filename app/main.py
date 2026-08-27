import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.api.endpoints.auth import router as auth_router
from app.api.endpoints.exam import router as exam_router
from app.api.endpoints.ingest import router as ingest_router
from app.api.endpoints.query import router as query_router
from app.core.database import close_database_connections, initialize_database_schema
from app.core.database import neo4j_driver, postgres_engine, qdrant_client
from app.core.config import settings
from app.core.processing_coordinator import processing_coordinator
from app.core.security import DemoProtectionMiddleware
from app.services.security_service import rate_limit_service
from app.services.document_processing_service import document_processing_service
from app.services.storage_service import storage_service
from sqlalchemy import text

LOCAL_DEV_ORIGIN_REGEX = r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await initialize_database_schema()
    await asyncio.to_thread(
        document_processing_service.ingestion_service.validate_qdrant_collection
    )
    await processing_coordinator.start()
    try:
        yield
    finally:
        await processing_coordinator.stop()
        await rate_limit_service.close()
        await close_database_connections()


app = FastAPI(title="ConceptGraph", lifespan=lifespan)

app.add_middleware(DemoProtectionMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://0.0.0.0:3000",
        "http://0.0.0.0:5173",
        *settings.configured_cors_origins,
    ],
    allow_origin_regex=LOCAL_DEV_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(query_router)
app.include_router(exam_router)
app.include_router(ingest_router)


@app.get("/api/v1/health", tags=["system"])
async def health_check() -> dict[str, str]:
    try:
        async with postgres_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable.") from exc
    return {"status": "healthy"}


@app.get("/api/v1/ready", tags=["system"])
async def readiness_check() -> dict[str, str | int]:
    unavailable: list[str] = []
    try:
        async with postgres_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        unavailable.append("postgresql")
    try:
        await asyncio.to_thread(qdrant_client.get_collections)
    except Exception:
        unavailable.append("qdrant")
    try:
        await neo4j_driver.verify_connectivity()
    except Exception:
        unavailable.append("neo4j")
    try:
        await asyncio.to_thread(storage_service.check_ready)
    except Exception:
        unavailable.append("object_storage")
    if not processing_coordinator.started:
        unavailable.append("processing_coordinator")
    if unavailable:
        raise HTTPException(
            status_code=503,
            detail="Required services are unavailable: " + ", ".join(unavailable),
        )
    return {
        "status": "ready",
        "processing_queue_depth": processing_coordinator.queue_depth,
    }
