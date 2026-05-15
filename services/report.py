#=====================================================
#  services/report.py — Report Generation Service
#=====================================================
#
#  Responsibilities:
#    - Orchestrate the full weekly report generation pipeline
#    - Coordinate BitableService / DocService for data fetching
#    - Call LLM for summarization and style rewriting
#    - Deliver the final report as a Feishu Doc or chat message
#
#  Pipeline:
#    1. Load ManagerPersona from AstrBot sp (by manager open_id)
#    2. Detect current week range
#    3. Fetch weekly submissions (Bitable or Doc, based on config)
#    4. LLM pass 1: summarize submissions → structured draft
#    5. LLM pass 2: rewrite draft in manager's persona
#    6. Create output Feishu Doc via DocService
#    7. Notify chat via LarkCardService.send_report_ready_card()
#
#  Does NOT contain:
#    - Raw Feishu API calls (delegated to BitableService / DocService)
#    - Prompt text (lives in prompts/)
#    - Card definitions (lives in services/lark_card.py)
#=====================================================

## Dependencies
# from services.bitable import BitableService
# from services.doc import DocService
# from services.lark_card import LarkCardService
# from prompts import summarize, rewrite
# from models.report import WeeklySubmission, DeptReport, ReportResult
# from models.config import ManagerPersona
# from utils.time import get_current_week_range

## ReportService
#
# class ReportService:
#     __init__(lark_api, llm_provider, card_service: LarkCardService)
#         # store dependencies
#
#     generate(chat_id, manager_open_id) -> ReportResult
#         # full pipeline — see module docstring above
#         # returns ReportResult(success, doc_url, message)
#
#     _load_persona(manager_open_id) -> ManagerPersona
#         # load ManagerPersona from AstrBot sp by manager open_id
#         # raise if not configured
#
#     _fetch_submissions(source, config) -> List[WeeklySubmission]
#         # dispatch to BitableService or DocService based on config.source_type
#
#     _call_llm_summarize(submissions: List[WeeklySubmission]) -> str
#         # build prompt via prompts/summarize.py
#         # call LLM, return structured summary draft
#
#     _call_llm_rewrite(draft: str, persona: ManagerPersona) -> str
#         # build prompt via prompts/rewrite.py with persona injected
#         # call LLM, return polished final report text
#
#     detect_week_range() -> tuple[date, date]
#         # return (monday, sunday) of the current week
#         # delegates to utils/time.py
