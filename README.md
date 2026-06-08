# BadBoy Telegram Event Watcher

Minimal Python bot for watching BadBoy tasting/gastro-tour pages and posting updates to Telegram.

## What It Does

- Watches BadBoy event pages for `kyiv`, `lviv`, and/or `dnipro`.
- Stores known events in `state.json`.
- Posts when a new event appears after the initial baseline.
- On the event date, posts a "today" reminder with remaining tickets, then removes that event from the active `seen_events` state.
- Lets Telegram chats subscribe with commands when the scheduled job runs.
- Can post to one separate Telegram channel per city.

## Telegram Setup

1. Create a bot with `@BotFather`.
2. Add the token as a GitHub Actions secret named `TELEGRAM_BOT_TOKEN`.
3. For channel posting, create one channel per city and add the bot as an admin in each channel.
4. Add one GitHub Actions secret per channel:

```text
TELEGRAM_TARGET_CHAT_ID_LVIV=@your_lviv_channel
TELEGRAM_TARGET_CHAT_ID_KYIV=@your_kyiv_channel
TELEGRAM_TARGET_CHAT_ID_DNIPRO=@your_dnipro_channel
```

For a public channel, the value can be `@your_channel_username`.
For a private channel, use the numeric chat id, usually starting with `-100`.

There is also a backwards-compatible `TELEGRAM_TARGET_CHAT_ID` secret for one generic channel. Prefer the per-city secrets for this setup.

## City Selection

For per-city channels, set a GitHub Actions repository variable:

```text
FOLLOW_CITIES=lviv,kyiv,dnipro
```

If unset, the workflow defaults to all three cities.

For a direct bot chat, send one of these commands to the bot:

```text
/follow lviv
/follow kyiv
/follow dnipro
/followall
/unfollow lviv
/cities
```

Because this runs on GitHub Actions cron, bot commands are picked up on the next scheduled run, not instantly.

## Local Dry Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python badboy_bot.py
```

Without `TELEGRAM_BOT_TOKEN`, it will scrape and update `state.json` but only print messages it would send.

## GitHub Actions

The workflow in `.github/workflows/check-events.yml` runs every 30 minutes and commits `state.json` changes back to the repository.

The first run creates a baseline and does not spam all existing events unless you set:

```text
NOTIFY_ON_FIRST_RUN=true
```
