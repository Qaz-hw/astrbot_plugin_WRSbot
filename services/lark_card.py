#=====================================================
#  services/lark_card.py — Feishu Card Service
#=====================================================
#
#  Responsibilities:
#    - Hold all Feishu card template IDs
#    - Send template cards via lark_oapi IM message API
#    - Handle card action callbacks (button clicks) synchronously
#    - Inject the card action processor into the SDK dispatcher
#
#  Design notes:
#    handle_card_action_sync() MUST be synchronous — the SDK
#    calls it without await and needs P2CardActionTriggerResponse
#    back immediately. Async side-effects use asyncio.create_task().
#
#  Does NOT contain:
#    - AstrBot command handlers (lives in main.py)
#    - LLM or report logic
#    - Bitable or Doc API calls
#=====================================================

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                      WRSbot · Card Interaction Map                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
#
#  Notation:
#    →  card     card update (same message replaced in-place)
#    ⇒  card     new message sent
#    ✉           async message dispatched to another user
#    [TBD]       card not yet created in Feishu card builder
#
# ┌──────────────────────────────────────────────────────────────────────────────┐
# │  WRSBOT_WELCOME_CARD_ID                                                     │
# │  Trigger: first contact with bot / /start command                           │
# ├──────────────────────┬──────────────────────────┬───────────────────────────┤
# │  Button              │  Condition               │  Result                   │
# ├──────────────────────┼──────────────────────────┼───────────────────────────┤
# │  指令列表             │  always                  │  toast: 指令列表          │
# │  使用帮助             │  always                  │  toast: 帮助信息          │
# │  角色说明             │  always                  │  toast: 角色说明          │
# │  文件夹配置           │  always                  │  → BINDFOLDER_TUTORIAL    │
# │  开始使用             │  已配置 · 管理员          │  → ADMIN_DASHBOARD [TBD]  │
# │  开始使用             │  已配置 · 员工            │  → USER_DASHBOARD  [TBD]  │
# │  开始使用 [disabled]  │  文件夹未配置             │  toast: 请先完成配置      │
# └──────────────────────┴──────────────────────────┴───────────────────────────┘
#
# ┌──────────────────────────────────────────────────────────────────────────────┐
# │  WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID                                         │
# │  Trigger: 文件夹配置 on welcome card                                         │
# │  Context: bot prompts user to reply with a Feishu folder share URL          │
# ├──────────────────────┬──────────────────────────┬───────────────────────────┤
# │  Button              │  Condition               │  Result                   │
# ├──────────────────────┼──────────────────────────┼───────────────────────────┤
# │  重新扫描             │  URL found in chat       │  → BINDING_SUCCESS        │
# │  重新扫描             │  URL not found           │  → NOT_BINDING_FEEDBACK   │
# │  我已回复链接         │  URL found in chat       │  → BINDING_SUCCESS        │
# │  我已回复链接         │  URL not found           │  → NOT_BINDING_FEEDBACK   │
# └──────────────────────┴──────────────────────────┴───────────────────────────┘
#
# ┌──────────────────────────────────────────────────────────────────────────────┐
# │  WRSBOT_BINDING_FEEDBACK_SUCCESS_CARD_ID                                    │
# │  Trigger: folder URL detected after button click                            │
# │  Terminal — no buttons                                                      │
# └──────────────────────────────────────────────────────────────────────────────┘
#
# ┌──────────────────────────────────────────────────────────────────────────────┐
# │  WRSBOT_NOT_BINDING_FEEDBACK_CARD_ID                                        │
# │  Trigger: no folder URL detected after button click                         │
# ├──────────────────────┬──────────────────────────┬───────────────────────────┤
# │  Button              │  Condition               │  Result                   │
# ├──────────────────────┼──────────────────────────┼───────────────────────────┤
# │  重试                 │  always                  │  → BINDFOLDER_TUTORIAL    │
# └──────────────────────┴──────────────────────────┴───────────────────────────┘
#
# ┌──────────────────────────────────────────────────────────────────────────────┐
# │  WRSBOT_ADMIN_DASHBOARD_CARD_ID                                    [TBD]    │
# │  Trigger: 开始使用 · manager role                                            │
# │  Shows: X / N 人已提交本周周报                                               │
# ├──────────────────────┬──────────────────────────┬───────────────────────────┤
# │  Button              │  Condition               │  Result                   │
# ├──────────────────────┼──────────────────────────┼───────────────────────────┤
# │  查看周报文件         │  always                  │  link → folder URL        │
# │  催交报告             │  有未提交成员             │  ✉ SUBMISSION_REMINDER    │
# │                      │                          │    sent to each non-sub   │
# │  开始周报汇总         │  all submitted           │  start pipeline           │
# │  开始周报汇总         │  not all submitted       │  → CONFIRM_PARTIAL [TBD]  │
# └──────────────────────┴──────────────────────────┴───────────────────────────┘
#
# ┌──────────────────────────────────────────────────────────────────────────────┐
# │  WRSBOT_SUBMISSION_REMINDER_CARD_ID                                [TBD]    │
# │  Trigger: 催交报告 · sent to each non-submitting member                     │
# ├──────────────────────┬──────────────────────────┬───────────────────────────┤
# │  Button              │  Condition               │  Result                   │
# ├──────────────────────┼──────────────────────────┼───────────────────────────┤
# │  去填写报告           │  always                  │  link → doc / bitable     │
# └──────────────────────┴──────────────────────────┴───────────────────────────┘
#
# ┌──────────────────────────────────────────────────────────────────────────────┐
# │  WRSBOT_CONFIRM_PARTIAL_SUMMARY_CARD_ID                            [TBD]    │
# │  Trigger: 开始周报汇总 when not all members have submitted                  │
# ├──────────────────────┬──────────────────────────┬───────────────────────────┤
# │  Button              │  Condition               │  Result                   │
# ├──────────────────────┼──────────────────────────┼───────────────────────────┤
# │  仍然开始             │  always                  │  start pipeline           │
# │  取消                 │  always                  │  dismiss                  │
# └──────────────────────┴──────────────────────────┴───────────────────────────┘
#
# ┌──────────────────────────────────────────────────────────────────────────────┐
# │  Summarization pipeline (async — no card during processing)                 │
# ├──────────────────────────────────────────────────────────────────────────────┤
# │  1. Fetch reports from Bitable / Docs                                        │
# │  2. LLM summarize → LLM rewrite (manager persona)                           │
# │  3. ⇒ rich text reply to manager (group chat or DM)                         │
# │  4. ⇒ WRSBOT_SUMMARY_RESULT_CARD_ID [TBD]                                   │
# └──────────────────────────────────────────────────────────────────────────────┘
#
# ┌──────────────────────────────────────────────────────────────────────────────┐
# │  WRSBOT_SUMMARY_RESULT_CARD_ID                                     [TBD]    │
# │  Trigger: sent after summarization pipeline completes                       │
# ├──────────────────────┬──────────────────────────┬───────────────────────────┤
# │  Button              │  Condition               │  Result                   │
# ├──────────────────────┼──────────────────────────┼───────────────────────────┤
# │  写入周报文档         │  always                  │  write to doc / bitable   │
# │  重新生成             │  always                  │  re-run pipeline          │
# │  [TBD]               │                          │                           │
# └──────────────────────┴──────────────────────────┴───────────────────────────┘
#
# ┌──────────────────────────────────────────────────────────────────────────────┐
# │  WRSBOT_USER_DASHBOARD_CARD_ID                                     [TBD]    │
# │  Trigger: 开始使用 · employee role                                           │
# │  (functionality TBD)                                                        │
# └──────────────────────────────────────────────────────────────────────────────┘

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta

from astrbot.api import logger
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.event.callback.processor import P2CardActionTriggerProcessor

# ──Connection Testing cards template IDs ────────────────────────────────────────────────────────
# Set via environment variables. Default values are test card IDs.
# Create cards in Feishu card builder and update these IDs before production.
WELCOME_CARD_ID = os.getenv("WELCOME_CARD_ID", "AAqtuTe2kNZbb")
ALERT_CARD_ID = os.getenv("ALERT_CARD_ID", "AAqtuTeNqRxte")
ALERT_RESOLVED_CARD_ID = os.getenv("ALERT_RESOLVED_CARD_ID", "AAqtuTeLM56jm")

# ──WRSbot cards template IDs ────────────────────────────────────────────────────────
# Set via environment variables. Default values are test card IDs.
WRSBOT_WELCOME_CARD_ID = os.getenv("WRSBOT_WELCOME_CARD_ID", "AAqtqD3jefquF")
WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID = os.getenv("WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID", "AAqt8pubhP0Br")
WRSBOT_BINDING_FEEDBACK_SUCCESS_CARD_ID = os.getenv("WRSBOT_BINDING_FEEDBACK_SUCCESS_CARD_ID", "AAqtVxQCOa3D8")
WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID = os.getenv("WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID", "AAqtwzfERE7gi")
WRSBOT_ADMIN_VIEW_CARD_ID = os.getenv("WRSBOT_ADMIN_VIEW_CARD_ID", "AAqtVhgEFc7gG")
WRSBOT_ADMIN_RSCHECK_CARD_ID = os.getenv("", "AAqtVXND6ex3t") #Report Submission check. triggered when starting weekly report summary when not everyone in the department is submitted their report 
# WRSBOT_CARD_ID = os.getenv("", "")
# WRSBOT_CARD_ID = os.getenv("", "")



class LarkCardService:
    def __init__(self, lark_api):
        self.lark_api = lark_api

    # ── Dispatcher injection ─────────────────────────────────────────────────

    def inject_into_dispatcher(self, platform_manager) -> None:
        """Inject card action handler into SDK's _callback_processor_map.

        Writes directly into the EventDispatcherHandler's internal dict so
        we can register without touching lark_adapter.py. The WS client holds
        a reference to the same object, so the write is immediately visible.
        """
        get_insts = getattr(platform_manager, "get_insts", None)
        if not get_insts:
            return
        for platform in get_insts():
            if hasattr(platform, "event_handler") and hasattr(
                platform.event_handler, "_callback_processor_map"
            ):
                platform.event_handler._callback_processor_map[
                    "p2.card.action.trigger"
                ] = P2CardActionTriggerProcessor(self.handle_card_action_sync)
                logger.info("✅ 飞书卡片动作处理器已注入事件分发器")

    # ── Card action callback ─────────────────────────────────────────────────

    def handle_card_action_sync(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """Route card button clicks to the correct response.

        Must be synchronous — no awaits. Async side-effects (e.g. sending a
        new message) are scheduled with asyncio.create_task().
        """
        if data.event is None:
            return P2CardActionTriggerResponse({})

        action = data.event.action
        action_value: dict = action.value or {} if action is not None else {}
        action_key: str = action_value.get("action", "")
        open_id: str = (
            data.event.operator.open_id or ""
            if data.event.operator is not None
            else ""
        )

        if action_key == "send_alarm":
            logger.info("✅ 飞书卡片动作已在处理")
            if self.lark_api:
                asyncio.create_task(self.send_alarm_card("open_id", open_id))
            return P2CardActionTriggerResponse({})

        if action_key == "complete_alarm":
            logger.info("✅ 飞书卡片动作已在处理")
            form_value: dict = action.form_value or {} if action is not None else {}
            notes = str(form_value.get("notes_input", ""))
            alarm_time: str = action_value.get("time", "")
            complete_time = datetime.now(timezone(timedelta(hours=8))).strftime(
                "%Y-%m-%d %H:%M:%S (UTC+8)"
            )
            return P2CardActionTriggerResponse(
                {
                    "toast": {
                        "type": "info",
                        "content": "已处理完成！",
                        "i18n": {"zh_cn": "已处理完成！", "en_us": "Resolved!"},
                    },
                    "card": {
                        "type": "template",
                        "data": {
                            "template_id": ALERT_RESOLVED_CARD_ID,
                            "template_variable": {
                                "alarm_time": alarm_time,
                                "open_id": open_id,
                                "complete_time": complete_time,
                                "notes": notes,
                            },
                        },
                    },
                }
            )

        if action_key == "show_commands":
            logger.info("✅ 飞书卡片动作已在处理")
            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": "请使用 /help 查看所有指令"}}
            )

        if action_key == "start_report":
            logger.info("✅ 飞书卡片动作已在处理")
            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": "请在群聊中发送 /生成周报 指令"}}
            )
        
        # todo: add actions for WRSbot workflow cards

        return P2CardActionTriggerResponse({})

    
    # ── Functional testing cards sending ─────────────────────────────────────────────────────────

    async def send_template_card(
        self,
        receive_id_type: str,
        receive_id: str,
        card_id: str,
        template_variables: dict,
    ) -> bool:
        """Base method — send any Feishu template card by ID and variables."""
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        content = json.dumps(
            {
                "type": "template",
                "data": {
                    "template_id": card_id,
                    "template_variable": template_variables,
                },
            },
            ensure_ascii=False,
        )
        return await LarkMessageEvent._send_im_message(
            self.lark_api,
            content=content,
            msg_type="interactive",
            receive_id=receive_id,
            receive_id_type=receive_id_type,
        )

    async def send_welcome_card(self, open_id: str) -> bool:
        """Send the welcome card to a user by open_id."""
        if not WELCOME_CARD_ID:
            logger.warning("[WRSbot] WELCOME_CARD_ID 未设置，跳过发送欢迎卡片")
            return False
        return await self.send_template_card(
            "open_id", open_id, WELCOME_CARD_ID, {"open_id": open_id}
        )

    async def send_alarm_card(self, receive_id_type: str, receive_id: str) -> bool:
        """Send the alarm card with current UTC+8 timestamp."""
        if not ALERT_CARD_ID:
            logger.warning("[WRSbot] ALERT_CARD_ID 未设置，跳过发送告警卡片")
            return False
        alarm_time = datetime.now(timezone(timedelta(hours=8))).strftime(
            "%Y-%m-%d %H:%M:%S (UTC+8)"
        )
        return await self.send_template_card(
            receive_id_type, receive_id, ALERT_CARD_ID, {"alarm_time": alarm_time}
        )

    async def send_keyword_card(
        self,
        sender_name: str,
        matched_keyword: str,
        text: str,
        reply_message_id: str,
    ) -> None:
        """Send the keyword trigger card as a reply to the original message."""
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        card_json = {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": f"WRSbot · 检测到关键词「{matched_keyword}」"},
                "template": "blue",
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"**来自：** {sender_name}\n**消息：** {text}",
                    },
                    {"tag": "hr"},
                    {
                        "tag": "column_set",
                        "flex_mode": "none",
                        "columns": [
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [
                                    {
                                        "tag": "button",
                                        "text": {"tag": "plain_text", "content": "查看指令列表"},
                                        "type": "default",
                                        "behaviors": [
                                            {"type": "callback", "value": {"action": "show_commands"}}
                                        ],
                                    }
                                ],
                            },
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [
                                    {
                                        "tag": "button",
                                        "text": {"tag": "plain_text", "content": "开始生成周报"},
                                        "type": "primary",
                                        "behaviors": [
                                            {"type": "callback", "value": {"action": "start_report"}}
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                ]
            },
        }
        await LarkMessageEvent._send_interactive_card(
            card_json,
            lark_client=self.lark_api,
            reply_message_id=reply_message_id,
        )


    # ── WRSbot workflow cards sending ─────────────────────────────────────────────────────────

    async def send_wrsbot_welcome(self, open_id: str) -> bool:
        """Send wrsbot welcome card to a user by open_id"""
        if not WRSBOT_WELCOME_CARD_ID:
            logger.warning("[WRSbot] WRSBOT_WELCOME_CARD_ID not set, skipped sending welcome card")
            return False
        return await self. send_template_card(
            "open_id", open_id, WRSBOT_WELCOME_CARD_ID,{"open_id": open_id}
        )


    # todo: write the workflow as comment
    # todo: write functionalities 
    # todo: For developer's benefit should I store all the lark cards in the directory for viewing purpose? Think about this question. 