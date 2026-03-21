# Telegram Stealth Relay Bot (Dockerized)

一个基于 **Telethon + Docker Compose** 的 Telegram 消息中继系统：
- `telegram_bot.py`（Userbot）负责监听源频道/群
- `bot_relay.py`（RelayBot）负责无痕重发到目标频道/群（可投递到 forum topics）

---

## ✨ 功能概览

- 支持多源到多目标的消息中继
- 支持相册（media group）聚合转发
- 支持命令过滤（`/`）与系统回执过滤（`🤖`）
- 统一配置模块（JSON + `.env` 覆盖）
- 结构化 JSON 日志
- 限流 + 重试 + 死信（DLQ）
- 运行时配置热重载（检测 `config.json` 变更）
- **自动展开 Twitter/X 链接**：当消息文本里包含 Tweet 链接时，relaybot 会用 `yt-dlp` 下载图片/视频并以媒体形式发到目标频道/话题（下载失败则回退为普通文本转发）

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

安全相关（新增）：
- `relay.allowed_sender_ids`：RelayBot 私聊白名单（推荐填你的 `master_account_id`）
  - 环境变数覆盖：`RELAY_ALLOWED_SENDER_IDS=123,456`

AdminBot（新增）：
- `ADMIN_BOT_TOKEN`：AdminBot token（用于管理 UI）
- `ADMIN_BOT_ADMIN_USER_IDS`：AdminBot 白名单 user_id（默认 `master_account_id`）

启动 AdminBot（docker compose profile）：

```bash
docker compose --profile admin up -d --build
```

### 🧵 Topics / 路由（可选）

如果目标是带 Topics 的 supergroup（forum），可以用下面的字段把不同来源路由到不同 topic：

- `relay.default_destinations`: 默认投递目标（未命中 routes 时使用）
- `relay.routes`: 按 `source_chat` 精确匹配来源的路由规则
- `topic_title`: 目标 topic 标题（不存在会自动创建）
- `topic_from_source`: 为 true 时，自动使用“来源频道/群标题”作为 topic 名（不存在会自动创建）
- `bucket_topics`: 自动分流到桶，例如 `{"prefix": "Asian Porn ", "count": 5, "by": "message"}` 会投递到 `Asian Porn 1..5`
  - `by: "message"`：按每条消息分流（更平均）
  - `by: "source"`：按来源 peer_id 分流（同一来源更稳定）
- `strip_text`: 是否清空原始文字/广告（默认 `true`）
- `blocklist_substrings`: 命中任意子串则丢弃消息（用于过滤诈骗/广告）
- `blocklist_regexes`: 正则命中则丢弃消息（大小写不敏感）
- `block_contact_ads`: 是否启用“私信/下单/@xxx/t.me”等联系方式广告过滤（默认 true）
- `contact_ad_keywords`: 自定义联系方式广告关键词（可选）
- `fallback_to_general_topic`: topic 无法解析时是否降级发送到“General”里（默认 true；推荐设为 false 避免污染）
- `ensure_forum_topics`: 用于 scripts/sync_forum_topics.py 预创建 topics 并写入映射
- `forum_topic_top_messages`: 手动提供 topic 标题 -> top_message 映射（用于把消息投递到指定 topic）
  - 由于 Telegram 对 bot 的 MTProto 接口有限制，bot 账号在部分环境下无法调用 `GetForumTopicsRequest` 获取 topic 列表；此时需要用 userbot 生成映射并写入配置。

### 🌐 Topic 标题本地化 / 重命名（可选）

如果你希望不同目标群使用不同语言的 topic 标题（例如同一分类在英文群是 `Asian bear`，中文群是 `亞洲熊`），可以在配置里添加：

- `relay.topic_renames`: `{chat_id: {old_title: new_title}}`

然后用 userbot 一键应用并同步映射：

```bash
docker compose run --rm userbot python scripts/sync_forum_topics.py --rename --icons --write
```

默认可用的 topic icon（custom emoji）列表可以用 userbot 查看：

```bash
docker compose run --rm userbot python scripts/list_topic_icons.py
```

列出某个 supergroup 的 topic 列表（仅查询，不会创建/修改）：

```bash
docker compose run --rm userbot python scripts/list_topics.py --peer <supergroup_id_or_username>
# 输出 JSON: {"topic_title": top_message, ...}
docker compose run --rm userbot python scripts/list_topics.py --peer <peer> --json
```

列出 **当前 userbot 账号可见的所有** 开启 Topics（forum）的 supergroup，并列出它们的 topics（仅查询，不会创建/修改）：

```bash
docker compose run --rm userbot python scripts/list_topics.py --all-forums
# 输出 JSON: [{peer_id,title,username,topics:{title: top_message}}, ...]
docker compose run --rm userbot python scripts/list_topics.py --all-forums --json
```

（新增）列出当前 userbot 账号可见的所有对话（含 group/supergroup/channel/user），方便找 peer_id：

```bash
# 文本：peer_id  kind  forum=0/1  title  @username
docker compose run --rm userbot python scripts/list_dialogs.py

# 仅列 supergroup+channel 且模糊筛选
docker compose run --rm userbot python scripts/list_dialogs.py --kinds supergroup,channel --search "keyword"

# JSON 输出
docker compose run --rm userbot python scripts/list_dialogs.py --json
```

（新增）命令行加入/退出群组/频道（不依赖 Telegram 私聊指令）：

```bash
docker compose run --rm userbot python scripts/join_leave.py --join  --peer <@username_or_-100..._or_invite_link>
docker compose run --rm userbot python scripts/join_leave.py --leave --peer <@username_or_-100...>
```

（新增）命令行编辑 config.json（添加监听 / 添加路由）：

```bash
# 添加/更新 bot_mappings
docker compose run --rm userbot python scripts/config_cli.py add-listen --source <src_peer> --target-bot <@middle_bot> --write

# 添加/合并 relay.routes（--dest 用 JSON，支持 topic_title/topic_from_source/bucket_topics 等）
docker compose run --rm userbot python scripts/config_cli.py add-route \
  --source-chat <src_peer_id> \
  --dest '{"chat_id":-100DEST1,"topic_title":"General"}' \
  --dest '{"chat_id":-100DEST2,"topic_title":"Other"}' \
  --write
```

（新增）检测/清理无效（删除/私有/不可访问）的 chats/topics，并可选择自动退出（不会创建 topic）：

```bash
# 仅报告（dry-run）
docker compose run --rm userbot python scripts/prune_invalid_config.py \
  --prune-bot-mappings \
  --prune-destinations \
  --prune-topic-mapping --prune-closed-topics

# 应用修改并写回 config.json，并尝试自动退出（leave）被移除的 chats
# 注意：要在同一次执行里加上 --leave，否则先 --write 移除了配置，第二次再跑就没有可 leave 的项目了。
docker compose run --rm userbot python scripts/prune_invalid_config.py \
  --prune-bot-mappings \
  --prune-destinations \
  --prune-topic-mapping --prune-closed-topics \
  --write --leave
```

（已有脚本）仅针对 topic 的缺失/关闭状态做检测与 prune（不会创建 topic）：

```bash
docker compose run --rm userbot python scripts/prune_forum_topics.py --prune-missing --prune-closed --json
```

### 🗓️ Topic 日更保底（可选）

`scripts/autofill_topics.py` 会把每个 topic 的“每日帖子数”补到指定数量：
- 如果当天新内容不足，会从该 topic 的历史消息中随机挑选并重发
- 使用 SQLite 记录最近使用过的消息，尽量均匀且减少重复

如果某个 topic 还是空的（没有历史媒体），可以先用 `scripts/backfill_routes.py` 把来源频道的历史媒体“灌入”到对应 topic（基于 `relay.routes`）：

```bash
docker compose run --rm userbot python scripts/backfill_routes.py \
  --source-chat-id <source_chat_id> \
  --dest-chat-id <dest_supergroup_id> \
  --only-topic-title "<topic_title>" \
  --max-posts 20
```

如果你希望“某个来源频道/群在最近 N 分钟没有新内容时，就自动从该来源的历史里抽取旧媒体补发到对应 topic”，用：

```bash
docker compose run --rm userbot python scripts/autofill_inactive_sources.py \
  --inactive-min 60 \
  --fill-count 10 \
  --lookback-days 30 \
  --daemon --interval-min 60
```

它会读取：
- `bot_mappings`（确定要监控/补发的来源）
- `relay.routes` + `relay.default_destinations`（确定每个来源应投递到哪个目标 topic / bucket topic）

一次性执行：

```bash
docker compose run --rm userbot python scripts/autofill_topics.py --min-per-topic 10
```

后台常驻（每小时检查一次）：

```bash
docker compose run --rm userbot python scripts/autofill_topics.py --daemon --interval-min 60 --min-per-topic 10
```

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

### Twitter/X 链接自动下载（tweet 媒体展开）

当 relaybot 收到的消息文本里包含 Tweet 链接（`twitter.com/.../status/<id>` 或 `x.com/.../status/<id>`）时，会尝试用 `yt-dlp` 下载该 Tweet 的图片/视频，并以媒体形式发到目标频道/话题。

配置项在 `relay` 里：

- `expand_twitter_links`（默认 `true`）：是否启用
- `twitter_cookies_file`（默认 `null`）：可选。指向一个 cookies 文件路径（Netscape cookiefile 格式），用于在 X 限制访问/需要登录时下载媒体
- `twitter_max_media_files`（默认 `8`）：最多发送多少个媒体文件（避免一次 tweet 太多图导致发不出去）

环境变量：
- `RELAY_TWITTER_COOKIES_FILE`：覆盖 `twitter_cookies_file`（Docker/Compose 下建议放到仓库目录，比如 `./twitter.cookies.txt`，然后设置为 `/app/twitter.cookies.txt`）

> 注：某些 X 视频是 HLS 分片格式，需要 `ffmpeg` 才能合并。Docker 镜像已内置 `ffmpeg`。

配置文件：
- `config.json`：主配置（持久化）
- `.env`：环境覆盖（敏感信息建议优先放这里）

> 注意：如果你修改了 `config.json` 但行为没变，优先检查 `.env` / 容器环境变量是否还在覆盖（尤其是 `RELAY_DEST_CHANNELS`、`RELAY_BOT_TOKEN`、`RELAY_MASTER_ACCOUNT_ID`）。

`docker-compose.yml` 已通过 `env_file: .env` 自动注入运行环境。

---

## 🧩 项目结构

- `telegram_bot.py`：Userbot 主逻辑（监听、命令处理、转发映射）
- `bot_relay.py`：RelayBot 主逻辑（过滤、重发）
- `admin_bot.py`：AdminBot 管理 UI（需要单独的 bot token）
- `common_config.py`：统一配置读取/保存、`.env` 支持、热重载检测
- `structured_logger.py`：JSON 日志输出
- `delivery.py`：限流、重试、DLQ
- `command_utils.py`：命令解析工具
- `twitter_expand.py`：Tweet URL 提取 + yt-dlp 下载封装
- `scripts/install.sh`：交互式一键安装脚本
- `tests/`：最小单元测试

---

## 🧪 本地验证（Docker 环境）

```bash
python -m unittest discover -s tests -v
python -m py_compile telegram_bot.py bot_relay.py common_config.py structured_logger.py delivery.py command_utils.py twitter_expand.py
bash -n scripts/install.sh
```
