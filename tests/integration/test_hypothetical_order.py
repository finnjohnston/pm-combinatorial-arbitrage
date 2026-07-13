import pytest
from book.public_book import PublicMarketBook
from execution.hypothetical_order import HypotheticalTakerOrder, HypotheticalOrderError


def make_buy_book(yes_ask: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.no_buys.set_real_qty(1000 - yes_ask, qty)
    return b


def make_sell_book(yes_bid: int, qty: int) -> PublicMarketBook:
    b = PublicMarketBook()
    b.yes_buys.set_real_qty(yes_bid, qty)
    return b


async def test_fills_correctly_no_erosion():
    b = make_buy_book(yes_ask=480, qty=100)
    order = HypotheticalTakerOrder(b, "buy", 100, participation_rate=1.0)
    result = order.resolve()
    assert result.filled_qty == 100
    assert result.unfilled_qty == 0
    assert len(result.fills) == 1
    assert result.fills[0].price == 480


async def test_sell_side_fills_correctly():
    b = make_sell_book(yes_bid=600, qty=50)
    order = HypotheticalTakerOrder(b, "sell", 50, participation_rate=1.0)
    result = order.resolve()
    assert result.filled_qty == 50
    assert result.fills[0].price == 600


async def test_erosion_reduces_fill():
    b = make_buy_book(yes_ask=480, qty=100)
    order = HypotheticalTakerOrder(b, "buy", 100, participation_rate=1.0)
    b.load_snapshot({}, {1000 - 480: 60})
    result = order.resolve()
    assert result.filled_qty == 60


async def test_full_erosion_gives_zero_fill():
    b = make_buy_book(yes_ask=480, qty=100)
    order = HypotheticalTakerOrder(b, "buy", 100, participation_rate=1.0)
    b.load_snapshot({}, {1000 - 480: 0})  # level fully gone
    result = order.resolve()
    assert result.filled_qty == 0
    assert result.unfilled_qty == 100


async def test_resolve_twice_raises():
    b = make_buy_book(yes_ask=480, qty=100)
    order = HypotheticalTakerOrder(b, "buy", 100)
    order.resolve()
    with pytest.raises(HypotheticalOrderError):
        order.resolve()


async def test_listener_removed_after_resolve():
    b = make_buy_book(yes_ask=480, qty=100)
    assert b.listener_count("buy") == 0
    order = HypotheticalTakerOrder(b, "buy", 100)
    assert b.listener_count("buy") == 1
    order.resolve()
    assert b.listener_count("buy") == 0


async def test_participation_cap_reduces_alloc():
    b = make_buy_book(yes_ask=480, qty=100)
    order = HypotheticalTakerOrder(b, "buy", 100, participation_rate=0.5)
    result = order.resolve()
    assert result.filled_qty == 50


async def test_partial_book_fills_partial():
    b = make_buy_book(yes_ask=480, qty=40)
    order = HypotheticalTakerOrder(b, "buy", 100, participation_rate=1.0)
    result = order.resolve()
    assert result.filled_qty == 40
    assert result.unfilled_qty == 60


async def test_no_listener_registered_when_alloc_empty():
    b = PublicMarketBook()
    order = HypotheticalTakerOrder(b, "buy", 100)
    assert b.listener_count("buy") == 0
    result = order.resolve()
    assert result.filled_qty == 0


def test_cancel_detaches_listener():
    from book.public_book import PublicMarketBook
    from execution.hypothetical_order import HypotheticalTakerOrder
    book = PublicMarketBook()
    book.no_buys.set_real_qty(600, 1000)
    order = HypotheticalTakerOrder(book, "buy", 100)
    assert book.listener_count("buy") == 1

    order.cancel()

    assert book.listener_count("buy") == 0


def test_cancel_is_idempotent():
    from book.public_book import PublicMarketBook
    from execution.hypothetical_order import HypotheticalTakerOrder
    book = PublicMarketBook()
    book.no_buys.set_real_qty(600, 1000)
    order = HypotheticalTakerOrder(book, "buy", 100)
    order.cancel()
    order.cancel()  # no error, listener not double-removed
    assert book.listener_count("buy") == 0


def test_resolve_after_cancel_raises():
    import pytest
    from book.public_book import PublicMarketBook
    from execution.hypothetical_order import HypotheticalOrderError, HypotheticalTakerOrder
    book = PublicMarketBook()
    book.no_buys.set_real_qty(600, 1000)
    order = HypotheticalTakerOrder(book, "buy", 100)
    order.cancel()
    with pytest.raises(HypotheticalOrderError):
        order.resolve()


def test_limit_price_caps_buy_fills():
    from book.public_book import PublicMarketBook
    from execution.hypothetical_order import HypotheticalTakerOrder
    book = PublicMarketBook()
    # YES asks at 480 (no_buys 520) and 900 (no_buys 100)
    book.no_buys.set_real_qty(520, 100)
    book.no_buys.set_real_qty(100, 1000)

    order = HypotheticalTakerOrder(book, "buy", 500, limit_price=480)
    result = order.resolve()

    assert result.filled_qty == 100  # only the 480 level
    assert all(f.price <= 480 for f in result.fills)


def test_limit_price_caps_sell_fills():
    from book.public_book import PublicMarketBook
    from execution.hypothetical_order import HypotheticalTakerOrder
    book = PublicMarketBook()
    book.yes_buys.set_real_qty(600, 100)  # bid 600
    book.yes_buys.set_real_qty(300, 1000)  # bid 300

    order = HypotheticalTakerOrder(book, "sell", 500, limit_price=600)
    result = order.resolve()

    assert result.filled_qty == 100  # refuses to sell at 300
    assert all(f.price >= 600 for f in result.fills)
