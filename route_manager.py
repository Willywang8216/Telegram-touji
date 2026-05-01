"""Shared route-command helpers used by both telegram_bot.py and bot_relay.py.

All async functions accept an explicit ``client`` (Telethon TelegramClient) as
their first argument so neither bot's module-level singleton leaks in here.
"""

import shlex
from typing import Any

from telethon import utils

from telegram_link_utils import looks_like_message_link, parse_message_link
from route_filter_utils import filter_routes, parse_route_filters


# ---------------------------------------------------------------------------
# Destination token helpers
# ---------------------------------------------------------------------------

def parse_destinations_tokens(tokens: list[str]) -> list[dict[str, Any]]:
    """Convert CLI-style destination tokens into destination dicts.

    Token formats:
      <chat_id>@<topic_id>       → {"chat_id": int, "topic_id": int}
      <chat_id>="<title>"        → {"chat_id": int, "topic_title": str}
      <chat_id>                  → {"chat_id": int}
    """
    out: list[dict[str, Any]] = []
    for tok in tokens:
        if not tok:
            continue
        if "@" in tok:
            chat_str, topic_str = tok.split("@", 1)
            out.append({"chat_id": int(chat_str), "topic_id": int(topic_str)})
            continue
        if "=" in tok:
            chat_str, title = tok.split("=", 1)
            out.append({"chat_id": int(chat_str), "topic_title": title.strip()})
            continue
        out.append({"chat_id": int(tok)})
    return out


def format_destinations(destinations: list[dict[str, Any]]) -> str:
    """Inverse of parse_destinations_tokens: format dests back to CLI tokens."""
    parts: list[str] = []
    for d in destinations or []:
        chat_id = d.get("chat_id")
        if d.get("topic_id") is not None:
            parts.append(f"{chat_id}@{d.get('topic_id')}")
        elif d.get("topic_title"):
            parts.append(f"{chat_id}=\"{d.get('topic_title')}\"")
        else:
            parts.append(str(chat_id))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Message-link resolver
# ---------------------------------------------------------------------------

async def resolve_message_link(client, link: str) -> tuple[int, int, int | None]:
    """Resolve a Telegram message link to (chat_id, message_id, topic_top).

    Uses the same logic as the old _extract_source_topic_top_id helper:
      - reply_to.reply_to_top_id if set
      - msg.id if msg.is_topic
      - parsed.topic_id as final fallback

    Raises ValueError("invalid_link") or ValueError("message_not_found").
    """
    parsed = parse_message_link(link)
    if not parsed:
        raise ValueError("invalid_link")

    ent = await client.get_entity(parsed.chat)
    chat_id = int(utils.get_peer_id(ent))

    msg = await client.get_messages(ent, ids=int(parsed.message_id))
    if not msg:
        raise ValueError("message_not_found")

    reply_to = getattr(msg, "reply_to", None)
    top = getattr(reply_to, "reply_to_top_id", None)
    if top:
        topic_top: int | None = int(top)
    elif getattr(msg, "is_topic", False):
        try:
            topic_top = int(getattr(msg, "id", 0) or 0) or None
        except Exception:  # noqa: BLE001
            topic_top = None
    else:
        topic_top = None

    if topic_top is None and parsed.topic_id is not None:
        topic_top = int(parsed.topic_id)

    return chat_id, int(parsed.message_id), topic_top


# ---------------------------------------------------------------------------
# Filter-arg normaliser
# ---------------------------------------------------------------------------

async def normalize_routes_filter_args(
    client,
    event,
    args: str,
) -> str | None:
    """Parse filter args, resolving any Telegram message links into dest= / topic= tokens.

    Returns the normalized filter string, or None if a link parse failed
    (already replied to event in that case).
    """
    tokens = shlex.split(args or "")
    if not tokens:
        return (args or "").strip()

    dest_ids: list[int] = []
    topic_top: int | None = None
    topic_title: str | None = None
    keep: list[str] = []

    for tok in tokens:
        if looks_like_message_link(tok):
            try:
                chat_id, _, top = await resolve_message_link(client, tok)
            except Exception as exc:  # noqa: BLE001
                await event.reply(f"🤖 错误: 无法解析链接: {tok} ({type(exc).__name__})")
                return None

            dest_ids.append(int(chat_id))

            if top is not None and topic_top is None:
                topic_top = int(top)

                if topic_top == 1:
                    topic_title = "General"
                else:
                    try:
                        ent = await client.get_entity(int(chat_id))
                        msg = await client.get_messages(ent, ids=int(topic_top))
                        action = getattr(msg, "action", None) if msg else None
                        title = getattr(action, "title", None) if action else None
                        if title:
                            topic_title = str(title)
                    except Exception:  # noqa: BLE001
                        topic_title = None

            continue

        keep.append(tok)

    if dest_ids:
        seen: set[int] = set()
        uniq: list[int] = []
        for d in dest_ids:
            if d in seen:
                continue
            seen.add(d)
            uniq.append(d)
        keep.append("dest=" + ",".join(str(x) for x in uniq))

    # Prefer filtering by resolved topic title (works for both topic_id and topic_title routes).
    if topic_title:
        keep.append("topic=" + shlex.quote(topic_title))
    elif topic_top is not None:
        keep.append(f"topic_id={int(topic_top)}")

    return " ".join([x for x in keep if str(x).strip()]).strip()


# ---------------------------------------------------------------------------
# Route-listing report builder
# ---------------------------------------------------------------------------

async def build_routes_report(
    client,
    routes: list[dict[str, Any]],
    args: str,
) -> str | None:
    """Build the formatted route listing string.

    Returns None when no routes match the given filters.
    """
    entity_cache: dict[int, object] = {}
    topic_title_cache: dict[tuple[int, int], str] = {}

    async def _get_entity(chat_id: int):
        if chat_id in entity_cache:
            return entity_cache[chat_id]
        ent = await client.get_entity(chat_id)
        entity_cache[chat_id] = ent
        return ent

    def _entity_label(ent) -> str:
        title = getattr(ent, "title", None)
        username = getattr(ent, "username", None)
        if title and username:
            return f"{title} (@{username})"
        if title:
            return str(title)
        if username:
            return f"@{username}"
        return str(getattr(ent, "id", ""))

    def _internal_chat_id(chat_id: int) -> int | None:
        # t.me/c/<internal>/<msg_id> uses chat id without the -100 prefix.
        if str(int(chat_id)).startswith("-100"):
            return abs(int(chat_id)) - 1000000000000
        return None

    def _chat_link(ent, chat_id: int) -> str | None:
        username = getattr(ent, "username", None)
        if username:
            return f"https://t.me/{username}"
        internal = _internal_chat_id(chat_id)
        if internal is not None:
            return f"https://t.me/c/{internal}/1"
        return None

    def _message_link(ent, chat_id: int, msg_id: int) -> str | None:
        username = getattr(ent, "username", None)
        if username:
            return f"https://t.me/{username}/{int(msg_id)}"
        internal = _internal_chat_id(chat_id)
        if internal is not None:
            return f"https://t.me/c/{internal}/{int(msg_id)}"
        return None

    async def _topic_title(chat_id: int, top_message_id: int) -> str | None:
        if top_message_id == 1:
            return "General"

        key = (chat_id, top_message_id)
        if key in topic_title_cache:
            return topic_title_cache[key]

        ent = await _get_entity(chat_id)
        msg = await client.get_messages(ent, ids=int(top_message_id))
        if not msg:
            return None

        action = getattr(msg, "action", None)
        title = getattr(action, "title", None)
        if not title:
            return None

        topic_title_cache[key] = str(title)
        return str(title)

    filtered = list(routes)
    if (args or "").strip():
        flt = parse_route_filters(args or "")
        topic_sub = str(flt.get("topic") or "").casefold().strip() or None

        base = dict(flt)
        base["topic"] = None
        filtered = filter_routes(list(routes), filters=base)

        if topic_sub is not None:
            matched: list[dict[str, Any]] = []
            for r in filtered:
                ok = False
                for d in (r.get("destinations") or []):
                    if topic_sub in str(d.get("topic_title") or "").casefold():
                        ok = True
                        break

                    if d.get("topic_id") is not None and d.get("chat_id") is not None:
                        try:
                            title = await _topic_title(int(d.get("chat_id")), int(d.get("topic_id")))
                        except Exception:  # noqa: BLE001
                            title = None
                        if title and topic_sub in str(title).casefold():
                            ok = True
                            break

                if ok:
                    matched.append(r)
            filtered = matched

    if not filtered:
        return None

    keep = {id(r) for r in filtered}

    out: list[str] = [f"🤖 Routes ({len(filtered)}/{len(routes)}):"]
    if (args or "").strip():
        out.append(f"Filters: {args}")
    out.append("Tip: use /export_routes to always download a file.")

    for idx, r in enumerate(routes, start=1):
        if id(r) not in keep:
            continue

        out.append(f"\n{idx})")

        source_chats = [int(x) for x in (r.get("source_chats") or [])]
        source_topics = [int(x) for x in (r.get("source_topics") or [])]
        destinations = list(r.get("destinations") or [])

        out.append("  Sources:")
        for cid in source_chats:
            ent = None
            label = None
            url = None
            try:
                ent = await _get_entity(cid)
                label = _entity_label(ent)
                url = _chat_link(ent, cid)
            except Exception:  # noqa: BLE001
                ent = None

            line = f"    - {cid}"
            if label:
                line += f" | {label}"
            if url:
                line += f" | {url}"
            out.append(line)

            if source_topics:
                topic_parts: list[str] = []
                for tid in source_topics:
                    try:
                        title = await _topic_title(cid, tid)
                    except Exception:  # noqa: BLE001
                        title = None
                    part = str(tid)
                    if title:
                        part += f" | {title}"

                    if ent is not None:
                        turl = _message_link(ent, cid, tid)
                        if turl:
                            part += f" | {turl}"

                    topic_parts.append(part)
                out.append("      topics: " + "; ".join(topic_parts))
            else:
                try:
                    ent2 = ent or (entity_cache.get(cid) or await _get_entity(cid))
                    if getattr(ent2, "forum", False):
                        out.append("      topics: ALL")
                except Exception:  # noqa: BLE001
                    pass

        out.append("  Destinations:")
        for d in destinations:
            chat_id = int(d.get("chat_id"))
            ent = None
            chat_label = None
            chat_url = None
            try:
                ent = await _get_entity(chat_id)
                chat_label = _entity_label(ent)
                chat_url = _chat_link(ent, chat_id)
            except Exception:  # noqa: BLE001
                ent = None

            line = f"    - {chat_id}"
            if chat_label:
                line += f" | {chat_label}"
            if chat_url:
                line += f" | {chat_url}"

            if d.get("topic_id") is not None:
                topic_id = int(d.get("topic_id"))
                topic_label = None
                try:
                    topic_label = await _topic_title(chat_id, topic_id)
                except Exception:  # noqa: BLE001
                    topic_label = None

                line += f" | topic_id={topic_id}"
                if topic_label:
                    line += f" | {topic_label}"
                if ent is not None:
                    turl = _message_link(ent, chat_id, topic_id)
                    if turl:
                        line += f" | {turl}"
            elif d.get("topic_title"):
                line += f" | topic_title=\"{d.get('topic_title')}\""

            out.append(line)

    return "\n".join(out)
