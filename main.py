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
import os
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
from .services.lark_context import set_active_lark_api

# TODO: add more comments for future dev
# TODO: Setup persona for user and admin for things like when they are talking to the bot 

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
        # This installation has both a small test Feishu app and the real
        # MRSbot app enabled.  Warm only the latter at startup; inbound events
        # still bind their own adapter normally.
        startup_platform_id = os.getenv("WRSBOT_STARTUP_LARK_PLATFORM_ID", "MRSbot")
        lark_apis = []
        startup_lark_apis = []
        if get_insts:
            for platform in get_insts():
                if hasattr(platform, "lark_api"):
                    lark_apis.append(platform.lark_api)
                    # AstrBot v4.26 stores a platform instance's configured
                    # ID in its config dict, not as platform.platform_id.
                    platform_config = getattr(platform, "config", {})
                    platform_id = (
                        platform_config.get("id", "")
                        if isinstance(platform_config, dict)
                        else getattr(platform, "platform_id", "")
                    )
                    if platform_id == startup_platform_id:
                        startup_lark_apis.append(platform.lark_api)

        if startup_lark_apis:
            self.lark_api = startup_lark_apis[0]
            logger.info(
                f"[WRSbot] 启动时使用飞书平台 {startup_platform_id!r} 预热通讯录缓存"
            )
        elif lark_apis:
            # Keep the plugin operational if this deployment uses a different
            # platform ID, but make the configuration issue unmistakable.
            self.lark_api = lark_apis[0]
            logger.warning(
                f"[WRSbot] 未找到启动预热平台 {startup_platform_id!r}；"
                "将等待入站飞书事件选择通讯录缓存"
            )

        self.card_service = LarkCardService(self.lark_api)
        self.card_service.inject_into_dispatcher(self.context.platform_manager)
        self.doc_service = DocService(self.lark_api)
        self.drive_service = DriveService(self.lark_api)
        self.bitable_service = BitableService(self.lark_api)
        self.contact_service = ContactService(self.lark_api)
        await self.contact_service.start_cache(startup_lark_apis)
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


    @filter.regex(r"^(hello|Hello|HELLO|你好|您好)$")
    async def cmd_send_wrs_welcome(self, event: AstrMessageEvent):
        """
        Send WRSbot welcome Lark card when user greets the bot.
        Trigger:
            hello
            Hello
            HELLO
           你好
           您好
        """

        # Only support Lark / Feishu
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        if not isinstance(event, LarkMessageEvent):
            yield event.plain_result(
                "This command is only available on Lark/Feishu"
            )
            return

        try:
            # Bind current Lark bot instance
            self.card_service.bind_lark_api(
                event.get_sender_id(),
                event.bot
            )

            self.contact_service.bind_lark_api(
                event.get_sender_id(),
                event.bot
            )

            # Send card by replying to current message
            # Avoid Feishu open_id cross-app issue
            await self.card_service.send_wrsbot_welcome(
                open_id=event.get_sender_id(),
                reply_message_id=event.message_obj.message_id,
                lark_api=event.bot,
            )

            # Tell AstrBot that this event already has a response
            event._has_send_oper = True

        except Exception as e:
            import traceback

            traceback.print_exc()

            yield event.plain_result(
                f"WRSbot welcome failed: {str(e)}"
            )

    @filter.command("文件夹配置")
    async def cmd_folder_config(self, event: AstrMessageEvent):
        """Show the caller's folder-binding status card.

        ``send_not_binding_card`` is intentionally the sole renderer here:
        its internal state builder returns either the bound or unbound card
        from one managed-department snapshot.  Do not pre-check
        ``is_fully_bound`` here, because a second lookup can race a contact
        cache refresh and produce mismatched status/name values.
        """
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        if not isinstance(event, LarkMessageEvent):
            yield event.plain_result("此命令仅支持飞书平台。")
            return

        open_id = event.get_sender_id()
        self.card_service.bind_lark_api(open_id, event.bot)
        self.contact_service.bind_lark_api(open_id, event.bot)
        set_active_lark_api(event.bot)
        reply_message_id = event.message_obj.message_id
        await self.card_service.send_not_binding_card(
            open_id,
            lark_api=event.bot,
            reply_message_id=reply_message_id,
        )
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
        """[Dev] Raw LLM passthrough — does NOT apply the session persona.

        Used for testing the base model's behavior without any WRSBot
        personality or workflow prompt interfering. General chat (no command)
        still uses the persona via AstrBot's default chat pipeline; only this
        command bypasses it.

        Usage: /test_llm <问题>
        """
        prompt = event.message_str.removeprefix("/test_llm").strip()
        if not prompt:
            yield event.plain_result("请在命令后输入问题，例如：/test_llm 你好")
            return

        provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        if not provider:
            yield event.plain_result("当前没有配置 LLM 提供者。")
            return

        # Intentionally NO system_prompt — this is a raw probe of the model.
        response = await provider.text_chat(
            prompt=prompt,
            system_prompt=None,
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
            lark_api = getattr(event, "bot", None)
            self.contact_service.bind_lark_api(
                event.get_sender_id(), lark_api, schedule_refresh=False
            )
            await self.contact_service._do_refresh(
                event.get_sender_id(), lark_api=lark_api
            )
            org_tree = await self.contact_service.get_cached_org_tree(
                event.get_sender_id(), lark_api=lark_api
            )
        except Exception as e:
            yield event.plain_result(f"获取部门列表失败: {e}")
            return
        yield event.plain_result(
            self.contact_service.format_org_tree_dump(org_tree, source="实时")
        )

    @filter.command("test_cached_feishu_contact")
    async def test_cached_feishu_contact(self, event: AstrMessageEvent):
        """[Admin] 查看当前内存缓存中的层级部门树（不发起 API 请求）。"""
        if not self.lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return
        try:
            lark_api = getattr(event, "bot", None)
            # This command must inspect existing data only.  Do not schedule a
            # refresh here, otherwise a cache miss races the background API call.
            self.contact_service.bind_lark_api(
                event.get_sender_id(), lark_api, schedule_refresh=False
            )
            hierarchy = await self.contact_service.get_cached_org_hierarchy(
                event.get_sender_id(), lark_api=lark_api
            )
        except Exception as e:
            yield event.plain_result(
                "通讯录缓存尚未就绪。请等待自动缓存刷新完成后重试，"
                "或先执行 /test_feishu_contact 进行一次实时刷新。"
            )
            return
        yield event.plain_result(
            self.contact_service.format_org_hierarchy_dump(hierarchy, source="缓存")
        )

    @filter.command("dump_bindings")
    async def dump_bindings(self, event: AstrMessageEvent):
        """[Developer] 显示周报及 PMbot 日报文件夹绑定状态，与通讯录交叉校验。"""
        try:
            org_tree = await self.contact_service.get_cached_org_tree(event.get_sender_id())
            hierarchy = await self.contact_service.get_cached_org_hierarchy(
                event.get_sender_id()
            )
        except Exception as e:
            yield event.plain_result(f"获取缓存失败: {e}")
            return
        yield event.plain_result(dump_dept_bindings(org_tree, hierarchy))

    @filter.command("check_user")
    async def cmd_check_user(self, event: AstrMessageEvent):
        """转储当前发送者的飞书通讯录用户档案。"""
        if not self.lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return
        self.contact_service.bind_lark_api(
            event.get_sender_id(), getattr(event, "bot", None)
        )
        yield event.plain_result(
            await self.contact_service.get_user_profile_dump(
                event.get_sender_id(), lark_api=getattr(event, "bot", None)
            )
        )

    @filter.command("list_contacts")
    async def list_contacts(self, event: AstrMessageEvent):
        """列出飞书通讯录中所有成员（扁平列表，不含部门树）。"""
        logger.info(f"CMD HIT | msg: {repr(event.message_str)} | is_wake: {event.is_wake}")

        if not self.lark_api:
            yield event.plain_result("未找到飞书适配器，请确认当前平台为飞书。")
            return

        try:
            lark_api = getattr(event, "bot", None)
            self.contact_service.bind_lark_api(event.get_sender_id(), lark_api)
            contacts, errors = await self.contact_service.list_all_members(lark_api=lark_api)
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
        """为当前会话设置人格。用法：/set_persona <人格名称>

        Robust arg parsing — accepts the command with or without a leading "/"
        and with @-mentions intact (group chat). The previous implementation
        used `removeprefix("/set_persona")` which silently failed when the
        user typed `set_persona X` (no slash), leaving the persona name as
        `"set_persona X"` and producing a confusing "找不到人格" error.
        """
        text = event.message_str.strip()
        # Strip Lark @-mention formats so "@bot set_persona X" parses correctly
        text = re.sub(r"\[At:[^\]]*\]", "", text)
        text = re.sub(r"@\S+", "", text).strip()
        # Split on first whitespace — first token is the command name (with or
        # without leading "/"), second token is the persona name. Anything
        # after the second whitespace is treated as part of the name (personas
        # typically have no spaces, but tolerate it just in case).
        parts = text.split(None, 1)
        name = parts[1].strip() if len(parts) > 1 else ""

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

    @filter.command("my_style")
    async def my_style(self, event: AstrMessageEvent):
        """[Dev] Dump the caller's saved personal style profile from sp.

        Shows both the raw fields (tone_tags / custom_instructions /
        writing_samples) and the structured_profile (actuator dials computed
        at save time). Useful for debugging when the view-style card shows
        an unexpected value — e.g. "tone tags don't display" usually means
        the catalog filter rejected them; you'll see the saved list here.

        Usage: /my_style
        """
        from .utils.manager_style import get_manager_style, STYLE_TAG_CATALOG

        open_id = event.get_sender_id()
        profile = await get_manager_style(open_id)

        tone_tags = profile.get("tone_tags") or []
        custom    = (profile.get("custom_instructions") or "").strip()
        samples   = (profile.get("writing_samples") or "").strip()
        updated   = profile.get("updated_at") or "（尚未保存）"
        structured = profile.get("structured_profile")

        lines: list[str] = [
            "══ 个人风格档案 ══",
            f"open_id      : {open_id}",
            f"最后更新     : {updated}",
            "",
            "── 原始字段（raw, saved verbatim）──",
            f"tone_tags ({len(tone_tags)}): {tone_tags if tone_tags else '（空 — 卡片可能未发送 / 被目录过滤）'}",
            f"custom_instructions ({len(custom)} 字):",
            f"  {custom or '（空）'}",
            f"writing_samples ({len(samples)} 字):",
        ]
        if samples:
            # Truncate long samples in the dump
            preview = samples if len(samples) <= 600 else (samples[:600] + "\n  …(truncated)")
            for ln in preview.splitlines():
                lines.append(f"  {ln}")
        else:
            lines.append("  （空）")
        lines.append("")

        # Structured profile (actuator dials computed at save time)
        if isinstance(structured, dict) and structured:
            lines.append("── 结构化档案（structured_profile, 写入阶段使用）──")
            lines.append(f"sentence_length       : {structured.get('sentence_length')}")
            lines.append(f"formality (1-5)       : {structured.get('formality')}")
            lines.append(f"voice                 : {structured.get('voice')}")
            lines.append(f"emoji_density (0-1)   : {structured.get('emoji_density')}")
            lines.append(f"tone_summary          : {structured.get('tone_summary') or '（空）'}")
            sigs = structured.get("signature_phrases") or []
            lines.append(f"signature_phrases ({len(sigs)}): {sigs if sigs else '（空）'}")
            trans = structured.get("preferred_transitions") or []
            lines.append(f"preferred_transitions ({len(trans)}): {trans if trans else '（空）'}")
            banned = structured.get("banned_phrases") or []
            lines.append(f"banned_phrases ({len(banned)}): {banned[:6]}" + ("..." if len(banned) > 6 else ""))
            extras = (structured.get("extras") or "").strip()
            lines.append(f"extras                : {extras or '（空）'}")
        else:
            lines.append("── 结构化档案：尚未生成 ──")
            lines.append("  (LLM 结构化步骤未跑或失败 — 保存时缺少 llm_provider)")
        lines.append("")

        # Diagnostic — show catalog so user can compare
        lines.append("── 调试参考 ──")
        lines.append(f"STYLE_TAG_CATALOG (saved tags must match exactly): {STYLE_TAG_CATALOG}")
        lines.append(f"sp key: global:wrsbot:manager_style:{open_id}")

        yield event.plain_result("\n".join(lines))

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
                "Hello":                      self.cmd_send_wrs_welcome,
                "check_user":                 self.cmd_check_user,
                "test_feishu_contact":        self.test_feishu_contact,
                "test_cached_feishu_contact": self.test_cached_feishu_contact,
                "list_contacts":              self.list_contacts,
                "set_doc_folder":             self.set_doc_folder,
                "test_feishu_doc":            self.test_feishu_doc,
                "test_weekly_file":           self.test_weekly_file,
                "test_persona":               self.test_persona,
                "set_persona":                self.set_persona,
                "my_style":                   self.my_style,
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

        This is the sole "natural language → workflow" entry point. The old
        keyword_card_reply handler (substring "周报"/"帮助" → card) was removed
        — its routing was too brittle and called a service method that no
        longer exists.
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
        self.card_service.bind_lark_api(open_id, event.bot)
        set_active_lark_api(event.bot)
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
        # IMPORTANT: call event.stop_event() AFTER any `yield` in this
        # handler. AstrBot's scheduler breaks out of the handler generator
        # the next time it checks `event.is_stopped()` after a yield — so
        # stopping the event before yielding means the post-yield code
        # (e.g. admin_view) is never reached. See scheduler.py:52-58.
        if intent == "generate_summary":
            if not is_admin:
                return  # unauthorized — silent skip, no error to user
            if is_group:
                yield event.plain_result("正在为您加载部门管理视图，请到私聊查看 📊")
            event.stop_event()
            try:
                if self.card_service.is_fully_bound(open_id):
                    await self.views.admin_view(open_id)
                else:
                    await self.card_service.send_not_binding_card(open_id)
            except Exception as e:
                logger.error(f"[NaturalTrigger] 管理员流程失败: {e}")
            return

        if intent == "get_writing_folder":
            try:
                reply = await self.views._build_writing_folder_reply(open_id)
            except Exception as e:
                logger.error(f"[NaturalTrigger] 查询周报文件夹失败: {e}")
                reply = "查询周报文件夹时出错，请稍后重试或联系部门负责人。"
            yield event.plain_result(reply)
            event.stop_event()
            return

    # NOTE: the old keyword_card_reply handler ("周报" / "帮助" substring →
    # send_keyword_card) was removed. It called a service method that no
    # longer exists (LarkCardService.send_keyword_card) and was crashing on
    # every message containing "周报". Its job is now handled by
    # natural_report_trigger, which uses an LLM intent classifier instead
    # of brittle substring matching — that's where to add new natural-
    # language entry points.

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
            org_tree = await self.plugin.contact_service.get_cached_org_tree(sender_id)
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
            sender_id = event.get_sender_id()
            org_tree = await self.plugin.contact_service.get_cached_org_tree(sender_id)
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
            org_tree = await self.plugin.contact_service.get_cached_org_tree(sender_id)
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
