import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")


class ConfigManager:
    def __init__(self, path: str | None = None):
        self.path = Path(path or DEFAULT_CONFIG_PATH)
        self._mtime: float | None = None
        self._config: dict[str, Any] | None = None

    def _load_dotenv(self) -> None:
        # Load a .env from the same directory as the config file (keeps tests deterministic
        # and avoids accidentally reading unrelated .env files).
        env_file = self.path.parent / ".env"
        if not env_file.exists():
            return
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

    def load(self, force: bool = False) -> dict[str, Any]:
        self._load_dotenv()
        mtime = self.path.stat().st_mtime
        if force or self._config is None or self._mtime != mtime:
            with self.path.open("r", encoding="utf-8") as f:
                self._config = json.load(f)
            self._mtime = mtime
        return self._config

    def reload_if_changed(self) -> bool:
        mtime = self.path.stat().st_mtime
        if self._mtime is None or mtime != self._mtime:
            self.load(force=True)
            return True
        return False

    def save(self, config: dict[str, Any]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        self._config = config
        self._mtime = self.path.stat().st_mtime


def _env_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def _env_str(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def build_proxy(proxy_cfg: dict[str, Any] | None):
    if not proxy_cfg or not proxy_cfg.get("proxy_type"):
        return None
    proxy_type = str(proxy_cfg.get("proxy_type", "")).lower()
    return (
        proxy_type,
        proxy_cfg.get("addr"),
        proxy_cfg.get("port"),
        proxy_cfg.get("username"),
        proxy_cfg.get("password"),
    )


def load_userbot_settings(manager: ConfigManager) -> dict[str, Any]:
    cfg = manager.load()
    return {
        "api_id": _env_int("API_ID", int(cfg["api_id"])),
        "api_hash": _env_str("API_HASH", cfg["api_hash"]),
        "master_account_id": _env_int("MASTER_ACCOUNT_ID", int(cfg["master_account_id"])),
        "bot_mappings": cfg.get("bot_mappings", []),
        "proxy": build_proxy(cfg.get("proxy")),
    }


def _normalize_chat_id(value: Any) -> int:
    # Accept already-int or stringified int.
    return int(value)


def _normalize_destinations(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if "chat_id" not in item:
            continue
        dest = dict(item)
        dest["chat_id"] = _normalize_chat_id(dest["chat_id"])
        out.append(dest)
    return out


def load_relay_settings(manager: ConfigManager) -> dict[str, Any]:
    cfg = manager.load()
    relay = cfg.get("relay", {})

    api_id = _env_int("RELAY_API_ID", _env_int("API_ID", int(relay.get("api_id", cfg.get("api_id", 0)))))
    api_hash = _env_str("RELAY_API_HASH", _env_str("API_HASH", relay.get("api_hash", cfg.get("api_hash", ""))))
    bot_token = _env_str("RELAY_BOT_TOKEN", relay.get("bot_token", ""))

    # Optional: restrict which user is allowed to DM the relay bot to trigger relays.
    # If unset/0 -> allow any sender (less safe, but backwards-compatible).
    master_account_id = _env_int(
        "RELAY_MASTER_ACCOUNT_ID",
        int(relay.get("master_account_id", 0) or 0),
    )

    dest_raw = _env_str("RELAY_DEST_CHANNELS")
    if dest_raw:
        dest_channels = [int(x.strip()) for x in dest_raw.split(",") if x.strip()]
    else:
        dest_channels = [int(x) for x in relay.get("dest_channels", [])]

    if not api_id or not api_hash or not bot_token or not dest_channels:
        raise ValueError("Relay 配置缺失: api_id/api_hash/bot_token/dest_channels")

    post_captions = relay.get("post_captions", {})
    post_captions_norm: dict[int, str] = {}
    if isinstance(post_captions, dict):
        for k, v in post_captions.items():
            try:
                post_captions_norm[_normalize_chat_id(k)] = str(v)
            except Exception:  # noqa: BLE001
                continue

    fallback_topic_titles = relay.get("fallback_topic_titles", {})
    fallback_topic_titles_norm: dict[int, str] = {}
    if isinstance(fallback_topic_titles, dict):
        for k, v in fallback_topic_titles.items():
            try:
                fallback_topic_titles_norm[_normalize_chat_id(k)] = str(v)
            except Exception:  # noqa: BLE001
                continue

    routes_raw = relay.get("routes", [])
    routes: list[dict[str, Any]] = []
    if isinstance(routes_raw, list):
        for r in routes_raw:
            if not isinstance(r, dict):
                continue
            source_chats = [_normalize_chat_id(x) for x in r.get("source_chats", [])]
            destinations = _normalize_destinations(r.get("destinations", []))
            if not source_chats or not destinations:
                continue
            routes.append({"source_chats": source_chats, "destinations": destinations})

    default_destinations = _normalize_destinations(relay.get("default_destinations", []))

    general_topic_buckets = relay.get("general_topic_buckets", {})
    general_topic_buckets_norm: dict[int, list[str]] = {}
    if isinstance(general_topic_buckets, dict):
        for k, v in general_topic_buckets.items():
            try:
                chat_id = _normalize_chat_id(k)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(v, list):
                continue
            general_topic_buckets_norm[chat_id] = [str(x) for x in v if str(x).strip()]

    ensure_forum_topics_raw = relay.get("ensure_forum_topics", [])
    ensure_forum_topics: list[dict[str, Any]] = []
    if isinstance(ensure_forum_topics_raw, list):
        for item in ensure_forum_topics_raw:
            if not isinstance(item, dict):
                continue
            if "chat_id" not in item:
                continue
            try:
                chat_id = _normalize_chat_id(item["chat_id"])
            except Exception:  # noqa: BLE001
                continue
            topics = item.get("topics", []) or []
            if not isinstance(topics, list):
                continue
            topics_norm = [str(x) for x in topics if str(x).strip()]
            ensure_forum_topics.append({"chat_id": chat_id, "topics": topics_norm})

    topic_renames_raw = relay.get("topic_renames", {})
    topic_renames: dict[int, dict[str, str]] = {}
    if isinstance(topic_renames_raw, dict):
        for k, v in topic_renames_raw.items():
            try:
                chat_id = _normalize_chat_id(k)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(v, dict):
                continue
            mapping: dict[str, str] = {}
            for old, new in v.items():
                if str(old).strip() and str(new).strip():
                    mapping[str(old)] = str(new)
            if mapping:
                topic_renames[chat_id] = mapping

    topic_deletes_raw = relay.get("topic_deletes", {})
    topic_deletes: dict[int, list[str]] = {}
    if isinstance(topic_deletes_raw, dict):
        for k, v in topic_deletes_raw.items():
            try:
                chat_id = _normalize_chat_id(k)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(v, list):
                continue
            titles = [str(x) for x in v if str(x).strip()]
            if titles:
                topic_deletes[chat_id] = titles

    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "bot_token": bot_token,
        "dest_channels": dest_channels,
        "master_account_id": int(master_account_id or 0),
        # Optional relay behavior controls (used by bot_relay.py)
        "strip_text": bool(relay.get("strip_text", False)),
        "post_captions": post_captions_norm,
        "blocklist_substrings": list(relay.get("blocklist_substrings", []) or []),
        "routes": routes,
        "default_destinations": default_destinations,
        "fallback_topic_titles": fallback_topic_titles_norm,
        "fallback_to_general_topic": bool(relay.get("fallback_to_general_topic", False)),
        # If enabled: unrouted sources get distributed into topic buckets per destination chat.
        "distribute_unrouted_to_buckets": bool(relay.get("distribute_unrouted_to_buckets", False)),
        # "source" (default): stable bucket per source_chat_id
        # "message": bucket varies per forwarded message/album (more even distribution)
        "unrouted_distribution_mode": str(relay.get("unrouted_distribution_mode", "source") or "source"),
        "general_topic_buckets": general_topic_buckets_norm,
        # Optional forum topic management (used by bot_relay.py)
        "ensure_forum_topics": ensure_forum_topics,
        "topic_renames": topic_renames,
        "topic_deletes": topic_deletes,
    }
