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

    Each bullet/line in the original report becomes ONE FactItem. The
    extract stage is responsible for atomizing ("we did A and B" → 2 items)
    and tagging each one with project + category.
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
    """One project's slot in the ReportPlan, post-dedup + ranking."""
    project_id:     str       # normalized id (post any alias merging)
    display_name:   str       # human-readable
    importance:     int       # 1-5; only ≥2 makes it into the plan
    owners:         list[str] # names from contributing facts
    kpi:            list[str] # post-dedup text (verbatim facts merged)
    projects:       list[str]
    risks:          list[str]
    next_week:      list[str]
    merged_aliases: list[str] # other project_ids merged into this one


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

_EXTRACT_FACTS_SYSTEM = """你是一名周报事实结构化助手。你的工作是：把一份原始周报，逐条拆解成"原子事实"，每条事实都打上【项目标签】+【类别标签】。

────────────────────────────
输入
────────────────────────────
- name: 周报作者的姓名（或团队标识）
- raw_report: 该作者本周写的原始周报文字（可能很乱：可能混着流水账、感想、Q&A 等）

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
  "quality_note": ""   // 见下方第 5 条规则
}

────────────────────────────
核心规则
────────────────────────────

1. **原子化（critical）**
   - 一条 "items" 项 = 一个独立可验证的事实。
   - "完成 A，同时推进 B" 必须拆成 2 条。
   - "做了一些优化" 这种聚合表述：作为 1 条保留模糊性，不要补具体内容。
   - 流水账 / 心情 / 总结性套话（"本周整体推进顺利"）一律 **丢弃**，不进入 items。

2. **项目标签（critical）**
   - project_id 规范：纯小写英文/拼音 + 数字 + 下划线，无空格、无标点、无中文。
       好：apollo, beacon_v2, oncall_q1
       差：Apollo, beacon-v2, 阿波罗
   - project_name 是人类可读的显示名（保留原文里的写法风格）。
   - 同一项目的不同写法（"Apollo" / "阿波罗" / "APL-2026" / "阿波罗项目"）
     必须映射到 **同一个 project_id**（如 "apollo"）。
   - 不能归属到具体项目的事实（OKR 培训、修复内部工具 bug、参加 all-hands、
     一次性的杂项支持等），统一用 project_id = "_misc"，project_name = "其他"。
   - **不要为不存在的项目编造名字。** 原文模糊就用 _misc。

3. **类别标签（恰好一个）**
   - kpi: 已经完成的事，最好带量化结果。
       "完成 X 上线，覆盖 30% 用户"  →  kpi
       "修复 PROD-1234 缺陷"          →  kpi
   - projects: 仍在推进、未闭环的里程碑。
       "X 模块已完成开发，下周开始 QA"  →  projects
       "Y 项目正在联调，预计周五出包"   →  projects
   - risks: 当前仍影响进度的阻塞 / 跨组依赖 / 质量风险 / 上线风险。
       "Z 监控未对齐，发布前需要 SRE 介入"   →  risks
       "依赖 A 团队接口未交付，B 项目延期"   →  risks
   - next_week: 明确的下周动作（**只能**来自原文里 "下周/下一步/计划" 等表述）。
       "下周完成 X 的全量发布"           →  next_week
       "继续推进 Y"（来自下周计划段落）  →  next_week

4. **事实保真（HARD）**
   - 数字、百分比、JIRA / 缺陷号、版本号、服务名、模块名、日期、时间点 — **逐字保留**。
   - 不要把原文没出现的影响 / 因果 / 结论补进 text。
   - 不要"合理化"模糊表述。原文写 "做了一些优化" 就保留 "做了一些优化"，不要扩展成 "做了性能优化"。
   - 不要复述章节标题（不要在 text 里写 "本周完成了..."）。

5. **质量标注**
   - quality_note 留空 ""，除非原文严重缺失（< 100 字 + 无具体动作 / 完全偏题 / 与工作无关）。
   - 非空时 ≤15 字，例：「内容仅 1 句」「未提交具体进展」「与工作无关」。

6. **不做的事情**
   - 不要排序（按原文出现顺序输出即可，后续阶段会重排）。
   - 不要去重（同一作者内的小重复也保留，下游 plan 阶段会处理）。
   - 不要分组（plan 阶段按 project_id 分组，你只负责打标签）。
   - 不要润色 text 的措辞。"""


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

_PLAN_SYSTEM = """你是一名"部门周报规划官"。你的任务是把多名员工的【原子事实清单】合并、去重、按项目归类、按重要性排序，产出一份【部门周报计划】（ReportPlan）的严格 JSON。

────────────────────────────
输入
────────────────────────────
- dept_name, iso_week: 部门 + 周次
- employees: list[EmployeeFacts]。每个 EmployeeFacts 包含：
    name, items (list[FactItem], 每项有 category/project_id/project_name/text), quality_note
- not_submitted: 本周未提交周报的成员姓名

────────────────────────────
输出
────────────────────────────
严格 JSON。不要 markdown 代码块。
{
  "cross_cutting_highlights": ["...", "..."],   // 本周最值得放报告顶部的 1-3 条（按重要性降序）
  "projects": [
    {
      "project_id":     "apollo",
      "display_name":   "Apollo",
      "importance":     5,
      "owners":         ["张三", "李四"],
      "kpi":            ["..."],
      "projects":       ["..."],
      "risks":          ["..."],
      "next_week":      ["..."],
      "merged_aliases": []          // 其他被合并进来的 project_id
    },
    ...                              // 按 importance 降序
  ],
  "low_quality_members": [
    {"name": "王五", "reason": "周报仅 1 句话"}
  ]
}

────────────────────────────
核心工作（按优先级）
────────────────────────────

1. **跨员工去重（critical, 40% 注意力预算）**
   合并标准（强 → 弱）：
   a. project_id 相同 + text 几乎相同（包含相同的项目名、JIRA、版本号、目标）
      → 强合并：保留信息更完整的版本，丢弃其他。
   b. project_id 相同 + 同一目标 / 同一里程碑名
      → 中合并：把多个 text 合并为一句更完整的表述（但**只能用原文的词**）。
   c. project_id 相同 + 时间窗口相近 + 描述相关
      → 弱合并：如果信息不冲突就合并；如果冲突，写入 risks 一条「原始周报对 X 表述不一致」。
   d. project_id 不同但其实是同一项目（如 "apollo" vs "apl_2026"）
      → 把出现次数少的合并进出现次数多的 project_id，把被合并的 id 放进 merged_aliases。

2. **按项目分组（critical, 20% 注意力预算）**
   - 所有 FactItem 按 project_id 聚合到一个 ProjectRollup。
   - 同一项目下，按原 FactItem 的 category 分到 kpi / projects / risks / next_week 四个数组。
   - 同一项目下，KPI 里已写"完成"的事项，不再在 projects 里重复出现。

3. **importance 评分（critical, 30% 注意力预算）**
   评分标准：
   - 5 = 战略级 / 线上事件 / 高层关注（核心系统上线、客户级事故、跨部门重大对齐）
   - 4 = 多人协作的主要项目（≥3 人 涉及，或本周有明确里程碑达成）
   - 3 = 单人推进但有实质性产出（一个独立项目的关键节点）
   - 2 = 例行优化 / 文档 / 维护性工作 / 单条 next_week 但无 kpi
   - 1 = 一次性提及、无后续、无具体动作 → **直接 drop，不进入输出**
   - **_misc 项目永远归为 importance=2**（除非里面有线上事故级内容）

4. **cross_cutting_highlights 选择（10% 注意力预算）**
   从最终 projects 列表中，挑 1-3 条本周最值得管理层先看到的事。
   优先级：
   - 线上 / 业务直接影响（"X 完成全量发布，性能 +12%"）
   - 跨多个项目的趋势（"本周 3 个项目集中进入 QA"）
   - 重要风险（"Y 项目因依赖延迟，发布延后到下下周"）
   严禁：单条流水账、客套话、与项目无关的事项。

5. **low_quality_members 收集**
   - 来自 quality_note 非空的员工：照搬 reason。
   - 来自 not_submitted 名单：reason 写 "未提交"。

────────────────────────────
事实保真（HARD）
────────────────────────────
- kpi / projects / risks / next_week 里的每条 text，必须来自输入的 FactItem.text。
  可以**合并 / 缩短 / 重排顺序**，但**不能新增内容、不能添加输入里没有的数字或事实**。
- project_id 不能创造。所有 project_id 必须来自输入 FactItem.project_id（除非通过 merged_aliases 合并）。
- next_week 不能"补建议"。原文没说要做的事，不要写进 next_week。
- 不要在任何 text 里加入员工姓名（owners 字段已经记录，正文不需要）。

────────────────────────────
输出规模
────────────────────────────
- 最终 projects 数组通常 3-8 个（视部门规模）。超过 8 个说明 importance≤2 的项目漏 drop 了。
- 每个 ProjectRollup 的 kpi/projects/risks/next_week 数组各通常 ≤4 条，超过应再合并。
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

    Single LLM call. The model's job is dedup + ranking + cross-cut
    selection — pure structural reasoning over structured input. Facts
    cannot drift because they arrive (and leave) as JSON, not prose.

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
        return [str(x).strip() for x in v if str(x).strip()]

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
            "owners":         _str_list(p.get("owners")),
            "kpi":            _str_list(p.get("kpi")),
            "projects":       _str_list(p.get("projects")),
            "risks":          _str_list(p.get("risks")),
            "next_week":      _str_list(p.get("next_week")),
            "merged_aliases": _str_list(p.get("merged_aliases")),
        })
    # Sort by importance descending (the model is asked to do this; enforce it)
    projects_out.sort(key=lambda x: -x["importance"])

    lqm_out: list[LowQualityMember] = []
    for lqm in parsed.get("low_quality_members", []) or []:
        if not isinstance(lqm, dict):
            continue
        name = str(lqm.get("name", "")).strip()
        if not name:
            continue
        lqm_out.append({
            "name":   name,
            "reason": str(lqm.get("reason", "") or "").strip() or "—",
        })

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

_WRITE_SYSTEM = """你是一名资深主管文笔助手。你的任务是把一份已经规划好的【ReportPlan JSON】渲染成 Markdown 部门周报，发给管理层阅读。

────────────────────────────
输入
────────────────────────────
- plan: ReportPlan JSON（已结构化、已去重、已按重要性排序、已挑好高光）
- style_profile: 该管理者的写作风格档案（actuator-level dials；可能为空）
- dept_name, iso_week, submitted, not_submitted: 上下文

────────────────────────────
事实保真（HARD — 不可违反）
────────────────────────────
- plan 里的每一条 text 必须出现在最终报告中。可以合并 / 缩短 / 顺序调整，**绝不能新增、删除、替换事实**。
- 数字、百分比、JIRA 号、版本号、服务名、日期 100% 逐字保留。
- 不要补"持续推进"、"为后续打基础"、"赋能业务"这类填充语句。
- 如果某个 ProjectRollup 的 risks 或 next_week 数组为空，那一段直接写 " - —"（破折号），不要编造内容。
- 不在正文加入员工姓名（owners 字段已展示在项目标题）。

────────────────────────────
输出结构（严格按此输出）
────────────────────────────

#### 🌟 本周高光
{把 cross_cutting_highlights 每条写成一行，用 ** 加粗最关键的项目名 / 数字 / 动作}

{ 仅当 low_quality_members 非空 OR not_submitted 非空时，输出下面整个「说明」段；否则整段省略 }
#### 📝 说明
- 未提交：{not_submitted 加顿号连接；如为空跳过此行}
- 周报质量待补充：{low_quality_members 加顿号连接，格式 "姓名（reason）"；如为空跳过此行}

#### 📊 项目进展
{按 plan.projects 顺序，每个项目一个 ### 子块；importance≤2 的项目放进最后的「其他事项」}

### {display_name}（负责人：{owners 顿号连接}）
**本周完成**
{把 kpi 数组渲染成 "- xxx" bullets；如为空写 "- —"}

**项目进展**
{把 projects 数组渲染成 bullets；如为空写 "- —"；如该项目所有里程碑已在本周完成且无 in-flight 里程碑，整段省略}

**风险与阻塞**
{risks bullets；如为空写 "- —"}

**下周计划**
{next_week bullets；如为空写 "- —"}

{ 仅当存在 importance==2 的项目时输出 }
#### 🗒️ 其他事项
{把 importance==2 的项目压缩成一行，格式 "**{display_name}**: 一句话总结"}

────────────────────────────
渲染细则
────────────────────────────

1. **bullet 写法**
   - 一条 bullet 1-2 句话。先写结果 / 状态，再写影响 / 后续。
   - 主动语态，结果先行。"完成 X，提升 Y 至 Z%" 优于 "为了 Y，本周完成了 X"。
   - 不要在 bullet 里重复 ### 项目标题。

2. **章节标题里的 emoji**
   - 默认按上方模板（🌟 📝 📊 🗒️）。
   - 如果 style_profile.emoji_density == 0，去掉所有 emoji。
   - 如果 style_profile.emoji_density > 0.5，可以在 bullet 行首适度加 ✅⚠️📌 等小图标。

3. **style_profile 应用（如果 style_profile 提供）**
   - sentence_length: short / medium / long → 控制每条 bullet 的句长。
   - formality 1-5: 1=口语化, 3=正常职场, 5=正式书面。映射示例：
       formality≥4 → 用 "已完成" 而非 "做完了"，用 "推进" 而非 "搞"
       formality≤2 → 可以用更轻松的表述
   - voice: "we" → 句首可用 "我们"；"team" → 用 "团队"；"neutral" → 不出现主语
   - signature_phrases: 列表里的词 / 句式可以自然融入（不要堆砌）。
   - banned_phrases: 列表里的词**绝对不出现**。常见全局禁用："总体来说"、"综上所述"、"首先其次最后"、"值得注意的是"、"为...奠定基础"、"赋能"、"抓手"、"保驾护航"。
   - extras: 原文保留的额外指令，作为软约束遵守。

4. **如果 style_profile 为空**
   - 默认 formality=3, voice=neutral, emoji_density=0.3。
   - 默认 banned_phrases = 上面常见全局禁用列表。

────────────────────────────
自检（输出前必须满足）
────────────────────────────
- 每个 importance≥3 的 ProjectRollup 是否独立成块？✓
- cross_cutting_highlights 是否都出现在「本周高光」段？✓
- 输出中是否有任何事实**不在** plan 里？必须 ✗
- 是否有空段被填充了套话？必须 ✗

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
