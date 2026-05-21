#=====================================================
#  services/pipelines/views.py — Admin / user view + reminder + view_doc
#=====================================================
#
#  These pipelines are read-mostly: they fetch the week's submission state
#  and surface it via a card or a text DM. None of them mutate Bitable /
#  Doc content — that's report.py's job.
#
#  Registered on card_service via:
#      set_admin_pipeline(admin_view)
#      set_user_pipeline(user_view)
#      set_reminder_pipeline(reminder)
#      set_view_doc_pipeline(view_doc)
#=====================================================

import json
from datetime import datetime, timedelta

from astrbot.api import logger

from ._base import PipelineBase


class ViewsPipelines(PipelineBase):
    """View-and-notify pipelines (no content mutation)."""

    async def admin_view(self, open_id: str, message_id: str = "") -> None:
        """
        [PIPELINE] Send the admin dashboard view card to a manager.

        Workflow:
            1. Resolve manager → managed dept via cached org tree
            2. Look up the dept's folder token in .env
            3. Discover this week's file/table in the folder
               (bitable table-name match first, drive fallback with LLM picker)
            4. Run check_submissions (LLM-based) for the dept
            5. Patch or DM the admin view card with the result

        Args:
            open_id:    Feishu user ID of the manager
            message_id: when provided, patches an existing card in place;
                        when empty, sends a fresh DM

        Triggered by:
            - wrsbot_start card action (welcome flow, admin role)
            - natural_report_trigger (LLM-detected generate_summary intent)
            - cancel_update_style / save_manager_style (patch-back from style card)
        """
        from ..report import check_submissions
        from ...utils.env_config import get_dept_folder_token

        try:
            org_tree = await self.contact_service.get_cached_org_tree()
            managed = [
                e for e in org_tree
                if e.get("manager") and e["manager"].open_id == open_id
            ]
            if not managed:
                logger.warning(f"[AdminView] 未找到管理的部门: open_id={open_id}")
                await self._dm_text(
                    open_id,
                    "您当前账户不是任何部门的负责人，无法查看管理视图。如有疑问请联系系统管理员。",
                )
                return

            dept = managed[0]["dept"]
            dept_name = dept.name or ""
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            folder_token = get_dept_folder_token(open_dept_id)
            if not folder_token:
                logger.warning(f"[AdminView] 文件夹未绑定: dept={dept_name}")
                await self._dm_text(
                    open_id,
                    f"「{dept_name}」尚未绑定周报文件夹。请执行 /文件夹配置 完成绑定后再试。",
                )
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
                    await self._dm_text(
                        open_id,
                        f"「{dept_name}」本周未找到周报文件。请确认本周周报已在绑定文件夹中创建。",
                    )
                    return

            # ── Submission check ─────────────────────────────────────────────
            if weekly_file.get("type") == "bitable" and not self.bitable_service:
                logger.warning("[AdminView] bitable_service 不存在，无法处理多维表格")
                await self._dm_text(
                    open_id,
                    "Bitable 服务未启用，无法读取多维表格周报。请联系系统管理员。",
                )
                return

            provider = self.context.get_using_provider(umo=f"lark:open_id:{open_id}")
            if not provider:
                logger.warning(f"[AdminView] 未找到 LLM provider: open_id={open_id}")
                await self._dm_text(
                    open_id,
                    "当前没有可用的 LLM provider，无法分析周报提交情况。请联系系统管理员配置。",
                )
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
                await self._dm_text(
                    open_id,
                    f"周报提交情况检查失败：{result['error']}。请稍后重试或联系系统管理员。",
                )
                return

            await self.card_service.send_admin_view_card(open_id, result, message_id)

        except Exception as e:
            logger.error(f"[AdminView] 管理员视图加载失败: {e}")
            await self._dm_text(
                open_id,
                f"加载管理视图时出现异常：{e}。请稍后重试或联系系统管理员。",
            )

    async def user_view(self, open_id: str, message_id: str = "") -> None:
        """
        [PIPELINE] Send the per-user view card showing this user's submission status.

        Workflow:
            1. Find which dept this user belongs to via the org tree
            2. Look up that dept's folder token in .env
            3. Discover this week's file/table (same logic as admin_view)
            4. Run check_submissions to get the submitted-name list
            5. Check whether this user's name appears in `submitted`
            6. Patch or DM the user view card with their is_submitted flag

        Args:
            open_id:    Feishu user ID of the team member
            message_id: when provided, patches an existing card in place;
                        when empty, sends a fresh DM

        Triggered by:
            - wrsbot_start card action (welcome flow, non-admin role)
            - reminder pipeline (DM'd to every unsubmitted member)
        """
        from ..report import check_submissions
        from ...utils.env_config import get_dept_folder_token

        try:
            org_tree = await self.contact_service.get_cached_org_tree()

            dept_entry = None
            for entry in org_tree:
                if any(getattr(m, "open_id", None) == open_id for m in entry.get("members", [])):
                    dept_entry = entry
                    break

            if not dept_entry:
                logger.warning(f"[UserView] 未找到所属部门: open_id={open_id}")
                return

            dept         = dept_entry["dept"]
            dept_name    = dept.name or ""
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            folder_token = get_dept_folder_token(open_dept_id)
            if not folder_token:
                logger.warning(f"[UserView] 文件夹未绑定: dept={dept_name}")
                return

            # ── Find this week's file/table ──────────────────────────────────
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

            user_name = next(
                (m.name for m in dept_entry.get("members", []) if getattr(m, "open_id", None) == open_id),
                "",
            )
            is_submitted = user_name in result["submitted"]

            await self.card_service.send_user_view_card(
                open_id, dept_name, is_submitted, message_id
            )

        except Exception as e:
            logger.error(f"[UserView] 用户视图加载失败: {e}")

    async def reminder(self, open_id: str) -> None:
        """
        [PIPELINE] DM the user view card to every member who hasn't submitted yet.

        Mirrors the discovery half of admin_view (folder → weekly file →
        submission check), then fans out user_view_card sends to each name
        in not_submitted. Names are mapped to open_ids via result["members"].

        Workflow:
            1. Resolve manager → dept → folder (as in admin_view)
            2. Discover this week's file/table
            3. Run check_submissions
            4. For each name in not_submitted, look up the open_id and
               DM that member the user_view_card with is_submitted=False

        Args:
            open_id: Feishu user ID of the manager who clicked 提醒

        Triggered by:
            - summary_reminder card action (button on the admin view card)
        """
        from ..report import check_submissions
        from ...utils.env_config import get_dept_folder_token

        try:
            org_tree = await self.contact_service.get_cached_org_tree()
            managed = [
                e for e in org_tree
                if e.get("manager") and e["manager"].open_id == open_id
            ]
            if not managed:
                logger.warning(f"[Reminder] 未找到管理的部门: open_id={open_id}")
                return

            dept         = managed[0]["dept"]
            dept_name    = dept.name or ""
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            folder_token = get_dept_folder_token(open_dept_id)
            if not folder_token:
                logger.warning(f"[Reminder] 文件夹未绑定: dept={dept_name}")
                return

            # ── Find this week's file/table (same flow as admin_view) ────────
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

    async def view_doc(self, open_id: str) -> None:
        """
        [PIPELINE] DM the caller's department folder URL as a text message.

        Reuses _build_writing_folder_reply for dept lookup + URL resolution
        (covers unbound / not-in-dept cases with friendly fallback text).
        The card-side toast is the click feedback; the actual URL arrives
        as a separate text DM so it renders as a clickable link in Feishu.

        Args:
            open_id: Feishu user ID requesting the link

        Triggered by:
            - view_doc card action (button on user / admin view cards)
        """
        if not self.lark_api:
            return
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
        try:
            reply = await self._build_writing_folder_reply(open_id)
            await LarkMessageEvent._send_im_message(
                self.lark_api,
                content=json.dumps({"text": reply}, ensure_ascii=False),
                msg_type="text",
                receive_id=open_id,
                receive_id_type="open_id",
            )
            logger.info(f"[ViewDoc] 已发送文档链接: open_id={open_id}")
        except Exception as e:
            logger.error(f"[ViewDoc] 发送文档链接失败: {e}")
