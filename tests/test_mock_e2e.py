from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ibkr_tws_http.app import create_app
from ibkr_tws_http.config import ServerConfig, Settings, TwsConfig
from ibkr_tws_http.mock_tws import OPTIONS, UNDERLYING, MockTwsClient, create_mock_gateway


def _option_spec(index: int) -> dict[str, object]:
    option = OPTIONS[index]
    return {
        "con_id": option.conId,
        "symbol": option.symbol,
        "sec_type": option.secType,
        "exchange": option.exchange,
        "currency": option.currency,
        "last_trade_date_or_contract_month": option.lastTradeDateOrContractMonth,
        "strike": option.strike,
        "right": option.right,
        "multiplier": option.multiplier,
    }


def test_no_subscription_workflow_against_mock_tws_callbacks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    MockTwsClient.instances.clear()
    tws = TwsConfig(
        connect_timeout_seconds=0.25,
        request_timeout_seconds=0.25,
        reconnect_delay_seconds=0.25,
    )
    settings = Settings(
        server=ServerConfig(),
        tws=tws,
    )
    app = create_app(settings, gateway=create_mock_gateway(tws))

    with TestClient(app) as client:
        chain = client.get(
            "/v1/options/chain-snapshot",
            params={
                "symbol": "XXI",
                "expiration": "20280121",
                "strike_min": 2.5,
                "strike_max": 10,
                "market_data_type": "auto",
                "timeout_seconds": 3,
                "max_concurrency": 2,
            },
        )
        assert chain.status_code == 200, chain.text
        payload = chain.json()
        assert len(payload["quotes"]) == 8
        assert all(quote["snapshot"] is not None for quote in payload["quotes"])
        assert payload["underlying_snapshot"] is None
        assert payload["underlying_error"]["message"] == "TWS market data request timed out"
        assert payload["retried_contracts"] >= 4
        assert payload["reference_underlying_price"] == 6.28
        assert payload["reference_underlying_price_source"] == "option_model_median"

        put_quotes = [quote for quote in payload["quotes"] if quote["contract"]["right"] == "P"]
        assert all(quote["attempts"] == 2 for quote in put_quotes)
        for quote in payload["quotes"]:
            snapshot = quote["snapshot"]
            assert snapshot["market_data_type"] in {"delayed", "delayed-frozen"}
            assert snapshot["market_quote_complete"] is False
            assert snapshot["model_complete"] is True
            assert snapshot["implied_volatility"]["source"] == "model"
            assert snapshot["implied_volatility"]["market_mid_rejection_reason"] == (
                "missing_bid_or_ask"
            )
            assert snapshot["implied_volatility"]["quote_observed_at"] is None
            assert {notice["code"] for notice in snapshot["tws_notices"]} >= {10091, 10167}

        portfolio = client.get(
            "/v1/options/portfolio-snapshot",
            params={"timeout_seconds": 3, "max_concurrency": 1},
        )
        assert portfolio.status_code == 200, portfolio.text
        portfolio_payload = portfolio.json()
        assert portfolio_payload["position_count"] == 2
        assert portfolio_payload["returned_count"] == 2
        assert portfolio_payload["coverage_percentage"] == 100
        assert {quote["position"] for quote in portfolio_payload["quotes"]} == {"3", "-2"}

        comparison = client.post(
            "/v1/options/volatility-comparison",
            json={
                "option_contract": _option_spec(0),
                "underlying_contract": {
                    "con_id": UNDERLYING.conId,
                    "symbol": "XXI",
                    "sec_type": "STK",
                    "exchange": "SMART",
                    "primary_exchange": "NYSE",
                    "currency": "USD",
                },
            },
        )
        assert comparison.status_code == 200, comparison.text
        metrics = comparison.json()["metrics"]
        assert metrics["current_option_implied_volatility"] == 0.745
        assert metrics["current_option_implied_volatility_source"] == "model"
        assert metrics["latest_underlying_implied_volatility"] == 0.69
        assert metrics["latest_underlying_historical_volatility"] == 0.5
        assert metrics["option_premium_to_underlying_implied"] == 0.05500000000000005
        assert metrics["option_ratio_to_underlying_implied"] > 1
        assert metrics["option_percentile_in_underlying_implied_history"] == 100

        observed = client.post(
            "/v1/options/observed-history",
            json={"contract": {"con_id": OPTIONS[0].conId, "sec_type": "OPT"}},
        )
        assert observed.status_code == 200, observed.text
        observed_payload = observed.json()
        assert observed_payload["continuous_coverage"] is False
        assert observed_payload["samples"]
        assert all(sample["source"] == "model" for sample in observed_payload["samples"])

        daily_history = client.post(
            "/v1/historical-bars",
            json={"contract": _option_spec(0), "bar_size": "1 day"},
        )
        intraday_history = client.post(
            "/v1/historical-bars",
            json={"contract": _option_spec(0), "bar_size": "5 mins"},
        )
        assert daily_history.status_code == 502
        assert daily_history.json()["tws_error"]["code"] == 162
        assert intraday_history.status_code == 502
        assert intraday_history.json()["tws_error"]["code"] == 2188

        capabilities = client.get("/v1/capabilities")
        assert capabilities.status_code == 200
        capability_payload = capabilities.json()
        assert capability_payload["live_market_quotes"] == {
            "status": "unavailable",
            "evidence_codes": [10091, 10167],
        }
        assert capability_payload["delayed_option_model"]["status"] == "available"
        assert capability_payload["exact_option_historical_data"] == {
            "status": "unavailable",
            "evidence_codes": [162, 2188],
        }
        assert capability_payload["transient_option_history_enabled"] is True
        assert capability_payload["transient_option_history_samples"] >= 8
        assert capability_payload["adaptive_batch_scheduler"] is True
        assert capability_payload["continuous_uptime_required"] is False
        assert capability_payload["additional_market_data_subscriptions_required"] is False
        assert capability_payload["persistent_storage"] is False
        assert capability_payload["all_state_transient"] is True

    assert list(tmp_path.iterdir()) == []

    with TestClient(app) as restarted_client:
        cleared = restarted_client.post(
            "/v1/options/observed-history",
            json={"contract": {"con_id": OPTIONS[0].conId, "sec_type": "OPT"}},
        )
        assert cleared.status_code == 200
        assert cleared.json()["samples"] == []
