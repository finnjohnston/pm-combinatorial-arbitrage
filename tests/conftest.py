import random
import pytest
from book.public_book import PublicMarketBook
from execution.config import ExecutionConfig


@pytest.fixture
def book():
    return PublicMarketBook()


@pytest.fixture
def config():
    return ExecutionConfig(min_latency_ms=0, max_latency_ms=0, participation_rate=1.0)


@pytest.fixture
def seeded_rng():
    return random.Random(42)


def make_book(yes_bids: dict[int, int], no_bids: dict[int, int]) -> PublicMarketBook:
    b = PublicMarketBook()
    for price, qty in yes_bids.items():
        b.yes_buys.set_real_qty(price, qty)
    for price, qty in no_bids.items():
        b.no_buys.set_real_qty(price, qty)
    return b
