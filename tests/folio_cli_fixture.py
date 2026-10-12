"""Run the real service/worker/CLI with a deterministic provider result."""

import asyncio

from folio.cli import daemon
from folio.models import Record, Snapshot
from folio.service import BrowserConnector


async def sync(self, since):
    await asyncio.sleep(1)
    return Snapshot(
        records=[Record(id="fixture", resource="orders", data={"merchant": "Test café"})],
        resources=["orders"],
        complete=True,
        coverage="fixture",
    )


BrowserConnector.sync = sync
daemon()
