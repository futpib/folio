from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

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


class OptionSnapshotJournal:
    def __init__(self, path: Path | None) -> None:
        self._path = path.expanduser() if path is not None else None
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._path is not None

    async def start(self) -> None:
        if self._path is None:
            return
        await asyncio.to_thread(self._open)

    async def stop(self) -> None:
        await asyncio.to_thread(self._close)

    async def record(self, snapshot: MarketDataSnapshotResponse) -> None:
        implied = snapshot.implied_volatility
        if (
            not self.enabled
            or snapshot.contract.sec_type != "OPT"
            or implied is None
            or implied.value is None
            or implied.source is None
        ):
            return
        await asyncio.to_thread(self._record, snapshot)

    async def count(self) -> int:
        if not self.enabled:
            return 0
        return await asyncio.to_thread(self._count)

    async def query(
        self,
        request: ObservedOptionHistoryRequest,
    ) -> ObservedOptionHistoryResponse:
        if not self.enabled:
            return ObservedOptionHistoryResponse(contract=request.contract, samples=[])
        rows = await asyncio.to_thread(self._query, request)
        samples = [
            ObservedOptionSnapshotResponse(
                collected_at=datetime.fromisoformat(row[0]),
                implied_volatility=row[1],
                source=row[2],
                bid=row[3],
                ask=row[4],
                quote_observed_at=(datetime.fromisoformat(row[5]) if row[5] else None),
                quote_age_seconds=row[6],
                spread_percentage=row[7],
            )
            for row in rows
        ]
        return ObservedOptionHistoryResponse(
            contract=request.contract,
            first_observation_at=samples[0].collected_at if samples else None,
            last_observation_at=samples[-1].collected_at if samples else None,
            samples=samples,
        )

    def _open(self) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._connection is not None:
                return
            connection = sqlite3.connect(self._path, check_same_thread=False)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS option_snapshots (
                    contract_key TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    implied_volatility REAL NOT NULL,
                    source TEXT NOT NULL CHECK (source IN ('market_mid', 'model')),
                    bid REAL,
                    ask REAL,
                    quote_observed_at TEXT,
                    quote_age_seconds REAL,
                    spread_percentage REAL,
                    PRIMARY KEY (contract_key, collected_at)
                )
                """
            )
            connection.commit()
            self._connection = connection

    def _close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()

    def _record(self, snapshot: MarketDataSnapshotResponse) -> None:
        implied = snapshot.implied_volatility
        assert implied is not None and implied.value is not None and implied.source is not None
        with self._lock:
            connection = self._require_connection()
            connection.execute(
                """
                INSERT OR IGNORE INTO option_snapshots (
                    contract_key, collected_at, implied_volatility, source, bid, ask,
                    quote_observed_at, quote_age_seconds, spread_percentage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _contract_key(snapshot.contract),
                    snapshot.collected_at.astimezone(UTC).isoformat(),
                    implied.value,
                    implied.source,
                    snapshot.bid,
                    snapshot.ask,
                    (
                        implied.quote_observed_at.astimezone(UTC).isoformat()
                        if implied.quote_observed_at is not None
                        else None
                    ),
                    implied.quote_age_seconds,
                    implied.spread_percentage,
                ),
            )
            connection.commit()

    def _count(self) -> int:
        with self._lock:
            row = (
                self._require_connection()
                .execute("SELECT COUNT(*) FROM option_snapshots")
                .fetchone()
            )
        return int(row[0])

    def _query(self, request: ObservedOptionHistoryRequest) -> list[tuple]:
        conditions = ["contract_key = ?"]
        parameters: list[object] = [_contract_key(request.contract)]
        if request.start is not None:
            conditions.append("collected_at >= ?")
            parameters.append(request.start.astimezone(UTC).isoformat())
        if request.end is not None:
            conditions.append("collected_at <= ?")
            parameters.append(request.end.astimezone(UTC).isoformat())
        parameters.append(request.limit)
        statement = f"""
            SELECT collected_at, implied_volatility, source, bid, ask,
                   quote_observed_at, quote_age_seconds, spread_percentage
            FROM (
                SELECT collected_at, implied_volatility, source, bid, ask,
                       quote_observed_at, quote_age_seconds, spread_percentage
                FROM option_snapshots
                WHERE {" AND ".join(conditions)}
                ORDER BY collected_at DESC
                LIMIT ?
            )
            ORDER BY collected_at ASC
        """
        with self._lock:
            rows = self._require_connection().execute(statement, parameters).fetchall()
        return rows

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("option snapshot journal is not started")
        return self._connection
