"""Run async coroutines from Celery sync tasks safely.

With ``--pool=solo`` (and sometimes prefork) Celery reuses one process.
Each ``asyncio.run()`` creates a new event loop, but SQLAlchemy's async
engine keeps pooled connections bound to the previous loop — which causes:

    RuntimeError: ... Future ... attached to a different loop

Disposing the engine after every task clears that state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from app.database import engine

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    async def _runner() -> T:
        try:
            return await coro
        finally:
            await engine.dispose()

    return asyncio.run(_runner())
