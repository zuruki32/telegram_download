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
import json
import os
import re
import sys
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat, User

try:
    from tqdm import tqdm
except ImportError:  # progress bars are optional
    tqdm = None


# File extensions we keep. Set from --ext in main; None means "everything".
# Default is ebook/document formats so photos, videos, stickers and audio
# are skipped.
ALLOWED_EXTS = {"pdf", "epub", "mobi", "azw3", "azw", "djvu", "fb2",
                "cbz", "cbr", "doc", "docx", "zip", "rar", "7z"}

# Archive types we can unpack after downloading (when extraction is on).
ARCHIVE_EXTS = {"zip", "rar", "7z"}
EXTRACT = True  # set from --no-extract in main

# Remembers which channels you chose, so you don't re-pick them every run.
SELECTION_FILE = Path(__file__).resolve().parent / "selected_channels.json"


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


def load_selection():
    """Return saved {channel_id: name} from a previous run."""
    if SELECTION_FILE.exists():
        try:
            data = json.loads(SELECTION_FILE.read_text(encoding="utf-8"))
            return {int(k): v for k, v in data.items()}
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_selection(mapping):
    """Persist {channel_id: name} so next run reuses it automatically."""
    try:
        SELECTION_FILE.write_text(
            json.dumps({str(k): v for k, v in mapping.items()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  (could not save channel selection: {e})")


def _within(base, target):
    """Guard against archive entries escaping the destination folder."""
    try:
        base = base.resolve()
        return base == target.resolve() or base in target.resolve().parents
    except Exception:  # noqa: BLE001
        return False


def extract_archive(path):
    """Unpack a downloaded archive into a folder named after it.

    The original archive is kept. Returns True if extraction succeeded.
    """
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    dest = path.with_suffix("")  # e.g. book.zip -> book/
    try:
        if ext == "zip":
            import zipfile
            if not zipfile.is_zipfile(path):
                return False
            dest.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path) as z:
                for member in z.namelist():
                    out = dest / member
                    if not _within(dest, out):
                        print(f"  skipped unsafe path in {path.name}: {member}")
                        continue
                    z.extract(member, dest)
            return True
        if ext == "7z":
            try:
                import py7zr
            except ImportError:
                print("  (.7z found; run 'pip install py7zr' to auto-extract)")
                return False
            dest.mkdir(parents=True, exist_ok=True)
            with py7zr.SevenZipFile(path, mode="r") as z:
                z.extractall(path=dest)
            return True
        if ext == "rar":
            try:
                import rarfile
            except ImportError:
                print("  (.rar found; run 'pip install rarfile' and install "
                      "WinRAR or 7-Zip to auto-extract)")
                return False
            dest.mkdir(parents=True, exist_ok=True)
            with rarfile.RarFile(path) as z:
                z.extractall(dest)
            return True
    except Exception as e:  # noqa: BLE001
        print(f"  could not extract {path.name}: {e}")
        try:  # remove an empty folder left behind by a failed extract
            if dest.exists() and not any(dest.iterdir()):
                dest.rmdir()
        except Exception:  # noqa: BLE001
            pass
    return False


async def list_dialogs(client):
    """Return the chats you've joined, newest-active first."""
    dialogs = []
    async for d in client.iter_dialogs():
        dialogs.append(d)
    return dialogs


async def pick_targets(client, all_flag, reset=False):
    dialogs = await list_dialogs(client)
    # Only things that can hold files worth bulk-downloading.
    choices = [d for d in dialogs if isinstance(d.entity, (Channel, Chat))]

    if not choices:
        sys.exit("No channels or groups found on this account.")

    if all_flag:
        return [d.entity for d in choices]

    by_id = {d.entity.id: d.entity for d in choices}
    # Previously chosen channels you're still a member of.
    saved = {} if reset else load_selection()
    saved_ids = set(saved) & set(by_id)

    print("\nChannels / groups you've joined "
          "(✓ = already saved, downloaded every run):\n")
    for i, d in enumerate(choices):
        mark = "✓" if d.entity.id in saved_ids else " "
        print(f"  {mark} [{i:>3}] {kind_label(d.entity):<8} {d.name}")

    if saved_ids:
        print(f"\n{len(saved_ids)} channel(s) already saved (marked ✓). "
              "Type numbers to ADD more, or just press Enter to keep them.")
        prompt = "Add channels (Enter = none): "
    else:
        print("\nEnter numbers separated by commas (e.g. 0,3,7), "
              "or 'all' for everything.")
        prompt = "Download from: "
    raw = input(prompt).strip().lower()

    added_ids = set()
    if raw == "all":
        added_ids = set(by_id)
    elif raw:
        for part in raw.replace(" ", "").split(","):
            if not part:
                continue
            if not part.isdigit() or int(part) >= len(choices):
                sys.exit(f"Invalid selection: {part}")
            added_ids.add(choices[int(part)].entity.id)

    final_ids = saved_ids | added_ids
    if not final_ids:
        sys.exit("Nothing selected.")

    # Remember the full set (id -> name) for next time.
    save_selection({cid: getattr(by_id[cid], "title", str(cid))
                    for cid in final_ids})

    new = added_ids - saved_ids
    if new:
        print(f"Added {len(new)} new channel(s). "
              f"Now watching {len(final_ids)} total.")
    elif saved_ids:
        print(f"Using {len(final_ids)} saved channel(s).")
    return [by_id[cid] for cid in final_ids]


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


def channel_folder(entity, out_root):
    """Per-channel output folder (created if missing)."""
    folder = out_root / safe_name(getattr(entity, "title", None), str(entity.id))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ---------------------------------------------------------------------------
# Resume ledger
# ---------------------------------------------------------------------------
# Each channel folder keeps a "_completed.txt" listing every message id
# already saved (with the file name, for your eyes). On restart we read it
# and skip what's done, so a stopped run never starts over.
LEDGER_NAME = "_completed.txt"


def load_ledger(channel_root):
    """Return (set_of_done_message_ids, path_to_ledger)."""
    path = channel_root / LEDGER_NAME
    done = set()
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                token = line.strip().split("\t", 1)[0].strip()
                if token.isdigit():
                    done.add(int(token))
        except Exception:  # noqa: BLE001 - a bad ledger shouldn't stop us
            pass
    return done, path


def record_done(done, ledger_path, message_id, name):
    """Mark a message as finished, in memory and in the ledger file."""
    if done is not None:
        done.add(message_id)
    if ledger_path is not None:
        try:
            with open(ledger_path, "a", encoding="utf-8") as f:
                f.write(f"{message_id}\t{name}\n")
        except Exception:  # noqa: BLE001
            pass


# Trailing "volume / part / number" markers to strip off a title, e.g.
# "Evil Love Appears 1-3", "Title (Vol 2)", "Name part 4".
_VOL_TAIL = re.compile(
    r"\s*(?:v(?:ol(?:ume)?)?|parts?|pts?|ch(?:apter)?s?|books?|bk"
    r"|episodes?|eps?)?\s*[\(\[]?\s*\d+(?:\s*[-–~]+\s*\d+)?\s*[\)\]]?\s*$",
    re.IGNORECASE)


def _clean_title(text):
    """Turn a caption line or filename into a tidy novel folder name.

    Skips divider/emoji lines, drops a trailing volume/part number, and
    returns "" if nothing book-like remains (so the file stays in the
    channel folder instead of a junk folder like "--").
    """
    if not text:
        return ""
    # Pick the first line that actually has words (>= 2 letters), so
    # decorative lines such as "----" or "➖➖➖" are ignored.
    line = ""
    for candidate in str(text).splitlines():
        if sum(ch.isalpha() for ch in candidate) >= 2:
            line = candidate
            break
    if not line:
        return ""
    s = re.sub(r"[_\.]+", " ", line)          # separators -> spaces
    s = re.sub(r"[\(\)\[\]]", " ", s)          # drop brackets
    for _ in range(3):                         # peel trailing "vol/part N"
        peeled = _VOL_TAIL.sub("", s).strip()
        if peeled == s or sum(ch.isalpha() for ch in peeled) < 2:
            break
        s = peeled
    s = re.sub(r"\s+", " ", s).strip(" -–_")
    name = safe_name(s, "")
    # safe_name may have turned emoji/symbols into "_"; tidy the edges.
    name = re.sub(r"\s{2,}", " ", name).strip(" _-.")
    return name if sum(ch.isalpha() for ch in name) >= 2 else ""


def novel_folder(caption, filename=""):
    """Subfolder name for a book: prefer the caption, else the filename."""
    return _clean_title(caption) or _clean_title(Path(filename or "").stem)


def message_ext(message):
    """Lower-case extension (no dot) of a message's file, or ''."""
    if message.file is None:
        return ""
    ext = message.file.ext or ""
    if not ext and message.file.name:
        ext = Path(message.file.name).suffix
    return ext.lower().lstrip(".")


def wanted(message):
    """True if this file's type is one we download (per --ext)."""
    if ALLOWED_EXTS is None:
        return True
    return message_ext(message) in ALLOWED_EXTS


async def download_one(client, message, folder, done=None, ledger_path=None):
    """Download a single message's media into folder.

    Returns 'downloaded', 'skipped', 'filtered', or 'failed'. Shared by the
    bulk backfill and the live listener so both behave identically. `done` /
    `ledger_path` are the channel-level resume ledger.
    """
    if not message.media:
        return "skipped"

    # Wrong file type (photo, video, ...) -> not what we're after.
    if not wanted(message):
        return "filtered"

    # Fast skip: the ledger already says this one is done.
    if done is not None and message.id in done:
        return "skipped"

    folder.mkdir(parents=True, exist_ok=True)
    dest = target_filename(message, folder)
    # Backup skip: the file is already on disk (ledger lost or first run).
    if dest.exists() and dest.stat().st_size > 0:
        record_done(done, ledger_path, message.id, dest.name)
        return "skipped"

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

    def finish():
        record_done(done, ledger_path, message.id, dest.name)
        if EXTRACT and message_ext(message) in ARCHIVE_EXTS:
            if extract_archive(dest):
                print(f"  extracted {dest.name}")
        return "downloaded"

    try:
        await client.download_media(message, file=str(dest),
                                    progress_callback=progress)
        return finish()
    except FloodWaitError as e:
        # Telegram asked us to slow down; honor it and retry once.
        if bar:
            bar.close()
            bar = None
        print(f"  rate-limited, waiting {e.seconds}s ...")
        await asyncio.sleep(e.seconds + 1)
        try:
            await client.download_media(message, file=str(dest))
            return finish()
        except Exception as e2:  # noqa: BLE001
            print(f"  failed msg {message.id}: {e2}")
            return "failed"
    except Exception as e:  # noqa: BLE001
        # Clean up any partial file so the next run retries it.
        if dest.exists() and dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
        print(f"  failed msg {message.id}: {e}")
        return "failed"
    finally:
        if bar:
            bar.close()


async def download_channel(client, entity, out_root, limit):
    root = channel_folder(entity, out_root)
    done, ledger_path = load_ledger(root)
    title = getattr(entity, "title", str(entity.id))
    print(f"\n=== {title} -> {root} ===")

    counts = {"downloaded": 0, "skipped": 0, "filtered": 0, "failed": 0}
    album = []          # messages of the album currently being collected
    album_gid = None    # grouped_id of that album

    async def flush():
        # Download the buffered album into one subfolder named after its
        # caption (the novel title). Standalone posts flush as a group of 1.
        if not album:
            return
        caption = ""
        fname = ""
        for m in album:
            text = (m.message or "").strip()
            if text and not caption:
                caption = text
            if m.file and m.file.name and not fname:
                fname = m.file.name
        sub = novel_folder(caption, fname)
        dest_folder = root / sub if sub else root
        for m in album:
            counts[await download_one(client, m, dest_folder,
                                      done, ledger_path)] += 1
        album.clear()

    async for message in client.iter_messages(entity, limit=limit):
        if not message.media:
            continue
        gid = getattr(message, "grouped_id", None)
        if gid is not None and gid == album_gid:
            album.append(message)       # same album, keep collecting
            continue
        await flush()                   # new album/post -> save the last one
        album_gid = gid
        album.append(message)
    await flush()

    print(f"  done: {counts['downloaded']} downloaded, "
          f"{counts['skipped']} already had, "
          f"{counts['filtered']} wrong type, {counts['failed']} failed")
    return counts["downloaded"], counts["skipped"], counts["failed"]


async def listen_mode(client, entities, out_root):
    """Stay connected and download new media as it's posted."""
    folders = {e.id: channel_folder(e, out_root) for e in entities}
    names = ", ".join(getattr(e, "title", str(e.id)) for e in entities)
    ledgers = {}  # chat_id -> (root, done_set, ledger_path)

    def ledger_for(chat_id, chat):
        if chat_id not in ledgers:
            root = folders.get(chat_id) or channel_folder(chat, out_root)
            done, path = load_ledger(root)
            ledgers[chat_id] = (root, done, path)
        return ledgers[chat_id]

    @client.on(events.Album(chats=entities))
    async def on_album(event):
        # A whole novel posted as one album -> its own subfolder.
        root, done, path = ledger_for(event.chat_id, event.chat)
        fname = next((m.file.name for m in event.messages
                      if m.file and m.file.name), "")
        sub = novel_folder(event.text, fname)
        dest_folder = root / sub if sub else root
        title = getattr(event.chat, "title", str(event.chat_id))
        for m in event.messages:
            if m.media and await download_one(client, m, dest_folder,
                                              done, path) == "downloaded":
                print(f"[{title}] saved -> {dest_folder}")

    @client.on(events.NewMessage(chats=entities))
    async def on_message(event):
        # Album parts are handled by on_album; skip them here.
        if event.message.grouped_id or not event.message.media:
            return
        root, done, path = ledger_for(event.chat_id, event.chat)
        fname = event.message.file.name if event.message.file else ""
        sub = novel_folder(event.message.message, fname)
        dest_folder = root / sub if sub else root
        title = getattr(event.chat, "title", str(event.chat_id))
        if await download_one(client, event.message, dest_folder,
                              done, path) == "downloaded":
            print(f"[{title}] saved -> {dest_folder}")

    print(f"\nListening for new files on: {names}")
    print("Leave this running. New files download automatically. Ctrl+C to stop.")
    await client.run_until_disconnected()


async def main():
    global ALLOWED_EXTS, EXTRACT
    parser = argparse.ArgumentParser(
        description="Download media files from Telegram channels you joined.")
    parser.add_argument("-o", "--out", default="downloads",
                        help="output directory (default: ./downloads)")
    parser.add_argument("--all", action="store_true",
                        help="download from every channel/group, no prompt")
    parser.add_argument("--limit", type=int, default=None,
                        help="max messages to scan per channel (default: all; "
                             "use 0 with --listen to skip old files)")
    parser.add_argument("--listen", action="store_true",
                        help="after grabbing history, keep running and "
                             "auto-download new files as they're posted")
    parser.add_argument("--ext", default=",".join(sorted(ALLOWED_EXTS)),
                        help="comma-separated file types to keep "
                             "(default: ebook/document formats). "
                             "e.g. --ext pdf,epub")
    parser.add_argument("--all-types", action="store_true",
                        help="download every file type, no filtering "
                             "(photos, videos, everything)")
    parser.add_argument("--no-extract", action="store_true",
                        help="don't auto-unpack downloaded zip/rar/7z archives")
    parser.add_argument("--reset-channels", action="store_true",
                        help="forget saved channels and choose fresh")
    parser.add_argument("--session", default="tg_downloader",
                        help="session name; keeps you logged in between runs")
    args = parser.parse_args()

    EXTRACT = not args.no_extract

    # Decide which file types to keep.
    if args.all_types:
        ALLOWED_EXTS = None
        print("Downloading ALL file types.")
    else:
        ALLOWED_EXTS = {e.strip().lower().lstrip(".")
                        for e in args.ext.split(",") if e.strip()}
        print(f"Keeping only: {', '.join(sorted(ALLOWED_EXTS))}")

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

    targets = await pick_targets(client, args.all, reset=args.reset_channels)

    # Backfill existing files first (unless --listen with --limit 0).
    if not (args.listen and args.limit == 0):
        totals = [0, 0, 0]
        for entity in targets:
            try:
                d, s, f = await download_channel(client, entity, out_root,
                                                 args.limit)
                totals[0] += d
                totals[1] += s
                totals[2] += f
            except Exception as e:  # noqa: BLE001
                print(f"  skipping channel ({e})")
        print(f"\nHistory done. {totals[0]} downloaded, {totals[1]} skipped, "
              f"{totals[2]} failed. Files are in: {out_root}")

    if args.listen:
        # Keep running; download new files as they arrive.
        await listen_mode(client, targets, out_root)
    else:
        await client.disconnect()
        print(f"All finished. Files are in: {out_root}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped. Re-run the same command to resume where you left off.")
