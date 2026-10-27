# AstrBot · OpenAI 兼容 & 多入口治理分支

> **非官方 AI 改版分支 / Unofficial AI-Modified Fork**
>
> 本仓库是 [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot) 的非官方分支，
> 在 **v4.28.2** 基础上做了 9 个方向的深度改造。全部代码改动由 AI 生成
> （WorkBuddy / Claude），在人工需求与决策指导下完成，**未经上游审核**。
>
> | | |
> |---|---|
> | **上游 Upstream** | https://github.com/AstrBotDevs/AstrBot |
> | **基线 Baseline** | v4.28.2（commit `3c7adaf`） |
> | **推荐分支** | **`v4.28.2`**（当前主力，功能最全） |
> | **协议 License** | GNU AGPL-3.0（**保持不变**，原作者版权声明完整保留） |
> | **改动规模** | 约 85 个文件 / +14,250 行 |
> | **改动说明** | [FORK.md](FORK.md) |
>
> **请勿将本分支的问题反馈到上游仓库。** 本分支与 AstrBot 团队无关。

---

## 这个分支解决什么问题

AstrBot 是一个优秀的 IM 智能体平台，但在"**当作被其他程序调用的 AI 后端**"和"**多入口差异化治理**"这两个方向上留了空白。本分支针对性地补齐了 9 块能力：

| # | 能力 | 一句话说明 | 上游是否有 |
|---|---|---|---|
| 1 | [OpenAI 兼容 API](#一openai-兼容-api) | 让 AstrBot 自己变成一个 OpenAI 端点 | ❌ 无 |
| 2 | [插件分类与入口策略](#二插件分类与入口策略) | 同一套插件，按入口决定谁可用 | ❌ 无 |
| 3 | [分级管理员](#三分级管理员tier-admin) | 把"管理员"从单一开关拆成 3 级 + 群角色 | ❌ 无 |
| 4 | [上下文管道](#四上下文管道与模块开关) | 把 7 个插件的注入按显式阶段表编排 | ❌ 无 |
| 5 | [适配器编辑器](#五适配器编辑器受控的-ai-改代码通道) | AI 提议改适配器，人工逐条审批落盘 | ❌ 无 |
| 6 | [模型组](#六模型组) | 给一组 provider 起名字，配置里填名字即可 | ❌ 无 |
| 7 | [用户画像与多轮反思](#七用户画像与多轮反思) | 从聊天记录推断"用户是谁" | ❌ 无 |
| 8 | [聊天内管理指令](#八聊天内管理指令) | 把 WebUI 的管理动作搬进聊天窗口 | ❌ 无 |
| 9 | [插件内测准入](#九插件内测准入) | 插件灰度发布：内测码 / 白名单 / 管理员放行 | ❌ 无 |

> 上述 9 个概念在上游 v4.28.2 中检索命中数均为 **0**（`git grep -i <概念> 3c7adaf`），
> 属于本分支新增，而非对上游既有功能的改写。

**Dashboard 侧新增 6 个页面**：`API 服务`、`管理员等级`、`模块开关`、`用户形象`、
`适配器编辑`、`插件内测`（上游 19 个页面 → 本分支 25 个）。

---

## 与原版的功能区别（逐项）

### 一、OpenAI 兼容 API

**原版**：AstrBot 的 Dashboard 服务（FastAPI，默认 `6185`）只有管理型 HTTP API（`/api/*`），
不对外提供任何推理协议。想让别的程序复用 AstrBot 的模型/人格/插件工具，只能自己写桥接。

**本分支**：在同一个 ASGI 应用上挂载 `/v1/*`，使 AstrBot **原生就是一个 OpenAI 端点**。

#### 端点

| 方法 | 路径 | 状态 |
|---|---|---|
| GET | `/v1/models`、`/v1/models/{id}` | ✅ 真实实现 |
| POST | `/v1/chat/completions` | ✅ 真实（SSE 流式 + function calling） |
| POST | `/v1/completions` | ✅ 真实（包装为 chat） |
| POST | `/v1/embeddings` | ✅ 真实 |
| POST | `/v1/rerank` | ✅ 真实（Jina / Cohere 风格，非 OpenAI 标准） |
| POST | `/v1/audio/transcriptions`、`/v1/audio/translations` | ✅ 真实（两者行为一致） |
| POST | `/v1/audio/speech` | ✅ 真实（返回二进制音频） |
| GET | `/v1/health` | ✅ 真实，**免鉴权** |
| POST/GET | `images/*`、`moderations`、`responses`、`files`、`batches`、`fine_tuning/jobs`、`assistants`、`vector_stores` | ⚠️ 占位，返回 **501**（需先通过鉴权） |

#### 鉴权为什么必须独立

上游 `auth.py` 把 `Authorization: Bearer` 解释为 **Dashboard JWT**，API Key 走的是 `ApiKey` 头；
而 OpenAI 客户端固定使用 `Authorization: Bearer sk-...`，两者语义冲突。因此本分支实现了**独立的 Key 体系**：

- 格式：`sk-astrbot-` + `token_urlsafe(32)`
- 存储：只存 SHA-256 摘要与掩码前缀，**明文仅在创建时返回一次**
- 与管理面 **JWT 完全隔离**：`sk-astrbot-` Key 只能调 `/v1/*`，不能管理 Dashboard
- 支持从 `Authorization: Bearer`、`Authorization: ApiKey`、`X-API-Key`、`?api_key=` 四处提取
- ⚠️ **无过期时间、无轮换机制**

#### 对话的四条分流路径

这是本模块最核心的设计。`/v1/chat/completions` 会按请求内容自动选择执行路径：

| 优先级 | 条件 | 走哪条路 | 行为 |
|---|---|---|---|
| 1 | `messages` 含 `role=tool`（工具结果续轮） | **直连 provider** | 完整历史由客户端提供，避免与管道记忆重复。**此条优先级最高，会覆盖 `pipe_tools`** |
| 2 | 含 `tools` 且 `pipe_tools=true` | **完整管道** | 服务端插件工具与客户端工具**并存** |
| 3 | 含 `tools` 且 `pipe_tools=false`（默认） | **直连 provider** | 保留客户端 function calling，**并注入 AstrBot 人格** |
| 4 | 不含 `tools` | **完整管道** | 人格 / 插件工具 / 记忆全部生效 |

**"完整管道"意味着什么**：请求会被注入 AstrBot 的 webchat 队列，走与真实聊天完全相同的
pipeline —— 人格、插件 LLM 工具、上下文注入、记忆全部生效。这是本分支最有价值的部分：
**你的 OpenAI 客户端能直接调用 AstrBot 生态里的插件工具。**

**`pipe_tools=true` 时的混合分流（S3 策略）**：

- 一轮内**只有**客户端工具 → 中断本轮，把 `tool_calls` 原样回传客户端执行
- 一轮内**混合**服务端与客户端工具 → 服务端工具照常执行并继续 loop；客户端工具写入占位结果，
  引导模型"下一轮单独调用"，使后续轮次自然收敛为纯外部轮

#### 人格注入

所有路径都会注入人格，但两条路径的机制不同：

- **管道路径**：`persona_id` → `post_data["_forced_persona_id"]` → webchat 队列 →
  event extra → `astr_main_agent.py` 在人格解析后**覆盖**
- **直连路径**：直接解析人格并追加为 `system` 消息

⚠️ 两处不对称：① 管道路径只按 `personas_v3` 的 **name** 匹配，v4 数据库里 UUID 形式的人格匹配不到；
② `inject_persona=false` **只关直连路径**，管道路径仍会应用会话/配置档案人格。

#### 配置

配置文件：`<数据目录>/openai_compat.json`（按 mtime 热加载，写入走临时文件 + `os.replace` 原子替换）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 总开关；关闭后 `/v1/*` 返回 503 |
| `default_username` | str | `"openai"` | 管道内会话身份 |
| `default_model` | str | `""` | 请求未指定 model 时的回落 |
| `request_timeout` | int | `180` | 管道执行超时（秒），**仅管道路径生效** |
| `model_aliases` | dict | `{}` | 别名 → provider id |
| `keys` | list | `[]` | Key 记录（无明文） |
| `inject_persona` | bool | `true` | 直连路径是否注入人格 |
| `persona_id` | str | `""` | 强制人格 id |
| `pipe_tools` | bool | `false` | 带 tools 时是否走完整管道 |

#### 已知限制

- **直连路径没有超时包装**（只有管道路径有）
- **流式对直连只是形式上的**：整段文本作为单个 delta 交付
- **`usage` 不是真实 token 数**，而是字符数估算；流式响应不带 `usage`
- 服务层**无并发控制、无限流**；底层 webchat 队列 `maxsize=128`，满时阻塞
- `/v1/completions` 接受 `stream` 参数但完全忽略
- `allow_plugins`（按 Key 放开插件）**只能手改 JSON**，没有 HTTP 写入入口

---

### 二、插件分类与入口策略

**原版**：所有已加载插件对所有平台一视同仁。想"只在 QQ 上用某个插件"只能靠插件自己判断平台。

**本分支**：给插件打上 `functional` / `interactive` 标签，**按入口决定哪些插件可用**。

#### 两个类别

| 类别 | 中文 | 定义 | 效果 |
|---|---|---|---|
| `functional` | 功能性 | 通过 LLM 工具提供能力，不依赖平台社交能力 | **所有入口可用** |
| `interactive` | 互动性 | 依赖收发消息、@、点赞、群管理、主动发言 | 受限入口不可用 |

> ⚠️ **默认是 `functional`**（`DEFAULT_CATEGORY`），不是旧文档写的"默认互动性"。

#### 类别来源优先级

```
覆盖表（Dashboard 手工设置）
  > metadata.yaml 的 category 字段
    > metadata.yaml 的 tags 自动推断
      > 默认 functional
```

`tags` 推断用两张词表（互动 33 词 / 功能 47 词）做子串匹配；**两类同时命中时取 `interactive`**。
面板会显示每个插件的类别来源（`override` / `metadata` / `tag` / `default`），来源为 `default`
的插件会被提示"未分类，按功能性处理"。

#### 内置入口策略

| 入口键 | 含义 | 默认允许 |
|---|---|---|
| `default` | 未列出的适配器兜底 | functional + interactive |
| `weixin_oc` | 个人微信（openclaw-weixin / ClawBot） | 仅 functional |
| `openai_compat` | OpenAI 兼容 HTTP 接口 | 仅 functional（可按 Key 放开） |

入口身份解析优先用事件自报的 `_entry`，其次用机器人实例 id，再退回适配器类型名。
**支持新增自定义入口**，且只持久化与内置默认不同的项。

#### 关键设计：运行时拦截，不改变加载

**所有插件照常 import、注册 handler、实例化**，分类只在两个点生效：

1. **消息侧**：`waking_check` 阶段过滤消息 handler
2. **工具侧**：LLM 请求前裁剪 `func_tool`

两处共用同一份上下文与 `tags` 来源，避免出现"handler 能跑但工具被裁"的分裂状态。

#### 管理 API

`/api/v1/plugin-categories/*`：`GET /plugins`、`GET /meta`、`PATCH /plugins/{name}`、
`DELETE /plugins/{name}`、`GET|PUT /entry-policy`。

持久化在 SQLite `data_v4.db` 的 `preferences` 表（`plugin_category_overrides`、
`entry_category_policy`），**不是 JSON 文件**。按机器人渠道的策略则写在
`cmd_config.json` 的 `platform[i].allowed_plugin_categories`。

⚠️ **前端没有 entry-policy 编辑入口**，自定义入口目前只能走 API。

---

### 三、分级管理员（tier admin）

**原版**：只有一份 `admins_id` 名单，命中即管理员，没有层级概念。

**本分支**：把原独立插件 `builtin_commands_extension` 并入内核 `builtin_commands`，
形成**三级超管 + 群角色**的完整权限体系。

#### 两套并存的刻度（混用会出错）

| 刻度 | 取值 | 用在哪 |
|---|---|---|
| **超管等级** | 0 普通成员 / 1 普通超管 / 2 中级超管 / 3 最高超管 | 对外 API、`/setop` 参数 |
| **层级 rank** | top 5 / mid 4 / normal 3 / 群主 2 / 群管 1 / 成员 0 | 越级比较、提权校验 |

#### 身份判定

- `get_level(event)` → 0-3
- `require_tier(min_level)` 装饰器，**异常时 fail-closed（降为 0）**
- 名单支持 `@名字(123456)`、`@1(2717858671)`、纯 ID 三种写法，自动归一化

⚠️ **找不到配置时所有人降为普通成员**，没有回退到 `is_admin()` 的逻辑。

#### 与原生 `is_admin()` 的关系

`WakingCheckStage` 不再读 `admins_id`，改读三级名单，命中任一超管即 `role="admin"`。
**这是语义上的替代**，但 `is_admin()` 接口和 `admins_id` 配置都保留了 —— 两者可能不一致，
且**分级名单不会写回 `admins_id`**。

#### 命令保护（重要：默认几乎不设防）

配置留空时 `_parse_scope("")` 返回 `"all"`，**内置命令默认 0 级即可用**。
真正有硬门槛的只有三条：

| 命令 | 门槛 | 说明 |
|---|---|---|
| `/op` | 中级超管及以上 | 不能授予高于自身的层级 |
| `/setop` | **仅最高超管** | 硬编码，配置无法放开 |
| `/deop` | 普通超管即可 | 须 `issuer_rank >= target_rank` |

另有 `dashboard_update`、`plugin get/off/on` 对非超管角色**硬拒绝**（白名单也覆盖不了）。

#### 管理 API 与页面

`/api/v1/tier-admin/config`（GET/PUT）、`/api/v1/tier-admin/resolve?user_id=`。
WebUI 页面「管理员等级」可编辑三级名单、逐级配置可用指令与禁用插件、查询任意用户层级。

#### 对外契约

`tier_api` 被跨包 import（`adapter_editor.py`、`star_manager.py`、`waking_check/stage.py`），
并在 `sys.modules` 注册 `builtin_commands_extension.tier_api` 别名，
**使老插件 `from builtin_commands_extension.tier_api import require_tier` 继续可用**。

---

### 四、上下文管道与模块开关

**原版**：多个上下文注入插件各自注册 `on_llm_request`，靠 `priority` 隐式排队，
顺序难以推理、冲突难以排查。

**本分支**：新增**编排层** `astrbot_plugin_context_pipeline`，用**显式阶段表**取代隐式优先级。

#### 阶段表

| order | 阶段 | 委托给哪个插件 |
|---|---|---|
| 100 | 群聊语境 | `astrbot_plugin_group_context` |
| 200 | 跨群记忆 | `astrbot_plugin_cross_group_memory` |
| 300 | 自学习 | `astrbot_plugin_self_learning` |
| 400 | 世界书 | `astrbot_plugin_worldbook` |
| 500 | 时间感知 | `astrbot_plugin_LLMPerception` |
| 600 | 主动回复提示词 | `astrbot_plugin_customized_active_reply_prompt` |
| 900 | 注入压缩 | `astrbot_plugin_prompt_compress` |
| 850 | 清理上下文（自带） | — |
| 1000 | 检查输出（自带） | — |

管道自身只注册 3 个钩子（`on_llm_request` 调度、`on_decorating_result` 检查、`on_astrbot_loaded` 绑定），
**业务逻辑仍留在被委托的插件里**。绑定用弱引用，插件热重载后自动重绑。

#### 两个自带能力（默认都关闭，因为各要多调一次 LLM）

**清理上下文**：当系统提示超过阈值（默认 1200 字符）时，用小模型把指定的注入块
（默认 `group_context` + `cross_group_memory`）**重写**成简洁要点。
与"注入压缩"的区别：压缩是**截断**（确定性长度），清理是**重写**（保语义去冗余）。

**检查输出**：发送前检查回复，三模式 `off` / `rule`（纯正则）/ `llm`（规则 + 小模型复核），
三动作 `block`（拦截替换）/ `warn`（只记日志）/ `strip`（删除命中片段，仅 rule 模式有效）。

#### 模块开关页面

Dashboard 的「模块开关」页**管的不是内核模块**，而是这个插件的阶段开关与总开关，
并展示每个阶段的绑定状态（未绑定/已执行/已跳过/出错/待命）与耗时。

API：`/api/v1/module-switches/registry`、`/{plugin_name}`、`/{plugin_name}/rebind`。

#### 已知偏差

- 注释称清理"插在压缩之前"，但因写在循环外，**实际在压缩之后执行**
- 总开关 `enabled` **不影响输出检查**；且改动需重载插件才生效
- `cmd_status` 与 `cmd_rebind` 注册了同名指令 `上下文管线`

---

### 五、适配器编辑器（受控的 AI 改代码通道）

**原版**：无。AI 无法修改自己的适配器代码。

**本分支**：提供一条**"AI 提议 → 静态门禁 → 人工逐条审批 → 显式落盘 → 自动备份"**的受控通道。

#### 安全边界（最重要）

**只能改平台适配器，不能改内核。** 依据：

- 路径正则硬性要求 `astrbot/core/platform/sources/<适配器名>/...`
- 10 个核心路径片段在子串层面先行拒绝（`core/provider`、`core/db`、`core/config`、
  `core/star`、`dashboard/api`、`startup`、`main.py`、`requirements`、`pyproject`、`setup`）
- AI 侧**没有 shell / 写文件能力**，只暴露一个提案工具

#### 六道门禁

| 门禁 | 检查内容 |
|---|---|
| ① 路径 | 拒绝 `..`、拒绝核心路径、必须匹配适配器目录正则 |
| ② 保护名单 | 默认 `aiocqhttp` 不可改 |
| ③ 配额 | 单用户 30 分钟内 ≤3 次、24 小时内 ≤10 次 |
| ④ 规模 | 单文件 ≤400 KB、单次增删行数 ≤200 |
| ⑤ 出站调用 | **新增**任何消息发送/上传类调用即拒绝（比对签名集合，删除调用放行） |
| ⑥ 危险导入 | 禁 `subprocess`、`socket`、`ctypes`、`shutil`、`pickle`、`marshal` 等；禁 `eval`/`exec`/`compile`/`__import__` |

任一门禁失败即整体拒绝，不生成提案。

#### 两阶段落盘

`approve` 只把状态改为 `approved`，**不动文件**；必须再点"写入文件"才真正落盘。
落盘前 `ast.parse` 预检 → 备份 → 写入 → 读回 `compile()` 校验 → 失败自动回滚。

审批权限默认 `top_admin`。所有操作写 `audit.log`。

#### 回滚

每次应用前自动备份，**只增不删**。回退前会先备份当前版本（回退本身可再回退）。
新建文件的回退 = 删除该文件。回退后状态保持 `applied` 以保留审计链。

#### 已知限制

- `metadata.yaml` 的"仅允许新增条目"限制**未实现**，目前是普通文件全量覆盖
- `apply` 后需**手动重启 AstrBot** 生效（无自动重启）
- 聊天命令的权限校验存在 `await` 缺失问题，实际可能恒为不通过

---

### 六、模型组

**原版**：配置里凡是填模型的地方都要填具体 provider id，同一组模型在几十处重复填写。

**本分支**：允许给一组 provider 起个名字，**组名被当作 provider id 直接使用**。

```json
{
  "model_groups": [
    {
      "name": "daily",
      "label": "日常对话",
      "members": ["HomeAPI/gpt-4o", "BackupAPI/claude", "fallback-group"],
      "strategy": "fallback",
      "scope": { "default": true },
      "enabled": true
    }
  ]
}
```

#### 能力

- 成员可填 provider id **或另一个组名**（支持嵌套、自动去重、检测循环引用）
- 两种策略：`fallback`（按声明顺序）与 `round_robin`（轮询游标存 DB，**跨重启保留**）
- 作用域：`scope.default` / `scope.umo`（群或用户）/ `scope.users`
- **插件零改动**：把配置里原本填 provider id 的地方改成组名即可

#### 解析优先级

```
显式组名 > 组 scope 匹配 > 组 scope.default > 原生 provider 解析
```

显式 provider id **不会被组遮蔽**。仅 `CHAT_COMPLETION` 参与分组，STT/TTS/Embedding/Rerank 不受影响。

#### 与上游 `56cbc4a4` 的区别（容易混淆）

上游那个 commit 的 "sticky groups" 是**纯前端**的：模型选择下拉框按 provider source 分桶、
分组标题吸顶。本分支的 `model_groups` 是**内核解析层**。两者**不同层、无冲突、互补**。

#### 已知限制（务必注意）

- **组内不做运行时故障转移**：只取第一个"已加载"的成员。
  真正的失败回退是上游的 `fallback_provider_ids`，**与本功能未打通** —— 组不会自动充当回退链
- 组级 `params`（按厂商翻译参数）**是死配置**，`map_params_to_provider` 无运行时调用方
- `scope.users` 与 `scope.umo` 实际比较同一个值，**未区分用户与群**
- **没有独立的模型组管理页面**，只能在配置页 JSON 编辑器里手写
- `/provider` 命令无法把组名设为会话模型（`set_provider` 要求 id 在 `inst_map` 中）

---

### 七、用户画像与多轮反思

**原版**：只有"机器人人格"（决定机器人怎么演），没有"用户是谁"的建模。

**本分支**：新增 `astrbot/core/persona/`，从聊天记录推断用户画像。

> ⚠️ **两者不是一回事**：上游人格在 `personas` 表、管的是"机器人怎么演"；
> 本分支画像在 `user_personas` 表、管的是"它眼里的用户是谁"。
> API 前缀只差一个 s（`/api/v1/persona` vs `/api/v1/personas`），极易混淆。

#### 数据模型

| 表 | 主键 | 关键字段 |
|---|---|---|
| `user_personas` | `user_id` | `summary` / `traits` / `evidence` / `confidence` / `sample_count` |
| `persona_group_adjusts` | `user_id` + `group_id` | 群内修正项（预留结构） |
| `persona_inference_logs` | 自增 id | 推理流水（成功/失败、耗时、样本数） |

设计上"一个用户一份全局画像 + 每个群一份修正项"，避免样本量最大的群绑架全局判断。

#### 推理机制

输入最近 120 条消息 → 统计客观指标（平均长度、提问率、夜间活跃率等）→
调用模型输出 `summary`(≤30 字) + `traits`（性格/习惯/情绪/社交角色）+ `evidence`（3-5 条原话）。

置信度分档：`(0, 0.0) / (20, 0.6) / (100, 0.8) / (300, 0.95)`，**硬上限 0.95**。
结论自相矛盾时按维度/标签重合度打折。失败时不写库、保留旧画像。

#### 多轮反思

**注意：反思的对象是"回复草稿"，不是用户画像。**

- 轮次 1-4（默认 2）：第 1 轮生成草稿，后续轮让模型自检改写
- 四种发言方式：`internal`（只发最终版）/ `progressive`（每轮一条）/ `with_thinking`（思考摘要 + 最终版）/ `off`
- 渐进去重：新段前 12 字二元组重合 ≥0.5 视为重复丢弃
- 任一轮失败即降级，沿用上一版草稿

#### ⚠️ 当前状态：数据层完整，但未接入对话主流程

这是必须如实说明的一点：

- `should_infer()`（自动空闲触发）与 `reflect()` **在全仓库都没有调用方**（除单元测试）
- 画像**不会进入任何 prompt** —— `core.persona` 只被 `db/sqlite.py` 和 `dashboard/api/persona.py` 引用
- 群修正项只有 DB 层的 `upsert`，**无业务调用方**
- `send_interval` 仅被解析与展示，`reflect()` 内没有 sleep 实现

**当前可用的是**：WebUI「用户形象」页的配置、手动推理、列表/详情/流水查看、删除。
可以理解为**一个完整的数据采集与展示闭环，但还没有接上对话链路**。

#### 管理 API

`/api/v1/persona/settings`、`/list`、`/{user_id}`、`/{user_id}/logs`、
`PUT /settings`、`PUT /reflection`、`POST /infer`、`DELETE /{user_id}`。

---

### 八、聊天内管理指令

**原版**：插件启停、模型切换、对话管理、人格切换只能在 WebUI 操作。

**本分支**：新增内置插件 `astrbot_plugin_chat_commands`，把这些动作搬进聊天窗口。

#### 命令清单（14 个 handler，无别名）

| 指令 | 用途 | 权限键 |
|---|---|---|
| `/llm` | 开关 LLM | `llm` |
| `/plugin ls` | 列出已加载插件 | `plugin ls` |
| `/plugin off <名>` | 禁用插件 | `plugin off` |
| `/plugin on <名>` | 启用插件 | `plugin on` |
| `/plugin get <地址>` | 从仓库安装插件 | `plugin get` |
| `/plugin help <名>` | 插件信息 + 其注册指令 | `plugin help` |
| `/model [名或序号]` | 列模型 / 切换（可跨 provider） | `model` |
| `/history [页]` | 对话记录（每页 6 条） | `history` |
| `/ls [页]` | 对话列表 | `ls` |
| `/groupnew <群ID>` | 为指定群新建对话 | `groupnew` |
| `/switch [序号]` | 按 `/ls` 序号切换 | `switch` |
| `/rename <新名>` | 重命名当前对话 | `rename` |
| `/del` | 删除当前对话 | `del` |
| `/persona [...]` | 查看 / 切换人格 | `persona` |

#### 与内核 `builtin_commands` 的关系

**互补而非重复**。内核保留的 14 个命令（`help`/`sid`/`reset`/`op`/`provider` 等）本插件一个都没注册；
本插件的 10 组指令，正是上游 `f50f6cd4` 从内核删掉的旧内置指令 —— 本分支把它们**恢复**为独立插件。

#### 定位

`metadata.yaml` 显式声明 `category: interactive`，因此**被 `openai_compat` 入口策略拦截**
（注意：是"被拦截"，插件仍然加载，只是 handler 被过滤）。

权限走三级超管体系，不是 `is_admin`。`plugin get/off/on` 对非超管有**不可覆盖的硬限制**。

#### ⚠️ 已知缺陷

`self.conversation_c` **从未实例化**（`main.py` 只创建了 4 个命令类，却有 6 处调用它），
导致 `/history`、`/ls`、`/groupnew`、`/switch`、`/rename`、`/del` 运行时抛 `AttributeError`。
`help.py`、`name.py`、`sid.py`、`setunset.py` 为死代码。

---

### 九、插件内测准入

**原版**：插件只有"装/不装"和"启用/禁用"两种状态，无法做灰度发布。

**本分支**：新增内测（beta）准入网关 `astrbot/core/star/beta_gate.py`，
把若干插件标记为"内测插件"后，**只有获授权用户才能使用**。

#### 三种授权途径

| 途径 | 说明 |
|---|---|
| 白名单用户 | 后台手动填写用户 ID |
| 一次性内测码 | `/内测 <内测码>` 兑换；码可限定单个插件，也可放开全部内测插件 |
| 管理员放行 | 可配置是否对管理员豁免 |

#### 拦截方式（命令拦截，钩子静默）

未获授权的用户触发内测插件的**命令**时，内核拦截并回复**自定义提示语**；
内测插件的**被动钩子与 LLM 工具则静默跳过** —— 避免每条消息都刷提示。

生效点与插件分类完全一致（两处）：

- 消息侧：`waking_check` 阶段在插件分类过滤**之后**再过滤 handler
- 工具侧：`astr_main_agent` 在入口裁剪**之后**再裁剪 `func_tool`

#### 插件名模糊解析

配套的 `astrbot/core/star/plugin_resolver.py` 解决"手写插件名不完整"的问题：
`quote_pool` / `quotepool` / `引用池` 都能定位到 `astrbot_plugin_quote_pool`。

八级匹配优先级：完整注册名 → 去前缀短名 → 前缀匹配 → 子串匹配 → 分词匹配 →
子序列匹配 → `difflib` 拼写容错 → 描述/标签/展示名匹配。
候选直接来自内核实时维护的 `star_registry`，**不做缓存**，新装插件立即可解析。

#### 状态存储

全部存在 sp 全局存储（SQLite `preferences` 表），**改动即时生效、无需重启**：

| 键 | 内容 |
|---|---|
| `beta_gate.config` | `{enabled, plugins, user_ids, admin_bypass, block_message, redeem_success_message}` |
| `beta_gate.keys` | `{key: {scope, note, created_at, expires_at, used_by, used_at}}` |
| `beta_gate.grants` | `{uid: {plugins, source, at}}` |

#### 命令与 API

聊天指令：`/内测 <内测码>`（兑换）、`/内测状态`（查看自己的资格）。

管理 API：`/api/v1/plugin-beta/*` —— `GET /plugins`、`GET /resolve` 做快速选中；
内测名单、白名单、内测码与兑换记录的读写。

WebUI 页面「插件内测」（`/plugin-beta`），配套组件 `PluginNameInput.vue` 提供插件名模糊补全。

---

## 快速开始

### 部署本分支

```bash
# 1. 取 v4.28.2 分支（推荐）
git clone -b v4.28.2 https://github.com/YLHhhh123/astrbot-openai-compat.git
cd astrbot-openai-compat

# 2. 安装依赖并启动（与上游一致）
uv sync
uv run main.py
```

Dashboard 默认监听 `6185`。上游的 Docker / 桌面端 / 面板部署方式**全部继续适用**，
详见 [AstrBot 官方文档](https://docs.astrbot.app/)。

### 启用 OpenAI 兼容接口

1. 打开 Dashboard → **API 服务** 页
2. 新建 Key（明文只显示一次）
3. 用任意 OpenAI 客户端调用：

```bash
curl http://127.0.0.1:6185/v1/chat/completions \
  -H "Authorization: Bearer sk-astrbot-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:6185/v1",
    api_key="sk-astrbot-xxxxxxxx",
)

# 不带 tools：走完整管道，人格/插件工具/记忆全部生效
print(client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "帮我查一下今天的天气"}],
).choices[0].message.content)
```

### 配置插件分类

Dashboard → **插件管理** → 已安装插件页顶部「插件分类与入口策略」面板。
渠道级策略在**机器人设置**里按渠道勾选。

---

## 验证情况

| 层级 | 手段 | 结果 |
|---|---|---|
| 内核测试 | 7 个套件（插件分类 87 / 模型组 47 / 画像 37 / 推理 44 / 反思 45 / 群集成 10 / 人格补丁 14） | ✅ **284/284** |
| 插件测试 | 适配器编辑器门禁 42 + 提案 33；上下文管道 32 + 集成 25 + 静态 5 | ✅ **137/137** |
| **合计** | | ✅ **421 项全部通过** |
| 语法 | `py_compile` | ✅ |
| 前端 | `vite build` + `vue-tsc` 类型检查 | ✅ |
| **真机运行** | 生产服务器（v4.28.2）长期运行 | ✅ 见下 |

**真机验证证据**（服务器 `10.31.3.142`）：

- `GET /v1/health` → `200 {"status":"ok","enabled":true,"object":"astrbot.openai_compat"}`
- `GET /v1/models`（无 Key）→ `401 invalid_api_key`（鉴权链路生效）
- Dashboard 全部 fork 模块文件存在，服务 `active`、`NRestarts=0`
- 三级超管名单、插件分类覆盖表均已持久化并在运行中生效

**仍未覆盖的部分**：

- 用户画像的自动触发与注入（代码未接线，见第七节）
- 多轮反思在真实回复链路的接入（同上）
- 适配器编辑器在真实适配器上的端到端改动
- 部分模块的边界情况（各节"已知限制"已逐条列出）

---

## 分支说明

| 分支 | 基线 | 功能 | 建议 |
|---|---|---|---|
| **`v4.28.2`** | v4.28.2（`3c7adaf`） | **全部 9 个模块** | ✅ **推荐使用** |
| `main` | v4.28.0（`a412146`） | 仅前 3 个模块 | 历史分支，保留兼容 |

两个分支**功能并不相同**：`main` 只有 OpenAI 兼容 API、插件分类、Dashboard 页面；
分级管理员、上下文管道、适配器编辑器、模型组、用户画像、聊天指令都只在 `v4.28.2` 上。

---

## 免责声明

1. **本分支非官方**，与 AstrBot 团队无关，未经过上游审核。
2. **全部改动由 AI 生成**，可能存在设计缺陷、边界情况未覆盖、未预料的行为。
3. 各章节的"已知限制"是**如实记录**，请在使用前阅读。特别是：
   - 用户画像与多轮反思**尚未接入对话主流程**
   - 聊天指令的 6 个对话管理命令**存在 `AttributeError`**
   - 模型组的 `params` 与组内故障转移**未生效**
   - OpenAI 直连路径**无超时**，`usage` 为估算值
4. **请勿将本分支的问题反馈到上游仓库**，请在本仓库提 issue。
5. 使用本分支造成的一切后果由使用者自行承担。

---

## 协议与致谢

本项目遵循上游相同的 **GNU Affero General Public License v3.0**（见 [LICENSE](LICENSE)），
保留原项目全部版权声明与署名。任何对本分支的再分发同样须遵循 AGPL-3.0。

感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 及其贡献者提供了这个优秀的项目。

---

<div align="center">

_Companionship and capability should never be at odds._

<img src="https://files.astrbot.app/watashiwa-koseino-desukara.gif" width="100"/>

</div>
