#=====================================================
#  services/drive.py — Feishu Drive Discovery Service
#=====================================================
#
#  Responsibilities:
#    - List files inside a bound Feishu folder
#    - Identify this week's report file by name pattern
#    - Fall back to an LLM callable when no pattern matches
#    - Return clean dicts including file type so callers know
#      which downstream service to use (bitable vs docx)
#
#  File type values returned by Feishu Drive API:
#    "bitable" → use BitableService
#    "docx"    → use DocService (new format)
#    "doc"     → use DocService (legacy format, same read API)
#
#  Does NOT contain:
#    - Bitable record reading (bitable.py)
#    - Doc content reading (doc.py)
#    - Card logic
#    - LLM implementation — callers inject llm_fn
#=====================================================

import re
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from astrbot.api import logger
from lark_oapi.api.drive.v1 import ListFileRequest
from .lark_context import get_active_lark_api

# Matches date-range names like:
#   2026.5.19-2026.5.25   2025-5-19~2025-5-25   2025/5/19至2025/5/25
_DATE_RANGE_RE = re.compile(
    r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})"  # start date
    r"\s*[-~～至—]\s*"                           # separator (hyphen / tilde / 至 / em-dash)
    r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})"  # end date
)


class DriveService:
    def __init__(self, lark_api):
        self.lark_api = lark_api

    # ── Folder listing ───────────────────────────────────────────────────────

    async def list_folder_files(self, folder_token: str) -> list[dict]:
        """List all files in a Feishu Drive folder, sorted by modified time (newest first).

        Each dict: {name, type, token, created_time, modified_time}
          type: "bitable" | "docx" | "doc" | "sheet" | ...
          token: used as app_token (bitable) or document_id (docx/doc)
        """
        req = (
            ListFileRequest.builder()
            .folder_token(folder_token)
            .page_size(50)
            .order_by("EditedTime")
            .direction("DESC")
            .build()
        )
        api = get_active_lark_api(self.lark_api)
        resp = await api.drive.v1.file.alist(req)
        if not resp.success():
            raise RuntimeError(
                f"[Drive] 文件夹列表失败: code={resp.code} msg={resp.msg}"
            )
        return [
            {
                "name": f.name or "",
                "type": f.type or "",
                "token": f.token or "",
                "created_time": getattr(f, "created_time", 0) or 0,
                "modified_time": getattr(f, "modified_time", 0) or 0,
            }
            for f in (resp.data.files or [])
        ]

    # ── Week matching ────────────────────────────────────────────────────────

    @staticmethod
    def _this_week_bounds() -> tuple[datetime.date, datetime.date]:
        """Return (monday, sunday) for the current calendar week."""
        today = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        return monday, sunday

    @staticmethod
    def match_this_week(files: list[dict]) -> dict | None:
        """Try to find this week's file by name pattern. Returns first match or None.

        Supported patterns (checked in order):
          1. ISO week  : "2026-W21"  anywhere in the name
          2. Date range: "2026.5.19-2026.5.25"  (overlap with this Mon–Sun)
        """
        monday, sunday = DriveService._this_week_bounds()
        year, week, _ = monday.isocalendar()
        iso_tag = f"{year}-W{week:02d}"

        for f in files:
            name = f["name"]

            # Pattern 1 — ISO week tag
            if iso_tag in name:
                logger.debug(f"[Drive] ISO 周匹配: {name!r}")
                return f

            # Pattern 2 — date range overlap
            m = _DATE_RANGE_RE.search(name)
            if m:
                try:
                    start = datetime(
                        int(m.group(1)), int(m.group(2)), int(m.group(3))
                    ).date()
                    end = datetime(
                        int(m.group(4)), int(m.group(5)), int(m.group(6))
                    ).date()
                    # Overlap: range touches this week at any point
                    if start <= sunday and end >= monday:
                        logger.debug(f"[Drive] 日期区间匹配: {name!r}")
                        return f
                except ValueError:
                    pass  # malformed date numbers in the name, skip

        return None

    # ── Main entry point ─────────────────────────────────────────────────────

    async def find_this_week_file(
        self,
        folder_token: str,
        llm_fn: Callable[[list[str]], Awaitable[str | None]] | None = None,
    ) -> dict | None:
        """Find this week's report file in a folder.

        Strategy:
          1. List all files in the folder
          2. Try rule-based name matching (ISO week / date range).
          3. If no match and llm_fn is provided, ask the LLM to pick from the name list.
             llm_fn receives a list[str] of file names and must return the best-match
             name as a string, or None if it cannot decide.
          4. Return the matching file dict, or None if nothing found.
        """
        files = await self.list_folder_files(folder_token)
        if not files:
            logger.warning(f"[Drive] 文件夹为空: folder={folder_token}")
            return None

        # Stage 1: rule-based
        match = self.match_this_week(files)
        if match:
            logger.info(
                f"[Drive] 本周文件（规则匹配）: {match['name']!r} type={match['type']}"
            )
            return match

        # Stage 2: LLM fallback
        if llm_fn:
            names = [f["name"] for f in files]
            logger.info(f"[Drive] 规则未匹配，交由 LLM 判断，候选: {names}")
            try:
                chosen = await llm_fn(names)
            except Exception as e:
                logger.warning(f"[Drive] LLM 判断失败: {e}")
                chosen = None

            if chosen:
                result = next((f for f in files if f["name"] == chosen), None)
                if result:
                    logger.info(
                        f"[Drive] 本周文件（LLM 匹配）: {result['name']!r} type={result['type']}"
                    )
                    return result
                logger.warning(
                    f"[Drive] LLM 返回的文件名 {chosen!r} 不在列表中"
                )

        logger.warning(f"[Drive] 未找到本周文件: folder={folder_token}")
        return None
