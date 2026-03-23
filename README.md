# Telegram Relay Bot (Telethon + Docker)

A two-stage Telegram relay system:

- `telegram_bot.py` (userbot): logs in as your Telegram user account, listens to source chats, and forwards/copies messages to a relay bot.
- `bot_relay.py` (relaybot): logs in as a Telegram bot account and reposts messages into destination channels and forum topics.

This repository is designed to be run via Docker Compose.

## Contents

- Overview
- How it works
- Quick start
- Configuration (`config.json` and `.env`)
- Routing (multi-destination + forum topics)
- Filtering (ban words + link limit)
- Manage from the Telegram app (commands)
- Troubleshooting
- Development / tests

## Overview

Features:

- Multi-source → multi-destination routing
- Telegram albums (media groups)
- Structured JSON logs
- Rate limit + retry + DLQ (dead-letter queue)
- Hot reload when `config.json` changes
- Forum topic routing for destination forums (Telegram “Topics”)
- Optional Twitter/X link expansion using `yt-dlp` (downloads tweet media and sends it as native media)

## How it works

1) Your **user account** (userbot) watches source channels/groups.
2) When a new message arrives, userbot forwards/copies it to your **relay bot** chat.
3) The relay bot reposts it into destination channels/topics based on `relay.routes`.

Why two stages?

- Many destination channels should be posted **as a bot** (clean sender identity).
- A bot account has limitations (especially around forum topic listing); userbot can do “admin-like” fetching/exporting.

## Quick start

### Requirements

- Docker + Docker Compose
- Telegram API credentials (API ID + API hash)
- A Telegram bot token

### Install

This repo includes an interactive install script:

```bash
REPO_URL="<REDACTED_REPO_URL>" \
bash -c "$(curl -fsSL <REDACTED_INSTALL_SCRIPT_URL>)"
```

(Replace the URLs with your actual repo/script URL if you use the installer. If you run locally, just clone and edit configs.)

### Start

```bash
docker compose up -d --build
```

## Configuration

There are two configuration layers:

- `config.json`: the canonical persisted configuration.
- `.env`: optional environment variable overrides (recommended for secrets).

Important: if `.env` (or container env vars) sets a value, it can override `config.json`. If behavior doesn’t change after editing `config.json`, check `.env`.

### Minimal redacted config example

Create `config.json` based on `config.example.json`.

```json
{
  "api_id": 123456,
  "api_hash": "<REDACTED>",
  "master_account_id": 123456789,
  "bot_mappings": [
    {"source_chat": -1001111111111, "target_bot": "@YourRelayBot"}
  ],
  "relay": {
    "api_id": 123456,
    "api_hash": "<REDACTED>",
    "bot_token": "<REDACTED>",
    "dest_channels": [-1002222222222],

    "routes": [
      {
        "source_chats": [-1001111111111],
        "destinations": [
          {"chat_id": -1002222222222, "topic_title": "Topic A"},
          {"chat_id": -1003333333333, "topic_title": "Topic B"}
        ]
      }
    ]
  }
}
```

### bot_mappings (userbot stage)

`bot_mappings` tells userbot what to listen to and which bot username to forward to.

- `source_chat`: a chat ID (e.g. `-100...`) or a username
- `target_bot`: must be a **bot username** like `@YourRelayBot`

If you mistakenly put a channel username or chat ID as `target_bot`, your **user account** may forward directly into that chat.

### relay section (relaybot stage)

Core settings:

- `relay.bot_token`: the relay bot token
- `relay.dest_channels`: destination chats (used when no route matches)
- `relay.routes`: explicit routing rules
- `relay.default_destinations`: fallback destinations when no route matches

Optional behavior:

- `relay.strip_text`: if `true`, remove all text/captions (media-only relay)
- `relay.post_captions`: per-destination appended footer text
- `relay.require_forum_topic`: if `true`, skip messages when the topic cannot be resolved

## Routing

### Multi-destination (one source → many destinations)

Each route can have multiple destinations:

```json
{
  "source_chats": [-1001111111111],
  "destinations": [
    {"chat_id": -1002222222222, "topic_title": "🔥 Hot"},
    {"chat_id": -1003333333333, "topic_title": "🔥 今日爆熱"}
  ]
}
```

### Forum topics (destination)

Telegram forum topics require sending with a `reply_to` pointing at the topic’s **top_message id** (Bot API calls it `message_thread_id`).

In this project:

- `destinations[].topic_title`: a human-friendly title used for matching
- `relay.forum_topic_ids`: mapping of destination `chat_id -> {topic_title -> top_message_id}`

Why `forum_topic_ids` is necessary:

- Bots are often blocked from listing/searching forum topics via MTProto.
- When the relay bot cannot resolve the topic, Telegram posts into the forum’s General topic.

### Export forum topic IDs (recommended)

Run this from a userbot-enabled environment (user login):

```bash
python scripts/export_forum_topic_ids.py --config config.json --write
```

If you see `forum_topic_not_found` warnings but you believe the topic exists, it may be:

- The topic is archived/hidden
- The topic has a different title than your config

### Topic title aliases

If your config uses “semantic” titles but the actual forum topic titles are different, use:

- `relay.topic_title_aliases`

Example:

```json
"topic_title_aliases": {
  "-1002222222222": {
    "🏋️ Gym Hunks": "💪 Jocks & Twinks"
  }
}
```

Then rerun export:

```bash
python scripts/export_forum_topic_ids.py --config config.json --write
```

### Dump available topics (for manual mapping)

```bash
python scripts/export_forum_topic_ids.py --config config.json --dump-topics-file topics_dump.json
```

This writes a JSON file containing the titles currently visible to MTProto.

## Filtering (ban words + link limit)

### Ban words

`relay.blocklist_substrings` is a simple substring match. If any banned substring appears in the incoming message text (case-insensitive), the message is skipped.

Notes:

- This list is applied by **userbot** (before sending to relaybot) and by **relaybot** (before posting).
- Very short tokens (e.g. single characters) can cause false positives.

### Link limit

Messages containing **more than 3 links** are skipped.

- Counted patterns: `http://`, `https://`, `www.`, `t.me/`
- Hashtags like `#tag` are not treated as links

The link rule is enforced both in userbot and relaybot.

## Manage from the Telegram app (commands)

You can manage what the userbot listens to from within Telegram, without editing `config.json` manually.

The command handler is implemented in `telegram_bot.py` and only accepts commands sent from `master_account_id`.

Commands:

- `/join <chat>`: join a channel/group (username or invite link)
- `/leave <chat>`: leave a channel/group
- `/add_listen <source_chat> <@relay_bot_username>`: start listening to a source chat and forward to a relay bot
- `/remove_listen <source_chat>`: stop listening
- `/list_listen`: list current listen mappings

Practical usage:

- Open **Saved Messages** (or a private chat with your own account).
- Send the commands there (your userbot is the same account, so it sees them).

Important:

- These commands manage **listening** (userbot → relaybot). They do not edit `relay.routes`.
- Routing to destination topics is controlled by `relay.routes` in `config.json`.

If you want “fully configure routes from Telegram chat commands”, that can be added, but it is not implemented by default.

## Troubleshooting

### “My user account is posting into the destination chat”

Common causes:

1) `bot_mappings[].target_bot` is wrong (not a bot username)
2) `bot_session.session` was previously logged in as a user

Fix:

- Ensure `target_bot` is `@YourRelayBot`
- Delete `bot_session.session` and restart relaybot

### “Everything goes to the General topic”

- Populate `relay.forum_topic_ids` using `scripts/export_forum_topic_ids.py --write`
- If you want to skip instead of falling back to General, set:
  - `relay.require_forum_topic: true`

### “Topic exists but export cannot find it”

- Check if the topic is archived/hidden in the Telegram client
- Use `relay.topic_title_aliases` and rerun export

### Hot reload

Both bots watch `config.json` changes. If you update config in-place, they will reload automatically.

If you use `.env` overrides, you must restart containers for env changes to take effect.

## Development / tests

Recommended local checks:

```bash
python -m unittest discover -s tests -v
python -m py_compile telegram_bot.py bot_relay.py common_config.py structured_logger.py delivery.py command_utils.py twitter_expand.py
bash -n scripts/install.sh
```
