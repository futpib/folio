from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ibkr_tws_http.models import (
    ContractSpec,
    ImpliedVolatilityResponse,
    MarketDataSnapshotResponse,
    MarketDataType,
    ObservedOptionHistoryRequest,
)
from ibkr_tws_http.observations import TransientOptionObservationStore


def _snapshot(con_id: int, collected_at: datetime) -> MarketDataSnapshotResponse:
    contract = ContractSpec(con_id=con_id, sec_type="OPT")
    return MarketDataSnapshotResponse(
        contract=contract,
        requested_market_data_type=MarketDataType.AUTO,
        market_data_type=MarketDataType.DELAYED,
        market_quote_complete=False,
        quote_complete=False,
        model_complete=True,
        enrichment_complete=False,
        complete=False,
        collected_at=collected_at,
        implied_volatility=ImpliedVolatilityResponse(
            value=0.75,
            source="model",
            bid_implied_volatility=None,
            ask_implied_volatility=None,
            quote_observed_at=None,
            quote_age_seconds=None,
            spread_percentage=None,
            market_mid_eligible=False,
            market_mid_rejection_reason="missing_bid_or_ask",
            max_quote_age_seconds=5,
        ),
        fields={},
    )


def test_observations_are_bounded_and_cleared_at_process_lifecycle_boundaries() -> None:
    async def scenario() -> None:
        store = TransientOptionObservationStore(max_samples=2)
        await store.start()
        started = datetime(2026, 8, 26, tzinfo=UTC)
        await store.record(_snapshot(1, started))
        await store.record(_snapshot(1, started + timedelta(seconds=1)))
        await store.record(_snapshot(1, started + timedelta(seconds=2)))

        history = await store.query(
            ObservedOptionHistoryRequest(contract=ContractSpec(con_id=1, sec_type="OPT"))
        )
        assert await store.count() == 2
        assert [sample.collected_at for sample in history.samples] == [
            started + timedelta(seconds=1),
            started + timedelta(seconds=2),
        ]
        assert history.continuous_coverage is False
        assert "cleared on restart" in history.coverage_note

        await store.stop()
        assert await store.count() == 0

    asyncio.run(scenario())


def test_transient_observation_store_requires_a_positive_bound() -> None:
    with pytest.raises(ValueError, match="max_samples must be positive"):
        TransientOptionObservationStore(max_samples=0)
