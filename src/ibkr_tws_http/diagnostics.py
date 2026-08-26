from __future__ import annotations

from .gateway import GatewayRequestError
from .models import (
    CapabilitiesResponse,
    CapabilityStatusResponse,
    MarketDataSnapshotResponse,
)


class CapabilityTracker:
    _ENTITLEMENT_CODES = frozenset({354, 10089, 10090, 10091, 10167, 10168, 10186, 10197})
    _OPTION_HISTORY_CODES = frozenset({162, 2188})

    def __init__(self) -> None:
        self._live_available = False
        self._live_unavailable_codes: set[int] = set()
        self._delayed_model_available = False
        self._option_history_unavailable_codes: set[int] = set()

    def observe_snapshot(self, snapshot: MarketDataSnapshotResponse) -> None:
        codes = {notice.code for notice in snapshot.tws_notices if notice.code is not None}
        self._live_unavailable_codes.update(codes & self._ENTITLEMENT_CODES)
        if snapshot.market_data_type.value in {"live", "frozen"} and snapshot.market_quote_complete:
            self._live_available = True
        if (
            snapshot.market_data_type.value in {"delayed", "delayed-frozen"}
            and snapshot.model_complete
        ):
            self._delayed_model_available = True

    def observe_error(self, error: GatewayRequestError) -> None:
        if error.code in self._ENTITLEMENT_CODES:
            self._live_unavailable_codes.add(error.code)
        if error.code in self._OPTION_HISTORY_CODES:
            self._option_history_unavailable_codes.add(error.code)

    def response(
        self,
        *,
        tws_connected: bool,
        observation_samples: int,
    ) -> CapabilitiesResponse:
        if self._live_available:
            live_status = "available"
        elif self._live_unavailable_codes:
            live_status = "unavailable"
        else:
            live_status = "unknown"
        return CapabilitiesResponse(
            tws_connected=tws_connected,
            live_market_quotes=CapabilityStatusResponse(
                status=live_status,
                evidence_codes=sorted(self._live_unavailable_codes),
            ),
            delayed_option_model=CapabilityStatusResponse(
                status="available" if self._delayed_model_available else "unknown",
            ),
            exact_option_historical_data=CapabilityStatusResponse(
                status=("unavailable" if self._option_history_unavailable_codes else "unknown"),
                evidence_codes=sorted(self._option_history_unavailable_codes),
            ),
            transient_option_history_samples=observation_samples,
        )
