# Telegram channel file downloader

Downloads media files (PDFs, videos, photos, documents, etc.) from the
channels and groups **your own account has joined**, using your developer
API key from [my.telegram.org](https://my.telegram.org).

It works even on channels where the owner enabled **"Restrict saving
content."** That setting only hides the save button in the official apps —
the file data still has to reach your client to be displayed, and this tool
writes that data to disk. Files are for your own offline reading; don't
redistribute them.

## 1. Get your API key (one time)

1. Go to https://my.telegram.org and log in with your phone number.
2. Open **API development tools**.
3. Create an app (any name). Copy the **api_id** and **api_hash**.

## 2. Install

```bash
cd telegram-downloader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Run

Put your key in a `.env` file next to `download.py` (copy `.env.example`):

```
TG_API_ID=1234567
TG_API_HASH=your_api_hash_here
```

The app loads it automatically. `.env` is gitignored, so your key is never
committed. (You can also `export TG_API_ID` / `TG_API_HASH` instead; those
override the file.)

Then:

```bash
python download.py
```

The first run asks for:
- your **phone number**,
- the **login code** Telegram sends you,
- your **2FA password** (only if you set one).

After that you stay logged in (a `tg_downloader.session` file is created).

It then lists the channels/groups you joined and asks which to download from.

## Options

```bash
python download.py --all              # download from every channel, no prompt
python download.py -o ~/tg-archive    # choose where files go
python download.py --limit 500        # only scan the newest 500 messages per channel
```

## Download speed (parallel connections)

On some routes each single connection is speed-throttled (common for
Iran→Europe), so the default one-stream download is slow — and a proxy does
**not** fix this, because the same data still goes over the same throttled
link. The official Telegram app feels fast only because it uses many
connections at once.

This app does the same: it downloads each file over several parallel
connections and adds them up, beating the per-connection cap. It's on by
default (4 connections). Raise it for more speed:

```bash
python download.py --connections 8    # ~8x a single stream, if the link allows
python download.py --connections 1    # old single-stream behaviour
```

If the parallel path ever fails for a file, it automatically falls back to a
normal single-stream download, so downloads always still work. Each parallel
connection goes through the same proxy as the main client, and each chunk is
retried, so a slow handshake on a high-latency link doesn't abort a file.

On a slow proxy the server often drops idle connections and Telethon silently
reconnects — messages like `Server closed the connection: 0 bytes read...` are
that recovery, not lost data (watch the progress bar: it keeps going). These
are hidden by default; add `--verbose` to see them.

On a slow/throttled proxy link you can raise the tolerances:

```bash
python download.py --proxy socks5://127.0.0.1:10808 --connections 8 \
                   --timeout 60 --retries 15
```

## Using a proxy (no VPN)

If Telegram is blocked on your network, connect the app through a proxy
instead of a VPN. Pass it with `--proxy`, or put `TG_PROXY=...` in your `.env`.

**MTProto proxy** (easiest for Telegram — the `t.me/proxy` links people
share):

```bash
python download.py --proxy "https://t.me/proxy?server=HOST&port=PORT&secret=SECRET"
```

You can also write it as `mtproxy://HOST:PORT:SECRET`.

**SOCKS5 / HTTP proxy** (e.g. from a VPS you control):

```bash
python download.py --proxy socks5://127.0.0.1:1080
python download.py --proxy socks5://user:pass@HOST:PORT
python download.py --proxy http://HOST:PORT
```

Or in `.env`:

```
TG_PROXY=https://t.me/proxy?server=HOST&port=PORT&secret=SECRET
```

Notes:
- MTProto proxies need no extra setup. SOCKS/HTTP use the `PySocks`
  package (already in `requirements.txt`). Do **not** `pip install socks` —
  that's a different, empty package; the right one is `PySocks`.
- Get a working MTProto proxy from the proxy channels people share on
  Telegram, or run your own; the app can't provide one for you.

## File types

By default it downloads **only ebook/document files** and skips photos,
videos, stickers and audio. The default types are:
`pdf, epub, mobi, azw3, azw, djvu, fb2, cbz, cbr, doc, docx, zip, rar, 7z`.

```bash
python download.py --ext pdf,epub     # keep only these types
python download.py --all-types        # download everything, no filtering
```

Videos and photos are skipped by extension. But a large file of an *allowed*
type (e.g. a 200 MB comic pdf, or a video wrapped in a zip) still counts. To
cap size:

```bash
python download.py --max-size 50      # skip any file larger than 50 MB
```

## One-time verify (reconcile what you already downloaded)

If you downloaded files with older versions (folders may be named
differently now) or want to be sure nothing is missing or corrupt, run this
**once**:

```bash
python download.py --verify
```

It finds every file already on disk **by its filename** (the message-id
prefix), no matter which folder it's in — so your old folders count and
aren't re-downloaded. It checks each file is present and not broken (valid
PDF/zip/epub), re-downloads only the missing or corrupt ones, moves any file
left loose in the channel folder (from older versions) into its own folder,
and rebuilds each channel's `_completed.txt` to match what's really there.

You only do this once. Normal runs don't re-verify everything — new files
are checked as they download (they're written to a temporary name and only
marked complete once fully saved), so the completed list stays trustworthy.

### Just fix the log (no downloading)

If you only want to tidy the `_completed.txt` logs — not re-download missing
files — use `--relog`. It's instant and offline (no Telegram login): it
rebuilds every log from the files on disk, moves any file left loose in a
channel folder into its own folder (named from the filename), removes
duplicate and `.part` entries, and sorts it.

```bash
python download.py --relog
```

## Duplicate detection

Two independent checks, both on by default and scoped **per channel/group**
(the same book can still exist in different channels — you compare those
yourself):

**1. Exact duplicates (everywhere).** Within one channel's folder, a file is
skipped only when another file there has **both the same byte size and the
same name**. Same size but a different name is kept, so unrelated books that
happen to share a size aren't lost.

```bash
python download.py --no-dedup          # turn exact-duplicate skipping off
```

**2. PDF-over-EPUB (groups only).** In a **group**, if a book exists as a
**PDF**, its **EPUB** is skipped — matched by base title with the trailing
volume/part number removed, so a single `Novel.pdf` blocks `Novel 1.epub`,
`Novel 2.epub`, … The PDF is always kept, and extra EPUB parts are kept only
when there's no PDF. This does **not** apply to channels.

```bash
python download.py --no-format-dedup   # keep both PDF and EPUB in groups
```

Every skip is written to `downloads/_skipped.txt` (id, channel, reason, name),
so you can review and pull anything back with `--force-ids`.

### Reviewing skips

Every file skipped as a duplicate is written to `downloads/_skipped.txt`
(rebuilt each run) with its message id, channel, reason, and filename. Look
through it, and if something was skipped by mistake, download it by its id:

```bash
python download.py --force-ids 200,214,377
```

Those ids download even if dedup would skip them.

## Remembered channels

The first time you pick channels, your choice is saved to
`selected_channels.json` (gitignored). On later runs those channels are
marked with a ✓ and downloaded automatically — you don't re-pick them. The
full list still shows every channel, so you can type numbers to **add** newly
joined ones, or `-number` to **remove** a saved channel (e.g. `4,-12` adds 4
and drops 12). Just press Enter to keep the saved set as-is.

```bash
python download.py --reset-channels   # forget the saved list and choose fresh
```

## Auto-extracting archives

Downloaded `.zip`, `.rar`, and `.7z` files are unpacked automatically into a
folder next to the archive, so books come out ready to read. The original
archive is kept.

- `.zip` works out of the box.
- `.7z` uses the `py7zr` package (in requirements.txt).
- `.rar` uses the `rarfile` package **plus** WinRAR or 7-Zip installed on your
  system. If those aren't present, the `.rar` is downloaded but left for you
  to open manually.

```bash
python download.py --no-extract         # keep archives packed
python download.py --delete-archives    # delete each archive after it unpacks
```

## Listener mode (auto-download new files)

Keep the app running so it downloads new files the moment they're posted:

```bash
python download.py --listen           # grab history, then watch for new files
python download.py --listen --limit 0 # skip old files, only watch for new ones
```

Pick the channels when prompted, then leave the window open. Each new file
is saved automatically into that channel's folder. Press Ctrl+C to stop.

## Good to know

- **A folder per file.** Inside each channel's folder, every file goes in its
  own subfolder named after the file (the book title), e.g.
  `downloads/<channel>/Evil Love Appears 1 3/...`. If a file has no name, the
  caption is used, and failing that the message id — so nothing is ever left
  loose in the channel folder.
- **Resumable, by filename.** Before downloading, the app scans the channel
  folder and recognises what's already there by each file's id prefix
  (`000000952_...`) — no matter which subfolder it's in or what the log says.
  So nothing re-downloads needlessly, old folder layouts still count, and
  half-finished `.part` files never count as done. `_completed.txt` is just a
  human-readable record of what was saved.
- **Safe partial downloads.** Files download to a temporary `.part` name and
  are renamed only when complete, so a dropped connection never leaves a
  half-file that looks finished. The app also retries through network
  hiccups.
- **Organized naming.** Files are prefixed with their message id so order is
  preserved and names never collide.
- **Rate limits.** Telegram throttles bulk downloads; the tool waits
  automatically when asked, so large channels just take a while.

## Keep this private

Your `api_id`, `api_hash`, and the `.session` file give full access to your
Telegram account. Don't commit them or share them.
