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
- `dest_channels`（逗号分隔，旧版兼容：无路由时会广播到这些频道/群）

### 🧵 Topics / 路由（可选）

如果目标是带 Topics 的 supergroup（forum），可以用下面的字段把不同来源路由到不同 topic：

- `relay.default_destinations`: 默认投递目标（未命中 routes 时使用）
- `relay.routes`: 按 `source_chat` 精确匹配来源的路由规则
- `topic_title`: 目标 topic 标题（不存在会自动创建）
- `topic_from_source`: 为 true 时，自动使用“来源频道/群标题”作为 topic 名（不存在会自动创建）
- `bucket_topics`: 自动分流到桶（按来源 peer_id 取模），例如 `{"prefix": "Asian Porn ", "count": 5}` 会投递到 `Asian Porn 1..5`
- `strip_text`: 是否清空原始文字/广告（默认 `true`）
- `blocklist_substrings`: 命中任意子串则丢弃消息（用于过滤诈骗/广告）
- `ensure_forum_topics`: 启动时预创建一些 topics

配置文件：
- `config.json`：主配置（持久化）
- `.env`：环境覆盖（敏感信息建议优先放这里）

如果你没有运行 `scripts/install.sh`，可以先：

```bash
cp .env.example .env
```

然后编辑 `.env` 填入敏感信息。

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
