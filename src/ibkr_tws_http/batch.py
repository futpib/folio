from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .gateway import Gateway, GatewayRequestError, GatewayTimeout, MarketDataSnapshot
from .models import ContractSpec, MarketDataSnapshotRequest, MarketDataType


@dataclass(frozen=True, slots=True)
class BatchItemResult:
    snapshot: MarketDataSnapshot | None
    error: GatewayRequestError | GatewayTimeout | None


@dataclass(frozen=True, slots=True)
class BatchCollection:
    results: dict[int, BatchItemResult]
    attempts: tuple[int, ...]
    retried_contracts: int
    collection_started_at: datetime
    collected_at: datetime
    preflight_attempted: bool
    preflight_market_data_type: MarketDataType | None
    preflight_seconds: float


async def collect_market_data_batch(
    gateway: Gateway,
    specs: list[ContractSpec],
    *,
    market_data_type: MarketDataType,
    max_concurrency: int,
    timeout_seconds: float,
    request_timeout_seconds: float,
    preflight_index: int | None = None,
) -> BatchCollection:
    collection_started_at = datetime.now(UTC)
    deadline = time.monotonic() + timeout_seconds
    results: dict[int, BatchItemResult] = {}
    attempts = [0] * len(specs)

    async def collect_one(index: int, budget: float) -> None:
        attempts[index] += 1
        request = MarketDataSnapshotRequest(
            contract=specs[index],
            market_data_type=market_data_type,
        )
        try:
            snapshot = await asyncio.wait_for(
                gateway.market_data_snapshot(request),
                timeout=max(0.001, budget),
            )
        except TimeoutError:
            results[index] = BatchItemResult(
                snapshot=None,
                error=GatewayTimeout("adaptive batch attempt timed out"),
            )
        except (GatewayRequestError, GatewayTimeout) as exc:
            results[index] = BatchItemResult(snapshot=None, error=exc)
        else:
            results[index] = BatchItemResult(snapshot=snapshot, error=None)

    preflight_attempted = (
        market_data_type is MarketDataType.AUTO
        and preflight_index is not None
        and 0 <= preflight_index < len(specs)
    )
    preflight_market_data_type = None
    preflight_seconds = 0.0
    if preflight_attempted:
        preflight_started = time.monotonic()
        preflight_budget = min(2.0, max(0.001, (deadline - preflight_started) * 0.2))
        await collect_one(preflight_index, preflight_budget)
        preflight_seconds = time.monotonic() - preflight_started
        preflight_result = results.get(preflight_index)
        if preflight_result is not None and preflight_result.snapshot is not None:
            preflight_market_data_type = preflight_result.snapshot.market_data_type

    def needs_attempt(index: int) -> bool:
        result = results.get(index)
        return result is None or isinstance(result.error, GatewayTimeout)

    async def collect_wave(indices: list[int], wave_deadline: float) -> None:
        if not indices:
            return
        queue: asyncio.Queue[int] = asyncio.Queue()
        for index in indices:
            queue.put_nowait(index)
        concurrency = min(max_concurrency, len(indices))
        groups = max(1, math.ceil(len(indices) / concurrency))
        wave_budget = max(0.001, wave_deadline - time.monotonic())
        item_budget = min(request_timeout_seconds, max(0.001, wave_budget / groups))

        async def worker() -> None:
            while not queue.empty():
                remaining = wave_deadline - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await collect_one(index, min(item_budget, remaining))

        workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
        try:
            await asyncio.wait_for(
                asyncio.gather(*workers),
                timeout=max(0.001, wave_deadline - time.monotonic()),
            )
        except TimeoutError:
            for worker_task in workers:
                worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        except BaseException:
            for worker_task in workers:
                worker_task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

    first_wave = [
        index
        for index in range(len(specs))
        if attempts[index] == 0 or (attempts[index] < 2 and needs_attempt(index))
    ]
    remaining = max(0.0, deadline - time.monotonic())
    first_wave_deadline = time.monotonic() + remaining * 0.65
    await collect_wave(first_wave, first_wave_deadline)

    retry_indices = [
        index for index in range(len(specs)) if attempts[index] < 2 and needs_attempt(index)
    ]
    if retry_indices and time.monotonic() < deadline:
        await collect_wave(retry_indices, deadline)

    for index in range(len(specs)):
        if index not in results:
            results[index] = BatchItemResult(
                snapshot=None,
                error=GatewayTimeout("batch market data collection deadline reached"),
            )
        if attempts[index] == 0:
            attempts[index] = 1

    return BatchCollection(
        results=results,
        attempts=tuple(attempts),
        retried_contracts=sum(value > 1 for value in attempts),
        collection_started_at=collection_started_at,
        collected_at=datetime.now(UTC),
        preflight_attempted=preflight_attempted,
        preflight_market_data_type=preflight_market_data_type,
        preflight_seconds=preflight_seconds,
    )
