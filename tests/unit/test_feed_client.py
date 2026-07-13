import asyncio
import json
from unittest.mock import AsyncMock, call, patch

import pytest

from feed.client import (
    FeedClient,
    _SUBSCRIBE_BATCH_SIZE,
    _SUBSCRIBE_CMD_PAUSE_S,
)


def make_client() -> FeedClient:
    c = FeedClient(key_id="test", private_key_path="test.pem")
    c._ws = AsyncMock()
    c._ws.send = AsyncMock()
    return c


def sent_payloads(client) -> list[dict]:
    return [json.loads(c.args[0]) for c in client._ws.send.call_args_list]


async def ack(client, msg_id: int, sid: int, channel: str = "orderbook_delta") -> None:
    await client.record_subscribed(
        {"id": msg_id, "type": "subscribed", "msg": {"channel": channel, "sid": sid}}
    )


# subscribe: batched market_tickers commands

async def test_subscribe_sends_one_command_per_channel_for_small_batch():
    client = make_client()
    await client.subscribe(["A", "B"])
    assert client._ws.send.call_count == 2  # one per channel, both tickers batched


async def test_subscribe_message_format():
    client = make_client()
    await client.subscribe(["MKTX"])
    payloads = sent_payloads(client)
    channels = [p["params"]["channels"][0] for p in payloads]
    assert "orderbook_delta" in channels
    assert "market_lifecycle_v2" in channels
    assert all(p["params"]["market_tickers"] == ["MKTX"] for p in payloads)
    assert all(p["cmd"] == "subscribe" for p in payloads)


async def test_subscribe_ids_are_unique_and_incrementing():
    client = make_client()
    await client.subscribe([f"MKT-{i}" for i in range(5)])
    ids = [p["id"] for p in sent_payloads(client)]
    assert ids == list(range(1, len(ids) + 1))


async def test_subscribe_batches_large_ticker_lists():
    client = make_client()
    tickers = [f"MKT-{i}" for i in range(_SUBSCRIBE_BATCH_SIZE + 1)]
    await client.subscribe(tickers, channels=["orderbook_delta"])
    payloads = sent_payloads(client)
    assert len(payloads) == 2
    assert len(payloads[0]["params"]["market_tickers"]) == _SUBSCRIBE_BATCH_SIZE
    assert len(payloads[1]["params"]["market_tickers"]) == 1


async def test_subscribe_pauses_between_commands():
    client = make_client()
    tickers = [f"MKT-{i}" for i in range(_SUBSCRIBE_BATCH_SIZE + 1)]
    with patch("feed.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await client.subscribe(tickers, channels=["orderbook_delta"])
    pauses = [c for c in mock_sleep.call_args_list if c == call(_SUBSCRIBE_CMD_PAUSE_S)]
    assert len(pauses) == 1  # 2 commands → 1 pause


async def test_subscribe_no_pause_for_single_command():
    client = make_client()
    with patch("feed.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await client.subscribe(["A", "B"], channels=["orderbook_delta"])
    mock_sleep.assert_not_called()


async def test_subscribe_empty_list_sends_nothing():
    client = make_client()
    await client.subscribe([])
    client._ws.send.assert_not_called()


async def test_subscribe_channels_param_controls_channel_name():
    client = make_client()
    await client.subscribe(["MKTX"], channels=["orderbook_delta"])
    payloads = sent_payloads(client)
    assert len(payloads) == 1
    assert payloads[0]["params"]["channels"] == ["orderbook_delta"]


# sid tracking + unsubscribe / update_subscription

async def test_full_group_unsubscribe_sends_sid():
    client = make_client()
    await client.subscribe(["MKTX"], channels=["orderbook_delta"])
    await ack(client, 1, sid=42)

    client._ws.send.reset_mock()
    await client.unsubscribe(["MKTX"])

    payloads = sent_payloads(client)
    assert len(payloads) == 1
    assert payloads[0]["cmd"] == "unsubscribe"
    assert payloads[0]["params"]["sids"] == [42]
    assert client._subs == {}


async def test_partial_group_unsubscribe_uses_update_subscription():
    client = make_client()
    await client.subscribe(["A", "B", "C"], channels=["orderbook_delta"])
    await ack(client, 1, sid=7)

    client._ws.send.reset_mock()
    await client.unsubscribe(["B"])

    payloads = sent_payloads(client)
    assert len(payloads) == 1
    assert payloads[0]["cmd"] == "update_subscription"
    assert payloads[0]["params"]["sids"] == [7]
    assert payloads[0]["params"]["market_tickers"] == ["B"]
    assert payloads[0]["params"]["action"] == "delete_markets"
    assert client._subs[7][1] == {"A", "C"}


async def test_unsubscribe_without_known_sid_sends_nothing():
    client = make_client()
    await client.unsubscribe(["UNKNOWN"])
    client._ws.send.assert_not_called()


async def test_unsubscribe_spanning_multiple_groups():
    client = make_client()
    await client.subscribe(["A"], channels=["orderbook_delta"])
    await client.subscribe(["A"], channels=["market_lifecycle_v2"])
    await ack(client, 1, sid=1, channel="orderbook_delta")
    await ack(client, 2, sid=2, channel="market_lifecycle_v2")

    client._ws.send.reset_mock()
    await client.unsubscribe(["A"])

    payloads = sent_payloads(client)
    assert len(payloads) == 2
    assert sorted(p["params"]["sids"][0] for p in payloads) == [1, 2]
    assert all(p["cmd"] == "unsubscribe" for p in payloads)


async def test_unsubscribe_forgets_sid_after_use():
    client = make_client()
    await client.subscribe(["MKTX"], channels=["orderbook_delta"])
    await ack(client, 1, sid=42)
    await client.unsubscribe(["MKTX"])

    client._ws.send.reset_mock()
    await client.unsubscribe(["MKTX"])
    client._ws.send.assert_not_called()


async def test_record_subscribed_unknown_id_is_ignored():
    client = make_client()
    await client.record_subscribed({"id": 999, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 7}})
    await client.unsubscribe(["ANY"])
    client._ws.send.assert_not_called()


# pending bookkeeping: error acks + unsubscribe racing the subscribe ack

async def test_error_ack_clears_pending():
    client = make_client()
    await client.subscribe(["MKTX"], channels=["orderbook_delta"])
    assert client._pending

    client.record_error({"id": 1, "type": "error", "msg": {"code": 6, "msg": "bad"}})

    assert client._pending == {}


async def test_unsubscribe_before_ack_trims_group_when_ack_arrives():
    client = make_client()
    await client.subscribe(["A", "B"], channels=["orderbook_delta"])

    # unsubscribe A before the subscribe ack arrives
    await client.unsubscribe(["A"])
    assert "A" in client._pending_unsubs

    client._ws.send.reset_mock()
    await ack(client, 1, sid=9)

    payloads = sent_payloads(client)
    assert len(payloads) == 1
    assert payloads[0]["cmd"] == "update_subscription"
    assert payloads[0]["params"]["market_tickers"] == ["A"]
    assert client._subs[9][1] == {"B"}
    assert client._pending_unsubs == set()


async def test_unsubscribe_before_ack_of_whole_group_unsubscribes_on_ack():
    client = make_client()
    await client.subscribe(["A"], channels=["orderbook_delta"])
    await client.unsubscribe(["A"])

    client._ws.send.reset_mock()
    await ack(client, 1, sid=9)

    payloads = sent_payloads(client)
    assert len(payloads) == 1
    assert payloads[0]["cmd"] == "unsubscribe"
    assert payloads[0]["params"]["sids"] == [9]
    assert client._subs == {}


async def test_connect_clears_state():
    client = FeedClient(key_id="test", private_key_path="test.pem")
    client._subs[5] = ("orderbook_delta", {"MKTX"})
    client._pending[3] = ("orderbook_delta", ["MKTY"])
    client._pending_unsubs.add("MKTZ")

    with patch("feed.client.websockets.connect", new=AsyncMock(return_value=AsyncMock())), \
         patch("feed.client.make_headers", return_value={}):
        await client.connect()

    assert client._subs == {}
    assert client._pending == {}
    assert client._pending_unsubs == set()
