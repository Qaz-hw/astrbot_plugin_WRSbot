#=====================================================
#  models/report.py — Report Data Models
#=====================================================
#
#  Responsibilities:
#    - Define typed data structures for weekly report objects
#    - Used across services, prompts, and main.py
#    - Pure data only — no API calls, no AstrBot imports
#
#  Models:
#    WeeklySubmission  — one person's submitted weekly report
#    DeptReport        — aggregated department report object
#    ReportResult      — outcome returned by ReportService.generate()
#=====================================================

## Dependencies
# from dataclasses import dataclass, field
# from typing import Optional, List

## WeeklySubmission
# @dataclass
# class WeeklySubmission:
#     submitter_name: str          # display name from Feishu contact
#     submitter_open_id: str       # Feishu open_id
#     week_label: str              # e.g. "2026-W20"
#     raw_content: str             # unprocessed report text
#     source: str                  # "bitable" | "doc"

## DeptReport
# @dataclass
# class DeptReport:
#     dept_name: str
#     week_label: str
#     submissions: List[WeeklySubmission]
#     summary_draft: str           # output of LLM summarization pass
#     final_report: str            # output of LLM rewrite pass

## ReportResult
# @dataclass
# class ReportResult:
#     success: bool
#     doc_url: Optional[str]       # URL of the generated Feishu Doc
#     message: str                 # human-readable status for chat reply
