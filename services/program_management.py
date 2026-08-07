#=====================================================
#  services/program_management.py — Program Management Extraction
#=====================================================
#
# Standalone extraction module for turning one employee's weekly report into
# project-management task updates suitable for a future Feishu Bitable sync.
#
# This module deliberately does NOT read Feishu files, write to Bitable, send
# cards, or register a pipeline.  A future orchestration layer can call
# extract_program_management_tasks() for each employee and map the resulting
# structured tasks to the target Bitable schema.
#=====================================================

from __future__ import annotations

import json
from typing import TypedDict

from astrbot.api import logger


class ProgramTaskProgress(TypedDict):
    """One factual progress update for one project-management task."""

    as_of: str     # Exact source date/time without the "截至" prefix; "" if absent.
    fact: str       # Progress fact; numbers, dates, IDs, and links are verbatim.


class ProgramManagementTask(TypedDict):
    """One employee-owned task, ready to become one Program Management row."""

    project_id: str
    project_name: str
    task: str
    status: str                       # in_progress | completed | blocked | unknown
    progress_updates: list[ProgramTaskProgress]
    blockers: list[str]
    completion_criteria: list[str]    # Includes deliverable descriptions and source URLs.
    actual_completion_date: str       # Only present when the report clearly states completion.
    overdue: str                      # yes | no | unknown; never guess when no due-date evidence.


class EmployeeProgramManagementTasks(TypedDict):
    """Program-management extraction result for one employee report."""

    name: str
    tasks: list[ProgramManagementTask]
    quality_note: str


_PROGRAM_MANAGEMENT_EXTRACT_SYSTEM = """你是一名项目管理事实结构化助手。把一名员工的原始周报拆解成可写入【项目管理多维表格】的任务更新。

────────────────────────────
输入
────────────────────────────
- name: 周报作者姓名
- raw_report: 该员工本周周报原文

────────────────────────────
输出
────────────────────────────
仅输出严格 JSON；不要 Markdown 代码块、不要解释、不要输出 JSON 之外的文字：
{
  "tasks": [
    {
      "project_id": "<稳定的小写项目 id；无项目时 _misc>",
      "project_name": "<项目显示名；无项目时 未归类>",
      "task": "<一个可跟踪的具体任务，不要写员工姓名>",
      "status": "in_progress | completed | blocked | unknown",
      "progress_updates": [
        {
          "as_of": "<原文明确出现的日期/时间，如 08.07、2026-08-07 16:00；未出现则空字符串>",
          "fact": "<截至该时间点的进展事实>"
        }
      ],
      "blockers": ["<阻塞原因；无则 []>"],
      "completion_criteria": ["<完成标准、交付物说明或原文中的文件链接；无则 []>"],
      "actual_completion_date": "<仅任务明确已完成时填写原文中的实际完成日期；否则空字符串>",
      "overdue": "yes | no | unknown"
    }
  ],
  "quality_note": ""
}

────────────────────────────
核心规则（HARD）
────────────────────────────
1. **事实保真**
   - 只能提取 raw_report 中明确出现的事实；不得补全、猜测、延伸或虚构。
   - 数字、百分比、JIRA/缺陷号、版本号、日期、时间、系统名、文件链接必须逐字保留。
   - 一个句子中有多个独立任务或进展时，拆成多条 task / progress_updates。

2. **项目与任务**
   - 同一项目的不同任务必须分别输出；不要把多个任务合并成一个泛泛任务。
   - project_id 只用于同名项目归一：小写、短横线、英文/数字；无明确项目则 `_misc`。
   - task 必须是可执行/可验收的事项，不要写“持续推进”“支持相关工作”这类空泛标题。

3. **进展与截至时间**
   - 每个 progress_updates.fact 只写一条原子进展事实。
   - as_of 只能使用原文明确写出的日期或时间；没有则必须是空字符串，绝不能根据今天、周次或文件名推断。
   - 未来写入 Bitable 时，调用方可将有时间的记录渲染为 `截至{as_of}：{fact}`。

4. **阻塞原因**
   - 仅提取明确写出的风险、依赖、等待项、缺陷、资源/权限问题或“阻塞/卡住”等表述。
   - 没有明确阻塞时 blockers 必须是 []；不能把“下周计划”或普通待办当作阻塞。

5. **完成标准 / 交付物**
   - 仅提取原文明确提到的验收标准、交付物、文档、代码包、测试报告等。
   - 原文中的 URL 必须原样放入 completion_criteria，不能省略、改写或编造链接。

6. **实际完成日期与逾期**
   - actual_completion_date 只有在任务明确“已完成/已交付/已上线”等，且原文给出实际完成日期时才填写；否则空字符串。
   - overdue 只有在原文明确说明“逾期/延期/未按期”，或同时给出计划截止日期与更晚的实际完成日期时为 `yes`。
   - 原文明确说明按期完成/未逾期，或能从明确日期直接比较得出按期完成时为 `no`。
   - 其余所有情况一律为 `unknown`；绝不能猜测。

7. **质量标记**
   - 周报过短、没有可跟踪任务或完全无关时 tasks=[]，quality_note 写不超过 15 字中文原因。
   - 正常时 quality_note=""。

不要排序、不要跨员工去重、不要把任务改写成管理层摘要。"""


def build_program_management_extract_prompt(name: str, raw_report: str) -> tuple[str, str]:
    """Build the LLM prompt for one employee's program-management tasks."""
    user = (
        f"name: {name}\n\n"
        f"raw_report:\n---\n{raw_report}\n---\n\n"
        "请按规则输出 JSON。"
    )
    return _PROGRAM_MANAGEMENT_EXTRACT_SYSTEM, user


def _string_list(value) -> list[str]:
    """Keep non-empty string values only, preserving their source order."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


async def extract_program_management_tasks(
    name: str,
    raw_report: str,
    llm_provider,
    session_id: str,
) -> EmployeeProgramManagementTasks:
    """Extract program-management tasks from one employee weekly report.

    This is intentionally equivalent in role to ``extract_employee_facts`` in
    ``services.report``: one bounded LLM call, strict JSON parsing, and a safe
    empty result on provider or format failure.  It does not call Feishu APIs
    or persist anything.
    """
    system_prompt, prompt = build_program_management_extract_prompt(name, raw_report)
    empty: EmployeeProgramManagementTasks = {
        "name": name,
        "tasks": [],
        "quality_note": "",
    }

    try:
        response = await llm_provider.text_chat(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=session_id,
        )
    except Exception as exc:
        logger.warning(f"[ProgramManagement][{name}] LLM 调用失败: {exc}")
        return {**empty, "quality_note": "LLM 调用失败"}

    raw = (getattr(response, "completion_text", "") or "").strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) >= 2 else raw

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            f"[ProgramManagement][{name}] JSON 解析失败: {exc} | 原始: {raw[:200]}"
        )
        return {**empty, "quality_note": "返回格式异常"}

    if not isinstance(parsed, dict):
        return {**empty, "quality_note": "返回格式异常"}

    valid_statuses = {"in_progress", "completed", "blocked", "unknown"}
    valid_overdue = {"yes", "no", "unknown"}
    tasks: list[ProgramManagementTask] = []

    for raw_task in parsed.get("tasks", []) or []:
        if not isinstance(raw_task, dict):
            continue

        task = str(raw_task.get("task", "") or "").strip()
        if not task:
            continue

        status = str(raw_task.get("status", "unknown") or "unknown").strip().lower()
        if status not in valid_statuses:
            status = "unknown"

        overdue = str(raw_task.get("overdue", "unknown") or "unknown").strip().lower()
        if overdue not in valid_overdue:
            overdue = "unknown"

        actual_completion_date = (
            str(raw_task.get("actual_completion_date", "") or "").strip()
            if status == "completed"
            else ""
        )

        progress_updates: list[ProgramTaskProgress] = []
        for raw_update in raw_task.get("progress_updates", []) or []:
            if not isinstance(raw_update, dict):
                continue
            fact = str(raw_update.get("fact", "") or "").strip()
            if not fact:
                continue
            progress_updates.append(
                {
                    "as_of": str(raw_update.get("as_of", "") or "").strip(),
                    "fact": fact,
                }
            )

        tasks.append(
            {
                "project_id": str(raw_task.get("project_id", "_misc") or "_misc").strip().lower() or "_misc",
                "project_name": str(raw_task.get("project_name", "未归类") or "未归类").strip() or "未归类",
                "task": task,
                "status": status,
                "progress_updates": progress_updates,
                "blockers": _string_list(raw_task.get("blockers")),
                "completion_criteria": _string_list(raw_task.get("completion_criteria")),
                "actual_completion_date": actual_completion_date,
                "overdue": overdue,
            }
        )

    return {
        "name": name,
        "tasks": tasks,
        "quality_note": str(parsed.get("quality_note", "") or "").strip(),
    }


def format_task_for_bitable_cell(task: ProgramManagementTask) -> str:
    """Render one extracted task as readable multi-line Bitable cell text.

    This helper only formats an already extracted task. It does not update a
    Bitable. Missing source timestamps remain visibly unspecified rather than
    being invented.
    """
    lines = [task["task"]]
    for update in task["progress_updates"]:
        prefix = f"截至{update['as_of']}" if update["as_of"] else "截至（原文未提供时间）"
        lines.append(f"{prefix}：{update['fact']}")
    if task["blockers"]:
        lines.append(f"阻塞原因：{'；'.join(task['blockers'])}")
    if task["completion_criteria"]:
        lines.append(f"完成标准/交付物：{'；'.join(task['completion_criteria'])}")
    if task["actual_completion_date"]:
        lines.append(f"实际完成日期：{task['actual_completion_date']}")
    lines.append(f"是否逾期：{task['overdue']}")
    return "\n".join(lines)
