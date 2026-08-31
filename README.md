# astrbot_plugin_chat_insight · 聊天洞察

> Chat Insight = ChatLogger 的只读统计前端。
> 把已经积累的聊天数据，变成用户看得懂的统计、排行、关键词和画像。
> 合并并取代 [astrbot_plugin_chat_statistics](https://github.com/OborozukiNoYume) 与 astrbot_plugin_user_profile。

## 定位与架构

```
AstrBot → ChatLogger → chatlog.db → Chat Insight（本插件，只读）
```

- ChatLogger 是唯一聊天数据来源；本插件**不监听消息、不建第二套聊天库**。
- 对 `chatlog.db` **严格只读**（`mode=ro` 连接，契约见 ChatLogger 的 `QUERY_GUIDE.md`，要求 schema `user_version >= 3`），不修改其任何数据与结构。
- 分层：`commands(main.py) → service → repository → SQLite`，SQL 全部集中在 repository，命令层不写 SQL。
- 查询在独立线程执行（`asyncio.to_thread`），不阻塞事件循环；ChatLogger 未安装/数据库缺失时插件照常加载，命令返回清晰提示。
- `insight/` 包为纯 Python（不依赖 astrbot），可独立测试；所有 `json_each` 查询带 `json_valid` 防护（content_json 可能被截断为非法 JSON）。

## 命令

时间参数统一语法：`今日`、`昨日`、`本周`、`上周`、`本月`、`上月`、`本季度`、`上季度`、`半年`（最近 6 个自然月）、`今年`、`历史`/`总榜`、`N天`（如 `7天`）。群统计默认 `7天`，用户画像默认 `历史`（全期）。

**公开命令（所有群友可用）**

| 命令 | 别名 | 参数 | 说明 |
|---|---|---|---|
| `/发言榜` | `rank` / `发言排行` | `[群 <群号>] [时间] [N]` | 发言榜：数量 / 排名 / 占比 |
| `/词云` | `wordcloud` | `[时间] [N]`（默认自己） | 个人词云；查他人按自然顺序任选：`@某人 词云 [时间]`、`@某人 /词云 [时间]`（At 在前需靠口语触发兜底——框架对首段 @普通人的消息不唤醒）、`/词云 @某人 [时间]`；`/词云 全群 [时间]` 查整群（兼容 `user me` / `group` 英文写法） |
| `/用户画像` | `profile` | `[@某人] [用户 <QQ号\|我>] [时间]` | 完整用户画像，六视图合并输出：综合 / 活跃 / 关键词 / 风格 / 互动 / 机器人（查他人需管理员） |
| `/群画像` | `group_profile` | — | 当前群画像（发言成员数 ≠ 群成员总数） |

**管理命令组**：`/画像维护`（别名 `insight-admin`，仅管理员）——`状态`（数据源契约检查）、`刷新`（清缓存并重识别 Bot ID），裸调用自动列出子命令。

> v0.5.0 按「如无必要勿增实体」收敛：原 `/聊天统计` 组（总览/趋势/时段/关键词/关键词趋势）与 `/昵称` 已移除——时间窗分析与关键词分别由 `/群画像`（含 24h 分布与日趋势卡片）和 `/词云` 覆盖，全期零群友调用。

个人词云口语触发（默认开启，`wordcloud_trigger_enabled` 可关）：一律需要 @机器人或唤醒前缀——`@机器人 我的词云`、`@机器人 我的历史词云`、`@机器人 @某人 历史词云`；裸「词云」与普通聊天一律不响应。

## 统计口径

- **用户消息**：恒过滤 `sender_type='user'`，机器人消息不计入任何统计。
- **唤醒消息（waked_bot）分场景**：群统计（排行/关键词/群画像，例外：群画像的「高频互动对」含对 Bot 的回复，不排除）默认排除 `waked_bot=1`（斜杠命令、@机器人、引用机器人、私聊），防止命令文本污染统计，受 `exclude_waked_messages` 配置控制；**用户画像的行为统计（活跃/风格/互动/Bot）不排除**——唤醒 Bot 本身是用户行为；用户画像的关键词恒排除。
- **关键词 / 词云**：取 `content_json` 的 `plain` 段（结构化文本），jieba 分词后按 token 出现次数计数；过滤 URL、纯数字、标点、单字、停用词。
- **消息长度**：`LENGTH(content)` 字符长度，≠ 汉字字数；纯图片/语音等空文本消息不参与。
- **时间**：`ts` 为 UTC epoch 秒，今日/本周/小时分布/活跃天数均按配置时区（默认 `Asia/Shanghai`）在应用层换算；周为周一起始。
- **措辞纪律**：只呈现可验证的频次/分布事实——「高频互动对象」而非「好友」，「主要讨论关键词」而非「兴趣」，不推导心理标签。
- **隐私**：用户画像默认 `current_group` 范围（在群 A 查询不暴露群 B 数据）；查他人需管理员；Bot 画像的私聊统计为全局口径（输出注明）。

## 明确不属于本插件（负面清单）

```
❌ 聊天记录搜索      ❌ Memory / 自动记忆     ❌ RAG / Embedding
❌ 用户长期偏好      ❌ 人格判断 / 情绪分析    ❌ LLM 自动画像
❌ LLM 前置 Context  ❌ 复杂社交网络分析       ❌ 群成员真实人数
```

历史信息 → 记忆 → 召回 → LLM Context 这条链路属于记忆类插件（如 LivingMemory）；
本插件只负责：历史聊天 → 统计 → 用户主动查询。

## 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `database_path` | 空（自动定位） | chatlog.db 路径，只读 |
| `timezone` | `Asia/Shanghai` | 统计时区（IANA 名） |
| `default_top_n` | `10` | 榜单/关键词默认条数 |
| `max_query_days` | `90` | 单次查询最大天数 |
| `max_messages_scan` | `50000` | 词云/关键词取文本上限，超出保留最新部分 |
| `wordcloud_enabled` | `true` | 关闭后 `/词云` 输出文字版词频 |
| `font_path` | 空（内置字体） | 词云字体；内置 `assets/fonts/NotoSansSC.ttf` |
| `stopwords_path` / `extra_stopwords` | 空 / `[]` | 自定义停用词 |
| `wordcloud_trigger_enabled` | `true` | 个人词云口语触发开关 |
| `wordcloud_retention_days` | `7` | 词云 PNG 保留天数，生成时自动清理过期图，`0` 关闭 |
| `exclude_waked_messages` | `true` | 群统计排除唤醒消息 |
| `profile_scope` | `current_group` | 用户画像范围（`all` 为全部会话） |
| `render_mode` | `text` | 画像与群报输出形式：`text` 文本 / `image` 图片卡片（覆盖 `/用户画像`、`/群画像`、定时群报），渲染失败自动回退文本 |
| `cache_ttl_minutes` | `30` | 画像内存缓存分钟数，`0` 关闭 |
| `report_enabled` | `false` | 定时群报开关（详见下方） |
| `report_frequency` | `weekly` | 群报频率：每日（报昨日）/ 每周（报上周）/ 每月（报上月） |
| `report_day` | `1` | 每周模式的星期（1=周一…7=周日） |
| `report_day_of_month` | `1` | 每月模式的日期（1-31，超当月天数取月末） |
| `report_hour` / `report_minute` | `8` / `0` | 群报触发时刻（时/分两个滑块，杜绝非法输入） |
| `report_groups` | `[]` | 目标群号列表（纯数字），留空不推送 |
| `report_sections` | 全选 | 播报内容复选：总览 / 发言榜 / 词云（词云即关键词的可视化） |
| `report_min_messages` | `10` | 区间消息数低于该值的群自动跳过，`0` 不限制 |

## 定时群报

开启 `report_enabled` 并配置目标群后，插件按频率（每日/每周/每月）和设定时刻自动推送群报，**统计区间与频率联动**：每日报昨日、每周报上周（周一起始自然周）、每月报上月（自然月）。分节内容在 WebUI 勾选，词云以图片发送（无可用文本自动降级文字版）。配置改动保存后最迟约 5 分钟生效（无需重启）；数据源未就绪或配置非法时循环自动等待重试，不影响主进程。

## 图片卡片（render_mode = image）

`/用户画像`、`/群画像` 与定时群报可切换为 HTML 卡片图输出（模板在 `insight/templates/`，数据组装与文本版同源同口径）。

渲染链（逐级回退）：**本地 chromium**（playwright，模板与数据不出本机，单张约 1 秒）→ **AstrBot 官方 t2i 服务**（本地不可用时，**模板与统计数据——昵称、QQ 号、群号等——需上传该服务**）→ 文本输出。各级 45 秒超时，服务过载/返回异常内容同样自动回退；词云不并入卡片，仍独立成图。

启用本地渲染（可选，强烈推荐）：

```bash
.venv/bin/pip install playwright
.venv/bin/playwright install chromium
```

未安装时自动使用云端渲染，功能不受影响。

## 已知限制

- 时区带夏令时（DST）的地区，天/小时桶按区间起点的固定偏移换算，跨 DST 切换日有近似（中国时区无影响）。
- 「最常被谁回复」（`reply_user_id` 无索引）与「@ 网络」（`json_each` 展开）为已知全扫描，按画像时间范围（默认历史=全期）执行；「最常被@」已限 90 天窗口；变慢后由 chatlogger 上游按缓建预案加索引。
- 用户风格的连发统计需按 `(user_id, ts)` 索引拉取该用户全部 ts（纯整数序列）；活跃大户首次查询为秒级，靠 TTL 缓存缓解。
- 词云 PNG 按区间命名写入 `plugin_data`，生成时自动清理超过 `wordcloud_retention_days`（默认 7 天）的旧图。

## 部署

1. 依赖 ChatLogger 插件（数据来源）；首次加载时 AstrBot 自动安装 `requirements.txt`（jieba / wordcloud / emoji）。
2. 将本目录放入（或符号链接到）`data/plugins/astrbot_plugin_chat_insight`。
3. 若此前使用 chat_statistics / user_profile，请停用或移除它们以避免命令冲突（`/词云`、`/用户画像`（`/profile`）等命令名重叠）。

## 开发

```bash
python -m pytest tests/ -q：契约/时区/口径/画像/群报/缓存/坏JSON/边界
```

包结构：`insight/`（纯 Python）：`db`（只读连接）· `timeutil`（唯一时间实现）· `textproc`（清洗/分词）· `repository`（全部 SQL）· `service`（业务聚合）· `render`（渲染与降级）· `colloquial`（口语触发匹配）· `cache`（画像 TTL 缓存）。

## License

AGPL-3.0
