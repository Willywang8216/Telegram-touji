# Telegram Stealth Relay Bot (Dockerized)

一个基于 **Telethon + Docker Compose** 的 Telegram 消息中继系统：
- `telegram_bot.py`（Userbot）负责监听源频道/群
- `bot_relay.py`（RelayBot）负责无痕重发到目标频道

---

## ✨ 功能概览

- 支持多源到多目标的消息中继
- 支持相册（media group）聚合转发
- 支持命令过滤（`/`）与系统回执过滤（`🤖`）
- 统一配置模块（JSON + `.env` 覆盖）

---

## ✅ 首次使用（推荐）

1) 复制环境变量模板并填写：

```bash
cp .env.example .env
```

2) 生成 `config.json`（推荐用向导）：

```bash
python scripts/config_wizard.py
```

3) 启动：

```bash
docker compose up -d --build
```

> 注意：请先 `cd` 到**仓库根目录**再运行 `docker compose ...`。如果你在 `~` 等其他目录运行，可能会把巨大目录作为 build context 发送到 Docker，导致构建很慢甚至出现 `permission denied`。

## Configuration FAQ

### `MASTER_ACCOUNT_ID` 是什么？
是你的 **Telegram 账号的数字 user id**。该账号可以私聊 userbot 来执行 `/join` `/leave` `/add_listen` 等指令。
获取方式：运行 `scripts/list_dialogs.py` 后会打印 `my_user_id=...`，把这个值填入即可（或者用 `@userinfobot` 获取）。

### `RELAY_API_ID` / `RELAY_API_HASH` 是什么？可以和 `API_ID` / `API_HASH` 一样吗？
它们也是 Telegram 的 MTProto App 凭证（来自 https://my.telegram.org）。
**可以直接复用同一组** `API_ID` / `API_HASH`（本项目默认也是这么用的）。

### `RELAY_DEST_CHANNELS` 如何写多个？
用**逗号分隔**的目标频道/群 `peer_id`（通常是 `-100...`），例如：

```bash
RELAY_DEST_CHANNELS=-1001111111111,-1002222222222,-1003333333333
```

> 注意：本项目当前实现里 `RELAY_DEST_CHANNELS` 只能是**数字 ID**（不能用 `@channelname`）。

### 能否做到“不同源频道 → 不同目标频道 / Topics”？
支持。你可以在 `config.json` 的 `relay.routes` 里按源频道（peer_id）配置不同的目标：
- 不同目标频道/群
- 或者路由到某个 Supergroup 的 **Topic**（通过 `topic` 标题；不存在会尝试自动创建）

`RELAY_DEST_CHANNELS` 仍然保留为“全量广播/兜底”的旧用法。

---

## 🛠️ 列出对话 ID（peer_id）

```bash
# 打印列表（默认仅频道/群）
docker compose run --rm userbot python scripts/list_dialogs.py

# 导出为 JSON（供 config_wizard 选择）
docker compose run --rm userbot python scripts/list_dialogs.py --json dialogs.json

# 临时传入 api_id/api_hash（无需 .env/config.json）
docker compose run --rm -e API_ID=123 -e API_HASH=xxxx userbot \
  python scripts/list_dialogs.py --json dialogs.json
```

## 🧵 列出某个群的 Topics（论坛话题）

> 注意：Topics 只存在于 **开启了 Topics 的 Supergroup（论坛群）**。
>
> `scripts/list_dialogs.py` 不会显示 topics，因为 topics 不是“对话（dialog）”，需要单独查询。

```bash
docker compose run --rm userbot python scripts/list_topics.py --peer -1001234567890

# 导出为 JSON
docker compose run --rm userbot python scripts/list_topics.py --peer -1001234567890 --json topics.json
```

---

## 🔐 注意事项

- `config.json` / `.env` / `*.session` 都包含敏感信息：已默认加入 `.gitignore`，请勿提交。
- Userbot 账号需要能访问所有 `source_chat`。
- RelayBot 对目标频道通常需要管理员权限。
