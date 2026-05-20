#=====================================================
#  services/pipelines/report.py — Report generate / rewrite / submit
#=====================================================
#
#  These pipelines are the write side of the system. generate builds the
#  draft, rewrite re-runs the LLM against an existing draft, and submit
#  writes the final text back into Bitable (cell-by-cell via LLM column
#  split) or appends to the weekly Doc.
#
#  Registered on card_service via:
#      set_report_pipelines(generate=generate, rewrite=rewrite, submit=submit)
#=====================================================

import json
from datetime import datetime, timedelta

from astrbot.api import logger, sp

from ..bitable import BitableService
from ._base import PipelineBase


class ReportPipelines(PipelineBase):
    """Generate / rewrite / submit pipelines (content mutation)."""

    async def generate(self, open_id: str, message_id: str = "") -> None:
        """
        [PIPELINE] Read this week's reports → single LLM pass → streaming card + action card.

        Workflow:
            1. Resolve manager → dept → folder (same as admin_view)
            2. Find this week's file via _find_weekly_source
            3. Load the manager's style profile from sp (tags + custom
               instructions + writing samples for voice anchoring)
            4. Read raw content (Bitable records or Doc text)
            5. Run check_submissions in parallel — the LLM can prepend a
               「说明」 section if anyone hasn't submitted / quality is low
            6. Single LLM pass via generate_report_stream — combined fact
               extraction + executive style, streamed into a CardKit card
            7. Persist the final draft to sp keyed by f"draft:{open_id}"
               (the rewrite pipeline reads from here)
            8. Patch the generating card → action card (rewrite/submit
               buttons), or DM a fresh action card if no message_id

        Args:
            open_id:    Feishu user ID of the manager
            message_id: when provided, patches the in-flight generating
                        card into the final action card on completion

        Triggered by:
            - start_summary card action (button on the admin view card)
        """
        if not self.lark_api:
            return
        from ..report import (
            generate_report_stream_mapreduce,
            check_submissions,
        )
        from ...utils.env_config import get_dept_folder_token
        from ...utils.manager_style import get_manager_style

        # Generate always uses the 3-stage pipeline (extract → plan → write).
        # Both bitable and doc paths converge into generate_report_stream_mapreduce:
        #   - bitable: each record splits into its own (name, text) tuple →
        #     extract fans out in parallel under llm_semaphore
        #   - doc:     single ("团队", full_doc_text) tuple → extract runs once
        #              (the doc can't be cheaply split per-employee here)
        # Either way the plan + write stages run identically downstream so the
        # final report has the same project-centric structure regardless of
        # source file type.
        #
        # SCALE TARGET (current): 30–50 employees per dept.
        #
        # TODO [scale > 100 employees per dept]:
        #   - Add a hierarchical layer between extract and plan:
        #         extract (per employee, parallel)
        #           → team_rollup (per team, parallel — new function)
        #             → plan (single call, sees team rollups not raw facts)
        #             → write (streaming)
        #     Needed because at 100+ employees the plan stage's JSON input
        #     starts to dilute attention. Team-level rollup acts as a
        #     coarsening layer; plan only sees ~5-10 team rollups instead
        #     of 100 raw EmployeeFacts.
        #   - Track team membership via contact_service.get_cached_org_tree
        #     (sub-departments exist in Feishu org tree but we don't model
        #     them yet — see services/contact.py for the cache shape).

        try:
            # ── Resolve folder ────────────────────────────────────────────────
            org_tree = await self.contact_service.get_cached_org_tree()
            managed = [
                e for e in org_tree
                if e.get("manager") and e["manager"].open_id == open_id
            ]
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

            # Read source content. For bitable we keep `records` to split per
            # employee in the extract stage; for doc we pass a single
            # ("团队", full_doc_text) tuple so extract still runs (just as
            # one bigger call instead of N parallel small ones).
            records: list[dict] = []
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
            # pass the dept's submitted / not_submitted lists into the prompt
            # so the model prepends a 「说明」 section when applicable.
            submitted: list[str]     = []
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

            # ── 3-stage generate (extract → plan → write) ────────────────────
            # Build the (name, text) tuples the orchestrator expects:
            #   bitable → list of per-employee tuples (parallel extract fan-out)
            #   doc     → single ("团队", full_doc) tuple (one extract call,
            #             still flows through plan + write so output structure
            #             is consistent across source types)
            session_id    = f"wrsbot_generate:{open_id}"
            style_profile = await get_manager_style(open_id)

            if weekly_file["type"] == "bitable":
                employee_inputs = BitableService.split_records_per_employee(records)
                logger.info(
                    f"[Generate] 3-stage path (bitable): N={len(employee_inputs)} 员工"
                )
            else:
                employee_inputs = [("团队", content)]
                logger.info(
                    f"[Generate] 3-stage path (doc): 整篇文档作为单源输入 "
                    f"({len(content)} 字符)"
                )

            stream = generate_report_stream_mapreduce(
                employee_inputs, dept_name, iso_tag, provider, session_id,
                submitted=submitted, not_submitted=not_submitted,
                style_profile=style_profile,
                semaphore=self.llm_semaphore,
            )

            final = await self._stream_to_card(
                open_id,
                stream,
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

    async def rewrite(self, open_id: str, style_input: str) -> None:
        """
        [PIPELINE] Rewrite the stored draft with optional style instructions, then resend.

        Workflow:
            1. Load the stored draft from sp (created by generate)
            2. Load the manager's style profile (same voice anchor as generate)
            3. Stream rewrite_summary_stream into a new CardKit card
            4. Overwrite the stored draft text with the rewritten version
            5. Send the summary action card (rewrite/submit buttons)

        Args:
            open_id:     Feishu user ID of the manager
            style_input: free-form instructions from the card's textarea
                         (e.g. "更简洁一些" or "去掉每段开头的总结句")

        Triggered by:
            - rewrite_summary card action (textarea + submit on the
              summary action card)
        """
        if not self.lark_api:
            return
        from ..report import rewrite_summary_stream
        from ...utils.manager_style import get_manager_style

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
            style_profile = await get_manager_style(open_id)
            final = await self._stream_to_card(
                open_id,
                rewrite_summary_stream(
                    stored["text"], provider,
                    session_id=f"wrsbot_rewrite:{open_id}",
                    style_input=style_input,
                    not_submitted=not_submitted,
                    style_profile=style_profile,
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

    async def submit(self, open_id: str) -> None:
        """
        [PIPELINE] Write the stored draft into the bound weekly file.

        Bitable path:
            1. Discover the table's column schema (list_fields)
            2. LLM-map sections → columns (split_report_to_columns), with
               markdown stripped for cell display
            3. Anchor the row with 姓名 = "部门总结" so find_summary_record
               picks it up next time
            4. update_record if the row exists, otherwise create_record

        Doc path:
            1. Prepend a "#### 部门总结（{dept}）" heading
            2. Append the heading + text as new blocks at the doc tail

        After successful write, sends a confirmation text DM.

        Args:
            open_id: Feishu user ID of the manager

        Triggered by:
            - submit_report card action (button on the summary action card)
        """
        if not self.lark_api:
            return
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

            file_type   = weekly_file.get("type", "")
            target_desc = ""  # filled below for the success DM

            if file_type == "bitable":
                if not self.bitable_service:
                    logger.warning("[Submit] bitable_service 未就绪")
                    return
                from ..report import split_report_to_columns, _md_to_plain

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
