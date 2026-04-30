import asyncio
import logging
import re
import shlex
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from telethon import Button, TelegramClient, events, functions, types, utils

from command_utils import parse_command
from common_config import ConfigManager, load_relay_settings
from delivery import AsyncRateLimiter, with_retry, write_dlq
from relay_filters import (
    count_links,
    document_meta_text,
    filter_haystack,
    has_too_many_links,
    is_blocked,
    is_disallowed_document,
    is_gif_or_sticker,
    is_link_only,
    is_location_message,
    is_photo_message,
    is_short_video,
    is_video_message,
    video_duration_seconds,
)
from telegram_link_utils import looks_like_message_link, parse_message_link
from twitter_expand import download_tweet_media, extract_tweet_urls
from route_filter_utils import filter_routes, parse_route_filters

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

_IMAGE_FILE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
_VIDEO_FILE_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}

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

        # Simple per-user wizard state for inline-menu flows (routes management).
        self._menu_state: dict[int, dict[str, Any]] = {}

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

    def _ignored_source_chats(self) -> set[int]:
        settings = self.current_settings()
        out: set[int] = set()

        for x in settings.get("ignore_source_chats", []) or []:
            try:
                out.add(int(x))
            except Exception:  # noqa: BLE001
                continue

        for x in settings.get("dest_channels", []) or []:
            try:
                out.add(int(x))
            except Exception:  # noqa: BLE001
                continue

        for d in settings.get("default_destinations", []) or []:
            try:
                out.add(int(d.get("chat_id")))
            except Exception:  # noqa: BLE001
                continue

        for r in settings.get("routes", []) or []:
            for d in r.get("destinations", []) or []:
                try:
                    out.add(int(d.get("chat_id")))
                except Exception:  # noqa: BLE001
                    continue

        return out

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
        return is_blocked(text, self.current_settings().get("blocklist_substrings", []) or [])

    def _count_links(self, text: str | None) -> int:
        return count_links(text)

    def _has_too_many_links(self, text: str | None) -> bool:
        return has_too_many_links(text)

    def _is_link_only(self, text: str | None) -> bool:
        return is_link_only(text)

    def _document_meta_text(self, msg) -> str:
        return document_meta_text(msg)

    def _filter_haystack(self, msg, stripped_original_text: str) -> str:
        return filter_haystack(stripped_original_text, msg)

    def _is_gif_or_sticker(self, msg) -> bool:
        return is_gif_or_sticker(msg, telethon_types=types)

    def _is_disallowed_document(self, msg) -> bool:
        return is_disallowed_document(msg)

    def _is_location_message(self, msg) -> bool:
        return is_location_message(msg, telethon_types=types)

    def _is_video_message(self, msg) -> bool:
        return is_video_message(msg)

    def _video_duration_seconds(self, msg) -> int | None:
        return video_duration_seconds(msg, telethon_types=types)

    def _is_short_video(self, msg) -> bool:
        return is_short_video(msg, telethon_types=types)

    def _is_photo_message(self, msg) -> bool:
        return is_photo_message(msg)

    def _looks_like_video_path(self, path: str) -> bool:
        ext = Path(str(path)).suffix.lower()
        return ext in _VIDEO_FILE_EXTS

    def _looks_like_image_path(self, path: str) -> bool:
        ext = Path(str(path)).suffix.lower()
        return ext in _IMAGE_FILE_EXTS

    def _should_relay_single(self, msg, *, uploadable_media: bool, expanded: ExpandedMedia | None) -> tuple[bool, str]:
        if self._is_gif_or_sticker(msg):
            return False, "gif_or_sticker"
        if self._is_disallowed_document(msg):
            return False, "disallowed_document"

        if uploadable_media:
            if self._is_video_message(msg):
                if self._is_short_video(msg):
                    return False, "short_video"
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
        if any(self._is_gif_or_sticker(m) for m in msgs):
            return False, "gif_or_sticker"
        if any(self._is_disallowed_document(m) for m in msgs):
            return False, "disallowed_document"

        if any(self._is_video_message(m) for m in msgs):
            if any(self._is_short_video(m) for m in msgs if self._is_video_message(m)):
                return False, "short_video"
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

    async def _resolve_message_link(self, link: str) -> tuple[int, int, int | None]:
        parsed = parse_message_link(link)
        if not parsed:
            raise ValueError("invalid_link")

        ent = await self.client.get_entity(parsed.chat)
        chat_id = int(utils.get_peer_id(ent))

        msg = await self.client.get_messages(ent, ids=int(parsed.message_id))
        if not msg:
            raise ValueError("message_not_found")

        reply_to = getattr(msg, "reply_to", None)
        top = getattr(reply_to, "reply_to_top_id", None)
        topic_top = int(top) if top else (int(msg.id) if getattr(msg, "is_topic", False) else None)
        if topic_top is None and parsed.topic_id is not None:
            topic_top = int(parsed.topic_id)

        return chat_id, int(parsed.message_id), topic_top

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

    async def _normalize_routes_filter_args(self, event, args: str) -> str | None:
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
                    chat_id, _, top = await self._resolve_message_link(tok)
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
                            ent = await self.client.get_entity(int(chat_id))
                            msg = await self.client.get_messages(ent, ids=int(topic_top))
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

    def _menu_buttons(self, menu: str) -> list[list[Any]]:
        menu = (menu or "main").strip().lower()

        if menu == "routes":
            return [
                [Button.inline("📋 List", b"routes:list"), Button.inline("⬇️ Export", b"routes:export")],
                [Button.inline("➕ Add", b"routes:add"), Button.inline("➖ Remove", b"routes:remove")],
                [Button.inline("✏️ Set destinations", b"routes:set_dest")],
                [Button.inline("⬅️ Back", b"menu:main")],
            ]

        if menu == "xwatch":
            return [
                [Button.inline("📋 List watches", b"xwatch:list")],
                [Button.inline("⬅️ Back", b"menu:main")],
            ]

        if menu == "wizard_cancel":
            return [[Button.inline("Cancel", b"wizard:cancel")]]

        # main
        return [
            [Button.inline("Routes", b"menu:routes"), Button.inline("X Watch", b"menu:xwatch")],
            [Button.inline("Help", b"menu:help")],
        ]

    def _menu_text(self, menu: str) -> str:
        menu = (menu or "main").strip().lower()

        if menu == "routes":
            return "\n".join(
                [
                    "🤖 Routes menu",
                    "",
                    "You can use buttons (wizard) or type commands directly:",
                    "- /list_routes [filters...]",
                    "- /add_route ...",
                    "- /remove_route <index>",
                    "- /set_destinations <index> ...",
                ]
            )

        if menu == "xwatch":
            return "\n".join(
                [
                    "🤖 Twitter/X watch menu",
                    "",
                    "List current watches with the button below, or use commands:",
                    "- /list_x_watch",
                    "- /add_x_watch ...",
                    "- /remove_x_watch <index>",
                    "",
                    "Note: the userbot (telegram_bot.py) must be running/logged-in to poll X.",
                ]
            )

        if menu == "help":
            return "\n".join(
                [
                    "🤖 Help",
                    "",
                    "This relay bot forwards what it receives (from userbot) into your configured routes.",
                    "",
                    "Quick tips:",
                    "- Use /start to open the interactive menu.",
                    "- Use /list_routes to check current routing.",
                    "- Filters: source=<id[,..]> dest=<id[,..]> topic=<substring> topic_id=<id>",
                    "",
                    "Advanced: send /help for full command list (this message) and then use the buttons.",
                ]
            )

        return "\n".join(
            [
                "🤖 Control panel",
                "",
                "Choose an action below. (You can still type commands anytime.)",
            ]
        )

    async def _send_menu_message(self, chat_id: int, *, menu: str = "main") -> None:
        await self.client.send_message(chat_id, message=self._menu_text(menu), buttons=self._menu_buttons(menu))

    async def _send_routes_report(self, chat_id: int, args: str = "") -> None:
        cfg = self.config_manager.load(force=True)
        relay = cfg.get("relay", {}) or {}
        routes = relay.get("routes", []) or []
        if not routes:
            await self.client.send_message(chat_id, message="🤖 routes 为空")
            return

        text = await self._build_routes_report(list(routes), args)
        if not text:
            await self.client.send_message(chat_id, message="🤖 未匹配到 routes。")
            return

        if len(text) <= 3500:
            await self.client.send_message(chat_id, message=text)
            return

        tmp = tempfile.TemporaryDirectory(prefix="routes_")
        try:
            path = f"{tmp.name}/routes.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            await self.client.send_file(chat_id, path, caption="🤖 Routes 太长，已导出为文件")
        finally:
            tmp.cleanup()

    async def _send_routes_export(self, chat_id: int, args: str = "") -> None:
        cfg = self.config_manager.load(force=True)
        relay = cfg.get("relay", {}) or {}
        routes = relay.get("routes", []) or []
        if not routes:
            await self.client.send_message(chat_id, message="🤖 routes 为空")
            return

        text = await self._build_routes_report(list(routes), args)
        if not text:
            await self.client.send_message(chat_id, message="🤖 未匹配到 routes。")
            return

        tmp = tempfile.TemporaryDirectory(prefix="routes_")
        try:
            path = f"{tmp.name}/routes.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            await self.client.send_file(chat_id, path, caption="🤖 Routes 导出")
        finally:
            tmp.cleanup()

    async def _send_x_watch_list(self, chat_id: int) -> None:
        cfg = self.config_manager.load(force=True)
        tw = cfg.get("twitter_watch", {}) or {}
        enabled = bool(tw.get("enabled", False))
        sources = tw.get("sources", []) or []

        if not sources:
            await self.client.send_message(chat_id, message=f"🤖 twitter_watch: enabled={enabled} | sources=0")
            return

        lines = [f"🤖 twitter_watch: enabled={enabled}", ""]
        for i, s in enumerate(sources, start=1):
            if not isinstance(s, dict):
                continue
            profile = str(s.get("profile") or s.get("account") or s.get("url") or "")
            source_chat_id = s.get("source_chat_id")
            target_bot = s.get("target_bot")
            interval = s.get("poll_interval_sec", 300)
            fetch_limit = s.get("fetch_limit", 30)
            lines.append(
                f"{i}) {profile} | source_chat_id={source_chat_id} | target_bot={target_bot} | interval={interval}s | fetch_limit={fetch_limit}"
            )

        await self.client.send_message(chat_id, message="\n".join(lines)[:3500])

    async def handle_callback(self, event) -> None:
        settings = self.current_settings()
        allowed_sender = int(settings.get("master_account_id", 0) or 0)
        if allowed_sender and getattr(event, "sender_id", None) != allowed_sender:
            await event.answer("Not allowed", alert=False)
            return

        data = getattr(event, "data", b"") or b""
        try:
            key = data.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            key = ""

        await event.answer()

        sender_id = int(getattr(event, "sender_id", 0) or 0)

        if key == "menu:main":
            self._menu_state.pop(sender_id, None)
            await event.edit(self._menu_text("main"), buttons=self._menu_buttons("main"))
            return

        if key == "menu:routes":
            self._menu_state.pop(sender_id, None)
            await event.edit(self._menu_text("routes"), buttons=self._menu_buttons("routes"))
            return

        if key == "menu:xwatch":
            self._menu_state.pop(sender_id, None)
            await event.edit(self._menu_text("xwatch"), buttons=self._menu_buttons("xwatch"))
            return

        if key == "menu:help":
            await event.edit(self._menu_text("help"), buttons=self._menu_buttons("main"))
            return

        if key == "wizard:cancel":
            self._menu_state.pop(sender_id, None)
            await event.edit(self._menu_text("routes"), buttons=self._menu_buttons("routes"))
            return

        if key == "routes:list":
            await self._send_routes_report(int(event.chat_id), "")
            return

        if key == "routes:export":
            await self._send_routes_export(int(event.chat_id), "")
            return

        if key == "routes:add":
            self._menu_state[sender_id] = {"step": "add_route_source"}
            await event.edit(
                "🤖 Add route\n\nSend the SOURCE as a chat_id (e.g. -100...) or a Telegram message link.",
                buttons=self._menu_buttons("wizard_cancel"),
            )
            return

        if key == "routes:remove":
            self._menu_state[sender_id] = {"step": "remove_route_index"}
            await event.edit(
                "🤖 Remove route\n\nSend the route index number (from /list_routes).",
                buttons=self._menu_buttons("wizard_cancel"),
            )
            return

        if key == "routes:set_dest":
            self._menu_state[sender_id] = {"step": "set_dest_index"}
            await event.edit(
                "🤖 Set destinations\n\nSend the route index number (from /list_routes).",
                buttons=self._menu_buttons("wizard_cancel"),
            )
            return

        if key == "xwatch:list":
            await self._send_x_watch_list(int(event.chat_id))
            return

    async def _build_routes_report(self, routes: list[dict[str, Any]], args: str) -> str | None:
        entity_cache: dict[int, object] = {}
        topic_title_cache: dict[tuple[int, int], str] = {}

        async def _get_entity(chat_id: int):
            if chat_id in entity_cache:
                return entity_cache[chat_id]
            ent = await self.client.get_entity(chat_id)
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
            msg = await self.client.get_messages(ent, ids=int(top_message_id))
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

    async def _handle_command(self, event, stripped_text: str) -> None:
        cmd, args = parse_command(stripped_text)

        if cmd in {"/start", "/menu"}:
            self._menu_state.pop(int(getattr(event, "sender_id", 0) or 0), None)
            await self._send_menu_message(int(event.chat_id), menu="main")
            return

        if cmd == "/help":
            self._menu_state.pop(int(getattr(event, "sender_id", 0) or 0), None)
            text = "\n".join(
                [
                    "🤖 RelayBot help (DM commands to this bot)",
                    "",
                    "Tip: use /start to open the interactive menu for route management.",
                    "",
                    "Routes viewing / filtering:",
                    "- /list_routes [filters...]",
                    "  Filters: source=<id[,..]> dest=<id[,..]> topic=<substring> topic_id=<id> + free text terms",
                    "  Examples:",
                    "    /list_routes",
                    "    /list_routes source=-1001234567890",
                    "    /list_routes dest=-1002222222222",
                    "    /list_routes topic=\"Hot\"",
                    "- /export_routes [filters...]  (always sends a routes.txt file)",
                    "",
                    "Route editing:",
                    "- /add_route <source_chat[,..]> [source_topic=<top_msg_id>] <dest...>",
                    "  Dest formats:",
                    "    <dest_chat_id>=\"<topic_title>\"",
                    "    <dest_chat_id>@<topic_top_message_id>",
                    "    <dest_chat_id>",
                    "  Link-based route form:",
                    "    /add_route <source_message_link> <dest_message_link> [dest_message_link...]",
                    "- /remove_route <index>",
                    "- /set_destinations <index> <dest...>",
                    "",
                    "Twitter/X watch (poll profiles via userbot):",
                    "- /list_x_watch",
                    "- /add_x_watch (old) <x_profile> <source_chat_id> <@relay_bot> ...",
                    "- /add_x_watch (new) <x_profile> <dest_message_link> [dest_message_link...] ...",
                    "- /remove_x_watch <index>",
                    "",
                    "Note: userbot (telegram_bot.py) must be running and logged in. It will poll X and DM tweet URLs to this bot.",
                ]
            )
            if len(text) <= 3500:
                await event.reply(text, buttons=self._menu_buttons("main"))
            else:
                tmp = tempfile.TemporaryDirectory(prefix="help_")
                try:
                    path = f"{tmp.name}/help.txt"
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
                    await self.client.send_file(event.chat_id, path, caption="🤖 Help", buttons=self._menu_buttons("main"))
                finally:
                    tmp.cleanup()
            return

        if cmd == "/list_x_watch":
            cfg = self.config_manager.load(force=True)
            tw = cfg.get("twitter_watch", {}) or {}
            enabled = bool(tw.get("enabled", False))
            sources = tw.get("sources", []) or []

            if not sources:
                await event.reply(f"🤖 twitter_watch: enabled={enabled} | sources=0")
                return

            lines = [f"🤖 twitter_watch: enabled={enabled}", ""]
            for i, s in enumerate(sources, start=1):
                if not isinstance(s, dict):
                    continue
                profile = str(s.get("profile") or s.get("account") or s.get("url") or "")
                source_chat_id = s.get("source_chat_id")
                target_bot = s.get("target_bot")
                interval = s.get("poll_interval_sec", 300)
                fetch_limit = s.get("fetch_limit", 30)
                lines.append(
                    f"{i}) {profile} | source_chat_id={source_chat_id} | target_bot={target_bot} | interval={interval}s | fetch_limit={fetch_limit}"
                )

            await event.reply("\n".join(lines)[:3500])
            return

        if cmd == "/add_x_watch":
            tokens = shlex.split(args or "")
            if not tokens:
                await event.reply(
                    "🤖 用法(旧): /add_x_watch <x_profile_or_username> <source_chat_id> <@relay_bot> [poll_interval_sec=300] [fetch_limit=30] [archive_file=state/... ]\n"
                    "🤖 用法(新): /add_x_watch <x_profile_or_username> <dest_message_link> [dest_message_link...] [poll_interval_sec=...] [fetch_limit=...] [archive_file=...]\n"
                    "提示: 新用法会自动生成 source_chat_id，并自动添加 route。"
                )
                return

            def _is_int_token(s: str) -> bool:
                try:
                    int(str(s).strip())
                    return True
                except Exception:  # noqa: BLE001
                    return False

            async def _infer_default_relay_bot() -> str | None:
                try:
                    me = await self.client.get_me()
                except Exception:  # noqa: BLE001
                    me = None
                username = getattr(me, "username", None) if me is not None else None
                if username:
                    return "@" + str(username).lstrip("@").strip()
                return None

            def _allocate_source_chat_id(sources: list[object]) -> int:
                used: set[int] = set()
                for s in sources:
                    if not isinstance(s, dict):
                        continue
                    try:
                        used.add(int(s.get("source_chat_id")))
                    except Exception:  # noqa: BLE001
                        continue
                cand = -900000001
                while cand in used:
                    cand -= 1
                return cand

            def _dedupe_destinations(dests: list[dict[str, Any]]) -> list[dict[str, Any]]:
                seen: set[tuple[int, int | None, str | None]] = set()
                out: list[dict[str, Any]] = []
                for d in dests or []:
                    try:
                        chat_id = int(d.get("chat_id"))
                    except Exception:  # noqa: BLE001
                        continue
                    topic_id = int(d.get("topic_id")) if d.get("topic_id") is not None else None
                    topic_title = str(d.get("topic_title")) if d.get("topic_title") else None
                    key = (chat_id, topic_id, topic_title)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(d)
                return out

            profile = tokens[0]

            positional: list[str] = []
            kv: dict[str, str] = {}
            for t in tokens[1:]:
                if "=" in t:
                    k, v = t.split("=", 1)
                    kv[k.strip()] = v.strip()
                else:
                    positional.append(t)

            cfg = self.config_manager.load(force=True)
            tw = cfg.get("twitter_watch", {}) or {}
            sources = list(tw.get("sources", []) or [])

            destinations: list[dict[str, Any]] = []
            non_link_dest_tokens: list[str] = []

            old_style = len(positional) >= 2 and _is_int_token(positional[0])
            if old_style:
                source_chat_id = int(positional[0])
                bot = "@" + positional[1].strip().lstrip("@")
                dest_tokens = positional[2:]

                for t in dest_tokens:
                    if looks_like_message_link(t):
                        try:
                            chat_id, _, topic_top = await self._resolve_message_link(t)
                        except Exception as exc:  # noqa: BLE001
                            await event.reply(f"🤖 错误: 无法解析链接: {t} ({type(exc).__name__})")
                            return

                        dest: dict[str, Any] = {"chat_id": int(chat_id)}
                        if topic_top is not None:
                            dest["topic_id"] = int(topic_top)
                        destinations.append(dest)
                    else:
                        non_link_dest_tokens.append(t)
            else:
                bot = kv.get("relay_bot") or kv.get("bot") or ""

                # Also allow passing the relay bot as a positional "@BotUserName" token.
                pos_bot = next((p for p in positional if str(p).strip().startswith("@")), None)
                if pos_bot:
                    positional = [p for p in positional if p != pos_bot]
                    bot = str(pos_bot)

                if bot:
                    bot = "@" + str(bot).strip().lstrip("@")
                else:
                    inferred = await _infer_default_relay_bot()
                    if not inferred:
                        await event.reply("🤖 错误: 无法推断 relay bot 用户名。请用旧用法，或在新用法里加 relay_bot=@YourRelayBot。")
                        return
                    bot = inferred

                source_chat_id = int(kv.get("source_chat_id") or _allocate_source_chat_id(sources))

                for t in positional:
                    if looks_like_message_link(t):
                        try:
                            chat_id, _, topic_top = await self._resolve_message_link(t)
                        except Exception as exc:  # noqa: BLE001
                            await event.reply(f"🤖 错误: 无法解析链接: {t} ({type(exc).__name__})")
                            return

                        dest: dict[str, Any] = {"chat_id": int(chat_id)}
                        if topic_top is not None:
                            dest["topic_id"] = int(topic_top)
                        destinations.append(dest)
                    else:
                        non_link_dest_tokens.append(t)

            if bot == "@":
                await event.reply("🤖 错误: 机器人用户名需以 @ 开头")
                return

            poll_interval_sec = int(kv.get("poll_interval_sec") or kv.get("interval") or 300)
            fetch_limit = int(kv.get("fetch_limit") or kv.get("limit") or 30)
            archive_file = kv.get("archive_file")

            try:
                ent = await self.client.get_entity(bot)
                if not getattr(ent, "bot", False):
                    await event.reply("🤖 错误: 目标必须是机器人账号（Bot），不能是频道/群/普通用户")
                    return
            except Exception as exc:  # noqa: BLE001
                await event.reply(f"🤖 无法解析 bot: {type(exc).__name__}: {exc}")
                return

            destinations.extend(self._parse_destinations_tokens(non_link_dest_tokens))
            destinations = _dedupe_destinations(destinations)

            new_entry: dict[str, Any] = {
                "profile": str(profile),
                "source_chat_id": int(source_chat_id),
                "target_bot": bot,
                "poll_interval_sec": max(30, int(poll_interval_sec)),
                "fetch_limit": max(1, min(int(fetch_limit), 200)),
            }
            if archive_file:
                new_entry["archive_file"] = str(archive_file)

            updated = False
            for i, s in enumerate(sources):
                if not isinstance(s, dict):
                    continue
                existing_profile = str(s.get("profile") or s.get("account") or s.get("url") or "")
                try:
                    existing_source = int(s.get("source_chat_id") or 0)
                except Exception:  # noqa: BLE001
                    existing_source = 0

                if existing_profile == str(profile) and existing_source == int(source_chat_id):
                    sources[i] = new_entry
                    updated = True
                    break

            if not updated:
                sources.append(new_entry)

            tw["enabled"] = True
            tw["sources"] = sources
            cfg["twitter_watch"] = tw

            # Optional: auto-add a route when using dest links.
            if destinations:
                cfg.setdefault("relay", {})
                routes = list((cfg.get("relay", {}) or {}).get("routes", []) or [])

                merged = False
                for r in routes:
                    if not isinstance(r, dict):
                        continue
                    srcs = [int(x) for x in (r.get("source_chats") or []) if str(x).strip()]
                    if srcs == [int(source_chat_id)] and not (r.get("source_topics") or []):
                        r["destinations"] = _dedupe_destinations(list(r.get("destinations") or []) + destinations)
                        merged = True
                        break

                if not merged:
                    routes.append({"source_chats": [int(source_chat_id)], "destinations": destinations})

                cfg["relay"]["routes"] = routes

            self._save_config_and_reload(cfg)

            if destinations:
                await event.reply(
                    "🤖 已添加 twitter_watch source，并已添加/更新 route。\n"
                    f"profile={profile} | source_chat_id={source_chat_id} | target_bot={bot} | interval={new_entry['poll_interval_sec']}s | fetch_limit={new_entry['fetch_limit']}\n"
                    f"destinations={self._format_destinations(destinations)}"
                )
            else:
                await event.reply(
                    "🤖 已添加 twitter_watch source。\n"
                    "下一步: 用 /add_route <source_chat_id> <dest...> 把 source_chat_id 路由到目标话题/频道。\n"
                    f"profile={profile} | source_chat_id={source_chat_id} | target_bot={bot}"
                )
            return

        if cmd == "/remove_x_watch":
            tokens = shlex.split(args or "")
            if len(tokens) != 1:
                await event.reply("🤖 用法: /remove_x_watch <index>")
                return

            idx = int(tokens[0]) - 1
            cfg = self.config_manager.load(force=True)
            tw = cfg.get("twitter_watch", {}) or {}
            sources = list(tw.get("sources", []) or [])

            if idx < 0 or idx >= len(sources):
                await event.reply("🤖 错误: index 超出范围")
                return

            removed = sources.pop(idx)
            tw["sources"] = sources
            cfg["twitter_watch"] = tw
            self.config_manager.save(cfg)

            profile = None
            try:
                profile = str((removed or {}).get("profile") or "")
            except Exception:  # noqa: BLE001
                profile = None

            await event.reply(f"🤖 已移除 twitter_watch: {profile or '(unknown)'}")
            return

        if cmd == "/list_routes":
            cfg = self.config_manager.load(force=True)
            relay = cfg.get("relay", {}) or {}
            routes = relay.get("routes", []) or []
            if not routes:
                await event.reply("🤖 routes 为空")
                return

            args2 = await self._normalize_routes_filter_args(event, args)
            if args2 is None:
                return

            text = await self._build_routes_report(list(routes), args2)
            if not text:
                await event.reply(
                    "🤖 未匹配到 routes。用法: /list_routes [source=..] [dest=..] [topic=..] [topic_id=..] [free_text..]\n"
                    "提示: 也可以直接传一个 Telegram 消息链接（t.me/...）来按 destination/topic 过滤。"
                )
                return

            if len(text) <= 3500:
                await event.reply(text)
                return

            tmp = tempfile.TemporaryDirectory(prefix="routes_")
            try:
                path = f"{tmp.name}/routes.txt"
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                await self.client.send_file(event.chat_id, path, caption="🤖 Routes 太长，已导出为文件")
            finally:
                tmp.cleanup()
            return

        if cmd == "/export_routes":
            cfg = self.config_manager.load(force=True)
            relay = cfg.get("relay", {}) or {}
            routes = relay.get("routes", []) or []
            if not routes:
                await event.reply("🤖 routes 为空")
                return

            args2 = await self._normalize_routes_filter_args(event, args)
            if args2 is None:
                return

            text = await self._build_routes_report(list(routes), args2)
            if not text:
                await event.reply("🤖 未匹配到 routes。")
                return

            tmp = tempfile.TemporaryDirectory(prefix="routes_")
            try:
                path = f"{tmp.name}/routes.txt"
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                await self.client.send_file(event.chat_id, path, caption="🤖 Routes 导出")
            finally:
                tmp.cleanup()
            return

        if cmd == "/add_route":
            tokens = shlex.split(args or "")
            if len(tokens) < 2:
                await event.reply(
                    "🤖 用法: /add_route <source_chat[,..]> [source_topic=<top_msg_id>] <dest_chat>@<topic_top_msg_id> | <dest_chat>=\"<topic_title>\" ...\n"
                    "或: /add_route <source_message_link> <dest_message_link> [dest_message_link...]"
                )
                return

            source_token = tokens[0]
            if looks_like_message_link(source_token):
                parsed = parse_message_link(source_token)
                if not parsed:
                    await event.reply("🤖 链接无效")
                    return

                try:
                    ent = await self.client.get_entity(parsed.chat)
                    src_chat_id = int(utils.get_peer_id(ent))
                except Exception:  # noqa: BLE001
                    await event.reply("🤖 无法解析 source chat（机器人可能不在该群/频道）")
                    return

                src_topic_id = int(parsed.topic_id) if parsed.topic_id is not None else None

                destinations: list[dict[str, Any]] = []
                non_link_dest_tokens: list[str] = []
                for t in tokens[1:]:
                    if looks_like_message_link(t):
                        chat_id, _, topic_top = await self._resolve_message_link(t)
                        dest: dict[str, Any] = {"chat_id": int(chat_id)}
                        if topic_top is not None:
                            dest["topic_id"] = int(topic_top)
                        destinations.append(dest)
                    else:
                        non_link_dest_tokens.append(t)

                destinations.extend(self._parse_destinations_tokens(non_link_dest_tokens))
                if not destinations:
                    await event.reply("🤖 错误: destinations 为空")
                    return

                cfg = self.config_manager.load(force=True)
                cfg.setdefault("relay", {})
                relay = cfg.get("relay", {}) or {}
                routes = list(relay.get("routes", []) or [])

                new_route: dict[str, Any] = {"source_chats": [int(src_chat_id)], "destinations": destinations}
                if src_topic_id is not None:
                    new_route["source_topics"] = [int(src_topic_id)]

                routes.append(new_route)
                relay["routes"] = routes
                cfg["relay"] = relay
                self._save_config_and_reload(cfg)
                await event.reply("🤖 已添加 route")
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
                await event.reply(
                    "🤖 用法: /set_destinations <index> <dest_chat>@<topic_top_msg_id> | <dest_chat>=\"<topic_title>\" ...\n"
                    "或: /set_destinations <index> <dest_message_link> [dest_message_link...]"
                )
                return

            idx = int(tokens[0]) - 1

            destinations: list[dict[str, Any]] = []
            non_link_tokens: list[str] = []
            for t in tokens[1:]:
                if looks_like_message_link(t):
                    chat_id, _, topic_top = await self._resolve_message_link(t)
                    dest: dict[str, Any] = {"chat_id": int(chat_id)}
                    if topic_top is not None:
                        dest["topic_id"] = int(topic_top)
                    destinations.append(dest)
                else:
                    non_link_tokens.append(t)

            destinations.extend(self._parse_destinations_tokens(non_link_tokens))

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

    async def _handle_menu_input(self, event, text: str) -> None:
        sender_id = int(getattr(event, "sender_id", 0) or 0)
        state = self._menu_state.get(sender_id) or {}
        step = str(state.get("step") or "")

        s = (text or "").strip()
        if not s:
            await event.reply("🤖 请输入内容，或点 Cancel 取消。")
            return

        if s.lower() in {"/cancel", "cancel"}:
            self._menu_state.pop(sender_id, None)
            await self._send_menu_message(int(event.chat_id), menu="routes")
            return

        if step == "add_route_source":
            state["source"] = s
            state["step"] = "add_route_dest"
            self._menu_state[sender_id] = state
            await event.reply(
                "🤖 Add route\n\n"
                "Now send DESTINATIONS (same formats as /add_route). Examples:\n"
                "- -1001234567890=\"Topic Title\"\n"
                "- -1001234567890@777\n"
                "- https://t.me/c/123456/777/888\n\n"
                "Optional: include source_topic=777 before destinations.\n"
                "Send /cancel to abort."
            )
            return

        if step == "add_route_dest":
            source = str(state.get("source") or "").strip()
            self._menu_state.pop(sender_id, None)
            await self._handle_command(event, f"/add_route {source} {s}")
            await self._send_menu_message(int(event.chat_id), menu="routes")
            return

        if step == "remove_route_index":
            self._menu_state.pop(sender_id, None)
            await self._handle_command(event, f"/remove_route {s}")
            await self._send_menu_message(int(event.chat_id), menu="routes")
            return

        if step == "set_dest_index":
            state["index"] = s
            state["step"] = "set_dest_values"
            self._menu_state[sender_id] = state
            await event.reply(
                "🤖 Set destinations\n\n"
                "Now send the new destinations for this route (same formats as /set_destinations).\n"
                "Example: -1001234567890=\"Topic Title\" -1009999999999@777\n\n"
                "Send /cancel to abort."
            )
            return

        if step == "set_dest_values":
            idx = str(state.get("index") or "").strip()
            self._menu_state.pop(sender_id, None)
            await self._handle_command(event, f"/set_destinations {idx} {s}")
            await self._send_menu_message(int(event.chat_id), menu="routes")
            return

        self._menu_state.pop(sender_id, None)
        await event.reply("🤖 菜单状态已重置。请发送 /start 重新打开菜单。")

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

        sender_id = int(getattr(event, "sender_id", 0) or 0)
        if sender_id in self._menu_state:
            await self._handle_menu_input(event, stripped_text)
            return

        # Convenience: allow commands without a leading "/".
        if stripped_text:
            head = str(stripped_text.split(" ", 1)[0] or "").casefold().strip()
            if head in {
                "help",
                "start",
                "menu",
                "list_routes",
                "export_routes",
                "add_route",
                "remove_route",
                "set_destinations",
                "list_x_watch",
                "add_x_watch",
                "remove_x_watch",
            }:
                await self._handle_command(event, "/" + stripped_text)
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

            if source_chat_id is not None and int(source_chat_id) in self._ignored_source_chats():
                log_event(
                    self.logger,
                    logging.INFO,
                    "album_skipped_ignored_source",
                    group_id=gid,
                    source_chat_id=int(source_chat_id),
                )
                return
            caption_for_blocking = caption

            meta_parts = [self._document_meta_text(m) for m in msgs]
            filter_text = "\n".join([p for p in [caption_for_blocking, *meta_parts] if str(p).strip()])

            if filter_text and self._is_blocked(filter_text):
                log_event(
                    self.logger,
                    logging.INFO,
                    "album_skipped_blocked",
                    group_id=gid,
                    source_chat_id=source_chat_id,
                )
                return

            if any(self._is_gif_or_sticker(m) for m in msgs):
                log_event(
                    self.logger,
                    logging.INFO,
                    "album_skipped_gif_or_sticker",
                    group_id=gid,
                    source_chat_id=source_chat_id,
                )
                return

            if any(self._is_location_message(m) for m in msgs):
                log_event(
                    self.logger,
                    logging.INFO,
                    "album_skipped_location",
                    group_id=gid,
                    source_chat_id=source_chat_id,
                )
                return

            if any(self._is_disallowed_document(m) for m in msgs):
                log_event(
                    self.logger,
                    logging.INFO,
                    "album_skipped_disallowed_document",
                    group_id=gid,
                    source_chat_id=source_chat_id,
                )
                return

            if any(getattr(m, "media", None) for m in msgs) and not str(caption_for_blocking or "").strip():
                log_event(
                    self.logger,
                    logging.INFO,
                    "album_skipped_attachment_only",
                    group_id=gid,
                    source_chat_id=source_chat_id,
                )
                return

            if self._is_link_only(caption_for_blocking) and not extract_tweet_urls(caption_for_blocking or ""):
                log_event(
                    self.logger,
                    logging.INFO,
                    "album_skipped_link_only",
                    group_id=gid,
                    source_chat_id=source_chat_id,
                )
                return

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

                extra_text: str | None = None
                full_caption = caption
                if post_caption:
                    if len(str(post_caption)) <= MEDIA_CAPTION_LIMIT:
                        full_caption = str(post_caption)
                    else:
                        full_caption = None
                        extra_text = str(post_caption)

                if caption_for_blocking and self._is_blocked(caption_for_blocking):
                    log_event(self.logger, logging.INFO, "message_blocked", chat_id=chat_id, group_id=gid)
                    continue

                # Don't apply blocklist to post_caption overrides; only the inbound content.
                if not post_caption and full_caption and self._is_blocked(full_caption):
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

                    if extra_text:
                        await with_retry(
                            lambda: self.client.send_message(chat_id, message=extra_text, reply_to=reply_to),
                            retries=3,
                            base_delay=1,
                            logger=self.logger,
                            action="send_album_post_caption",
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

        if source_chat_id is not None and int(source_chat_id) in self._ignored_source_chats():
            log_event(
                self.logger,
                logging.INFO,
                "message_skipped_ignored_source",
                message_id=msg.id,
                source_chat_id=int(source_chat_id),
            )
            return

        # We use the original text for tweet URL detection even if strip_text is enabled.
        display_text = stripped_original_text
        if self.current_settings().get("strip_text"):
            display_text = ""

        media = getattr(msg, "media", None)
        uploadable_media = media is not None and not isinstance(media, types.MessageMediaWebPage)

        if self._is_location_message(msg):
            log_event(
                self.logger,
                logging.INFO,
                "message_skipped_location",
                message_id=msg.id,
                source_chat_id=source_chat_id,
            )
            return

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

        filter_haystack = self._filter_haystack(msg, stripped_original_text)
        if expanded is not None:
            names = [Path(str(p)).name for p in (expanded.files or [])]
            extra = "\n".join([n for n in names if str(n).strip()])
            if extra:
                filter_haystack = (filter_haystack + "\n" if filter_haystack else "") + extra

        if filter_haystack and self._is_blocked(filter_haystack):
            log_event(
                self.logger,
                logging.INFO,
                "message_skipped_blocked",
                message_id=msg.id,
                source_chat_id=source_chat_id,
            )
            if expanded is not None:
                expanded.cleanup()
            return

        if self._is_gif_or_sticker(msg):
            log_event(
                self.logger,
                logging.INFO,
                "message_skipped_gif_or_sticker",
                message_id=msg.id,
                source_chat_id=source_chat_id,
            )
            if expanded is not None:
                expanded.cleanup()
            return

        if self._is_disallowed_document(msg):
            log_event(
                self.logger,
                logging.INFO,
                "message_skipped_disallowed_document",
                message_id=msg.id,
                source_chat_id=source_chat_id,
            )
            if expanded is not None:
                expanded.cleanup()
            return

        if (uploadable_media or expanded is not None) and not stripped_original_text.strip():
            log_event(
                self.logger,
                logging.INFO,
                "message_skipped_attachment_only",
                message_id=msg.id,
                source_chat_id=source_chat_id,
            )
            if expanded is not None:
                expanded.cleanup()
            return

        if self._is_link_only(stripped_original_text) and not extract_tweet_urls(stripped_original_text) and expanded is None:
            log_event(
                self.logger,
                logging.INFO,
                "message_skipped_link_only",
                message_id=msg.id,
                source_chat_id=source_chat_id,
            )
            return

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

                extra_text: str | None = None
                full_text = display_text
                if post_caption:
                    if (uploadable_media or expanded is not None) and len(str(post_caption)) > MEDIA_CAPTION_LIMIT:
                        full_text = ""
                        extra_text = str(post_caption)
                    else:
                        full_text = str(post_caption)

                # Block based on original inbound text even if strip_text is enabled.
                if stripped_original_text and self._is_blocked(stripped_original_text):
                    log_event(self.logger, logging.INFO, "message_blocked", chat_id=chat_id, message_id=msg.id)
                    continue
                # Don't apply blocklist to post_caption overrides; only the inbound content.
                if not post_caption and full_text and self._is_blocked(full_text):
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
                    elif extra_text:
                        await with_retry(
                            lambda: self.client.send_message(chat_id, message=extra_text, reply_to=reply_to),
                            retries=3,
                            base_delay=1,
                            logger=self.logger,
                            action="send_post_caption",
                        )
                    else:
                        log_event(self.logger, logging.INFO, "message_skipped", chat_id=chat_id, message_id=msg.id)
                        continue

                    if extra_text and (uploadable_media or expanded is not None):
                        await with_retry(
                            lambda: self.client.send_message(chat_id, message=extra_text, reply_to=reply_to),
                            retries=3,
                            base_delay=1,
                            logger=self.logger,
                            action="send_post_caption",
                        )

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

    @client.on(events.CallbackQuery())
    async def callback_handler(event):
        await bot.handle_callback(event)

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
