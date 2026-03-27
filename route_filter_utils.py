import json
import shlex
from typing import Any


def parse_route_filters(args: str) -> dict[str, Any]:
    """Parse /list_routes and /export_routes filters.

    Supported tokens (shlex-split):
    - source=<chat_id>
    - dest=<chat_id>
    - topic=<substring>
    - topic_id=<top_message_id>
    - any other token becomes a free-text term
    """

    tokens = shlex.split(args or "")
    out: dict[str, Any] = {"source": None, "dest": None, "topic": None, "topic_id": None, "terms": []}

    for tok in tokens:
        if not tok:
            continue
        if "=" in tok:
            k, v = tok.split("=", 1)
            k = (k or "").strip().lower()
            v = (v or "").strip()
            if not v:
                continue

            if k in {"source", "src"}:
                try:
                    out["source"] = int(v)
                except Exception:  # noqa: BLE001
                    continue
                continue

            if k in {"dest", "dst"}:
                try:
                    out["dest"] = int(v)
                except Exception:  # noqa: BLE001
                    continue
                continue

            if k in {"topic"}:
                out["topic"] = v
                continue

            if k in {"topic_id", "tid"}:
                try:
                    out["topic_id"] = int(v)
                except Exception:  # noqa: BLE001
                    continue
                continue

            out["terms"].append(tok)
            continue

        out["terms"].append(tok)

    out["terms"] = [str(x) for x in (out.get("terms") or []) if str(x).strip()]
    return out


def _route_blob(route: dict[str, Any]) -> str:
    try:
        return json.dumps(route, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(route)


def filter_routes(routes: list[dict[str, Any]], *, filters: dict[str, Any]) -> list[dict[str, Any]]:
    src = filters.get("source")
    dest = filters.get("dest")
    topic = str(filters.get("topic") or "").casefold().strip() or None
    topic_id = filters.get("topic_id")

    terms = [str(t).casefold() for t in (filters.get("terms") or []) if str(t).strip()]

    out: list[dict[str, Any]] = []

    for r in routes or []:
        if src is not None and src not in (r.get("source_chats") or []):
            continue

        if dest is not None:
            dests = r.get("destinations") or []
            if not any(int(d.get("chat_id")) == int(dest) for d in dests if d.get("chat_id") is not None):
                continue

        if topic_id is not None:
            dests = r.get("destinations") or []
            if not any(int(d.get("topic_id")) == int(topic_id) for d in dests if d.get("topic_id") is not None):
                continue

        if topic is not None:
            dests = r.get("destinations") or []
            if not any(topic in str(d.get("topic_title") or "").casefold() for d in dests):
                continue

        if terms:
            blob = _route_blob(r).casefold()
            if any(t not in blob for t in terms):
                continue

        out.append(r)

    return out
