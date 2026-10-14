"""Drain in-flight thread or async writes before releasing a document fence."""

import asyncio
import contextlib


async def run_blocking_safely(function, /, *args, **kwargs):
    return await run_async_safely(asyncio.to_thread(function, *args, **kwargs))


async def run_async_safely(operation):
    """Drain a started write before cancellation releases its caller's fence.

    This cannot retract a remote commit or survive a process kill. The caller
    must still make writes idempotent and reconcile ambiguous remote failures.
    """
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Keep the caller's document lock/lease until the write has settled.
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
