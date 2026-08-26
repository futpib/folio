from __future__ import annotations

import asyncio
from collections import OrderedDict

from .models import (
    ContractSpec,
    MarketDataSnapshotResponse,
    ObservedOptionHistoryRequest,
    ObservedOptionHistoryResponse,
    ObservedOptionSnapshotResponse,
)


def _contract_key(contract: ContractSpec) -> str:
    if contract.con_id is not None:
        return f"conid:{contract.con_id}"
    return contract.model_dump_json(exclude_none=True)


class TransientOptionObservationStore:
    def __init__(self, *, max_samples: int = 10_000) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self._max_samples = max_samples
        self._samples: OrderedDict[
            tuple[str, str],
            ObservedOptionSnapshotResponse,
        ] = OrderedDict()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self.clear()

    async def stop(self) -> None:
        await self.clear()

    async def clear(self) -> None:
        async with self._lock:
            self._samples.clear()

    async def record(self, snapshot: MarketDataSnapshotResponse) -> None:
        implied = snapshot.implied_volatility
        if (
            snapshot.contract.sec_type != "OPT"
            or implied is None
            or implied.value is None
            or implied.source is None
        ):
            return
        sample = ObservedOptionSnapshotResponse(
            collected_at=snapshot.collected_at,
            implied_volatility=implied.value,
            source=implied.source,
            bid=snapshot.bid,
            ask=snapshot.ask,
            quote_observed_at=implied.quote_observed_at,
            quote_age_seconds=implied.quote_age_seconds,
            spread_percentage=implied.spread_percentage,
        )
        key = (_contract_key(snapshot.contract), snapshot.collected_at.isoformat())
        async with self._lock:
            self._samples[key] = sample
            self._samples.move_to_end(key)
            while len(self._samples) > self._max_samples:
                self._samples.popitem(last=False)

    async def count(self) -> int:
        async with self._lock:
            return len(self._samples)

    async def query(
        self,
        request: ObservedOptionHistoryRequest,
    ) -> ObservedOptionHistoryResponse:
        contract_key = _contract_key(request.contract)
        async with self._lock:
            samples = [
                sample
                for (stored_contract, _), sample in self._samples.items()
                if stored_contract == contract_key
                and (request.start is None or sample.collected_at >= request.start)
                and (request.end is None or sample.collected_at <= request.end)
            ]
        samples = samples[-request.limit :]
        return ObservedOptionHistoryResponse(
            contract=request.contract,
            first_observation_at=samples[0].collected_at if samples else None,
            last_observation_at=samples[-1].collected_at if samples else None,
            samples=samples,
        )
