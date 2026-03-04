import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telethon import TelegramClient, functions, types
from telethon.errors.rpcbaseerrors import BadRequestError

from common_config import ConfigManager, load_userbot_settings


def _topic_title_variants(title: str) -> list[str]:
    if not title:
        return []

    t = str(title)
    out = [t]

    # If config topic names have a leading emoji ("🍑 Foo"), allow matching old titles.
    if " " in t:
        first, rest = t.split(" ", 1)
        if len(first) <= 8 and rest:
            out.append(rest)

    return out


async def list_topics(client: TelegramClient, peer):
    entity = await client.get_entity(peer)
    res = await client(
        functions.messages.GetForumTopicsRequest(
            peer=entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
            q="",
        )
    )
    return entity, list(res.topics)


async def find_topic(client: TelegramClient, peer, title: str):
    entity = await client.get_entity(peer)

    for q in _topic_title_variants(title):
        res = await client(
            functions.messages.GetForumTopicsRequest(
                peer=entity,
                offset_date=None,
                offset_id=0,
                offset_topic=0,
                limit=50,
                q=str(q),
            )
        )
        for t in list(res.topics):
            if t.title == title or t.title == str(q):
                return entity, t

    return entity, None


async def _default_topic_icon_ids(client: TelegramClient) -> dict[str, int]:
    res = await client(
        functions.messages.GetStickerSetRequest(
            stickerset=types.InputStickerSetEmojiDefaultTopicIcons(),
            hash=0,
        )
    )

    if not hasattr(res, "documents"):
        return {}

    out: dict[str, int] = {}
    for doc in res.documents:
        alt = None
        for attr in getattr(doc, "attributes", []) or []:
            if isinstance(attr, types.DocumentAttributeCustomEmoji):
                alt = getattr(attr, "alt", None)
                break
        if not alt:
            continue
        out.setdefault(str(alt), int(doc.id))

    return out


def _is_topic_not_modified(exc: Exception) -> bool:
    return "topic_not_modified" in str(exc).lower()


def _is_chat_not_modified(exc: Exception) -> bool:
    return "chat_not_modified" in str(exc).lower() or "CHAT_NOT_MODIFIED" in str(exc)


async def apply_chat_title_renames(client: TelegramClient, chat_title_renames: dict[str, str]) -> None:
    if not chat_title_renames:
        return

    for peer, new_title in chat_title_renames.items():
        if not peer or not new_title:
            continue

        try:
            entity = await client.get_entity(int(peer))
            await client(functions.channels.EditTitleRequest(channel=entity, title=str(new_title)))
            print(f"Renamed chat {peer} to '{new_title}'")
        except Exception as exc:  # noqa: BLE001
            if _is_chat_not_modified(exc):
                print(f"Chat {peer} already has title '{new_title}', skipping.")
                continue
            raise


async def apply_topic_renames(client: TelegramClient, peer, renames: dict[str, str]) -> None:
    if not renames:
        return

    for old, new in renames.items():
        if not old or not new or old == new:
            continue

        entity, topic = await find_topic(client, peer, str(old))
        if not topic:
            continue

        _, new_topic = await find_topic(client, peer, str(new))
        if new_topic:
            continue

        try:
            await client(functions.messages.EditForumTopicRequest(peer=entity, topic_id=int(topic.id), title=str(new)))
        except BadRequestError as exc:
            if _is_topic_not_modified(exc):
                continue
            raise


async def apply_topic_deletes(client: TelegramClient, peer, delete_titles: list[str]) -> None:
    if not delete_titles:
        return

    for title in delete_titles:
        if not title:
            continue

        entity, topic = await find_topic(client, peer, str(title))
        if not topic:
            continue

        # Deleting a forum topic is done by deleting its history using the topic top message id.
        await client(functions.messages.DeleteTopicHistoryRequest(peer=entity, top_msg_id=int(topic.top_message)))


async def apply_topic_icons(
    client: TelegramClient,
    peer,
    topic_icon_emojis: dict[str, str],
    default_icon_ids: dict[str, int],
) -> None:
    if not topic_icon_emojis:
        return
    if not default_icon_ids:
        return

    for title, emoji in topic_icon_emojis.items():
        if not title or not emoji:
            continue

        entity, t = await find_topic(client, peer, str(title))
        if not t:
            continue

        icon_id = default_icon_ids.get(str(emoji))
        if not icon_id:
            continue

        try:
            await client(
                functions.messages.EditForumTopicRequest(
                    peer=entity,
                    topic_id=int(t.id),
                    icon_emoji_id=int(icon_id),
                )
            )
        except BadRequestError as exc:
            if _is_topic_not_modified(exc):
                continue
            raise


async def ensure_topics(
    client: TelegramClient,
    peer,
    titles: list[str],
    topic_icon_emojis: dict[str, str] | None = None,
    default_icon_ids: dict[str, int] | None = None,
):
    topic_icon_emojis = topic_icon_emojis or {}
    default_icon_ids = default_icon_ids or {}

    out: dict[str, int] = {}

    for title in titles:
        if not title:
            continue

        entity, existing_topic = await find_topic(client, peer, str(title))
        if existing_topic:
            out[str(title)] = int(existing_topic.top_message)
            continue

        icon_emoji_id = None
        emoji = topic_icon_emojis.get(str(title))
        if emoji:
            icon_emoji_id = default_icon_ids.get(str(emoji))

        await client(
            functions.messages.CreateForumTopicRequest(
                peer=entity,
                title=str(title),
                icon_color=0x6FB9F0,
                icon_emoji_id=int(icon_emoji_id) if icon_emoji_id else None,
            )
        )

        _, created = await find_topic(client, peer, str(title))
        if created:
            out[str(title)] = int(created.top_message)

    return out


async def main():
    parser = argparse.ArgumentParser(description="Ensure forum topics exist and write topic->top_message mapping into config")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--write", action="store_true", help="Write mapping into relay.forum_topic_top_messages")
    parser.add_argument("--rename", action="store_true", help="Apply relay.topic_renames before syncing")
    parser.add_argument("--icons", action="store_true", help="Apply relay.topic_icon_emojis (default icon pack)")
    parser.add_argument("--delete", action="store_true", help="Apply relay.topic_deletes before syncing")
    parser.add_argument("--rename-chats", action="store_true", help="Apply relay.chat_title_renames")
    args = parser.parse_args()

    config_manager = ConfigManager(args.config)
    cfg = config_manager.load(force=True)

    relay = cfg.get("relay") or {}
    ensure = relay.get("ensure_forum_topics") or []
    if not ensure:
        raise SystemExit("relay.ensure_forum_topics is empty")

    settings = load_userbot_settings(config_manager)
    session_dir = Path("data/sessions")
    session_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session_dir / "userbot_tools"), settings["api_id"], settings["api_hash"], proxy=settings["proxy"])
    await client.start()

    try:
        out: dict[str, dict[str, int]] = {}
        renames_cfg = relay.get("topic_renames") or {}
        icons_cfg = relay.get("topic_icon_emojis") or {}
        deletes_cfg = relay.get("topic_deletes") or {}

        if args.rename_chats:
            chat_title_renames = relay.get("chat_title_renames") or {}
            if isinstance(chat_title_renames, dict) and chat_title_renames:
                await apply_chat_title_renames(client, {str(k): str(v) for k, v in chat_title_renames.items()})

        default_icon_ids: dict[str, int] = {}
        if args.icons:
            default_icon_ids = await _default_topic_icon_ids(client)

        for item in ensure:
            chat_id = item.get("chat_id")
            titles = [str(t) for t in (item.get("topics") or [])]
            if not chat_id or not titles:
                continue

            peer = int(chat_id)

            if args.delete:
                chat_deletes = deletes_cfg.get(str(peer)) or deletes_cfg.get(peer) or []
                if isinstance(chat_deletes, list) and chat_deletes:
                    await apply_topic_deletes(client, peer, [str(x) for x in chat_deletes if x])

            if args.rename:
                chat_renames = renames_cfg.get(str(peer)) or renames_cfg.get(peer) or {}
                if isinstance(chat_renames, dict) and chat_renames:
                    await apply_topic_renames(client, peer, {str(k): str(v) for k, v in chat_renames.items()})

            chat_icons = icons_cfg.get(str(peer)) or icons_cfg.get(peer) or {}
            if not isinstance(chat_icons, dict):
                chat_icons = {}

            existing = await ensure_topics(
                client,
                peer,
                titles,
                topic_icon_emojis={str(k): str(v) for k, v in chat_icons.items()},
                default_icon_ids=default_icon_ids,
            )

            if args.icons and chat_icons:
                await apply_topic_icons(
                    client,
                    peer,
                    topic_icon_emojis={str(k): str(v) for k, v in chat_icons.items()},
                    default_icon_ids=default_icon_ids,
                )

            out[str(peer)] = {t: int(existing[t]) for t in titles if t in existing}

        print(json.dumps(out, ensure_ascii=False, indent=2))

        if args.write:
            relay["forum_topic_top_messages"] = out
            cfg["relay"] = relay
            config_manager.save(cfg)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
