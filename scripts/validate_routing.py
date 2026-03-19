import json
import sys
from collections import Counter
from pathlib import Path


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else Path("config.json")
    if not path.exists():
        print(f"Config not found: {path}", file=sys.stderr)
        return 2

    cfg = load_config(path)
    relay = cfg.get("relay", {})

    bot_mappings = cfg.get("bot_mappings", []) or []
    bot_sources = [int(m["source_chat"]) for m in bot_mappings if isinstance(m, dict) and "source_chat" in m]

    routes = relay.get("routes", []) or []
    route_sources: list[int] = []
    for r in routes:
        if not isinstance(r, dict):
            continue
        for x in r.get("source_chats", []) or []:
            try:
                route_sources.append(int(x))
            except Exception:
                pass

    bot_sources_set = set(bot_sources)
    route_sources_set = set(route_sources)

    dupes = [k for k, c in Counter(route_sources).items() if c > 1]
    missing_in_bot_mappings = sorted(route_sources_set - bot_sources_set)

    print(f"Config: {path}")
    print(f"bot_mappings sources: {len(bot_sources_set)}")
    print(f"routes sources: {len(route_sources_set)}")

    if dupes:
        print("\nERROR: source_chats appear in multiple routes (will match first route only):")
        for s in sorted(dupes):
            print(f"  - {s}")

    if missing_in_bot_mappings:
        print("\nERROR: source_chats listed in routes but missing in bot_mappings (will never be forwarded):")
        for s in missing_in_bot_mappings:
            print(f"  - {s}")

    not_routed = sorted(bot_sources_set - route_sources_set)
    if not_routed:
        print("\nINFO: sources present in bot_mappings but not in any route (will go to default_destinations / dest_channels):")
        for s in not_routed[:50]:
            print(f"  - {s}")
        if len(not_routed) > 50:
            print(f"  ... and {len(not_routed) - 50} more")

    # Exit code policy:
    # - duplicates or missing sources are hard errors
    # - unrouted sources are informational
    if dupes or missing_in_bot_mappings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
