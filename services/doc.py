#=====================================================
#  services/doc.py — Feishu Docs Service
#=====================================================
#
#  Responsibilities:
#    - Wrap lark_oapi Docs API with domain-level operations
#    - Read plaintext content from a Feishu Doc
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

from lark_oapi.api.docx.v1 import RawContentDocumentRequest
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
        """Read raw plaintext content of a Feishu Doc."""
        req = (
            RawContentDocumentRequest.builder()
            .document_id(doc_token)
            .build()
        )
        resp = await self.lark_api.docx.v1.document.araw_content(req)
        if not resp.success():
            raise RuntimeError(f"code={resp.code} msg={resp.msg}")
        return resp.data.content or ""

    async def create_report_doc(self, folder_token: str, title: str, content_blocks: list) -> str:
        """Create a new doc in the folder and write content blocks into it. Returns doc URL."""
        raise NotImplementedError
