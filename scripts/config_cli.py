import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when running as `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common_config import ConfigManager


def _load_config(path: str) -> tuple[ConfigManager, dict]:
    mgr = ConfigManager(path)
    cfg = mgr.load(force=True)
    return mgr, cfg


def _save_config(mgr: ConfigManager, cfg: dict, *, write: bool) -> None:
    if write:
        mgr.save(cfg)


def _json_dest(s: str) -> dict:
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid --dest JSON: {exc}: {s}")
    if not isinstance(obj, dict) or obj.get("chat_id") is None:
        raise SystemExit(f"--dest must be a JSON object with chat_id: {s}")
    obj["chat_id"] = int(obj["chat_id"])
    return obj


def cmd_add_listen(args) -> None:
    mgr, cfg = _load_config(args.config)
    mappings = list(cfg.get("bot_mappings") or [])

    src = args.source
    target_bot = args.target_bot

    # normalize (keep as string in config to preserve @username sources if used)
    src_key = str(src)

    replaced = False
    for m in mappings:
        if str(m.get("source_chat")) == src_key:
            m["target_bot"] = str(target_bot)
            replaced = True

    if not replaced:
        mappings.append({"source_chat": src, "target_bot": str(target_bot)})

    cfg["bot_mappings"] = mappings
    _save_config(mgr, cfg, write=bool(args.write))
    print(json.dumps({"ok": True, "action": "add_listen", "source_chat": src, "target_bot": target_bot}, ensure_ascii=False))


def cmd_remove_listen(args) -> None:
    mgr, cfg = _load_config(args.config)
    mappings = list(cfg.get("bot_mappings") or [])

    src_key = str(args.source)
    before = len(mappings)
    mappings = [m for m in mappings if str(m.get("source_chat")) != src_key]

    cfg["bot_mappings"] = mappings
    _save_config(mgr, cfg, write=bool(args.write))
    print(json.dumps({"ok": True, "action": "remove_listen", "removed": before - len(mappings)}, ensure_ascii=False))


def cmd_add_route(args) -> None:
    mgr, cfg = _load_config(args.config)
    relay = cfg.get("relay") or {}

    routes = list(relay.get("routes") or [])

    try:
        src_chat = int(args.source_chat)
    except Exception:  # noqa: BLE001
        raise SystemExit("--source-chat must be an integer peer id (e.g. -100...) for routing")

    dests = [_json_dest(x) for x in (args.dest or [])]
    if not dests:
        raise SystemExit("Provide at least one --dest '{\"chat_id\":..., ...}'")

    # find existing route
    route = None
    for r in routes:
        try:
            if "source_chat" in r and int(r.get("source_chat")) == int(src_chat):
                route = r
                break
        except Exception:  # noqa: BLE001
            continue

    if route is None:
        route = {"source_chat": int(src_chat), "destinations": []}
        routes.append(route)

    existing = list(route.get("destinations") or route.get("dest_channels") or [])
    existing_norm = []
    for d in existing:
        if isinstance(d, int):
            existing_norm.append({"chat_id": int(d)})
        elif isinstance(d, dict):
            dd = dict(d)
            if dd.get("chat_id") is not None:
                dd["chat_id"] = int(dd["chat_id"])
            existing_norm.append(dd)

    # append if not identical
    for d in dests:
        if d not in existing_norm:
            existing_norm.append(d)

    route["destinations"] = existing_norm

    relay["routes"] = routes
    cfg["relay"] = relay

    _save_config(mgr, cfg, write=bool(args.write))
    print(json.dumps({"ok": True, "action": "add_route", "source_chat": src_chat, "destinations": dests}, ensure_ascii=False))


def cmd_remove_route(args) -> None:
    mgr, cfg = _load_config(args.config)
    relay = cfg.get("relay") or {}

    routes = list(relay.get("routes") or [])

    try:
        src_chat = int(args.source_chat)
    except Exception:  # noqa: BLE001
        raise SystemExit("--source-chat must be an integer peer id (e.g. -100...)")

    before = len(routes)
    routes = [r for r in routes if not ("source_chat" in r and int(r.get("source_chat")) == int(src_chat))]

    relay["routes"] = routes
    cfg["relay"] = relay

    _save_config(mgr, cfg, write=bool(args.write))
    print(json.dumps({"ok": True, "action": "remove_route", "source_chat": src_chat, "removed": before - len(routes)}, ensure_ascii=False))


def cmd_show(args) -> None:
    _, cfg = _load_config(args.config)

    relay = cfg.get("relay") or {}
    summary = {
        "bot_mappings": cfg.get("bot_mappings") or [],
        "relay": {
            "default_destinations": relay.get("default_destinations"),
            "routes": relay.get("routes"),
            "ensure_forum_topics": relay.get("ensure_forum_topics"),
            "forum_topic_top_messages": relay.get("forum_topic_top_messages"),
        },
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="CLI helper to edit config.json (bot_mappings and relay.routes)")
    parser.add_argument("--config", default="config.json", help="Path to config.json")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-listen", help="Add/replace an entry in bot_mappings")
    p.add_argument("--source", required=True, help="source_chat (e.g. -100... or @username)")
    p.add_argument("--target-bot", required=True, help="@middle_bot_username")
    p.add_argument("--write", action="store_true", help="Write changes to config.json")
    p.set_defaults(func=cmd_add_listen)

    p = sub.add_parser("remove-listen", help="Remove an entry from bot_mappings")
    p.add_argument("--source", required=True, help="source_chat to remove")
    p.add_argument("--write", action="store_true", help="Write changes to config.json")
    p.set_defaults(func=cmd_remove_listen)

    p = sub.add_parser("add-route", help="Add/merge a relay.routes entry for a source_chat")
    p.add_argument("--source-chat", required=True, help="Integer peer id (e.g. -100...)")
    p.add_argument(
        "--dest",
        action="append",
        help=(
            "Destination as JSON. Repeatable. Example: "
            "--dest '{\"chat_id\":-1001,\"topic_title\":\"General\"}' "
            "or --dest '{\"chat_id\":-1002,\"bucket_topics\":{\"prefix\":\"Bucket \",\"count\":5,\"by\":\"source\"}}'"
        ),
    )
    p.add_argument("--write", action="store_true", help="Write changes to config.json")
    p.set_defaults(func=cmd_add_route)

    p = sub.add_parser("remove-route", help="Remove a relay.routes entry by source_chat")
    p.add_argument("--source-chat", required=True, help="Integer peer id (e.g. -100...)")
    p.add_argument("--write", action="store_true", help="Write changes to config.json")
    p.set_defaults(func=cmd_remove_route)

    p = sub.add_parser("show", help="Print a config summary")
    p.add_argument("--json", action="store_true", help="Output JSON")
    p.set_defaults(func=cmd_show)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
