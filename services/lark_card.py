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

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contact import ContactService

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
WRSBOT_COMMAND_LIST_ID = os.getenv("WRSBOT_COMMAND_LIST_ID", "AAqtjqFzhM57P")

WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID = os.getenv("WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID", "AAqt8pubhP0Br")
WRSBOT_BINDING_FEEDBACK_SUCCESS_CARD_ID = os.getenv("WRSBOT_BINDING_FEEDBACK_SUCCESS_CARD_ID", "AAqtVxQCOa3D8")
WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID = os.getenv("WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID", "AAqtwzfERE7gi")
WRSBOT_ADMIN_VIEW_CARD_ID = os.getenv("WRSBOT_ADMIN_VIEW_CARD_ID", "AAqtVhgEFc7gG")
WRSBOT_ADMIN_REPORT_SUMMARY_CHECK_CARD_ID = os.getenv("WRSBOT_ADMIN_REPORT_SUMMARY_CHECK_CARD_ID", "AAqtVXND6ex3t") #Report Submission check. triggered when starting weekly report summary when not everyone in the department is submitted their report 
WRSBOT_CARD_USER_VIEW_ID = os.getenv("WRSBOT_CARD_USER_VIEW_ID", "AAqtDwcOM8ogx")
# WRSBOT_CARD_ID = os.getenv("", "")



class LarkCardService:
    def __init__(self, lark_api):
        self.lark_api = lark_api
        self.contact_service: "ContactService | None" = None

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

        # ──Functionalities testing card action callback ─────────────────────────────────────────────────
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
        # ──WRSbot card action callback ─────────────────────────────────────────────────

        # ── WRSbot welcome card actions ──────────────────────────────────────────

        if action_key == "return_to_welcome_page":
            logger.info("[Lark_card] ← 返回主菜单")
            return P2CardActionTriggerResponse({
                "card": {
                    "type": "template",
                    "data": {
                        "template_id": WRSBOT_WELCOME_CARD_ID,
                        "template_variable": {"open_id": open_id},
                    },
                }
            })

        if action_key == "wrsbot_command_list":
            logger.info("[Lark_card] 指令列表")
            is_admin = self.contact_service is not None and self.contact_service.is_manager(open_id)
            return P2CardActionTriggerResponse({
                "card": {
                    "type": "template",
                    "data": {
                        "template_id": WRSBOT_COMMAND_LIST_ID,
                        "template_variable": {
                            "open_id": open_id,
                            "role_label": "部门负责人" if is_admin else "团队成员",
                            "cmd_list": _MANAGER_CMD_LIST if is_admin else _EMPLOYEE_CMD_LIST,
                        },
                    },
                }
            })

        if action_key == "wrsbot_help":
            logger.info("[Lark_card] 帮助")
            return P2CardActionTriggerResponse({"card": {"type": "raw", "data": _HELP_CARD}})

        if action_key == "wrsbot_start":
            logger.info("[Lark_card] 开始使用")
            is_admin = self.contact_service is not None and self.contact_service.is_manager(open_id)
            if is_admin:
                from ..utils.env_config import get_dept_folder_token
                managed = self._get_managed_depts(open_id)
                all_bound = bool(managed) and all(
                    bool(get_dept_folder_token(getattr(e["dept"], "open_department_id", "") or ""))
                    for e in managed
                )
                if not all_bound:
                    return P2CardActionTriggerResponse(
                        {"card": self._build_not_binding_card_data(open_id)}
                    )
                return P2CardActionTriggerResponse({
                    "card": {
                        "type": "template",
                        "data": {
                            "template_id": WRSBOT_ADMIN_VIEW_CARD_ID,
                            "template_variable": {"open_id": open_id},
                        },
                    }
                })
            return P2CardActionTriggerResponse({
                "card": {
                    "type": "template",
                    "data": {
                        "template_id": WRSBOT_CARD_USER_VIEW_ID,
                        "template_variable": {"open_id": open_id},
                    },
                }
            })

        if action_key == "start_binding":
            logger.info("[Lark_card] 开始绑定")
            dept_id = action_value.get("dept_id", "")
            return P2CardActionTriggerResponse({
                "card": {
                    "type": "template",
                    "data": {
                        "template_id": WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID,
                        "template_variable": {"open_id": open_id, "dept_id": dept_id},
                    },
                }
            })

        if action_key == "send_folder_url":
            logger.info("[Lark_card] 提交文件夹链接")
            from ..utils.env_config import extract_folder_token_from_url, set_dept_folder_token

            form_value: dict = action.form_value or {} if action is not None else {}
            url = str(form_value.get("folder_url_input", "")).strip()
            dept_id = action_value.get("dept_id", "")
            logger.info(f"[Lark_card][send_folder_url] form_value={form_value} url={repr(url)}")

            # Resolve dept_id for single-dept case (template card doesn't carry it)
            if not dept_id:
                managed = self._get_managed_depts(open_id)
                if managed:
                    dept_id = getattr(managed[0]["dept"], "open_department_id", "") or ""

            if not url:
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": "请粘贴飞书文件夹链接",
                        "i18n": {"zh_cn": "请粘贴飞书文件夹链接", "en_us": "Please paste a Feishu folder URL."},
                    },
                    "card": self._build_not_binding_card_data(open_id, failed=True),
                })

            token = extract_folder_token_from_url(url)
            if not token:
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": "链接格式不正确，请粘贴飞书文件夹链接（如：https://xxx.feishu.cn/drive/folder/xxx）",
                        "i18n": {
                            "zh_cn": "链接格式不正确，请粘贴飞书文件夹链接",
                            "en_us": "Invalid URL. Expected: https://xxx.feishu.cn/drive/folder/xxx",
                        },
                    },
                    "card": self._build_not_binding_card_data(open_id, failed=True),
                })

            if not dept_id:
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": "无法确定绑定部门，请联系管理员",
                        "i18n": {"zh_cn": "无法确定绑定部门，请联系管理员", "en_us": "Cannot determine department. Contact admin."},
                    },
                    "card": self._build_not_binding_card_data(open_id, failed=True),
                })

            set_dept_folder_token(dept_id, token)
            logger.info(f"[Lark_card] 文件夹绑定成功: dept={dept_id} token={token}")
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "success",
                    "content": "绑定成功！",
                    "i18n": {"zh_cn": "绑定成功！", "en_us": "Binding successful!"},
                },
                "card": {
                    "type": "template",
                    "data": {
                        "template_id": WRSBOT_BINDING_FEEDBACK_SUCCESS_CARD_ID,
                        "template_variable": {"open_id": open_id},
                    },
                },
            })

        if action_key == "rescan_org_tree":
            logger.info("[Lark_card] 重新扫描通讯录")
            if self.contact_service:
                asyncio.create_task(self.contact_service._do_refresh())
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "info",
                    "content": "正在重新扫描，请稍后点击重新扫描查看最新状态",
                    "i18n": {
                        "zh_cn": "正在重新扫描，请稍后点击重新扫描查看最新状态",
                        "en_us": "Rescanning, please click Rescan again shortly.",
                    },
                },
                "card": self._build_not_binding_card_data(open_id),
            })

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
            logger.warning("[Lark_card] WELCOME_CARD_ID 未设置，跳过发送欢迎卡片")
            return False
        return await self.send_template_card(
            "open_id", open_id, WELCOME_CARD_ID, {"open_id": open_id}
        )

    async def send_alarm_card(self, receive_id_type: str, receive_id: str) -> bool:
        """Send the alarm card with current UTC+8 timestamp."""
        if not ALERT_CARD_ID:
            logger.warning("[Lark_card] ALERT_CARD_ID 未设置，跳过发送告警卡片")
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
            logger.warning("[Lark_card] WRSBOT_WELCOME_CARD_ID not set, skipped sending welcome card")
            return False
        return await self. send_template_card(
            "open_id", open_id, WRSBOT_WELCOME_CARD_ID,{"open_id": open_id}
        )


    # ── Binding status helpers (sync — reads from cached org tree) ──────────────

    def _get_managed_depts(self, open_id: str) -> list[dict]:
        """Return org tree entries where this user is the department leader."""
        if not self.contact_service:
            return []
        org_tree = self.contact_service._org_tree_cache or []
        return [
            e for e in org_tree
            if e.get("manager") and e["manager"].open_id == open_id
        ]

    def _build_not_binding_card_data(self, open_id: str, *, failed: bool = False) -> dict:
        """Return the card 'type'+'data' dict for the binding status card.

        Single dept → template card (card builder) with computed variables.
        Multi dept  → inline JSON card with one row per department.
        All bound + not failed → success card instead.
        """
        from ..utils.env_config import get_dept_folder_token

        managed = self._get_managed_depts(open_id)

        # All depts bound and no failure → show success card
        if not failed and managed and all(
            bool(get_dept_folder_token(getattr(e["dept"], "open_department_id", "") or ""))
            for e in managed
        ):
            return {
                "type": "template",
                "data": {
                    "template_id": WRSBOT_BINDING_FEEDBACK_SUCCESS_CARD_ID,
                    "template_variable": {"open_id": open_id},
                },
            }

        if not managed:
            return {
                "type": "template",
                "data": {
                    "template_id": WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID,
                    "template_variable": {
                        "dept_name": "未知部门",
                        "dept_num": 0,
                        "unreg_dept_num": 0,
                        "binding_status": "绑定失败" if failed else "未绑定",
                        "heading_colour_code": "red" if failed else "orange",
                        "doc_is_not_binded": True,
                    },
                },
            }

        if len(managed) == 1:
            dept = managed[0]["dept"]
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            token = get_dept_folder_token(open_dept_id)
            is_bound = bool(token)
            colour = "red" if failed else ("green" if is_bound else "orange")
            status = "绑定失败" if failed else ("已绑定" if is_bound else "未绑定")
            return {
                "type": "template",
                "data": {
                    "template_id": WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID,
                    "template_variable": {
                        "dept_name": dept.name or "",
                        "dept_num": 1,
                        "unreg_dept_num": 0 if (is_bound and not failed) else 1,
                        "binding_status": status,
                        "heading_colour_code": colour,
                        "doc_is_not_binded": not is_bound,
                    },
                },
            }

        return {"type": "raw", "data": _build_multi_dept_binding_card(managed)}

    # todo: write the workflow as comment
    # todo: write functionalities
    # todo: For developer's benefit should I store all the lark cards in the directory for viewing purpose? Think about this question.



# ── Multi-dept binding card builder ─────────────────────────────────────────
# Used when a manager leads more than one department.
# Builds inline JSON — one dept row per department entry.

def _build_multi_dept_binding_card(managed_depts: list[dict]) -> dict:
    from ..utils.env_config import get_dept_folder_token

    dept_rows = []
    total_unbound = 0

    for entry in managed_depts:
        dept = entry["dept"]
        open_dept_id = getattr(dept, "open_department_id", "") or ""
        token = get_dept_folder_token(open_dept_id)
        is_bound = bool(token)
        if not is_bound:
            total_unbound += 1
        dept_name    = dept.name or ""
        status_label = "已绑定" if is_bound else "未绑定"
        colour       = "green"  if is_bound else "orange"

        dept_rows.append({
            "tag": "column_set",
            "background_style": "grey-50",
            "horizontal_spacing": "8px",
            "horizontal_align": "left",
            "margin": "0px 0px 0px 0px",
            "columns": [{
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_spacing": "8px",
                "horizontal_align": "left",
                "vertical_align": "top",
                "elements": [{
                    "tag": "column_set",
                    "horizontal_spacing": "8px",
                    "horizontal_align": "left",
                    "margin": "0px 0px 0px 0px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 3,
                            "vertical_spacing": "8px",
                            "horizontal_align": "left",
                            "vertical_align": "top",
                            "elements": [{
                                "tag": "markdown",
                                "content": f"### {dept_name}部门周报文件夹   <text_tag color='{colour}'> {status_label} </text_tag>",
                                "text_align": "left",
                                "text_size": "heading",
                                "margin": "0px 0px 0px 0px",
                            }],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "vertical_spacing": "8px",
                            "horizontal_align": "left",
                            "vertical_align": "top",
                            "elements": [{
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "重新绑定"},
                                "type": "default",
                                "width": "fill",
                                "size": "medium",
                                "disabled": not is_bound,
                                "disabled_tips": {
                                    "tag": "plain_text",
                                    "content": "文件夹还未绑定",
                                },
                                "behaviors": [{
                                    "type": "callback",
                                    "value": {"action": "start_binding", "dept_id": open_dept_id},
                                }],
                                "margin": "0px 0px 0px 0px",
                            }],
                        },
                    ],
                }],
            }],
        })

    total = len(managed_depts)
    header_colour  = "green" if total_unbound == 0 else "orange"
    header_status  = f"{total - total_unbound}/{total} 已绑定"

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "周报文件夹配置",
                "i18n_content": {"en_us": "Weekly Report Folder Configuration"},
            },
            "subtitle": {"tag": "plain_text", "content": header_status},
            "template": header_colour,
            "padding": "12px 12px 12px 12px",
        },
        "body": {
            "direction": "horizontal",
            "horizontal_spacing": "8px",
            "vertical_spacing": "8px",
            "padding": "12px 12px 12px 12px",
            "elements": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "返回主界面"},
                    "type": "primary",
                    "width": "default",
                    "size": "medium",
                    "behaviors": [{"type": "callback", "value": {"action": "return_to_welcome_page"}}],
                    "margin": "0px 0px 0px 0px",
                },
                {"tag": "hr", "margin": "0px 0px 0px 0px"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": "部门绑定状态",
                        "i18n_content": {"en_us": "Department binding status"},
                        "text_size": "notation",
                    },
                    "margin": "0px 0px 0px 0px",
                },
                *dept_rows,
                {"tag": "hr", "margin": "0px 0px 0px 0px"},
                {
                    "tag": "column_set",
                    "horizontal_spacing": "8px",
                    "horizontal_align": "left",
                    "margin": "0px 0px 0px 0px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "vertical_spacing": "8px",
                            "elements": [{
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "重新扫描", "i18n_content": {"en_us": "Rescan"}},
                                "type": "default",
                                "width": "fill",
                                "size": "medium",
                                "behaviors": [{"type": "callback", "value": {"action": "rescan_org_tree"}}],
                                "margin": "0px 0px 0px 0px",
                            }],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "vertical_spacing": "8px",
                            "elements": [{
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "开始绑定 ->", "i18n_content": {"en_us": "Start binding ->"}},
                                "type": "primary_filled",
                                "width": "fill",
                                "size": "medium",
                                "behaviors": [{"type": "callback", "value": {"action": "start_binding"}}],
                                "margin": "0px 0px 0px 0px",
                            }],
                        },
                    ],
                },
            ],
        },
    }


# ── Command list strings ─────────────────────────────────────────────────────

_EMPLOYEE_CMD_LIST = (
    "• **查看我的提交状态** — 确认本周是否已提交\n"
    "• **打开我的周报文档** — 跳转至填写页面\n"
    "• **帮助** — 查看使用说明"
)

_MANAGER_CMD_LIST = (
    "• **查看团队提交情况** — 本周 N / Total 人已提交\n"
    "• **催交报告** — 向未提交成员发送提醒卡片\n"
    "• **生成周报总结** — 启动 LLM 汇总流程\n"
    "• **重新生成** — 更多人提交后重跑汇总\n"
    "• **写入飞书文档** — 将草稿写入绑定文件夹\n"
    "• **查看我的提交状态**\n"
    "• **帮助**"
)


# ── Inline card definitions ──────────────────────────────────────────────────
# Used for in-place updates returned directly from handle_card_action_sync.
# Keep card JSON here, not inside handlers, so handlers stay readable.

_HELP_CARD = {
    "schema": "2.0",
    "header": {
        "title": {"tag": "plain_text", "content": "WRSbot · 使用帮助"},
        "template": "blue",
    },
    "body": {
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "WRSbot 帮助部门负责人自动汇总团队周报，生成管理风格的部门周报总结。\n\n"
                    "**基本流程**\n"
                    "1. 管理员完成文件夹绑定配置\n"
                    "2. 团队成员填写个人周报\n"
                    "3. 管理员触发周报生成\n"
                    "4. 审阅草稿后一键写入文档\n\n"
                    "**私聊触发**\n"
                    "直接向 WRSbot 发送 `你好` 或 `Hello` 打开主菜单。\n\n"
                    "如需进一步帮助，请联系系统管理员。"
                ),
            },
            {"tag": "hr"},
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "← 返回主菜单"},
                "type": "default",
                "behaviors": [
                    {"type": "callback", "value": {"action": "return_to_welcome_page"}}
                ],
            },
        ]
    },
}


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
