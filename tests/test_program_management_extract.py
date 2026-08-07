"""Executable regression task for Program Management extraction.

Run the tests only:
    python3 -m unittest discover -s tests -p 'test_program_management_extract.py' -v

Run the tests and print the extracted values:
    python3 tests/test_program_management_extract.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def _install_astrbot_stub() -> None:
    """Make this pure unit test runnable without AstrBot installed."""
    try:
        import astrbot.api  # noqa: F401
    except ModuleNotFoundError:
        astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
        astrbot_api = types.ModuleType("astrbot.api")
        astrbot_api.logger = logging.getLogger("test.program_management")
        sys.modules["astrbot.api"] = astrbot_api
        astrbot.api = astrbot_api


_install_astrbot_stub()

from services.program_management import (  # noqa: E402
    extract_program_management_tasks,
    format_task_for_bitable_cell,
)


_SAMPLE_REPORT = """\
项目：WRSbot 项目管理看板
- 截至 08.07 16:00，完成项目管理多维表格字段梳理和员工周报任务提取原型。
- 当前阻塞：等待项目负责人确认“是否逾期”字段的员工填写格式。
- 交付物：字段说明文档 https://example.feishu.cn/docx/program-management-fields

项目：周报文件选择
- 2026-08-06 完成 LLM 幻觉文件名回归测试，测试全部通过，按期完成。
"""

_SAMPLE_MODEL_OUTPUT = {
    "tasks": [
        {
            "project_id": "wrsbot-program-management",
            "project_name": "WRSbot 项目管理看板",
            "task": "完成项目管理多维表格字段梳理和员工周报任务提取原型",
            "status": "in_progress",
            "progress_updates": [
                {
                    "as_of": "08.07 16:00",
                    "fact": "完成项目管理多维表格字段梳理和员工周报任务提取原型",
                }
            ],
            "blockers": ["等待项目负责人确认“是否逾期”字段的员工填写格式"],
            "completion_criteria": [
                "字段说明文档 https://example.feishu.cn/docx/program-management-fields"
            ],
            # Deliberately supplied by the fake model. The parser must remove
            # it because this task is not marked completed.
            "actual_completion_date": "2026-08-07",
            "overdue": "unknown",
        },
        {
            "project_id": "weekly-source-selection",
            "project_name": "周报文件选择",
            "task": "完成 LLM 幻觉文件名回归测试",
            "status": "completed",
            "progress_updates": [
                {
                    "as_of": "2026-08-06",
                    "fact": "测试全部通过，按期完成",
                }
            ],
            "blockers": [],
            "completion_criteria": ["LLM 幻觉文件名回归测试全部通过"],
            "actual_completion_date": "2026-08-06",
            "overdue": "no",
        },
    ],
    "quality_note": "",
}


class _FakeResponse:
    def __init__(self, completion_text: str):
        self.completion_text = completion_text


class _FakeLLMProvider:
    """Records the prompt and returns one deterministic structured response."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    async def text_chat(self, *, prompt: str, system_prompt: str, session_id: str):
        self.calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "session_id": session_id,
            }
        )
        return _FakeResponse(json.dumps(self.payload, ensure_ascii=False))


async def run_sample_program_management_extract() -> dict:
    """Run the extract task and return its exact structured values."""
    provider = _FakeLLMProvider(_SAMPLE_MODEL_OUTPUT)
    result = await extract_program_management_tasks(
        name="Justin",
        raw_report=_SAMPLE_REPORT,
        llm_provider=provider,
        session_id="test:program-management:justin",
    )
    return result


class ProgramManagementExtractTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_returns_every_required_task_value(self):
        provider = _FakeLLMProvider(_SAMPLE_MODEL_OUTPUT)

        result = await extract_program_management_tasks(
            name="Justin",
            raw_report=_SAMPLE_REPORT,
            llm_provider=provider,
            session_id="test:program-management:justin",
        )

        self.assertEqual(result["name"], "Justin")
        self.assertEqual(result["quality_note"], "")
        self.assertEqual(len(result["tasks"]), 2)
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("name: Justin", provider.calls[0]["prompt"])
        self.assertIn(_SAMPLE_REPORT.strip(), provider.calls[0]["prompt"])
        self.assertIn('"overdue": "yes | no | unknown"', provider.calls[0]["system_prompt"])

        active_task = result["tasks"][0]
        self.assertEqual(active_task["project_name"], "WRSbot 项目管理看板")
        self.assertEqual(active_task["status"], "in_progress")
        self.assertEqual(active_task["progress_updates"][0]["as_of"], "08.07 16:00")
        self.assertEqual(
            active_task["blockers"],
            ["等待项目负责人确认“是否逾期”字段的员工填写格式"],
        )
        self.assertIn("https://example.feishu.cn/docx/program-management-fields", active_task["completion_criteria"][0])
        self.assertEqual(active_task["actual_completion_date"], "")
        self.assertEqual(active_task["overdue"], "unknown")

        completed_task = result["tasks"][1]
        self.assertEqual(completed_task["status"], "completed")
        self.assertEqual(completed_task["actual_completion_date"], "2026-08-06")
        self.assertEqual(completed_task["overdue"], "no")

    async def test_bitable_formatter_returns_timestamped_values(self):
        result = await run_sample_program_management_extract()

        cell_value = format_task_for_bitable_cell(result["tasks"][0])

        self.assertIn("截至08.07 16:00：完成项目管理多维表格字段梳理和员工周报任务提取原型", cell_value)
        self.assertIn("阻塞原因：等待项目负责人确认“是否逾期”字段的员工填写格式", cell_value)
        self.assertIn("完成标准/交付物：字段说明文档 https://example.feishu.cn/docx/program-management-fields", cell_value)
        self.assertIn("是否逾期：unknown", cell_value)
        self.assertNotIn("实际完成日期：2026-08-07", cell_value)


def _run_tests_then_print_values() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProgramManagementExtractTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1

    extracted = asyncio.run(run_sample_program_management_extract())
    print("\nExtracted Program Management values:")
    print(json.dumps(extracted, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_tests_then_print_values())
