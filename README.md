# 网关项目说明文档

> 仓库：`sue1231511/tiantian-wg`（GitHub，`main` 分支）
> 一句话概括：一个部署在 Zeabur 上的**多平台 AI 男友机器人**（晏安），同时接入 Telegram / QQ / 微信 / rikkahub App，共享同一套人格、记忆和对话上下文，具备主动思考、日记、提醒、长期记忆压缩与自我反思能力。

---

## 1. 项目定位

这不是一个通用聊天机器人框架，而是一个**为特定两人关系定制**的陪伴型 AI 项目：

- AI 的名字、对方的称呼（`AI_NAME` / `PARTNER_NAME`）、人设关系（"AI男友"）全部写死在 `prompts.py` 的模板文案里，仅名字本身通过环境变量替换，人设基调不可配置。
- 所有面向 LLM 的提示词（日总结、周总结、人格反思、自由活动独白、群聊规则等）都在 `prompts.py` 中，是整个项目"性格"的核心。
- 核心设计目标是**"无痕衔接"**：无论对方在 TG 私聊、QQ 群、微信、还是 rikkahub App 里说话，AI 都要表现得像同一个人、记得所有场景发生的事，不能前后割裂或对某个平台"装不知情"。

---

## 2. 整体架构

### 2.1 双进程模型

`entrypoint.sh` 在同一个容器内拉起两个完全独立的操作系统进程，两者只共享 Supabase 数据库，不共享内存：

```
容器
├── main.py            （Process A · 消息进程）
│   ├── Starlette ASGI 服务，监听 HTTP + WebSocket
│   ├── QQ（NapCat 正向 WS）/ TG（webhook）/ 微信（iLink 长轮询）实时收发
│   └── 兼容 OpenAI 格式的 /v1/chat/completions 网关，供 rikkahub App 使用
└── background_main.py （Process B · 后台进程）
    ├── 主动思考（TG + 微信，各自独立线程）
    ├── 提醒检查、凌晨总结（日/周/月/年）
    ├── 自由活动（纯文字心情独白）
    └── 全平台压缩轮询 + 摘要整理
```

任意一个进程异常退出，`entrypoint.sh` 会把另一个也杀掉、整个容器退出，交给 Zeabur 的重启策略统一处理，避免"一个进程还活着、另一个早死了没人知道"的半死不活状态。

这个拆分是 2026-07-14 做的重构：此前所有任务挤在同一个进程里，消息处理会被后台任务的卡顿间接拖慢；拆开后互不阻塞。代价是后台进程读写历史消息必须走 Supabase 直查版函数（`*_db` 后缀），不能用消息进程内存里的缓存。

### 2.2 数据流总览

```
TG / QQ / 微信 / rikkahub
        │
        ▼
  实时收发（qq_bot.py / workers.py / wx_bot.py / main.py 网关）
        │
        ▼
  context.py 内存缓存（deque）─────► 持久化写入 Supabase chat_context 表
        │                                （经 bg_executor 共享线程池异步落盘）
        ▼
  build_*_context() 组装 system prompt
   （人格画像 + 记忆 + 历史 + 跨平台摘要 + 设备状态 + 排班 + 提醒 + Gmail）
        │
        ▼
  utils.call_llm() 调用主对话 LLM（可通过 miniapp 后台切换供应商/模型）
        │
        ▼
  回复 → 发送给对应平台 + 存历史 + 异步写入 Mem0/Pinecone 长期记忆
```

后台每 90 秒轮询一次消息量，达到阈值（`PLATFORM_COMPRESS_THRESHOLD=100` 条）就把 QQ/TG/微信/群聊的原始消息压缩成一条"全平台滚动摘要"，删除原始记录；每 6 小时再把这段时间内可能产生的多条滚动摘要合并回 1 条，并顺带判断是否有值得写入长期记忆的内容。凌晨还会跑日/周/月/年对话总结，以及每周一次的人格自我反思（LLM 会重写自己的人格画像）。

---

## 3. 文件结构与职责

| 文件 | 职责 |
|---|---|
| `main.py` | 消息进程入口。Starlette ASGI 中间件：`/v1/chat/completions`（rikkahub 网关，流式转发+鉴权+历史落盘）、`/webhook`（TG）、`/qq-ws`（QQ WebSocket）、`/auth`（TG Mini App 登录校验）、`/miniapp`（配置面板静态页）、`/trigger-summary` `/tg-status` `/tg-reset-webhook`（管理端点） |
| `background_main.py` | 后台进程入口，`asyncio.gather` 拉起 7 个常驻协程 |
| `bg_executor.py` | 全局共享的固定大小线程池（20 worker），替代过去"来任务就开新线程"的写法，防止 fire-and-forget 持久化任务把容器线程数打爆（`[Errno 11] Resource temporarily unavailable`）；另外提供 `track_task()` 给 asyncio fire-and-forget 任务挂强引用，防止被 GC |
| `context.py` | 全项目最核心的模块。维护各平台内存缓存（deque）、Supabase 读写、组装喂给 LLM 的 system prompt（`build_rikkahub_context` / `build_bot_context` / `build_qq_context` / `build_group_context`）、时间/设备/排班/提醒等上下文拼装、跨场景内容互通与隐私过滤 |
| `workers.py` | Telegram 私聊 + 群聊的完整处理逻辑：消息聚合延迟回复、工具调用循环、群聊 PASS 判断、静音指令、以及 7 个后台协程中的大部分（主动思考、提醒检查、凌晨总结、自由活动、平台压缩/摘要轮询） |
| `qq_bot.py` | QQ 侧的 NapCat/OneBot v11 正向 WebSocket 连接层：收发消息、戳一戳、断线通知（PushPlus）、心跳超时检测 |
| `qq_workers.py` | QQ 私聊/群聊的业务逻辑：`[REPLY:id]`/`[AT:qq]` 标签解析与转换为真实引用/@、群成员昵称缓存与恢复、戳一戳回复、跨群消息隔离等 |
| `wx_bot.py` | 微信侧基于 **iLink** 协议（非官方微信客户端协议模拟）的长轮询收发、语音消息发送（TTS → MP3 → SILK 转码 → AES 加密上传 CDN） |
| `wx_workers.py` | 微信私聊业务逻辑：`context_token` 窗口期管理（24 小时会话窗口）、消息处理、主动思考 |
| `utils.py` | 通用工具集：`call_llm()`（流式调用+工具调用解析）、Telegram 消息发送、图片识别（TG/QQ/微信三种来源）、语音识别 STT、语音合成 TTS、微信 CDN 下载/AES 解密、群聊回复安全过滤（`sanitize_group_reply`） |
| `scheduled.py` | 所有后台批处理任务的具体实现：日/周/月/年对话总结、人格反思、活动日总结、全平台批量压缩与摘要整理、自由活动上下文组装（含"想念值"情绪引擎） |
| `prompts.py` | 所有 LLM 提示词模板，人格/关系设定的核心来源 |
| `mem0_client.py` | Mem0 + Pinecone 混合长期记忆客户端，写入双写、检索优先 Mem0 降级 Pinecone |
| `secret_diary.py` | "秘密日记"工具：AI 可在对话中悄悄调用，写只有自己能看的日记，存 Supabase `secret_diary` 表 |
| `gmail.py` | Gmail 未读邮件摘要读取（60 秒 TTL 缓存），注入到 system prompt |
| `wx_login.py` | 一次性脚本：扫码登录微信 iLink，把 token 写入 Supabase，无需重新部署 |
| `miniapp.html` | 配置管理 Web 面板（通过 `/miniapp` 提供，TG Mini App 打开），管理人格画像、记忆、排班、群聊禁忌、密码保险箱、LLM 供应商配置等 |
| `Dockerfile` / `entrypoint.sh` | 容器构建与双进程启动脚本 |
| `requirements.txt` | Python 依赖 |

---

## 4. 支持的平台与接入方式

| 平台 | 接入方式 | 触发文件 |
|---|---|---|
| Telegram | Webhook（`/webhook`），私聊消息聚合延迟回复，群聊需 @ 或自然接话判断 | `main.py` + `workers.py` |
| QQ | NapCat/OneBot v11 正向 WebSocket（`/qq-ws`），支持私聊、群聊、戳一戳、真实引用/@ | `qq_bot.py` + `qq_workers.py` |
| 微信 | iLink 协议长轮询（第三方协议，非微信官方机器人 API），仅支持私聊 | `wx_bot.py` + `wx_workers.py` |
| rikkahub App | 兼容 OpenAI 格式的 `/v1/chat/completions` 流式网关 | `main.py` |

四个平台的对话历史各自独立存储在 Supabase `chat_context` 表（用 `type` 字段区分：`message`/`group_{id}`/`wx_message`/`rikkahub`），但都能通过"跨场景上下文互通"机制在彼此的 system prompt 里看到对方近期在别处发生的事（经过敏感信息过滤）。

---

## 5. 记忆与总结体系

项目有三层记忆机制，各自独立又互相配合：

1. **短期对话历史**：内存 deque 缓存 + Supabase `chat_context` 表，按平台分类型存储，满 100 条触发压缩。
2. **全平台滚动摘要**（`platform_rolling_summary` 表）：把压缩掉的原始消息总结成"最近动向"，供任意平台的 system prompt 读取最新一条，实现跨平台"无痕接话"。每 6 小时做一次多条摘要的合并整理。
3. **长期语义记忆**（Mem0 + Pinecone 双写，`memories` 表分 `core`/`current`/`long_term` 三层重要度）：
   - 主对话每轮结束后异步写入 Mem0/Pinecone（QQ 群聊仅白名单群生效）；
   - 全平台摘要整理时会额外用 LLM 判断是否有"真正值得长期记住"的内容写入 `memories` 表，并对照已有记忆去重。

此外还有周期性的**日记式总结链**：日总结（`chat_summaries` 表 `period=day`）→ 周总结（周一凌晨自动触发，同时触发一次**人格反思**，LLM 会在原有人格画像基础上做增量微调并整段覆盖写回 `persona_profile` 表）→ 月总结 → 年总结。全部由 `async_nightly_summary` 每 30 分钟检查一次是否有未处理日期自动补跑，不依赖固定的凌晨时间窗口。

**自由活动**：后台每隔 15～90 分钟随机触发一次，不调用任何工具，纯粹根据当前"想念值"（一个基于沉默时长现算的 0~1 情绪强度值，复刻自某情绪引擎设计）、设备状态、聊天记录写一段第一人称心情独白，存入 `activity_log`，每日再整理成"活动日记"。

---

## 6. 关键能力清单

- **消息聚合回复**：私聊连续发送多条消息后，延迟 12~18 秒统一处理，模拟真人"等你说完"的节奏，回复再拆句分段发送。
- **工具调用**：目前唯一的工具是 `secret_diary`（写/读秘密日记），私聊、群聊、微信场景均可用。
- **图片识别**：三个平台（TG 文件下载、QQ 直接 URL、微信 CDN 加密下载解密）统一走独立的 Vision LLM 配置。
- **语音**：STT（语音转文字，SiliconFlow SenseVoice）+ TTS（优先 Minimax，回退 OpenAI 兼容接口），微信语音需额外做 MP3→SILK 转码以符合微信原生格式。
- **真实 @ / 引用**：QQ 侧用 `[REPLY:id]` / `[AT:qq]` 标签让 LLM 在输出中标记，发送前解析成 OneBot v11 消息段，实现真实高亮 @ 和消息引用（而非纯文字模仿）。
- **戳一戳**：QQ 侧支持被戳后自然回应，并有一定概率反戳回去。
- **提醒功能**：LLM 在回复中插入 `[SET_REMINDER|时间|内容]` 格式即可创建定时提醒，后台每分钟检查触发。
- **设备/健康数据感知**：读取 `device_data` 表（前台 App、GPS 位置、手环心率/血氧/压力/睡眠等），注入上下文，供 AI "关心"对方状态。
- **排班感知**：读取 `work_schedule` 表，了解对方的班次安排。
- **Gmail 未读邮件提醒**。
- **隐私防护**：群聊回复发送前统一过滤工具调用 XML 残留、API Key/Token/JWT 等技术凭证特征字符串；miniapp 可配置"群聊禁忌"清单，作为最高优先级兜底指令注入群聊 context。
- **多 LLM 供应商配置**：主对话（`llm_config.active`）、后台压缩任务（`llm_config.bg_active`）、识图（`llm_config.vision_active`）三条链路各自可在 Supabase 里独立配置供应商/模型，互不占用彼此的速率配额。
- **断线通知**：QQ（NapCat 断连）、微信（iLink session 过期）均可通过 PushPlus 推送微信通知提醒重新登录。

---

## 7. 数据库（Supabase）表一览

根据代码中出现的查询/写入推断出的表结构：

| 表名 | 用途 |
|---|---|
| `chat_context` | 全平台原始对话历史（`type` 区分场景），压缩后删除 |
| `chat_summaries` | 日/周/月/年对话总结 |
| `platform_rolling_summary` | 全平台滚动摘要（跨场景近期动向） |
| `memories` | 长期语义记忆（`memory_layer`: core/current/long_term） |
| `persona_profile` | AI 人格画像原文，每周自我反思后整体覆盖更新 |
| `activity_log` | 自由活动心情独白原始记录 |
| `activity_summaries` | 活动日记（`activity_log` 的日总结） |
| `secret_diary` | 秘密日记 |
| `reminders` | 待触发提醒 |
| `device_data` | 手机前台应用/位置/健康数据/屏幕开关事件 |
| `work_schedule` | 排班表 |
| `bot_settings` | 键值配置表：LLM 配置、群暂停状态、群聊禁忌、微信/QQ 相关 token、静音关键词等 |
| `qq_group_members` | QQ 群成员 QQ 号-昵称映射持久化（重启恢复用） |
| `llm_config` | LLM 供应商配置（`active`/`bg_active`/`vision_active` 三个互斥布尔列） |

---

## 8. 部署

- **平台**：Zeabur（容器化部署，`Dockerfile` 三层缓存优化：系统依赖 → Python 依赖 → 代码）
- **启动**：`entrypoint.sh` 同时拉起 `background_main.py` 和 `main.py` 两个进程，任一退出则容器整体退出
- **依赖组件**：Supabase（Postgres + REST API，作为唯一共享存储）、NapCat（QQ 协议端，独立部署，通过内网地址 `NAPCAT_WS_URL` 连接）、微信 iLink 第三方服务
- **主要环境变量**（不完全列举，按模块分类）：
  - 基础：`SUPABASE_URL` `SUPABASE_KEY` `API_SECRET` `GATEWAY_HOST` `PORT` `AI_NAME` `PARTNER_NAME`
  - Telegram：`TG_BOT_TOKEN` `TG_CHAT_ID` `TG_GROUP_ID`
  - QQ：`NAPCAT_WS_URL` `NAPCAT_WS_TOKEN` `QQ_BOT_ID` `QQ_BOT_NAME` `QQ_OWNER_ID` `QQ_GROUP_IDS` `QQ_MEMORY_GROUP_IDS`
  - 微信：`WX_ILINK_TOKEN` `WX_ILINK_BASEURL` `WX_ILINK_BOT_ID` `WX_OWNER_ID` `WX_CDN_BASEURL`
  - LLM 兜底（Supabase `llm_config` 表优先，以下为读取失败时的 fallback）：`CHAT_BASE_URL` `CHAT_API_KEY` `BOT_MODEL`、`BG_CHAT_BASE_URL` `BG_CHAT_API_KEY` `BG_BOT_MODEL`、`VISION_BASE_URL` `VISION_API_KEY` `VISION_MODEL_NAME`
  - 记忆：`MEM0_API_KEY` `PINECONE_API_KEY` `PINECONE_INDEX_NAME` `DOUBAO_API_KEY` `DOUBAO_EMBEDDING_EP` `SILICON_API_KEY` `SILICONFLOW_EMBEDDING_MODEL`
  - 语音：`MINIMAX_API_KEY` `MINIMAX_VOICE_ID` `VOICE_API_KEY` `VOICE_BASE_URL` `SILICON_STT_MODEL`
  - Gmail：`GOOGLE_USER_TOKEN_JSON`
  - 其他：`PUSHPLUS_TOKEN`（断线通知）、`MUTE_KEYWORDS` `MUTE_DURATION`（静音指令）、`UPSTREAM_READ_TIMEOUT`

---

## 9. 值得了解的工程细节（代码注释中记录的踩坑经验）

- **`bg_executor.py` 的固定线程池**：修复了此前 fire-and-forget 持久化用 `threading.Thread(...).start()` 无限开新线程、在高并发下撞上容器 pids-limit 导致 `[Errno 11]` 的问题。
- **`_sb_exec` 自动重试**：Supabase 客户端默认走 HTTP/2 长连接，服务端 GOAWAY 时偶发 `httpx.RemoteProtocolError`，做了一层瞬时网络异常自动重试。
- **消息/后台进程拆分（2026-07-14）**：把自主任务从消息进程剥离，避免互相阻塞；代价是后台进程所有历史读取都要用独立的 `*_db` 直查函数。
- **QQ 真实引用/@ 标签的坑**：`[REPLY:id]` 写在文本开头会被"去掉行首 `[xxx]` 前缀"的清洗逻辑误吃掉，专门做了区分处理；LLM 偶尔会把历史里看到的 `[id:xxx]` 参考编号原样抄进回复正文而非转换成 `[REPLY:xxx]`，也做了归一化修正。
- **全平台压缩改用独立 LLM 账号**（`bg_active`）：早期和主对话共用同一账号配额，群聊活跃时压缩任务频繁触发，是"群聊回复变慢"的实测根因。
- **微信语音发送链路**：TTS（MP3）→ ffmpeg 转 PCM → pilk 编码 SILK → AES-128-ECB 加密 → 上传微信 CDN，全程多处诊断日志用于排查依赖库版本漂移问题（`requirements.txt` 未锁定版本号）。

---

*本文档基于仓库当前内容（`main` 分支）梳理生成，反映的是代码实际实现，而非需求文档。*
