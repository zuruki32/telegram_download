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

## Listener mode (auto-download new files)

Keep the app running so it downloads new files the moment they're posted:

```bash
python download.py --listen           # grab history, then watch for new files
python download.py --listen --limit 0 # skip old files, only watch for new ones
```

Pick the channels when prompted, then leave the window open. Each new file
is saved automatically into that channel's folder. Press Ctrl+C to stop.

## Good to know

- **A folder per novel.** Inside each channel's folder, files are grouped by
  their caption (the novel/book title) into subfolders — albums posted
  together, or single files with a title, each get their own folder:
  `downloads/<channel>/<novel title>/...`. Files with no caption stay in the
  channel folder.
- **Resumable with a ledger.** Each channel folder has a `_completed.txt`
  listing every file already saved (message id + name). On restart the app
  reads it and skips what's done, so stopping (Ctrl+C) and re-running never
  starts over. It also double-checks the files on disk as a backup.
- **Organized naming.** Files are prefixed with their message id so order is
  preserved and names never collide.
- **Rate limits.** Telegram throttles bulk downloads; the tool waits
  automatically when asked, so large channels just take a while.

## Keep this private

Your `api_id`, `api_hash`, and the `.session` file give full access to your
Telegram account. Don't commit them or share them.
