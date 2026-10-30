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

Put your key in the environment so you aren't asked each time:

```bash
export TG_API_ID=1234567
export TG_API_HASH=your_api_hash_here
```

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

## Good to know

- **Resumable.** Re-run the same command and it skips files already saved,
  so you can stop (Ctrl+C) and continue later.
- **Organized.** Each channel gets its own folder; files are prefixed with
  their message id so order is preserved and names never collide.
- **Rate limits.** Telegram throttles bulk downloads; the tool waits
  automatically when asked, so large channels just take a while.

## Keep this private

Your `api_id`, `api_hash`, and the `.session` file give full access to your
Telegram account. Don't commit them or share them.
