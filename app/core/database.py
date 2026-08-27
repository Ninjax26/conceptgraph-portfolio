from collections.abc import AsyncGenerator
from dataclasses import dataclass
import hashlib
from pathlib import Path
import uuid

from neo4j import AsyncDriver, AsyncGraphDatabase
from qdrant_client import QdrantClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text

from app.core.config import settings
from app.models.document_upload import Base


@dataclass(slots=True)
class DatabaseClients:
    postgres: AsyncSession
    neo4j: AsyncDriver
    qdrant: QdrantClient


postgres_engine: AsyncEngine = create_async_engine(
    settings.postgres_dsn,
    pool_pre_ping=True,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=postgres_engine,
    expire_on_commit=False,
    autoflush=False,
)

neo4j_driver: AsyncDriver = AsyncGraphDatabase.driver(
    settings.neo4j_uri,
    auth=(settings.neo4j_username, settings.neo4j_password),
    connection_timeout=settings.provider_timeout_seconds,
)

qdrant_client: QdrantClient = QdrantClient(
    url=settings.qdrant_url,
    api_key=settings.qdrant_api_key_value,
    cloud_inference=settings.embedding_provider == "qdrant_cloud",
    check_compatibility=False,
    timeout=settings.provider_timeout_seconds,
)


async def get_postgres_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def get_db() -> AsyncGenerator[DatabaseClients, None]:
    async with AsyncSessionLocal() as session:
        yield DatabaseClients(
            postgres=session,
            neo4j=neo4j_driver,
            qdrant=qdrant_client,
        )


async def initialize_database_schema() -> None:
    async with postgres_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        migrations = [
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS course_uuid VARCHAR(64)",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS stage VARCHAR(32) NOT NULL DEFAULT 'UPLOADED'",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS failure_category VARCHAR(32)",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS retryable BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS last_attempted_at TIMESTAMPTZ",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(128)",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS processed_chunk_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS graph_node_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE document_uploads ADD COLUMN IF NOT EXISTS graph_edge_count INTEGER NOT NULL DEFAULT 0",
            "CREATE INDEX IF NOT EXISTS ix_document_uploads_course_uuid ON document_uploads (course_uuid)",
            "CREATE INDEX IF NOT EXISTS ix_document_uploads_content_hash ON document_uploads (content_hash)",
            "CREATE INDEX IF NOT EXISTS ix_document_uploads_stage ON document_uploads (stage)",
            "CREATE INDEX IF NOT EXISTS ix_document_uploads_lease_expires_at ON document_uploads (lease_expires_at)",
            "ALTER TABLE processing_attempts ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ",
        ]
        for statement in migrations:
            await conn.execute(text(statement))
        await _migrate_legacy_uploads(conn)


async def _migrate_legacy_uploads(conn) -> None:
    rows = (
        await conn.execute(
            text("SELECT upload_id, course_id, stored_file_path, status, result_json, created_at FROM document_uploads WHERE course_uuid IS NULL")
        )
    ).mappings().all()
    for row in rows:
        normalized = " ".join(row["course_id"].strip().casefold().split())
        course_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"conceptgraph:course:{normalized}"))
        await conn.execute(
            text(
                "INSERT INTO courses (id, normalized_name, display_name) VALUES (:id, :normalized, :display) "
                "ON CONFLICT (normalized_name) DO NOTHING"
            ),
            {"id": course_uuid, "normalized": normalized, "display": row["course_id"].strip().upper()},
        )
        file_path = Path(row["stored_file_path"])
        content_hash = None
        if file_path.exists():
            digest = hashlib.sha256()
            with file_path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            content_hash = digest.hexdigest()
        result = row["result_json"] or {}
        chunks = int(result.get("chunks_indexed", 0))
        nodes = int(result.get("nodes_upserted", 0))
        edges = int(result.get("relationships_upserted", 0))
        if row["status"] == "completed" and chunks > 0:
            stage, status, retryable, category = "READY", "ready", False, None
        elif row["status"] in {"queued", "running"}:
            stage, status, retryable, category = "FAILED", "failed", True, "WORKER_ERROR"
        else:
            stage, status, retryable, category = "FAILED", "failed", False, "UNKNOWN_ERROR"
        await conn.execute(
            text(
                "UPDATE document_uploads SET course_uuid=:course_uuid, content_hash=COALESCE(content_hash,:hash), "
                "stage=:stage, status=:status, retryable=:retryable, failure_category=COALESCE(failure_category,:category), "
                "processed_chunk_count=:chunks, graph_node_count=:nodes, graph_edge_count=:edges, "
                "last_attempted_at=COALESCE(last_attempted_at, updated_at) WHERE upload_id=:upload_id"
            ),
            {"course_uuid": course_uuid, "hash": content_hash, "stage": stage, "status": status,
             "retryable": retryable, "category": category, "chunks": chunks, "nodes": nodes,
             "edges": edges, "upload_id": row["upload_id"]},
        )


async def close_database_connections() -> None:
    await postgres_engine.dispose()
    await neo4j_driver.close()
    qdrant_client.close()
