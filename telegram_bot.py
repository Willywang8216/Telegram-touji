import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from telethon import types
from telethon.events import NewMessage
from telethon.sync import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest

from telethon_spam import group_looks_like_promo_directory, message_looks_like_promo_directory

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
forwarding_map: dict[int, dict] = {}
bot_mappings = settings["bot_mappings"]
rate_limiter = AsyncRateLimiter(rate_per_sec=8)
DLQ_PATH = "logs/userbot_dlq.jsonl"

SRC_MARKER_PREFIX = "[[SRC:"
SRC_MARKER_SUFFIX = "]]"


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

            peer_id = client.get_peer_id(source_entity)
            forwarding_map[int(peer_id)] = {"target_bot": target_entity, "noforwards": bool(mapping.get("noforwards"))}
            log_event(logger, logging.INFO, "mapping_updated", source_chat=str(source_chat), target_bot=str(target_bot))
        except Exception as exc:  # noqa: BLE001
            log_event(logger, logging.ERROR, "mapping_failed", source_chat=str(source_chat), error=str(exc))


def _is_protected_forward_error(exc: Exception) -> bool:
    return "protected chat" in str(exc).lower()


def _is_invalid_message_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "message id is invalid" in msg or "specified message id is invalid" in msg


async def _reupload_single_to_bot(target_bot, msg, caption: str) -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="touji_media_"))
    try:
        path = await client.download_media(msg, file=str(tmp_dir))
        if path:
            await client.send_file(target_bot, str(path), caption=caption)
        else:
            await client.send_message(target_bot, caption)
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


async def _reupload_group_to_bot(target_bot, msgs: list, caption: str) -> None:
    tmp_dir = Path(tempfile.mkdtemp(prefix="touji_album_"))
    paths: list[str] = []

    try:
        for m in msgs:
            if getattr(m, "media", None) is None:
                continue
            p = await client.download_media(m, file=str(tmp_dir))
            if p:
                paths.append(str(p))

        if paths:
            await client.send_file(target_bot, paths, caption=caption)
        else:
            await client.send_message(target_bot, caption)
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


def _src_marker(peer_id: int) -> str:
    return f"{SRC_MARKER_PREFIX}{peer_id}{SRC_MARKER_SUFFIX}"


async def _copy_single_to_bot(target_bot, message_id: int, chat_id: int, force_reupload: bool = False):
    msg = await client.get_messages(chat_id, ids=message_id)
    caption = _src_marker(chat_id)
    if getattr(msg, "message", None):
        caption = f"{caption} {msg.message}"

    await rate_limiter.wait()
    if msg.media is not None:
        if force_reupload:
            await _reupload_single_to_bot(target_bot, msg, caption)
            return

        try:
            await client.send_file(target_bot, msg.media, caption=caption)
        except Exception as exc:  # noqa: BLE001
            if _is_protected_forward_error(exc):
                await _reupload_single_to_bot(target_bot, msg, caption)
            else:
                raise
    else:
        await with_retry(
            lambda: client.send_message(target_bot, caption),
            retries=3,
            base_delay=1,
            logger=logger,
            action="copy_single_send_message",
        )


async def _copy_group_to_bot(target_bot, message_ids: list[int], chat_id: int) -> None:
    msgs = await client.get_messages(int(chat_id), ids=[int(x) for x in message_ids])

    if msgs is None:
        msg_list: list = []
    elif isinstance(msgs, list):
        msg_list = [m for m in msgs if m]
    else:
        msg_list = [msgs]

    caption = _src_marker(int(chat_id))
    first_text = next((getattr(m, "message", None) for m in msg_list if getattr(m, "message", None)), None)
    if first_text:
        caption = f"{caption} {first_text}"

    await rate_limiter.wait()
    await _reupload_group_to_bot(target_bot, msg_list, caption)


async def safe_forward_single(target_bot, message_id, chat_id, noforwards: bool = False):
    if noforwards:
        try:
            await _copy_single_to_bot(target_bot, int(message_id), int(chat_id), force_reupload=True)
            log_event(logger, logging.INFO, "message_copied", chat_id=str(chat_id), message_id=message_id, method="reupload")
        except Exception as copy_exc:  # noqa: BLE001
            if _is_protected_forward_error(copy_exc):
                log_event(
                    logger,
                    logging.WARNING,
                    "message_skipped_protected",
                    chat_id=str(chat_id),
                    message_id=message_id,
                    error=str(copy_exc),
                )
                return

            payload = {"chat_id": chat_id, "message_id": message_id, "error": str(copy_exc)}
            write_dlq(DLQ_PATH, payload)
            log_event(logger, logging.ERROR, "message_copy_failed", **payload)
        return

    await rate_limiter.wait()
    try:
        await client.forward_messages(target_bot, message_id, from_peer=chat_id)
        log_event(logger, logging.INFO, "message_forwarded", chat_id=str(chat_id), message_id=message_id)
    except Exception as exc:  # noqa: BLE001
        if _is_invalid_message_error(exc):
            log_event(
                logger,
                logging.WARNING,
                "message_skipped_invalid",
                chat_id=str(chat_id),
                message_id=message_id,
                error=str(exc),
            )
            return

        if _is_protected_forward_error(exc):
            try:
                await _copy_single_to_bot(target_bot, int(message_id), int(chat_id), force_reupload=False)
                log_event(logger, logging.INFO, "message_copied", chat_id=str(chat_id), message_id=message_id, method="fallback")
                return
            except Exception as copy_exc:  # noqa: BLE001
                if _is_protected_forward_error(copy_exc):
                    log_event(
                        logger,
                        logging.WARNING,
                        "message_skipped_protected",
                        chat_id=str(chat_id),
                        message_id=message_id,
                        error=str(copy_exc),
                    )
                    return

                payload = {"chat_id": chat_id, "message_id": message_id, "error": str(copy_exc)}
                write_dlq(DLQ_PATH, payload)
                log_event(logger, logging.ERROR, "message_copy_failed", **payload)
                return

        payload = {"chat_id": chat_id, "message_id": message_id, "error": str(exc)}
        write_dlq(DLQ_PATH, payload)
        log_event(logger, logging.ERROR, "message_forward_failed", **payload)


@client.on(NewMessage())
async def handler(event):
    if config_manager.reload_if_changed():
        global bot_mappings
        bot_mappings = load_userbot_settings(config_manager)["bot_mappings"]
        await rebuild_forwarding_map()
        log_event(logger, logging.INFO, "config_hot_reloaded")

    if event.chat_id in forwarding_map:
        mapping = forwarding_map[event.chat_id]
        target_bot = mapping["target_bot"]
        noforwards = bool(mapping.get("noforwards"))

        msg = event.message
        if getattr(msg, "action", None) is not None:
            return

        # Only forward real media. This avoids directory/promoting spam (often hidden as text links).
        media = getattr(msg, "media", None)
        if isinstance(media, types.MessageMediaWebPage):
            media = None

        if msg.grouped_id:
            async with media_group_lock:
                key = (event.chat_id, event.message.grouped_id)
                if key not in media_group_cache:
                    media_group_cache[key] = {"messages": [], "task": None, "target_bot": target_bot, "noforwards": noforwards}
                media_group_cache[key]["messages"].append(event.message.id)
                if media_group_cache[key]["task"]:
                    media_group_cache[key]["task"].cancel()
                media_group_cache[key]["task"] = asyncio.create_task(process_media_group(key))
        else:
            await safe_forward_single(target_bot, event.message.id, event.chat_id, noforwards=noforwards)


async def process_media_group(key: tuple[int, int]):
    from_peer, grouped_id = key
    await asyncio.sleep(1.5)

    async with media_group_lock:
        data = media_group_cache.get(key)
        if not data:
            return
        # Remove first so we never double-send even if forwarding throws.
        del media_group_cache[key]

    if data.get("noforwards"):
        try:
            await _copy_group_to_bot(data["target_bot"], data["messages"], int(from_peer))
            log_event(logger, logging.INFO, "group_copied", group_id=grouped_id, count=len(data["messages"]), method="reupload")
        except Exception as exc:  # noqa: BLE001
            payload = {"group_id": grouped_id, "messages": data["messages"], "error": str(exc)}
            write_dlq(DLQ_PATH, payload)
            log_event(logger, logging.ERROR, "group_copy_failed", **payload)
        return

    try:
        await rate_limiter.wait()
        await with_retry(
            lambda: client.forward_messages(data["target_bot"], data["messages"], from_peer=from_peer),
            retries=3,
            base_delay=1,
            logger=logger,
            action="forward_group",
        )
        log_event(logger, logging.INFO, "group_forwarded", group_id=grouped_id, count=len(data["messages"]))
    except Exception as exc:  # noqa: BLE001
        if _is_invalid_message_error(exc):
            log_event(logger, logging.WARNING, "group_skipped_invalid", group_id=grouped_id, error=str(exc))
            return

        if _is_protected_forward_error(exc):
            try:
                await _copy_group_to_bot(data["target_bot"], data["messages"], int(from_peer))
                log_event(logger, logging.INFO, "group_copied", group_id=grouped_id, count=len(data["messages"]), method="fallback")
                return
            except Exception as copy_exc:  # noqa: BLE001
                payload = {"group_id": grouped_id, "messages": data["messages"], "error": str(copy_exc)}
                write_dlq(DLQ_PATH, payload)
                log_event(logger, logging.ERROR, "group_copy_failed", **payload)
                return

        payload = {"group_id": grouped_id, "messages": data["messages"], "error": str(exc)}
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
