from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, sp
from datetime import datetime, timedelta

from astrbot.dashboard.routes import persona

from astrbot.dashboard.routes import persona

@register("helloworld", "YourName", "一个简单的 Hello World 插件", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context: Context = context

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

###################Feishu message connection testing prompts####################

    # 注册指令的装饰器。指令名为 helloworld。注册成功后，发送 `/helloworld` 就会触发这个指令，并回复 `你好, {user_name}!`
    @filter.command("helloworld")
    async def helloworld(self, event: AstrMessageEvent):
        """这是一个 hello world 指令""" # 这是 handler 的描述，将会被解析方便用户了解插件内容。建议填写。
        user_name = event.get_sender_name()
        logger.info("✅ Hello World 指令被触发了！") #Terminal 中会输出日志，方便开发调试。
        user_ocupation = event.get_sender_id() # 获取用户的职业信息，可能为 None
        message_str = event.message_str # 用户发的纯文本消息字符串
        message_chain = event.get_messages() # 用户所发的消息的消息链 # from astrbot.api.message_components import *
        logger.info(message_chain)
        yield event.plain_result(f"Hello, {user_name}, 你发了 {message_str}!") # 发送一条纯文本消息

    @filter.command("创建周报总结")
    async def create_weekly_report(self, event: AstrMessageEvent):
        """这是一个创建周报总结的指令"""
        user_name = event.get_sender_name()
        # 这里可以添加一些逻辑来生成周报总结
        logger.info("✅ 创建周报总结指令被触发了！") #Terminal 中会输出日志，方便开发调试。
        weekly_report = f"{user_name} 的周报总结：\n- 完成了任务 A\n- 参与了会议 B\n- 学习了新技术 C"
        yield event.plain_result(weekly_report)

    @filter.command("关于WRSbot")
    async def about_wrsbot(self, event: AstrMessageEvent):
        """这是一个关于WRSbot的指令"""
        logger.info("✅ 关于WRSbot指令被触发了！") #Terminal 中会输出日志，方便开发调试。
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

####################LLM connection testing prompts####################

    #Embedded LLM test command
    @filter.command("test_llm")
    async def test_llm(self, event: AstrMessageEvent):
        """调用 LLM 并返回回答。用法：/test_llm <问题>"""
        prompt = event.message_str.removeprefix("/test_llm").strip()
        persona = await self.context.persona_manager.get_default_persona_v3(umo=event.unified_msg_origin)
        system_prompt = persona["prompt"] if persona else None\
        
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

####################Feishu contacts connection testing prompts end####################
# todo: 添加一些 Feishu 联系人连接测试的指令，例如获取联系人列表、获取特定联系人的信息等。


######################Astrbot bot persona testing prompts####################

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

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        pass
