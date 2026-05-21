#=====================================================
#  services/pipelines/_base.py — Pipeline base class
#=====================================================
#
#  Holds the service references shared across every pipeline and the
#  cross-domain helpers used by more than one of them. Domain classes
#  (ViewsPipelines, ReportPipelines, StylePipelines) subclass this so
#  helpers are callable as `self.<helper>` without per-call plumbing.
#
#  Why a base class instead of free functions:
#    - Helpers like `_stream_to_card` need lark_api + card_service + a
#      logger context; passing all of those into every call site is noisy
#    - Subclasses can reuse helpers without re-importing them
#    - Single point of truth for which services any pipeline depends on
#=====================================================

import asyncio
import json
from astrbot.api import logger

# Global LLM concurrency cap. Bounds the parallel fan-out of map-reduce
# generate (variant 1) so we don't burst-call past provider RPM/TPM limits
# when a manager triggers a large-dept report.
#
# SCALE TARGET (current): 30–50 employees per dept, single bot instance.
#   8 concurrent calls × ~2-4s per extract = full dept extracted in ~8-25s.
#
# TODO [scale > 100 employees per dept]:
#   - Raise to 16-32 (verify against provider tier RPM/TPM first).
#   - Consider a per-dept queue so simultaneous Monday-morning report runs
#     from multiple depts don't starve each other on this single shared cap.
#   - Move to per-provider-tier semaphores if you start mixing models
#     (e.g. Haiku for extract, Sonnet for reduce).
# TODO [multi-bot deployment]:
#   - This is a process-local semaphore. Horizontal scaling across multiple
#     bot processes requires a distributed limiter (Redis token bucket) to
#     avoid each process hammering the provider with its own cap-8 budget.
_LLM_SEMAPHORE = asyncio.Semaphore(8)


class PipelineBase:
    """Base class — stashes services and exposes the shared helpers.

    Pipelines are async methods. Each subclass adds methods registered
    on `card_service` via its `set_*_pipeline` setters. See the
    `services/pipelines/__init__.py` header for the package layout.
    """

    def __init__(
        self,
        lark_api,
        context,
        contact_service,
        drive_service,
        bitable_service,
        doc_service,
        card_service,
    ):
        self.lark_api        = lark_api
        self.context         = context
        self.contact_service = contact_service
        self.drive_service   = drive_service
        self.bitable_service = bitable_service
        self.doc_service     = doc_service
        self.card_service    = card_service
        # Shared LLM concurrency limiter for map-reduce fan-out.
        # Same semaphore across every pipeline instance so one big report
        # generation can't starve another card-action LLM call.
        self.llm_semaphore   = _LLM_SEMAPHORE

    # ── Shared helpers ───────────────────────────────────────────────────────

    async def _build_writing_folder_reply(self, open_id: str) -> str:
        """
        [HELPER] Resolve a user's dept → folder URL → user-facing reply text.

        URL is built deterministically from FEISHU_DRIVE_FOLDER_URL_PREFIX
        plus the stored token, so no per-binding URL storage is needed.

        States:
            1. User not in any dept           → ask them to contact admin
            2. Dept found but folder unbound  → ask manager to bind
            3. Folder bound                   → return URL + short instruction

        Args:
            open_id: Feishu user ID requesting the link

        Returns:
            Friendly Chinese message (always a usable string, never None)
        """
        from ...utils.env_config import get_dept_folder_url

        org_tree = await self.contact_service.get_cached_org_tree()
        dept_entry = next(
            (e for e in org_tree
             if any(getattr(m, "open_id", None) == open_id for m in e.get("members", []))),
            None,
        )
        if not dept_entry:
            return "未在通讯录中找到您的部门，请联系管理员核对。"

        dept         = dept_entry["dept"]
        dept_name    = dept.name or ""
        open_dept_id = getattr(dept, "open_department_id", "") or ""

        url = get_dept_folder_url(open_dept_id)
        if url:
            return (
                f"📂 {dept_name} 本周周报文件夹：\n{url}\n\n"
                f"请进入文件夹，找到本周对应的周报文档/多维表格并填写您的内容～"
            )

        return (
            f"{dept_name} 尚未绑定周报文件夹。"
            f"请联系部门负责人执行 /文件夹配置 完成绑定。"
        )

    async def _stream_to_card(
        self,
        open_id: str,
        text_stream,
        header_title: str = "",
        fallback_subtitle: str = "",
    ) -> str:
        """
        [HELPER] Drive a cumulative-text async generator into a Feishu streaming card.

        text_stream yields full snapshots (not deltas) — the CardKit content
        endpoint expects the full accumulated text each tick. A decoupled
        sender loop pushes whenever the snapshot changes; RTT acts as natural
        rate-limiting so we don't hammer the API per token.

        Falls back to a plain Feishu post message if CardKit is unavailable
        or card delivery fails, so the user always receives the report.

        Note: AstrBot ships LarkMessageEvent.send_streaming with similar
        decoupled-sender logic, but it's instance-bound (needs message_obj,
        platform_meta, reply context) and expects a MessageChain generator.
        Our card-callback pipeline has none of those, so we own the loop.

        Args:
            open_id:           Feishu user ID receiving the card
            text_stream:       async generator yielding cumulative text snapshots
            header_title:      title shown on the CardKit card header
            fallback_subtitle: subtitle used for the plain-message fallback

        Returns:
            Final accumulated text (last snapshot from the stream)
        """
        import asyncio

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

        snapshot  = ""
        last_sent = ""
        sequence  = 0
        done      = False
        changed   = asyncio.Event()

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

    async def _dm_text(self, open_id: str, text: str) -> None:
        """
        [HELPER] Send a plain-text DM to a user. Swallow send errors so
        callers can use this as an error-notification fallback without
        risking a secondary exception masking the original failure.
        """
        if not self.lark_api or not open_id or not text:
            return
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
        try:
            await LarkMessageEvent._send_im_message(
                self.lark_api,
                content=json.dumps({"text": text}, ensure_ascii=False),
                msg_type="text",
                receive_id=open_id,
                receive_id_type="open_id",
            )
        except Exception as e:
            logger.warning(f"[DM] 发送失败 open_id={open_id}: {e}")

    async def _send_report_message(
        self,
        open_id: str,
        report_text: str,
        dept_name: str = "",
        iso_week:  str = "",
    ) -> None:
        """
        [HELPER] Send the LLM-generated report as a Feishu post message.

        Uses msg_type="post" with a single `md` segment so the markdown
        (headers, bullets, bold) renders natively — the same path AstrBot
        uses for normal LLM replies. dept_name + iso_week become the post
        title for context, separate from the model output.

        Args:
            open_id:     Feishu user ID receiving the report
            report_text: full markdown body
            dept_name:   prefix in the post title (e.g. "AI 研究部")
            iso_week:    week tag in the post title (e.g. "2026-W21")
        """
        if not self.lark_api:
            return
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent

        title = " · ".join(p for p in (dept_name, iso_week, "周报总结") if p)
        content = {
            "zh_cn": {
                "title":   title,
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

    async def _find_weekly_source(self, folder_token: str, open_id: str = "") -> "dict | None":
        """
        [HELPER] Locate this week's report file in a bound folder.

        Workflow:
            1. List the folder contents
            2. Prefer a Bitable whose table name contains the ISO week tag
               (e.g. "2026-W21"); return early on a hit
            3. Otherwise fall through to drive.find_this_week_file, which
               regex-matches names and uses an LLM picker as a last resort

        Args:
            folder_token: Feishu drive folder token (DEPT_FOLDER_* in .env)
            open_id:      when non-empty, enables the LLM picker fallback
                          (needs an open_id to choose the right LLM provider)

        Returns:
            File dict with type/token (and table_id/table_name for bitable),
            or None if nothing matches this week
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
        """
        [HELPER] Build the LLM file-picker callable used by drive.find_this_week_file.

        Picks the most-likely-current-week file from a name list using the
        user's configured LLM provider. Card-callback pipelines have no
        event context, so we close over the open_id passed in here instead.

        Args:
            open_id: drives provider selection via lark:open_id:<id> umo

        Returns:
            async callable `_llm_pick(names: list[str]) -> str | None`
            (returns None when LLM call fails or model replies "none")
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
