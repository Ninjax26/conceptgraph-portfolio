"""Drain an in-flight thread operation before releasing its document lease."""

import asyncio
import contextlib


async def run_blocking_safely(function, /, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling an await cannot stop a Python thread or retract a remote write.
        # Keep the heartbeat alive until this operation finishes, then release the lease.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.result()
        raise
