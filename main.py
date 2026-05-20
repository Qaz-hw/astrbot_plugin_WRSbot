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

import json
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, sp
from astrbot.api.platform import MessageType

from .services.lark_card import LarkCardService
from .services.doc import DocService
from .services.drive import DriveService
from .services.bitable import BitableService
from .services.contact import ContactService
from .utils.env_config import dump_dept_bindings

# todo：add 支持配置“个人风格特征词”，让输出结果尽可能模仿管理者的撰写口吻。function
# todo: update commands for user & manager's better workflow e.g. start with user_view/manager_view card
# todo: completely formulate, strucutre the code, delete redundent files/ unused functions and add more comments
# todo: action view_doc send user straight to department weekly report folder

@register("helloworld", "YourName", "一个简单的 Hello World 插件", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context: Context = context
        self.lark_api = None
        self.card_service: LarkCardService = None
        self.doc_service: DocService = None
        self.drive_service: DriveService = None
        self.bitable_service: BitableService | None = None
        self.contact_service: ContactService = None

    async def initialize(self):
        """Grab lark_api from the platform adapter and set up services."""
        get_insts = getattr(self.context.platform_manager, "get_insts", None)
        if get_insts:
            for platform in get_insts():
                if hasattr(platform, "lark_api") and self.lark_api is None:
                    self.lark_api = platform.lark_api

        self.card_service = LarkCardService(self.lark_api)
        self.card_service.inject_into_dispatcher(self.context.platform_manager)
        self.doc_service = DocService(self.lark_api)
        self.drive_service = DriveService(self.lark_api)
        self.bitable_service = BitableService(self.lark_api)
        self.contact_service = ContactService(self.lark_api)
        await self.contact_service.start_cache()
        self.card_service.contact_service = self.contact_service
        self.card_service.set_admin_pipeline(self._admin_view_pipeline)
        self.card_service.set_user_pipeline(self._user_view_pipeline)
        self.card_service.set_report_pipelines(
            generate=self._generate_pipeline,
            rewrite=self._rewrite_pipeline,
            submit=self._submit_pipeline,
        )
        self.card_service.set_reminder_pipeline(self._reminder_pipeline)

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
    async def cmd_send_testing_welcome(self, event: AstrMessageEvent):
        """向发送消息的用户发送欢迎卡片。"""
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        if not isinstance(event, LarkMessageEvent):
            yield event.plain_result("此命令仅支持飞书平台。")
            return

        await self.card_service.send_welcome_card(event.get_sender_id())
        event._has_send_oper = True


    @filter.command("Hello")
    async def cmd_send_wrs_welcome(self, event: AstrMessageEvent):
        """Send Welcome lark card to users"""
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        if not isinstance(event, LarkMessageEvent):
            yield event.plain_result("This command is only abled on Lark/Feishu")
            return

        await self.card_service.send_wrsbot_welcome(event.get_sender_id())
        event._has_send_oper = True

    @filter.command("文件夹配置")
    async def cmd_folder_config(self, event: AstrMessageEvent):
        """检查当前用户的部门文件夹绑定状态，发送对应的反馈卡片。

        - 全部部门已绑定 → send_binding_success_card
        - 否则（含非管理员、未绑定、部分未绑定）→ send_not_binding_card
        """
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        if not isinstance(event, LarkMessageEvent):
            yield event.plain_result("此命令仅支持飞书平台。")
            return

        open_id = event.get_sender_id()
        if self.card_service.is_fully_bound(open_id):
            await self.card_service.send_binding_success_card(open_id)
        else:
            await self.card_service.send_not_binding_card(open_id)
        event._has_send_oper = True

    @filter.command("清除所有绑定")
    async def cmd_clear_all_bindings(self, event: AstrMessageEvent):
        """清除 .env 中所有 DEPT_FOLDER_* 文件夹绑定（仅用于开发测试）。"""
        from .utils.env_config import clear_all_dept_bindings

        n = clear_all_dept_bindings()
        logger.warning(f"[Bindings] 已清除 {n} 条 DEPT_FOLDER_* 绑定 (sender={event.get_sender_id()})")
        yield event.plain_result(f"✅ 已清除 {n} 条文件夹绑定。")

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

    @filter.event_message_type(filter.EventMessageType.ALL)
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
        """[管理员] 实时查询飞书通讯录，直接调用 API。"""
        if not self.lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return
        try:
            org_tree = await self.contact_service.get_org_tree()
        except Exception as e:
            yield event.plain_result(f"获取部门列表失败: {e}")
            return
        yield event.plain_result(
            self.contact_service.format_org_tree_dump(org_tree, source="实时")
        )

    @filter.command("test_cached_feishu_contact")
    async def test_cached_feishu_contact(self, event: AstrMessageEvent):
        """[Admin] 查看当前内存缓存中的部门树（不发起 API 请求）。"""
        if not self.lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return
        try:
            org_tree = await self.contact_service.get_cached_org_tree()
        except Exception as e:
            yield event.plain_result(f"获取缓存失败: {e}")
            return
        yield event.plain_result(
            self.contact_service.format_org_tree_dump(org_tree, source="缓存")
        )

    @filter.command("dump_bindings")
    async def dump_bindings(self, event: AstrMessageEvent):
        """[Developer] 显示所有部门文件夹绑定状态，与缓存通讯录交叉校验。"""
        try:
            org_tree = await self.contact_service.get_cached_org_tree()
        except Exception as e:
            yield event.plain_result(f"获取缓存失败: {e}")
            return
        yield event.plain_result(dump_dept_bindings(org_tree))

    @filter.command("check_user")
    async def cmd_check_user(self, event: AstrMessageEvent):
        """转储当前发送者的飞书通讯录用户档案。"""
        if not self.lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return
        yield event.plain_result(
            await self.contact_service.get_user_profile_dump(event.get_sender_id())
        )

    @filter.command("list_contacts")
    async def list_contacts(self, event: AstrMessageEvent):
        """列出飞书通讯录中所有成员（扁平列表，不含部门树）。"""
        logger.info(f"CMD HIT | msg: {repr(event.message_str)} | is_wake: {event.is_wake}")

        if not self.lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return

        try:
            contacts, errors = await self.contact_service.list_all_members()
        except Exception as e:
            yield event.plain_result(f"获取部门列表失败: {e}")
            return

        if errors:
            logger.warning("list_contacts 部分部门获取失败:\n" + "\n".join(errors))

        if not contacts:
            diag = "未能获取任何成员。"
            if errors:
                diag += "\n\n错误详情：\n" + "\n".join(errors)
            yield event.plain_result(diag)
            return

        lines = [f"══ 飞书通讯录成员（共 {len(contacts)} 人）══", ""]
        for i, c in enumerate(contacts, 1):
            line = f"{i}. {c['name']}"
            if c["job_title"]:
                line += f"  [{c['job_title']}]"
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

    # Must mirror all @filter.command handlers above.
    # Group @mentions bypass AstrBot's command router and reach the LLM directly —
    # this dispatcher intercepts /commands before that happens.
    _GROUP_COMMANDS: dict = {}

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def group_command_dispatcher(self, event: AstrMessageEvent):
        """Intercept commands in group chat before the LLM pipeline runs."""
        text = event.message_str
        text = re.sub(r"\[At:[^\]]*\]", "", text)  # [At:ou_xxx] format
        text = re.sub(r"@\S+", "", text)            # @WRSbot format
        text = text.strip()

        logger.debug(f"[Dispatcher] raw={repr(event.message_str)} stripped={repr(text)}")

        if not text:
            return

        cmd = text.lstrip("/").split()[0]

        if not self._GROUP_COMMANDS:
            self._GROUP_COMMANDS = {
                "whoami":                     self.cmd_whoami,
                "关于WRSbot":                 self.about_wrsbot,
                "helloworld":                 self.helloworld,
                "发起告警":                   self.cmd_send_alarm,
                "你好":                       self.cmd_send_testing_welcome,
                "Hello":                      self.cmd_send_wrs_welcome,
                "check_user":                 self.cmd_check_user,
                "test_feishu_contact":        self.test_feishu_contact,
                "test_cached_feishu_contact": self.test_cached_feishu_contact,
                "list_contacts":              self.list_contacts,
                "set_doc_folder":             self.set_doc_folder,
                "test_feishu_doc":            self.test_feishu_doc,
                "test_weekly_file":           self.test_weekly_file,
                "创建周报总结":               self.create_weekly_report,
                "test_persona":               self.test_persona,
                "set_persona":                self.set_persona,
                "test_llm":                   self.test_llm,
                "dump_bindings":              self.dump_bindings,
            }

        handler = self._GROUP_COMMANDS.get(cmd)
        if handler is None:
            return  # unknown /command — let LLM handle naturally

        event.stop_event()
        async for result in handler(event):
            yield result

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

        sender_name = event.get_sender_id()
        try:
            user = await self.contact_service.get_user(event.get_sender_id())
            if user:
                sender_name = user.name or sender_name
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

    async def _user_view_pipeline(self, open_id: str, message_id: str = "") -> None:
        """Async pipeline for the user view card.

        Finds this week's file → runs submission check → checks if this
        user's name is in the submitted list → sends user view card as DM.
        """
        from .services.report import check_submissions
        from .utils.env_config import get_dept_folder_token
        from datetime import datetime, timedelta

        try:
            org_tree = await self.contact_service.get_cached_org_tree()

            # Find which dept this user belongs to
            dept_entry = None
            for entry in org_tree:
                if any(getattr(m, "open_id", None) == open_id for m in entry.get("members", [])):
                    dept_entry = entry
                    break

            if not dept_entry:
                logger.warning(f"[UserView] 未找到所属部门: open_id={open_id}")
                return

            dept      = dept_entry["dept"]
            dept_name = dept.name or ""
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            folder_token = get_dept_folder_token(open_dept_id)
            if not folder_token:
                logger.warning(f"[UserView] 文件夹未绑定: dept={dept_name}")
                return

            # Find this week's file/table
            files = await self.drive_service.list_folder_files(folder_token)
            weekly_file: dict | None = None

            bitable_files = [f for f in files if f["type"] == "bitable"]
            if bitable_files and self.bitable_service:
                today  = datetime.now().date()
                monday = today - timedelta(days=today.weekday())
                year, week, _ = monday.isocalendar()
                iso_tag = f"{year}-W{week:02d}"
                tables  = await self.bitable_service.list_tables(bitable_files[0]["token"])
                matched = next((t for t in tables if iso_tag in t["name"]), None)
                if matched:
                    weekly_file = {
                        **bitable_files[0],
                        "table_id":   matched["table_id"],
                        "table_name": matched["name"],
                    }

            if not weekly_file:
                weekly_file = await self.drive_service.find_this_week_file(
                    folder_token, llm_fn=self._make_drive_llm_fn(open_id)
                )

                if not weekly_file:
                    logger.warning(f"[UserView] 未找到本周文件: folder={folder_token}")
                    return

            if weekly_file.get("type") == "bitable" and not self.bitable_service:
                logger.warning("[UserView] bitable_service 不存在，无法处理多维表格")
                return
        
            provider = self.context.get_using_provider(umo=f"lark:open_id:{open_id}")
            if not provider:
                logger.warning(f"[UserView] 未找到 LLM provider: open_id={open_id}")
                return

            result = await check_submissions(
                weekly_file=weekly_file,
                sender_id=open_id,
                contact_service=self.contact_service,
                doc_service=self.doc_service,
                bitable_service=self.bitable_service,
                llm_provider=provider,
                session_id=f"wrsbot_user:{open_id}",
            )

            if not result["ok"]:
                logger.warning(f"[UserView] 提交检查失败: {result['error']}")
                return

            # Check if this user's name appears in the submitted list
            user_name    = next(
                (m.name for m in dept_entry.get("members", []) if getattr(m, "open_id", None) == open_id),
                "",
            )
            is_submitted = user_name in result["submitted"]

            await self.card_service.send_user_view_card(open_id, dept_name, is_submitted, message_id)

        except Exception as e:
            logger.error(f"[UserView] 用户视图加载失败: {e}")

    async def _generate_pipeline(self, open_id: str, message_id: str = "") -> None:
        """Read this week's reports → LLM summarize → LLM rewrite → send report card + action card."""
        if not self.lark_api:
            return
        from .utils.env_config import get_dept_folder_token
        from .services.report import (
            generate_report_stream,
            check_submissions,
        )
        from datetime import datetime, timedelta

        try:
            # ── Resolve folder ────────────────────────────────────────────────
            org_tree = await self.contact_service.get_cached_org_tree()
            managed = [e for e in org_tree if e.get("manager") and e["manager"].open_id == open_id]
            if not managed:
                logger.warning(f"[Generate] 未找到管理的部门: open_id={open_id}")
                return
            dept         = managed[0]["dept"]
            dept_name    = dept.name or ""
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            folder_token = get_dept_folder_token(open_dept_id)
            if not folder_token:
                logger.warning(f"[Generate] 文件夹未绑定: dept={dept_name}")
                return

            # ── Find this week's file ─────────────────────────────────────────
            weekly_file = await self._find_weekly_source(folder_token, open_id)
            if not weekly_file:
                logger.warning(f"[Generate] 未找到本周文件: folder={folder_token}")
                return

            if not self.bitable_service:
                return
            provider = self.context.get_using_provider(umo=f"lark:open_id:{open_id}")
            if not provider:
                return

            # ── Read content ──────────────────────────────────────────────────
            today  = datetime.now().date()
            monday = today - timedelta(days=today.weekday())
            year, week, _ = monday.isocalendar()
            iso_tag = f"{year}-W{week:02d}"

            if weekly_file["type"] == "bitable":
                records = await self.bitable_service.list_records(
                    weekly_file["token"], weekly_file.get("table_id", "")
                )
                content = BitableService.records_to_text(records)
            else:
                content = await self.doc_service.read_doc_blocks(weekly_file["token"])

            if not content.strip():
                logger.warning("[Generate] 文件内容为空，无法生成总结")
                return

            # ── Submission status (so the LLM can flag missing members) ──────
            # Manager can trigger generation even if not everyone submitted —
            # we pass the dept's submitted / not_submitted lists to both passes
            # so the model prepends a 「说明」 section when applicable.
            submitted: list[str] = []
            not_submitted: list[str] = []
            try:
                check_result = await check_submissions(
                    weekly_file=weekly_file,
                    sender_id=open_id,
                    contact_service=self.contact_service,
                    doc_service=self.doc_service,
                    bitable_service=self.bitable_service,
                    llm_provider=provider,
                    session_id=f"wrsbot_gen_check:{open_id}",
                )
                if check_result.get("ok"):
                    submitted     = check_result.get("submitted", [])
                    not_submitted = check_result.get("not_submitted", [])
                    logger.info(
                        f"[Generate] 提交情况: {len(submitted)}/{check_result.get('total', 0)} "
                        f"未提交={not_submitted}"
                    )
                else:
                    logger.warning(f"[Generate] 提交检查未成功: {check_result.get('error')}")
            except Exception as e:
                logger.warning(f"[Generate] 提交检查异常（继续生成）: {e}")

            # ── LLM generate (single pass, streamed into one CardKit card) ───
            # Combined fact-extraction + executive-style write in one LLM call.
            # Replaces the previous summarize → rewrite two-card flow that
            # produced near-identical 草稿 / 总结 cards on most inputs.
            session_id = f"wrsbot_generate:{open_id}"
            final = await self._stream_to_card(
                open_id,
                generate_report_stream(
                    content, dept_name, iso_tag, provider, session_id,
                    submitted=submitted, not_submitted=not_submitted,
                ),
                header_title=f"{dept_name} · {iso_tag} · 周报总结",
                fallback_subtitle="周报总结",
            )

            # ── Persist draft ─────────────────────────────────────────────────
            # Keep not_submitted alongside the draft so the rewrite pipeline
            # can pass it back to the LLM to preserve the 「说明」 section.
            await sp.put_async(
                scope="global", scope_id="wrsbot",
                key=f"draft:{open_id}",
                value={
                    "text":          final,
                    "weekly_file":   weekly_file,
                    "dept_name":     dept_name,
                    "iso_week":      iso_tag,
                    "not_submitted": not_submitted,
                },
            )

            # ── Deliver action card ───────────────────────────────────────────
            # Patch the in-place generating card → action card (rewrite/submit).
            # Fall back to a DM only if there's no message_id to patch.
            if message_id:
                await self.card_service.send_generated_success_card(message_id)
            else:
                await self.card_service.send_summary_action_card(open_id)
            logger.info(f"[Generate] 周报总结生成完成: dept={dept_name}")

        except Exception as e:
            logger.error(f"[Generate] 周报总结生成失败: {e}")

    async def _rewrite_pipeline(self, open_id: str, style_input: str) -> None:
        """Rewrite the stored draft with optional style instructions, then resend."""
        if not self.lark_api:
            return
        from .services.report import rewrite_summary_stream

        logger.warning(f"[Rewrite] pipeline start open_id={open_id} style_input={style_input!r}")

        try:
            stored = await sp.get_async(
                scope="global", scope_id="wrsbot", key=f"draft:{open_id}", default=None
            )
            if not stored:
                logger.warning(f"[Rewrite] 未找到草稿: open_id={open_id}")
                return

            provider = self.context.get_using_provider(umo=f"lark:open_id:{open_id}")
            if not provider:
                return

            dept_name     = stored.get("dept_name", "")
            iso_week      = stored.get("iso_week", "")
            not_submitted = stored.get("not_submitted", []) or []
            final = await self._stream_to_card(
                open_id,
                rewrite_summary_stream(
                    stored["text"], provider,
                    session_id=f"wrsbot_rewrite:{open_id}",
                    style_input=style_input,
                    not_submitted=not_submitted,
                ),
                header_title=f"{dept_name} · {iso_week} · 周报总结",
                fallback_subtitle="周报总结",
            )

            await sp.put_async(
                scope="global", scope_id="wrsbot",
                key=f"draft:{open_id}",
                value={**stored, "text": final},
            )

            await self.card_service.send_summary_action_card(open_id)
            logger.info(f"[Rewrite] 改写完成: open_id={open_id}")

        except Exception as e:
            logger.error(f"[Rewrite] 改写失败: {e}")

    async def _stream_to_card(
        self,
        open_id: str,
        text_stream,
        header_title: str = "",
        fallback_subtitle: str = "",
    ) -> str:
        """Drive a cumulative-text async generator into a Feishu streaming card.

        text_stream yields full snapshots (not deltas) — the CardKit content
        endpoint expects the full accumulated text each tick. A decoupled
        sender loop pushes whenever the snapshot changes; RTT acts as natural
        rate-limiting so we don't hammer the API per token.

        Falls back to a plain Feishu post message if CardKit is unavailable
        or card delivery fails, so the user always receives the report.

        Returns the final accumulated text.

        Note: AstrBot ships LarkMessageEvent.send_streaming with similar
        decoupled-sender logic, but it's instance-bound (needs message_obj,
        platform_meta, reply context) and expects a MessageChain generator.
        Our card-callback pipeline has none of those, so we own the loop.
        See services/lark_card.py CardKit helpers for the full rationale.
        """
        import asyncio

        # Try CardKit first
        card_id = await self.card_service.create_streaming_card(header_title)
        sent = False
        if card_id:
            sent = await self.card_service.send_streaming_card_to_user(open_id, card_id)

        if not card_id or not sent:
            # Fallback: buffer everything, send as one post message at the end
            logger.warning("[Stream] CardKit 不可用，回退为普通 post 消息")
            final = ""
            async for snapshot in text_stream:
                final = snapshot
            if final:
                await self._send_report_message(open_id, final, dept_name=fallback_subtitle)
            return final

        snapshot = ""
        last_sent = ""
        sequence = 0
        done = False
        changed = asyncio.Event()

        async def sender_loop() -> None:
            nonlocal sequence, last_sent
            while not done:
                await changed.wait()
                changed.clear()
                current = snapshot
                if current and current != last_sent:
                    sequence += 1
                    ok = await self.card_service.update_streaming_text(card_id, current, sequence)
                    if ok:
                        last_sent = current
                    if snapshot != current:
                        changed.set()

        sender_task = asyncio.create_task(sender_loop())

        try:
            async for snap in text_stream:
                snapshot = snap
                changed.set()
        finally:
            done = True
            changed.set()
            try:
                await sender_task
            except Exception as e:
                logger.warning(f"[Stream] sender_loop 异常: {e}")

            # Final flush — make sure the closing snapshot is delivered
            if snapshot and snapshot != last_sent:
                sequence += 1
                await self.card_service.update_streaming_text(card_id, snapshot, sequence)
            sequence += 1
            await self.card_service.close_streaming_card(card_id, sequence)

        return snapshot

    async def _send_report_message(
        self,
        open_id: str,
        report_text: str,
        dept_name: str = "",
        iso_week: str = "",
    ) -> None:
        """Send the LLM-generated report as a Feishu post message.

        Uses msg_type="post" with a single `md` segment so the markdown
        (headers, bullets, bold) renders natively — the same path AstrBot
        uses for normal LLM replies. dept_name + iso_week become the post
        title for context, separate from the model output.
        """
        if not self.lark_api:
            return
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        title = " · ".join(p for p in (dept_name, iso_week, "周报总结") if p)
        content = {
            "zh_cn": {
                "title": title,
                "content": [[{"tag": "md", "text": report_text}]],
            }
        }
        await LarkMessageEvent._send_im_message(
            self.lark_api,
            content=json.dumps(content, ensure_ascii=False),
            msg_type="post",
            receive_id=open_id,
            receive_id_type="open_id",
        )

    async def _submit_pipeline(self, open_id: str) -> None:
        """Write the stored draft to bitable 部门总结 row."""
        if not self.lark_api:
            return
        from astrbot.api import sp
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        try:
            stored = await sp.get_async(
                scope="global", scope_id="wrsbot", key=f"draft:{open_id}", default=None
            )
            if not stored:
                logger.warning(f"[Submit] 未找到草稿: open_id={open_id}")
                return

            weekly_file = stored.get("weekly_file", {})
            final_text  = stored.get("text", "")
            dept_name   = stored.get("dept_name", "")

            file_type = weekly_file.get("type", "")
            target_desc = ""  # filled below for the success DM

            if file_type == "bitable":
                if not self.bitable_service:
                    logger.warning("[Submit] bitable_service 未就绪")
                    return
                from .services.report import split_report_to_columns, _md_to_plain

                app_token = weekly_file["token"]
                table_id  = weekly_file.get("table_id", "")

                # ── Discover the table's column schema ──────────────────────
                column_names: list[str] = []
                try:
                    fields = await self.bitable_service.list_fields(app_token, table_id)
                    column_names = [f["field_name"] for f in fields if f.get("field_name")]
                    logger.info(f"[Submit] 表格列: {column_names}")
                except Exception as e:
                    logger.warning(f"[Submit] 读取列定义失败（继续单列写入）: {e}")

                # ── Ask the LLM to map sections → columns ───────────────────
                fields_map: dict[str, str] = {}
                provider = self.context.get_using_provider(umo=f"lark:open_id:{open_id}")
                if column_names and provider:
                    fields_map = await split_report_to_columns(
                        final_text, column_names, provider,
                        session_id=f"wrsbot_split:{open_id}",
                    )
                    logger.info(f"[Submit] LLM 列分配: {list(fields_map.keys())}")

                # Fallback when split failed: dump cleaned text into 本周进展.
                if not fields_map:
                    fields_map = {"本周进展": _md_to_plain(final_text)}

                # Always anchor the row with 姓名 = 部门总结 so find_summary_record
                # picks it up next time.
                fields_map["姓名"] = "部门总结"

                record_id = await self.bitable_service.find_summary_record(app_token, table_id)
                if record_id:
                    await self.bitable_service.update_record(
                        app_token, table_id, record_id, fields_map
                    )
                else:
                    record_id = await self.bitable_service.create_record(
                        app_token, table_id, fields_map,
                    )
                    logger.info(f"[Submit] 已创建部门总结行: record_id={record_id}")
                target_desc = "本周多维表格「部门总结」行"

            elif file_type in ("docx", "doc"):
                if not self.doc_service:
                    logger.warning("[Submit] doc_service 未就绪")
                    return
                # Prepend a heading so the appended section is visually anchored
                # at the doc's tail. Re-submitting will append another section —
                # caller is responsible for any dedupe.
                doc_id = weekly_file["token"]
                heading = f"#### 部门总结（{dept_name}）" if dept_name else "#### 部门总结"
                report_with_heading = f"{heading}\n{final_text}"
                n_blocks = await self.doc_service.append_report(doc_id, report_with_heading)
                logger.info(f"[Submit] 已追加 {n_blocks} 个块到 Doc: doc_id={doc_id}")
                target_desc = "本周飞书文档末尾"

            else:
                logger.warning(f"[Submit] 不支持的文件类型: {file_type!r}")
                return

            await LarkMessageEvent._send_im_message(
                self.lark_api,
                content=json.dumps(
                    {"text": f"✅ 周报总结已写入【{dept_name}】{target_desc}。"},
                    ensure_ascii=False,
                ),
                msg_type="text",
                receive_id=open_id,
                receive_id_type="open_id",
            )
            logger.info(f"[Submit] 写入完成: dept={dept_name}")

        except Exception as e:
            logger.error(f"[Submit] 写入失败: {e}")

    async def _find_weekly_source(self, folder_token: str, open_id: str = "") -> "dict | None":
        """Shared helper — bitable table-match first, doc name-match (with LLM fallback) second.

        open_id: when provided, the drive fallback uses an LLM picker to
        disambiguate filenames that don't match the ISO-week / date-range
        regex patterns (e.g. "2026周报week21" wouldn't match the rules).
        """
        from datetime import datetime, timedelta
        today  = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        year, week, _ = monday.isocalendar()
        iso_tag = f"{year}-W{week:02d}"

        files = await self.drive_service.list_folder_files(folder_token)

        bitable_files = [f for f in files if f["type"] == "bitable"]
        if bitable_files and self.bitable_service:
            tables  = await self.bitable_service.list_tables(bitable_files[0]["token"])
            matched = next((t for t in tables if iso_tag in t["name"]), None)
            if matched:
                return {
                    **bitable_files[0],
                    "table_id":   matched["table_id"],
                    "table_name": matched["name"],
                }

        llm_fn = self._make_drive_llm_fn(open_id) if open_id else None
        return await self.drive_service.find_this_week_file(folder_token, llm_fn=llm_fn)

    def _make_drive_llm_fn(self, open_id: str):
        """Return an async callable for drive.find_this_week_file's llm_fn.

        Picks the most-likely-current-week file from a name list using the
        user's configured LLM provider. Mirrors _WeeklyFileTestRunner._make_llm_fn
        but bound to open_id since card-callback pipelines have no event context.
        """
        async def _llm_pick(names: list[str]) -> str | None:
            from datetime import datetime as _dt
            today = _dt.now()
            _, week, _ = today.isocalendar()
            umo = f"lark:open_id:{open_id}"
            provider = self.context.get_using_provider(umo=umo)
            if not provider:
                return None
            prompt = (
                f"今天是 {today.strftime('%Y-%m-%d')}（第 {week} 周）。"
                f"以下是飞书文件夹中的文件名列表：\n"
                + "\n".join(f"- {n}" for n in names)
                + "\n\n哪个文件最可能是本周的周报？请只回复文件名，不要添加其他内容。如果都不像，请回复 none。"
            )
            try:
                resp = await provider.text_chat(prompt=prompt, session_id=umo)
            except Exception as e:
                logger.warning(f"[Drive LLM] 调用失败: {e}")
                return None
            chosen = resp.completion_text.strip().strip('"').strip("'")
            return chosen if chosen.lower() != "none" else None

        return _llm_pick

    async def _admin_view_pipeline(self, open_id: str, message_id: str = "") -> None:
        """Async pipeline triggered by wrsbot_start card action for admin users.

        Resolves the manager's dept folder → finds this week's file/table →
        runs submission check → sends the admin view card as a DM.
        Runs via asyncio.create_task() so the sync card handler returns immediately.
        """
        from .services.report import check_submissions
        from .utils.env_config import get_dept_folder_token
        from datetime import datetime, timedelta

        try:
            org_tree = await self.contact_service.get_cached_org_tree()
            managed = [
                e for e in org_tree
                if e.get("manager") and e["manager"].open_id == open_id
            ]
            if not managed:
                logger.warning(f"[AdminView] 未找到管理的部门: open_id={open_id}")
                return

            dept = managed[0]["dept"]
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            folder_token = get_dept_folder_token(open_dept_id)
            if not folder_token:
                logger.warning(f"[AdminView] 文件夹未绑定: dept={dept.name}")
                return

            # ── Find this week's file/table ──────────────────────────────────
            files = await self.drive_service.list_folder_files(folder_token)
            weekly_file: dict | None = None

            bitable_files = [f for f in files if f["type"] == "bitable"]
            if bitable_files and self.bitable_service:
                today = datetime.now().date()
                monday = today - timedelta(days=today.weekday())
                year, week, _ = monday.isocalendar()
                iso_tag = f"{year}-W{week:02d}"
                tables = await self.bitable_service.list_tables(bitable_files[0]["token"])
                matched = next((t for t in tables if iso_tag in t["name"]), None)
                if matched:
                    weekly_file = {
                        **bitable_files[0],
                        "table_id":   matched["table_id"],
                        "table_name": matched["name"],
                    }

            if not weekly_file:
                weekly_file = await self.drive_service.find_this_week_file(
                    folder_token, llm_fn=self._make_drive_llm_fn(open_id)
                )

                if not weekly_file:
                    logger.warning(f"[AdminView] 未找到本周文件: folder={folder_token}")
                    return

            # ── Submission check ─────────────────────────────────────────────
            if weekly_file.get("type") == "bitable" and not self.bitable_service:
                logger.warning("[UserView] bitable_service 不存在，无法处理多维表格")
                return
        
            provider = self.context.get_using_provider(umo=f"lark:open_id:{open_id}")
            if not provider:
                logger.warning(f"[UserView] 未找到 LLM provider: open_id={open_id}")
                return
            if not provider:
                logger.warning(f"[AdminView] 无可用 LLM 提供者")
                return

            result = await check_submissions(
                weekly_file=weekly_file,
                sender_id=open_id,
                contact_service=self.contact_service,
                doc_service=self.doc_service,
                bitable_service=self.bitable_service,
                llm_provider=provider,
                session_id=f"wrsbot_admin:{open_id}",
            )

            if not result["ok"]:
                logger.warning(f"[AdminView] 提交检查失败: {result['error']}")
                return

            await self.card_service.send_admin_view_card(open_id, result, message_id)

        except Exception as e:
            logger.error(f"[AdminView] 管理员视图加载失败: {e}")

    async def _reminder_pipeline(self, open_id: str) -> None:
        """DM the user_view_card to every member who hasn't submitted yet.

        Mirrors the discovery half of _admin_view_pipeline (folder → weekly
        file → submission check), then fans out user_view_card sends to each
        name in not_submitted. Names are mapped to open_ids via the members
        list returned by check_submissions.
        """
        from .services.report import check_submissions
        from .utils.env_config import get_dept_folder_token
        from datetime import datetime, timedelta

        try:
            org_tree = await self.contact_service.get_cached_org_tree()
            managed = [
                e for e in org_tree
                if e.get("manager") and e["manager"].open_id == open_id
            ]
            if not managed:
                logger.warning(f"[Reminder] 未找到管理的部门: open_id={open_id}")
                return

            dept = managed[0]["dept"]
            dept_name = dept.name or ""
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            folder_token = get_dept_folder_token(open_dept_id)
            if not folder_token:
                logger.warning(f"[Reminder] 文件夹未绑定: dept={dept_name}")
                return

            # ── Find this week's file/table (same flow as admin pipeline) ────
            files = await self.drive_service.list_folder_files(folder_token)
            weekly_file: dict | None = None

            bitable_files = [f for f in files if f["type"] == "bitable"]
            if bitable_files and self.bitable_service:
                today = datetime.now().date()
                monday = today - timedelta(days=today.weekday())
                year, week, _ = monday.isocalendar()
                iso_tag = f"{year}-W{week:02d}"
                tables = await self.bitable_service.list_tables(bitable_files[0]["token"])
                matched = next((t for t in tables if iso_tag in t["name"]), None)
                if matched:
                    weekly_file = {
                        **bitable_files[0],
                        "table_id":   matched["table_id"],
                        "table_name": matched["name"],
                    }

            if not weekly_file:
                weekly_file = await self.drive_service.find_this_week_file(
                    folder_token, llm_fn=self._make_drive_llm_fn(open_id)
                )
                if not weekly_file:
                    logger.warning(f"[Reminder] 未找到本周文件: folder={folder_token}")
                    return

            provider = self.context.get_using_provider(umo=f"lark:open_id:{open_id}")
            if not provider:
                logger.warning(f"[Reminder] 未找到 LLM provider: open_id={open_id}")
                return

            result = await check_submissions(
                weekly_file=weekly_file,
                sender_id=open_id,
                contact_service=self.contact_service,
                doc_service=self.doc_service,
                bitable_service=self.bitable_service,
                llm_provider=provider,
                session_id=f"wrsbot_reminder:{open_id}",
            )

            if not result["ok"]:
                logger.warning(f"[Reminder] 提交检查失败: {result['error']}")
                return

            not_submitted = set(result.get("not_submitted", []))
            if not not_submitted:
                logger.info(f"[Reminder] 全员已提交，无需提醒: dept={dept_name}")
                return

            # Map names → open_ids via the members list (check_submissions only
            # returns names in submitted/not_submitted; open_ids live in members).
            sent = 0
            for m in result.get("members", []):
                if m.get("name") in not_submitted and m.get("open_id"):
                    await self.card_service.send_user_view_card(
                        m["open_id"], dept_name, is_submitted=False
                    )
                    sent += 1
            logger.info(
                f"[Reminder] dept={dept_name} 已发送 {sent}/{len(not_submitted)} 张提醒卡片"
            )

        except Exception as e:
            logger.error(f"[Reminder] 提交提醒失败: {e}")

    @filter.command("test_feishu_doc")
    async def test_feishu_doc(self, event: AstrMessageEvent):
        """测试飞书文档权限：列举文件、读取内容、识别本周文件。
        用法：/test_feishu_doc [folder_token | folder_url]"""
        if not self.lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return
        yield event.plain_result(await _DocTestRunner(self).run(event))

    @filter.command("test_weekly_file")
    async def test_weekly_file(self, event: AstrMessageEvent):
        """测试本周文件/表格识别逻辑（Bitable 表格匹配 + Doc 文件名匹配）。
        用法：/test_weekly_file [folder_token | folder_url]"""
        if not self.lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return
        yield event.plain_result(await _WeeklyFileTestRunner(self).run(event))

#==============================================================
#                          Lifecycle
#==============================================================

    async def terminate(self):
        self.contact_service.stop_cache()


# ==============================================================
#                     Dev Test Runners
# ==============================================================
# Heavy implementation logic for developer test commands.
# Kept here so command handlers above stay thin.
# Each runner receives the plugin instance and an event.
# ==============================================================

class _DocTestRunner:
    """Runs the multi-step /test_feishu_doc diagnostic and returns a formatted result string."""

    def __init__(self, plugin: "MyPlugin"):
        self.plugin = plugin

    async def run(self, event: AstrMessageEvent) -> str:
        from .utils.env_config import extract_folder_token_from_url, set_dept_folder_token

        inline_arg = event.message_str.removeprefix("test_feishu_doc").strip().split("?")[0].strip()

        if inline_arg.startswith("http"):
            folder_token = extract_folder_token_from_url(inline_arg) or ""
            source_label = f"URL 参数 (token: {folder_token})"
        elif inline_arg:
            folder_token = inline_arg
            source_label = f"手动参数 (token: {folder_token})"
        else:
            # No arg — auto-detect from org tree membership + .env binding
            folder_token, source_label = await self._resolve_folder_token(event)

        lines = ["══ 飞书文档访问测试 ══", f"  目标: {source_label}", ""]

        if not folder_token:
            logger.warning(f"[DocTest] 未能确定文件夹 token: {source_label}  sender={event.get_sender_id()}")
            lines.append("❌ 无法继续：未找到文件夹 token。")
            lines.append("   请通过以下任一方式提供：")
            lines.append("   • /test_feishu_doc <folder_url>  — 直接传入文件夹链接")
            lines.append("   • 在绑定流程中完成部门文件夹绑定，再重试无参数命令")
            return "\n".join(lines)

        # Save token to .env when a URL is provided, matched against the sender's managed dept.
        if inline_arg.startswith("http") and folder_token:
            lines += await self._save_token(event, folder_token, set_dept_folder_token)
            lines.append("")

        lines += await self._step_list_files(folder_token)
        lines += await self._step_read_doc(self._last_doc_token)
        lines += await self._step_find_weekly(event, folder_token)
        lines += await self._step_submission_check(event)

        return "\n".join(lines)

    async def _resolve_folder_token(self, event: AstrMessageEvent) -> tuple[str, str]:
        """Auto-detect the folder token for the sender based on org tree membership and .env.

        Searches the cached org tree for which department the sender belongs to,
        then looks up the bound folder token from .env for that dept.

        Returns (folder_token, source_label). folder_token is "" when nothing is found.
        """
        from .utils.env_config import get_dept_folder_token

        sender_id = event.get_sender_id()
        try:
            org_tree = await self.plugin.contact_service.get_cached_org_tree()
        except Exception as e:
            logger.warning(f"[DocTest] 通讯录缓存未就绪，无法自动匹配文件夹: {e}")
            return "", "未知（通讯录缓存未就绪）"

        for entry in org_tree:
            in_dept = any(
                getattr(m, "open_id", None) == sender_id
                for m in entry.get("members", [])
            )
            if not in_dept:
                continue

            dept = entry["dept"]
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            dept_name = dept.name or open_dept_id
            token = get_dept_folder_token(open_dept_id)

            if token:
                logger.info(f"[DocTest] 自动匹配文件夹: dept={dept_name} token={token}")
                return token, f"{dept_name} 部门（.env 自动匹配，token: {token}）"

            # Dept found but no token bound
            logger.warning(f"[DocTest] 找到部门但未绑定文件夹: dept={dept_name} open_dept_id={open_dept_id}")
            return "", f"{dept_name} 部门（未绑定文件夹，请先完成绑定流程）"

        logger.warning(f"[DocTest] 发送者不在任何部门: sender_id={sender_id}")
        return "", f"未在通讯录中找到你所属的部门（open_id: {sender_id}）"

    # ── Internal state ───────────────────────────────────────────────────────

    _last_doc_token: str | None = None    # set by _step_list_files, used by _step_read_doc
    _last_weekly_file: dict | None = None  # set by _step_find_weekly, used by _step_submission_check

    # ── Steps ────────────────────────────────────────────────────────────────

    async def _save_token(self, event, folder_token: str, set_fn) -> list[str]:
        lines = []
        try:
            org_tree = await self.plugin.contact_service.get_cached_org_tree()
            sender_id = event.get_sender_id()
            managed = [e for e in org_tree if e.get("manager") and e["manager"].open_id == sender_id]
            if managed:
                open_dept_id = getattr(managed[0]["dept"], "open_department_id", "") or ""
                dept_name = managed[0]["dept"].name or open_dept_id
                if open_dept_id:
                    set_fn(open_dept_id, folder_token)
                    lines.append(f"✅ token 已保存至 .env（部门：{dept_name}）")
            else:
                lines.append("⚠️ 未找到你管理的部门，token 未保存至 .env")
        except Exception as e:
            lines.append(f"⚠️ 保存 token 失败: {e}")
        return lines

    async def _step_list_files(self, folder_token: str) -> list[str]:
        lines = ["【1】访问测试 — 列举目标文件夹内容"]
        self._last_doc_token = None
        try:
            files = await self.plugin.doc_service.list_folder_files(folder_token)
            lines.append(f"  ✅ 成功，共 {len(files)} 个条目")
            for f in files:
                lines.append(f"  - [{f.type}] {f.name}  (token: {f.token})")
                if f.type == "docx" and self._last_doc_token is None:
                    self._last_doc_token = f.token
        except Exception as e:
            lines.append(f"  ❌ 失败: {e}")
        return lines

    async def _step_read_doc(self, doc_token: str | None) -> list[str]:
        lines = ["", "【2】读取测试 — 读取文档原始内容"]
        if not doc_token:
            lines.append("  ⚠️ 跳过：未找到 docx 文件")
            return lines
        try:
            content = await self.plugin.doc_service.read_doc_plaintext(doc_token)
            preview = content.replace("\n", " ")[:120]
            if len(content) > 120:
                preview += "..."
            lines.append(f"  ✅ 成功，内容预览: {preview}")
        except Exception as e:
            lines.append(f"  ❌ 失败: {e}")
        return lines

    async def _step_find_weekly(self, event: AstrMessageEvent, folder_token: str) -> list[str]:
        """Step 3 — locate this week's report source.

        Two strategies depending on what's in the folder:

        Bitable: one persistent file per dept folder, tables named by ISO week
                 (e.g. "2026-W21"). Bot finds the bitable by file type, then
                 finds this week's table by name match inside it.

        Doc:     one file per week, named with ISO week or date range.
                 Bot scans the folder for a matching filename, with LLM fallback.
                 # NOTE: if departments use a non-standard naming convention,
                 # update match_this_week() in services/drive.py to add new patterns,
                 # or adjust the LLM prompt in _make_llm_fn().
        """
        from datetime import datetime, timedelta

        lines = ["", "【3】本周文件/表格识别"]
        self._last_weekly_file = None

        if not folder_token:
            lines.append("  ⚠️ 跳过：未提供文件夹 token")
            return lines

        today = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        year, week, _ = monday.isocalendar()
        iso_tag = f"{year}-W{week:02d}"

        try:
            files = await self.plugin.drive_service.list_folder_files(folder_token)
        except Exception as e:
            lines.append(f"  ❌ 列举文件夹失败: {e}")
            return lines

        # ── Bitable strategy ─────────────────────────────────────────────────
        # Dept keeps one persistent bitable in the folder; each week is a table
        # inside it named by ISO week (e.g. "2026-W21").
        bitable_files = [f for f in files if f["type"] == "bitable"]
        if bitable_files:
            bitable_file = bitable_files[0]   # one bitable per dept folder
            lines.append(f"  📊 Bitable 文件: 「{bitable_file['name']}」")
            try:
                bitable = self.plugin.bitable_service
                if not bitable:
                    lines.append("  ❌ BitableService 未初始化")
                    return lines
                tables = await bitable.list_tables(bitable_file["token"])
                matched = next((t for t in tables if iso_tag in t["name"]), None)
                if matched:
                    self._last_weekly_file = {
                        **bitable_file,
                        "table_id":   matched["table_id"],
                        "table_name": matched["name"],
                    }
                    lines.append(
                        f"  ✅ 本周表格: 「{matched['name']}」  table_id={matched['table_id']}"
                    )
                else:
                    all_names = [t["name"] for t in tables]
                    lines.append(
                        f"  ⚠️ 未找到本周表格（{iso_tag}），现有表格: {all_names}"
                    )
            except Exception as e:
                lines.append(f"  ❌ 读取 Bitable 表格列表失败: {e}")
            return lines

        # ── Doc strategy ─────────────────────────────────────────────────────
        # One doc file per week; identified by ISO week or date-range in filename.
        # LLM fallback fires only when neither pattern matches.
        try:
            found = await self.plugin.drive_service.find_this_week_file(
                folder_token, llm_fn=self._make_llm_fn(event)
            )
            if found:
                self._last_weekly_file = {**found, "table_id": None, "table_name": None}
                lines.append(
                    f"  ✅ 找到本周文件: 「{found['name']}」  type={found['type']}  token={found['token']}"
                )
            else:
                lines.append("  ⚠️ 未识别出本周文件（规则和 LLM 均未匹配）")
        except Exception as e:
            lines.append(f"  ❌ 失败: {e}")
        return lines

    async def _step_submission_check(self, event: AstrMessageEvent) -> list[str]:
        """Step 4 — delegate to check_submissions() in services/report.py, then format for display."""
        from .services.report import check_submissions

        lines = ["", "【4】提交情况检查 — 成员 vs 周报内容"]

        if not self._last_weekly_file:
            lines.append("  ⚠️ 跳过：第3步未找到本周文件")
            return lines

        provider = self.plugin.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            lines.append("  ❌ 无可用 LLM 提供者")
            return lines

        if not self.plugin.bitable_service:
            lines.append("  ❌ BitableService 未初始化")
            return lines

        r = await check_submissions(
            weekly_file=self._last_weekly_file,
            sender_id=event.get_sender_id(),
            contact_service=self.plugin.contact_service,
            doc_service=self.plugin.doc_service,
            bitable_service=self.plugin.bitable_service,
            llm_provider=provider,
            session_id=event.unified_msg_origin,
        )

        if not r["ok"]:
            lines.append(f"  ❌ {r['error']}")
            return lines

        file_type  = r["file_type"]
        table_name = self._last_weekly_file.get("table_name", "")
        if file_type == "bitable":
            lines.append(f"  📋 Bitable: 共 {len(r['members'])} 条记录（表格：{table_name}）")
        else:
            lines.append(f"  📄 Doc: 读取成功（{file_type}）")

        lines.append(f"  👥 部门：{r['dept_name']}，共 {r['total']} 名成员")
        lines.append(f"  ✅ 已提交 ({len(r['submitted'])}/{r['total']})：{', '.join(r['submitted']) or '（无）'}")
        lines.append(f"  ❌ 未提交 ({len(r['not_submitted'])}/{r['total']})：{', '.join(r['not_submitted']) or '（无）'}")
        return lines

    def _make_llm_fn(self, event: AstrMessageEvent):
        """Return an async callable that asks the LLM to pick the best file name from a list."""
        plugin = self.plugin

        async def _llm_pick(names: list[str]) -> str | None:
            from datetime import datetime as _dt
            today = _dt.now()
            _, week, _ = today.isocalendar()
            prompt = (
                f"今天是 {today.strftime('%Y-%m-%d')}（第 {week} 周）。"
                f"以下是飞书文件夹中的文件名列表：\n"
                + "\n".join(f"- {n}" for n in names)
                + "\n\n哪个文件最可能是本周的周报？请只回复文件名，不要添加其他内容。如果都不像，请回复 none。"
            )
            provider = plugin.context.get_using_provider(umo=event.unified_msg_origin)
            if not provider:
                return None
            resp = await provider.text_chat(prompt=prompt, session_id=event.unified_msg_origin)
            chosen = resp.completion_text.strip().strip('"').strip("'")
            return chosen if chosen.lower() != "none" else None

        return _llm_pick


class _WeeklyFileTestRunner:
    """Focused test for /test_weekly_file — only checks file/table discovery, no content reads."""

    def __init__(self, plugin: "MyPlugin"):
        self.plugin = plugin

    async def run(self, event: AstrMessageEvent) -> str:
        from .utils.env_config import extract_folder_token_from_url

        inline_arg = event.message_str.removeprefix("test_weekly_file").strip().split("?")[0].strip()

        if inline_arg.startswith("http"):
            folder_token = extract_folder_token_from_url(inline_arg) or ""
            source_label = f"URL 参数 (token: {folder_token})"
        elif inline_arg:
            folder_token = inline_arg
            source_label = f"手动参数 (token: {folder_token})"
        else:
            folder_token, source_label = await self._resolve_folder_token(event)

        lines = ["══ 本周文件识别测试 ══", f"  目标: {source_label}", ""]

        if not folder_token:
            lines.append("❌ 无法继续：未找到文件夹 token。")
            lines.append("   请通过 /test_weekly_file <folder_url> 直接传入，或先完成部门绑定。")
            return "\n".join(lines)

        from datetime import datetime, timedelta
        today = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        year, week, _ = monday.isocalendar()
        iso_tag = f"{year}-W{week:02d}"
        lines.append(f"  本周 ISO 标签: {iso_tag}  (本周一: {monday})")
        lines.append("")

        # ── List all files ───────────────────────────────────────────────────
        try:
            files = await self.plugin.drive_service.list_folder_files(folder_token)
        except Exception as e:
            lines.append(f"❌ 文件夹列举失败: {e}")
            return "\n".join(lines)

        lines.append(f"【文件夹内容】共 {len(files)} 个条目")
        for f in files:
            lines.append(f"  [{f['type']:8s}] {f['name']}  token={f['token']}")
        lines.append("")

        # ── Bitable strategy ─────────────────────────────────────────────────
        lines.append("【Bitable 策略】")
        bitable_files = [f for f in files if f["type"] == "bitable"]
        if not bitable_files:
            lines.append("  — 文件夹内无 Bitable 文件，跳过")
        else:
            bitable_file = bitable_files[0]
            lines.append(f"  📊 Bitable 文件: 「{bitable_file['name']}」  token={bitable_file['token']}")
            bitable = self.plugin.bitable_service
            if not bitable:
                lines.append("  ❌ BitableService 未初始化")
            else:
                try:
                    tables = await bitable.list_tables(bitable_file["token"])
                    lines.append(f"  所有表格 ({len(tables)} 个):")
                    for t in tables:
                        tag = "  ◀ 本周" if iso_tag in t["name"] else ""
                        lines.append(f"    - 「{t['name']}」  id={t['table_id']}{tag}")
                    matched = next((t for t in tables if iso_tag in t["name"]), None)
                    if matched:
                        lines.append(f"  ✅ 匹配: 「{matched['name']}」  table_id={matched['table_id']}")
                    else:
                        lines.append(f"  ⚠️ 未找到含 {iso_tag!r} 的表格")
                except Exception as e:
                    lines.append(f"  ❌ 读取表格列表失败: {e}")
        lines.append("")

        # ── Doc strategy ─────────────────────────────────────────────────────
        lines.append("【Doc 策略】")
        doc_files = [f for f in files if f["type"] in ("docx", "doc")]
        if not doc_files:
            lines.append("  — 文件夹内无 Doc/Docx 文件，跳过")
        else:
            from .services.drive import DriveService
            rule_match = DriveService.match_this_week(doc_files)
            if rule_match:
                lines.append(f"  ✅ 规则匹配: 「{rule_match['name']}」  type={rule_match['type']}")
            else:
                lines.append(f"  — 规则未匹配（检查了 {len(doc_files)} 个文件），尝试 LLM 回退…")
                try:
                    found = await self.plugin.drive_service.find_this_week_file(
                        folder_token, llm_fn=self._make_llm_fn(event)
                    )
                    if found:
                        lines.append(f"  ✅ LLM 匹配: 「{found['name']}」  type={found['type']}")
                    else:
                        lines.append("  ⚠️ LLM 也未能识别本周文件")
                except Exception as e:
                    lines.append(f"  ❌ LLM 回退失败: {e}")

        return "\n".join(lines)

    async def _resolve_folder_token(self, event: AstrMessageEvent) -> tuple[str, str]:
        from .utils.env_config import get_dept_folder_token
        sender_id = event.get_sender_id()
        try:
            org_tree = await self.plugin.contact_service.get_cached_org_tree()
        except Exception as e:
            return "", f"未知（通讯录缓存未就绪: {e}）"

        for entry in org_tree:
            in_dept = any(
                getattr(m, "open_id", None) == sender_id
                for m in entry.get("members", [])
            )
            if not in_dept:
                continue
            dept = entry["dept"]
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            dept_name = dept.name or open_dept_id
            token = get_dept_folder_token(open_dept_id)
            if token:
                return token, f"{dept_name} 部门（.env 自动匹配，token: {token}）"
            return "", f"{dept_name} 部门（未绑定文件夹）"

        return "", f"未在通讯录中找到你所属的部门（open_id: {sender_id}）"

    def _make_llm_fn(self, event: AstrMessageEvent):
        plugin = self.plugin

        async def _llm_pick(names: list[str]) -> str | None:
            from datetime import datetime as _dt
            today = _dt.now()
            _, week, _ = today.isocalendar()
            prompt = (
                f"今天是 {today.strftime('%Y-%m-%d')}（第 {week} 周）。"
                f"以下是飞书文件夹中的文件名列表：\n"
                + "\n".join(f"- {n}" for n in names)
                + "\n\n哪个文件最可能是本周的周报？请只回复文件名，不要添加其他内容。如果都不像，请回复 none。"
            )
            provider = plugin.context.get_using_provider(umo=event.unified_msg_origin)
            if not provider:
                return None
            resp = await provider.text_chat(prompt=prompt, session_id=event.unified_msg_origin)
            chosen = resp.completion_text.strip().strip('"').strip("'")
            return chosen if chosen.lower() != "none" else None

        return _llm_pick
