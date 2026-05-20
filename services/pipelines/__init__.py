#=====================================================
#  services/pipelines — Card-action and event-triggered workflows
#=====================================================
#
#  The orchestration layer between AstrBot's command/event handlers
#  (which live in main.py) and the per-domain services (drive, bitable,
#  doc, contact, report, lark_card). Each pipeline is an async method
#  that takes an open_id (and sometimes message_id / extra args), does
#  its multi-step work, and delivers a card or message via card_service.
#
#  Layout:
#    _base.py   — PipelineBase: holds service references + shared helpers
#                 (stream-to-card, send-report-message, weekly-source lookup,
#                  drive LLM picker, writing-folder reply builder)
#    views.py   — ViewsPipelines: admin_view, user_view, reminder, view_doc
#    report.py  — ReportPipelines: generate, rewrite, submit
#    style.py   — StylePipelines: open_style_config, save_manager_style
#
#  Each domain class subclasses PipelineBase so all helpers are reachable
#  via self.* with no per-call plumbing. main.py constructs one instance
#  of each class with the same service handles and registers their methods
#  on card_service via the set_*_pipeline setters.
#=====================================================

from ._base import PipelineBase
from .views import ViewsPipelines
from .report import ReportPipelines
from .style import StylePipelines

__all__ = ["PipelineBase", "ViewsPipelines", "ReportPipelines", "StylePipelines"]
