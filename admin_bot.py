import asyncio
import logging
from pathlib import Path

from telethon import Button, TelegramClient, events, functions, types, utils

from common_config import ConfigManager, load_admin_bot_settings, load_userbot_settings
from structured_logger import get_logger, log_event

logger = get_logger("adminbot")
config_manager = ConfigManager()

user_settings = load_userbot_settings(config_manager)
admin_settings = load_admin_bot_settings(config_manager)

session_dir = Path("data/sessions")
session_dir.mkdir(parents=True, exist_ok=True)


def _is_admin(sender_id: int) -> bool:
    return int(sender_id) in {int(x) for x in (admin_settings.get("admin_user_ids") or [])}


def _entity_kind(ent) -> str:
    if isinstance(ent, types.User):
        return "user"
    if isinstance(ent, types.Chat):
        return "group"
    if isinstance(ent, types.Channel):
        if bool(getattr(ent, "megagroup", False)):
            return "supergroup"
        if bool(getattr(ent, "broadcast", False)):
            return "channel"
        return "channel"
    return type(ent).__name__


async def _fetch_dialogs(user_client: TelegramClient) -> list[dict]:
    out: list[dict] = []

    async for d in user_client.iter_dialogs():
        ent = getattr(d, "entity", None)
        if ent is None:
            continue

        kind = _entity_kind(ent)
        if kind not in {"group", "supergroup", "channel"}:
            continue

        try:
            peer_id = int(utils.get_peer_id(ent))
        except Exception:  # noqa: BLE001
            continue

        title = getattr(ent, "title", None) or getattr(ent, "username", None) or ""
        title = str(title)
        username = getattr(ent, "username", None)
        username = f"@{username}" if username else None

        forum = bool(getattr(ent, "forum", False)) if isinstance(ent, types.Channel) else False

        out.append({"peer_id": peer_id, "kind": kind, "title": title, "username": username, "forum": forum})

    out.sort(key=lambda x: (x.get("title") or "").lower())
    return out


def _mapped_sources(cfg: dict) -> tuple[set[int], set[str]]:
    id_set: set[int] = set()
    user_set: set[str] = set()
    for m in (cfg.get("bot_mappings") or []):
        src = m.get("source_chat")
        if src is None:
            continue
        s = str(src).strip()
        if not s:
            continue
        if s.startswith("@"): 
            user_set.add(s.lower())
            continue
        try:
            id_set.add(int(s))
        except Exception:  # noqa: BLE001
            continue
    return id_set, user_set


def _dialog_is_mapped(dialog: dict, mapped_ids: set[int], mapped_usernames: set[str]) -> bool:
    if int(dialog.get("peer_id")) in mapped_ids:
        return True
    u = (dialog.get("username") or "").lower().strip()
    return bool(u) and u in mapped_usernames


def _dest_candidates(cfg: dict) -> list[int]:
    relay = cfg.get("relay") or {}
    out: set[int] = set()

    for cid in relay.get("dest_channels") or []:
        try:
            out.add(int(cid))
        except Exception:  # noqa: BLE001
            continue

    for d in relay.get("default_destinations") or []:
        if isinstance(d, int):
            out.add(int(d))
        elif isinstance(d, dict) and d.get("chat_id") is not None:
            out.add(int(d.get("chat_id")))

    for r in relay.get("routes") or []:
        for d in r.get("destinations") or r.get("dest_channels") or []:
            if isinstance(d, int):
                out.add(int(d))
            elif isinstance(d, dict) and d.get("chat_id") is not None:
                out.add(int(d.get("chat_id")))

    return sorted(out)


def _normalize_destinations(existing) -> list[dict]:
    out = []
    for d in existing or []:
        if isinstance(d, int):
            out.append({"chat_id": int(d)})
        elif isinstance(d, dict):
            dd = dict(d)
            if dd.get("chat_id") is not None:
                dd["chat_id"] = int(dd["chat_id"])
            out.append(dd)
    return out


def _add_or_replace_listen(cfg: dict, *, source_chat: int, target_bot: str) -> None:
    mappings = list(cfg.get("bot_mappings") or [])
    src_key = str(int(source_chat))

    replaced = False
    for m in mappings:
        if str(m.get("source_chat")) == src_key:
            m["target_bot"] = str(target_bot)
            replaced = True

    if not replaced:
        mappings.append({"source_chat": int(source_chat), "target_bot": str(target_bot)})

    cfg["bot_mappings"] = mappings


def _add_route(cfg: dict, *, source_chat: int, dest: dict) -> None:
    relay = cfg.get("relay") or {}
    routes = list(relay.get("routes") or [])

    route = None
    for r in routes:
        try:
            if "source_chat" in r and int(r.get("source_chat")) == int(source_chat):
                route = r
                break
        except Exception:  # noqa: BLE001
            continue

    if route is None:
        route = {"source_chat": int(source_chat), "destinations": []}
        routes.append(route)

    existing_norm = _normalize_destinations(route.get("destinations") or route.get("dest_channels") or [])

    dd = dict(dest)
    dd["chat_id"] = int(dd.get("chat_id"))

    if dd not in existing_norm:
        existing_norm.append(dd)

    route["destinations"] = existing_norm
    relay["routes"] = routes
    cfg["relay"] = relay


async def _list_forum_topics(user_client: TelegramClient, chat_id: int) -> list[str]:
    ent = await user_client.get_entity(int(chat_id))
    res = await user_client(
        functions.messages.GetForumTopicsRequest(
            peer=ent,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
            q="",
        )
    )
    return [t.title for t in (res.topics or []) if getattr(t, "title", None)]


def _main_menu_buttons():
    return [
        [Button.inline("Unmapped sources", b"m:unmapped"), Button.inline("Mapped sources", b"m:mapped")],
        [Button.inline("Routes", b"m:routes"), Button.inline("Refresh dialogs", b"m:refresh")],
    ]


def _chunk(items: list[dict], page: int, page_size: int = 8) -> tuple[list[dict], int]:
    page = max(0, int(page))
    start = page * page_size
    end = start + page_size
    total_pages = (len(items) + page_size - 1) // page_size if items else 1
    return items[start:end], total_pages


async def main():
    user_client = TelegramClient(
        str(session_dir / "userbot_tools"),
        user_settings["api_id"],
        user_settings["api_hash"],
        proxy=user_settings["proxy"],
    )
    await user_client.start()

    bot_client = TelegramClient(
        str(session_dir / "admin_bot"),
        admin_settings["api_id"],
        admin_settings["api_hash"],
    )
    await bot_client.start(bot_token=admin_settings["bot_token"])

    state: dict[int, dict] = {}
    dialogs_cache: list[dict] = []

    async def refresh_dialogs() -> None:
        nonlocal dialogs_cache
        dialogs_cache = await _fetch_dialogs(user_client)

    await refresh_dialogs()

    async def show_menu(chat_id: int, text: str = "Admin Bot"):
        await bot_client.send_message(chat_id, text, buttons=_main_menu_buttons())

    async def render_sources(event, *, mode: str, page: int = 0):
        cfg = config_manager.load(force=True)
        mapped_ids, mapped_usernames = _mapped_sources(cfg)

        items = dialogs_cache
        if mode == "unmapped":
            items = [d for d in dialogs_cache if not _dialog_is_mapped(d, mapped_ids, mapped_usernames)]
        elif mode == "mapped":
            items = [d for d in dialogs_cache if _dialog_is_mapped(d, mapped_ids, mapped_usernames)]

        subset, total_pages = _chunk(items, page)

        rows = []
        for d in subset:
            label = d.get("title") or str(d.get("peer_id"))
            rows.append([Button.inline(label[:60], f"src:{d['peer_id']}".encode())])

        nav = []
        if page > 0:
            nav.append(Button.inline("< Prev", f"p:{mode}:{page-1}".encode()))
        if page + 1 < total_pages:
            nav.append(Button.inline("Next >", f"p:{mode}:{page+1}".encode()))
        if nav:
            rows.append(nav)

        rows.append([Button.inline("Back", b"m:home")])

        title = f"{mode.title()} sources ({len(items)}) — page {page+1}/{total_pages}"
        await event.edit(title, buttons=rows)

    async def render_routes(event):
        cfg = config_manager.load(force=True)
        relay = cfg.get("relay") or {}
        routes = relay.get("routes") or []

        lines = []
        for r in routes:
            src = r.get("source_chat")
            dests = r.get("destinations") or r.get("dest_channels") or []
            lines.append(f"{src} -> {len(dests)} destinations")

        if not lines:
            lines = ["(no routes)"]

        await event.edit("Routes:\n" + "\n".join(lines[:50]), buttons=[[Button.inline("Back", b"m:home")]])

    async def render_source_actions(event, source_peer_id: int):
        cfg = config_manager.load(force=True)
        target_bot = None
        for m in cfg.get("bot_mappings") or []:
            if m.get("target_bot"):
                target_bot = str(m.get("target_bot"))
                break

        d = next((x for x in dialogs_cache if int(x.get("peer_id")) == int(source_peer_id)), None)
        title = d.get("title") if d else str(source_peer_id)
        username = d.get("username") if d else None

        text = f"Source: {title}\npeer_id: {source_peer_id}"
        if username:
            text += f"\nusername: {username}"

        buttons = [
            [Button.inline("Enable listen", f"act:listen:{source_peer_id}".encode())],
            [Button.inline("Add route", f"act:route:{source_peer_id}".encode())],
            [Button.inline("Back", b"m:home")],
        ]

        if not target_bot:
            text += "\n\nNo existing bot_mappings found. Create one in config.json first (or via scripts/install.sh)."

        await event.edit(text, buttons=buttons)

    async def render_destinations(event, source_peer_id: int):
        cfg = config_manager.load(force=True)
        dests = _dest_candidates(cfg)
        if not dests:
            await event.edit(
                "No destinations found in config. Add relay.dest_channels or relay.default_destinations first.",
                buttons=[[Button.inline("Back", b"m:home")]],
            )
            return

        rows = []
        for cid in dests[:40]:
            rows.append([Button.inline(str(cid), f"dst:{source_peer_id}:{cid}".encode())])
        rows.append([Button.inline("Back", b"m:home")])
        await event.edit(f"Pick destination for source {source_peer_id}:", buttons=rows)

    async def render_route_mode(event, source_peer_id: int, dest_chat_id: int):
        rows = [
            [Button.inline("Topic from source title", f"mode:fromsrc:{source_peer_id}:{dest_chat_id}".encode())],
            [Button.inline("Pick existing topic", f"mode:pick:{source_peer_id}:{dest_chat_id}".encode())],
            [Button.inline("Manual topic title", f"mode:manual:{source_peer_id}:{dest_chat_id}".encode())],
            [Button.inline("Back", b"m:home")],
        ]
        await event.edit(f"Routing mode (source {source_peer_id} -> dest {dest_chat_id}):", buttons=rows)

    async def render_topics(event, source_peer_id: int, dest_chat_id: int, page: int = 0):
        sender_id = int(event.sender_id)
        topics = state.get(sender_id, {}).get("topics") or []
        subset, total_pages = _chunk([{"title": t} for t in topics], page)

        rows = []
        offset = page * 8
        for i, item in enumerate(subset):
            title = item["title"]
            rows.append([Button.inline(title[:60], f"t:{offset+i}".encode())])

        nav = []
        if page > 0:
            nav.append(Button.inline("< Prev", f"tp:{page-1}".encode()))
        if page + 1 < total_pages:
            nav.append(Button.inline("Next >", f"tp:{page+1}".encode()))
        if nav:
            rows.append(nav)

        rows.append([Button.inline("Back", b"m:home")])
        await event.edit(f"Pick topic — page {page+1}/{total_pages}", buttons=rows)

    @bot_client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_message(event):
        if not _is_admin(int(event.sender_id)):
            return

        text = (event.raw_text or "").strip()
        sender_id = int(event.sender_id)

        if text.startswith("/start"):
            await show_menu(event.chat_id, "Admin Bot")
            return

        st = state.get(sender_id) or {}
        if st.get("step") == "await_manual_topic":
            topic = text
            src = int(st.get("source_peer_id"))
            dest = int(st.get("dest_chat_id"))

            cfg = config_manager.load(force=True)
            _add_route(cfg, source_chat=src, dest={"chat_id": dest, "topic_title": topic})
            config_manager.save(cfg)

            await user_client.send_message("me", "config updated")

            state.pop(sender_id, None)
            await show_menu(event.chat_id, f"Route added: {src} -> {dest} (topic_title={topic})")
            return

        await show_menu(event.chat_id, "Admin Bot\n\nUse the buttons below.")

    @bot_client.on(events.CallbackQuery())
    async def on_callback(event):
        if not _is_admin(int(event.sender_id)):
            await event.answer("Not allowed", alert=True)
            return

        data = (event.data or b"").decode(errors="ignore")
        sender_id = int(event.sender_id)

        if data == "m:home":
            await event.edit("Admin Bot", buttons=_main_menu_buttons())
            return

        if data == "m:refresh":
            await refresh_dialogs()
            await event.edit("Dialogs refreshed.", buttons=_main_menu_buttons())
            return

        if data in {"m:unmapped", "m:mapped"}:
            await event.edit("Loading...")
            await render_sources(event, mode=data.split(":", 1)[1], page=0)
            return

        if data == "m:routes":
            await render_routes(event)
            return

        if data.startswith("p:"):
            _, mode, page_s = data.split(":", 2)
            await render_sources(event, mode=mode, page=int(page_s))
            return

        if data.startswith("src:"):
            src = int(data.split(":", 1)[1])
            await render_source_actions(event, src)
            return

        if data.startswith("act:listen:"):
            src = int(data.split(":", 2)[2])

            cfg = config_manager.load(force=True)
            target_bot = None
            for m in cfg.get("bot_mappings") or []:
                if m.get("target_bot"):
                    target_bot = str(m.get("target_bot"))
                    break

            if not target_bot:
                await event.edit(
                    "No target_bot found. Create one bot_mappings entry first.",
                    buttons=[[Button.inline("Back", b"m:home")]],
                )
                return

            _add_or_replace_listen(cfg, source_chat=src, target_bot=target_bot)
            config_manager.save(cfg)

            await user_client.send_message("me", "config updated")
            log_event(logger, logging.INFO, "listen_added", source_chat=src, target_bot=target_bot)
            await event.edit(f"Listen enabled: {src} -> {target_bot}", buttons=_main_menu_buttons())
            return

        if data.startswith("act:route:"):
            src = int(data.split(":", 2)[2])
            await render_destinations(event, src)
            return

        if data.startswith("dst:"):
            _, src_s, dest_s = data.split(":", 2)
            await render_route_mode(event, int(src_s), int(dest_s))
            return

        if data.startswith("mode:fromsrc:"):
            _, _, src_s, dest_s = data.split(":", 3)
            src = int(src_s)
            dest = int(dest_s)
            cfg = config_manager.load(force=True)
            _add_route(cfg, source_chat=src, dest={"chat_id": dest, "topic_from_source": True})
            config_manager.save(cfg)
            await user_client.send_message("me", "config updated")
            await event.edit(f"Route added: {src} -> {dest} (topic_from_source=true)", buttons=_main_menu_buttons())
            return

        if data.startswith("mode:manual:"):
            _, _, src_s, dest_s = data.split(":", 3)
            state[sender_id] = {"step": "await_manual_topic", "source_peer_id": int(src_s), "dest_chat_id": int(dest_s)}
            await event.edit("Send me the topic title as a message.", buttons=[[Button.inline("Cancel", b"m:home")]])
            return

        if data.startswith("mode:pick:"):
            _, _, src_s, dest_s = data.split(":", 3)
            src = int(src_s)
            dest = int(dest_s)

            await event.edit("Loading topics...")
            try:
                topics = await _list_forum_topics(user_client, dest)
            except Exception as exc:  # noqa: BLE001
                await event.edit(
                    f"Failed to list topics for {dest}: {exc}\n\nUse 'Manual topic title' instead.",
                    buttons=[[Button.inline("Back", b"m:home")]],
                )
                return

            if not topics:
                await event.edit(
                    f"No topics found for {dest}. Use 'Manual topic title' or create topics first.",
                    buttons=[[Button.inline("Back", b"m:home")]],
                )
                return

            state[sender_id] = {"step": "pick_topic", "source_peer_id": src, "dest_chat_id": dest, "topics": topics}
            await render_topics(event, src, dest, page=0)
            return

        if data.startswith("tp:"):
            st = state.get(sender_id) or {}
            if st.get("step") != "pick_topic":
                await event.answer("No active topic picker", alert=True)
                return

            await render_topics(event, int(st.get("source_peer_id")), int(st.get("dest_chat_id")), page=int(data.split(":", 1)[1]))
            return

        if data.startswith("t:"):
            st = state.get(sender_id) or {}
            if st.get("step") != "pick_topic":
                await event.answer("No active topic picker", alert=True)
                return

            idx = int(data.split(":", 1)[1])
            topics = st.get("topics") or []
            if idx < 0 or idx >= len(topics):
                await event.answer("Invalid selection", alert=True)
                return

            src = int(st.get("source_peer_id"))
            dest = int(st.get("dest_chat_id"))
            title = str(topics[idx])

            cfg = config_manager.load(force=True)
            _add_route(cfg, source_chat=src, dest={"chat_id": dest, "topic_title": title})
            config_manager.save(cfg)

            await user_client.send_message("me", "config updated")

            state.pop(sender_id, None)
            await event.edit(f"Route added: {src} -> {dest} (topic_title={title})", buttons=_main_menu_buttons())
            return

        await event.answer("Unknown action", alert=True)

    log_event(logger, logging.INFO, "adminbot_started", admin_user_ids=admin_settings.get("admin_user_ids"))
    await bot_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
