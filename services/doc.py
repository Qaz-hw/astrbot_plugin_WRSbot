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

from lark_oapi.api.docx.v1 import RawContentDocumentRequest, ListDocumentBlockRequest
from lark_oapi.api.drive.v1 import ListFileRequest


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
        resp = await self.lark_api.drive.v1.file.alist(req)
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
        resp = await self.lark_api.docx.v1.document.araw_content(req)
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

            resp = await self.lark_api.docx.v1.document_block.alist(builder.build())
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
    9:  "- ",   # bullet (unordered list)
    10: "1. ",  # ordered list (all rendered as "1." — LLM understands sequence)
    12: "> ",   # quote / callout
    13: "☐ ",  # todo / checkbox
}

# block_type → attribute name on the Block object that holds the text content.
# These mirror the JSON field names in the Feishu API response.
_TYPE_ATTR: dict[int, str] = {
    2:  "text",
    3:  "heading1",
    4:  "heading2",
    5:  "heading3",
    6:  "heading4",
    7:  "heading5",
    8:  "heading6",
    9:  "bullet",
    10: "ordered",
    11: "code",
    12: "quote",
    13: "todo",
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
        if btype == 14:     # divider — no text
            lines.append("---")
            continue

        text = _extract_block_text(block)
        if not text.strip():
            continue        # skip empty blocks (images, unsupported types, etc.)

        if btype in _HEADING_PREFIX:
            lines.append("")  # blank line before each heading for section separation
            lines.append(f"{_HEADING_PREFIX[btype]}{text}")
        elif btype in _INLINE_PREFIX:
            lines.append(f"{_INLINE_PREFIX[btype]}{text}")
        elif btype == 11:   # code block
            lines.append(f"```\n{text}\n```")
        else:               # paragraph and any other text-bearing block
            lines.append(text)

    return "\n".join(lines).strip()
