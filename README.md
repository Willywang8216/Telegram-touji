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

配置文件：
- `config.json`：主配置（持久化）
- `.env`：环境覆盖（敏感信息建议优先放这里）

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

## 🛠️ 辅助脚本

### 列出对话与 ID（peer_id）

`source_chat` / `relay.dest_channels` 使用的是 Telethon 的 `peer_id`（频道/超群常见为 `-100...`）。

```bash
# 打印列表（默认仅频道/群）
docker compose run --rm userbot python scripts/list_dialogs.py

# 额外导出为 JSON（供 config_wizard 选择）
docker compose run --rm userbot python scripts/list_dialogs.py --json dialogs.json

# 或本地：
python scripts/list_dialogs.py --json dialogs.json

# 如果你还没生成 config.json，可以用参数/环境变量提供 api_id/api_hash：
# API_ID=123 API_HASH=xxx python scripts/list_dialogs.py
# 或：python scripts/list_dialogs.py --api-id 123 --api-hash xxx
```

### 交互生成 config.json / .env

```bash
docker compose run --rm userbot python scripts/config_wizard.py --dialogs dialogs.json
# 或本地：
python scripts/config_wizard.py --dialogs dialogs.json
```

---

## 🔐 权限/注意事项

- Userbot 账号需要能访问所有 `source_chat`（私有频道/群需先加入）。
- RelayBot 对 `relay.dest_channels` 里的目标频道通常需要管理员权限（至少允许发送消息/媒体）。
- 首次使用请先对 `target_bot` 发送 `/start`，确保能收到私聊消息。

---

## 🧪 本地验证

```bash
python -m unittest discover -s tests -v
python -m py_compile telegram_bot.py bot_relay.py common_config.py structured_logger.py delivery.py command_utils.py
bash -n scripts/install.sh
```

---
