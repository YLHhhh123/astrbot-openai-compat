# 会话指令集（astrbot_plugin_chat_commands）

提供会话、插件、模型、人格相关的扩展指令。**权限分级由内核内置插件
`builtin_commands` 统一提供**，本插件不含任何权限配置。

## 指令

| 指令 | 说明 |
|---|---|
| `/llm` | 开启 / 关闭 LLM |
| `/plugin ls\|off\|on\|get\|help` | 插件管理 |
| `/model` | 查看或切换模型 |
| `/history [页]` | 查看对话记录 |
| `/ls [页]` | 查看对话列表 |
| `/groupnew <sid>` | 创建新群聊对话 |
| `/switch [序号]` | 切换对话 |
| `/rename <新名>` | 重命名对话 |
| `/del` | 删除当前对话 |
| `/persona` | 查看或切换人格 |

管理员指令 `/op` `/setop` `/deop` 与基础指令（`/help` `/sid` `/reset` 等）由内核
`builtin_commands` 提供，不在本插件内，以免重复注册。

## 分类

`interactive`（互动性）—— 属于面向对话的互动能力，OpenAI 兼容 HTTP 入口默认不加载。

## 配置

无独立配置。权限读取自 `data/config/builtin_commands_config.json`，
可在 WebUI「管理员等级」页面编辑。
