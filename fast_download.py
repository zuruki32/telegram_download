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

    def __init__(self, client, connections=4, retries=2, retry_delay=2):
        self.client = client
        self.connections = max(1, int(connections))
        # Telethon's _call already retries internally, so keep this small:
        # a couple of tries, then let the caller fall back to single stream.
        self.retries = max(1, int(retries))
        self.retry_delay = retry_delay
        self._senders = {}     # dc_id -> [MTProtoSender, ...]
        self._auth_keys = {}   # dc_id -> AuthKey (for non-home DCs)
        self._lock = asyncio.Lock()

    async def _call_retry(self, sender, request):
        """One chunk request, retried through slow-link timeouts/hiccups."""
        last = None
        for attempt in range(self.retries):
            try:
                return await self.client._call(sender, request)
            except Exception as e:  # noqa: BLE001 - WinError 121, timeouts, etc.
                last = e
                await asyncio.sleep(self.retry_delay)
        raise last

    async def _make_sender(self, dc_id):
        """Create one connected sender, retrying the connect a few times.

        All senders connect through the client's proxy (proxy=client._proxy),
        so with Tun off only this process uses the tunnel.
        """
        client = self.client
        dc = await client._get_dc(dc_id)
        home = dc_id == client.session.dc_id
        last = None
        for attempt in range(3):
            auth = client.session.auth_key if home else self._auth_keys.get(dc_id)
            sender = MTProtoSender(auth, loggers=client._log)
            try:
                await sender.connect(client._connection(
                    dc.ip_address, dc.port, dc.id,
                    loggers=client._log, proxy=client._proxy))
                if not home and auth is None:
                    exported = await client(ExportAuthorizationRequest(dc_id))
                    init = client._init_request
                    init.query = ImportAuthorizationRequest(
                        id=exported.id, bytes=exported.bytes)
                    await sender.send(InvokeWithLayerRequest(LAYER, init))
                    self._auth_keys[dc_id] = sender.auth_key
                return sender
            except Exception as e:  # noqa: BLE001
                last = e
                try:
                    await sender.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(1 + attempt)
        raise last

    async def _dc_senders(self, dc_id):
        async with self._lock:
            if dc_id not in self._senders:
                senders = []
                for i in range(self.connections):
                    if i:
                        await asyncio.sleep(0.15)   # stagger, easier on the link
                    senders.append(await self._make_sender(dc_id))
                self._senders[dc_id] = senders
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
        batch = []
        failed = False
        try:
            while done < part_count:
                # Fire one request per connection at once, collect in order.
                batch = [(i, asyncio.ensure_future(
                            self._call_retry(senders[i], reqs[i])))
                         for i in range(n) if done + i < part_count]
                for i, fut in batch:
                    result = await fut
                    # CDN-redirected files return upload.fileCdnRedirect, not
                    # file bytes; the parallel path can't follow it, so bail to
                    # the native downloader with a clear reason.
                    data = getattr(result, "bytes", None)
                    if data is None:
                        raise TypeError(
                            f"CDN redirect / unsupported file "
                            f"({type(result).__name__})")
                    if data:
                        out.write(data)
                        downloaded += len(data)
                        if progress is not None:
                            r = progress(min(downloaded, size), size)
                            if inspect.isawaitable(r):
                                await r
                    reqs[i].offset += stride
                done += len(batch)
                batch = []
            return downloaded
        except BaseException:   # includes CancelledError from the watchdog
            failed = True
            raise
        finally:
            # Always drain any still-pending chunk tasks (covers both errors
            # AND cancellation), so none are left as "Task exception never
            # retrieved". Drop the pool only when something went wrong.
            for _, fut in batch:
                if not fut.done():
                    fut.cancel()
            if batch:
                await asyncio.gather(*(f for _, f in batch),
                                     return_exceptions=True)
            if failed:
                await self._drop(dc_id)

    async def close(self):
        for dc_id in list(self._senders):
            await self._drop(dc_id)
