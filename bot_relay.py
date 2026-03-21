import asyncio
import logging
import tempfile
from dataclasses import dataclass
from typing import Any, Callable

from telethon import TelegramClient, events, functions, types, utils

from common_config import ConfigManager, load_relay_settings
from delivery import AsyncRateLimiter, with_retry, write_dlq
from twitter_expand import download_tweet_media, extract_tweet_urls
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

DLQ_PATH = "logs/relay_dlq.jsonl"
MEDIA_CAPTION_LIMIT = 1024


@dataclass
class ExpandedMedia:
    files: list[Any]
    cleanup: Callable[[], None]
    url: str


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


def _extract_forward_source_chat_id(msg) -> int | None:
    fwd = getattr(msg, "fwd_from", None) or getattr(msg, "forward", None)
    if not fwd:
        return None

    from_id = getattr(fwd, "from_id", None)
    if from_id is not None:
        try:
            return utils.get_peer_id(from_id)
        except Exception:  # noqa: BLE001
            pass

    channel_id = getattr(fwd, "channel_id", None)
    if channel_id is not None:
        try:
            return utils.get_peer_id(types.PeerChannel(int(channel_id)))
        except Exception:  # noqa: BLE001
            pass

    return None


class ForumTopicResolver:
    def __init__(self, client: TelegramClient, logger):
        self.client = client
        self.logger = logger
        self._cache: dict[int, dict[str, int]] = {}

    async def get_or_create_top_message_id(self, chat_id: int, title: str) -> int | None:
        chat_cache = self._cache.setdefault(chat_id, {})
        if title in chat_cache:
            return chat_cache[title]

        topic = await self.find_topic(chat_id, title)
        if topic is None:
            await self._create_topic(chat_id, title)
            topic = await self.find_topic(chat_id, title)

        top = int(topic["top_message"]) if topic else None
        if top is not None:
            chat_cache[title] = top
        return top

    async def clear_cache(self, chat_id: int) -> None:
        self._cache.pop(int(chat_id), None)

    async def find_topic(self, chat_id: int, title: str) -> dict[str, int] | None:
        try:
            peer = await self.client.get_input_entity(chat_id)
            res = await self.client(
                functions.messages.GetForumTopicsRequest(
                    peer=peer,
                    q=title,
                    offset_date=0,
                    offset_id=0,
                    offset_topic=0,
                    limit=100,
                )
            )
            for t in getattr(res, "topics", []) or []:
                if getattr(t, "title", None) == title:
                    topic_id = int(getattr(t, "id", 0) or 0)
                    top_message = int(getattr(t, "top_message", 0) or 0)
                    if topic_id and top_message:
                        return {"topic_id": topic_id, "top_message": top_message}
        except Exception as exc:  # noqa: BLE001
            log_event(
                self.logger,
                logging.INFO,
                "forum_topics_list_failed",
                chat_id=chat_id,
                title=title,
                error=f"{type(exc).__name__}: {exc}",
            )
        return None

    async def _create_topic(self, chat_id: int, title: str) -> None:
        try:
            peer = await self.client.get_input_entity(chat_id)
            await self.client(
                functions.messages.CreateForumTopicRequest(
                    peer=peer,
                    title=title,
                )
            )
            log_event(self.logger, logging.INFO, "forum_topic_created", chat_id=chat_id, title=title)
        except Exception as exc:  # noqa: BLE001
            log_event(
                self.logger,
                logging.INFO,
                "forum_topic_create_failed",
                chat_id=chat_id,
                title=title,
                error=f"{type(exc).__name__}: {exc}",
            )


class RelayBot:
    def __init__(
        self,
        client: TelegramClient,
        config_manager: ConfigManager,
        settings: dict[str, Any],
        *,
        tweet_resolver=None,
        rate_limiter: AsyncRateLimiter | None = None,
        dlq_path: str = DLQ_PATH,
        logger=None,
    ):
        self.client = client
        self.config_manager = config_manager
        self.settings = settings
        self.tweet_resolver = tweet_resolver
        self.rate_limiter = rate_limiter or AsyncRateLimiter(rate_per_sec=8)
        self.dlq_path = dlq_path
        self.logger = logger or get_logger("relaybot")
        self.topic_resolver = ForumTopicResolver(client, self.logger)

        # Keyed by (chat_id, grouped_id) to avoid collisions across different private senders.
        self.media_group_cache: dict[tuple[int, int], dict[str, Any]] = {}
        self.media_group_lock = asyncio.Lock()

    def current_settings(self) -> dict[str, Any]:
        if self.config_manager.reload_if_changed():
            self.settings = load_relay_settings(self.config_manager)
            log_event(
                self.logger,
                logging.INFO,
                "config_hot_reloaded",
                dest_channels=self.settings["dest_channels"],
                master_account_id=self.settings.get("master_account_id", 0),
                routes=len(self.settings.get("routes", []) or []),
            )
        return self.settings

    def resolve_destinations(self, source_chat_id: int | None, *, seed: int | None = None) -> list[dict[str, Any]]:
        settings = self.current_settings()
        source_chat_id = int(source_chat_id or 0)

        for r in settings.get("routes", []) or []:
            if source_chat_id in (r.get("source_chats") or []):
                return list(r.get("destinations") or [])

        if settings.get("distribute_unrouted_to_buckets") and settings.get("general_topic_buckets"):
            buckets: dict[int, list[str]] = settings.get("general_topic_buckets", {}) or {}

            mode = (settings.get("unrouted_distribution_mode") or "source").strip().lower()
            if mode == "message" and seed is not None:
                idx_seed = abs(int(seed))
            else:
                idx_seed = abs(source_chat_id) if source_chat_id else 0

            out: list[dict[str, Any]] = []
            for cid in settings.get("dest_channels", []) or []:
                topics = buckets.get(int(cid)) or []
                if topics:
                    out.append({"chat_id": int(cid), "topic_title": topics[idx_seed % len(topics)]})
                else:
                    out.append({"chat_id": int(cid)})
            return out

        if settings.get("default_destinations"):
            return list(settings.get("default_destinations") or [])

        return [{"chat_id": int(x)} for x in settings.get("dest_channels", [])]

    async def sync_forum_topics(self) -> None:
        settings = self.current_settings()

        required: dict[int, set[str]] = {}

        def add(chat_id: int, title: str | None) -> None:
            if not title:
                return
            required.setdefault(int(chat_id), set()).add(str(title))

        for item in settings.get("ensure_forum_topics", []) or []:
            if not isinstance(item, dict) or "chat_id" not in item:
                continue
            cid = int(item["chat_id"])
            for t in item.get("topics", []) or []:
                add(cid, t)

        for cid, topics in (settings.get("general_topic_buckets") or {}).items():
            for t in topics or []:
                add(int(cid), t)

        for r in settings.get("routes", []) or []:
            for d in r.get("destinations", []) or []:
                add(int(d.get("chat_id")), d.get("topic_title"))

        for d in settings.get("default_destinations", []) or []:
            add(int(d.get("chat_id")), d.get("topic_title"))

        for cid, title in (settings.get("fallback_topic_titles") or {}).items():
            add(int(cid), title)

        for chat_id, renames in (settings.get("topic_renames") or {}).items():
            if not isinstance(renames, dict):
                continue
            for old, new in renames.items():
                if not old or not new or old == new:
                    continue

                old_topic = await self.topic_resolver.find_topic(int(chat_id), str(old))
                if not old_topic:
                    continue
                new_topic = await self.topic_resolver.find_topic(int(chat_id), str(new))
                if new_topic:
                    continue

                try:
                    peer = await self.client.get_input_entity(int(chat_id))
                    await self.client(
                        functions.messages.EditForumTopicRequest(
                            peer=peer,
                            topic_id=int(old_topic["topic_id"]),
                            title=str(new),
                        )
                    )
                    await self.topic_resolver.clear_cache(int(chat_id))
                    log_event(self.logger, logging.INFO, "forum_topic_renamed", chat_id=int(chat_id), old=str(old), new=str(new))
                except Exception as exc:  # noqa: BLE001
                    log_event(
                        self.logger,
                        logging.INFO,
                        "forum_topic_rename_failed",
                        chat_id=int(chat_id),
                        old=str(old),
                        new=str(new),
                        error=f"{type(exc).__name__}: {exc}",
                    )

        for chat_id, titles in (settings.get("topic_deletes") or {}).items():
            for title in titles or []:
                topic = await self.topic_resolver.find_topic(int(chat_id), str(title))
                if not topic:
                    continue
                try:
                    peer = await self.client.get_input_entity(int(chat_id))
                    await self.client(
                        functions.messages.DeleteTopicHistoryRequest(
                            peer=peer,
                            top_msg_id=int(topic["top_message"]),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    log_event(
                        self.logger,
                        logging.INFO,
                        "forum_topic_delete_history_failed",
                        chat_id=int(chat_id),
                        title=str(title),
                        error=f"{type(exc).__name__}: {exc}",
                    )

                try:
                    peer = await self.client.get_input_entity(int(chat_id))
                    await self.client(
                        functions.messages.EditForumTopicRequest(
                            peer=peer,
                            topic_id=int(topic["topic_id"]),
                            hidden=True,
                            closed=True,
                        )
                    )
                    await self.topic_resolver.clear_cache(int(chat_id))
                    log_event(self.logger, logging.INFO, "forum_topic_hidden", chat_id=int(chat_id), title=str(title))
                except Exception as exc:  # noqa: BLE001
                    log_event(
                        self.logger,
                        logging.INFO,
                        "forum_topic_hide_failed",
                        chat_id=int(chat_id),
                        title=str(title),
                        error=f"{type(exc).__name__}: {exc}",
                    )

        for chat_id, titles in required.items():
            for title in sorted(titles):
                await self.topic_resolver.get_or_create_top_message_id(int(chat_id), str(title))

    def _post_caption_for(self, chat_id: int) -> str | None:
        captions = self.current_settings().get("post_captions", {}) or {}
        return captions.get(int(chat_id))

    def _is_blocked(self, text: str) -> bool:
        for s in self.current_settings().get("blocklist_substrings", []) or []:
            if s and s in text:
                return True
        return False

    async def _maybe_expand_twitter_media(self, original_text: str) -> ExpandedMedia | None:
        settings = self.current_settings()
        if not settings.get("expand_twitter_links", True):
            return None

        tweet_urls = extract_tweet_urls(original_text)
        if not tweet_urls:
            return None

        url = tweet_urls[0]
        tmp = tempfile.TemporaryDirectory(prefix="relaybot_tweet_")

        cookies_file = settings.get("twitter_cookies_file")
        max_files = int(settings.get("twitter_max_media_files", 8) or 8)

        try:
            if self.tweet_resolver is not None:
                resolved = await _maybe_await(self.tweet_resolver.resolve(url))
                if resolved:
                    return ExpandedMedia(files=list(resolved), cleanup=tmp.cleanup, url=url)
                tmp.cleanup()
                return None

            files = await asyncio.to_thread(
                download_tweet_media,
                url,
                tmp.name,
                cookies_file=cookies_file,
                max_files=max_files,
                logger=self.logger,
            )
            if not files:
                tmp.cleanup()
                return None

            return ExpandedMedia(files=[str(p) for p in files], cleanup=tmp.cleanup, url=url)
        except Exception as exc:  # noqa: BLE001
            tmp.cleanup()
            log_event(
                self.logger,
                logging.INFO,
                "tweet_media_download_failed",
                url=url,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None

    async def handle(self, event) -> None:
        settings = self.current_settings()

        allowed_sender = int(settings.get("master_account_id", 0) or 0)
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
                    source_chat_id = _extract_forward_source_chat_id(msg)
                    self.media_group_cache[key] = {"messages": [], "task": None, "source_chat_id": source_chat_id}
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
                source_chat_id = self.media_group_cache[key].get("source_chat_id")
                msgs = self.media_group_cache[key]["messages"]
                del self.media_group_cache[key]

            msgs.sort(key=lambda x: x.id)

            caption = next(
                (
                    (getattr(m, "raw_text", None) or getattr(m, "text", None) or "")
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

            if self.current_settings().get("strip_text"):
                caption = None

            for dest in self.resolve_destinations(source_chat_id, seed=gid):
                chat_id = int(dest["chat_id"])
                topic_title = dest.get("topic_title")

                post_caption = self._post_caption_for(chat_id)
                full_caption = caption
                if post_caption:
                    full_caption = (full_caption or "") + ("\n\n" if full_caption else "") + post_caption

                if full_caption and self._is_blocked(full_caption):
                    log_event(self.logger, logging.INFO, "message_blocked", chat_id=chat_id, group_id=gid)
                    continue

                reply_to = None
                if topic_title:
                    reply_to = await self.topic_resolver.get_or_create_top_message_id(chat_id, str(topic_title))

                if reply_to is None and self.current_settings().get("fallback_to_general_topic"):
                    fallback = (self.current_settings().get("fallback_topic_titles", {}) or {}).get(chat_id)
                    if fallback:
                        reply_to = await self.topic_resolver.get_or_create_top_message_id(chat_id, str(fallback))

                await self.rate_limiter.wait()
                try:
                    await with_retry(
                        lambda: self.client.send_file(chat_id, files, caption=self._trim_caption(full_caption), reply_to=reply_to),
                        retries=3,
                        base_delay=1,
                        logger=self.logger,
                        action="send_album",
                    )
                    log_event(
                        self.logger,
                        logging.INFO,
                        "album_sent",
                        chat_id=chat_id,
                        topic_title=str(topic_title) if topic_title else None,
                        group_id=gid,
                        source_chat_id=source_chat_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    payload = {
                        "chat_id": chat_id,
                        "topic_title": str(topic_title) if topic_title else None,
                        "group_id": gid,
                        "source_chat_id": source_chat_id,
                        "error": str(exc),
                    }
                    write_dlq(self.dlq_path, payload)
                    log_event(self.logger, logging.ERROR, "album_send_failed", **payload)
        except asyncio.CancelledError:
            return

    def _trim_caption(self, text: str | None) -> str | None:
        if not text:
            return None
        s = str(text)
        if len(s) <= MEDIA_CAPTION_LIMIT:
            return s
        return s[: MEDIA_CAPTION_LIMIT - 1] + "…"

    async def send_copy(self, msg) -> None:
        source_chat_id = _extract_forward_source_chat_id(msg)
        original_text = getattr(msg, "raw_text", "") or ""

        # We use the original text for tweet URL detection even if strip_text is enabled.
        display_text = original_text
        if self.current_settings().get("strip_text"):
            display_text = ""

        media = getattr(msg, "media", None)
        uploadable_media = media is not None and not isinstance(media, types.MessageMediaWebPage)

        expanded: ExpandedMedia | None = None
        if not uploadable_media:
            expanded = await self._maybe_expand_twitter_media(original_text)

        try:
            for dest in self.resolve_destinations(source_chat_id, seed=msg.id):
                chat_id = int(dest["chat_id"])
                topic_title = dest.get("topic_title")

                post_caption = self._post_caption_for(chat_id)
                full_text = display_text
                if post_caption:
                    full_text = (full_text or "") + ("\n\n" if full_text else "") + post_caption

                if full_text and self._is_blocked(full_text):
                    log_event(self.logger, logging.INFO, "message_blocked", chat_id=chat_id, message_id=msg.id)
                    continue

                reply_to = None
                if topic_title:
                    reply_to = await self.topic_resolver.get_or_create_top_message_id(chat_id, str(topic_title))

                if reply_to is None and self.current_settings().get("fallback_to_general_topic"):
                    fallback = (self.current_settings().get("fallback_topic_titles", {}) or {}).get(chat_id)
                    if fallback:
                        reply_to = await self.topic_resolver.get_or_create_top_message_id(chat_id, str(fallback))

                await self.rate_limiter.wait()
                try:
                    if uploadable_media:
                        await with_retry(
                            lambda: self.client.send_file(chat_id, media, caption=self._trim_caption(full_text), reply_to=reply_to),
                            retries=3,
                            base_delay=1,
                            logger=self.logger,
                            action="send_message",
                        )
                    elif expanded is not None:
                        await with_retry(
                            lambda: self.client.send_file(chat_id, expanded.files, caption=self._trim_caption(full_text), reply_to=reply_to),
                            retries=3,
                            base_delay=1,
                            logger=self.logger,
                            action="send_tweet_media",
                        )
                        log_event(self.logger, logging.INFO, "tweet_media_sent", chat_id=chat_id, url=expanded.url)
                    elif full_text:
                        await with_retry(
                            lambda: self.client.send_message(chat_id, message=full_text, reply_to=reply_to),
                            retries=3,
                            base_delay=1,
                            logger=self.logger,
                            action="send_message",
                        )
                    else:
                        log_event(self.logger, logging.INFO, "message_skipped", chat_id=chat_id, message_id=msg.id)
                        continue

                    log_event(
                        self.logger,
                        logging.INFO,
                        "message_sent",
                        chat_id=chat_id,
                        topic_title=str(topic_title) if topic_title else None,
                        message_id=msg.id,
                        source_chat_id=source_chat_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    payload = {
                        "chat_id": chat_id,
                        "topic_title": str(topic_title) if topic_title else None,
                        "message_id": msg.id,
                        "source_chat_id": source_chat_id,
                        "error": str(exc),
                    }
                    write_dlq(self.dlq_path, payload)
                    log_event(self.logger, logging.ERROR, "message_send_failed", **payload)
        finally:
            if expanded is not None:
                try:
                    expanded.cleanup()
                except Exception:  # noqa: BLE001
                    pass


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

    await bot.sync_forum_topics()

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
