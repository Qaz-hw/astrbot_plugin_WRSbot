# WRSbot · 部署说明文档

> 本文档包含 WRSbot 上线所需的完整步骤：运行环境、依赖、飞书开放平台配置、API 权限清单、首次启动验收清单。
> 配合根目录 `README.md`（架构 / 代码结构）和 `AGENT_CONFIG.md`（Agent / Prompt 配置）阅读。

---

## 1. 运行环境要求

### 1.1 系统

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | Linux / macOS / Windows | 推荐 Linux Server（Ubuntu 22.04+ / CentOS 8+） |
| Python | **>= 3.12** | AstrBot 4.23.6 要求 |
| 网络 | 可访问 `open.feishu.cn` + LLM Provider 域名 | 私有部署须开通到飞书的出站 HTTPS |
| 公网入口 | 可选 | 飞书 WebSocket 长连接模式无需公网入口；回调模式需要 HTTPS |
| 时区 | 建议 UTC+8 | 部分时间戳显示采用 UTC+8 |

### 1.2 依赖框架

WRSbot 是 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的插件，不能脱离 AstrBot 单独运行。

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| AstrBot | >= 4.23.6 | 自动安装在父仓库根目录 |
| lark-oapi | >= 1.4.15 | 飞书官方 Python SDK；AstrBot 已声明该依赖 |
| 包管理 | `uv` | AstrBot 默认使用 |

### 1.3 LLM Provider

至少配置 1 个 AstrBot 支持的 LLM Provider（OpenAI / Anthropic / 通义 / 文心 / DeepSeek / Gemini 等均可）。
WRSbot 通过 AstrBot 的 Provider 抽象层调用 LLM，**插件本身不绑定具体厂商**。

详情见 `AGENT_CONFIG.md` 第 4.2 节 (模型选型建议)。

---

## 2. 飞书开放平台配置

### 2.1 创建企业自建应用

1. 登录 [飞书开放平台](https://open.feishu.cn) → 「开发者后台」 → 「创建企业自建应用」
2. 记录以下凭证（部署 AstrBot 飞书适配器时需要填）：
   - **App ID**（`cli_*`）
   - **App Secret**
   - **Encrypt Key**（事件加密用，可选但推荐开启）
   - **Verification Token**

### 2.2 启用机器人能力

「应用功能」 → 「机器人」 → 启用机器人，设置：
- 机器人名称（建议 `WRSbot` 或 `部门周报助手`）
- 机器人头像
- 默认消息卡片回调（**重要**：必须启用，否则卡片按钮失效）

### 2.3 事件订阅 / 长连接

WRSbot 推荐使用 **长连接模式（WebSocket）**，无需公网入口和 HTTPS 回调地址。

「事件订阅」面板：
1. 模式选择：「**长连接 / WebSocket**」
2. 订阅事件：见下方权限清单中的「事件 scope」列

如果选择 HTTPS 回调模式：
1. 配置 Request URL（指向部署的 AstrBot 实例）
2. 配置 Encrypt Key + Verification Token（同 2.1）
3. 确保公网入口 HTTPS 证书有效

### 2.4 应用发布

测试阶段：把测试人员加入「应用可用范围」即可使用。
正式上线：通过企业管理员后台审核发布到全员（或指定部门）。

---

## 3. 飞书 API 权限清单（按功能分组）

> 下列权限按 WRSbot 实际使用的 API 调用反向推导，已对应到飞书开放平台权限中心的 scope 名称。
> **scope 名称请以飞书开放平台后台的最新命名为准**（飞书近年来对 scope 做过几次拆分 / 重命名）；
> 如某权限名找不到，请在权限中心搜索相近关键词，或参考飞书官方 API 文档对应的「权限要求」一节。

### 3.1 通讯录权限

| Scope 名 | 用途 | 对应 SDK 调用 |
| --- | --- | --- |
| `contact:department.base:readonly` | 列出部门 | `lark_api.contact.v3.department.alist` |
| `contact:user.base:readonly` | 列出 / 查询用户（姓名、open_id、所属部门、是否管理者） | `lark_api.contact.v3.user.alist` / `user.aget` |
| `contact:contact:readonly` | （部分场景）只读通讯录通用权限 | 上述任一调用的兜底权限 |

**用途说明**：
- 加载部门树，识别请求者是否为部门负责人
- 列出部门成员，与本周文件内容做对比，识别已 / 未提交者
- `/whoami`、`/check_user` 等管理命令

### 3.2 云空间权限

| Scope 名 | 用途 | 对应 SDK 调用 |
| --- | --- | --- |
| `drive:drive.metadata:readonly` | 列出绑定文件夹下的文件 | `lark_api.drive.v1.file.alist` |
| `drive:file:readonly` | 读取文件元信息 | 上述调用的可选补充权限 |

**用途说明**：
- 部门负责人绑定本部门周报文件夹后，机器人扫描该文件夹找到本周对应的多维表格或文档
- 识别本周文件依赖 ISO 周次匹配（`2026-W21` 等）+ 文件名规则 + 必要时 LLM 兜底

### 3.3 飞书文档（docx）权限

| Scope 名 | 用途 | 对应 SDK 调用 |
| --- | --- | --- |
| `docx:document:readonly` | 读取文档全文 | `lark_api.docx.v1.document.araw_content` |
| `docx:document.block:readonly` | 读取文档 block 结构（提取 markdown 风格段落） | `lark_api.docx.v1.document_block.alist` / `aget` |
| `docx:document` | 向文档末尾写入新 block（提交周报总结时） | `lark_api.docx.v1.document_block_children.acreate` |

**用途说明**：
- 部门以 Doc/Docx 作为周报载体时，读取本周文档解析成员提交
- 「写入文档」按钮触发时，把生成的部门周报总结作为新 block 追加到文档末尾

### 3.4 多维表格（bitable）权限

| Scope 名 | 用途 | 对应 SDK 调用 |
| --- | --- | --- |
| `bitable:app:readonly` | 读取多维表格元信息、表 / 字段 / 记录 | `lark_api.bitable.v1.app_table.alist` / `app_table_field.alist` / `app_table_record.alist` |
| `bitable:app` | 创建 / 更新「部门总结」记录行 | `lark_api.bitable.v1.app_table_record.acreate` / `aupdate` |

**用途说明**：
- 部门以 Bitable 作为周报载体时，读取员工记录用作提交检查 + 报告生成
- 「写入文档」按钮触发时，按列名结构化写回「部门总结」行（先查 → 有则 update，无则 create）

### 3.5 消息（im）权限

| Scope 名 | 用途 | 对应 SDK 调用 |
| --- | --- | --- |
| `im:message` | 发送私聊消息、群消息、交互卡片 + patch 已发送消息 | `LarkMessageEvent._send_im_message` / `lark_api.im.v1.message.apatch` |
| `im:message:send_as_bot` | （部分场景）以 Bot 身份主动发消息 | 上述调用的等价 scope（按平台版本而定） |
| `im:resource` | （可选）发送图片 / 文件等资源 | 当前不需要，预留 |

### 3.6 事件订阅 scope（不是 API 权限，是订阅 scope）

| 事件 scope | 用途 |
| --- | --- |
| `im.message.receive_v1` | 接收用户发给机器人的消息（私聊 + @机器人 群消息），用于 slash command + 自然语言触发 |
| 卡片回调 | 启用「消息卡片回调」即可，不需要单独 scope，但需要在应用控制台「事件订阅」面板下勾选；WRSbot 用 `p2.card.action.trigger` |

### 3.7 CardKit（流式卡片）

| Scope 名 | 用途 | 对应 SDK 调用 |
| --- | --- | --- |
| `cardkit:card` | 创建 / 更新 / 关闭流式卡片实体 | `lark_api.cardkit.v1.card.acreate` / `card_element.acontent` / `card.asettings` |

**用途说明**：
- 周报生成时，把 LLM 流式输出实时推送到飞书 CardKit 卡片
- 比一次性发完整消息体验好很多（不会让用户面对 30 秒"加载中"）

### 3.8 权限申请汇总表

部署时，把以下 scope 全部勾选申请：

```text
通讯录：
  contact:department.base:readonly
  contact:user.base:readonly
  contact:contact:readonly

云空间：
  drive:drive.metadata:readonly
  drive:file:readonly

飞书文档：
  docx:document
  docx:document.block:readonly
  docx:document:readonly

多维表格：
  bitable:app
  bitable:app:readonly

消息：
  im:message
  im:message:send_as_bot

卡片：
  cardkit:card

事件订阅：
  im.message.receive_v1
  消息卡片回调（在事件订阅面板启用）
```

> 实际名称可能因飞书后台版本略有不同；若有权限名找不到，请在权限中心按关键词搜索后确认。

---

## 4. AstrBot 飞书适配器配置

### 4.1 在 AstrBot Dashboard 中添加飞书平台

1. 启动 AstrBot 后访问 Dashboard（默认 `http://localhost:6185`）
2. 「平台配置」 → 「添加平台」 → 选择 `Lark / Feishu`
3. 填入 2.1 节记录的 App ID / App Secret / Encrypt Key / Verification Token
4. 模式选择：长连接（推荐）或回调
5. 保存并启用

### 4.2 在 AstrBot Dashboard 中添加 LLM Provider

1. 「Provider 配置」 → 「添加 Provider」
2. 选择厂商，填入 API Key + 模型名
3. 设为默认 Provider

---

## 5. 插件安装与配置

### 5.1 安装插件

将插件目录放置到 AstrBot 的 `data/plugins/astrbot_plugin_WRSbot/`。

如果是 Git 部署：

```bash
cd <AstrBot 根目录>/data/plugins/
git clone https://github.com/Qaz-hw/astrbot_plugin_WRSbot.git
```

重启 AstrBot，Dashboard 「插件管理」中可看到 `WRSbot`，启用即可。

### 5.2 插件 `.env` 配置

插件目录下创建 `.env`（已在 `.gitignore` 中，不会被提交）：

```env
# 部门文件夹绑定（部门负责人通过卡片绑定后自动写入；也可手动预填）
# 格式：DEPT_FOLDER_<open_department_id 中横线替换为下划线>=<folder_token>
# 例：
# DEPT_FOLDER_od_c5a40c187b6a50163de9c30b4dbe84b4=fldcnABCDEFGHIJKL
# DEPT_URL_od_c5a40c187b6a50163de9c30b4dbe84b4=https://example.feishu.cn/drive/folder/fldcnABCDEFGHIJKL

# 管理员 open_id 列表（可选，逗号分隔）
# 通过 /whoami 命令查询 open_id
# ADMIN_OPEN_IDS=ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

`.env` 字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `DEPT_FOLDER_<id>` | 否（卡片流程自动写入） | 部门周报文件夹 token |
| `DEPT_URL_<id>` | 否 | 文件夹的 URL（用于回显给员工） |
| `ADMIN_OPEN_IDS` | 否 | 跨部门超级管理员；未设置时按通讯录中的部门负责人识别 |

> 卡片按钮可视化绑定后会自动写入 `.env`。生产环境也可由运维预先填好。

### 5.3 卡片模板 ID（可选自定义）

WRSbot 使用了 14 张飞书卡片模板（位于 `services/lark_card.py` 顶部的 `*_CARD_ID` 常量）。

默认值是开发环境下创建的模板 ID。生产部署时建议：

1. 在飞书开放平台 → 「**飞书卡片搭建工具**」中重新搭建一套属于本企业的模板
2. 通过环境变量覆盖（变量名见源码顶部）：

```env
WRSBOT_WELCOME_CARD_ID=AAqxxxxxxxxxx
WRSBOT_COMMAND_LIST_ID=AAqxxxxxxxxxx
WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID=AAqxxxxxxxxxx
WRSBOT_BINDING_FEEDBACK_SUCCESS_CARD_ID=AAqxxxxxxxxxx
WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID=AAqxxxxxxxxxx
WRSBOT_ADMIN_VIEW_CARD_ID=AAqxxxxxxxxxx
WRSBOT_ADMIN_REPORT_SUMMARY_CHECK_CARD_ID=AAqxxxxxxxxxx
WRSBOT_CARD_USER_VIEW_ID=AAqxxxxxxxxxx
```

> 临时演示也可以直接用默认模板，但默认模板的版权属于开发者环境，正式生产请自建。

---

## 6. 启动 AstrBot

在 AstrBot 根目录：

```bash
uv sync           # 同步依赖
uv run main.py    # 启动主进程
```

默认服务地址：
- AstrBot API：`http://localhost:6185`
- Dashboard 静态页：`http://localhost:6185` 或 dev 模式 `http://localhost:3000`

后台运行（生产环境）推荐 systemd / supervisord / pm2 / Docker，确保进程崩溃后自动重启。

---

## 7. 首次上线验收清单

按顺序执行以下命令 / 操作，全部通过后才能认为部署完成。

### 7.1 基础连通性

| 操作 | 期望结果 |
| --- | --- |
| 在飞书私聊机器人发送 `/whoami` | 输出当前 sender_id / open_id / 平台名等调试信息 |
| `/Hello` | 收到欢迎卡片（确认卡片渲染 + im:message 权限） |
| `/test_llm 你好` | LLM 正常回复（确认 Provider 配置） |
| `/test_feishu_contact` | 输出实时部门树（确认 contact 权限） |

### 7.2 通讯录与角色识别

| 操作 | 期望结果 |
| --- | --- |
| `/test_cached_feishu_contact` | 缓存版部门树，与实时版一致 |
| `/list_contacts` | 列出所有可见员工 |
| `/check_user` | 输出当前用户的部门 / 上级 / 是否管理者 |

### 7.3 文件夹绑定流程

| 操作 | 期望结果 |
| --- | --- |
| 部门负责人发送 `/文件夹配置` | 收到绑定卡片，列出该负责人的所有管辖部门 |
| 粘贴飞书文件夹链接并提交 | 卡片切换为「绑定成功」，`.env` 中写入 `DEPT_FOLDER_*` |
| `/dump_bindings` | 列出所有部门绑定状态 |

### 7.4 本周文件识别

| 操作 | 期望结果 |
| --- | --- |
| 在绑定文件夹中放一份命名包含 `2026-W21` 的多维表格或文档 | `/test_weekly_file` 命中该文件 |
| 改成无 ISO 周标签的命名 | 触发 LLM 兜底，仍能识别 |

### 7.5 端到端流程

| 操作 | 期望结果 |
| --- | --- |
| 团队成员填写本周周报到绑定文件 | 文件中出现各人姓名 + 内容 |
| 负责人点击「开始使用」 → 进入管理视图 | 看到 N/M 提交比例 |
| 点击「催交报告」 | 未提交成员收到提醒卡 |
| 点击「开始周报汇总」 | 流式卡片逐字生成周报内容 |
| 在改写输入框输入"更简洁"并点击「重新改写」 | 生成新版本 |
| 点击「写入文档」 | 回到原文件确认部门总结被追加 / 写入「部门总结」行 |

### 7.6 自然语言触发

| 操作 | 期望结果 |
| --- | --- |
| 在群聊 `@机器人 帮我生成本周周报` | 群里收到 toast，私聊收到管理视图卡 |
| 私聊"我要写周报" | 收到本部门周报文件夹 URL |
| 私聊"今天天气如何" | 不触发（confidence 过低，静默跳过） |

---

## 8. 部署后维护

### 8.1 日志位置

- AstrBot 主日志：`<AstrBot 根目录>/data/logs/`
- 关键日志前缀（grep 用）：
  - `[NaturalTrigger]` — 自然语言意图分类
  - `[AdminView]` / `[UserView]` — 视图加载流程
  - `[Generate]` / `[3Stage]` — 周报生成流水线
  - `[Submit]` — 写入文档 / 多维表格
  - `[Lark_card]` — 卡片回调与按钮路由
  - `[ManagerStyle]` / `[StyleStructure]` — 风格档案

### 8.2 缓存刷新

- 通讯录缓存：默认每 5 小时自动刷新，也可在绑定卡片上点「重新扫描」手动触发
- 部门绑定 `.env`：进程启动时加载；运行中修改 `.env` **不会**热生效，需重启 AstrBot

### 8.3 用户反馈渠道

- 风格档案在管理者使用过程中持续微调（`/my_style`）
- 周报输出质量异常 → 检查 `services/report.py` 的 `_*_SYSTEM` prompt + 日志
- 卡片渲染异常 → 检查 `services/lark_card.py` 中模板 ID 与飞书后台是否一致

### 8.4 监控建议

生产环境建议监控：

| 指标 | 阈值参考 |
| --- | --- |
| AstrBot 进程存活 | 必须存活 |
| 飞书 WebSocket 连接状态 | 断连超 60s 报警 |
| LLM Provider 调用错误率 | > 5% 报警 |
| 单次周报生成总耗时 | > 90s 报警（30-50 人部门正常 30-60s） |
| 卡片 patch 失败率 | > 10% 报警 |

---

## 9. 卸载 / 迁移

### 9.1 卸载

1. AstrBot Dashboard 「插件管理」禁用 WRSbot
2. 删除 `<AstrBot 根目录>/data/plugins/astrbot_plugin_WRSbot/` 目录
3. 飞书开放平台「应用功能」可保留，不影响其他企业应用

### 9.2 迁移环境

1. 复制插件目录到目标机器
2. 复制 `.env`（保留部门绑定 + 管理员配置）
3. 复制 AstrBot 的 `data/shared_preferences/` 目录（保留管理者风格档案 + 周报草稿历史）
4. 在新机器重新部署 AstrBot 主框架
5. 在飞书后台修改回调地址 / 长连接连接源

---

## 10. 常见问题（FAQ）

**Q1：群里 `@机器人 帮我生成周报` 收到 toast 但私聊没卡片？**

A：检查 `services/pipelines/views.py::admin_view` 日志中的 `[AdminView]` 警告。
常见原因：
- 该用户不在通讯录里被识别为部门负责人 → 检查 `/check_user`
- 该部门未绑定文件夹 → 让该用户执行 `/文件夹配置`
- 本周文件未创建或命名不含 `2026-Wxx` 周次标签

**Q2：周报生成卡在「正在解析 N 份周报内容…」？**

A：通常是 LLM Provider 限流。
- 降低 `services/pipelines/_base.py::_LLM_SEMAPHORE` 的并发上限（如从 8 降到 4）
- 或在 Provider 后台扩容 RPM/TPM

**Q3：写入文档后格式与原文档对不上（章节层级 / bullet 样式不同）？**

A：当前 Write 阶段的 markdown 是按 Lark 卡片显示效果调优的。已在 `services/pipelines/report.py` 的 doc-append 分支留下 TODO 注释，后续会加 doc 已有格式自动对齐。

**Q4：管理者更换 LLM 模型后输出风格变了？**

A：Write 阶段的输出强依赖模型本身的中文写作能力。建议：
- Plan / Extract 可以用更便宜的小模型（任务结构化、易控）
- Write / Rewrite 保留中-高端模型（影响最终交付观感）

**Q5：能否禁用某个 Agent？**

A：可以。例如关闭自然语言触发：注释掉 `main.py::natural_report_trigger` 的 `@filter.event_message_type(...)` 装饰器。
关闭 Style Structuring：在 `utils/manager_style.py::save_manager_style` 中传 `llm_provider=None`，会回退到原始字段渲染。

---

## 11. 联系与支持

- 插件仓库：https://github.com/Qaz-hw/astrbot_plugin_WRSbot
- AstrBot 主仓库：https://github.com/AstrBotDevs/AstrBot
- 飞书开放平台文档：https://open.feishu.cn/document
- Bug / 功能反馈：在插件仓库提 Issue，附复现步骤 + 日志片段
