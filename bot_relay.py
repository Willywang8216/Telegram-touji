import asyncio
import logging
from typing import Any

from telethon import TelegramClient, events

from common_config import ConfigManager, load_relay_settings
from delivery import AsyncRateLimiter, with_retry, write_dlq
from structured_logger import get_logger, log_event

DLQ_PATH = "logs/relay_dlq.jsonl"


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


class RelayBot:
    def __init__(
        self,
        client: TelegramClient,
        config_manager: ConfigManager,
        settings: dict[str, Any],
        *,
        rate_limiter: AsyncRateLimiter | None = None,
        dlq_path: str = DLQ_PATH,
        logger=None,
    ):
        self.client = client
        self.config_manager = config_manager
        self.settings = settings
        self.rate_limiter = rate_limiter or AsyncRateLimiter(rate_per_sec=8)
        self.dlq_path = dlq_path
        self.logger = logger or get_logger("relaybot")

        # Keyed by (chat_id, grouped_id) to avoid collisions across different private senders.
        self.media_group_cache: dict[tuple[int, int], dict[str, Any]] = {}
        self.media_group_lock = asyncio.Lock()

    def current_dest_channels(self) -> list[int]:
        if self.config_manager.reload_if_changed():
            # Hot reload destinations (and optional sender restriction)
            self.settings = load_relay_settings(self.config_manager)
            log_event(
                self.logger,
                logging.INFO,
                "config_hot_reloaded",
                dest_channels=self.settings["dest_channels"],
                master_account_id=self.settings.get("master_account_id", 0),
            )
        return self.settings["dest_channels"]

    async def handle(self, event) -> None:
        # Optional safety: only accept DMs from a specific user id.
        allowed_sender = int(self.settings.get("master_account_id", 0) or 0)
        if allowed_sender and getattr(event, "sender_id", None) != allowed_sender:
            log_event(
                self.logger,
                logging.INFO,
                "unauthorized_sender_blocked",
                sender_id=getattr(event, "sender_id", None),
            )
            return

        sender = await event.get_sender()
        if sender and getattr(sender, "is_self", False):
            return

        stripped_text = (getattr(event, "raw_text", "") or "").strip()
        if stripped_text.startswith("/"):
            log_event(self.logger, logging.INFO, "command_blocked", text=stripped_text)
            return
        if stripped_text.startswith("🤖"):
            log_event(self.logger, logging.INFO, "system_reply_blocked", text=stripped_text)
            return

        msg = event.message
        if msg.grouped_id:
            async with self.media_group_lock:
                key = (event.chat_id, msg.grouped_id)
                if key not in self.media_group_cache:
                    self.media_group_cache[key] = {"messages": [], "task": None}
                self.media_group_cache[key]["messages"].append(msg)
                task = self.media_group_cache[key].get("task")
                if task:
                    task.cancel()
                self.media_group_cache[key]["task"] = asyncio.create_task(self.process_media_group(key))
            log_event(self.logger, logging.INFO, "album_cached", group_id=msg.grouped_id)
        else:
            await self.send_copy(msg)

    async def process_media_group(self, key: tuple[int, int]) -> None:
        try:
            await asyncio.sleep(2)
            async with self.media_group_lock:
                if key not in self.media_group_cache:
                    return
                msgs = self.media_group_cache[key]["messages"]
                del self.media_group_cache[key]

            msgs.sort(key=lambda x: x.id)
            caption = next(
                (
                    (getattr(m, "raw_text", None) or getattr(m, "text", None))
                    for m in msgs
                    if (getattr(m, "raw_text", None) or getattr(m, "text", None))
                ),
                None,
            )
            files = [m.media for m in msgs if getattr(m, "media", None)]
            _, gid = key

            if not files:
                log_event(self.logger, logging.INFO, "album_skipped_no_media", group_id=gid)
                return

            for cid in self.current_dest_channels():
                await self.rate_limiter.wait()
                try:
                    await with_retry(
                        lambda: self.client.send_file(cid, files, caption=caption),
                        retries=3,
                        base_delay=1,
                        logger=self.logger,
                        action="send_album",
                    )
                    log_event(self.logger, logging.INFO, "album_sent", channel_id=cid, group_id=gid)
                except Exception as exc:  # noqa: BLE001
                    payload = {"channel_id": cid, "group_id": gid, "error": str(exc)}
                    write_dlq(self.dlq_path, payload)
                    log_event(self.logger, logging.ERROR, "album_send_failed", **payload)
        except asyncio.CancelledError:
            return

    async def send_copy(self, msg) -> None:
        text = getattr(msg, "raw_text", "") or ""
        caption = text or None

        for cid in self.current_dest_channels():
            await self.rate_limiter.wait()
            try:
                if getattr(msg, "media", None):
                    await with_retry(
                        lambda: self.client.send_file(cid, msg.media, caption=caption),
                        retries=3,
                        base_delay=1,
                        logger=self.logger,
                        action="send_message",
                    )
                elif text:
                    await with_retry(
                        lambda: self.client.send_message(cid, message=text),
                        retries=3,
                        base_delay=1,
                        logger=self.logger,
                        action="send_message",
                    )
                else:
                    log_event(self.logger, logging.INFO, "message_skipped", channel_id=cid, message_id=msg.id)
                    continue

                log_event(self.logger, logging.INFO, "message_sent", channel_id=cid, message_id=msg.id)
            except Exception as exc:  # noqa: BLE001
                payload = {"channel_id": cid, "message_id": msg.id, "error": str(exc)}
                write_dlq(self.dlq_path, payload)
                log_event(self.logger, logging.ERROR, "message_send_failed", **payload)


async def start_relay_client(settings: dict[str, Any], logger) -> TelegramClient:
    # NOTE: session file persists on disk. If it was ever logged-in as a USER (not a bot),
    # Telethon will consider it "authorized" and ignore bot_token.
    client = TelegramClient("bot_session", settings["api_id"], settings["api_hash"])
    await client.connect()

    try:
        authorized = await _maybe_await(client.is_user_authorized())
        if authorized:
            me = await client.get_me()
            if me and not getattr(me, "bot", False):
                log_event(
                    logger,
                    logging.ERROR,
                    "relay_session_is_user",
                    user_id=getattr(me, "id", None),
                    username=getattr(me, "username", None),
                    hint="Delete bot_session.session and restart relaybot so it logs in with bot_token.",
                )
                # Best-effort auto-fix: log out and re-login as bot.
                try:
                    await client.log_out()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        # If anything goes wrong, we still try bot login below.
        pass

    await client.start(bot_token=settings["bot_token"])
    me = await client.get_me()
    if not me or not getattr(me, "bot", False):
        raise RuntimeError(
            "Relaybot did not authenticate as a bot. Remove bot_session.session and restart, "
            "and ensure RELAY_BOT_TOKEN is correct."
        )
    return client


async def main() -> None:
    logger = get_logger("relaybot")
    config_manager = ConfigManager()
    settings = load_relay_settings(config_manager)

    client = await start_relay_client(settings, logger)
    bot = RelayBot(client, config_manager, settings, logger=logger)

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def handler(event):
        await bot.handle(event)

    log_event(
        logger,
        logging.INFO,
        "relaybot_started",
        dest_channels=settings["dest_channels"],
        master_account_id=settings.get("master_account_id", 0),
    )
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
