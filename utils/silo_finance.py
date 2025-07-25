import json
import logging
from copy import deepcopy

from typing import Dict, List, Optional, Tuple
from eth_typing import ChecksumAddress
from web3.contract import Contract
from web3 import Web3

from constants.chains import Chain
from constants.silo_finance import (
    SiloFinanceMarket,
    SILO_FINANCE_INTEGRATION_ID_TO_MARKET,
)
from utils.web3_utils import W3_BY_CHAIN
from constants.silo_finance import PAGINATION_SIZE
from integrations.cached_balances_integration import CachedBalancesIntegration
from utils.web3_utils import fetch_events_logs_with_retry


class SiloFinance(CachedBalancesIntegration):
    """
    Silo Finance LP-eUSDe token markets integration.
    """

    def get_market(self) -> SiloFinanceMarket:
        return SILO_FINANCE_INTEGRATION_ID_TO_MARKET[self.integration_id]

    def get_silo_contract(self) -> Contract:
        market = self.get_market()
        return get_silo_contract(chain=self.chain, market=market)

    def get_logger(self) -> logging.Logger:
        logger = logging.getLogger(self.integration_id.get_column_name())
        logger.setLevel(logging.INFO)
        return logger

    def get_block_balances(
        self, cached_data: Dict[int, Dict[ChecksumAddress, float]], blocks: List[int]
    ) -> Dict[int, Dict[ChecksumAddress, float]]:
        logger = self.get_logger()
        market = self.get_market()
        logger.info(f"Getting block data for Silo Finance {market.address}")
        new_block_data: Dict[int, Dict[ChecksumAddress, float]] = {}
        if not blocks:
            logger.error("No blocks provided to get_block_balances")
            return new_block_data

        cache_copy: Dict[int, Dict[ChecksumAddress, float]] = deepcopy(cached_data)
        for block in sorted(blocks):
            # find the closest prev block in the data
            prev_block = self.start_block
            start = prev_block
            bals: Dict[ChecksumAddress, float] = {}
            if cache_copy:
                prev_block, bals = self.find_closest_cached_data(block, cache_copy)
                logger.info(
                    f"Found closest cached data for block {block}: {prev_block}"
                )
                start = prev_block + 1
            supply_index = self.fetch_supply_index(block)
            logger.info(f"Supply index on block {block}: {supply_index}")
            while start <= block:
                to_block = min(start + PAGINATION_SIZE, block)
                logger.info(
                    f"fetching transfers for market {market.address} from block {start} to block {to_block}"
                )
                # Transfers are used to keep track of the user positions.
                for transfer in self.fetch_transfers(
                    market=market, from_block=start, to_block=to_block
                ):
                    self.process_transfer_event(
                        transfer=transfer, bals=bals, supply_index=supply_index
                    )
                start = to_block + 1
                logger.warning(
                    f"processed transfers for market {market.address} from block {start} to block {to_block}"
                )
            new_block_data[block] = bals
            cache_copy[block] = bals
        return new_block_data

    def find_closest_cached_data(
        self, block: int, cached_data: Dict[int, Dict[ChecksumAddress, float]]
    ) -> Optional[Tuple[int, Dict[ChecksumAddress, float]]]:
        sorted_existing_blocks = sorted(
            cached_data,
            reverse=True,
        )
        for existing_block in sorted_existing_blocks:
            if existing_block < block:
                return (existing_block, deepcopy(cached_data[existing_block]))
        return None

    def fetch_supply_index(self, block: int) -> float:
        """
        Supply index is the number of assets that 1 share is worth. It's used to convert between shares and assets.
        1 shares = 1 * supply_index (assets)
        """
        market = self.get_market()
        silo_contract = self.get_silo_contract()
        return (
            silo_contract.functions.convertToAssets(10**market.shares_decimals).call(
                block_identifier=block
            )
            / 10**market.shares_decimals
        )

    def fetch_transfers(
        self, *, market: SiloFinanceMarket, from_block: int, to_block: int
    ):
        non_borrowable_token = get_non_borrowable_erc20_contract(
            chain=self.chain, market=market
        )
        yield from fetch_events_logs_with_retry(
            f"Silo Finance LP-eUSDe {market.address}",
            contract_event=non_borrowable_token.events.Transfer(),
            from_block=from_block,
            to_block=to_block,
        )

        borrowable_token = get_borrowable_erc20_contract(
            chain=self.chain, market=market
        )
        yield from fetch_events_logs_with_retry(
            f"Silo Finance LP-eUSDe {market.address}",
            contract_event=borrowable_token.events.Transfer(),
            from_block=from_block,
            to_block=to_block,
        )

    def process_transfer_event(
        self,
        *,
        transfer: Dict,
        bals: Dict[ChecksumAddress, float],
        supply_index: float,
    ):
        sender = transfer["args"]["from"]
        receiver = transfer["args"]["to"]
        value = transfer["args"]["value"]
        if is_null_address(sender):
            # Minting
            add_to_bals(
                bals=bals,
                address=receiver,
                value=value * supply_index,
                decimals=18,
            )
        elif is_null_address(receiver):
            # Burning
            subtract_from_bals(
                bals=bals, address=sender, value=value * supply_index, decimals=18
            )
        else:
            # Transfer
            # Convert to assets
            add_to_bals(
                bals=bals,
                address=sender,
                value=value * supply_index,
                decimals=18,
            )
            subtract_from_bals(
                bals=bals, address=receiver, value=value * supply_index, decimals=18
            )


ROUND_DECIMALS = 6


def is_null_address(address: ChecksumAddress) -> bool:
    return address == "0x0000000000000000000000000000000000000000"


def add_to_bals(
    bals: Dict[ChecksumAddress, float],
    address: ChecksumAddress,
    value: int,
    decimals: int,
):
    if address not in bals:
        bals[address] = 0
    bals[address] += round(value / 10**decimals, ROUND_DECIMALS)
    bals[address] = round(bals[address], ROUND_DECIMALS)


def subtract_from_bals(
    bals: Dict[ChecksumAddress, float],
    address: ChecksumAddress,
    value: int,
    decimals: int,
):
    if address not in bals:
        bals[address] = 0
    bals[address] -= round(value / 10**decimals, ROUND_DECIMALS)
    # If the balance is negative, set it to 0. happens because of rounding errors.
    if bals[address] < 0:
        bals[address] = 0
    bals[address] = round(bals[address], ROUND_DECIMALS)


def get_silo_contract(chain: Chain, market: SiloFinanceMarket) -> Contract:
    with open("abi/ISilo.json") as f:
        silo_abi = json.load(f)
        w3 = W3_BY_CHAIN[chain]["w3"]
        return w3.eth.contract(
            address=Web3.to_checksum_address(market.address), abi=silo_abi
        )


def get_borrowable_erc20_contract(chain: Chain, market: SiloFinanceMarket) -> Contract:
    with open("abi/ERC20_abi.json") as f:
        borrowable_token_abi = json.load(f)
        w3 = W3_BY_CHAIN[chain]["w3"]
        return w3.eth.contract(
            address=Web3.to_checksum_address(market.address),
            abi=borrowable_token_abi,
        )


def get_non_borrowable_erc20_contract(
    chain: Chain, market: SiloFinanceMarket
) -> Contract:
    with open("abi/ERC20_abi.json") as f:
        non_borrowable_token_abi = json.load(f)
        w3 = W3_BY_CHAIN[chain]["w3"]
        return w3.eth.contract(
            address=Web3.to_checksum_address(market.non_borrowable_token_address),
            abi=non_borrowable_token_abi,
        )
