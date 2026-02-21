import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from telethon.events import NewMessage
from telethon.sync import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest

from command_utils import parse_command
from common_config import ConfigManager, load_userbot_settings
from delivery import AsyncRateLimiter, with_retry, write_dlq
from structured_logger import get_logger, log_event

logger = get_logger("userbot")
config_manager = ConfigManager()
settings = load_userbot_settings(config_manager)

media_group_cache = {}
media_group_lock = asyncio.Lock()

client = TelegramClient("anon", settings["api_id"], settings["api_hash"], proxy=settings["proxy"])
forwarding_map = {}
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
            peer_id = await client.get_peer_id(source_entity)
            forwarding_map[peer_id] = target_entity
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


async def _copy_single_to_bot(target_bot, message_id: int, chat_id: int):
    msg = await client.get_messages(chat_id, ids=message_id)
    caption = _src_marker(chat_id)
    if getattr(msg, "message", None):
        caption = f"{caption} {msg.message}"

    await rate_limiter.wait()
    if msg.media is not None:
        try:
            await with_retry(
                lambda: client.send_file(target_bot, msg.media, caption=caption),
                retries=3,
                base_delay=1,
                logger=logger,
                action="copy_single_send_file",
            )
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


async def safe_forward_single(target_bot, message_id, chat_id):
    await rate_limiter.wait()
    try:
        await with_retry(
            lambda: client.forward_messages(target_bot, message_id, from_peer=chat_id),
            retries=3,
            base_delay=1,
            logger=logger,
            action="forward_single",
        )
        log_event(logger, logging.INFO, "message_forwarded", chat_id=str(chat_id), message_id=message_id)
    except Exception as exc:  # noqa: BLE001
        if _is_invalid_message_error(exc):
            log_event(logger, logging.WARNING, "message_skipped_invalid", chat_id=str(chat_id), message_id=message_id, error=str(exc))
            return

        if _is_protected_forward_error(exc):
            try:
                await _copy_single_to_bot(target_bot, int(message_id), int(chat_id))
                log_event(logger, logging.INFO, "message_copied", chat_id=str(chat_id), message_id=message_id)
                return
            except Exception as copy_exc:  # noqa: BLE001
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
        target_bot = forwarding_map[event.chat_id]
        if event.message.grouped_id:
            async with media_group_lock:
                gid = event.message.grouped_id
                if gid not in media_group_cache:
                    media_group_cache[gid] = {"messages": [], "task": None, "target_bot": target_bot}
                media_group_cache[gid]["messages"].append(event.message.id)
                if media_group_cache[gid]["task"]:
                    media_group_cache[gid]["task"].cancel()
                media_group_cache[gid]["task"] = asyncio.create_task(process_media_group(gid, event.chat_id))
        else:
            await safe_forward_single(target_bot, event.message.id, event.chat_id)


async def _copy_group_to_bot(target_bot, message_ids: list[int], chat_id: int):
    msgs = await client.get_messages(chat_id, ids=message_ids)
    msgs = [m for m in (list(msgs) if msgs else []) if m]
    msgs.sort(key=lambda m: m.id)

    files = [m.media for m in msgs if m.media is not None]
    original_caption = next((m.message for m in msgs if getattr(m, "message", None)), "")
    caption = _src_marker(chat_id)
    if original_caption:
        caption = f"{caption} {original_caption}"

    await rate_limiter.wait()
    if files:
        try:
            await with_retry(
                lambda: client.send_file(target_bot, files, caption=caption),
                retries=3,
                base_delay=1,
                logger=logger,
                action="copy_group_send_file",
            )
        except Exception as exc:  # noqa: BLE001
            if _is_protected_forward_error(exc):
                await _reupload_group_to_bot(target_bot, msgs, caption)
            else:
                raise
    else:
        await with_retry(
            lambda: client.send_message(target_bot, caption),
            retries=3,
            base_delay=1,
            logger=logger,
            action="copy_group_send_message",
        )


async def process_media_group(grouped_id, from_peer):
    await asyncio.sleep(1.5)
    async with media_group_lock:
        if grouped_id in media_group_cache:
            data = media_group_cache[grouped_id]
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
                    try:
                        msgs = await client.get_messages(int(from_peer), ids=[int(x) for x in data["messages"]])
                        msgs = [m for m in (list(msgs) if msgs else []) if m]
                        ids = [int(m.id) for m in msgs]
                        if ids:
                            await with_retry(
                                lambda: client.forward_messages(data["target_bot"], ids, from_peer=from_peer),
                                retries=3,
                                base_delay=1,
                                logger=logger,
                                action="forward_group_partial",
                            )
                            log_event(logger, logging.INFO, "group_forwarded_partial", group_id=grouped_id, count=len(ids))
                            return
                    except Exception as retry_exc:  # noqa: BLE001
                        payload = {"group_id": grouped_id, "messages": data["messages"], "error": str(retry_exc)}
                        write_dlq(DLQ_PATH, payload)
                        log_event(logger, logging.ERROR, "group_forward_failed", **payload)
                        return

                if _is_protected_forward_error(exc):
                    try:
                        await _copy_group_to_bot(data["target_bot"], [int(x) for x in data["messages"]], int(from_peer))
                        log_event(logger, logging.INFO, "group_copied", group_id=grouped_id, count=len(data["messages"]))
                    except Exception as copy_exc:  # noqa: BLE001
                        payload = {"group_id": grouped_id, "messages": data["messages"], "error": str(copy_exc)}
                        write_dlq(DLQ_PATH, payload)
                        log_event(logger, logging.ERROR, "group_copy_failed", **payload)
                else:
                    payload = {"group_id": grouped_id, "messages": data["messages"], "error": str(exc)}
                    write_dlq(DLQ_PATH, payload)
                    log_event(logger, logging.ERROR, "group_forward_failed", **payload)
            finally:
                del media_group_cache[grouped_id]


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
                    await client.get_entity(bot)
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
