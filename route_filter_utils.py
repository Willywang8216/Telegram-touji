import json
import shlex
from typing import Any


def parse_route_filters(args: str) -> dict[str, Any]:
    """Parse /list_routes and /export_routes filters.

    Supported tokens (shlex-split):
    - source=<chat_id>[,<chat_id>...]
    - dest=<chat_id>[,<chat_id>...]
    - topic=<substring>
    - topic_id=<top_message_id>
    - any other token becomes a free-text term
    """

    tokens = shlex.split(args or "")
    out: dict[str, Any] = {"source": [], "dest": [], "topic": None, "topic_id": None, "terms": []}

    def _extend_int_list(key: str, raw: str) -> None:
        for part in str(raw or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out[key].append(int(part))
            except Exception:  # noqa: BLE001
                continue

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
                _extend_int_list("source", v)
                continue

            if k in {"dest", "dst"}:
                _extend_int_list("dest", v)
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

    if not out["source"]:
        out["source"] = None
    elif len(out["source"]) == 1:
        out["source"] = out["source"][0]

    if not out["dest"]:
        out["dest"] = None
    elif len(out["dest"]) == 1:
        out["dest"] = out["dest"][0]

    return out


def _route_blob(route: dict[str, Any]) -> str:
    try:
        return json.dumps(route, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(route)


def _as_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out: list[int] = []
        for x in value:
            try:
                out.append(int(x))
            except Exception:  # noqa: BLE001
                continue
        return out
    try:
        return [int(value)]
    except Exception:  # noqa: BLE001
        return []


def filter_routes(routes: list[dict[str, Any]], *, filters: dict[str, Any]) -> list[dict[str, Any]]:
    srcs = _as_int_list(filters.get("source"))
    dests_filter = _as_int_list(filters.get("dest"))

    topic = str(filters.get("topic") or "").casefold().strip() or None
    topic_id = filters.get("topic_id")

    terms = [str(t).casefold() for t in (filters.get("terms") or []) if str(t).strip()]

    out: list[dict[str, Any]] = []

    for r in routes or []:
        if srcs and not any(int(s) in (r.get("source_chats") or []) for s in srcs):
            continue

        if dests_filter:
            rdests = r.get("destinations") or []
            if not any(
                int(d.get("chat_id")) in dests_filter
                for d in rdests
                if d.get("chat_id") is not None
            ):
                continue

        if topic_id is not None:
            rdests = r.get("destinations") or []
            if not any(int(d.get("topic_id")) == int(topic_id) for d in rdests if d.get("topic_id") is not None):
                continue

        if topic is not None:
            rdests = r.get("destinations") or []
            if not any(topic in str(d.get("topic_title") or "").casefold() for d in rdests):
                continue

        if terms:
            blob = _route_blob(r).casefold()
            if any(t not in blob for t in terms):
                continue

        out.append(r)

    return out
