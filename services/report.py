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

import asyncio
import json
import re
from typing import AsyncGenerator, TypedDict
from astrbot.api import logger
from .bitable import BitableService
from .doc import DocService
from .contact import ContactService


# ── 3-stage pipeline types ────────────────────────────────────────────────────
# Stage A (extract): per-employee → EmployeeFacts  (list[FactItem] + quality_note)
# Stage B (plan):    EmployeeFacts[] → ReportPlan  (dedup, group by project, rank)
# Stage C (write):   ReportPlan + style → Markdown (streamed to CardKit)
#
# Each FactItem carries its project_id and category, so plan stage can group
# deterministically by project_id and let the LLM focus on dedup + ranking
# instead of re-parsing free text.

class FactItem(TypedDict):
    """One atomic fact extracted from one employee's report.

    Each bullet/line becomes ONE FactItem. Extract stage atomizes
    ("did A and B" → 2 items) and tags with project + category.
    """
    category:     str   # "kpi" | "projects" | "risks" | "next_week"
    project_id:   str   # normalized lowercase id; "_misc" if no project
    project_name: str   # human-readable display name
    text:         str   # the atomic fact (numbers/IDs verbatim)


class EmployeeFacts(TypedDict):
    """Structured facts from ONE source (employee in bitable, or "团队" in doc)."""
    name:         str             # source label — employee name or "团队"
    items:        list[FactItem]  # flat list; plan stage will group/dedup
    quality_note: str             # "" if fine; ≤15-char phrase if sparse/off-topic


class ProjectRollup(TypedDict):
    """One project's slot in the ReportPlan, post-dedup + ranking.

    No owners field — managers don't want personal attribution in
    project blocks. Personal attribution only appears in low_quality_members.
    """
    project_id:     str
    display_name:   str
    importance:     int            # 1-5; only ≥2 makes it into the plan
    kpi:            list[str]
    projects:       list[str]
    risks:          list[str]
    next_week:      list[str]
    merged_aliases: list[str]      # other project_ids merged into this one


class LowQualityMember(TypedDict):
    name:   str
    reason: str


class ReportPlan(TypedDict):
    """Complete blueprint for the write stage. Everything that ships in the
    final report must come from this structure — write stage cannot
    fabricate."""
    cross_cutting_highlights: list[str]          # 1-3 top-of-report bullets
    projects:                 list[ProjectRollup]  # sorted by importance desc
    low_quality_members:      list[LowQualityMember]


# ── Markdown → plain text helpers ──────────────────────────────────────────
# Used when writing the report into a Bitable cell — the LLM emits markdown
# (####, -, **bold**) but cells render the raw markers literally. Strip them
# for readability while keeping line structure intact.

_HEADING_LINE_RE     = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_BULLET_LINE_RE      = re.compile(r"^(\s*)[-*]\s+(.+)$")
_BOLD_INLINE_RE      = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_INLINE_RE    = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def _md_to_plain(text: str) -> str:
    """Strip markdown markers for cleaner display in a Bitable cell.

    Keeps line structure (newlines) so multi-line cells remain readable.
    - `#### Title`   → `Title`
    - `- item`       → `• item`
    - `**bold**`     → `bold`
    - `*italic*`     → `italic`
    """
    out_lines: list[str] = []
    for line in text.splitlines():
        s = line
        h = _HEADING_LINE_RE.match(s)
        if h:
            s = h.group(1)
        else:
            b = _BULLET_LINE_RE.match(s)
            if b:
                s = f"{b.group(1)}• {b.group(2)}"
        s = _BOLD_INLINE_RE.sub(r"\1", s)
        s = _ITALIC_INLINE_RE.sub(r"\1", s)
        out_lines.append(s)
    return "\n".join(out_lines).strip()


# ── Prompt builders ─────────────────────────────────────────────────────────
# Kept private to this module — they're only called by the public functions
# below (check_submissions / rewrite_summary_stream / the 3-stage pipeline
# fns extract_employee_facts / plan_report / write_report_stream).

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




_REWRITE_SYSTEM = """你是一名向部门负责人汇报的资深主管文笔助手。你的任务是按管理者给出的【额外改写要求】+【个人风格档案】，对一份已有的部门周报草稿做实质性的风格与措辞调整，**而不是仅做标点和措辞微调**。

────────────────────────────
事实约束（HARD — 不可违反）
────────────────────────────
- 不得新增、删除、虚构、改写草稿中的任何事实。
- 数字、百分比、JIRA / 缺陷号、版本号、服务 / 模块名、日期、时间点 100% 逐字保留。
- 可重新组织语序、合并表述、缩短句子，但**不能删信息**。
- **不要在正文或项目标题里出现任何员工姓名。** 草稿中如果意外出现了人名，改写时一并去掉（人名只允许在「说明」段保留）。

────────────────────────────
结构约束（保持草稿的项目级布局）
────────────────────────────
草稿采用项目级（project-first）布局：
    #### 🌟 本周高光
    #### 📝 说明（可选）
    #### 📊 项目进展
    ### {项目 A}
        **本周完成** / **项目进展** / **风险与阻塞** / **下周计划**
    ### {项目 B}
        ...
    #### 🗒️ 其他事项（可选）

改写规则：
- **保留 ### 项目级标题不变**（项目名不要换、不要合并不同项目、不要拆分）。
- 项目标题里**只有项目名**，绝不要带 "(负责人：…)"。
- 每个项目内部的 4 个子标题（**本周完成** / **项目进展** / **风险与阻塞** / **下周计划**）保留。
- 「说明」段（如有）必须原样保留，置于所有项目之前。
- 「本周高光」段保留 1-3 条加粗强调形式。
- 「其他事项」段（如有）保留。

────────────────────────────
风格转换（必须执行，不是可选）
────────────────────────────
- 每条 bullet 1-2 句话，先写结果 / 状态，再写影响 / 后续。
- 主动语态、结果先行（"完成 X，性能 +12%" 优于 "为了 X，本周做了 Y"）。
- 同一项目内，按重要性排序：业务影响 > 关键里程碑 > 风险 > 文档/内部优化。
- 不复述项目标题（不要在 "本周完成" 段下写 "本周完成了..."）。
- 抽象层级要统一：所有 bullet 在同一项目级 / 里程碑级，不要混入实现细节
  （如 Redis key 拆分、helper 函数重构、单测补充等）；如果发现草稿里有这种粒度，
  在改写时**滚动合并**为更高层级的表述（参考 plan 阶段的合并风格）。

────────────────────────────
个人风格档案应用
────────────────────────────
- style_profile 提供的 actuator 字段（sentence_length / formality / voice /
  emoji_density / preferred_transitions / banned_phrases / signature_phrases /
  extras）必须按 [write 阶段] 的规则执行。
- banned_phrases 列表中的词**绝对不出现**——禁用词由 style_profile 决定，
  不再在本系统提示里硬编码全局禁用词。
- signature_phrases 可以自然融入（不要堆砌）。
- 如果 style_profile 为空，使用默认值：formality=3, voice=neutral, emoji_density=0.3。

────────────────────────────
自检（输出前必须满足）
────────────────────────────
- 改写结果与草稿逐项目逐 bullet 对照，事实是否 100% 保留？必须 ✓
- ### 项目标题是否完全没改？必须 ✓
- 4 个子标题（本周完成 / 项目进展 / 风险与阻塞 / 下周计划）是否在每个项目下都齐？必须 ✓
- 正文是否完全不出现员工姓名？必须 ✓
- 改动是否只是换同义词 / 加句号 / 调"完成"和"已完成"？如果是，**回到风格规则重新改写**。

直接输出改写后的 Markdown，不要解释、不要 ```代码块``` 包裹。"""


def _build_rewrite_prompt(
    draft: str,
    style_input: str = "",
    not_submitted: list[str] | None = None,
    style_profile: dict | None = None,
) -> tuple[str, str]:
    from ..utils.manager_style import render_style_for_prompt

    style_section = ""
    if style_input.strip():
        style_section = f"额外改写要求（优先执行）：{style_input.strip()}\n\n"

    # Per-manager saved profile — applied to rewrites too so the rewrite
    # button respects the same voice anchors as initial generation.
    profile_block = render_style_for_prompt(style_profile)
    profile_section = (
        f"## 个人风格档案（仅影响语气/措辞；不可删改事实）\n{profile_block}\n\n"
        if profile_block else ""
    )

    context_section = ""
    if not_submitted:
        context_section = (
            f"上下文：本周未提交周报的成员为 {'、'.join(not_submitted)}。"
            f"草稿中应已包含「说明」段落；改写时务必保留。\n\n"
        )
    user = (
        f"{style_section}"
        f"{profile_section}"
        f"{context_section}"
        f"请将以下周报草稿按照上述规则进行改写：\n\n"
        f"---\n{draft}\n---"
    )

    # ── Verbose prompt logging (only when a personal style profile is in use) ──
    # Mirrors the generate path so rewrite calls also expose what's actually
    # being sent. Gated on profile_block — style_input alone (no saved profile)
    # doesn't trigger verbose logs.
    if profile_block:
        logger.info(
            f"[Prompt][Rewrite] system_prompt: {len(_REWRITE_SYSTEM)} 字符 (constant _REWRITE_SYSTEM)"
        )
        if style_section:
            logger.info(f"[Prompt][Rewrite] + style_section (用户即席改写要求):\n{style_section.rstrip()}")
        logger.info(f"[Prompt][Rewrite] + profile_section (个人风格档案):\n{profile_section.rstrip()}")
        if context_section:
            logger.info(f"[Prompt][Rewrite] + context_section:\n{context_section.rstrip()}")
        logger.info(
            f"[Prompt][Rewrite] + draft: {len(draft)} 字符 raw draft omitted"
        )
        logger.info(f"[Prompt][Rewrite] total user prompt: {len(user)} 字符")

    return _REWRITE_SYSTEM, user


_COLUMN_SPLIT_SYSTEM = """你是一名报告分配助手。
你的任务是把一份部门周报的内容，按章节分配到目标 Bitable 表格的列中，以便结构化展示。

分配规则：
- 仅可使用「列名列表」中给出的列名作为 JSON 的 key，绝不能虚构新列
- 一个章节内容分配到最相关的一列；若多个章节内容相近，可合并到同一列
- 未匹配任何列的章节内容请丢弃，不要硬塞到无关列
- 同一份内容不要重复出现在多个列中
- 「说明」段落（如有）单独分配到名称含「说明」或「备注」的列；若没有此列则丢弃
- 「姓名」列固定写入字符串「部门总结」
- value 必须是去除 markdown 标记的纯文本：
    · 删除 #### / ### / ## / # 等标题符号
    · 删除 ** 加粗、* 斜体 等行内符号
    · 把 - 或 * 开头的要点改写为「• 」开头
    · 保留换行作为段落分隔
- 仅输出合法 JSON，不要包裹在 ```json``` 代码块中，不要附加任何说明文字"""


def _build_column_split_prompt(report_text: str, column_names: list[str]) -> tuple[str, str]:
    user = (
        f"列名列表：\n"
        + "\n".join(f"- {n}" for n in column_names)
        + f"\n\n周报草稿：\n---\n{report_text}\n---\n\n"
        f'请输出 JSON，形如 {{"姓名": "部门总结", "<列名1>": "<内容1>", "<列名2>": "<内容2>", ...}}'
    )
    return _COLUMN_SPLIT_SYSTEM, user


async def split_report_to_columns(
    report_text: str,
    column_names: list[str],
    llm_provider,
    session_id: str,
) -> dict[str, str]:
    """Ask the LLM to map a report's sections to Bitable column names.

    Returns {column_name: cleaned_content}. The model is instructed to strip
    markdown markers, but we also run _md_to_plain as a safety net in case it
    leaves some behind. Returns {} on parse / LLM failure — caller should
    fall back to single-cell write.
    """
    sys_p, usr_p = _build_column_split_prompt(report_text, column_names)
    try:
        resp = await llm_provider.text_chat(
            prompt=usr_p, system_prompt=sys_p, session_id=session_id,
        )
    except Exception as e:
        logger.warning(f"[ColumnSplit] LLM 调用失败: {e}")
        return {}

    raw = (resp.completion_text or "").strip()
    if raw.startswith("```"):
        # Strip code fences if the model added them despite the instruction
        raw = raw.split("```")[1]
        raw = raw.lstrip("json").strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"[ColumnSplit] LLM 返回非 JSON: {e} | 原始: {raw[:200]}")
        return {}

    if not isinstance(parsed, dict):
        logger.warning(f"[ColumnSplit] LLM 返回非对象: {type(parsed).__name__}")
        return {}

    # Filter to valid columns + final markdown safety-net
    valid_cols = set(column_names)
    cleaned: dict[str, str] = {}
    for k, v in parsed.items():
        if k not in valid_cols:
            logger.debug(f"[ColumnSplit] 丢弃未匹配列: {k!r}")
            continue
        text_v = v if isinstance(v, str) else str(v)
        cleaned[k] = _md_to_plain(text_v)
    return cleaned


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
        org_tree = await contact_service.get_cached_org_tree(sender_id)
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


# ── Streaming variants ─────────────────────────────────────────────────────
# Yield cumulative text snapshots (not deltas) — Feishu's CardKit streaming
# update API expects the full accumulated text each tick.

async def _consume_provider_stream(llm_stream) -> AsyncGenerator[str, None]:
    """Adapt AstrBot's text_chat_stream into cumulative-text snapshots.

    Provider semantics: intermediate yields carry is_chunk=True with the delta
    in result_chain; the closing yield has is_chunk=False with the full cleaned
    text. We accumulate deltas and prefer the final cleaned text when it arrives.
    """
    accumulated = ""
    async for resp in llm_stream:
        if resp.is_chunk:
            if resp.result_chain and resp.result_chain.chain:
                for comp in resp.result_chain.chain:
                    text = getattr(comp, "text", "")
                    if text:
                        accumulated += text
            if accumulated:
                yield accumulated
        else:
            final = (resp.completion_text or accumulated).strip()
            if final:
                yield final
            return
    if accumulated:
        yield accumulated.strip()


# ─────────────────────────────────────────────────────────────────────────────
# DEAD CODE (2026-05-21): generate_report_stream
#
# Single-call generate path. Replaced by the 3-stage pipeline (see
# generate_report_stream_mapreduce below). No pipeline calls this anymore;
# preserved as reference. Safe to delete once 3-stage is proven.
# ─────────────────────────────────────────────────────────────────────────────
'''
async def generate_report_stream(
    content: str,
    dept_name: str,
    iso_week: str,
    llm_provider,
    session_id: str,
    submitted: list[str] | None = None,
    not_submitted: list[str] | None = None,
    style_profile: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Single-pass report generation — fact extraction + executive style in one LLM call."""
    sys_p, usr_p = _build_generate_prompt(
        content, dept_name, iso_week,
        submitted=submitted, not_submitted=not_submitted,
        style_profile=style_profile,
    )
    stream = llm_provider.text_chat_stream(
        prompt=usr_p, system_prompt=sys_p, session_id=session_id,
    )
    async for snapshot in _consume_provider_stream(stream):
        yield snapshot
'''
# ─── END DEAD CODE ──────────────────────────────────────────────────────────


# ── Map-reduce report generation ──────────────────────────────────────────────
# Variant 1: per-employee fact extraction (parallel, small) → single reducer
# (streaming, full report). The map phase bounds attention per call (each
# call sees ONE employee), making the architecture scalable to large depts
# without quality degradation. The reduce phase preserves the existing
# CardKit streaming UX since only its output streams to the user.
#
# Two new system prompts:
#   _EXTRACT_FACTS_SYSTEM — runs N times in parallel, one per employee
#   _REDUCE_SYSTEM        — runs once, takes structured facts → final report
#
# Stage prompts are separated by responsibility:
#   _EXTRACT_FACTS_SYSTEM tags atomic facts with project + category.
#   _PLAN_SYSTEM dedups / groups / ranks / compresses into a ReportPlan.
#   _WRITE_SYSTEM renders the plan as project-centric Markdown.
# Each stage gets focused attention budget instead of one prompt juggling
# extraction + synthesis + rendering simultaneously.
#
# ─── SCALE TARGET ───────────────────────────────────────────────────────────
# Current design assumes 30–50 employees per dept. At this size, two stages
# (map: per-employee extract; reduce: dept synthesis) gives good attention
# focus + fits comfortably in single-call reducer context.
#
# TODO [if scale moves to 100+ employees per dept]: upgrade to a 3-stage
#   pipeline so the reducer never sees more than ~10 inputs:
#
#       extract   (parallel, per employee)
#         → ProjectRollup     (single call, JSON in / JSON out)
#             - groups EmployeeFacts by `project_id` extracted in stage 1
#             - dedups across employees, ranks by importance
#             - drops noise (one-off mentions, low-quality submissions)
#         → write             (single streaming call, JSON in / Markdown out)
#             - facts arrive STRUCTURED so the model can't fabricate;
#               it only reorders/rephrases what's in the rollup
#             - style_profile (see [eager style structuring] TODO below)
#               gets primary attention here since structure is already locked
#
#   New shapes to add:
#     class ProjectRollup(TypedDict):
#         project_id:    str   # normalized (lowercase + strip-punct)
#         display_name:  str   # human-readable
#         owners:        list[str]
#         kpi:           list[str]
#         projects:      list[str]
#         risks:         list[str]
#         next_week:     list[str]
#         importance:    int   # 1-5; reducer drops project_id with imp<2
#
#     class ReportPlan(TypedDict):
#         projects:                  list[ProjectRollup]
#         cross_cutting_highlights:  list[str]   # for the top of the report
#         low_quality_members:       list[str]   # surfaced in 「说明」 section
#
# TODO [eager style structuring]:
#   Today the style profile is read per-report at generate time. At scale
#   it should be normalized ONCE at /save_manager_style time into actuator-
#   level dials (sentence_length, formality_int, banned_phrases,
#   signature_phrases_from_samples) and cached. That's both cheaper and
#   gives the writer concrete knobs instead of vague style tags that get
#   diluted by the model's default "executive Chinese" register.
#
# TODO [project-centric output]:
#   Current reducer emits 4-section structure (KPI / 项目 / 风险 / 下周).
#   At scale, organize by project first: each project gets its own section
#   with the 4 categories WITHIN it. Readers consume project-first; the
#   current section-first layout forces them to mentally reassemble
#   "what's the state of Apollo?" from scattered bullets.

# ── STAGE A: extract ──────────────────────────────────────────────────────────
# Per-employee fact extraction. ONE call sees ONE employee's report and
# atomizes it into project-tagged FactItems. Critical work: project
# normalization (Apollo / 阿波罗 / APL → same project_id) and category
# assignment (kpi / projects / risks / next_week).
#
# Attention budget: facts ≈90%, project tagging ≈8%, quality flag ≈2%.

_EXTRACT_FACTS_SYSTEM = """你是一名周报事实结构化助手。把一份原始周报，逐条拆解成"原子事实"，每条事实打上【项目标签】+【类别标签】。

────────────────────────────
输入
────────────────────────────
- name: 周报作者的姓名（或团队标识）
- raw_report: 该作者本周写的原始周报文字（可能混着流水账、感想、Q&A 等）

────────────────────────────
输出
────────────────────────────
严格 JSON。不要 markdown 代码块，不要任何 JSON 之外的文字。
{
  "items": [
    {
      "category":     "kpi" | "projects" | "risks" | "next_week",
      "project_id":   "<规范化的项目 id>",
      "project_name": "<项目显示名>",
      "text":         "<一句话原子事实>"
    },
    ...
  ],
  "quality_note": ""
}

────────────────────────────
核心规则
────────────────────────────

1. **原子化**
   - 一条 "items" = 一个独立可验证的事实。
   - "完成 A，同时推进 B" 拆成 2 条。
   - 流水账 / 心情 / 总结性套话（"本周整体推进顺利"）丢弃。

2. **项目标签**
   - project_id：纯小写英文/拼音 + 数字 + 下划线，无空格、无标点、无中文。
       好：apollo, beacon_v2   差：Apollo, beacon-v2, 阿波罗
   - project_name：纯人类可读名。**严禁**包含 project_id、英文 id 后缀、括号技术注释。
       ✓ "AI 周报工具"、"订单系统"
       ✗ "AI周报工具（robot）"、"订单系统 (order_system)"
   - 同一项目的不同写法（"Apollo"/"阿波罗"/"APL-2026"）映射到同一 project_id。
   - 无法归属到具体项目的（培训、内部 bug、all-hands 等）用 project_id="_misc"，project_name="其他"。

3. **类别标签**
   - kpi: 已完成的事，最好带量化结果。
   - projects: 仍在推进、未闭环的里程碑。
   - risks: 仍影响进度的阻塞 / 跨组依赖 / 质量 / 上线风险。
   - next_week: 原文里 "下周/下一步/计划" 等明确表述的动作。

4. **事实保真（HARD）**
   - 数字、百分比、JIRA / 缺陷号、版本号、服务名、模块名、日期 — **逐字保留**。
   - 不补原文没出现的影响 / 因果 / 结论。
   - 原文写 "做了一些优化" 就保留原话，不要扩展。

5. **质量标注**
   - quality_note 留空 ""，除非原文严重缺失（< 100 字 + 无具体动作 / 完全偏题 / 与工作无关）。
   - 非空时 ≤15 字，例："内容仅 1 句"、"未提交具体进展"、"与工作无关"。

6. **不做的事情**
   - 不排序、不去重、不分组（plan 阶段处理）。
   - 不润色措辞。"""


def _build_extract_facts_prompt(name: str, raw_report: str) -> tuple[str, str]:
    user = (
        f"name: {name}\n\n"
        f"raw_report:\n---\n{raw_report}\n---\n\n"
        f"请按规则输出 JSON。"
    )
    return _EXTRACT_FACTS_SYSTEM, user


async def extract_employee_facts(
    name: str,
    raw_report: str,
    llm_provider,
    session_id: str,
) -> EmployeeFacts:
    """[STAGE A] Atomize ONE employee's report into project-tagged FactItems.

    Attention focus: facts + project tagging. No cross-employee work
    happens here (that's stage B's job).

    On any LLM/parse failure, returns an EmployeeFacts with items=[] and
    quality_note populated. Plan stage will surface the failure to the
    manager via low_quality_members rather than silently dropping the
    employee.
    """
    sys_p, usr_p = _build_extract_facts_prompt(name, raw_report)
    empty: EmployeeFacts = {
        "name":         name,
        "items":        [],
        "quality_note": "",
    }
    try:
        resp = await llm_provider.text_chat(
            prompt=usr_p, system_prompt=sys_p, session_id=session_id,
        )
    except Exception as e:
        logger.warning(f"[Extract][{name}] LLM 调用失败: {e}")
        return {**empty, "quality_note": "提取失败"}

    raw = (resp.completion_text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"[Extract][{name}] JSON 解析失败: {e} | 原始: {raw[:200]}")
        return {**empty, "quality_note": "返回格式异常"}
    if not isinstance(parsed, dict):
        return {**empty, "quality_note": "返回格式异常"}

    _VALID_CATEGORIES = {"kpi", "projects", "risks", "next_week"}
    items: list[FactItem] = []
    for raw_item in parsed.get("items", []) or []:
        if not isinstance(raw_item, dict):
            continue
        cat = str(raw_item.get("category", "")).strip().lower()
        if cat not in _VALID_CATEGORIES:
            continue
        text = str(raw_item.get("text", "")).strip()
        if not text:
            continue
        # Normalize project_id defensively in case the model slipped on rule 2.
        pid = str(raw_item.get("project_id", "") or "_misc").strip().lower()
        pid = re.sub(r"[^a-z0-9_]+", "_", pid).strip("_") or "_misc"
        pname = str(raw_item.get("project_name", "") or "").strip() or (
            "其他" if pid == "_misc" else pid
        )
        items.append({
            "category":     cat,
            "project_id":   pid,
            "project_name": pname,
            "text":         text,
        })

    return {
        "name":         name,
        "items":        items,
        "quality_note": str(parsed.get("quality_note", "") or ""),
    }


# ── STAGE B: plan ─────────────────────────────────────────────────────────────
# Takes ALL employees' FactItems, dedups across employees, groups by project_id,
# ranks projects by importance, picks cross-cutting highlights. Output is a
# fully-structured ReportPlan — the write stage will not need to make any
# structural decisions, only render this plan as markdown.
#
# Attention budget: dedup ≈40%, ranking ≈30%, project grouping ≈20%,
# highlight selection ≈10%. Facts are inputs — preservation is a hard
# constraint, not an attention cost.

_PLAN_SYSTEM = """你是一名"部门周报规划官"。把多名员工的【原子事实清单】合并、去重、按项目归类、按重要性排序，产出一份【部门周报计划】（ReportPlan）的严格 JSON。

────────────────────────────
输入
────────────────────────────
- dept_name, iso_week: 部门 + 周次
- employees: list[EmployeeFacts]，每个含 name + items(category/project_id/project_name/text) + quality_note
- not_submitted: 本周未提交周报的成员姓名

────────────────────────────
输出
────────────────────────────
严格 JSON。不要 markdown 代码块。无任何 JSON 外的文字。
{
  "cross_cutting_highlights": ["...", "..."],   // 1-3 条，按重要性降序
  "projects": [
    {
      "project_id":     "apollo",
      "display_name":   "Apollo",            // 纯人类可读名；严禁含 project_id 或 (xxx) 后缀
      "importance":     5,
      "kpi":            ["..."],
      "projects":       ["..."],
      "risks":          ["..."],
      "next_week":      ["..."],
      "merged_aliases": []                   // 其他被合并进来的 project_id
    },
    ...                                      // 按 importance 降序
  ],
  "low_quality_members": [
    {"name": "王五", "reason": "周报仅 1 句话"}
  ]
}

注：**没有 owners 字段**。本部门周报不展示个人归属。

────────────────────────────
核心工作
────────────────────────────

1. **跨员工去重**
   - project_id 相同 + 同一目标 → 合并为一句更完整的表述。
   - project_id 不同但其实同项目 → 合并到出现次数多的 id，另一个加入 merged_aliases。

2. **按项目分组**
   - FactItem 按 project_id 聚合到 ProjectRollup。
   - 同一项目内按 category 切到 kpi / projects / risks / next_week。
   - kpi 中"完成"的事项，不在 projects 里重复。

3. **管理层摘要改写**
   - 先讲结论 / 状态 / 影响，再讲细节。
   - 数字 / 缩写词 / 服务名 / JIRA / 版本号 100% 保留原文。
   - 不加 "我们 / 团队 / 本周" 这种主语 / 时间词（write 阶段会处理）。
   - 不加 emoji（write 阶段按 style_profile 决定）。
   - 不凭空补 "为后续奠定基础"、"持续推进"。
   - 不把 "调研了 X" 扩展为 "完成 X 选型"。

4. **importance 评分**
   - 5 = 战略级 / 线上事件 / 高层关注
   - 4 = 多人协作的主要项目（≥3 人涉及，或本周有明确里程碑达成）
   - 3 = 单人推进但有实质性产出
   - 2 = 例行优化 / 文档 / 维护性工作
   - 1 = 一次性提及、无后续 → **drop，不进入输出**
   - _misc 永远归为 importance=2

5. **cross_cutting_highlights 选择**
   报告顶部 1-3 行，**业务视角 / 战略状态 / 重大风险**优先。
   - 加 ** 加粗最关键的项目名 / 数字 / 动作。
   - 单条 15-60 字。
   - 不堆砌技术细节、不写客套话。

6. **low_quality_members 收集**
   - 来自 quality_note 非空的员工：照搬 reason。
   - 来自 not_submitted 名单：reason 写 "未提交"。
   - **严禁** name 为空 / null / "None" / 占位符（如 "未知成员1"）的行——直接丢弃。
   - 每条必须同时有非空 name + 非空 reason，缺一就 drop。

────────────────────────────
事实保真（HARD）
────────────────────────────
- 数字、百分比、JIRA / 缺陷号、版本号、服务 / 模块名、日期 100% 逐字保留。
- project_id 不能创造，只能来自输入或通过 merged_aliases 合并。
- next_week 不能"补建议"，原文没说就不写。
- 不在 text 里出现员工姓名。

────────────────────────────
输出规模
────────────────────────────
- projects 通常 3-6 个。
- 每个 ProjectRollup 的 kpi/projects/risks/next_week 通常各 ≤3 条。
- cross_cutting_highlights 严格 1-3 条。

不要解释，直接输出 JSON。"""


def _build_plan_prompt(
    facts_list: list[EmployeeFacts],
    dept_name: str,
    iso_week: str,
    not_submitted: list[str] | None = None,
) -> tuple[str, str]:
    payload = {
        "dept_name":     dept_name,
        "iso_week":      iso_week,
        "employees":     facts_list,
        "not_submitted": not_submitted or [],
    }
    user = (
        f"以下是输入数据，请生成 ReportPlan JSON：\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )
    return _PLAN_SYSTEM, user


async def plan_report(
    facts_list: list[EmployeeFacts],
    dept_name: str,
    iso_week: str,
    llm_provider,
    session_id: str,
    not_submitted: list[str] | None = None,
) -> ReportPlan:
    """[STAGE B] Build a ReportPlan from per-employee FactItems.

    On parse failure, returns an empty plan with all employees flagged as
    low_quality (so the manager sees the failure mode rather than a blank
    report).
    """
    sys_p, usr_p = _build_plan_prompt(facts_list, dept_name, iso_week, not_submitted)
    empty: ReportPlan = {
        "cross_cutting_highlights": [],
        "projects":                 [],
        "low_quality_members":      [],
    }
    try:
        resp = await llm_provider.text_chat(
            prompt=usr_p, system_prompt=sys_p, session_id=session_id,
        )
    except Exception as e:
        logger.warning(f"[Plan] LLM 调用失败: {e}")
        return {
            **empty,
            "low_quality_members": [
                {"name": f["name"], "reason": "规划阶段失败"} for f in facts_list
            ],
        }

    raw = (resp.completion_text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"[Plan] JSON 解析失败: {e} | 原始: {raw[:300]}")
        return {
            **empty,
            "low_quality_members": [
                {"name": f["name"], "reason": "规划阶段返回非 JSON"} for f in facts_list
            ],
        }
    if not isinstance(parsed, dict):
        return empty

    def _str_list(v) -> list[str]:
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for x in v:
            # Tolerate {severity,text} regressions by flattening to text only.
            if isinstance(x, dict):
                t = str(x.get("text", "") or "").strip()
            else:
                t = str(x).strip()
            if t:
                out.append(t)
        return out

    projects_out: list[ProjectRollup] = []
    for p in parsed.get("projects", []) or []:
        if not isinstance(p, dict):
            continue
        try:
            importance = int(p.get("importance", 0))
        except (TypeError, ValueError):
            importance = 0
        if importance < 2:
            continue  # plan rule: drop importance=1
        projects_out.append({
            "project_id":     str(p.get("project_id", "") or "_misc").strip().lower() or "_misc",
            "display_name":   str(p.get("display_name", "") or "").strip() or "其他",
            "importance":     importance,
            "kpi":            _str_list(p.get("kpi")),
            "projects":       _str_list(p.get("projects")),
            "risks":          _str_list(p.get("risks")),
            "next_week":      _str_list(p.get("next_week")),
            "merged_aliases": _str_list(p.get("merged_aliases")),
        })
    # Sort by importance descending (the model is asked to do this; enforce it)
    projects_out.sort(key=lambda x: -x["importance"])

    # Filter low_quality_members aggressively — only keep entries that have
    # BOTH a real name AND a real reason. The previous filter let JSON null,
    # the literal string "None"/"null", and various placeholder names through,
    # which produced rows like "周报质量待补充: None（返回格式异常）" in the
    # final report. Two-axis filter: name must be human-meaningful AND reason
    # must be non-empty.
    _NAME_BLACKLIST = {"", "none", "null", "无", "未知", "未提供", "n/a", "na"}
    lqm_out: list[LowQualityMember] = []
    for lqm in parsed.get("low_quality_members", []) or []:
        if not isinstance(lqm, dict):
            continue
        name_raw = lqm.get("name")
        if name_raw is None:
            continue
        name = str(name_raw).strip()
        if not name or name.lower() in _NAME_BLACKLIST:
            continue
        # Drop "未知成员N" autoplaceholder that BitableService.split_records_per_employee
        # uses when a record's 姓名 field is empty — those rows have no real
        # signal to report on.
        if re.fullmatch(r"未知成员\d*", name):
            continue
        reason = str(lqm.get("reason", "") or "").strip()
        if not reason:
            continue
        lqm_out.append({"name": name, "reason": reason})

    plan: ReportPlan = {
        "cross_cutting_highlights": _str_list(parsed.get("cross_cutting_highlights"))[:3],
        "projects":                 projects_out,
        "low_quality_members":      lqm_out,
    }
    logger.info(
        f"[Plan] dept={dept_name} → "
        f"{len(plan['projects'])} projects, "
        f"{len(plan['cross_cutting_highlights'])} highlights, "
        f"{len(plan['low_quality_members'])} low-quality"
    )
    return plan


# ── STAGE C: write ────────────────────────────────────────────────────────────
# Renders a ReportPlan as project-centric Markdown. Facts are LOCKED because
# they arrive as structured input; the model can only reorder + rephrase.
# Style is the primary variable here (structure is already decided by plan).
#
# Attention budget: style application ≈45%, readability ≈25%,
# structure rendering ≈20%, compression ≈10%. Fact preservation is a
# hard constraint enforced by the data flow.

_WRITE_SYSTEM = """你是一名资深主管文笔助手。把一份已规划好的【ReportPlan JSON】渲染成 Markdown 部门周报，发给管理层阅读。

────────────────────────────
输入
────────────────────────────
- plan: ReportPlan JSON（已结构化、已去重、已按重要性排序、已挑好高光）
- style_profile: 该管理者的写作风格档案（actuator-level dials；可能为空）
- dept_name, iso_week, submitted, not_submitted: 上下文

────────────────────────────
事实保真（HARD）
────────────────────────────
- plan 里每一条 text 必须出现在最终报告中。可以合并 / 缩短 / 顺序调整，**不能新增 / 删除 / 替换事实**。
- 数字、百分比、JIRA 号、版本号、服务名、日期 100% 逐字保留。
- 不补"持续推进"、"为后续打基础"、"赋能业务"这类填充语句。
- 如果某个 ProjectRollup 的 risks 或 next_week 数组为空，那一段写 "- —"，不要编造内容。
- **不在正文或项目标题里出现任何员工姓名**。plan 没有 owners 字段；本报告只关心项目状态。

────────────────────────────
输出结构
────────────────────────────

#### 🌟 本周高光
{cross_cutting_highlights 每条一行，用 ** 加粗最关键的项目名 / 数字 / 动作}

{ 仅当 low_quality_members 非空 OR not_submitted 非空时输出整个「说明」段；否则省略 }
#### 📝 说明
- 未提交：{not_submitted 顿号连接；如为空跳过此行}
- 周报质量待补充：{low_quality_members 顿号连接，格式 "姓名（reason）"；如为空跳过此行}

#### 📊 项目进展
{按 plan.projects 顺序，每个项目一个 ### 子块；importance≤2 的项目放进最后的「其他事项」}

### {display_name}

display_name 渲染前清洗：如果意外包含 (xxx) / （xxx）/ - xxx 之类的技术 id 后缀
（如 "AI周报工具（robot）"），去掉括号及其内容，只保留 "AI周报工具"。
标题里**只有项目名**，不带 "(负责人：...)" 等人名标注。

**本周完成**
{kpi 数组渲染成 "- xxx" bullets；如为空写 "- —"}

**项目进展**
{projects 数组渲染成 bullets；如为空写 "- —"；如所有里程碑已完成且无 in-flight，整段省略}

**风险与阻塞**
{risks 数组渲染成 bullets；如为空写 "- —"}

**下周计划**
{next_week bullets；如为空写 "- —"}

{ 仅当存在 importance==2 的项目时输出 }
#### 🗒️ 其他事项
{把 importance==2 的项目压缩成一行，格式 "**{display_name}**: 一句话总结"}

────────────────────────────
渲染细则
────────────────────────────

1. **bullet 写法**
   - 1-2 句话。先写结果 / 状态，再写影响 / 后续。
   - 主动语态，结果先行。"完成 X，提升 Y 至 Z%" 优于 "为了 Y，本周完成了 X"。
   - 不在 bullet 里重复 ### 项目标题。

2. **style_profile 应用**
   - sentence_length: short / medium / long → 控制句长。
   - formality 1-5: 1=口语化, 3=正常职场, 5=正式书面。
   - voice: "we" → 用 "我们"；"team" → 用 "团队"；"neutral" → 不出现主语。
   - signature_phrases: 可自然融入（不堆砌）。
   - banned_phrases: 列表里的词**绝对不出现**。常见全局禁用："总体来说"、"综上所述"、"首先其次最后"、"值得注意的是"、"为...奠定基础"、"赋能"、"抓手"、"保驾护航"。
   - emoji_density: 0=去掉章节标题 emoji；0.3-0.7=默认；>0.7=bullet 可加 ✅⚠️📌。

3. **style_profile 为空时**
   默认 formality=3, voice=neutral, emoji_density=0.3, 默认禁用上述全局列表。

────────────────────────────
自检
────────────────────────────
- 每个 importance≥3 的 ProjectRollup 是否独立成块？✓
- 正文是否完全不出现员工姓名？✓
- 输出是否有任何事实不在 plan 里？必须 ✗
- 是否有空段被填充套话？必须 ✗

直接输出 Markdown，不要解释，不要 ```代码块```。"""


def _build_write_prompt(
    plan: ReportPlan,
    dept_name: str,
    iso_week: str,
    submitted: list[str] | None = None,
    not_submitted: list[str] | None = None,
    style_profile: dict | None = None,
) -> tuple[str, str]:
    from ..utils.manager_style import render_style_for_prompt

    style_block = render_style_for_prompt(style_profile)
    style_section = (
        f"## style_profile\n{style_block}\n\n"
        if style_block else "## style_profile\n（未配置；使用默认风格）\n\n"
    )

    status_section = (
        f"## 上下文\n"
        f"- 部门：{dept_name}\n"
        f"- 周次：{iso_week}\n"
        f"- 已提交：{'、'.join(submitted) if submitted else '（无）'}\n"
        f"- 未提交：{'、'.join(not_submitted) if not_submitted else '（无）'}\n\n"
    )

    plan_json = json.dumps(plan, ensure_ascii=False, indent=2)
    user = (
        f"{status_section}"
        f"{style_section}"
        f"## ReportPlan\n```json\n{plan_json}\n```\n\n"
        f"请按系统提示中的结构输出 Markdown。"
    )

    if style_block:
        logger.info(
            f"[Prompt][Write] system={len(_WRITE_SYSTEM)} 字符, "
            f"plan={len(plan['projects'])} projects, "
            f"highlights={len(plan['cross_cutting_highlights'])}, "
            f"style_block_len={len(style_block)}"
        )

    return _WRITE_SYSTEM, user


async def write_report_stream(
    plan: ReportPlan,
    dept_name: str,
    iso_week: str,
    llm_provider,
    session_id: str,
    submitted: list[str] | None = None,
    not_submitted: list[str] | None = None,
    style_profile: dict | None = None,
) -> AsyncGenerator[str, None]:
    """[STAGE C] Stream the final Markdown report from a ReportPlan.

    Yields cumulative text snapshots suitable for the existing CardKit
    streaming path (_stream_to_card in pipelines/_base.py).
    """
    sys_p, usr_p = _build_write_prompt(
        plan, dept_name, iso_week,
        submitted=submitted, not_submitted=not_submitted,
        style_profile=style_profile,
    )
    stream = llm_provider.text_chat_stream(
        prompt=usr_p, system_prompt=sys_p, session_id=session_id,
    )
    async for snapshot in _consume_provider_stream(stream):
        yield snapshot


async def generate_report_stream_mapreduce(
    employee_inputs: list[tuple[str, str]],
    dept_name: str,
    iso_week: str,
    llm_provider,
    session_id: str,
    submitted: list[str] | None = None,
    not_submitted: list[str] | None = None,
    style_profile: dict | None = None,
    semaphore: "asyncio.Semaphore | None" = None,
) -> AsyncGenerator[str, None]:
    """[ORCHESTRATOR] 3-stage report generation: extract → plan → write.

    Stage A (MAP, parallel):  extract_employee_facts per employee. Each call
                              sees ONE source's report, produces project-tagged
                              FactItems. Bounded by the shared LLM semaphore.
    Stage B (single call):    plan_report — dedup across employees, group by
                              project_id, rank by importance, pick highlights.
                              Pure structured-input → structured-output call.
    Stage C (streaming):      write_report_stream — render ReportPlan as
                              project-centric Markdown, applying the manager's
                              structured style profile. Streams to CardKit.

    Progress snapshots are yielded between stages so the streaming card
    shows progress instead of looking frozen during the LLM-heavy phases.

    Args:
        employee_inputs: list of (source_name, raw_report_text). For bitable,
                         each tuple = one employee. For doc-based depts,
                         pass a single tuple ("团队", full_doc_text) since the
                         doc can't be cheaply split per-employee at this layer.
        dept_name:       department name (header context)
        iso_week:        ISO week tag (e.g. "2026-W21")
        llm_provider:    AstrBot provider, used for all 3 stages
        session_id:      session tag prefix; stages append :extract / :plan / :write
        submitted /
        not_submitted:   from check_submissions; informs the plan stage's
                         low_quality_members and the write stage's 说明 段
        style_profile:   per-manager style dict from manager_style.get_manager_style
                         (raw OR structured — render_style_for_prompt handles both)
        semaphore:       LLM concurrency cap for the parallel map fan-out.
                         Pass PipelineBase.llm_semaphore from the caller.

    Yields:
        Cumulative text snapshots for _stream_to_card consumption.
    """
    if semaphore is None:
        semaphore = asyncio.Semaphore(8)

    n = len(employee_inputs)
    logger.info(f"[3Stage] start: dept={dept_name} N={n}")

    # ── Pre-extract progress snapshot ───────────────────────────────────────
    yield f"正在解析 {n} 份周报内容…"

    # ── STAGE A — parallel per-source extraction ────────────────────────────
    async def _bounded_extract(name: str, raw: str) -> EmployeeFacts:
        async with semaphore:
            return await extract_employee_facts(
                name, raw, llm_provider,
                session_id=f"{session_id}:extract:{name}",
            )

    extract_tasks = [
        asyncio.create_task(_bounded_extract(name, raw))
        for name, raw in employee_inputs
    ]
    facts_list: list[EmployeeFacts] = list(await asyncio.gather(*extract_tasks))

    # Map-phase observability: which sources gave us thin output?
    for f in facts_list:
        # Count items per category for quick scan of extraction quality
        by_cat: dict[str, int] = {"kpi": 0, "projects": 0, "risks": 0, "next_week": 0}
        for it in f["items"]:
            cat = it["category"]
            if cat in by_cat:
                by_cat[cat] += 1
        logger.info(
            f"[3Stage][Extract] name={f['name']!r} items={len(f['items'])} "
            f"kpi={by_cat['kpi']} proj={by_cat['projects']} "
            f"risks={by_cat['risks']} next={by_cat['next_week']} "
            f"quality_note={f['quality_note']!r}"
        )

    yield f"已解析 {n} 份周报，正在按项目整合…"

    # ── STAGE B — single planning call (JSON in / JSON out) ─────────────────
    plan = await plan_report(
        facts_list, dept_name, iso_week, llm_provider,
        session_id=f"{session_id}:plan",
        not_submitted=not_submitted,
    )

    if not plan["projects"]:
        # Nothing to write. Surface a soft message instead of an empty card.
        logger.warning(f"[3Stage] plan produced 0 projects for dept={dept_name}")
        yield "本周未抽取到可呈报的项目内容。可能原因：周报内容过少 / 全部被 importance=1 过滤。"
        return

    yield (
        f"已规划 {len(plan['projects'])} 个项目"
        f"（{len(plan['cross_cutting_highlights'])} 条本周高光）"
        f"，正在生成周报…"
    )

    # ── STAGE C — streaming write ────────────────────────────────────────────
    async for snapshot in write_report_stream(
        plan, dept_name, iso_week, llm_provider,
        session_id=f"{session_id}:write",
        submitted=submitted, not_submitted=not_submitted,
        style_profile=style_profile,
    ):
        yield snapshot


async def rewrite_summary_stream(
    draft: str,
    llm_provider,
    session_id: str,
    style_input: str = "",
    not_submitted: list[str] | None = None,
    style_profile: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Streaming variant of rewrite_summary — yields cumulative text snapshots.

    not_submitted: passed through so the rewrite prompt knows to preserve the
    「说明」 section if the draft contains one.
    """
    sys_p, usr_p = _build_rewrite_prompt(
        draft, style_input,
        not_submitted=not_submitted,
        style_profile=style_profile,
    )
    stream = llm_provider.text_chat_stream(
        prompt=usr_p, system_prompt=sys_p, session_id=session_id,
    )
    async for snapshot in _consume_provider_stream(stream):
        yield snapshot


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
