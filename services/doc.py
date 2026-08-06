#=====================================================
#  services/doc.py — Feishu Docs Service
#=====================================================
#
#  Responsibilities:
#    - Wrap lark_oapi Docs API with domain-level operations
#    - Read plaintext content from a Feishu Doc
#    - Read structured block content from a Feishu Doc
#    - List files in a folder
#    - Create a new report doc and write content into it
#
#  Does NOT contain:
#    - LLM logic
#    - Report generation logic
#    - Card sending logic
#
#  Note:
#    lark_oapi provides raw API access (docx.v1, drive.v1).
#    This service adds multi-step operations, error handling,
#    and domain-specific abstractions on top.
#=====================================================

import re

from lark_oapi.api.docx.v1 import (
    RawContentDocumentRequest,
    ListDocumentBlockRequest,
    CreateDocumentBlockChildrenRequest,
    CreateDocumentBlockChildrenRequestBody,
    GetDocumentBlockRequest,
    Block,
)
from lark_oapi.api.drive.v1 import ListFileRequest
from astrbot.api import logger
from .lark_context import get_active_lark_api


class DocService:
    def __init__(self, lark_api):
        self.lark_api = lark_api

    async def list_folder_files(self, folder_token: str) -> list:
        """List files in a Feishu Drive folder. Returns list of file objects."""
        req = (
            ListFileRequest.builder()
            .folder_token(folder_token)
            .page_size(10)
            .build()
        )
        resp = await get_active_lark_api(self.lark_api).drive.v1.file.alist(req)
        if not resp.success():
            raise RuntimeError(f"code={resp.code} msg={resp.msg}")
        return resp.data.files or []

    async def read_doc_plaintext(self, doc_token: str) -> str:
        """Read raw plaintext content of a Feishu Doc.

        Fast but loses all structure — headings, bullets, sections collapsed
        into a flat string. Use read_doc_blocks() when structure matters.
        """
        req = (
            RawContentDocumentRequest.builder()
            .document_id(doc_token)
            .build()
        )
        resp = await get_active_lark_api(self.lark_api).docx.v1.document.araw_content(req)
        if not resp.success():
            raise RuntimeError(f"code={resp.code} msg={resp.msg}")
        return resp.data.content or ""

    async def read_doc_blocks(self, doc_token: str) -> str:
        """Read a Feishu Doc as structured markdown-like text via the blocks API.

        Converts headings, bullets, paragraphs, and quotes into markdown-style
        prefixes so the LLM receives document structure (who wrote which section,
        hierarchy of items) rather than a flat undivided blob.

        Returns a single string with newline-separated lines.
        Blocks with no text content (images, dividers, tables) are skipped.
        """
        all_blocks = []
        page_token: str | None = None

        while True:
            builder = (
                ListDocumentBlockRequest.builder()
                .document_id(doc_token)
                .page_size(500)
            )
            if page_token:
                builder = builder.page_token(page_token)

            resp = await get_active_lark_api(self.lark_api).docx.v1.document_block.alist(builder.build())
            if not resp.success():
                raise RuntimeError(
                    f"[Doc] blocks 读取失败: code={resp.code} msg={resp.msg}"
                )

            all_blocks.extend(resp.data.items or [])
            if not resp.data.has_more:
                break
            page_token = resp.data.page_token

        return _blocks_to_markdown(all_blocks)

    async def create_report_doc(self, folder_token: str, title: str, content_blocks: list) -> str:
        """Create a new doc in the folder and write content blocks into it. Returns doc URL."""
        raise NotImplementedError

    # ── Writing ──────────────────────────────────────────────────────────────

    async def append_report(self, doc_id: str, markdown_text: str) -> int:
        """Append a generated report (markdown-ish) to the end of a Feishu Doc.

        Parses the LLM's #### / `- ` / paragraph output into Feishu block dicts
        and inserts them as children of the document root. Returns the number
        of blocks appended.

        index handling: docx v1's CreateDocumentBlockChildren rejects index=-1
        with `1770001 invalid param`. We fetch the root block's current
        children count and use that as the index (always a valid positive
        position that lands at the tail).

        Caller is responsible for any "dedupe with previous 部门总结 section"
        logic — this method only appends; it does not delete existing content.
        """
        blocks = _markdown_to_block_dicts(markdown_text)
        if not blocks:
            return 0

        # Feishu limits `children` to 50 blocks per request.  Keep appending
        # at the advancing root index so a long generated report stays in its
        # original order rather than failing or being inserted backwards.
        max_blocks_per_request = 50
        append_index = await self._get_root_children_count(doc_id)
        api = get_active_lark_api(self.lark_api)

        for batch_start in range(0, len(blocks), max_blocks_per_request):
            batch = blocks[batch_start : batch_start + max_blocks_per_request]
            body = (
                CreateDocumentBlockChildrenRequestBody.builder()
                .children([Block(block) for block in batch])
                .index(append_index)
                .build()
            )
            req = (
                CreateDocumentBlockChildrenRequest.builder()
                .document_id(doc_id)
                .block_id(doc_id)
                .document_revision_id(-1)  # -1 = latest revision
                .request_body(body)
                .build()
            )
            resp = await api.docx.v1.document_block_children.acreate(req)
            if not resp.success():
                violation_details = ""
                err = getattr(resp, "error", None)
                if err is not None:
                    fvs = getattr(err, "field_violations", None) or []
                    if fvs:
                        violation_details = " field_violations=" + "; ".join(
                            f"{getattr(fv, 'field', '?')}={getattr(fv, 'value', '?')!r}: {getattr(fv, 'description', '?')}"
                            for fv in fvs
                        )
                    log_id = getattr(err, "log_id", None)
                    if log_id:
                        violation_details += f" log_id={log_id}"
                raise RuntimeError(
                    f"[Doc] append_report 失败: code={resp.code} msg={resp.msg} "
                    f"doc_id={doc_id} index={append_index} batch_start={batch_start} "
                    f"batch_blocks={len(batch)} total_blocks={len(blocks)}{violation_details}"
                )
            logger.debug(
                f"[Doc] append_report: doc={doc_id} index={append_index} blocks={len(batch)}"
            )
            append_index += len(batch)

        return len(blocks)

    async def _get_root_children_count(self, doc_id: str) -> int:
        """Return the number of direct children of the doc's root block.

        Used as the `index` for appending children at the end. Falls back to
        0 (prepend) if the call fails, so submission never hard-errors here.
        """
        req = (
            GetDocumentBlockRequest.builder()
            .document_id(doc_id)
            .block_id(doc_id)
            .build()
        )
        try:
            resp = await get_active_lark_api(self.lark_api).docx.v1.document_block.aget(req)
        except Exception as e:
            logger.warning(f"[Doc] 获取根块异常，回退 index=0: {e}")
            return 0
        if not resp.success() or resp.data is None or resp.data.block is None:
            logger.warning(
                f"[Doc] 获取根块失败({resp.code}): {resp.msg}; 回退 index=0"
            )
            return 0
        return len(resp.data.block.children or [])


# ── Block rendering helpers ──────────────────────────────────────────────────
# Module-level so they don't clutter the class and are independently testable.

# Feishu block_type integer → markdown prefix
# Reference: https://open.feishu.cn/document/ukTMukTMukTM/uUDN04SN0QjL1QDN/document-docx/docx-v1/document-block/block-overview
_HEADING_PREFIX: dict[int, str] = {
    3: "# ",
    4: "## ",
    5: "### ",
    6: "#### ",
    7: "##### ",
    8: "###### ",
}

_INLINE_PREFIX: dict[int, str] = {
    12: "- ",   # bullet (unordered list)
    13: "1. ",  # ordered list (all rendered as "1." — LLM understands sequence)
    15: "> ",   # quote / callout
    17: "☐ ",  # todo / checkbox
}

# block_type → attribute name on the Block object that holds the text content.
# These mirror the JSON field names in the Feishu API response.
# IMPORTANT: Feishu docx v1's block_type integers go heading1..9 (3..11), THEN
# bullet=12, ordered=13, code=14, quote=15, todo=17. An earlier version of this
# map had 9='bullet' which collides with heading7 and caused 1770001 invalid
# param on every doc write — keep this aligned with Feishu's official enum.
_TYPE_ATTR: dict[int, str] = {
    2:  "text",
    3:  "heading1",
    4:  "heading2",
    5:  "heading3",
    6:  "heading4",
    7:  "heading5",
    8:  "heading6",
    9:  "heading7",
    10: "heading8",
    11: "heading9",
    12: "bullet",
    13: "ordered",
    14: "code",
    15: "quote",
    17: "todo",
}


def _extract_block_text(block) -> str:
    """Extract plain text from a block's typed content object.

    Each block type stores its text in a type-specific attribute (e.g. heading1,
    bullet) which contains an `elements` list of TextElement objects. Each
    TextElement has a `text_run` with a `content` string.

    Returns "" for block types without text content (images, dividers, tables).
    """
    attr = _TYPE_ATTR.get(block.block_type)
    if not attr:
        return ""
    content_obj = getattr(block, attr, None)
    if not content_obj:
        return ""
    parts = []
    for elem in getattr(content_obj, "elements", []) or []:
        text_run = getattr(elem, "text_run", None)
        if text_run:
            parts.append(getattr(text_run, "content", "") or "")
    return "".join(parts)


# Inverse of _TYPE_ATTR — for emitting blocks from markdown-ish text.
# Maps "what we want to render" to (block_type, attr_name) for the API body.
_HEADING_LEVEL_TO_BLOCK: dict[int, tuple[int, str]] = {
    1: (3, "heading1"),
    2: (4, "heading2"),
    3: (5, "heading3"),
    4: (6, "heading4"),
    5: (7, "heading5"),
    6: (8, "heading6"),
}

_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET_LINE_RE  = re.compile(r"^[-*]\s+(.+)$")


def _make_block_dict(block_type: int, attr_name: str, content: str) -> dict:
    """Build a Feishu doc block dict for a single inline-text element.

    Includes empty `style: {}` on the Text container and `text_element_style: {}`
    on the TextRun. Both are documented as optional, but some Feishu docx v1
    deployments reject blocks without them with 1770001 invalid param.
    """
    return {
        "block_type": block_type,
        attr_name: {
            "style": {},
            "elements": [{
                "text_run": {
                    "content": content,
                    "text_element_style": {},
                }
            }],
        },
    }


def _markdown_to_block_dicts(text: str) -> list[dict]:
    """Convert the LLM's markdown-ish report output to Feishu block dicts.

    Supported:
      - ``#### Heading`` (any level 1-6 → heading1..heading6)
      - ``- bullet`` or ``* bullet`` → bullet block (block_type 9)
      - Everything else non-blank → paragraph (block_type 2)
    Blank lines are dropped (Feishu spaces blocks automatically).
    """
    blocks: list[dict] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue

        h = _HEADING_LINE_RE.match(s)
        if h:
            level = len(h.group(1))
            content = h.group(2).strip()
            btype, attr = _HEADING_LEVEL_TO_BLOCK.get(level, (4, "heading2"))
            blocks.append(_make_block_dict(btype, attr, content))
            continue

        b = _BULLET_LINE_RE.match(s)
        if b:
            blocks.append(_make_block_dict(12, "bullet", b.group(1).strip()))
            continue

        blocks.append(_make_block_dict(2, "text", s))
    return blocks


def _blocks_to_markdown(blocks: list) -> str:
    """Convert a flat list of Feishu Block objects to a markdown-like string.

    Block type 1 (page root) is a container with no text — skipped.
    Empty blocks (whitespace only) are skipped to avoid noise.
    A blank line is inserted before each heading to visually separate sections.
    """
    lines: list[str] = []
    for block in blocks:
        btype = block.block_type
        if btype == 1:      # page root — container, no content
            continue
        if btype == 22:     # divider — no text
            lines.append("---")
            continue

        text = _extract_block_text(block)
        if not text.strip():
            continue        # skip empty blocks (images, unsupported types, etc.)

        if btype in _HEADING_PREFIX:
            lines.append("")  # blank line before each heading for section separation
            lines.append(f"{_HEADING_PREFIX[btype]}{text}")
        elif btype == 14:   # code block — render as fenced code
            lines.append(f"```\n{text}\n```")
        elif btype in _INLINE_PREFIX:
            lines.append(f"{_INLINE_PREFIX[btype]}{text}")
        else:               # paragraph and any other text-bearing block
            lines.append(text)

    return "\n".join(lines).strip()
