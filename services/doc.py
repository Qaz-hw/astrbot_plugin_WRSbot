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

## Dependencies
# from lark_oapi.api.docx.v1 import RawContentDocumentRequest, CreateDocumentRequest, ...
# from lark_oapi.api.drive.v1 import ListFileRequest

## DocService
#
# class DocService:
#     __init__(lark_api)
#         # store lark_api client
#
#     list_folder_files(folder_token) -> List[dict]
#         # list all files inside a Feishu Drive folder
#         # returns list of {name, token, type, url}
#
#     read_doc_plaintext(document_id) -> str
#         # call RawContentDocumentRequest
#         # return clean plain text content of the doc
#
#     create_report_doc(folder_token, title, content_blocks) -> str
#         # step 1: create a new doc in the folder (CreateDocumentRequest)
#         # step 2: write content blocks into the doc
#         # return the doc URL for sharing
