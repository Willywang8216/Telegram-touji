# Telegram Relay Bot (Telethon + Docker)

A two-stage Telegram relay system:

- `telegram_bot.py` (**userbot**, your user account): listens to source chats and forwards/copies messages to a relay bot.
- `bot_relay.py` (**relaybot**, a bot account): reposts messages into destination channels and forum topics.

Designed to run via Docker Compose.

## What gets relayed (important)

This relay is media-focused:

- Single messages:
  - Videos (including video notes / GIF videos): relayed
  - Single photos: skipped
  - Text-only: skipped
- Albums (media groups):
  - Any album containing a video: relayed
  - Album with 2+ photos: relayed
  - Album with only 1 photo: skipped

Filters:

- Banned substrings: `relay.blocklist_substrings` (substring match)
- Link limit: messages with >3 links are skipped (`http://`, `https://`, `www.`, `t.me/`; hashtags don’t count)

Special handling:

- Protected content (`noforwards`) is copied by downloading media and re-uploading.
- Source forum topics: userbot embeds the source topic’s `top_message` id so relaybot can do topic-specific routing (`source_topics`).

## Quick start

### 0) Telegram prerequisites

1. Create your relay bot using **@BotFather** and copy the bot token.
2. Add the bot to each destination channel/supergroup and grant permission to post messages.
3. Ensure your user account (the one running userbot) can read the source chats.

### 1) Configure

- Copy `config.example.json` to `config.json`.
- Put secrets in `.env` when possible.

### 2) Start

```bash
docker compose up -d --build
```

### 3) First-time userbot login

Userbot may require interactive login (phone/code/2FA). Compose enables TTY for userbot, so you can attach:

```bash
docker attach toujibot_user
```

Relaybot logs in using `relay.bot_token` and does not require phone login.

## Finding chat ids and topic ids

### Chat ids (`-100...`)

- Public chats: you can often use `@username` in `/join` and `/list_topics`.
- For numeric ids, run:

```bash
python scripts/list_dialogs.py --type supergroup --type channel
```

Look for `peer_id`.

### Destination forum topic ids (recommended)

To post into destination topics reliably, populate `relay.forum_topic_ids`:

```bash
python scripts/export_forum_topic_ids.py --config config.json --write
```

To dump existing topic titles for manual mapping:

```bash
python scripts/export_forum_topic_ids.py --config config.json --dump-topics-file topics_dump.json
```

## Minimal redacted config example

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
          {"chat_id": -1002222222222, "topic_title": "🔥 Hot Right Now"},
          {"chat_id": -1003333333333, "topic_title": "🔥 今日爆熱"}
        ]
      }
    ],

    "blocklist_substrings": ["门槛", "费用", "暗号"],

    "distribute_unrouted_to_buckets": true,
    "unrouted_distribution_mode": "message",
    "general_topic_buckets": {
      "-1002222222222": ["🔥 Hot Right Now", "💪 Jocks & Twinks"]
    },

    "post_captions": {
      "-1002222222222": "Follow: @<your_channel>"
    },

    "expand_twitter_links": true,
    "twitter_cookies_file": null,
    "twitter_max_media_files": 8
  }
}
```

## More configuration features (with concrete examples)

### Default destinations (fallback when no route matches)

```json
{
  "relay": {
    "default_destinations": [
      {"chat_id": -1002222222222, "topic_title": "🧺 General"},
      {"chat_id": -1003333333333}
    ]
  }
}
```

### Require topic (skip if topic not resolvable)

```json
{
  "relay": {
    "require_forum_topic": true
  }
}
```

### Destination topic fallback

```json
{
  "relay": {
    "fallback_to_general_topic": true,
    "fallback_topic_titles": {
      "-1002222222222": "Chatting & Sharing"
    }
  }
}
```

### Export aliases for topic titles

Used by `scripts/export_forum_topic_ids.py` when your configured title doesn’t match the real title:

```json
{
  "relay": {
    "topic_title_aliases": {
      "-1002222222222": {
        "🏋️ Gym Hunks": "💪 Jocks & Twinks"
      }
    }
  }
}
```

Then rerun export.

### Twitter/X expansion

```json
{
  "relay": {
    "expand_twitter_links": true,
    "twitter_cookies_file": "/app/twitter.cookies.txt",
    "twitter_max_media_files": 8
  }
}
```

## Twitter/X account watch (poll & repost)

This repo can optionally poll X (Twitter) profiles and repost new tweets that contain relayed media.

How it works:

- Userbot (`telegram_bot.py`) polls configured profiles (best-effort via yt-dlp) and sends each new tweet URL to the relay bot in a DM.
- The message includes an embedded `SRC_CHAT_ID` marker so relaybot routing works.
- Relaybot (`bot_relay.py`) expands the tweet URL into media (using `relay.twitter_cookies_file` / `RELAY_TWITTER_COOKIES_FILE`) and applies the same filters as normal.

### Configure

1) Save your X cookies (Netscape format) to a file, e.g. `twitter.cookies.txt`.
   - Keep this file private. Don’t commit it.

2) In `config.json`, enable `twitter_watch` and configure sources.

3) Add a route for the `source_chat_id` you chose:

```
/add_route -900000001 -100<dest_chat_id>@<topic_top_msg_id>
```

Or use a topic title:

```
/add_route -900000001 -100<dest_chat_id>="Topic Title"
```

### Telegram commands

In userbot DM (Saved Messages):

- `/add_x_watch <x_profile_or_username> <source_chat_id> <@relay_bot> [poll_interval_sec=300] [fetch_limit=30] [archive_file=state/...]`
- `/list_x_watch`
- `/remove_x_watch <index>`

## Manage from Telegram (commands)

Userbot and relaybot can accept DM commands, but the allowed sender is controlled separately:

- Userbot: top-level `master_account_id`
- Relaybot: `relay.master_account_id` (or env `RELAY_MASTER_ACCOUNT_ID`)

For safety, set both to your own Telegram numeric user id.

Tip: use **Saved Messages** for userbot commands, and DM the relay bot for relaybot commands.

### Userbot commands (`telegram_bot.py`)

Listen management:

- `/join <chat>`
- `/leave <chat>`
- `/add_listen <source_chat> <@relay_bot_username>`
- `/remove_listen <source_chat>`
- `/list_listen`

Forum utilities:

- `/list_topics <chat_id_or_username> [limit]` (prints `top_message`)

Route management (edits `relay.routes` in `config.json`):

- `/list_routes`
- `/add_route <source_chat[,..]> [source_topic=<top_msg_id>] <dest_chat>@<topic_top_msg_id> | <dest_chat>="<topic_title>" ...`
- `/add_route <source_message_link> <dest_message_link> [dest_message_link...]`
  - You can copy a message link from inside a forum topic and use it directly (the bot will infer the source topic and destination topic ids).
- `/remove_route <index>`
- `/set_destinations <index> <dest...>`

Concrete examples:

```
/join https://t.me/<invite_or_username>
/add_listen -1001111111111 @YourRelayBot
/add_route -1001111111111 -1002222222222="🔥 Hot Right Now" -1003333333333="🔥 今日爆熱"
```

Topic-specific route example:

```
/list_topics -1001111111111
/add_route -1001111111111 source_topic=777 -1002222222222="Topic A"
```

Link-based route example (copy message links from Telegram):

```
/add_route https://t.me/c/<src_internal_id>/<topic_id>/<msg_id> https://t.me/c/<dest_internal_id>/<topic_id>/<msg_id>
```

### Relaybot commands (`bot_relay.py`)

- `/help` or `/start`
- `/list_routes`
- `/add_route ...`
- `/remove_route <index>`
- `/set_destinations <index> ...`

## Cookbook: common workflows

### A) Relay a new group/channel to two channels (specific topics)

```
/add_listen -1001111111111 @YourRelayBot
/add_route -1001111111111 -1002222222222="🔥 Hot Right Now" -1003333333333="🔥 今日爆熱"
```

Then export destination topic ids:

```bash
python scripts/export_forum_topic_ids.py --config config.json --write
```

### B) Route topic1/topic2/topic3 from a source forum into topic_a/topic_b/topic_c

1) Get source topic `top_message` ids:

```
/list_topics -1001111111111
```

2) Add routes:

```
/add_route -1001111111111 source_topic=777 -1002222222222="Topic A"
/add_route -1001111111111 source_topic=888 -1002222222222="Topic B"
/add_route -1001111111111 source_topic=999 -1002222222222="Topic C"
```

## Troubleshooting

### Not relaying anything

Common reasons:

- Text-only messages are skipped
- Single-photo messages are skipped
- Contains a banned word
- Contains >3 links

### Everything goes to the General topic

- Export `relay.forum_topic_ids`:
  ```bash
  python scripts/export_forum_topic_ids.py --config config.json --write
  ```
- Or use explicit destination `chat_id@topic_top_message` in routes.

### Validate routing configuration

```bash
python scripts/validate_routing.py config.json
```

### DLQ (dead-letter queue)

Failures are recorded as JSONL:

- `logs/userbot_dlq.jsonl`
- `logs/relay_dlq.jsonl`

### “My user account is posting into the destination chat”

- Ensure `bot_mappings[].target_bot` is a bot username (`@...`), not a channel.
- If relaybot accidentally logged in as a user before: delete `bot_session.session` and restart relaybot.

## Development / tests

```bash
python -m unittest discover -s tests -v
python -m py_compile telegram_bot.py bot_relay.py common_config.py structured_logger.py delivery.py command_utils.py twitter_expand.py
bash -n scripts/install.sh
```

## Engineering review summary

The codebase is already fairly focused and there are no obviously safe-to-delete core modules from a static pass. The main improvement areas are:

- Efficiency: reduce duplicate logic between `telegram_bot.py` and `bot_relay.py` over time by extracting shared relay filters into a common module.
- Security: avoid mutable startup installs in production if you need reproducible deployments; the current `run_userbot.sh` and `run_relaybot.sh` intentionally self-update `yt-dlp` for X/Twitter compatibility, which favors operability over supply-chain stability.
- Robustness: `.env` is now reloaded on config hot-reload checks as well, so environment changes in the config directory are picked up consistently.
- Scalability: route resolution is configuration-driven and adequate for the current size, but if route count grows substantially, pre-indexing routes by source chat/topic would reduce repeated linear scans.
- User experience: route/topic tooling is strong; the highest-value follow-up would be clearer operator docs around session locking, cookies handling, and route export/import workflows.
