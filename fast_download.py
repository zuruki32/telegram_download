"""
Parallel (multi-connection) file downloading for Telethon.

Telethon's default download uses a single connection, so on a route with a
per-connection speed throttle it can't go faster than that one stream. This
opens several connections to the file's data center and downloads different
chunks over each at once, then writes them back in order -- aggregating past
the per-connection limit, the same way the official Telegram app does.

Based on the well-known "FastTelethon" approach. It uses a few Telethon
internals; if anything goes wrong the caller falls back to the normal
single-stream download, so downloads always still work.
"""

import asyncio
import inspect
import math

from telethon import utils
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import (ExportAuthorizationRequest,
                                         ImportAuthorizationRequest)
from telethon.tl.functions.upload import GetFileRequest


class FastDownloader:
    """Reusable pool of parallel download connections, per data center."""

    def __init__(self, client, connections=4):
        self.client = client
        self.connections = max(1, int(connections))
        self._senders = {}     # dc_id -> [MTProtoSender, ...]
        self._auth_keys = {}   # dc_id -> AuthKey (for non-home DCs)
        self._lock = asyncio.Lock()

    async def _make_sender(self, dc_id):
        client = self.client
        dc = await client._get_dc(dc_id)
        if dc_id == client.session.dc_id:
            auth = client.session.auth_key          # home DC: reuse main key
        else:
            auth = self._auth_keys.get(dc_id)        # other DC: export once
        sender = MTProtoSender(auth, loggers=client._log)
        await sender.connect(client._connection(
            dc.ip_address, dc.port, dc.id,
            loggers=client._log, proxy=client._proxy))
        if auth is None:
            exported = await client(ExportAuthorizationRequest(dc_id))
            init = client._init_request
            init.query = ImportAuthorizationRequest(id=exported.id,
                                                    bytes=exported.bytes)
            await sender.send(InvokeWithLayerRequest(LAYER, init))
            self._auth_keys[dc_id] = sender.auth_key
        return sender

    async def _dc_senders(self, dc_id):
        async with self._lock:
            if dc_id not in self._senders:
                self._senders[dc_id] = [await self._make_sender(dc_id)
                                        for _ in range(self.connections)]
            return self._senders[dc_id]

    async def _drop(self, dc_id):
        senders = self._senders.pop(dc_id, [])
        for s in senders:
            try:
                await s.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def download(self, location, size, out, progress=None):
        """Download `location` (a Document) of `size` bytes into file `out`.

        Raises on failure (caller should fall back to normal download).
        """
        dc_id, input_loc = utils.get_input_location(location)
        senders = await self._dc_senders(dc_id)
        n = len(senders)
        part_size = utils.get_appropriated_part_size(size) * 1024
        part_count = math.ceil(size / part_size)
        stride = n * part_size

        reqs = [GetFileRequest(input_loc, offset=i * part_size, limit=part_size)
                for i in range(n)]
        downloaded = 0
        done = 0
        try:
            while done < part_count:
                # Fire one request per connection at once, collect in order.
                batch = []
                for i in range(n):
                    if done + i < part_count:
                        batch.append(
                            (i, asyncio.ensure_future(
                                self.client._call(senders[i], reqs[i]))))
                for i, fut in batch:
                    result = await fut
                    data = result.bytes
                    if data:
                        out.write(data)
                        downloaded += len(data)
                        if progress is not None:
                            r = progress(min(downloaded, size), size)
                            if inspect.isawaitable(r):
                                await r
                    reqs[i].offset += stride
                done += len(batch)
        except Exception:
            # A pooled connection may have died; rebuild it next time.
            await self._drop(dc_id)
            raise
        return downloaded

    async def close(self):
        for dc_id in list(self._senders):
            await self._drop(dc_id)
