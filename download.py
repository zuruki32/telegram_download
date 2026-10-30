#!/usr/bin/env python3
"""
Telegram channel file downloader.

Downloads media files from the channels/groups your account has joined,
using YOUR OWN developer API credentials (api_id + api_hash from
https://my.telegram.org -> API development tools).

It talks to Telegram over the raw MTProto API via Telethon, so it also
works on channels where the owner enabled "Restrict saving content":
that flag only hides the save button in the official apps, but the file
bytes are still delivered to any client in order to display them, and we
write those bytes to disk.

Only archive content you are allowed to view and keep. Don't redistribute
copyrighted material.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User

try:
    from tqdm import tqdm
except ImportError:  # progress bars are optional
    tqdm = None


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def load_env_file():
    """Load KEY=VALUE pairs from a local .env file next to this script.

    Keeps your api_id/api_hash out of the shell and out of git (.env is
    gitignored). Existing environment variables win, so you can still
    override with `export` if you want.
    """
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# Read from .env / environment first, fall back to prompting. Get these from
# https://my.telegram.org -> "API development tools".
def get_credentials():
    load_env_file()
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id:
        api_id = input("api_id (from my.telegram.org): ").strip()
    if not api_hash:
        api_hash = input("api_hash (from my.telegram.org): ").strip()
    try:
        api_id = int(api_id)
    except ValueError:
        sys.exit("api_id must be a number.")
    return api_id, api_hash


def kind_label(entity):
    if isinstance(entity, Channel):
        return "channel" if entity.broadcast else "group"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, User):
        return "user"
    return "chat"


def safe_name(text, fallback):
    """Make a string safe to use as a folder/file name."""
    text = (text or "").strip()
    if not text:
        return fallback
    keep = "".join(c if c.isalnum() or c in " ._-" else "_" for c in text)
    keep = keep.strip(". ")
    return keep[:120] or fallback


async def list_dialogs(client):
    """Return the chats you've joined, newest-active first."""
    dialogs = []
    async for d in client.iter_dialogs():
        dialogs.append(d)
    return dialogs


async def pick_targets(client, all_flag):
    dialogs = await list_dialogs(client)
    # Only things that can hold files worth bulk-downloading.
    choices = [d for d in dialogs if isinstance(d.entity, (Channel, Chat))]

    if not choices:
        sys.exit("No channels or groups found on this account.")

    if all_flag:
        return [d.entity for d in choices]

    print("\nChannels / groups you've joined:\n")
    for i, d in enumerate(choices):
        e = d.entity
        print(f"  [{i:>3}] {kind_label(e):<8} {d.name}")
    print("\nEnter numbers separated by commas (e.g. 0,3,7), "
          "or 'all' for everything.")
    raw = input("Download from: ").strip().lower()

    if raw == "all":
        return [d.entity for d in choices]

    picked = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit() or int(part) >= len(choices):
            sys.exit(f"Invalid selection: {part}")
        picked.append(choices[int(part)].entity)
    if not picked:
        sys.exit("Nothing selected.")
    return picked


def target_filename(message, folder):
    """Build a stable filename so re-runs can skip what's already done."""
    # Prefer the original name if the sender attached one.
    name = None
    if message.file is not None:
        name = message.file.name
    ext = (message.file.ext if message.file else "") or ""
    if name:
        base = safe_name(Path(name).stem, f"msg{message.id}")
        suffix = Path(name).suffix or ext
    else:
        base = f"msg{message.id}"
        suffix = ext
    # Prefix with message id to guarantee uniqueness and ordering.
    return folder / f"{message.id:>09}_{base}{suffix}"


async def download_channel(client, entity, out_root, limit):
    folder = out_root / safe_name(getattr(entity, "title", None), str(entity.id))
    folder.mkdir(parents=True, exist_ok=True)

    title = getattr(entity, "title", str(entity.id))
    print(f"\n=== {title} -> {folder} ===")

    downloaded = skipped = failed = 0
    async for message in client.iter_messages(entity, limit=limit):
        if not message.media:
            continue

        dest = target_filename(message, folder)
        # Resume support: skip files already pulled (non-empty).
        if dest.exists() and dest.stat().st_size > 0:
            skipped += 1
            continue

        bar = None
        if tqdm is not None and message.file and message.file.size:
            bar = tqdm(
                total=message.file.size,
                unit="B",
                unit_scale=True,
                desc=dest.name[:40],
                leave=False,
            )

        def progress(received, total, _bar=bar):
            if _bar is not None:
                _bar.update(received - _bar.n)

        try:
            await client.download_media(message, file=str(dest),
                                        progress_callback=progress)
            downloaded += 1
        except FloodWaitError as e:
            # Telegram asked us to slow down; honor it and retry once.
            if bar:
                bar.close()
            print(f"  rate-limited, waiting {e.seconds}s ...")
            await asyncio.sleep(e.seconds + 1)
            try:
                await client.download_media(message, file=str(dest))
                downloaded += 1
            except Exception as e2:  # noqa: BLE001
                failed += 1
                print(f"  failed msg {message.id}: {e2}")
            continue
        except Exception as e:  # noqa: BLE001
            failed += 1
            # Clean up any partial file so the next run retries it.
            if dest.exists() and dest.stat().st_size == 0:
                dest.unlink(missing_ok=True)
            print(f"  failed msg {message.id}: {e}")
        finally:
            if bar:
                bar.close()

    print(f"  done: {downloaded} downloaded, {skipped} already had, "
          f"{failed} failed")
    return downloaded, skipped, failed


async def main():
    parser = argparse.ArgumentParser(
        description="Download media files from Telegram channels you joined.")
    parser.add_argument("-o", "--out", default="downloads",
                        help="output directory (default: ./downloads)")
    parser.add_argument("--all", action="store_true",
                        help="download from every channel/group, no prompt")
    parser.add_argument("--limit", type=int, default=None,
                        help="max messages to scan per channel (default: all)")
    parser.add_argument("--session", default="tg_downloader",
                        help="session name; keeps you logged in between runs")
    args = parser.parse_args()

    api_id, api_hash = get_credentials()
    out_root = Path(args.out).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    # flood_sleep_threshold: auto-wait through short rate limits silently.
    client = TelegramClient(args.session, api_id, api_hash,
                            flood_sleep_threshold=60)
    # .start() handles the interactive login: phone -> SMS/app code ->
    # 2FA password if you have one. The session file remembers you after.
    await client.start()

    me = await client.get_me()
    print(f"Logged in as {me.first_name} "
          f"(@{me.username})" if me.username else f"Logged in as {me.first_name}")

    targets = await pick_targets(client, args.all)

    totals = [0, 0, 0]
    for entity in targets:
        try:
            d, s, f = await download_channel(client, entity, out_root, args.limit)
            totals[0] += d
            totals[1] += s
            totals[2] += f
        except Exception as e:  # noqa: BLE001
            print(f"  skipping channel ({e})")

    await client.disconnect()
    print(f"\nAll finished. {totals[0]} downloaded, {totals[1]} skipped, "
          f"{totals[2]} failed. Files are in: {out_root}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped. Re-run the same command to resume where you left off.")
