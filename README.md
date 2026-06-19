# BadBoy Telegram Event Watcher

Minimal Python bot for watching BadBoy Lviv tasting/gastro-tour pages and posting updates to Telegram.

## What It Does

- Watches the BadBoy Lviv event page.
- Stores known events in `state.json`.
- Posts when a new event appears after the initial baseline.
- On the event date, posts a "today" reminder with remaining tickets, then removes that event from the active `seen_events` state.
- If the event has `0` tickets left on the event date, skips the reminder and removes it from the active `seen_events` state.
- Can post to one or more Telegram channels.

## Telegram Setup

1. Create a bot with `@BotFather`.
2. Add the token as a GitHub Actions secret named `TELEGRAM_BOT_TOKEN`.
3. Add the bot as an admin in each Lviv channel that should receive updates.
4. Add this GitHub Actions secret:

```text
TELEGRAM_TARGET_CHAT_ID_LVIV=@your_lviv_channel
```

For a public channel, the value can be `@your_channel_username`.
For a private channel, use the numeric chat id, usually starting with `-100`.
To send updates to multiple channels, separate them with commas:

```text
TELEGRAM_TARGET_CHAT_ID_LVIV=@badboy_lviv,@another_lviv_channel
```

There is also a backwards-compatible `TELEGRAM_TARGET_CHAT_ID` secret for a generic channel list, but `TELEGRAM_TARGET_CHAT_ID_LVIV` is preferred.

## Local Dry Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python badboy_bot.py
```

Without `TELEGRAM_BOT_TOKEN`, it will scrape and update `state.json` but only print messages it would send.

## GitHub Actions

The workflow in `.github/workflows/check-events.yml` runs on its configured schedule and commits `state.json` changes back to the repository.

The first run creates a baseline and does not spam all existing events unless you set:

```text
NOTIFY_ON_FIRST_RUN=true
```
