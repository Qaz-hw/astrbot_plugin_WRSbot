#==============================================================
#  main.py — WRSbot Plugin Entry Point
#==============================================================
#
#  Responsibilities:
#    - Register the AstrBot plugin
#    - Register commands and event listeners
#    - Call service methods
#    - Keep code thin and readable
#
#  Does NOT contain:
#    - Feishu card definitions or sending logic (services/lark_card.py)
#    - Document parsing logic (services/doc.py)
#    - LLM prompt logic (prompts/)
#    - Report generation workflows (services/report.py)
#==============================================================

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, sp
from astrbot.api.platform import MessageType

from lark_oapi.api.drive.v1 import ListFileRequest
from lark_oapi.api.docx.v1 import RawContentDocumentRequest

from .services.lark_card import LarkCardService


@register("helloworld", "YourName", "一个简单的 Hello World 插件", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context: Context = context
        self.lark_api = None
        self.card_service: LarkCardService = None

    async def initialize(self):
        """Grab lark_api from the platform adapter and set up services."""
        get_insts = getattr(self.context.platform_manager, "get_insts", None)
        if get_insts:
            for platform in get_insts():
                if hasattr(platform, "lark_api") and self.lark_api is None:
                    self.lark_api = platform.lark_api

        self.card_service = LarkCardService(self.lark_api)
        self.card_service.inject_into_dispatcher(self.context.platform_manager)

#==============================================================
#                         Card Commands
#==============================================================

    @filter.command("发起告警")
    async def cmd_send_alarm(self, event: AstrMessageEvent):
        """向当前会话发送告警卡片。"""
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        if not isinstance(event, LarkMessageEvent):
            yield event.plain_result("此命令仅支持飞书平台。")
            return

        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            await self.card_service.send_alarm_card("chat_id", event.message_obj.group_id)
        else:
            await self.card_service.send_alarm_card("open_id", event.get_sender_id())

        event._has_send_oper = True

    @filter.command("你好")
    async def cmd_send_welcome(self, event: AstrMessageEvent):
        """向发送消息的用户发送欢迎卡片。"""
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        if not isinstance(event, LarkMessageEvent):
            yield event.plain_result("此命令仅支持飞书平台。")
            return

        await self.card_service.send_welcome_card(event.get_sender_id())
        event._has_send_oper = True

#==============================================================
#                      General Commands
#==============================================================

    @filter.command("helloworld")
    async def helloworld(self, event: AstrMessageEvent):
        """这是一个 hello world 指令"""
        user_name = event.get_sender_name()
        logger.info("✅ Hello World 指令被触发了！")
        message_str = event.message_str
        yield event.plain_result(f"Hello, {user_name}, 你发了 {message_str}!")

    @filter.command("创建周报总结")
    async def create_weekly_report(self, event: AstrMessageEvent):
        """这是一个创建周报总结的指令"""
        user_name = event.get_sender_name()
        logger.info("✅ 创建周报总结指令被触发了！")
        weekly_report = f"{user_name} 的周报总结：\n- 完成了任务 A\n- 参与了会议 B\n- 学习了新技术 C"
        yield event.plain_result(weekly_report)

    @filter.command("关于WRSbot")
    async def about_wrsbot(self, event: AstrMessageEvent):
        """这是一个关于WRSbot的指令"""
        yield event.plain_result("WRSbot 是一个基于 AstrBot 框架的机器人插件。\n 它可以帮助用户生成周报总结，并提供一些关于 WRSbot 的信息。")

    @filter.command("whoami")
    async def cmd_whoami(self, event: AstrMessageEvent):
        """转储当前消息事件中所有可获取的用户、会话、平台信息。"""
        obj = event.message_obj
        group = getattr(obj, "group", None)

        lines = [
            "══ 发送者 ══",
            f"sender_id       : {event.get_sender_id()}",
            f"sender_name     : {event.get_sender_name()}",
            f"role            : {event.role}",
            f"is_admin        : {event.is_admin()}",
            "",
            "══ 消息 ══",
            f"message_str     : {event.message_str}",
            f"message_id      : {getattr(obj, 'message_id', 'N/A')}",
            f"message_type    : {event.get_message_type().value}",
            f"message_outline : {event.get_message_outline()}",
            f"is_private_chat : {event.is_private_chat()}",
            f"timestamp       : {getattr(obj, 'timestamp', 'N/A')}",
            f"created_at      : {event.created_at}",
            "",
            "══ 会话 ══",
            f"session_id      : {event.get_session_id()}",
            f"unified_origin  : {event.unified_msg_origin}",
            f"is_wake         : {event.is_wake}",
            f"is_at_or_wake   : {event.is_at_or_wake_command}",
            "",
            "══ 平台 ══",
            f"platform_name   : {event.get_platform_name()}",
            f"platform_id     : {event.get_platform_id()}",
            f"self_id (bot)   : {event.get_self_id()}",
            "",
            "══ 群组 ══",
        ]

        if group:
            lines += [
                f"group_id        : {group.group_id}",
                f"group_name      : {group.group_name or 'N/A'}",
                f"group_owner     : {group.group_owner or 'N/A'}",
                f"group_admins    : {group.group_admins or 'N/A'}",
                f"group_avatar    : {group.group_avatar or 'N/A'}",
                f"member_count    : {len(group.members) if group.members else 'N/A'}",
            ]
        else:
            lines.append("（私聊，无群组信息）")

        extras = event.get_extra()
        lines += [
            "",
            "══ 额外信息 ══",
            f"extras          : {extras if extras else '（空）'}",
        ]

        yield event.plain_result("\n".join(lines))

#==============================================================
#                        LLM Commands
#==============================================================

    @filter.command("test_llm")
    async def test_llm(self, event: AstrMessageEvent):
        """调用 LLM 并返回回答。用法：/test_llm <问题>"""
        prompt = event.message_str.removeprefix("/test_llm").strip()
        persona = await self.context.persona_manager.get_default_persona_v3(umo=event.unified_msg_origin)
        system_prompt = persona["prompt"] if persona else None

        if not prompt:
            yield event.plain_result("请在命令后输入问题，例如：/test_llm 你好")
            return

        provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            yield event.plain_result("当前没有配置 LLM 提供者。")
            return

        response = await provider.text_chat(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=event.unified_msg_origin,
        )
        yield event.plain_result(response.completion_text)

#==============================================================
#                      Contact Commands
#==============================================================

    @filter.command("test_feishu_contact")
    async def test_feishu_contact(self, event: AstrMessageEvent):
        """测试飞书通讯录连接，列出根部门架构与成员角色。"""
        from lark_oapi.api.contact.v3 import ListDepartmentRequest, ListUserRequest

        platform = self.context.get_platform_inst(event.get_platform_id())
        lark_api = getattr(platform, "lark_api", None)
        if not lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return

        dept_req = (
            ListDepartmentRequest.builder()
            .parent_department_id("0")
            .page_size(50)
            .build()
        )
        dept_resp = await lark_api.contact.v3.department.alist(dept_req)
        if not dept_resp.success():
            yield event.plain_result(
                f"获取部门列表失败\ncode: {dept_resp.code}\nmsg: {dept_resp.msg}"
            )
            return

        departments = dept_resp.data.items or []
        lines = [f"══ 飞书通讯录（根部门共 {len(departments)} 个）══", ""]

        for dept in departments:
            dept_id = dept.department_id or ""
            dept_open_id = getattr(dept, "open_department_id", "") or ""
            member_count = getattr(dept, "member_count", "?")
            lines.append(f"📁 {dept.name}  (id: {dept_id}, 成员数: {member_count})")

            user_req = (
                ListUserRequest.builder()
                .department_id(dept_open_id)
                .user_id_type("open_id")
                .page_size(50)
                .build()
            )
            user_resp = await lark_api.contact.v3.user.alist(user_req)

            if not user_resp.success():
                lines.append(f"  └─ 获取成员失败: {user_resp.msg}")
                lines.append("")
                continue

            users = user_resp.data.items or []
            if not users:
                lines.append("  └─ （暂无成员）")
            else:
                for i, u in enumerate(users):
                    prefix = "  └─" if i == len(users) - 1 else "  ├─"
                    job_title = getattr(u, "job_title", "") or ""
                    job_level = getattr(u, "job_level_name", "") or ""
                    details = " | ".join([x for x in [job_title, job_level] if x])
                    line = f"{prefix} {u.name}"
                    if details:
                        line += f"  [{details}]"
                    lines.append(line)
            lines.append("")

        if not departments:
            lines.append("未找到任何部门，请确认应用通讯录权限已授权。")

        yield event.plain_result("\n".join(lines))

    @filter.command("list_contacts")
    async def list_contacts(self, event: AstrMessageEvent):
        """列出飞书通讯录中所有成员（扁平列表，不含部门树）。"""
        logger.info(f"CMD HIT | msg: {repr(event.message_str)} | is_wake: {event.is_wake}")

        from lark_oapi.api.contact.v3 import ListDepartmentRequest, ListUserRequest

        platform = self.context.get_platform_inst(event.get_platform_id())
        lark_api = getattr(platform, "lark_api", None)
        if not lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return

        dept_req = (
            ListDepartmentRequest.builder()
            .parent_department_id("0")
            .page_size(50)
            .build()
        )
        dept_resp = await lark_api.contact.v3.department.alist(dept_req)
        if not dept_resp.success():
            yield event.plain_result(
                f"获取部门列表失败\ncode: {dept_resp.code}\nmsg: {dept_resp.msg}"
            )
            return

        departments = dept_resp.data.items or []
        if not departments:
            yield event.plain_result("未找到任何部门，请确认应用通讯录权限已授权。")
            return

        seen = set()
        contacts = []
        errors = []

        for dept in departments:
            dept_open_id = getattr(dept, "open_department_id", "") or ""
            user_req = (
                ListUserRequest.builder()
                .department_id(dept_open_id)
                .user_id_type("open_id")
                .page_size(50)
                .build()
            )
            user_resp = await lark_api.contact.v3.user.alist(user_req)
            if not user_resp.success():
                errors.append(f"部门「{dept.name}」获取成员失败: code={user_resp.code} msg={user_resp.msg}")
                continue
            for u in user_resp.data.items or []:
                if u.open_id not in seen:
                    seen.add(u.open_id)
                    job_title = getattr(u, "job_title", "") or ""
                    contacts.append((u.name, job_title))

        if errors:
            logger.warning("list_contacts 部分部门获取失败:\n" + "\n".join(errors))

        if not contacts:
            diag = f"找到 {len(departments)} 个部门，但未能获取任何成员。"
            if errors:
                diag += "\n\n错误详情：\n" + "\n".join(errors)
            yield event.plain_result(diag)
            return

        lines = [f"══ 飞书通讯录成员（共 {len(contacts)} 人）══", ""]
        for i, (name, title) in enumerate(contacts, 1):
            line = f"{i}. {name}"
            if title:
                line += f"  [{title}]"
            lines.append(line)

        yield event.plain_result("\n".join(lines))

#==============================================================
#                       Persona Commands
#==============================================================

    @filter.command("test_persona")
    async def test_persona(self, event: AstrMessageEvent):
        """列出所有人格，并标注当前会话生效的人格。"""
        mgr = self.context.persona_manager
        active = await mgr.get_default_persona_v3(umo=event.unified_msg_origin)
        active_name = active["name"] if active else None

        all_personas = mgr.personas_v3
        if not all_personas:
            yield event.plain_result("当前没有任何人格配置。")
            return

        lines = [f"══ 所有人格（共 {len(all_personas)} 个）══", ""]
        for p in all_personas:
            marker = " ◀ 当前生效" if p["name"] == active_name else ""
            prompt_preview = p["prompt"][:80].replace("\n", " ")
            if len(p["prompt"]) > 80:
                prompt_preview += "..."
            lines += [
                f"【{p['name']}】{marker}",
                f"  prompt : {prompt_preview}",
                f"  tools  : {'（全部）' if p.get('tools') is None else (p.get('tools') or '（无）')}",
                f"  skills : {'（全部）' if p.get('skills') is None else (p.get('skills') or '（无）')}",
                "",
            ]
        yield event.plain_result("\n".join(lines))

    @filter.command("set_persona")
    async def set_persona(self, event: AstrMessageEvent):
        """为当前会话设置人格。用法：/set_persona <人格名称>"""
        name = event.message_str.removeprefix("/set_persona").strip()
        if not name:
            yield event.plain_result("请提供人格名称，例如：/set_persona WRSbot\n用 /test_persona 查看所有可用人格。")
            return

        mgr = self.context.persona_manager
        matched = next((p for p in mgr.personas_v3 if p["name"] == name), None)
        if not matched:
            names = ", ".join(p["name"] for p in mgr.personas_v3)
            yield event.plain_result(f"找不到人格「{name}」。\n可用人格：{names}")
            return

        session_config = await sp.get_async(
            scope="umo",
            scope_id=event.unified_msg_origin,
            key="session_service_config",
            default={},
        ) or {}
        session_config["persona_id"] = name
        await sp.put_async(
            scope="umo",
            scope_id=event.unified_msg_origin,
            key="session_service_config",
            value=session_config,
        )
        yield event.plain_result(f"✅ 当前会话人格已切换为「{name}」。")

#==============================================================
#                       Event Listeners
#==============================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        if "测试通讯录" not in text:
            return
        event.stop_event()
        yield event.plain_result("开始测试飞书通讯录...")

    CARD_KEYWORDS = ["周报", "帮助"]

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_card_reply(self, event: AstrMessageEvent):
        """检测关键词，回复飞书互动卡片。"""
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        text = event.message_str.strip()
        matched = next((kw for kw in self.CARD_KEYWORDS if kw in text), None)
        if not matched:
            return

        if not isinstance(event, LarkMessageEvent):
            yield event.plain_result(f"检测到关键词「{matched}」")
            return

        event.stop_event()

        from lark_oapi.api.contact.v3 import GetUserRequest
        platform = self.context.get_platform_inst(event.get_platform_id())
        lark_api = getattr(platform, "lark_api", None)
        sender_name = event.get_sender_id()
        if lark_api:
            try:
                u_req = (
                    GetUserRequest.builder()
                    .user_id(event.get_sender_id())
                    .user_id_type("open_id")
                    .build()
                )
                u_resp = await lark_api.contact.v3.user.aget(u_req)
                if u_resp.success() and u_resp.data and u_resp.data.user:
                    sender_name = u_resp.data.user.name or sender_name
            except Exception:
                pass

        await self.card_service.send_keyword_card(
            sender_name, matched, text, event.message_obj.message_id
        )
        event._has_send_oper = True

#==============================================================
#                        Doc Commands
#==============================================================

    @filter.command("set_doc_folder")
    async def set_doc_folder(self, event: AstrMessageEvent):
        """保存飞书文件夹 token 供后续命令使用。用法：/set_doc_folder <folder_token>"""
        token = event.message_str.removeprefix("set_doc_folder").strip().split("?")[0].strip()
        if not token:
            saved = await sp.get_async(scope="global", scope_id="wrsbot", key="doc_folder_token", default=None)
            if saved:
                yield event.plain_result(f"当前已保存的文件夹 token：{saved}\n如需更新，请运行 /set_doc_folder <新token>")
            else:
                yield event.plain_result("尚未设置文件夹 token。\n用法：/set_doc_folder <folder_token>")
            return
        await sp.put_async(scope="global", scope_id="wrsbot", key="doc_folder_token", value=token)
        yield event.plain_result(f"✅ 文件夹 token 已保存：{token}\n今后直接运行 /test_feishu_doc 即可。")

    @filter.command("test_feishu_doc")
    async def test_feishu_doc(self, event: AstrMessageEvent):
        """测试飞书文档权限：列举文件（访问）、读取内容。
        用法：/test_feishu_doc [folder_token]"""

        inline_token = event.message_str.removeprefix("test_feishu_doc").strip().split("?")[0].strip()
        folder_token = inline_token or await sp.get_async(scope="global", scope_id="wrsbot", key="doc_folder_token", default="") or ""

        platform = self.context.get_platform_inst(event.get_platform_id())
        lark_api = getattr(platform, "lark_api", None)
        if not lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return

        scope_label = f"指定文件夹 (token: {folder_token})" if folder_token else "机器人云空间根目录"
        lines = ["══ 飞书文档访问测试 ══", f"  目标: {scope_label}", ""]

        lines.append("【1】访问测试 — 列举目标文件夹内容")
        doc_token = None
        try:
            list_req = (
                ListFileRequest.builder()
                .folder_token(folder_token)
                .page_size(10)
                .build()
            )
            list_resp = await lark_api.drive.v1.file.alist(list_req)
            if list_resp.success():
                files = list_resp.data.files or []
                lines.append(f"  ✅ 成功，共 {len(files)} 个条目")
                for f in files:
                    lines.append(f"  - [{f.type}] {f.name}  (token: {f.token})")
                    if f.type == "docx" and doc_token is None:
                        doc_token = f.token
            else:
                lines.append(f"  ❌ 失败: code={list_resp.code}  msg={list_resp.msg}")
        except Exception as e:
            lines.append(f"  ❌ 异常: {e}")

        lines.append("")
        lines.append("【2】读取测试 — 读取文档原始内容")
        if doc_token:
            try:
                read_req = (
                    RawContentDocumentRequest.builder()
                    .document_id(doc_token)
                    .build()
                )
                read_resp = await lark_api.docx.v1.document.araw_content(read_req)
                if read_resp.success():
                    content = (read_resp.data.content or "").replace("\n", " ")
                    preview = content[:120] + ("..." if len(content) > 120 else "")
                    lines.append(f"  ✅ 成功，内容预览: {preview}")
                else:
                    lines.append(f"  ❌ 失败: code={read_resp.code}  msg={read_resp.msg}")
            except Exception as e:
                lines.append(f"  ❌ 异常: {e}")
        else:
            lines.append("  ⚠️ 跳过：未找到 docx 文件")

        yield event.plain_result("\n".join(lines))

#==============================================================
#                          Lifecycle
#==============================================================

    async def terminate(self):
        pass
