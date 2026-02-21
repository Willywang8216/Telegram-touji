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
        env_file = Path(".env")
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


def load_relay_settings(manager: ConfigManager) -> dict[str, Any]:
    cfg = manager.load()
    relay = cfg.get("relay", {})
    api_id = _env_int("RELAY_API_ID", _env_int("API_ID", int(relay.get("api_id", cfg.get("api_id", 0)))))
    api_hash = _env_str("RELAY_API_HASH", _env_str("API_HASH", relay.get("api_hash", cfg.get("api_hash", ""))))
    bot_token = _env_str("RELAY_BOT_TOKEN", relay.get("bot_token", ""))

    dest_raw = _env_str("RELAY_DEST_CHANNELS")
    if dest_raw:
        dest_channels = [int(x.strip()) for x in dest_raw.split(",") if x.strip()]
    else:
        dest_channels = [int(x) for x in relay.get("dest_channels", [])]

    default_destinations = relay.get("default_destinations")
    if default_destinations is None:
        default_destinations = [{"chat_id": cid} for cid in dest_channels]

    routes = relay.get("routes", [])
    strip_text = bool(relay.get("strip_text", True))
    blocklist_substrings = relay.get("blocklist_substrings", [])
    blocklist_regexes = relay.get("blocklist_regexes", [])
    block_contact_ads = bool(relay.get("block_contact_ads", True))
    contact_ad_keywords = relay.get("contact_ad_keywords")

    fallback_to_general_topic = bool(relay.get("fallback_to_general_topic", True))

    ensure_forum_topics = relay.get("ensure_forum_topics", [])
    forum_topic_top_messages = relay.get("forum_topic_top_messages", {})

    if not api_id or not api_hash or not bot_token or (not default_destinations and not routes):
        raise ValueError("Relay 配置缺失: api_id/api_hash/bot_token/(default_destinations|routes)")

    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "bot_token": bot_token,
        "dest_channels": dest_channels,
        "default_destinations": default_destinations,
        "routes": routes,
        "strip_text": strip_text,
        "blocklist_substrings": blocklist_substrings,
        "blocklist_regexes": blocklist_regexes,
        "block_contact_ads": block_contact_ads,
        "contact_ad_keywords": contact_ad_keywords,
        "fallback_to_general_topic": fallback_to_general_topic,
        "ensure_forum_topics": ensure_forum_topics,
        "forum_topic_top_messages": forum_topic_top_messages,
    }
