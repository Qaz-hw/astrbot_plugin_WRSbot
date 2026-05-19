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
#    - Card definitions (lives in services/lark_card.py)
#=====================================================

import json
from astrbot.api import logger
from .bitable import BitableService
from .doc import DocService
from .contact import ContactService


# ── Prompt builders ─────────────────────────────────────────────────────────
# Kept private to this module — they're only called by the public functions
# below (check_submissions / summarize_reports / rewrite_summary).

def _build_submission_check_prompt(members: list[dict], content: str, file_type: str) -> str:
    member_lines = "\n".join(
        f"- {m['name']}" + (f"（{m['job_title']}）" if m.get("job_title") else "")
        for m in members
    )
    format_hint = (
        "多维表格（每条 [记录 N] 代表一份提交，字段名和值已展开）"
        if file_type == "bitable"
        else "飞书文档纯文本（各成员报告可能以姓名、章节或段落区分）"
    )
    return (
        f"以下是本部门本周应提交周报的成员列表：\n"
        f"{member_lines}\n\n"
        f"以下是本周周报文件的内容（格式：{format_hint}）：\n"
        f"---\n{content}\n---\n\n"
        f"请根据文件内容，判断以上每位成员是否已提交本周周报。\n"
        f"匹配规则：\n"
        f"  - 以成员姓名为主要匹配依据\n"
        f"  - 若文件中出现该成员姓名且有对应内容，视为已提交\n"
        f"  - 若无法确认，归入未提交\n\n"
        f"仅以以下 JSON 格式回复，不要添加任何解释或其他内容：\n"
        f'{{"submitted": ["姓名A", "姓名B"], "not_submitted": ["姓名C"]}}'
    )


_SUMMARIZE_SYSTEM = """你是一名企业部门周报汇总助手。
你的任务是将多名成员的个人周报整合成一份结构清晰的部门周报草稿。

输出规则：
- 严格按照以下四个章节输出，不得增删章节
- 章节标题只允许使用加粗字体
- 每个章节列出2~6条要点，每条以「- 」开头
- 只使用原文提供的信息，不得虚构或推断不存在的内容
- 不要在输出中提及成员姓名，只呈现工作内容
- 不使用"总体来说"、"首先其次"、"在当今"等 AI 习惯用语
- 语言简洁客观，使用职场用语：推进、落地、对齐、复盘、跟进、输出、闭环

输出格式（严格遵守）：
本周KPI与业务进展
- ...

重点项目进展
- ...

风险与阻塞项
- ...

下周计划
- ..."""


def _build_summarize_prompt(content: str, dept_name: str, iso_week: str) -> tuple[str, str]:
    user = (
        f"以下是【{dept_name}】{iso_week} 的全体成员周报内容：\n\n"
        f"---\n{content}\n---\n\n"
        f"请按照四章节格式，输出部门周报汇总草稿。"
    )
    return _SUMMARIZE_SYSTEM, user


_REWRITE_SYSTEM = """你是一名企业报告润色编辑。
你的任务是将结构化的周报草稿改写为专业的管理层汇报风格。

改写规则：
- 保留草稿的所有事实内容，不得删减或虚构
- 语言简洁、客观、职场化，避免口语和学术腔
- 禁止使用以下表达：总体来说、综上所述、首先其次最后、在当今、值得注意的是
- 优先使用以下职场词汇：推进、落地、对齐、复盘、跟进、输出、闭环、拉齐
- 段落精炼，每条要点不超过两句话
- 保持原有的四章节结构（## 标题格式不变）"""


def _build_rewrite_prompt(draft: str, style_input: str = "") -> tuple[str, str]:
    style_section = ""
    if style_input.strip():
        style_section = f"额外改写要求（优先执行）：{style_input.strip()}\n\n"
    user = (
        f"{style_section}"
        f"请将以下周报草稿按照上述规则进行改写：\n\n"
        f"---\n{draft}\n---"
    )
    return _REWRITE_SYSTEM, user


async def check_submissions(
    weekly_file: dict,
    sender_id: str,
    contact_service: ContactService,
    doc_service: DocService,
    bitable_service: BitableService,
    llm_provider,
    session_id: str,
) -> dict:
    """Read this week's report content and ask the LLM who has submitted.

    weekly_file:  file dict from _step_find_weekly —
                  keys: type, token; bitable also needs table_id, table_name
    sender_id:    open_id of requesting user — used to locate their dept in org tree
    session_id:   AstrBot unified_msg_origin, forwarded to the LLM call

    Returns:
    {
        "ok":            bool,
        "error":         str | None,
        "dept_name":     str,
        "members":       list[dict],   # [{name, open_id, job_title}]
        "submitted":     list[str],    # names confirmed submitted
        "not_submitted": list[str],
        "total":         int,
        "file_type":     str,
    }
    """
    result: dict = {
        "ok":            False,
        "error":         None,
        "dept_name":     "",
        "members":       [],
        "submitted":     [],
        "not_submitted": [],
        "total":         0,
        "file_type":     weekly_file.get("type", ""),
    }

    # ── Read file content ────────────────────────────────────────────────────
    file_type  = weekly_file["type"]
    file_token = weekly_file["token"]

    try:
        if file_type == "bitable":
            table_id = weekly_file.get("table_id", "")
            if not table_id:
                result["error"] = "Bitable table_id missing — run week-file discovery first"
                return result
            records = await bitable_service.list_records(file_token, table_id)
            content = BitableService.records_to_text(records)
            logger.debug(f"[check_submissions] bitable: {len(records)} records")

        elif file_type in ("docx", "doc"):
            content = await doc_service.read_doc_blocks(file_token)
            logger.debug(f"[check_submissions] doc: {len(content)} chars")

        else:
            result["error"] = f"Unsupported file type: {file_type!r}"
            return result

    except Exception as e:
        result["error"] = f"读取文件内容失败: {e}"
        return result

    # ── Resolve dept members from org tree ───────────────────────────────────
    members: list[dict] = []
    dept_name = ""

    try:
        org_tree = await contact_service.get_cached_org_tree()
        for entry in org_tree:
            in_dept = any(
                getattr(m, "open_id", None) == sender_id
                for m in entry.get("members", [])
            )
            if not in_dept:
                continue
            dept_name = entry["dept"].name or ""
            members = [
                {
                    "name":      m.name or "",
                    "open_id":   m.open_id or "",
                    "job_title": getattr(m, "job_title", "") or "",
                }
                for m in entry.get("members", [])
            ]
            break
    except Exception as e:
        result["error"] = f"获取部门成员失败: {e}"
        return result

    if not members:
        result["error"] = f"未在通讯录中找到 sender_id={sender_id} 所属部门"
        return result

    result["dept_name"] = dept_name
    result["members"]   = members
    result["total"]     = len(members)

    # ── LLM submission check ─────────────────────────────────────────────────
    prompt = _build_submission_check_prompt(members, content, file_type)
    resp   = None

    try:
        resp   = await llm_provider.text_chat(prompt=prompt, session_id=session_id)
        raw    = resp.completion_text.strip()

        # Strip markdown code fences if the LLM wraps the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()

        parsed = json.loads(raw)
        result["submitted"]     = parsed.get("submitted", [])
        result["not_submitted"] = parsed.get("not_submitted", [])
        result["ok"] = True
        logger.info(
            f"[check_submissions] dept={dept_name} "
            f"submitted={len(result['submitted'])}/{result['total']}"
        )

    except json.JSONDecodeError as e:
        raw_preview = getattr(resp, "completion_text", "")[:200]
        result["error"] = f"LLM 返回非 JSON: {e} | 原始: {raw_preview}"
    except Exception as e:
        result["error"] = f"LLM 调用失败: {e}"

    return result


async def summarize_reports(
    content: str,
    dept_name: str,
    iso_week: str,
    llm_provider,
    session_id: str,
) -> str:
    """LLM pass 1 — summarize raw report content into a structured 4-section draft.

    content:   serialized text of bitable records or doc blocks
    iso_week:  e.g. "2026-W21"
    Returns the LLM's completion text (structured markdown draft).
    """
    sys_p, usr_p = _build_summarize_prompt(content, dept_name, iso_week)
    resp = await llm_provider.text_chat(
        prompt=usr_p,
        system_prompt=sys_p,
        session_id=session_id,
    )
    return resp.completion_text.strip()


async def rewrite_summary(
    draft: str,
    llm_provider,
    session_id: str,
    style_input: str = "",
) -> str:
    """LLM pass 2 — rewrite a structured draft into polished managerial style.

    draft:       output from summarize_reports()
    style_input: optional free-form style instructions from the manager
    Returns the final polished report text.
    """
    sys_p, usr_p = _build_rewrite_prompt(draft, style_input)
    resp = await llm_provider.text_chat(
        prompt=usr_p,
        system_prompt=sys_p,
        session_id=session_id,
    )
    return resp.completion_text.strip()


## ReportService (not yet implemented)
#
# class ReportService:
#     __init__(lark_api, llm_provider, card_service: LarkCardService)
#
#     generate(chat_id, manager_open_id) -> ReportResult
#         # full pipeline — see module docstring above
#
#     _load_persona(manager_open_id) -> ManagerPersona
#     _fetch_submissions(source, config) -> List[WeeklySubmission]
#     _call_llm_summarize(submissions) -> str
#     _call_llm_rewrite(draft, persona) -> str
#     detect_week_range() -> tuple[date, date]
