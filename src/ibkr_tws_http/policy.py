from __future__ import annotations

from dataclasses import replace

from .config import ExposureConfig
from .gateway import AccountValue, ContractInfo, ContractSearchResult, OptionChain, Position
from .models import ContractSpec, OptionChainsRequest


class PolicyDenied(ValueError):
    """A request or result is outside the configured exposure policy."""


class ExposurePolicy:
    def __init__(self, config: ExposureConfig):
        self._config = config

    def require_feature(self, feature: str) -> None:
        if feature not in self._config.features:
            raise PolicyDenied(f"feature {feature!r} is disabled")

    def check_contract(self, contract: ContractSpec) -> None:
        if contract.sec_type not in self._config.allowed_security_types:
            raise PolicyDenied(f"security type {contract.sec_type!r} is not allowed")
        if self._config.allowed_symbols and (
            contract.symbol is None or contract.symbol not in self._config.allowed_symbols
        ):
            raise PolicyDenied("symbol is not allowed")
        if (
            self._config.allowed_exchanges
            and contract.exchange not in self._config.allowed_exchanges
        ):
            raise PolicyDenied(f"exchange {contract.exchange!r} is not allowed")

    def check_option_chain(self, request: OptionChainsRequest) -> None:
        if request.underlying_security_type not in self._config.allowed_security_types:
            raise PolicyDenied(f"security type {request.underlying_security_type!r} is not allowed")
        derivative_type = "FOP" if request.underlying_security_type == "FUT" else "OPT"
        if derivative_type not in self._config.allowed_security_types:
            raise PolicyDenied(f"security type {derivative_type!r} is not allowed")
        if (
            self._config.allowed_symbols
            and request.underlying_symbol not in self._config.allowed_symbols
        ):
            raise PolicyDenied("symbol is not allowed")
        if (
            request.exchange
            and self._config.allowed_exchanges
            and request.exchange not in self._config.allowed_exchanges
        ):
            raise PolicyDenied(f"exchange {request.exchange!r} is not allowed")

    def filter_accounts(self, accounts: list[str]) -> list[str]:
        allowed = [account for account in accounts if self._account_allowed(account)]
        return [self.public_account(account) for account in allowed]

    def filter_account_summary(self, values: list[AccountValue]) -> list[AccountValue]:
        return [
            replace(value, account=self.public_account(value.account))
            for value in values
            if self._account_allowed(value.account)
            and value.tag in self._config.account_summary_tags
        ]

    def filter_positions(self, positions: list[Position]) -> list[Position]:
        return [
            replace(position, account=self.public_account(position.account))
            for position in positions
            if self._account_allowed(position.account) and self._contract_allowed(position.contract)
        ]

    def filter_contracts(self, contracts: list[ContractInfo]) -> list[ContractInfo]:
        return [contract for contract in contracts if self._contract_allowed(contract)]

    def filter_contract_search(
        self, results: list[ContractSearchResult]
    ) -> list[ContractSearchResult]:
        return [
            replace(
                result,
                derivative_security_types=tuple(
                    value
                    for value in result.derivative_security_types
                    if value in self._config.allowed_security_types
                ),
            )
            for result in results
            if self._contract_allowed(result.contract)
        ]

    def filter_option_chains(self, chains: list[OptionChain]) -> list[OptionChain]:
        if not self._config.allowed_exchanges:
            return chains
        return [chain for chain in chains if chain.exchange in self._config.allowed_exchanges]

    def public_account(self, account: str) -> str:
        alias = self._config.account_aliases.get(account)
        if alias is not None:
            return alias
        if self._config.expose_account_ids:
            return account
        return "account"

    def _account_allowed(self, account: str) -> bool:
        return not self._config.allowed_accounts or account in self._config.allowed_accounts

    def _contract_allowed(self, contract: ContractInfo) -> bool:
        if contract.sec_type not in self._config.allowed_security_types:
            return False
        if self._config.allowed_symbols and contract.symbol not in self._config.allowed_symbols:
            return False
        if self._config.allowed_exchanges:
            exchanges = {contract.exchange, contract.primary_exchange}
            if not any(value in self._config.allowed_exchanges for value in exchanges if value):
                return False
        return True
