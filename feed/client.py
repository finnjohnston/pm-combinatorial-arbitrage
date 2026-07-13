import asyncio
import json
import logging

import websockets

from .auth import make_headers

logger = logging.getLogger("feed")

_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
_SUBSCRIBE_BATCH_SIZE = 100   # tickers per subscribe command
_SUBSCRIBE_CMD_PAUSE_S = 0.05


class FeedClient:

    def __init__(self, key_id: str, private_key_path: str) -> None:
        self._key_id = key_id
        self._private_key_path = private_key_path
        self._ws = None
        self._msg_id = 0
        # sid -> (channel, currently subscribed tickers)
        self._subs: dict[int, tuple[str, set[str]]] = {}
        # msg_id -> (channel, tickers awaiting the "subscribed" ack)
        self._pending: dict[int, tuple[str, list[str]]] = {}
        # tickers unsubscribed before their subscribe ack arrived
        self._pending_unsubs: set[str] = set()

    async def connect(self) -> None:
        headers = make_headers(self._key_id, self._private_key_path)
        self._ws = await websockets.connect(_WS_URL, additional_headers=headers, ping_interval=20)
        self._subs.clear()
        self._pending.clear()
        self._pending_unsubs.clear()

    async def subscribe(
        self,
        tickers: list[str],
        channels: list[str] | None = None,
    ) -> None:
        if channels is None:
            channels = ["orderbook_delta", "market_lifecycle_v2"]
        tickers = list(tickers)
        first = True
        for channel in channels:
            for i in range(0, len(tickers), _SUBSCRIBE_BATCH_SIZE):
                batch = tickers[i:i + _SUBSCRIBE_BATCH_SIZE]
                if not first:
                    await asyncio.sleep(_SUBSCRIBE_CMD_PAUSE_S)
                first = False
                self._msg_id += 1
                self._pending[self._msg_id] = (channel, batch)
                await self._ws.send(json.dumps({
                    "id": self._msg_id,
                    "cmd": "subscribe",
                    "params": {
                        "channels": [channel],
                        "market_tickers": batch,
                    },
                }))

    async def record_subscribed(self, msg: dict) -> None:
        pending = self._pending.pop(msg.get("id"), None)
        if pending is None:
            return
        channel, tickers = pending
        sid = msg.get("msg", {}).get("sid")
        if sid is None:
            return
        group = set(tickers)
        self._subs[sid] = (channel, group)
        # an unsubscribe may have raced this ack; trim those tickers now
        late = group & self._pending_unsubs
        if late:
            self._pending_unsubs -= late
            await self._shrink_subscription(sid, late)

    def record_error(self, msg: dict) -> None:
        self._pending.pop(msg.get("id"), None)

    async def _shrink_subscription(self, sid: int, tickers: set[str]) -> None:
        channel, group = self._subs[sid]
        remaining = group - tickers
        self._msg_id += 1
        if remaining:
            await self._ws.send(json.dumps({
                "id": self._msg_id,
                "cmd": "update_subscription",
                "params": {
                    "sids": [sid],
                    "market_tickers": sorted(tickers),
                    "action": "delete_markets",
                },
            }))
            self._subs[sid] = (channel, remaining)
        else:
            await self._ws.send(json.dumps({
                "id": self._msg_id,
                "cmd": "unsubscribe",
                "params": {"sids": [sid]},
            }))
            del self._subs[sid]

    async def unsubscribe(
        self,
        tickers: list[str],
        channels: list[str] | None = None,
    ) -> None:
        target = set(tickers)
        for sid in list(self._subs):
            channel, group = self._subs[sid]
            if channels is not None and channel not in channels:
                continue
            overlap = group & target
            if overlap:
                await self._shrink_subscription(sid, overlap)
        # tickers whose subscribe ack hasn't arrived yet: trim when it does
        pending_tickers = {t for _, batch in self._pending.values() for t in batch}
        self._pending_unsubs |= target & pending_tickers

    async def receive(self) -> dict:
        raw = await self._ws.recv()
        return json.loads(raw)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
