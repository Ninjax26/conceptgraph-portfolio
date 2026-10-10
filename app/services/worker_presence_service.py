"""Short-lived worker presence, independent of per-document leases."""

from sqlalchemy import text
from app.core.database import AsyncSessionLocal


async def renew_worker_presence(instance_id, concurrency, ttl_seconds):
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM worker_presence WHERE expires_at < now()"))
        await session.execute(text("INSERT INTO worker_presence (instance_id, concurrency, expires_at) "
                                   "VALUES (:id, :concurrency, now() + :ttl * interval '1 second') "
                                   "ON CONFLICT (instance_id) DO UPDATE SET concurrency=:concurrency, expires_at=excluded.expires_at"),
                              {"id": instance_id, "concurrency": concurrency, "ttl": ttl_seconds})
        await session.commit()


async def active_worker_count():
    async with AsyncSessionLocal() as session:
        return int((await session.execute(text("SELECT count(*) FROM worker_presence WHERE expires_at > now()"))).scalar_one())


async def remove_worker_presence(instance_id):
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM worker_presence WHERE instance_id=:id"), {"id": instance_id})
        await session.commit()
