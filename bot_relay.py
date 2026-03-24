import asyncio
import logging
import re
import shlex
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from telethon import TelegramClient, events, functions, types, utils

from command_utils import parse_command
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
_MAX_LINKS = 3

_IMAGE_FILE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
_VIDEO_FILE_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}

_URL_RE = re.compile(r"(?i)(?:\bhttps?://|\bwww\.|\bt\.me/)\S+")

_TITLE_WS_RE = re.compile(r"\s+")
_EMBEDDED_SOURCE_MARKERS_RE = re.compile(r"^\u2063SRC_CHAT_ID=(-?\d+)\n?(?:\u2063SRC_TOPIC_ID=(\d+)\n?)?")


def _extract_embedded_source_markers_and_strip(text: str) -> tuple[int | None, int | None, str]:
    s = str(text or "")
    m = _EMBEDDED_SOURCE_MARKERS_RE.match(s)
    if not m:
        return None, None, s
    try:
        chat_id = int(m.group(1))
    except Exception:  # noqa: BLE001
        chat_id = None
    try:
        topic_id = int(m.group(2)) if m.group(2) else None
    except Exception:  # noqa: BLE001
        topic_id = None
    return chat_id, topic_id, s[m.end() :]


def normalize_forum_topic_title(value: str) -> str:
    # Prevent duplicate topic creation caused by Unicode/emoji differences such as:
    # - "✋" vs "✋️" (variation selector)
    # - topics created with icon emoji (title without emoji) vs a config title prefixed with emoji
    # - different whitespace
    s = unicodedata.normalize("NFKC", str(value or ""))
    s = s.replace("\ufe0f", "").replace("\ufe0e", "")
    s = _TITLE_WS_RE.sub(" ", s).strip()

    # Strip leading emoji/symbol decorations (keeps normal punctuation like '[' intact).
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isspace():
            i += 1
            continue
        cat = unicodedata.category(ch)
        if cat in {"So", "Sk", "Cf"}:
            i += 1
            continue
        break
    s = s[i:].lstrip()

    return s.casefold()


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
    def __init__(self, client: TelegramClient, logger, *, settings_getter: Callable[[], dict[str, Any]] | None = None):
        self.client = client
        self.logger = logger
        self._settings_getter = settings_getter or (lambda: {})

        # Cache: chat_id -> normalized_title -> {topic_id, top_message}
        self._cache: dict[int, dict[str, dict[str, int]]] = {}

        # Many forum-topic management APIs are restricted for bot accounts.
        # If we detect the API is unavailable, stop retrying on every message.
        self._topics_api_supported: bool | None = None
        self._logged_topics_api_disabled = False

    def _allow_creation(self) -> bool:
        return bool((self._settings_getter() or {}).get("allow_topic_creation", True))

    async def get_or_create_top_message_id(self, chat_id: int, title: str) -> int | None:
        key = normalize_forum_topic_title(title)
        chat_cache = self._cache.setdefault(int(chat_id), {})
        if key in chat_cache:
            return int(chat_cache[key]["top_message"])

        if self._topics_api_supported is False:
            if not self._logged_topics_api_disabled:
                self._logged_topics_api_disabled = True
                log_event(
                    self.logger,
                    logging.INFO,
                    "forum_topics_api_unavailable",
                    hint="This relay is logged in as a bot; resolving forum topics by title is restricted. Populate relay.forum_topic_ids (scripts/export_forum_topic_ids.py) or set destinations[].topic_id.",
                )
            return None

        topic = await self.find_topic(int(chat_id), title)
        if topic is None:
            if not self._allow_creation():
                log_event(self.logger, logging.INFO, "forum_topic_missing_create_disabled", chat_id=int(chat_id), title=str(title))
                return None

            await self._create_topic(int(chat_id), str(title))
            topic = await self.find_topic(int(chat_id), title)

        if topic is not None:
            chat_cache[key] = topic
            return int(topic["top_message"])

        return None

    async def clear_cache(self, chat_id: int) -> None:
        self._cache.pop(int(chat_id), None)

    def _pick_best_topic(self, matches: list[Any], *, chat_id: int, title: str) -> Any | None:
        if not matches:
            return None

        if len(matches) > 1:
            log_event(
                self.logger,
                logging.INFO,
                "forum_topic_duplicates_detected",
                chat_id=int(chat_id),
                title=str(title),
                duplicates=[{"id": int(getattr(t, "id", 0) or 0), "top_message": int(getattr(t, "top_message", 0) or 0), "hidden": bool(getattr(t, "hidden", False)), "pinned": bool(getattr(t, "pinned", False))} for t in matches],
            )

        # Prefer visible + pinned + most active.
        matches.sort(
            key=lambda t: (
                bool(getattr(t, "hidden", False)),
                not bool(getattr(t, "pinned", False)),
                -int(getattr(t, "top_message", 0) or 0),
                int(getattr(t, "id", 0) or 0),
            )
        )
        return matches[0]

    async def _collect_matching_topics(self, peer, *, title: str, q: str | None, max_pages: int) -> list[Any]:
        needle = normalize_forum_topic_title(title)
        matches: list[Any] = []

        offset_topic = 0
        offset_id = 0
        for _ in range(max_pages):
            res = await self.client(
                functions.messages.GetForumTopicsRequest(
                    peer=peer,
                    q=q,
                    offset_date=0,
                    offset_id=int(offset_id),
                    offset_topic=int(offset_topic),
                    limit=100,
                )
            )
            topics = list(getattr(res, "topics", []) or [])
            if not topics:
                break

            for t in topics:
                t_title = getattr(t, "title", None)
                if t_title is None:
                    continue
                if normalize_forum_topic_title(str(t_title)) == needle:
                    matches.append(t)

            last = topics[-1]
            next_offset_topic = int(getattr(last, "id", 0) or 0)
            next_offset_id = int(getattr(last, "top_message", 0) or 0)
            if (next_offset_topic, next_offset_id) == (offset_topic, offset_id):
                break
            offset_topic, offset_id = next_offset_topic, next_offset_id

        return matches

    async def find_topic(self, chat_id: int, title: str) -> dict[str, int] | None:
        if self._topics_api_supported is False:
            return None

        try:
            peer = await self.client.get_input_entity(int(chat_id))

            # First try with Telegram server-side search.
            matches = await self._collect_matching_topics(peer, title=str(title), q=str(title), max_pages=5)
            if not matches:
                # Fallback: full scan (handles cases where q doesn't match emoji/VS16/etc).
                matches = await self._collect_matching_topics(peer, title=str(title), q="", max_pages=20)

            self._topics_api_supported = True

            best = self._pick_best_topic(matches, chat_id=int(chat_id), title=str(title))
            if best is None:
                return None

            topic_id = int(getattr(best, "id", 0) or 0)
            top_message = int(getattr(best, "top_message", 0) or 0)
            if topic_id and top_message:
                return {"topic_id": topic_id, "top_message": top_message}
        except Exception as exc:  # noqa: BLE001
            err_name = type(exc).__name__
            err_text = str(exc)

            # Telethon raises BotMethodInvalidError when a method is blocked for bot accounts.
            if err_name == "BotMethodInvalidError" or "BOT_METHOD_INVALID" in err_text.upper():
                self._topics_api_supported = False

            log_event(
                self.logger,
                logging.INFO,
                "forum_topics_list_failed",
                chat_id=int(chat_id),
                title=str(title),
                error=f"{err_name}: {exc}",
            )
        return None

    async def _create_topic(self, chat_id: int, title: str) -> None:
        if not self._allow_creation():
            log_event(self.logger, logging.INFO, "forum_topic_create_disabled", chat_id=int(chat_id), title=str(title))
            return

        try:
            peer = await self.client.get_input_entity(int(chat_id))
            await self.client(
                functions.messages.CreateForumTopicRequest(
                    peer=peer,
                    title=str(title),
                )
            )
            log_event(self.logger, logging.INFO, "forum_topic_created", chat_id=int(chat_id), title=str(title))
        except Exception as exc:  # noqa: BLE001
            log_event(
                self.logger,
                logging.INFO,
                "forum_topic_create_failed",
                chat_id=int(chat_id),
                title=str(title),
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
        self.topic_resolver = ForumTopicResolver(client, self.logger, settings_getter=self.current_settings)
        self._warned_unresolved_topics: set[tuple[int, str]] = set()
        self._logged_topic_config_warning = False
        self._maybe_log_topic_config_warning(self.settings)

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
            self._maybe_log_topic_config_warning(self.settings)
        return self.settings

    def _maybe_log_topic_config_warning(self, settings: dict[str, Any]) -> None:
        if self._logged_topic_config_warning:
            return

        def has_topic_titles() -> bool:
            for r in settings.get("routes", []) or []:
                for d in r.get("destinations", []) or []:
                    if d.get("topic_title") and d.get("topic_id") is None:
                        return True

            for d in settings.get("default_destinations", []) or []:
                if d.get("topic_title") and d.get("topic_id") is None:
                    return True

            for _, topics in (settings.get("general_topic_buckets") or {}).items():
                if topics:
                    return True

            for _, title in (settings.get("fallback_topic_titles") or {}).items():
                if title:
                    return True

            for item in settings.get("ensure_forum_topics", []) or []:
                if item.get("topics"):
                    return True

            return False

        if not has_topic_titles():
            return

        if settings.get("forum_topic_ids"):
            return

        self._logged_topic_config_warning = True

        require = bool(settings.get("require_forum_topic"))
        allow_create = bool(settings.get("allow_topic_creation", True))

        if not allow_create:
            log_event(
                self.logger,
                logging.INFO,
                "forum_topic_ids_missing",
                require_forum_topic=require,
                hint="Topics are configured via topic_title, but relay.forum_topic_ids is empty and allow_topic_creation=false. Messages will go to the forum's General topic (or be skipped if require_forum_topic=true). Populate relay.forum_topic_ids (scripts/export_forum_topic_ids.py --write) or set destinations[].topic_id.",
            )
        else:
            log_event(
                self.logger,
                logging.INFO,
                "forum_topic_ids_missing",
                require_forum_topic=require,
                hint="Topics are configured via topic_title, but relay.forum_topic_ids is empty. Relay will try to resolve topics by title, but this is commonly restricted for bot accounts. Populate relay.forum_topic_ids (scripts/export_forum_topic_ids.py --write) or set destinations[].topic_id for reliable routing.",
            )

    def resolve_destinations(
        self,
        source_chat_id: int | None,
        *,
        source_topic_id: int | None = None,
        seed: int | None = None,
    ) -> list[dict[str, Any]]:
        settings = self.current_settings()
        source_chat_id = int(source_chat_id or 0)
        source_topic_id = int(source_topic_id) if source_topic_id is not None else None

        routes = settings.get("routes", []) or []

        # Prefer topic-specific routes when we have a source_topic_id.
        if source_topic_id is not None:
            for r in routes:
                if source_chat_id not in (r.get("source_chats") or []):
                    continue
                topics = r.get("source_topics") or []
                if topics and source_topic_id in topics:
                    return list(r.get("destinations") or [])

        # Fall back to chat-level routes.
        for r in routes:
            if source_chat_id in (r.get("source_chats") or []) and not (r.get("source_topics") or []):
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

    async def _resolve_reply_to(self, chat_id: int, topic_title: str | None, *, explicit_topic_id: int | None = None) -> int | None:
        if explicit_topic_id is not None:
            try:
                return int(explicit_topic_id)
            except Exception:  # noqa: BLE001
                return None

        if not topic_title:
            return None

        settings = self.current_settings()
        forum_topic_ids = (settings.get("forum_topic_ids") or {}).get(int(chat_id)) or {}
        key = normalize_forum_topic_title(str(topic_title))
        mapped = forum_topic_ids.get(key)
        if mapped is not None:
            try:
                return int(mapped)
            except Exception:  # noqa: BLE001
                return None

        return await self.topic_resolver.get_or_create_top_message_id(int(chat_id), str(topic_title))

    async def sync_forum_topics(self) -> None:
        settings = self.current_settings()

        if not settings.get("manage_forum_topics", True):
            log_event(self.logger, logging.INFO, "forum_topic_management_disabled")
            return

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

        if settings.get("allow_topic_creation", True):
            for chat_id, titles in required.items():
                for title in sorted(titles):
                    await self.topic_resolver.get_or_create_top_message_id(int(chat_id), str(title))
        else:
            log_event(self.logger, logging.INFO, "forum_topic_creation_disabled_skip_sync")

    def _post_caption_for(self, chat_id: int) -> str | None:
        captions = self.current_settings().get("post_captions", {}) or {}
        return captions.get(int(chat_id))

    def _is_blocked(self, text: str) -> bool:
        hay = str(text or "").casefold()
        for s in self.current_settings().get("blocklist_substrings", []) or []:
            if not s:
                continue
            if str(s).casefold() in hay:
                return True
        return False

    def _count_links(self, text: str | None) -> int:
        if not text:
            return 0
        return len(list(_URL_RE.finditer(str(text))))

    def _has_too_many_links(self, text: str | None) -> bool:
        return self._count_links(text) > _MAX_LINKS

    def _is_video_message(self, msg) -> bool:
        return bool(getattr(msg, "video", None) or getattr(msg, "video_note", None) or getattr(msg, "round_video", None) or getattr(msg, "gif", None))

    def _is_photo_message(self, msg) -> bool:
        if getattr(msg, "photo", None) is not None:
            return True

        doc = getattr(msg, "document", None)
        mime = str(getattr(doc, "mime_type", "") or "")
        if mime.startswith("image/") and not self._is_video_message(msg):
            return True

        return False

    def _looks_like_video_path(self, path: str) -> bool:
        ext = Path(str(path)).suffix.lower()
        return ext in _VIDEO_FILE_EXTS

    def _looks_like_image_path(self, path: str) -> bool:
        ext = Path(str(path)).suffix.lower()
        return ext in _IMAGE_FILE_EXTS

    def _should_relay_single(self, msg, *, uploadable_media: bool, expanded: ExpandedMedia | None) -> tuple[bool, str]:
        if uploadable_media:
            if self._is_video_message(msg):
                return True, "video"
            if self._is_photo_message(msg):
                return False, "single_photo"
            return True, "other_media"

        if expanded is not None:
            files = list(expanded.files or [])
            if any(self._looks_like_video_path(p) for p in files):
                return True, "expanded_video"
            image_count = sum(1 for p in files if self._looks_like_image_path(p))
            if image_count >= 2:
                return True, "expanded_multi_image"
            if image_count == 1:
                return False, "expanded_single_image"
            return False, "expanded_no_media"

        return False, "text_only"

    def _should_relay_album(self, msgs: list[Any]) -> tuple[bool, str]:
        if any(self._is_video_message(m) for m in msgs):
            return True, "video"

        photo_count = sum(1 for m in msgs if self._is_photo_message(m))
        if photo_count >= 2:
            return True, "multi_photo"
        if photo_count == 1:
            return False, "single_photo"

        # Non-photo album items (docs, etc). Keep previous behavior.
        if any(getattr(m, "media", None) for m in msgs):
            return True, "other_media"

        return False, "no_media"

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

    def _parse_destinations_tokens(self, tokens: list[str]) -> list[dict[str, Any]]:
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

    def _format_destinations(self, destinations: list[dict[str, Any]]) -> str:
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

    def _save_config_and_reload(self, cfg: dict[str, Any]) -> None:
        self.config_manager.save(cfg)
        self.settings = load_relay_settings(self.config_manager)
        self._maybe_log_topic_config_warning(self.settings)

    async def _handle_command(self, event, stripped_text: str) -> None:
        cmd, args = parse_command(stripped_text)

        if cmd in {"/help", "/start"}:
            await event.reply(
                "🤖 RelayBot commands:\n"
                "/list_routes\n"
                "/add_route <source_chat[,..]> [source_topic=<top_msg_id>] <dest_chat>@<topic_top_msg_id> | <dest_chat>=\"<topic_title>\" ...\n"
                "/remove_route <index>\n"
                "/set_destinations <index> <dest...>\n"
            )
            return

        if cmd == "/list_routes":
            cfg = self.config_manager.load(force=True)
            relay = cfg.get("relay", {}) or {}
            routes = relay.get("routes", []) or []
            if not routes:
                await event.reply("🤖 routes 为空")
                return

            lines: list[str] = []
            for i, r in enumerate(routes, start=1):
                src = ",".join(str(x) for x in (r.get("source_chats") or []))
                topics = r.get("source_topics") or []
                topic_str = f" topics={','.join(str(x) for x in topics)}" if topics else ""
                dest = self._format_destinations(r.get("destinations") or [])
                lines.append(f"{i}) {src}{topic_str} -> {dest}")

            await event.reply("🤖 Routes:\n" + "\n".join(lines[:50]))
            return

        if cmd == "/add_route":
            tokens = shlex.split(args or "")
            if len(tokens) < 2:
                await event.reply("🤖 用法: /add_route <source_chat[,..]> [source_topic=<top_msg_id>] <dest_chat>@<topic_top_msg_id> | <dest_chat>=\"<topic_title>\" ...")
                return

            source_chats = [int(x) for x in tokens[0].split(",") if x.strip()]
            source_topic_id = None
            dest_tokens: list[str] = []
            for t in tokens[1:]:
                if t.startswith("source_topic=") or t.startswith("topic="):
                    source_topic_id = int(t.split("=", 1)[1])
                    continue
                dest_tokens.append(t)

            destinations = self._parse_destinations_tokens(dest_tokens)
            if not destinations:
                await event.reply("🤖 错误: destinations 为空")
                return

            cfg = self.config_manager.load(force=True)
            cfg.setdefault("relay", {})
            relay = cfg.get("relay", {}) or {}
            routes = list(relay.get("routes", []) or [])

            new_route: dict[str, Any] = {"source_chats": source_chats, "destinations": destinations}
            if source_topic_id is not None:
                new_route["source_topics"] = [int(source_topic_id)]

            routes.append(new_route)
            relay["routes"] = routes
            cfg["relay"] = relay
            self._save_config_and_reload(cfg)
            await event.reply("🤖 已添加 route")
            return

        if cmd == "/remove_route":
            tokens = shlex.split(args or "")
            if len(tokens) != 1:
                await event.reply("🤖 用法: /remove_route <index>")
                return

            idx = int(tokens[0]) - 1
            cfg = self.config_manager.load(force=True)
            relay = cfg.get("relay", {}) or {}
            routes = list(relay.get("routes", []) or [])
            if idx < 0 or idx >= len(routes):
                await event.reply("🤖 错误: index 超出范围")
                return

            routes.pop(idx)
            relay["routes"] = routes
            cfg["relay"] = relay
            self._save_config_and_reload(cfg)
            await event.reply("🤖 已移除 route")
            return

        if cmd == "/set_destinations":
            tokens = shlex.split(args or "")
            if len(tokens) < 2:
                await event.reply("🤖 用法: /set_destinations <index> <dest_chat>@<topic_top_msg_id> | <dest_chat>=\"<topic_title>\" ...")
                return

            idx = int(tokens[0]) - 1
            destinations = self._parse_destinations_tokens(tokens[1:])

            cfg = self.config_manager.load(force=True)
            relay = cfg.get("relay", {}) or {}
            routes = list(relay.get("routes", []) or [])
            if idx < 0 or idx >= len(routes):
                await event.reply("🤖 错误: index 超出范围")
                return

            routes[idx]["destinations"] = destinations
            relay["routes"] = routes
            cfg["relay"] = relay
            self._save_config_and_reload(cfg)
            await event.reply("🤖 已更新 destinations")
            return

        await event.reply("🤖 未知命令。发送 /help 查看用法")

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
            await self._handle_command(event, stripped_text)
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
                    embedded_chat, embedded_topic, _ = _extract_embedded_source_markers_and_strip(getattr(msg, "raw_text", "") or "")
                    if source_chat_id is None:
                        source_chat_id = embedded_chat
                    self.media_group_cache[key] = {
                        "messages": [],
                        "task": None,
                        "source_chat_id": source_chat_id,
                        "source_topic_id": embedded_topic,
                    }

                self.media_group_cache[key]["messages"].append(msg)
                task = self.media_group_cache[key].get("task")
                if task:
                    task.cancel()
                self.media_group_cache[key]["task"] = asyncio.create_task(self.process_media_group(key))
            log_event(self.logger, logging.INFO, "album_cached", group_id=msg.grouped_id)
            return

        await self.send_copy(msg)

    async def process_media_group(self, key: tuple[int, int]) -> None:
        try:
            await asyncio.sleep(2)
            async with self.media_group_lock:
                if key not in self.media_group_cache:
                    return
                source_chat_id = self.media_group_cache[key].get("source_chat_id")
                source_topic_id = self.media_group_cache[key].get("source_topic_id")
                msgs = self.media_group_cache[key]["messages"]
                del self.media_group_cache[key]

            msgs.sort(key=lambda x: x.id)

            caption_raw = next(
                (
                    (getattr(m, "raw_text", None) or getattr(m, "text", None) or "")
                    for m in msgs
                    if (getattr(m, "raw_text", None) or getattr(m, "text", None))
                ),
                None,
            )
            embedded_chat, embedded_topic, caption = _extract_embedded_source_markers_and_strip(caption_raw or "")
            if source_chat_id is None:
                source_chat_id = embedded_chat
            if source_topic_id is None:
                source_topic_id = embedded_topic

            _, gid = key
            caption_for_blocking = caption

            if self._has_too_many_links(caption_for_blocking):
                log_event(
                    self.logger,
                    logging.INFO,
                    "album_skipped_too_many_links",
                    group_id=gid,
                    source_chat_id=source_chat_id,
                    links=self._count_links(caption_for_blocking),
                )
                return

            files = [m.media for m in msgs if getattr(m, "media", None)]

            if not files:
                log_event(self.logger, logging.INFO, "album_skipped_no_media", group_id=gid)
                return

            should_relay, reason = self._should_relay_album(msgs)
            if not should_relay:
                log_event(
                    self.logger,
                    logging.INFO,
                    "album_skipped_policy",
                    group_id=gid,
                    source_chat_id=source_chat_id,
                    reason=reason,
                )
                return

            for dest in self.resolve_destinations(source_chat_id, source_topic_id=source_topic_id, seed=gid):
                chat_id = int(dest["chat_id"])
                topic_title = dest.get("topic_title")

                post_caption = self._post_caption_for(chat_id)
                full_caption = caption
                if post_caption:
                    full_caption = (full_caption or "") + ("\n\n" if full_caption else "") + post_caption

                if caption_for_blocking and self._is_blocked(caption_for_blocking):
                    log_event(self.logger, logging.INFO, "message_blocked", chat_id=chat_id, group_id=gid)
                    continue

                if full_caption and self._is_blocked(full_caption):
                    log_event(self.logger, logging.INFO, "message_blocked", chat_id=chat_id, group_id=gid)
                    continue

                reply_to = await self._resolve_reply_to(chat_id, topic_title, explicit_topic_id=dest.get("topic_id"))

                if reply_to is None and self.current_settings().get("fallback_to_general_topic"):
                    fallback = (self.current_settings().get("fallback_topic_titles", {}) or {}).get(chat_id)
                    if fallback:
                        reply_to = await self._resolve_reply_to(chat_id, str(fallback))

                if reply_to is None and topic_title:
                    if self.current_settings().get("require_forum_topic"):
                        log_event(
                            self.logger,
                            logging.INFO,
                            "album_skipped_topic_not_found",
                            chat_id=chat_id,
                            topic_title=str(topic_title),
                            group_id=gid,
                            source_chat_id=source_chat_id,
                        )
                        continue

                    norm = normalize_forum_topic_title(str(topic_title))
                    warn_key = (int(chat_id), norm)
                    if warn_key not in self._warned_unresolved_topics:
                        self._warned_unresolved_topics.add(warn_key)
                        log_event(
                            self.logger,
                            logging.INFO,
                            "topic_unresolved_fallback_to_general",
                            chat_id=chat_id,
                            topic_title=str(topic_title),
                            hint="Populate relay.forum_topic_ids (run scripts/export_forum_topic_ids.py) to enable routing into topics.",
                        )

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
        forward_source = _extract_forward_source_chat_id(msg)
        original_text = getattr(msg, "raw_text", "") or ""
        embedded_chat, embedded_topic, stripped_original_text = _extract_embedded_source_markers_and_strip(original_text)
        source_chat_id = forward_source if forward_source is not None else embedded_chat
        source_topic_id = embedded_topic

        # We use the original text for tweet URL detection even if strip_text is enabled.
        display_text = stripped_original_text
        if self.current_settings().get("strip_text"):
            display_text = ""

        media = getattr(msg, "media", None)
        uploadable_media = media is not None and not isinstance(media, types.MessageMediaWebPage)

        if self._has_too_many_links(stripped_original_text):
            log_event(
                self.logger,
                logging.INFO,
                "message_skipped_too_many_links",
                message_id=msg.id,
                source_chat_id=source_chat_id,
                links=self._count_links(stripped_original_text),
            )
            return

        expanded: ExpandedMedia | None = None
        if not uploadable_media:
            expanded = await self._maybe_expand_twitter_media(stripped_original_text)

        try:
            should_relay, reason = self._should_relay_single(msg, uploadable_media=uploadable_media, expanded=expanded)
            if not should_relay:
                log_event(
                    self.logger,
                    logging.INFO,
                    "message_skipped_policy",
                    message_id=msg.id,
                    source_chat_id=source_chat_id,
                    reason=reason,
                )
                return

            for dest in self.resolve_destinations(source_chat_id, source_topic_id=source_topic_id, seed=msg.id):
                chat_id = int(dest["chat_id"])
                topic_title = dest.get("topic_title")

                post_caption = self._post_caption_for(chat_id)
                full_text = display_text
                if post_caption:
                    full_text = (full_text or "") + ("\n\n" if full_text else "") + post_caption

                # Block based on original inbound text even if strip_text is enabled.
                if stripped_original_text and self._is_blocked(stripped_original_text):
                    log_event(self.logger, logging.INFO, "message_blocked", chat_id=chat_id, message_id=msg.id)
                    continue
                if full_text and self._is_blocked(full_text):
                    log_event(self.logger, logging.INFO, "message_blocked", chat_id=chat_id, message_id=msg.id)
                    continue

                reply_to = await self._resolve_reply_to(chat_id, topic_title, explicit_topic_id=dest.get("topic_id"))

                if reply_to is None and self.current_settings().get("fallback_to_general_topic"):
                    fallback = (self.current_settings().get("fallback_topic_titles", {}) or {}).get(chat_id)
                    if fallback:
                        reply_to = await self._resolve_reply_to(chat_id, str(fallback))

                if reply_to is None and topic_title:
                    if self.current_settings().get("require_forum_topic"):
                        log_event(
                            self.logger,
                            logging.INFO,
                            "message_skipped_topic_not_found",
                            chat_id=chat_id,
                            topic_title=str(topic_title),
                            message_id=msg.id,
                            source_chat_id=source_chat_id,
                        )
                        continue

                    norm = normalize_forum_topic_title(str(topic_title))
                    warn_key = (int(chat_id), norm)
                    if warn_key not in self._warned_unresolved_topics:
                        self._warned_unresolved_topics.add(warn_key)
                        log_event(
                            self.logger,
                            logging.INFO,
                            "topic_unresolved_fallback_to_general",
                            chat_id=chat_id,
                            topic_title=str(topic_title),
                            hint="Populate relay.forum_topic_ids (run scripts/export_forum_topic_ids.py) to enable routing into topics.",
                        )

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

    if settings.get("manage_forum_topics", True):
        await bot.sync_forum_topics()
    else:
        log_event(logger, logging.INFO, "forum_topic_management_disabled")

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
