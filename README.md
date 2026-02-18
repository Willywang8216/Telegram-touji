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

---

## 🔐 注意事项

- `config.json` / `.env` / `*.session` 都包含敏感信息：已默认加入 `.gitignore`，请勿提交。
- Userbot 账号需要能访问所有 `source_chat`。
- RelayBot 对目标频道通常需要管理员权限。
