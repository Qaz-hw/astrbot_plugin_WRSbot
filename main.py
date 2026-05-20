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
from .services.pipelines import ViewsPipelines, ReportPipelines, StylePipelines
from .utils.env_config import dump_dept_bindings

# TODO：added 支持配置“个人风格特征词”，让输出结果尽可能模仿管理者的撰写口吻。function.py
# TODO: completely formulate, strucutre the code, delete redundent files/ unused functions and add more comments

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
        # ── Pipelines (orchestration layer; see services/pipelines/) ────────
        # Three domain classes, all sharing the same service handles via
        # PipelineBase. Each is registered on the card service so card
        # actions can dispatch into the right pipeline method.
        common_args = (
            self.lark_api, self.context,
            self.contact_service, self.drive_service,
            self.bitable_service, self.doc_service,
            self.card_service,
        )
        self.views  = ViewsPipelines(*common_args)
        self.report = ReportPipelines(*common_args)
        self.style  = StylePipelines(*common_args)

        self.card_service.set_admin_pipeline(self.views.admin_view)
        self.card_service.set_user_pipeline(self.views.user_view)
        self.card_service.set_reminder_pipeline(self.views.reminder)
        self.card_service.set_view_doc_pipeline(self.views.view_doc)
        self.card_service.set_report_pipelines(
            generate=self.report.generate,
            rewrite=self.report.rewrite,
            submit=self.report.submit,
        )
        self.card_service.set_style_pipelines(
            open_config=self.style.open_style_config,
            save=self.style.save_manager_style,
        )

#==============================================================
#                         Card Commands
#==============================================================


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

        #extras = event.get_extra()
        #lines += [
        #    "",
        #    "══ 额外信息 ══",
        #    f"extras          : {extras if extras else '（空）'}",
        #]

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

    # Confidence threshold for LLM-classified intent. The classifier returns
    # 0.0–1.0; only act when the model is genuinely sure. 0.9 favors missing
    # ambiguous requests over false-triggering on unrelated chat.
    _INTENT_THRESHOLD = 0.9

    # Cheap pre-filter — only invoke the LLM classifier when the message
    # plausibly relates to weekly reports. Skips LLM cost on every "hi" /
    # "ok" / unrelated chat. Keep lenient; LLM is the real decision-maker.
    _INTENT_HINTS = (
        "周报", "汇总", "总结", "写", "提交", "report", "summary", "submit",
    )

    _INTENT_SYSTEM = (
        "你是一名意图识别助手。判断用户的一句话属于以下哪种「与本周周报相关」的意图：\n"
        "  • generate_summary: 请求生成 / 汇总 / 输出本部门的周报总结\n"
        "      例：帮我生成周报汇总 / 给我周报总结 / 部门周报来一份 / 出本周周报\n"
        "  • get_writing_folder: 自己想要写 / 提交 / 上传本周周报，需要文件夹链接\n"
        "      例：我要写周报 / 周报往哪交 / 给我周报文件夹 / 这周周报怎么写\n"
        "  • none: 其他所有情况（讨论内容、抱怨、问别人提交了没、无关闲聊、查看历史周报）\n\n"
        "仅以下列 JSON 格式回复，禁止任何额外文字或代码块：\n"
        '{"intent": "<generate_summary|get_writing_folder|none>", '
        '"confidence": <0-1 小数>, "reason": "<不超过20字>"}'
    )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def natural_report_trigger(self, event: AstrMessageEvent):
        """LLM-gated natural-language entry — routes by detected intent + role.

        Intents (from _INTENT_SYSTEM classifier):
          • generate_summary    → managers only: fire admin view pipeline
          • get_writing_folder  → anyone: reply with bound folder URL
          • none / low conf     → silent skip

        Group behavior:
          • generate_summary: ack in-group, DM the admin view card
          • get_writing_folder: reply inline (URL is not sensitive)

        Failure modes:
          • LLM/parse error → fail closed, no trigger (welcome card + slash
            commands remain as deterministic fallbacks)
          • generate_summary by non-manager → silent skip (unauthorized)
          • get_writing_folder but no folder bound → reply with binding guidance

        Registered BEFORE keyword_card_reply so the broader "周报" keyword
        doesn't intercept specific intents first.
        """
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
        if not isinstance(event, LarkMessageEvent):
            return

        raw = event.message_str or ""
        if raw.lstrip().startswith("/"):
            return  # slash commands → group_command_dispatcher

        # Strip @-mentions so "@wrsbot 我要写周报" classifies on the body.
        text = re.sub(r"\[At:[^\]]*\]", "", raw)
        text = re.sub(r"@\S+", "", text).strip()
        if not text:
            return  # bare @-mention with no content — nothing to classify

        # Cheap pre-filter to skip LLM call on unrelated chat.
        if not any(h in text.lower() for h in self._INTENT_HINTS):
            return

        open_id = event.get_sender_id()
        provider = self.context.get_using_provider(
            umo=f"lark:open_id:{open_id}"
        )
        if not provider:
            logger.warning(f"[NaturalTrigger] 未找到 LLM provider: open_id={open_id}")
            return

        # ── LLM intent classification ─────────────────────────────────────
        try:
            resp = await provider.text_chat(
                prompt=text,
                system_prompt=self._INTENT_SYSTEM,
                session_id=f"wrsbot_intent:{open_id}",
            )
            raw_out = (resp.completion_text or "").strip()
            if raw_out.startswith("```"):
                raw_out = raw_out.split("```")[1].lstrip("json").strip()
            parsed     = json.loads(raw_out)
            intent     = str(parsed.get("intent", "none"))
            confidence = float(parsed.get("confidence", 0.0))
            reason     = str(parsed.get("reason", ""))
        except Exception as e:
            logger.warning(f"[NaturalTrigger] LLM 意图判定失败: {e}")
            return

        logger.info(
            f"[NaturalTrigger] open_id={open_id} text={text!r} "
            f"intent={intent} confidence={confidence} reason={reason!r}"
        )

        if intent == "none" or confidence < self._INTENT_THRESHOLD:
            return  # below threshold → silent skip

        is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
        is_admin = (
            self.contact_service is not None
            and self.contact_service.is_manager(open_id)
        )

        # ── Route by intent ───────────────────────────────────────────────
        if intent == "generate_summary":
            if not is_admin:
                return  # unauthorized — silent skip, no error to user
            event.stop_event()
            if is_group:
                yield event.plain_result("正在为您加载部门管理视图，请到私聊查看 📊")
            try:
                if self.card_service.is_fully_bound(open_id):
                    await self.views.admin_view(open_id)
                else:
                    await self.card_service.send_not_binding_card(open_id)
            except Exception as e:
                logger.error(f"[NaturalTrigger] 管理员流程失败: {e}")
            return

        if intent == "get_writing_folder":
            event.stop_event()
            try:
                reply = await self.views._build_writing_folder_reply(open_id)
            except Exception as e:
                logger.error(f"[NaturalTrigger] 查询周报文件夹失败: {e}")
                reply = "查询周报文件夹时出错，请稍后重试或联系部门负责人。"
            yield event.plain_result(reply)
            return

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
