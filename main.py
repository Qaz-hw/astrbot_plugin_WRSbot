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


    # todo: trigger command need to be rewrote, maybe involking LLM to estimate user's meaning.
    #       According to UX consideration, duo trigger might needed
    @filter.command("Hello")
    async def cmd_send_wrs_welcome(self, event: AstrMessageEvent):
        """Send Welcome lark card to users"""
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        if not isinstance(event, LarkMessageEvent):
            yield event.plain_result("This command is only abled on Lark/Feishu")
            return
        
        await self.card_service.send_wrsbot_welcome(event.get_sender_id())
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

    @filter.command("test_feishu_doc")
    async def test_feishu_doc(self, event: AstrMessageEvent):
        """测试飞书文档权限：列举文件、读取内容、识别本周文件。
        用法：/test_feishu_doc [folder_token | folder_url]"""
        if not self.lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return
        yield event.plain_result(await _DocTestRunner(self).run(event))

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
        lines = ["", "【3】本周文件识别 — 规则匹配 + LLM 兜底"]
        self._last_weekly_file = None
        if not folder_token:
            lines.append("  ⚠️ 跳过：未提供文件夹 token")
            return lines
        try:
            found = await self.plugin.drive_service.find_this_week_file(
                folder_token, llm_fn=self._make_llm_fn(event)
            )
            if found:
                self._last_weekly_file = found
                lines.append(
                    f"  ✅ 找到本周文件: 「{found['name']}」  type={found['type']}  token={found['token']}"
                )
            else:
                lines.append("  ⚠️ 未识别出本周文件（规则和 LLM 均未匹配）")
        except Exception as e:
            lines.append(f"  ❌ 失败: {e}")
        return lines

    async def _step_submission_check(self, event: AstrMessageEvent) -> list[str]:
        """Step 4 — read this week's file content, get dept members, ask LLM who submitted."""
        import json
        from .prompts.submission_check import build_submission_check_prompt

        lines = ["", "【4】提交情况检查 — 成员 vs 周报内容"]

        if not self._last_weekly_file:
            lines.append("  ⚠️ 跳过：第3步未找到本周文件")
            return lines

        file = self._last_weekly_file
        file_type = file["type"]
        file_token = file["token"]

        # ── Read file content ────────────────────────────────────────────────
        content = ""
        try:
            if file_type == "bitable":
                bitable = self.plugin.bitable_service
                if not bitable:
                    lines.append("  ❌ BitableService 未初始化")
                    return lines
                tables = await bitable.list_tables(file_token)
                if not tables:
                    lines.append("  ❌ Bitable 无可用表格")
                    return lines
                table_id = tables[0]["table_id"]
                records = await bitable.list_records(file_token, table_id)
                content = bitable.records_to_text(records)
                lines.append(f"  📋 Bitable: 共 {len(records)} 条记录（表格：{tables[0]['name'] or table_id}）")
            elif file_type in ("docx", "doc"):
                content = await self.plugin.doc_service.read_doc_plaintext(file_token)
                lines.append(f"  📄 Doc: 读取成功，{len(content)} 字符")
            else:
                lines.append(f"  ⚠️ 跳过：不支持的文件类型 {file_type!r}")
                return lines
        except Exception as e:
            lines.append(f"  ❌ 读取文件内容失败: {e}")
            return lines

        # ── Get dept members from org tree ───────────────────────────────────
        sender_id = event.get_sender_id()
        members: list[dict] = []
        try:
            org_tree = await self.plugin.contact_service.get_cached_org_tree()
            for entry in org_tree:
                in_dept = any(
                    getattr(m, "open_id", None) == sender_id
                    for m in entry.get("members", [])
                )
                if in_dept:
                    members = [
                        {
                            "name": m.name or "",
                            "open_id": m.open_id or "",
                            "job_title": getattr(m, "job_title", "") or "",
                        }
                        for m in entry.get("members", [])
                    ]
                    dept_name = entry["dept"].name or ""
                    lines.append(f"  👥 部门：{dept_name}，共 {len(members)} 名成员")
                    break
        except Exception as e:
            lines.append(f"  ❌ 获取部门成员失败: {e}")
            return lines

        if not members:
            lines.append("  ⚠️ 未找到你所属的部门成员，无法检查提交情况")
            return lines

        # ── Ask LLM ─────────────────────────────────────────────────────────
        prompt = build_submission_check_prompt(members, content, file_type)
        provider = self.plugin.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            lines.append("  ❌ 无可用 LLM 提供者")
            return lines

        try:
            resp = await provider.text_chat(prompt=prompt, session_id=event.unified_msg_origin)
            raw = resp.completion_text.strip()
            result = json.loads(raw)
            submitted     = result.get("submitted", [])
            not_submitted = result.get("not_submitted", [])
        except (json.JSONDecodeError, Exception) as e:
            lines.append(f"  ❌ LLM 解析失败: {e}\n  原始回复: {getattr(resp, 'completion_text', '')[:200]}")
            return lines

        total = len(members)
        lines.append(f"  ✅ 已提交 ({len(submitted)}/{total})：{', '.join(submitted) or '（无）'}")
        lines.append(f"  ❌ 未提交 ({len(not_submitted)}/{total})：{', '.join(not_submitted) or '（无）'}")
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
