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


# ── Map-reduce types ──────────────────────────────────────────────────────────
# Used by generate_report_stream_mapreduce (variant 1). Each employee's raw
# report becomes one EmployeeFacts via extract_employee_facts; the reducer
# consumes a list of these.

class EmployeeFacts(TypedDict):
    """Structured facts extracted from ONE employee's weekly report."""
    name:          str        # employee name as it appears in 通讯录 / 表格
    kpi:           list[str]  # results, launches, closed loops, quantified outcomes
    projects:      list[str]  # in-flight project milestones (not yet closed)
    risks:         list[str]  # blockers, delays, quality/launch concerns
    next_week:     list[str]  # explicit next-week actions
    quality_note:  str        # "" if fine; short phrase if sparse/vague/off-topic


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
# below (check_submissions / generate_report_stream / rewrite_summary_stream).

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


_GENERATE_SYSTEM = """你是互联网公司“部门管理周报助手”。你的任务是把多名员工的原始周报，压缩成一份可直接发给管理层的部门周报。

你的目标按优先级排序：
A. 事实正确
B. 管理层一眼读懂
C. 去重压缩
D. 风格统一

你会收到：受众（audience=leader 或 exec）、部门、周次、人员映射表、未提交名单、无效提交名单、事实锁定清单、style_input、原始周报。

硬性约束：
1) 只使用原文与输入元数据中的事实。没有证据就不写；不猜测因果、不补写结论、不发明影响。
2) 以下内容必须逐字保留：数字、百分比、比较关系、单位、日期/时间点、JIRA/缺陷号、版本号、服务/模块名、接口/字段名。
3) 若提供了“事实锁定清单”，清单中的字符串优先按原样出现在输出中；不得改写其写法。
4) 若多份原文之间存在冲突：优先保留共同一致的事实；无法统一的部分不要擅自裁决。若冲突影响管理判断，在“说明”中用一句话指出“原始周报存在信息不一致：……”
5) 人名默认不进入正文；只允许在“说明”中出现未提交、无效提交或必须点名的责任缺口。
6) 同一事项跨多人提及时必须合并。判断是否为同一事项的优先顺序是：项目/系统名 > JIRA/版本号/里程碑 > 同一目标结果 > 同一时间窗口。
7) 同一事项不得在不同章节重复堆砌：
   - 本周KPI与业务进展：写结果、上线、闭环、量化收益
   - 重点项目进展：写仍在推进中的里程碑状态
   - 风险与阻塞项：写当前仍影响进度、质量、联调、发布的阻塞
   - 下周计划：写下周明确动作
8) 若某事项已在 KPI 章节写明“完成并产生结果”，项目章节不要重复同一句；只有仍有里程碑待推进时才继续写项目状态。
9) 每条要点 1–2 句话，先写结果/状态，再写影响/后续。
10) 章节内按重要性排序：线上与业务影响 > 关键项目里程碑 > 风险/延期 > 文档与内部优化。
11) 压缩率：
   - audience=leader：输出约为原文的 45%–55%
   - audience=exec：输出约为原文的 40%–45%
12) style_input 只影响语气、抽象层级、压缩力度；不得改变事实、章节、排序与硬性约束。
13) 禁止空话/套话：总体来说、综上所述、首先/其次/最后、值得注意的是、稳步推进相关工作、为后续奠定基础、赋能、抓手、保驾护航。
14) 优先词汇：完成、上线、修复、推进、联调、对齐、闭环、复盘、灰度、延期、阻塞、定稿、启动、交付、输出。
15) 若某正式章节确无内容，用“ - 无新增关键事项。”填充，不要删除章节。

输出格式（严格）：
#### 说明
（仅当未提交 / 无效提交 / 冲突存在时输出，否则整段删除）

#### 本周KPI与业务进展
- ...

#### 重点项目进展
- ...

#### 风险与阻塞项
- ...

#### 下周计划
- ...
"""


def _build_generate_prompt(
    content: str,
    dept_name: str,
    iso_week: str,
    submitted: list[str] | None = None,
    not_submitted: list[str] | None = None,
    style_profile: dict | None = None,
) -> tuple[str, str]:
    from ..utils.manager_style import render_style_for_prompt

    status_section = ""
    if submitted is not None or not_submitted is not None:
        status_section = (
            f"本周提交情况（来自部门通讯录 + 提交检查）：\n"
            f"- 已提交：{'、'.join(submitted) if submitted else '（无）'}\n"
            f"- 未提交：{'、'.join(not_submitted) if not_submitted else '（无）'}\n\n"
        )

    # Per-manager style profile — empty string when not configured. Slotted
    # into the user prompt (not system) so per-manager content doesn't
    # fragment the system-prompt cache across users.
    style_block = render_style_for_prompt(style_profile)
    style_section = (
        f"## 个人风格档案（仅影响语气/措辞；不可改变事实、章节、排序与硬性约束）\n"
        f"{style_block}\n\n"
        if style_block else ""
    )

    user = (
        f"{status_section}"
        f"{style_section}"
        f"以下是【{dept_name}】{iso_week} 的全体成员周报原文：\n\n"
        f"---\n{content}\n---\n\n"
        f"请按照四章节格式，直接输出面向管理层的部门周报。"
    )
    if not_submitted:
        user += "\n注意：上方「未提交」名单非空，必须在正文前输出「说明」段落列出这些成员。"

    # ── Verbose prompt logging (only when a personal style profile is in use) ──
    # Helps verify the manager's tone_tags / custom_instructions / writing_samples
    # actually land in the prompt that hits the LLM. Gated on style_block so we
    # don't spam logs for managers without a saved profile.
    if style_block:
        logger.info(
            f"[Prompt][Generate] system_prompt: {len(_GENERATE_SYSTEM)} 字符 (constant _GENERATE_SYSTEM)"
        )
        if status_section:
            logger.info(f"[Prompt][Generate] + status_section:\n{status_section.rstrip()}")
        logger.info(f"[Prompt][Generate] + style_section (个人风格档案):\n{style_section.rstrip()}")
        logger.info(
            f"[Prompt][Generate] + content header: 【{dept_name}】{iso_week} "
            f"({len(content)} 字符 raw content omitted)"
        )
        if not_submitted:
            logger.info(
                f"[Prompt][Generate] + tail note: 必须输出说明段落 "
                f"(not_submitted={not_submitted})"
            )
        logger.info(f"[Prompt][Generate] total user prompt: {len(user)} 字符")

    return _GENERATE_SYSTEM, user


_REWRITE_SYSTEM = """你是一名向部门负责人汇报的资深主管文笔助手。
你的任务是按用户给出的额外要求重新调整一份已有的部门周报，做实质性的风格与结构调整，而不是仅做标点和措辞微调。

事实约束：
- 不得删减、虚构、或改写草稿中的任何事实
- 所有数据、版本号、工单号、量化指标、时间点必须原样保留
- 可重新组织顺序、合并表述，但不得删除信息

风格转换（必须执行，不是可选）：
- 每条要点压缩为 1-2 句话，去除过程性细节，突出结果、影响、数字
- 优先级排序：每个章节首条放最关键内容；次条次要；以此类推
- 主动语态，结果先行（"完成 X，效果 Y" 优于 "为了 Y，本周做了 X"）
- 禁止使用：总体来说、综上所述、首先其次最后、在当今、值得注意的是、不仅...而且、为...奠定基础
- 优先使用职场用语：推进、落地、对齐、复盘、跟进、输出、闭环、拉齐
- 保持原有的四章节结构（#### 标题格式不变）

「说明」段落规则：
- 草稿中的「说明」段落必须原样保留，置于所有章节之前
- 不得删除、不得合并到正文章节、不得新增空段落

判断你是否完成了实质性改写：把改写结果与草稿逐句对照，如果改动只是加句号、换同义词、调换"完成"和"已完成"，说明你没有做真正的风格转换——请回到上面的风格规则重新改写。"""


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
    """Single-pass report generation — fact extraction + executive style in one LLM call.

    Replaces the old summarize → rewrite two-pass flow. Yields cumulative text
    snapshots suitable for piping into a Feishu CardKit streaming card.

    submitted / not_submitted: from check_submissions. When provided, the prompt
    instructs the model to prepend a 「说明」 section listing missing members
    and flagging any low-quality submissions.
    """
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
# The reduce prompt differs from _GENERATE_SYSTEM: it explicitly tells the
# model the input is already-extracted structured facts, so it should focus
# on synthesis / dedup / executive-style writing, NOT re-extraction.

_EXTRACT_FACTS_SYSTEM = """你是一名周报事实提取助手。

输入：一名员工本周的原始周报。
输出：把该员工本周的所有事实归入四个类别，输出严格 JSON。

四个类别：
- kpi:        已完成、已上线、已闭环、量化结果（如：完成 X，提升 Y 至 Z%）
- projects:   仍在推进的项目里程碑（未闭环，下周或后续仍有动作）
- risks:      当前阻塞 / 延期 / 质量或上线风险 / 跨组待对齐事项
- next_week:  下周明确动作（不要复述本周完成项）

提取规则：
1. 严格保留：数字、百分比、JIRA/缺陷号、版本号、服务/模块名、日期、时间点
2. 一条 bullet = 一个原子事实，简洁，避免"做了一些工作"这类含糊表述
3. 如果某类别没有内容，返回空数组 []，禁止编造
4. quality_note: 若该周报内容稀少 / 表述含糊 / 偏题，请用不超过 15 字写明；正常请留空字符串 ""

仅输出 JSON，禁止任何额外文字或代码块包裹：
{"kpi": [...], "projects": [...], "risks": [...], "next_week": [...], "quality_note": "..."}"""


def _build_extract_facts_prompt(name: str, raw_report: str) -> tuple[str, str]:
    user = (
        f"员工姓名：{name}\n\n"
        f"原始周报：\n---\n{raw_report}\n---\n\n"
        f"请按规则提取并输出 JSON。"
    )
    return _EXTRACT_FACTS_SYSTEM, user


async def extract_employee_facts(
    name: str,
    raw_report: str,
    llm_provider,
    session_id: str,
) -> EmployeeFacts:
    """[MAP] Extract structured facts from ONE employee's weekly report.

    Small focused call: the LLM sees only one person's report, so attention
    is fully on extracting facts (no cross-employee dedup needed at this
    stage — the reducer handles that).

    Args:
        name:         employee name (used for traceability + reducer dedup)
        raw_report:   the employee's raw weekly report text
        llm_provider: AstrBot provider; same one used by the calling pipeline
        session_id:   provider session tag (e.g. "wrsbot_extract:<open_id>:<name>")

    Returns:
        EmployeeFacts dict. On parse / LLM failure, returns an empty-fields
        EmployeeFacts with quality_note populated so the reducer knows to
        mention the failure rather than silently dropping the person.
    """
    sys_p, usr_p = _build_extract_facts_prompt(name, raw_report)
    empty: EmployeeFacts = {
        "name":         name,
        "kpi":          [],
        "projects":     [],
        "risks":        [],
        "next_week":    [],
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
        # Strip code fences if the model added them despite the instruction
        raw = raw.split("```")[1].lstrip("json").strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"[Extract][{name}] JSON 解析失败: {e} | 原始: {raw[:200]}")
        return {**empty, "quality_note": "返回格式异常"}

    if not isinstance(parsed, dict):
        logger.warning(f"[Extract][{name}] LLM 返回非对象: {type(parsed).__name__}")
        return {**empty, "quality_note": "返回格式异常"}

    def _as_str_list(v) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x) for x in v if x]

    return {
        "name":         name,
        "kpi":          _as_str_list(parsed.get("kpi")),
        "projects":     _as_str_list(parsed.get("projects")),
        "risks":        _as_str_list(parsed.get("risks")),
        "next_week":    _as_str_list(parsed.get("next_week")),
        "quality_note": str(parsed.get("quality_note", "") or ""),
    }


_REDUCE_SYSTEM = """你是互联网公司"部门管理周报助手"。你的任务是把多名员工已提取好的【结构化事实】，合成一份可直接发给管理层的部门周报。

输入说明：
你会收到一份事实清单，已按员工分组，每位员工的事实已按四类预先归类（kpi / projects / risks / next_week）。事实提取阶段已完成，你不需要重新解析原文。

你的目标按优先级排序：
A. 事实正确（事实清单已经标准化，逐字保留即可）
B. 管理层一眼读懂
C. 跨员工去重 + 重要性排序
D. 风格统一

硬性约束：
1) 只使用事实清单中的内容。没有出现的事实不要补写。
2) 数字、百分比、JIRA/缺陷号、版本号、服务/模块名、日期必须逐字保留。
3) 跨员工去重：同一事项（同一项目名 > 同一 JIRA/版本号 > 同一目标结果 > 同一时间窗口）必须合并为一条。
4) 同一事项不在不同章节重复堆砌：
   - KPI 章节：写已完成 + 量化结果
   - 项目章节：写仍在推进的里程碑
   - 风险章节：写当前仍影响进度的阻塞
   - 下周章节：写下周明确动作
5) 若某事项已在 KPI 中写明"完成"，项目章节不要重复；只有仍有后续里程碑时才继续提及。
6) 每条要点 1-2 句话，先写结果/状态，再写影响/后续。
7) 章节内按重要性排序：线上与业务影响 > 关键项目里程碑 > 风险/延期 > 文档与内部优化。
8) 人名默认不进入正文；只允许在「说明」段落中出现。
9) 「说明」段落输出条件（满足任意一条即输出）：
   - 存在 quality_note 非空的员工（列出姓名 + 原因）
   - 用户在 user prompt 中提供了 not_submitted 名单
   否则整段删除。
10) 禁止空话/套话：总体来说、综上所述、首先/其次/最后、值得注意的是、稳步推进、为后续奠定基础、赋能、抓手、保驾护航。
11) 优先词汇：完成、上线、修复、推进、联调、对齐、闭环、复盘、灰度、延期、阻塞、定稿、启动、交付、输出。
12) 若某正式章节确无内容，用" - 无新增关键事项。"填充，不要删除章节。
13) style_profile（若提供）只影响语气、抽象层级、压缩力度；不得改变事实、章节、排序与硬性约束。

输出格式（严格）：
#### 说明
（仅在条件 9 满足时输出，否则整段删除）

#### 本周KPI与业务进展
- ...

#### 重点项目进展
- ...

#### 风险与阻塞项
- ...

#### 下周计划
- ...
"""


def _build_reduce_prompt(
    facts_list: list[EmployeeFacts],
    dept_name: str,
    iso_week: str,
    submitted: list[str] | None = None,
    not_submitted: list[str] | None = None,
    style_profile: dict | None = None,
) -> tuple[str, str]:
    from ..utils.manager_style import render_style_for_prompt

    status_section = ""
    if submitted is not None or not_submitted is not None:
        status_section = (
            f"本周提交情况（来自部门通讯录 + 提交检查）：\n"
            f"- 已提交：{'、'.join(submitted) if submitted else '（无）'}\n"
            f"- 未提交：{'、'.join(not_submitted) if not_submitted else '（无）'}\n\n"
        )

    # Personal style profile injection (same convention as single-call path)
    style_block = render_style_for_prompt(style_profile)
    style_section = (
        f"## 个人风格档案（仅影响语气/措辞；不可改变事实、章节、排序与硬性约束）\n"
        f"{style_block}\n\n"
        if style_block else ""
    )

    # Serialize the facts list as compact JSON. The model reads structured
    # input — no re-extraction needed.
    facts_json = json.dumps(facts_list, ensure_ascii=False, indent=2)

    user = (
        f"{status_section}"
        f"{style_section}"
        f"部门：{dept_name}\n周次：{iso_week}\n\n"
        f"以下是各成员本周事实清单（已结构化）：\n"
        f"```json\n{facts_json}\n```\n\n"
        f"请按照四章节格式合成面向管理层的部门周报。"
    )
    if not_submitted:
        user += "\n注意：上方「未提交」名单非空，必须在正文前输出「说明」段落列出这些成员。"
    if any(f.get("quality_note") for f in facts_list):
        user += "\n注意：部分员工的 quality_note 非空，必须在「说明」段落中列出对应姓名 + 原因。"

    # Verbose logging when a style profile is in use, matching the
    # single-call path. Helps verify the reducer prompt actually carries
    # the manager's voice anchors.
    if style_block:
        logger.info(
            f"[Prompt][Reduce] system_prompt: {len(_REDUCE_SYSTEM)} 字符 (constant _REDUCE_SYSTEM)"
        )
        if status_section:
            logger.info(f"[Prompt][Reduce] + status_section:\n{status_section.rstrip()}")
        logger.info(f"[Prompt][Reduce] + style_section:\n{style_section.rstrip()}")
        logger.info(
            f"[Prompt][Reduce] + facts: {len(facts_list)} 人, "
            f"{len(facts_json)} 字符 JSON"
        )
        logger.info(f"[Prompt][Reduce] total user prompt: {len(user)} 字符")

    return _REDUCE_SYSTEM, user


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
    """[ORCHESTRATOR] Map-reduce report generation (variant 1).

    Workflow:
        1. MAP — fan-out: extract_employee_facts() per employee in parallel,
           bounded by the provided semaphore. Each call is small, focused,
           cache-friendly (same system prompt across all calls).
        2. REDUCE — single streaming call. Takes the list[EmployeeFacts]
           plus dept context + style profile, synthesizes the final report.
           Output streams snapshot-by-snapshot for CardKit display.

    Pre-reduce snapshot: while map is running, yields short status snapshots
    ("正在分析 N 个人的周报...") so the streaming card shows progress instead
    of looking frozen. The reducer's first real snapshot replaces the status.

    Args:
        employee_inputs: list of (employee_name, raw_report_text). Caller is
                         responsible for splitting per-employee (trivial for
                         bitable: one record = one employee; doc path stays
                         on single-call generate_report_stream for now).
        dept_name:       department name for header context
        iso_week:        ISO week tag (e.g. "2026-W21") for header context
        llm_provider:    AstrBot provider, used for both map and reduce calls
        session_id:      session tag prefix; per-employee map calls append :name
        submitted /
        not_submitted:   from check_submissions; passed to reducer for 说明 logic
        style_profile:   personal style dict from manager_style.get_manager_style
        semaphore:       LLM concurrency cap. Pass PipelineBase.llm_semaphore
                         from the calling pipeline. If None, defaults to a
                         fresh semaphore(8) — fine for testing, not production.

    Yields:
        Cumulative text snapshots suitable for the existing CardKit
        streaming-card path (_stream_to_card in pipelines/_base.py).
    """
    if semaphore is None:
        semaphore = asyncio.Semaphore(8)

    n = len(employee_inputs)
    logger.info(f"[MapReduce] start: dept={dept_name} N={n}")

    # ── Pre-reduce progress snapshot ─────────────────────────────────────────
    # The card needs SOMETHING to render before the reducer starts streaming.
    # Yield a placeholder; gets overwritten by the reducer's first real chunk.
    yield f"正在分析 {n} 位成员的本周周报…"

    # ── MAP: parallel per-employee extraction ────────────────────────────────
    async def _bounded_extract(name: str, raw: str) -> EmployeeFacts:
        async with semaphore:
            return await extract_employee_facts(
                name, raw, llm_provider,
                session_id=f"{session_id}:extract:{name}",
            )

    map_tasks = [
        asyncio.create_task(_bounded_extract(name, raw))
        for name, raw in employee_inputs
    ]
    # gather with return_exceptions=False — _bounded_extract already swallows
    # per-employee errors into a quality_note, so no task should raise.
    facts_list: list[EmployeeFacts] = list(await asyncio.gather(*map_tasks))

    # Log map-phase summary so it's easy to see which employees produced thin output
    for f in facts_list:
        logger.info(
            f"[MapReduce] extracted name={f['name']!r} "
            f"kpi={len(f['kpi'])} proj={len(f['projects'])} "
            f"risks={len(f['risks'])} next={len(f['next_week'])} "
            f"quality_note={f['quality_note']!r}"
        )

    yield f"已分析 {n} 人，正在合成部门周报…"

    # ── REDUCE: single streaming synthesis call ──────────────────────────────
    sys_p, usr_p = _build_reduce_prompt(
        facts_list, dept_name, iso_week,
        submitted=submitted, not_submitted=not_submitted,
        style_profile=style_profile,
    )
    stream = llm_provider.text_chat_stream(
        prompt=usr_p, system_prompt=sys_p,
        session_id=f"{session_id}:reduce",
    )
    async for snapshot in _consume_provider_stream(stream):
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
