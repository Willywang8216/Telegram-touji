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
- 结构化 JSON 日志
- 限流 + 重试 + 死信（DLQ）
- 运行时配置热重载（检测 `config.json` 变更）

---

## 🚀 一键安装（交互填写配置）


```bash
REPO_URL="https://github.com/ike666888/Telegram-touji.git" \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/ike666888/Telegram-touji/main/scripts/install.sh)"
```

执行后脚本会：
1. 检测 Docker / Compose
2. 克隆仓库（如本地不存在）
3. 交互式询问配置参数
4. 生成 `config.json` 与 `.env`
5. 启动容器：`docker compose up -d --build`

---

## ⚙️ 配置说明

安装脚本会提示填写这些核心参数：

- `api_id`
- `api_hash`
- `master_account_id`
- `source_chat`
- `target_bot`
- `relay.bot_token`
- `dest_channels`（逗号分隔）

### ✅ 重要：避免“用户号”直接发到目标群/频道

如果你看到 **目标群/频道里显示你的用户号在发消息**，通常是以下两类问题：

1) **`bot_mappings[].target_bot` 配错了（最常见）**
- `target_bot` 必须是你的 **中转机器人（Bot）的用户名**，例如 `@MyRelayBot`。
- 不能填目标群/频道的 `@channel`、也不能填 `-100...` 这样的群/频道 ID。
- 否则 `telegram_bot.py` 会用你的 **用户号** 直接 `forward_messages()` 到目标群/频道。

2) **relaybot 的 session 不是 Bot（历史遗留 session）**
- 如果 `bot_session.session` 曾经用“用户号”登录过，Telethon 会认为已授权，从而忽略 `bot_token`，导致最终发送者变成用户号。
- 处理方式：删除 `bot_session.session` 后重启 `relaybot` 容器。

### relay.master_account_id（可选，安全建议开启）

`relay.master_account_id`（或环境变量 `RELAY_MASTER_ACCOUNT_ID`）用于限制：只有指定用户 ID 私聊机器人时，才会触发转发。
- 默认 `0`：不限制（兼容旧行为）
- 建议设置为你运行 userbot 的那个用户号 ID，防止别人私聊你的 bot 就能向目标频道群发。

配置文件：
- `config.json`：主配置（持久化）
- `.env`：环境覆盖（敏感信息建议优先放这里）

> 注意：如果你修改了 `config.json` 但行为没变，优先检查 `.env` / 容器环境变量是否还在覆盖（尤其是 `RELAY_DEST_CHANNELS`、`RELAY_BOT_TOKEN`、`RELAY_MASTER_ACCOUNT_ID`）。

`docker-compose.yml` 已通过 `env_file: .env` 自动注入运行环境。

---

## 🧩 项目结构

- `telegram_bot.py`：Userbot 主逻辑（监听、命令处理、转发映射）
- `bot_relay.py`：RelayBot 主逻辑（过滤、重发）
- `common_config.py`：统一配置读取/保存、`.env` 支持、热重载检测
- `structured_logger.py`：JSON 日志输出
- `delivery.py`：限流、重试、DLQ
- `command_utils.py`：命令解析工具
- `scripts/install.sh`：交互式一键安装脚本
- `tests/`：最小单元测试

---

## 🧪 本地验证

```bash
python -m unittest discover -s tests -v
python -m py_compile telegram_bot.py bot_relay.py common_config.py structured_logger.py delivery.py command_utils.py
bash -n scripts/install.sh
```

---
