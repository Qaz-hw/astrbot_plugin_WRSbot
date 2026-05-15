#=====================================================
#  services/bitable.py — Feishu Bitable Service
#=====================================================
#
#  Responsibilities:
#    - Wrap lark_oapi Bitable API with domain-level operations
#    - Fetch weekly report records from a Bitable table
#    - Handle pagination, field name mapping, and error handling
#    - Return typed WeeklySubmission models (not raw API dicts)
#
#  Does NOT contain:
#    - LLM logic
#    - Report generation logic
#    - Card sending logic
#=====================================================

## Dependencies
# from lark_oapi.api.bitable.v1 import ...
# from models.report import WeeklySubmission

## BitableService
#
# class BitableService:
#     __init__(lark_api, app_token)
#         # store lark_api client and default app_token
#
#     fetch_weekly_submissions(table_id, week_start, week_end) -> List[WeeklySubmission]
#         # query Bitable table for records within the week range
#         # handle pagination (page_token loop)
#         # map raw Bitable fields → WeeklySubmission dataclass
#         # return list of typed submissions
#
#     list_tables() -> List[dict]
#         # list all tables in the Bitable app
#         # useful for setup / debugging
#
#     get_submission_by_id(table_id, record_id) -> WeeklySubmission
#         # fetch a single record by record_id
#         # return as WeeklySubmission
