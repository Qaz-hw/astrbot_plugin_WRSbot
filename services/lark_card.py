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
import uuid
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contact import ContactService

from astrbot.api import logger
from .lark_context import set_active_lark_api
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.event.callback.processor import P2CardActionTriggerProcessor

# ──Connection Testing cards template IDs ────────────────────────────────────────────────────────
# Set via environment variables. Default values are test card IDs.
# Create cards in Feishu card builder and update these IDs before production.
WELCOME_CARD_ID = os.getenv("WRSBOT_WELCOME_CARD_ID", "AAqWG7tpchJNE")
ALERT_CARD_ID = os.getenv("ALERT_CARD_ID", "AAqtuTeNqRxte")
ALERT_RESOLVED_CARD_ID = os.getenv("ALERT_RESOLVED_CARD_ID", "AAqtuTeLM56jm")

# ──WRSbot cards template IDs ────────────────────────────────────────────────────────
# Set via environment variables. Default values are test card IDs.
WRSBOT_WELCOME_CARD_ID = os.getenv("WRSBOT_WELCOME_CARD_ID", "AAqWG7tpchJNE")
WRSBOT_COMMAND_LIST_ID = os.getenv("WRSBOT_COMMAND_LIST_ID", "AAqWG7LYdWWZ8")
WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID = os.getenv("WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID", "AAqWGzF0skSTw")
WRSBOT_BINDING_FEEDBACK_SUCCESS_CARD_ID = os.getenv("WRSBOT_BINDING_FEEDBACK_SUCCESS_CARD_ID", "AAqtVxQCOa3D8")
WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID = os.getenv("WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID", "AAqWGz3aB4KYM")
WRSBOT_ADMIN_VIEW_CARD_ID = os.getenv("WRSBOT_ADMIN_VIEW_CARD_ID", "AAqWGzpYUSdDc")
WRSBOT_ADMIN_REPORT_SUMMARY_CHECK_CARD_ID = os.getenv("WRSBOT_ADMIN_REPORT_SUMMARY_CHECK_CARD_ID", "AAqtVXND6ex3t") #Report Submission check. triggered when starting weekly report summary when not everyone in the department is submitted their report 
WRSBOT_CARD_USER_VIEW_ID = os.getenv("WRSBOT_CARD_USER_VIEW_ID", "AAqWGzJx4vkWm")
WRSBOT_SUMMARY_REWRITE_CARD_ID = os.getenv("WRSBOT_SUMMARY_REWRITE_CARD_ID", "AAqWGzI8irmU6")
WRSBOT_STYLE_CONFIG_CARD_ID = os.getenv("WRSBOT_STYLE_CONFIG_CARD_ID", "AAqWGzaxnlUyj")

# ──PMbot cards template IDs ────────────────────────────────────────────────────────
PMBOT_DAILY_REPORT_PM_UPDATE = os.getenv("PMBOT_DAILY_REPORT_PM_UPDATE", "AAqWlcPrTPRtx")
PMBOT_DAILY_REPORT_BINDING_CARD_ID = os.getenv("PMBOT_DAILY_REPORT_BINDING_CARD_ID", "AAqWl8hXjOWeZ")
# WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID = os.getenv("WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID", "AAqPYaf47fxzP")

class LarkCardService:
    def __init__(self, lark_api):
        self.lark_api = lark_api
        # An AstrBot process can host more than one Lark app.  open_ids are
        # app-scoped, so never assume the first startup adapter owns a later
        # inbound event.  Record the adapter that actually received each user.
        self._lark_api_by_open_id: dict[str, object] = {}
        self._lark_api_by_streaming_card_id: dict[str, object] = {}
        self.contact_service: "ContactService | None" = None
        self._admin_pipeline_fn    = None
        self._user_pipeline_fn     = None
        self._generate_pipeline_fn = None
        self._rewrite_pipeline_fn  = None
        self._submit_pipeline_fn   = None
        self._reminder_pipeline_fn = None
        self._open_style_config_fn = None
        self._save_style_fn        = None
        self._view_doc_fn          = None

    def bind_lark_api(self, open_id: str, lark_api) -> None:
        """Associate an app-scoped user ID with the adapter that received it."""
        if open_id and lark_api:
            self._lark_api_by_open_id[open_id] = lark_api

    def get_lark_api(self, open_id: str = "", lark_api=None):
        """Return the explicitly supplied or event-owned Lark API client."""
        return lark_api or self._lark_api_by_open_id.get(open_id) or self.lark_api

    def set_user_pipeline(self, fn) -> None:
        """Signature: async fn(open_id: str) -> None"""
        self._user_pipeline_fn = fn

    def set_admin_pipeline(self, fn) -> None:
        """Signature: async fn(open_id: str) -> None"""
        self._admin_pipeline_fn = fn

    def set_report_pipelines(self, *, generate, rewrite, submit) -> None:
        """Register the three report generation pipelines.

        generate: async fn(open_id: str) -> None
        rewrite:  async fn(open_id: str, style_input: str) -> None
        submit:   async fn(open_id: str) -> None
        """
        self._generate_pipeline_fn = generate
        self._rewrite_pipeline_fn  = rewrite
        self._submit_pipeline_fn   = submit

    def set_reminder_pipeline(self, fn) -> None:
        """Signature: async fn(manager_open_id: str) -> None"""
        self._reminder_pipeline_fn = fn

    def set_style_pipelines(self, *, open_config, save) -> None:
        """Register manager-style pipelines.

        open_config: async fn(open_id: str) -> None
                     Sends the style config card pre-filled with saved values.
        save:        async fn(open_id, tone_tags, custom_instructions,
                              writing_samples) -> None
                     Persists form_value to sp.
        """
        self._open_style_config_fn = open_config
        self._save_style_fn        = save

    def set_view_doc_pipeline(self, fn) -> None:
        """Signature: async fn(open_id: str) -> None

        Resolves the caller's dept folder URL and sends it as a text DM.
        Triggered by the view_doc card action.
        """
        self._view_doc_fn = fn

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
            if hasattr(platform, "lark_api") and hasattr(platform, "event_handler") and hasattr(
                platform.event_handler, "_callback_processor_map"
            ):
                # Bind this processor to the same adapter that owns this
                # websocket connection.  A card callback has no AstrBot event
                # object, so this closure is the only reliable source client.
                processor = P2CardActionTriggerProcessor(
                    lambda data, api=platform.lark_api: self.handle_card_action_sync(
                        data, lark_api=api
                    )
                )
                # lark-oapi has used both names across callback transport / SDK
                # versions.  Register both aliases against the same processor.
                platform.event_handler._callback_processor_map[
                    "p2.card.action.trigger"
                ] = processor
                platform.event_handler._callback_processor_map[
                    "card.action.trigger"
                ] = processor
                logger.info("✅ 飞书卡片动作处理器已注入事件分发器")

    # ── Card action callback ─────────────────────────────────────────────────

    def handle_card_action_sync(
        self, data: P2CardActionTrigger, *, lark_api=None
    ) -> P2CardActionTriggerResponse:
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
        message_id: str = (
            data.event.context.open_message_id or ""
            if data.event.context is not None
            else ""
        )
        self.bind_lark_api(open_id, lark_api)
        set_active_lark_api(lark_api)
        if self.contact_service:
            self.contact_service.bind_lark_api(open_id, lark_api)

        # ──Functionalities testing card action callback ─────────────────────────────────────────────────

        # [TEST] Sends a new alarm card as a DM to the clicking user.
        # open_id: Feishu user ID of the person who clicked the button.
#        if action_key == "send_alarm":
#            logger.info("✅ 飞书卡片动作已在处理")
#            if self.lark_api:
#                asyncio.create_task(self.send_alarm_card("open_id", open_id))
#            return P2CardActionTriggerResponse({})
#
#        # [TEST] Marks an alarm as resolved and replaces the card with a resolved summary.
#        # notes_input: text typed by the resolver in the card's notes field.
#        # alarm_time:  original alarm timestamp carried in the button's action_value (set when alarm was created).
#        # complete_time: current UTC+8 time, generated here at resolution.
#        if action_key == "complete_alarm":
#            logger.info("✅ 飞书卡片动作已在处理")
#            form_value: dict = action.form_value or {} if action is not None else {}
#            notes = str(form_value.get("notes_input", ""))
#            alarm_time: str = action_value.get("time", "")
#            complete_time = datetime.now(timezone(timedelta(hours=8))).strftime(
#                "%Y-%m-%d %H:%M:%S (UTC+8)"
#            )
#            return P2CardActionTriggerResponse(
#                {
#                    "toast": {
#                        "type": "info",
#                        "content": "已处理完成！",
#                        "i18n": {"zh_cn": "已处理完成！", "en_us": "Resolved!"},
#                    },
#                    "card": {
#                        "type": "template",
#                        "data": {
#                            "template_id": ALERT_RESOLVED_CARD_ID,
#                            "template_variable": {
#                                "alarm_time": alarm_time,
#                                "open_id": open_id,
#                                "complete_time": complete_time,
#                                "notes": notes,
#                            },
#                        },
#                    },
#                }
#            )
        # ──WRSbot card action callback ─────────────────────────────────────────────────

        # ── WRSbot welcome card actions ──────────────────────────────────────────

        # Replaces the current card in-place with the main welcome card.
        # open_id: passed as template variable so the welcome card can personalise the greeting.
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

        # ── PMbot welcome card action ────────────────────────────────────────────
        # PMbot stores its daily-report folder separately from WRSbot's weekly
        # report folder.  The daily binding card is shown until that folder exists.
        if action_key == "PMbot_start":
            logger.info("[Lark_card] PMbot 开始使用")
            from ..utils.env_config import get_dept_daily_report_folder_token

            is_admin = self.contact_service is not None and self.contact_service.is_manager(open_id)
            if not is_admin:
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": "仅部门负责人可以使用 PMbot",
                        "i18n": {"zh_cn": "仅部门负责人可以使用 PMbot"},
                    }
                })

            managed = self._get_managed_depts(open_id)
            all_bound = bool(managed) and all(
                bool(
                    get_dept_daily_report_folder_token(
                        getattr(entry["dept"], "open_department_id", "") or ""
                    )
                )
                for entry in managed
            )

            if not all_bound:
                # Like WRSbot's start gate, bind the first missing department
                # before entering the main PMbot card.  The daily binding uses
                # its own token namespace and cannot affect weekly bindings.
                unbound = next(
                    (
                        entry for entry in managed
                        if not get_dept_daily_report_folder_token(
                            getattr(entry["dept"], "open_department_id", "") or ""
                        )
                    ),
                    None,
                )
                if not unbound:
                    return P2CardActionTriggerResponse({
                        "toast": {
                            "type": "error",
                            "content": "未找到您管理的部门，无法绑定日报文件夹",
                            "i18n": {"zh_cn": "未找到您管理的部门，无法绑定日报文件夹"},
                        }
                    })

                dept = unbound["dept"]
                dept_id = getattr(dept, "open_department_id", "") or ""
                return P2CardActionTriggerResponse({
                    "card": {
                        "type": "template",
                        "data": {
                            "template_id": WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID,
                            "template_variable": {
                                "open_id": open_id,
                                "dept_id": dept_id,
                                "dept_name": dept.name or "",
                            },
                        },
                    }
                })

            return P2CardActionTriggerResponse({
                "card": {
                    "type": "template",
                    "data": {
                        "template_id": PMBOT_DAILY_REPORT_PM_UPDATE,
                        "template_variable": {"open_id": open_id},
                    },
                }
            })

        # ── PMbot daily-report folder binding ───────────────────────────────────
        # The PMbot binding card (AAqWl8hXjOWeZ) submits this action.  It saves to
        # DEPT_DAILY_REPORT_FOLDER_<department-id>, never DEPT_FOLDER_<...>, so
        # daily-report binding cannot overwrite the existing weekly-report folder.
        # Supported card input names: daily_report_folder_url_input (preferred)
        # and folder_url_input (compatible with the existing weekly-card field).
        if action_key == "bind_new_token_dr":
            logger.info("[Lark_card] PMbot 绑定日报文件夹")
            from ..utils.env_config import (
                extract_folder_token_from_url,
                set_dept_daily_report_folder_token,
            )

            form_value: dict = action.form_value or {} if action is not None else {}
            url = str(
                form_value.get("daily_report_folder_url_input")
                or form_value.get("folder_url_input")
                or ""
            ).strip()
            dept_id = str(action_value.get("dept_id", "") or "")

            if not dept_id:
                managed = self._get_managed_depts(open_id)
                if managed:
                    dept_id = getattr(managed[0]["dept"], "open_department_id", "") or ""

            if not url:
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": "请粘贴日报文件夹链接",
                        "i18n": {"zh_cn": "请粘贴日报文件夹链接"},
                    }
                })

            token = extract_folder_token_from_url(url)
            if not token:
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": "链接格式不正确，请粘贴飞书文件夹链接",
                        "i18n": {"zh_cn": "链接格式不正确，请粘贴飞书文件夹链接"},
                    }
                })

            if not dept_id:
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": "无法确定绑定部门，请联系管理员",
                        "i18n": {"zh_cn": "无法确定绑定部门，请联系管理员"},
                    }
                })

            set_dept_daily_report_folder_token(dept_id, token)
            logger.info(f"[Lark_card] PMbot 日报文件夹绑定成功: dept={dept_id} token={token}")
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "success",
                    "content": "日报文件夹绑定成功！",
                    "i18n": {"zh_cn": "日报文件夹绑定成功！"},
                },
                "card": {
                    "type": "template",
                    "data": {
                        "template_id": PMBOT_DAILY_REPORT_PM_UPDATE,
                        "template_variable": {"open_id": open_id},
                    },
                },
            })

        # Renders the command list card, tailored to the user's role.
        # is_admin:   checked synchronously against the cached org tree leader set — no await needed.
        # role_label: display label shown in the card header ("部门负责人" or "团队成员").
        # cmd_list:   markdown string of available commands; manager gets extra report-generation commands.
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

        # Returns an inline JSON card (not a template) with static usage instructions.
        # Uses "raw" type so the full card body is sent directly — no template variables needed.
        if action_key == "wrsbot_help":
            logger.info("[Lark_card] 帮助")
            return P2CardActionTriggerResponse({"card": {"type": "raw", "data": _HELP_CARD}})

        # Main entry point — routes to admin or employee view based on role.
        # Managers must have all their departments' folder tokens saved in .env before
        # the admin dashboard is shown; if any dept is unbound, the binding card is shown instead.
        # managed:   list of org tree entries where this user is the department leader.
        # all_bound: True only when every managed dept has a folder token stored in .env.
        # Employees skip the binding check and go directly to the user view.
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
                # TEMP TEST OVERRIDE (Justin): allow the manager dashboard path
                # even while the test contact tree/folder binding is incomplete.
                # TODO(public launch): remove WRSBOT_TEST_ADMIN_OPEN_IDS and this
                # bypass; authorization must then rely only on Feishu leaders.
                is_test_admin = (
                    self.contact_service is not None
                    and self.contact_service.is_test_admin(open_id)
                )
                if is_test_admin:
                    logger.warning("[Lark_card] 临时 Justin 测试权限：跳过文件夹绑定门槛")
                    all_bound = True
                if not all_bound:
                    return P2CardActionTriggerResponse(
                        {"card": self._build_not_binding_card_data(open_id)}
                    )
                # Pipeline is async — schedule it and return immediately with a loading toast.
                # The pipeline will send the admin view card as a DM once data is ready.
                if self._admin_pipeline_fn:
                    asyncio.create_task(self._admin_pipeline_fn(open_id, message_id))
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "info",
                        "content": "正在获取提交情况，稍后将发送管理视图卡片...",
                        "i18n": {
                            "zh_cn": "正在获取提交情况，稍后将发送管理视图卡片...",
                            "en_us": "Loading submission status, admin card coming shortly...",
                        },
                    }
                })
            if self._user_pipeline_fn:
                asyncio.create_task(self._user_pipeline_fn(open_id, message_id))
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "info",
                    "content": "正在获取提交情况，稍后将发送用户视图卡片...",
                    "i18n": {
                        "zh_cn": "正在获取提交情况，稍后将发送用户视图卡片...",
                        "en_us": "Loading your submission status...",
                    },
                }
            })

        # Opens the folder binding tutorial card for the target department.
        # dept_id: open_department_id carried from the button's action_value — tells the tutorial
        #          which dept is being bound. Empty for single-dept managers (resolved later in
        #          send_folder_url by looking up the manager's only managed dept).
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

        # Handles the two optional URLs in the binding tutorial.  Either URL
        # can be submitted alone; if both are valid, both folders are bound.
        # dept_id: open_department_id carried from the tutorial card's action_value; empty for single-dept
        #          managers (template card can't carry it), so we fall back to their only managed dept.
        # Each token is stored in its own weekly/daily environment key.
        if action_key == "send_folder_url":
            from ..utils.env_config import (
                extract_folder_token_from_url,
                set_dept_daily_report_folder_token,
                set_dept_folder_token,
            )

            form_value: dict = action.form_value or {} if action is not None else {}
            weekly_url = str(
                form_value.get("WRfolder_url_input")
                or form_value.get("folder_url_input")  # old card compatibility
                or ""
            ).strip()
            daily_url = str(form_value.get("DRfolder_url_input") or "").strip()
            dept_id = action_value.get("dept_id", "")

            # Single-dept template card doesn't carry dept_id in action_value — derive from org tree.
            if not dept_id:
                managed = self._get_managed_depts(open_id)
                if managed:
                    dept_id = getattr(managed[0]["dept"], "open_department_id", "") or ""

            if not weekly_url and not daily_url:
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": "请至少粘贴一个周报或日报文件夹链接",
                        "i18n": {"zh_cn": "请至少粘贴一个周报或日报文件夹链接"},
                    },
                    "card": self._build_not_binding_card_data(open_id),
                })

            weekly_token = extract_folder_token_from_url(weekly_url) if weekly_url else None
            daily_token = extract_folder_token_from_url(daily_url) if daily_url else None
            invalid_types = []
            if weekly_url and not weekly_token:
                invalid_types.append("周报")
            if daily_url and not daily_token:
                invalid_types.append("日报")
            if invalid_types:
                invalid_label = "、".join(invalid_types)
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": f"{invalid_label}文件夹链接格式不正确，请检查后重试",
                        "i18n": {"zh_cn": f"{invalid_label}文件夹链接格式不正确，请检查后重试"},
                    },
                    "card": self._build_not_binding_card_data(open_id),
                })

            if not dept_id:
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": "无法确定绑定部门，请联系管理员",
                        "i18n": {"zh_cn": "无法确定绑定部门，请联系管理员", "en_us": "Cannot determine department. Contact admin."},
                    },
                    "card": self._build_not_binding_card_data(open_id),
                })

            bound_types = []
            if weekly_token:
                set_dept_folder_token(dept_id, weekly_token)
                bound_types.append("周报")
            if daily_token:
                set_dept_daily_report_folder_token(dept_id, daily_token)
                bound_types.append("日报")

            bound_label = "和".join(bound_types)
            logger.info(
                f"[Lark_card] 文件夹绑定成功: dept={dept_id} types={','.join(bound_types)}"
            )
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "success",
                    "content": f"{bound_label}文件夹绑定成功！",
                    "i18n": {"zh_cn": f"{bound_label}文件夹绑定成功！"},
                },
                "card": self._build_not_binding_card_data(open_id),
            })

        # Triggered from the binding-success card when the manager wants to re-bind.
        # Clears the existing folder token for this dept, then drops the user back into
        # the tutorial card so they can paste a new folder URL.
        # dept_id: open_department_id carried in action_value; falls back to the manager's
        #          only managed dept for single-dept template cards (same fallback as start_binding).
        if action_key == "bind_new_token":
            logger.info("[Lark_card] 重新绑定文件夹")
            from ..utils.env_config import delete_dept_folder_token

            dept_id = action_value.get("dept_id", "")
            if not dept_id:
                managed = self._get_managed_depts(open_id)
                if managed:
                    dept_id = getattr(managed[0]["dept"], "open_department_id", "") or ""

            if dept_id:
                delete_dept_folder_token(dept_id)
                logger.info(f"[Lark_card] 已清除绑定: dept={dept_id}")

            return P2CardActionTriggerResponse({
                "card": {
                    "type": "template",
                    "data": {
                        "template_id": WRSBOT_BINDFOLDER_TUTORIAL_CARD_ID,
                        "template_variable": {"open_id": open_id, "dept_id": dept_id},
                    },
                }
            })

        # Kicks off a background org tree refresh without blocking the card response.
        # create_task() schedules _do_refresh() on the running event loop; the handler returns
        # immediately with the current (pre-refresh) binding card so the user isn't left waiting.
        # The toast instructs the user to click Rescan again once the refresh has completed.
        if action_key == "rescan_org_tree":
            logger.info("[Lark_card] 重新扫描通讯录")
            if self.contact_service:
                asyncio.create_task(
                    self.contact_service._do_refresh(open_id, lark_api=lark_api)
                )
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

        # ── Report generation actions ─────────────────────────────────────────

        # Triggered from admin view card. Kicks off the full generation pipeline:
        # read content → LLM summarize → LLM rewrite → send text DM + action card.
        if action_key == "start_summary":
            logger.info("[Lark_card] 开始生成周报总结")
            if self._generate_pipeline_fn:
                asyncio.create_task(self._generate_pipeline_fn(open_id, message_id))
            return P2CardActionTriggerResponse({"card": {"type": "raw", "data": _GENERATING_CARD}})

        # Triggered from the admin view card. Looks up this week's not_submitted
        # list and DMs each of those members the user_view_card as a nudge.
        # Toast returns immediately; the per-user DM fan-out runs in background.
        if action_key == "summary_reminder":
            logger.info("[Lark_card] 发送提交提醒")
            if self._reminder_pipeline_fn:
                asyncio.create_task(self._reminder_pipeline_fn(open_id))
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "info",
                    "content": "已向未提交成员发送提醒卡片",
                    "i18n": {
                        "zh_cn": "已向未提交成员发送提醒卡片",
                        "en_us": "Reminder cards sent to pending members.",
                    },
                }
            })


        # Branched flow for personal style:
        #   - First-time managers (no saved profile)  → editable template card
        #     directly (faster onboarding, no extra click)
        #   - Returning managers (has saved profile)  → inline display card
        #     showing the saved values + [返回 / 重新生成风格] buttons.
        # The display path exists because Feishu CardKit can't pre-fill the
        # template card's multi-select + textarea defaults via template
        # variables — so we surface the saved values via an inline JSON card.
        if action_key == "open_style_config":
            from ..utils.manager_style import get_manager_style_sync
            profile = get_manager_style_sync(open_id)
            has_saved = bool(
                profile.get("tone_tags")
                or profile.get("custom_instructions")
                or profile.get("writing_samples")
            )

            if has_saved:
                logger.info(f"[Lark_card] open_style_config → 展示已保存风格 (open_id={open_id})")
                return P2CardActionTriggerResponse({
                    "card": self._build_style_display_card_data(profile),
                })

            # No saved profile — go straight to the editable template.
            logger.info(f"[Lark_card] open_style_config → 直接打开编辑卡片 (无已保存) (open_id={open_id})")
            if not WRSBOT_STYLE_CONFIG_CARD_ID:
                logger.warning("[Lark_card] WRSBOT_STYLE_CONFIG_CARD_ID 未配置")
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": "风格配置卡片未配置，请联系管理员",
                        "i18n": {
                            "zh_cn": "风格配置卡片未配置，请联系管理员",
                            "en_us": "Style config card not configured.",
                        },
                    }
                })
            return P2CardActionTriggerResponse({
                "card": {
                    "type": "template",
                    "data": {
                        "template_id": WRSBOT_STYLE_CONFIG_CARD_ID,
                        "template_variable": {
                            "open_id":               open_id,
                            "tone_tags_preselected": profile["tone_tags"],
                            "custom_instructions":   profile["custom_instructions"],
                            "writing_samples":       profile["writing_samples"],
                            "updated_at":            profile["updated_at"] or "（尚未保存）",
                        },
                    },
                }
            })

        # Triggered by the "重新生成风格" button on the display card. Patches
        # to the editable style config template so the manager can submit a
        # new version. Same behavior the old open_style_config had — kept
        # separate so the display card stays the default entry point for
        # returning managers.
        if action_key == "edit_style_config":
            logger.info(f"[Lark_card] edit_style_config → 打开编辑卡片 (open_id={open_id})")
            if not WRSBOT_STYLE_CONFIG_CARD_ID:
                logger.warning("[Lark_card] WRSBOT_STYLE_CONFIG_CARD_ID 未配置")
                return P2CardActionTriggerResponse({
                    "toast": {
                        "type": "error",
                        "content": "风格配置卡片未配置，请联系管理员",
                    }
                })
            from ..utils.manager_style import get_manager_style_sync
            profile = get_manager_style_sync(open_id)
            return P2CardActionTriggerResponse({
                "card": {
                    "type": "template",
                    "data": {
                        "template_id": WRSBOT_STYLE_CONFIG_CARD_ID,
                        "template_variable": {
                            "open_id":               open_id,
                            "tone_tags_preselected": profile["tone_tags"],
                            "custom_instructions":   profile["custom_instructions"],
                            "writing_samples":       profile["writing_samples"],
                            "updated_at":            profile["updated_at"] or "（尚未保存）",
                        },
                    },
                }
            })

        # Sends the caller's department folder URL as a text DM. Resolves
        # dept via the cached org tree; falls back to a guidance message if
        # the folder isn't bound or the binding predates URL storage.
        # Card stays unchanged (toast-only); URL arrives as a separate DM
        # so it's clickable in Feishu.
        if action_key == "view_doc":
            logger.info("[Lark_card] 查看本周文档")
            if self._view_doc_fn:
                asyncio.create_task(self._view_doc_fn(open_id))
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "info",
                    "content": "已发送本周文档链接，请查收私聊",
                    "i18n": {
                        "zh_cn": "已发送本周文档链接，请查收私聊",
                        "en_us": "Folder link sent — check your DM.",
                    },
                }
            })

        # Two-stage patch for perceived speed:
        #   1. Return the admin view card NOW with "加载中…" placeholders →
        #      the card flips instantly, manager isn't stuck on the config card
        #   2. Schedule the admin pipeline async → patches the SAME message
        #      again with real submission status once check_submissions finishes
        if action_key == "cancel_update_style":
            logger.info("[Lark_card] 取消风格配置 → 即时回到管理视图（占位）+ 异步刷新")
            if self._admin_pipeline_fn:
                asyncio.create_task(
                    self._admin_pipeline_fn(open_id, message_id)
                )
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "info",
                    "content": "已取消",
                    "i18n": {"zh_cn": "已取消", "en_us": "Cancelled."},
                },
                "card": self._build_admin_view_loading_data(open_id),
            })


        if action_key == "save_manager_style":
            logger.info("[Lark_card] 保存个人风格")
            form_value: dict = action.form_value or {} if action is not None else {}

            # Feishu multi-select sends tone_tags_input in several possible
            # shapes depending on builder config:
            #   - list[str]                              (simplest case)
            #   - list[dict] with {value, text} entries  (rich option format)
            #   - str (comma-separated)                  (rare; some legacy templates)
            # Normalize all three to list[str] of the OPTION KEYS (must match
            # STYLE_TAG_CATALOG exactly downstream).
            raw_tags = form_value.get("tone_tags_input", [])
            logger.info(
                f"[Lark_card] save_manager_style raw tone_tags_input "
                f"(type={type(raw_tags).__name__}): {raw_tags!r}"
            )

            tone_tags: list[str] = []
            if isinstance(raw_tags, list):
                for item in raw_tags:
                    if isinstance(item, str):
                        tone_tags.append(item.strip())
                    elif isinstance(item, dict):
                        # Feishu select-option shape: prefer "value", fall back
                        # to "text" (some templates only set text).
                        v = item.get("value") or item.get("text") or ""
                        if v:
                            tone_tags.append(str(v).strip())
            elif isinstance(raw_tags, str):
                # Comma-separated fallback
                tone_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
            elif isinstance(raw_tags, dict):
                v = raw_tags.get("value") or raw_tags.get("text") or ""
                if v:
                    tone_tags = [str(v).strip()]

            tone_tags = [t for t in tone_tags if t]  # drop empties

            custom  = str(form_value.get("custom_instructions_input", "") or "")
            samples = str(form_value.get("writing_samples_input", "") or "")

            logger.info(
                f"[Lark_card] save_manager_style normalized → "
                f"tone_tags={tone_tags} custom_len={len(custom)} sample_len={len(samples)}"
            )

            if self._save_style_fn:
                asyncio.create_task(
                    self._save_style_fn(open_id, tone_tags, custom, samples)
                )
            # Same two-stage patch as cancel_update_style:
            #   1. Return admin view card NOW with "加载中…" placeholders
            #   2. Async admin pipeline patches the same message with real
            #      submission status once check_submissions finishes.
            if self._admin_pipeline_fn:
                asyncio.create_task(
                    self._admin_pipeline_fn(open_id, message_id)
                )
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "success",
                    "content": "风格已保存",
                    "i18n": {"zh_cn": "风格已保存", "en_us": "Style saved."},
                },
                "card": self._build_admin_view_loading_data(open_id),
            })

        # style_input: free-form style instructions from the manager's text input.
        # Reruns only the rewrite pass against the draft stored in sp.
        if action_key == "rewrite_summary":
            logger.info("[Lark_card] 重新改写周报总结")
            # Diagnostic: dump every value-carrying field on the action so we
            # can see exactly where Feishu placed the input. form_value is the
            # form-submit path; input_value is the direct-callback path; value
            # is the button's own action_value dict.
            logger.warning(
                "[Lark_card] rewrite_summary action dump: "
                f"tag={getattr(action, 'tag', None)!r} "
                f"name={getattr(action, 'name', None)!r} "
                f"value={getattr(action, 'value', None)!r} "
                f"form_value={getattr(action, 'form_value', None)!r} "
                f"input_value={getattr(action, 'input_value', None)!r} "
                f"option={getattr(action, 'option', None)!r} "
                f"options={getattr(action, 'options', None)!r}"
            )
            form_value: dict = action.form_value or {} if action is not None else {}
            # Try form_value first (form-submit path), then input_value (direct
            # callback path). Whichever is non-empty wins.
            style_input = str(form_value.get("style_input", "")).strip()
            if not style_input:
                style_input = str(getattr(action, "input_value", "") or "").strip()
            logger.info(f"[Lark_card] style_input: {style_input!r}")
            if self._rewrite_pipeline_fn:
                asyncio.create_task(self._rewrite_pipeline_fn(open_id, style_input))
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "info",
                    "content": "正在改写，完成后将发送新版本...",
                    "i18n": {
                        "zh_cn": "正在改写，完成后将发送新版本...",
                        "en_us": "Rewriting, new version coming shortly...",
                    },
                }
            })

        # Reads the draft from sp and writes it to bitable 部门总结 row.
        if action_key == "submit_summary":
            logger.info("[Lark_card] 写入周报总结到文件")
            if self._submit_pipeline_fn:
                asyncio.create_task(self._submit_pipeline_fn(open_id))
            return P2CardActionTriggerResponse({
                "toast": {
                    "type": "info",
                    "content": "正在写入，请稍候...",
                    "i18n": {
                        "zh_cn": "正在写入，请稍候...",
                        "en_us": "Writing to file, please wait...",
                    },
                }
            })

        return P2CardActionTriggerResponse({})

    
    # ── Functional testing cards sending ─────────────────────────────────────────────────────────

    async def patch_card(
        self, message_id: str, card_id: str, template_variables: dict, *, lark_api=None
    ) -> bool:
        """Replace an existing card in-place using the Feishu patch message API.

        message_id: open_message_id from the card action trigger context
        Uses the same template+variable format as send_template_card.
        """
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

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
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(content)
                .build()
            )
            .build()
        )
        api = self.get_lark_api(lark_api=lark_api)
        resp = await api.im.v1.message.apatch(req)
        if not resp.success():
            logger.warning(f"[LarkCard] patch_card 失败: code={resp.code} msg={resp.msg}")
            return False
        return True

    async def patch_inline_card(self, message_id: str, card_json: dict, *, lark_api=None) -> bool:
        """Patch an existing message in-place with a raw (non-template) card JSON.

        Patch API takes the card JSON directly as content — no {"type":"raw","data":...}
        wrapper. That wrapper is only valid in P2CardActionTriggerResponse, not here.
        """
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

        content = json.dumps(card_json, ensure_ascii=False)
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(PatchMessageRequestBody.builder().content(content).build())
            .build()
        )
        api = self.get_lark_api(lark_api=lark_api)
        resp = await api.im.v1.message.apatch(req)
        if not resp.success():
            logger.warning(f"[LarkCard] patch_inline_card 失败: code={resp.code} msg={resp.msg}")
            return False
        return True

    async def send_template_card(
        self,
        receive_id_type: str,
        receive_id: str,
        card_id: str,
        template_variables: dict,
        *,
        lark_api=None,
        reply_message_id: str = "",
    ) -> bool:
        """Send a template card, optionally replying through an incoming event's app."""
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
            self.get_lark_api(receive_id, lark_api),
            content=content,
            msg_type="interactive",
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            reply_message_id=reply_message_id,
        )

    # ── Testing functions, disabled when unused ─────────────────────────────────────────────────────────

#    async def send_welcome_card(self, open_id: str) -> bool:
#        """Send the welcome card to a user by open_id."""
#        if not WELCOME_CARD_ID:
#            logger.warning("[Lark_card] WELCOME_CARD_ID 未设置，跳过发送欢迎卡片")
#            return False
#        return await self.send_template_card(
#            "open_id", open_id, WELCOME_CARD_ID, {"open_id": open_id}
#        )
#
#    async def send_alarm_card(self, receive_id_type: str, receive_id: str) -> bool:
#        """Send the alarm card with current UTC+8 timestamp."""
#        if not ALERT_CARD_ID:
#            logger.warning("[Lark_card] ALERT_CARD_ID 未设置，跳过发送告警卡片")
#            return False
#        alarm_time = datetime.now(timezone(timedelta(hours=8))).strftime(
#            "%Y-%m-%d %H:%M:%S (UTC+8)"
#        )
#        return await self.send_template_card(
#            receive_id_type, receive_id, ALERT_CARD_ID, {"alarm_time": alarm_time}
#        )
#


    # ── WRSbot workflow cards sending ─────────────────────────────────────────────────────────

    async def send_wrsbot_welcome(
        self, *, open_id: str, reply_message_id: str, lark_api=None
    ) -> bool:
        """Reply to a received message with the WRSbot welcome card.

        ``lark_api`` should be the client carried by the incoming event.  A
        plugin-level cached client may belong to a different Lark adapter.
        """
        if not WRSBOT_WELCOME_CARD_ID:
            logger.warning("[Lark_card] WRSBOT_WELCOME_CARD_ID not set, skipped sending welcome card")
            return False
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        content = json.dumps(
            {
                "type": "template",
                "data": {
                    "template_id": WRSBOT_WELCOME_CARD_ID,
                    "template_variable": {"open_id": open_id},
                },
            },
            ensure_ascii=False,
        )
        return await LarkMessageEvent._send_im_message(
            self.get_lark_api(open_id, lark_api),
            content=content,
            msg_type="interactive",
            reply_message_id=reply_message_id,
        )

    # ── Folder binding status ────────────────────────────────────────────────

    def is_fully_bound(self, open_id: str) -> bool:
        """True if every department this user manages has a folder token in .env.

        Returns False if the user manages no departments at all (no point
        showing a success card to a non-manager).
        """
        from ..utils.env_config import get_dept_folder_token

        managed = self._get_managed_depts(open_id)
        if not managed:
            return False
        return all(
            bool(get_dept_folder_token(getattr(e["dept"], "open_department_id", "") or ""))
            for e in managed
        )

    async def send_binding_success_card(
        self, open_id: str, *, lark_api=None, reply_message_id: str = ""
    ) -> bool:
        """Send the binding-success card, optionally as a reply to an inbound message."""
        managed = self._get_managed_depts(open_id)
        if not managed:
            dept_name = "未知部门"
        elif len(managed) == 1:
            dept_name = managed[0]["dept"].name or ""
        else:
            dept_name = "所有部门"
        return await self.send_template_card(
            "open_id", open_id, WRSBOT_BINDING_FEEDBACK_SUCCESS_CARD_ID,
            {"open_id": open_id, "dept_name": dept_name},
            lark_api=lark_api,
            reply_message_id=reply_message_id,
        )

    async def send_not_binding_card(
        self, open_id: str, *, lark_api=None, reply_message_id: str = ""
    ) -> None:
        """Send the folder-binding status card to a user as a DM.

        Routes between single-dept template card and multi-dept inline JSON
        card via _build_not_binding_card_data. Note that _build_not_binding_card_data
        will *itself* return a success card if everything is bound — callers
        who specifically want the not-binding view should gate this with
        `not is_fully_bound(open_id)` first.
        """
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        card_data = self._build_not_binding_card_data(open_id)
        card_type = card_data.get("type", "")
        data = card_data.get("data", {})

        if card_type == "template":
            await self.send_template_card(
                "open_id", open_id,
                data["template_id"],
                data.get("template_variable", {}),
                lark_api=lark_api,
                reply_message_id=reply_message_id,
            )
        else:  # raw inline card (multi-dept)
            await LarkMessageEvent._send_im_message(
                self.get_lark_api(open_id, lark_api),
                content=json.dumps(data, ensure_ascii=False),
                msg_type="interactive",
                receive_id=open_id,
                receive_id_type="open_id",
                reply_message_id=reply_message_id,
            )


    # ── Admin view ───────────────────────────────────────────────────────────────

    async def send_admin_view_card(self, open_id: str, check_result: dict, message_id: str = "") -> bool:
        """Send the admin view card to the manager as a DM.

        check_result: the dict returned by services/report.py check_submissions()
        Template variables filled:
          dept_name                       — department name from org tree
          time_updated                    — when the check was run (UTC+8)
          weekly_report_submission_status — "7/10" fraction
          heading_colour                  — "green" (all submitted) or "yellow" (partial)
          submitted_names                 — comma-separated submitted names
          not_submitted_names             — comma-separated pending names
        """
        submitted     = check_result.get("submitted", [])
        not_submitted = check_result.get("not_submitted", [])
        total         = check_result.get("total", 0)
        dept_name     = check_result.get("dept_name", "")
        all_done      = total > 0 and len(submitted) == total

        time_updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        variables = {
            "open_id":                          open_id,
            "dept_name":                        dept_name,
            "time_updated":                     time_updated,
            "weekly_report_submission_status":  f"{len(submitted)}/{total}",
            "heading_colour":                   "green" if all_done else "yellow",
            "submitted_names":                  "、".join(submitted) or "（无）",
            "not_submitted_names":              "、".join(not_submitted) or "（无）",
        }
        if message_id:
            return await self.patch_card(
                message_id, WRSBOT_ADMIN_VIEW_CARD_ID, variables,
                lark_api=self.get_lark_api(open_id),
            )
        return await self.send_template_card("open_id", open_id, WRSBOT_ADMIN_VIEW_CARD_ID, variables)

    async def send_user_view_card(self, open_id: str, dept_name: str, is_submitted: bool, message_id: str = "") -> bool:
        """Send the user view card as a DM.

        Template variables:
          dept_name             — user's department name
          time_updated          — when the check ran (UTC+8)
          user_submission_status — status string in Chinese/English
        """
        time_updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        status = "已提交！ / Submitted!" if is_submitted else "未提交，请尽快编写~ / Not yet submitted, please write soon~"
        variables = {
            "open_id":                open_id,
            "dept_name":              dept_name,
            "time_updated":           time_updated,
            "user_submission_status": status,
        }
        if message_id:
            return await self.patch_card(
                message_id, WRSBOT_CARD_USER_VIEW_ID, variables,
                lark_api=self.get_lark_api(open_id),
            )
        return await self.send_template_card("open_id", open_id, WRSBOT_CARD_USER_VIEW_ID, variables)

    async def send_style_config_card(
        self,
        open_id: str,
        *,
        tone_tags: list[str],
        custom_instructions: str,
        writing_samples: str,
        updated_at: str,
    ) -> bool:
        """Send the manager-style config card pre-filled with saved values.

        Template variables — must match the placeholders in the Feishu builder
        template WRSBOT_STYLE_CONFIG_CARD_ID:
          open_id                 — for personalized header
          tone_tags_preselected   — list of currently-selected tag keys, fed
                                    into the multi-select's default_values
          custom_instructions     — pre-filled textarea content
          writing_samples         — pre-filled textarea content
          updated_at              — display string, "（尚未保存）" if empty
        """

        if not WRSBOT_STYLE_CONFIG_CARD_ID:
            logger.warning("[Lark_card] WRSBOT_STYLE_CONFIG_CARD_ID 未配置，无法发送风格配置卡片")
            return False
        variables = {
            "open_id":               open_id,
            "tone_tags_preselected": tone_tags,
            "custom_instructions":   custom_instructions or "",
            "writing_samples":       writing_samples or "",
            "updated_at":            updated_at or "（尚未保存）",
        }
        return await self.send_template_card(
            "open_id", open_id, WRSBOT_STYLE_CONFIG_CARD_ID, variables,
        )

    async def send_generated_success_card(self, open_id: str, message_id: str) -> None:
        """Patch the 'generating' card in-place with the 'generated successfully' notice."""
        # Using official Feishu CardKit template (WRSBOT_SUMMARY_REWRITE_CARD_ID)
        # instead of the inline _SUMMARY_ACTION_CARD — inline-form binding has
        # been unreliable for form_value. Template card handles form logic in
        # Feishu's card builder.
        await self.patch_card(
            message_id, WRSBOT_SUMMARY_REWRITE_CARD_ID, {},
            lark_api=self.get_lark_api(open_id),
        )
        # await self.patch_inline_card(message_id, _SUMMARY_ACTION_CARD)

    # ── CardKit streaming helpers ────────────────────────────────────────────
    # Used by main.py's _stream_to_card to render LLM output token-by-token
    # via Feishu's CardKit streaming card. Mirrors the in-tree implementation
    # at lark_event.py:780-937 but bound to self.lark_api so it can run from
    # a card-callback pipeline (no LarkMessageEvent instance available there).
    #
    # Why duplicate AstrBot's helpers instead of reusing them:
    #   1. LarkMessageEvent.send_streaming (the high-level orchestrator) is
    #      instance-bound — it needs self.message_obj.message_id (for reply
    #      context), self.platform_meta.name (metric tagging), and calls
    #      super().send() for framework-level side effects. Our trigger is a
    #      card-button callback dispatched via asyncio.create_task: no event
    #      instance, no incoming user message to reply to, sending an active
    #      DM to open_id. It doesn't drop in.
    #   2. The lower-level helpers (_create_streaming_card, _send_card_message,
    #      _update_streaming_text, _close_streaming_mode) only touch self.bot,
    #      so technically callable with our lark_api — but they're private
    #      _underscore instance methods. Calling them from outside the class
    #      couples us to AstrBot internals; a rename at any version bump
    #      silently breaks us.
    # Re-implementing here keeps us stable across AstrBot versions. If those
    # helpers ever become @staticmethod on a public surface, switch to them.

    async def create_streaming_card(self, open_id: str, header_title: str = "") -> str | None:
        """Create a CardKit streaming card entity. Returns card_id or None."""
        api = self.get_lark_api(open_id)
        if not api or api.cardkit is None:
            logger.error("[LarkCard] CardKit 模块未初始化")
            return None

        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

        card_json = {
            "schema": "2.0",
            "header": {"title": {"content": header_title, "tag": "plain_text"}},
            "config": {
                "streaming_mode": True,
                "summary": {"content": ""},
                "streaming_config": {
                    "print_frequency_ms": {"default": 50},
                    "print_step": {"default": 2},
                    "print_strategy": "fast",
                },
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": "", "element_id": "markdown_1"}
                ]
            },
        }

        req = (
            CreateCardRequest.builder()
            .request_body(
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(json.dumps(card_json, ensure_ascii=False))
                .build()
            )
            .build()
        )
        try:
            resp = await api.cardkit.v1.card.acreate(req)
        except Exception as e:
            logger.error(f"[LarkCard] 创建流式卡片失败: {e}")
            return None
        if not resp.success() or resp.data is None or not resp.data.card_id:
            logger.error(f"[LarkCard] 创建流式卡片失败({resp.code}): {resp.msg}")
            return None
        self._lark_api_by_streaming_card_id[resp.data.card_id] = api
        return resp.data.card_id

    async def send_streaming_card_to_user(self, open_id: str, card_id: str) -> bool:
        """Deliver a CardKit card entity as an interactive DM."""
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        content = json.dumps(
            {"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False
        )
        return await LarkMessageEvent._send_im_message(
            self.get_lark_api(open_id),
            content=content,
            msg_type="interactive",
            receive_id=open_id,
            receive_id_type="open_id",
        )

    async def update_streaming_text(
        self, card_id: str, content: str, sequence: int
    ) -> bool:
        """Push the full accumulated text to the streaming card's markdown_1 element."""
        api = self._lark_api_by_streaming_card_id.get(card_id) or self.lark_api
        if not api or api.cardkit is None:
            return False

        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        req = (
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id("markdown_1")
            .request_body(
                ContentCardElementRequestBody.builder()
                .content(content)
                .sequence(sequence)
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        try:
            resp = await api.cardkit.v1.card_element.acontent(req)
        except Exception as e:
            logger.debug(f"[LarkCard] 流式更新失败 (ignored): {e}")
            return False
        if not resp.success():
            logger.debug(f"[LarkCard] 流式更新失败({resp.code}): {resp.msg}")
            return False
        return True

    async def close_streaming_card(self, card_id: str, sequence: int) -> None:
        """Close streaming mode so the card is forwardable / static afterwards."""
        api = self._lark_api_by_streaming_card_id.get(card_id) or self.lark_api
        if not api or api.cardkit is None:
            return

        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody

        settings = json.dumps(
            {"config": {"streaming_mode": False}}, ensure_ascii=False
        )
        req = (
            SettingsCardRequest.builder()
            .card_id(card_id)
            .request_body(
                SettingsCardRequestBody.builder()
                .settings(settings)
                .sequence(sequence)
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        try:
            resp = await api.cardkit.v1.card.asettings(req)
        except Exception as e:
            logger.error(f"[LarkCard] 关闭流式异常: {e}")
            return
        if not resp.success():
            logger.warning(f"[LarkCard] 关闭流式失败({resp.code}): {resp.msg}")
        self._lark_api_by_streaming_card_id.pop(card_id, None)

    async def send_summary_action_card(self, open_id: str) -> None:
        """Send the post-generation action card with rewrite input and two buttons."""
        # Using official Feishu CardKit template (WRSBOT_SUMMARY_REWRITE_CARD_ID)
        # instead of the inline _SUMMARY_ACTION_CARD — see send_generated_success_card
        # for rationale.
        await self.send_template_card(
            "open_id", open_id, WRSBOT_SUMMARY_REWRITE_CARD_ID, {}
        )
        # from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
        # await LarkMessageEvent._send_im_message(
        #     self.lark_api,
        #     content=json.dumps(_SUMMARY_ACTION_CARD, ensure_ascii=False),
        #     msg_type="interactive",
        #     receive_id=open_id,
        #     receive_id_type="open_id",
        # )

    # ── Binding status helpers (sync — reads from cached org tree) ──────────────

    def _get_managed_depts(self, open_id: str) -> list[dict]:
        """Return org tree entries where this user is the department leader."""
        if not self.contact_service:
            return []
        return self.contact_service.get_managed_depts_sync(open_id)

    def _build_style_display_card_data(self, profile: dict) -> dict:
        """Build a view-only inline card showing the manager's saved style.

        Used when open_style_config fires AND there's already a saved profile.
        Display-only because the style template card's input fields can't be
        pre-filled via Feishu template_variable (multi-select + textarea
        defaults aren't accepted as variables). Showing the saved values in
        an inline JSON card sidesteps that limitation entirely.

        Two buttons:
          - 返回 (action: cancel_update_style) — back to admin view, no change
          - 重新生成风格 (action: edit_style_config) — opens the editable
            style_config_card template
        """
        tags    = profile.get("tone_tags", []) or []
        custom  = (profile.get("custom_instructions", "") or "").strip()
        samples = (profile.get("writing_samples",   "") or "").strip()
        updated = profile.get("updated_at", "") or "（尚未保存）"

        tags_line   = "、".join(tags) if tags else "（未选择）"
        custom_block  = custom or "（未填写）"
        samples_block = samples or "（未填写）"

        # Trim long samples in the display so the card doesn't dwarf the
        # screen. Editing still sees the full text via the template card.
        if len(samples_block) > 600:
            samples_block = samples_block[:600] + "\n\n…（更多内容已省略，编辑时可查看完整版）"

        markdown_body = (
            f"**最后更新：** {updated}\n\n"
            f"---\n\n"
            f"**🏷️ 语气标签**\n\n{tags_line}\n\n"
            f"**📝 自定义指令**\n\n{custom_block}\n\n"
            f"**✍️ 撰写样本（节选）**\n\n{samples_block}"
        )

        return {
            "type": "raw",
            "data": {
                "schema": "2.0",
                "config": {"update_multi": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "个人风格档案 · 当前版本"},
                    "template": "purple",
                },
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": markdown_body},
                        {"tag": "hr"},
                        {
                            "tag": "column_set",
                            "columns": [
                                {
                                    "tag": "column",
                                    "width": "weighted",
                                    "weight": 1,
                                    "elements": [{
                                        "tag": "button",
                                        "text": {"tag": "plain_text", "content": "返回"},
                                        "type": "default",
                                        "width": "fill",
                                        "behaviors": [{
                                            "type": "callback",
                                            "value": {"action": "cancel_update_style"},
                                        }],
                                    }],
                                },
                                {
                                    "tag": "column",
                                    "width": "weighted",
                                    "weight": 1,
                                    "elements": [{
                                        "tag": "button",
                                        "text": {"tag": "plain_text", "content": "重新生成风格"},
                                        "type": "primary",
                                        "width": "fill",
                                        "behaviors": [{
                                            "type": "callback",
                                            "value": {"action": "edit_style_config"},
                                        }],
                                    }],
                                },
                            ],
                        },
                    ],
                },
            },
        }

    def _build_admin_view_loading_data(self, open_id: str) -> dict:
        """Build a placeholder admin view card for instant patch-back.

        Used by save_manager_style / cancel_update_style so the card flips
        to the admin view *immediately* — the slow submission check runs in
        the background and patches the same message again with real data
        once it finishes. Dept name comes from the synchronous org-tree
        cache; submission stats show 加载中… until the async refresh lands.
        """
        dept_name = ""
        if self.contact_service:
            managed = self._get_managed_depts(open_id)
            if managed:
                dept_name = managed[0]["dept"].name or ""
        time_updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        return {
            "type": "template",
            "data": {
                "template_id": WRSBOT_ADMIN_VIEW_CARD_ID,
                "template_variable": {
                    "open_id":                          open_id,
                    "dept_name":                        dept_name or "—",
                    "time_updated":                     time_updated,
                    "weekly_report_submission_status":  "加载中…",
                    "heading_colour":                   "yellow",
                    "submitted_names":                  "加载中…",
                    "not_submitted_names":              "加载中…",
                },
            },
        }

    def _binding_card_dept_name(self, open_id: str, default_name: str) -> str:
        """Return the same department label that the binding action targets.

        ContactService already resolves a test administrator's explicit
        manager override.  The card must use that managed-department label,
        rather than substituting the user's ordinary membership department.
        """
        return default_name

    def _build_not_binding_card_data(self, open_id: str, *, failed: bool = False) -> dict:
        """Return the card 'type'+'data' dict for the binding status card.

        A single managed department uses the 文件夹配置 template, which exposes
        independent weekly-report and daily-report binding variables.  The
        multi-department inline card remains the existing weekly-only view.
        """
        from ..utils.env_config import (
            get_dept_daily_report_folder_token,
            get_dept_folder_token,
        )

        managed = self._get_managed_depts(open_id)

        if not managed:
            return {
                "type": "template",
                "data": {
                    "template_id": WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID,
                    "template_variable": {
                        "open_id": open_id,
                        "dept_name": "未知部门",
                        "dept_num": 0,
                        "unreg_dept_num": 0,
                        "binding_status": "绑定失败" if failed else "未绑定",
                        "heading_colour_code": "red" if failed else "orange",
                        "doc_is_not_binded": True,
                        "folder_binding_status": "0/0文件夹已绑定",
                        "wr_binding_status": "绑定失败" if failed else "未绑定",
                        "heading_colour": "red",
                        "wr_binding_colour_code": "red",
                        "dr_binding_status": "未绑定",
                        "wr_doc_is_not_binded": True,
                        "dr_doc_is_not_binded": True,
                        "dr_binding_colour_code": "red",
                    },
                },
            }

        if len(managed) == 1:
            dept = managed[0]["dept"]
            dept_id = getattr(dept, "open_department_id", "") or ""
            weekly_bound = bool(get_dept_folder_token(dept_id)) and not failed
            daily_bound = bool(get_dept_daily_report_folder_token(dept_id))

            def _status(is_bound: bool, *, is_failed: bool = False) -> tuple[str, str]:
                if is_failed:
                    return "绑定失败", "red"
                return ("已绑定", "green") if is_bound else ("未绑定", "red")

            wr_status, wr_colour = _status(weekly_bound, is_failed=failed)
            dr_status, dr_colour = _status(daily_bound)
            bound_count = int(weekly_bound) + int(daily_bound)
            heading_colour = ("red", "orange", "green")[bound_count]
            dept_name = self._binding_card_dept_name(open_id, dept.name or "")

            return {
                "type": "template",
                "data": {
                    "template_id": WRSBOT_NOT_BINDiNG_FEEDBACK_CARD_ID,
                    "template_variable": {
                        # Existing weekly-card variables are retained for
                        # compatibility with the copied card template.
                        "open_id": open_id,
                        "dept_id": dept_id,
                        "dept_name": dept_name,
                        "dept_num": 1,
                        "unreg_dept_num": 0 if weekly_bound else 1,
                        "binding_status": wr_status,
                        "heading_colour_code": wr_colour,
                        "doc_is_not_binded": not weekly_bound,
                        # New dual-folder variables used by 文件夹配置.
                        "folder_binding_status": f"{bound_count}/2文件夹已绑定",
                        "wr_binding_status": wr_status,
                        "heading_colour": heading_colour,
                        "wr_binding_colour_code": wr_colour,
                        "dr_binding_status": dr_status,
                        "wr_doc_is_not_binded": not weekly_bound,
                        "dr_doc_is_not_binded": not daily_bound,
                        "dr_binding_colour_code": dr_colour,
                    },
                },
            }

        return {
            "type": "raw",
            "data": _build_multi_dept_binding_card(managed, failed=failed),
        }

    # todo: write the workflow as comment
    # todo: write functionalities
    # todo: For developer's benefit should I store all the lark cards in the directory for viewing purpose? Think about this question.



# ── Inline binding card builder ──────────────────────────────────────────────
# Used for every unbound or failed binding state.
# Builds inline JSON — one status row per managed department.

def _build_multi_dept_binding_card(managed_depts: list[dict], *, failed: bool = False) -> dict:
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
        status_label = "绑定失败" if failed else ("已绑定" if is_bound else "未绑定")
        colour       = "red" if failed else ("green" if is_bound else "orange")

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
    header_colour = "red" if failed else ("green" if total_unbound == 0 else "orange")
    header_status = "绑定失败，请检查文件夹链接后重试" if failed else f"{total - total_unbound}/{total} 已绑定"

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


# ── Report generation state cards ───────────────────────────────────────────
# These two cards share the same message — _GENERATING_CARD is returned
# synchronously when the button is clicked; _GENERATED_SUCCESS_CARD patches
# it in-place via patch_inline_card() when the LLM pipeline finishes.

_GENERATING_CARD = {
    "schema": "2.0",
    "config": {"update_multi": True},
    "header": {
        "title": {"tag": "plain_text", "content": "正在生成周报总结..."},
        "template": "blue",
    },
    "body": {
        "elements": [
            {
                "tag": "markdown",
                "content": "AI 正在读取周报数据并汇总，请稍候。\n生成完成后将通过私信发送结果，并提供后续操作选项。",
            }
        ]
    },
}


# ── Post-generation action card ──────────────────────────────────────────────
# DEPRECATED: replaced by the official Feishu CardKit template
# WRSBOT_SUMMARY_REWRITE_CARD_ID. Inline form-binding for `style_input` proved
# unreliable (form_value came back empty even with form_action_type: submit
# and a flattened structure). The template card handles the form in Feishu's
# card builder, which should resolve form_value delivery.
# Kept commented for reference / quick revert.
#
# _SUMMARY_ACTION_CARD = {
#     "schema": "2.0",
#     "config": {"update_multi": True},
#     "header": {
#         "title": {"tag": "plain_text", "content": "周报总结已生成"},
#         "template": "green",
#     },
#     "body": {
#         "elements": [
#             {
#                 "tag": "markdown",
#                 "content": "请选择下一步操作。如需按特定风格改写，在下方输入要求后点击「重新改写」。",
#             },
#             {"tag": "hr"},
#             {
#                 "tag": "form",
#                 "name": "rewrite_form",
#                 "elements": [
#                     {
#                         "tag": "input",
#                         "name": "style_input",
#                         "placeholder": {"tag": "plain_text", "content": "改写风格要求（可选）：如「更简洁」、「突出结果」、「偏向老板汇报风格」"},
#                     },
#                     {
#                         "tag": "button",
#                         "text": {"tag": "plain_text", "content": "重新改写"},
#                         "type": "default",
#                         "width": "fill",
#                         "form_action_type": "submit",
#                         "behaviors": [{"type": "callback", "value": {"action": "rewrite_summary"}}],
#                     },
#                 ],
#             },
#             {"tag": "hr"},
#             {
#                 "tag": "button",
#                 "text": {"tag": "plain_text", "content": "写入文档 →"},
#                 "type": "primary",
#                 "width": "fill",
#                 "behaviors": [{"type": "callback", "value": {"action": "submit_summary"}}],
#             },
#         ]
#     },
# }


# ── Command list strings ─────────────────────────────────────────────────────

_EMPLOYEE_CMD_LIST = (
    "• **你好** / **Hello** — 打开主菜单\n"
    "• **开始使用** — 查看我的本周提交状态\n"
    "• **查看本周文档** — 获取周报文件夹链接\n"
    "• **使用帮助** — 查看使用说明"
)

_MANAGER_CMD_LIST = (
    "• **你好** / **Hello** — 打开主菜单\n"
    "• **/文件夹配置** — 绑定或重新绑定周报文件夹\n"
    "• **开始使用** — 查看团队本周提交情况\n"
    "• **催交报告** — 向未提交成员发送提醒卡片\n"
    "• **生成周报总结** — 启动 LLM 汇总流程\n"
    "• **重新生成** — 按需要重新生成总结草稿\n"
    "• **写入飞书文档** — 将总结写入飞书文档\n"
    "• **个人写作风格配置** — 设置总结的写作风格\n"
    "• **查看本周文档** — 获取周报文件夹链接\n"
    "• **使用帮助** — 查看使用说明"
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
                    "WRSbot 用于协助团队收集周报、查看提交进度，并由部门负责人生成周报总结。\n\n"
                    "**使用流程**\n"
                    "**1. 打开主菜单**\n"
                    "在私聊中向 WRSbot 发送「你好」或「Hello」，即可打开功能菜单。\n\n"
                    "**2. 团队成员填写周报**\n"
                    "点击「开始使用」查看自己的提交状态。点击「查看本周文档」，机器人会私聊发送本周周报文件夹链接。"
                    "请在对应文档或多维表格中填写本周工作内容。\n\n"
                    "**3. 部门负责人配置文件夹**\n"
                    "首次使用时，部门负责人发送 `/文件夹配置`，并根据卡片提示粘贴周报文件夹链接。"
                    "请确保团队成员和机器人均有该文件夹的访问权限。\n\n"
                    "**4. 查看提交情况**\n"
                    "部门负责人点击「开始使用」进入周报管理页面，可查看已提交人数、成员名单及数据更新时间。\n\n"
                    "**5. 催交与生成总结**\n"
                    "仍有成员未提交时，可点击「催交报告」发送提醒。提交后可点击「生成周报总结」生成草稿，"
                    "并按需要重新改写或写入飞书文档。\n\n"
                    "**注意事项**\n"
                    "• 周报文件必须放在已绑定的部门文件夹中。\n"
                    "• 如果提示未绑定文件夹，请联系部门负责人完成 `/文件夹配置`。\n"
                    "• 提交状态可能需要等待片刻更新。"
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
