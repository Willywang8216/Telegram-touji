import asyncio
import logging
import tempfile

from telethon import TelegramClient, utils
from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError, MessageIdInvalidError
from telethon.events import NewMessage
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest

from command_utils import parse_command
from common_config import ConfigManager, load_userbot_settings
from delivery import AsyncRateLimiter, with_retry, write_dlq

try:
    from structured_logger import get_logger, log_event
except ModuleNotFoundError:  # pragma: no cover
    import logging

    def get_logger(name: str) -> logging.Logger:
        logger = logging.getLogger(name)
        if logger.handlers:
            return logger
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        logger.addHandler(handler)
        logger.propagate = False
        return logger

    def log_event(logger: logging.Logger, level: int, message: str, **kwargs):
        logger.log(level, message)


logger = get_logger("userbot")
config_manager = ConfigManager()
settings = load_userbot_settings(config_manager)

# Keyed by (chat_id, grouped_id) to avoid collisions across different source chats.
media_group_cache: dict[tuple[int, int], dict] = {}
media_group_lock = asyncio.Lock()

client = TelegramClient("anon", settings["api_id"], settings["api_hash"], proxy=settings["proxy"])
forwarding_map: dict[int, object] = {}
bot_mappings = settings["bot_mappings"]
blocklist_substrings = (config_manager.load().get("relay", {}) or {}).get("blocklist_substrings", []) or []
rate_limiter = AsyncRateLimiter(rate_per_sec=8)
DLQ_PATH = "logs/userbot_dlq.jsonl"


def _is_blocked(text: str) -> bool:
    hay = str(text or "").casefold()
    for s in blocklist_substrings or []:
        if not s:
            continue
        if str(s).casefold() in hay:
            return True
    return False

# Prefix used when we cannot forward (e.g. protected content / noforwards).
# Relay bot will parse it and use it for routing, then strip it from outgoing captions/text.
_SOURCE_CHAT_ID_MARKER_PREFIX = "\u2063SRC_CHAT_ID="


def _with_source_marker(source_chat_id: int, text: str | None) -> str:
    return f"{_SOURCE_CHAT_ID_MARKER_PREFIX}{int(source_chat_id)}\n" + (text or "")


def update_config_file(new_bot_mappings):
    global bot_mappings
    cfg = config_manager.load()
    bot_mappings = new_bot_mappings
    cfg["bot_mappings"] = new_bot_mappings
    config_manager.save(cfg)
    log_event(logger, logging.INFO, "config_updated", mapping_count=len(new_bot_mappings))
    asyncio.create_task(rebuild_forwarding_map())


async def rebuild_forwarding_map():
    global forwarding_map
    forwarding_map = {}
    for mapping in bot_mappings:
        source_chat = mapping["source_chat"]
        target_bot = mapping["target_bot"]
        try:
            try:
                src_id = int(source_chat)
            except ValueError:
                src_id = source_chat
            source_entity = await client.get_entity(src_id)
            target_entity = await client.get_entity(str(target_bot))

            # Safety: prevent misconfiguration where target_bot is actually a channel/group.
            # If this happens, the *user account* will forward directly into that channel/group.
            if not getattr(target_entity, "bot", False):
                log_event(
                    logger,
                    logging.ERROR,
                    "mapping_failed_target_not_bot",
                    source_chat=str(source_chat),
                    target_bot=str(target_bot),
                )
                continue

            peer_id = int(utils.get_peer_id(source_entity))
            forwarding_map[peer_id] = target_entity
            log_event(logger, logging.INFO, "mapping_updated", source_chat=str(source_chat), target_bot=str(target_bot))
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "mapping_failed", source_chat=str(source_chat), error=str(exc))


def _is_invalid_message_error(exc: Exception) -> bool:
    return isinstance(exc, MessageIdInvalidError)


async def _send_copy_to_bot(target_bot, msg, *, source_chat_id: int):
    text = getattr(msg, "raw_text", None) or getattr(msg, "text", None) or ""
    marked_text = _with_source_marker(int(source_chat_id), text)

    media = getattr(msg, "media", None)
    if media is not None:
        tmp = tempfile.TemporaryDirectory(prefix="userbot_copy_")
        try:
            local_path = await client.download_media(msg, file=tmp.name)
            if not local_path:
                # If we can't download, at least send the marked caption/text.
                await with_retry(
                    lambda: client.send_message(target_bot, marked_text),
                    retries=3,
                    base_delay=1,
                    logger=logger,
                    action="copy_single_text_fallback",
                )
                return

            await with_retry(
                lambda: client.send_file(target_bot, local_path, caption=marked_text),
                retries=3,
                base_delay=1,
                logger=logger,
                action="copy_single_media",
            )
        finally:
            tmp.cleanup()
    else:
        await with_retry(
            lambda: client.send_message(target_bot, marked_text),
            retries=3,
            base_delay=1,
            logger=logger,
            action="copy_single_text",
        )


async def safe_forward_single(target_bot, message_id, chat_id, *, noforwards: bool = False, msg=None):
    await rate_limiter.wait()

    if noforwards and msg is not None:
        try:
            await _send_copy_to_bot(target_bot, msg, source_chat_id=int(chat_id))
            log_event(logger, logging.INFO, "message_copied", chat_id=str(chat_id), message_id=message_id)
        except Exception as exc:  # noqa: BLE001
            payload = {"chat_id": chat_id, "message_id": message_id, "error": str(exc)}
            write_dlq(DLQ_PATH, payload)
            log_event(logger, logging.ERROR, "message_copy_failed", **payload)
        return

    try:
        await with_retry(
            lambda: client.forward_messages(target_bot, message_id, from_peer=chat_id),
            retries=3,
            base_delay=1,
            logger=logger,
            action="forward_single",
        )
        log_event(logger, logging.INFO, "message_forwarded", chat_id=str(chat_id), message_id=message_id)
    except (ChatForwardsRestrictedError, MessageIdInvalidError) as exc:
        # Fallback: if forward fails but we have the message, try copy.
        if msg is not None:
            try:
                await _send_copy_to_bot(target_bot, msg, source_chat_id=int(chat_id))
                log_event(logger, logging.INFO, "message_copied_fallback", chat_id=str(chat_id), message_id=message_id)
                return
            except Exception as copy_exc:  # noqa: BLE001
                payload = {"chat_id": chat_id, "message_id": message_id, "error": str(copy_exc)}
                write_dlq(DLQ_PATH, payload)
                log_event(logger, logging.ERROR, "message_copy_failed", **payload)
                return

        payload = {"chat_id": chat_id, "message_id": message_id, "error": str(exc)}
        write_dlq(DLQ_PATH, payload)
        log_event(logger, logging.ERROR, "message_forward_failed", **payload)
    except Exception as exc:  # noqa: BLE001
        payload = {"chat_id": chat_id, "message_id": message_id, "error": str(exc)}
        write_dlq(DLQ_PATH, payload)
        log_event(logger, logging.ERROR, "message_forward_failed", **payload)


@client.on(NewMessage())
async def handler(event):
    if config_manager.reload_if_changed():
        global bot_mappings, blocklist_substrings
        bot_mappings = load_userbot_settings(config_manager)["bot_mappings"]
        blocklist_substrings = (config_manager.load().get("relay", {}) or {}).get("blocklist_substrings", []) or []
        await rebuild_forwarding_map()
        log_event(logger, logging.INFO, "config_hot_reloaded")

    target_bot = forwarding_map.get(event.chat_id)
    if not target_bot:
        return

    msg_text = getattr(event.message, "raw_text", "") or ""

    noforwards = bool(getattr(event.message, "noforwards", False))
    if not noforwards:
        try:
            chat = await event.get_chat()
            noforwards = bool(getattr(chat, "noforwards", False))
        except Exception:  # noqa: BLE001
            pass

    if event.message.grouped_id:
        async with media_group_lock:
            key = (event.chat_id, event.message.grouped_id)
            if key not in media_group_cache:
                media_group_cache[key] = {
                    "messages": [],
                    "task": None,
                    "target_bot": target_bot,
                    "noforwards": noforwards,
                    "blocked": False,
                }

            if msg_text and _is_blocked(msg_text):
                media_group_cache[key]["blocked"] = True

            media_group_cache[key]["messages"].append(event.message)
            if media_group_cache[key]["task"]:
                media_group_cache[key]["task"].cancel()
            media_group_cache[key]["task"] = asyncio.create_task(process_media_group(key))
    else:
        if msg_text and _is_blocked(msg_text):
            log_event(logger, logging.INFO, "message_blocked", chat_id=str(event.chat_id), message_id=getattr(event.message, "id", None))
            return
        await safe_forward_single(target_bot, event.message.id, event.chat_id, noforwards=noforwards, msg=event.message)


async def process_media_group(key: tuple[int, int]):
    from_peer, grouped_id = key
    await asyncio.sleep(1.5)

    async with media_group_lock:
        data = media_group_cache.get(key)
        if not data:
            return
        # Remove first so we never double-send even if forwarding throws.
        del media_group_cache[key]

    msgs = data.get("messages") or []
    msgs.sort(key=lambda m: m.id)

    if data.get("blocked"):
        log_event(logger, logging.INFO, "group_blocked", chat_id=str(from_peer), group_id=grouped_id)
        return

    caption_check = next(
        (
            (getattr(m, "raw_text", None) or getattr(m, "text", None) or "")
            for m in msgs
            if (getattr(m, "raw_text", None) or getattr(m, "text", None))
        ),
        "",
    )
    if caption_check and _is_blocked(caption_check):
        log_event(logger, logging.INFO, "group_blocked", chat_id=str(from_peer), group_id=grouped_id)
        return

    try:
        if data.get("noforwards"):
            # Protected content: we cannot forward, so we copy + embed source chat id marker.
            caption = next(
                (
                    (getattr(m, "raw_text", None) or getattr(m, "text", None) or "")
                    for m in msgs
                    if (getattr(m, "raw_text", None) or getattr(m, "text", None))
                ),
                "",
            )
            caption = _with_source_marker(int(from_peer), caption)

            tmp = tempfile.TemporaryDirectory(prefix="userbot_copy_group_")
            try:
                files: list[str] = []
                for m in msgs:
                    if getattr(m, "media", None) is None:
                        continue
                    p = await client.download_media(m, file=tmp.name)
                    if p:
                        files.append(str(p))

                if not files:
                    await with_retry(
                        lambda: client.send_message(data["target_bot"], caption),
                        retries=3,
                        base_delay=1,
                        logger=logger,
                        action="copy_group_text_fallback",
                    )
                    log_event(logger, logging.INFO, "group_copied_text_only", group_id=grouped_id, count=len(msgs))
                    return

                await rate_limiter.wait()
                await with_retry(
                    lambda: client.send_file(data["target_bot"], files, caption=caption),
                    retries=3,
                    base_delay=1,
                    logger=logger,
                    action="copy_group_media",
                )
                log_event(logger, logging.INFO, "group_copied", group_id=grouped_id, count=len(msgs))
            finally:
                tmp.cleanup()
        else:
            await rate_limiter.wait()
            await with_retry(
                lambda: client.forward_messages(data["target_bot"], [m.id for m in msgs], from_peer=from_peer),
                retries=3,
                base_delay=1,
                logger=logger,
                action="forward_group",
            )
            log_event(logger, logging.INFO, "group_forwarded", group_id=grouped_id, count=len(msgs))
    except Exception as exc:  # noqa: BLE001
        payload = {"group_id": grouped_id, "messages": [m.id for m in msgs], "error": str(exc)}
        write_dlq(DLQ_PATH, payload)
        log_event(logger, logging.ERROR, "group_forward_failed", **payload)


async def join_chat(entity):
    await client(JoinChannelRequest(entity))


async def leave_chat(entity):
    await client(LeaveChannelRequest(entity))


async def main():
    await client.start()
    log_event(logger, logging.INFO, "userbot_started")
    await rebuild_forwarding_map()

    @client.on(NewMessage(func=lambda e: e.is_private and e.sender_id == settings["master_account_id"]))
    async def command_handler(event):
        cmd, args = parse_command(event.message.text)
        if not cmd:
            return

        if cmd == "/join":
            try:
                ent = await client.get_entity(args)
                await join_chat(ent)
                await event.reply(f"🤖 已尝试加入: {ent.title}")
            except Exception as exc:  # noqa: BLE001
                await event.reply(f"🤖 加入失败: {exc}")

        elif cmd == "/leave":
            try:
                ent = await client.get_entity(args)
                await leave_chat(ent)
                await event.reply(f"🤖 已尝试退出: {ent.title}")
            except Exception as exc:  # noqa: BLE001
                await event.reply(f"🤖 退出失败: {exc}")

        elif cmd == "/add_listen":
            sub_parts = args.split(" ", 1)
            if len(sub_parts) == 2:
                src, bot = sub_parts[0], sub_parts[1].strip()
                if not bot.startswith("@"): 
                    await event.reply("🤖 错误: 机器人用户名需以 @ 开头")
                    return
                try:
                    ent = await client.get_entity(bot)
                    if not getattr(ent, "bot", False):
                        await event.reply("🤖 错误: 目标必须是机器人账号（Bot），不能是频道/群/普通用户")
                        return
                    exists = next((m for m in bot_mappings if str(m["source_chat"]) == str(src)), None)
                    if exists:
                        if exists["target_bot"] == bot:
                            await event.reply(f"🤖 '{src}' 已经在监听列表中了。")
                        else:
                            new_map = [m for m in bot_mappings if str(m["source_chat"]) != str(src)]
                            new_map.append({"source_chat": src, "target_bot": bot})
                            update_config_file(new_map)
                            await event.reply(f"🤖 更新成功: {src} -> {bot}")
                    else:
                        new_map = bot_mappings + [{"source_chat": src, "target_bot": bot}]
                        update_config_file(new_map)
                        await event.reply(f"🤖 添加成功: {src} -> {bot}")
                except Exception as exc:  # noqa: BLE001
                    await event.reply(f"🤖 操作失败: {exc}")
            else:
                await event.reply("🤖 用法: /add_listen <源ID> <@目标机器人>")

        elif cmd == "/remove_listen":
            if args:
                new_map = [m for m in bot_mappings if str(m["source_chat"]) != str(args)]
                if len(new_map) < len(bot_mappings):
                    update_config_file(new_map)
                    await event.reply(f"🤖 已移除监听: {args}")
                else:
                    await event.reply(f"🤖 '{args}' 不在列表中。")
            else:
                await event.reply("🤖 用法: /remove_listen <源ID>")

        elif cmd == "/list_listen":
            if bot_mappings:
                info = "\n".join([f"{m['source_chat']} -> {m['target_bot']}" for m in bot_mappings])
                await event.reply(f"🤖 当前监听:\n{info}")
            else:
                await event.reply("🤖 当前列表为空。")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
