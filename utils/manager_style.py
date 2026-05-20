#=====================================================
#  utils/manager_style.py — Per-manager style profile
#=====================================================
#
#  Stores each manager's report-generation style preferences in AstrBot's
#  shared preferences (sp), keyed by open_id. Used by the report pipelines
#  to bend the LLM toward the manager's voice without changing facts.
#
#  Schema (single dict, persisted as one sp value):
#    tone_tags:           list[str]  — selections from STYLE_TAG_CATALOG
#    custom_instructions: str        — free-form prompt added verbatim
#    writing_samples:     str        — paragraphs the manager wrote, used
#                                      as voice anchors (not as facts)
#    updated_at:          str        — display timestamp, UTC+8
#
#  sp coordinates (matches the existing draft-storage convention):
#    scope="global", scope_id="wrsbot", key=f"manager_style:{open_id}"
#
#  SCALE TARGET (current): 30–50 employees per dept, small number of managers.
#    Raw style fields are loaded per-report and rendered into the prompt as
#    natural-language. Works fine at this scale.
#
#  TODO [scale > 100 employees and/or many managers]: structure once at save
#    time, not per report. Add a sibling field:
#        structured_profile: dict {
#            sentence_length:           "short" | "medium" | "long",
#            voice:                     "we" | "team" | "neutral",
#            formality:                 1-5,
#            emoji_density:             0.0-1.0,
#            banned_phrases:            list[str],
#            signature_phrases:         list[str],  # inferred from samples
#            preferred_transitions:     list[str],  # inferred from samples
#        }
#    Compute this on /save_manager_style by firing ONE LLM call that reads
#    (tone_tags + custom_instructions + writing_samples) and emits the
#    actuator-level dials above. Cache. Per-report calls then read the
#    structured profile, not the raw fields — gives the writer concrete
#    knobs the model can actually apply, and eliminates the per-report
#    cost of style structuring (saves ~1s + tokens per generate).
#=====================================================

import json
from datetime import datetime, timezone, timedelta
from astrbot.api import logger, sp


# Predefined tag catalog — must match the option keys configured in the
# Feishu card builder so form_value round-trips cleanly. Tailored to
# weekly report summarization, NOT generic chat tone.



# TODO: tune this list once a few managers test the feature.
STYLE_TAG_CATALOG = [
    "务实克制",      # objective, no fluff
    "结果先行",      # outcome-first phrasing
    "数据驱动",      # quantify wherever possible
    "温暖鼓舞",      # warm, morale-positive
    "热情积极",      # enthusiastic
    "严谨细致",      # detailed, precise
    "简明扼要",      # terse, short sentences
    "正式汇报体",    # formal exec-style register
    "团队凝聚",      # emphasizes collective effort
]

_SP_SCOPE    = "global"
_SP_SCOPE_ID = "wrsbot"
_SP_KEY_FMT  = "manager_style:{open_id}"

# Always-banned phrases — appended to the per-manager banned list. These are
# global "AI-prose smells" that homogenize output across managers, regardless
# of any individual's style.
_GLOBAL_BANNED_PHRASES = [
    "总体来说", "综上所述", "首先其次最后", "首先、其次", "值得注意的是",
    "为...奠定基础", "为后续奠定基础", "稳步推进", "赋能", "抓手",
    "保驾护航", "持续推进相关工作",
]


def _empty_profile() -> dict:
    return {
        "tone_tags":           [],
        "custom_instructions": "",
        "writing_samples":     "",
        "updated_at":          "",
        "structured_profile":  None,
    }


def _empty_structured() -> dict:
    """Default actuator-level dials used when structuring fails or is skipped."""
    return {
        "sentence_length":       "medium",
        "formality":             3,
        "voice":                 "neutral",
        "emoji_density":         0.3,
        "preferred_transitions": [],
        "banned_phrases":        list(_GLOBAL_BANNED_PHRASES),
        "signature_phrases":     [],
        "tone_summary":          "",
        "extras":                "",
    }


# ── Style structuring (eager, runs at save time) ─────────────────────────────
# Converts raw manager inputs (tone_tags + custom_instructions + writing_samples)
# into actuator-level dials the write stage can directly apply. Runs ONCE per
# /save_manager_style — the per-report path reads the cached structured dict
# instead of re-paying this LLM call every generate.

_STYLE_STRUCTURE_SYSTEM = """你是一名"管理者写作风格分析助手"。你的任务是把一位部门管理者提供的【风格偏好 + 写作样本】，转化成一份【actuator-level dials】的结构化档案，让自动化写作系统能精确控制语气。

────────────────────────────
输入
────────────────────────────
- tone_tags:           管理者从预设标签中勾选的风格标签（可能为空）
- custom_instructions: 管理者写的自由文本要求（可能为空）
- writing_samples:     管理者本人写过的段落（最重要的 ground truth；可能为空）

────────────────────────────
输出（严格 JSON，不要 markdown 代码块）
────────────────────────────
{
  "sentence_length":       "short" | "medium" | "long",
  "formality":             1 到 5 的整数,
  "voice":                 "we" | "team" | "neutral",
  "emoji_density":         0.0 到 1.0 的小数,
  "preferred_transitions": [str],
  "banned_phrases":        [str],
  "signature_phrases":     [str],
  "tone_summary":          "<不超过30字的一句话总结>",
  "extras":                "<custom_instructions 中无法结构化的剩余内容，原文保留>"
}

────────────────────────────
字段定义
────────────────────────────

sentence_length（句长偏好）：
- short:  多数句子 ≤20 字；写作 punchy、节奏快
- medium: 20-40 字；常规职场表达
- long:   ≥40 字；信息密度高、有从句结构

formality（正式度）：
- 1: 非常口语化（像和朋友说话）
- 2: 轻松职场（带玩笑、emoji 偶尔出现）
- 3: 正常职场（中立、不刻意正式）
- 4: 偏向汇报体（用"已完成"、"推进"、"对齐"等词）
- 5: 严肃书面体（向高层 / 客户）

voice（主语风格）：
- "we":      句首会用"我们"、"咱们"
- "team":    用"团队"、"部门"作主语
- "neutral": 不出现主语，用动作开头

emoji_density（emoji 使用密度）：
- 0.0: 完全不用 emoji
- 0.3: 每 3-4 个 bullet 出现一次
- 0.5: 几乎每段一个
- 1.0: 每条 bullet 行首都有

preferred_transitions（偏好过渡词）：
- 从 writing_samples 中**实际出现过**的过渡词提取。
- 样本不足以提取就返回空数组 []，不要编造。
- 例：["另外", "同时", "考虑到", "鉴于"]

banned_phrases（禁用词汇）：
- 必须包含以下全局禁用（无论如何）：总体来说、综上所述、首先其次最后、值得注意的是、为...奠定基础、稳步推进、赋能、抓手、保驾护航、持续推进相关工作
- 再加上从 tone_tags 推导的禁用（例：tag="务实克制" → 禁用 "热情"、"令人激动"）
- 再加上 custom_instructions 中明示的禁用

signature_phrases（特征词汇 / 句式）：
- 只能来自 writing_samples 中**反复出现**（≥2 次）的词或句式。
- 不要超过 10 个。
- 样本为空就返回 []，不要编造。
- 例：["拉齐", "闭环", "兜底"]

tone_summary（一句话总结）：
- ≤30 字，写作阶段可读，帮助 LLM 一眼把握风格。
- 例："偏向克制、数据驱动，避免感情色彩，每条 bullet 短而有结果"

extras（剩余原文指令）：
- custom_instructions 中无法映射到上述结构化字段的内容，原文保留。
- 例：管理者写了"每段开头加 emoji；段末写一句下季展望" → emoji_density 已结构化，但"段末加下季展望"放进 extras。

────────────────────────────
推断原则
────────────────────────────
1. **writing_samples 是 ground truth** — 如果它与 tone_tags 矛盾（例：tag 说"热情积极"但样本句句克制），以 samples 为准。
2. samples 为空时，从 tags 给合理默认值；不要为不存在的样本编造特征词。
3. custom_instructions 优先级高于 tags（管理者明确写的指令最重要）。
4. **不要输出"我推断..."这类元评论**，只输出结构化 JSON。"""


async def structure_style_profile(
    tone_tags: list[str],
    custom_instructions: str,
    writing_samples: str,
    llm_provider,
    session_id: str,
) -> dict:
    """Run the structuring LLM call. Returns actuator-level dials dict.

    Fail-soft: any error returns _empty_structured() so save_manager_style
    can still persist the raw fields. The next generate will render from
    raw fields (via render_style_for_prompt fallback path) — degraded but
    not broken.
    """
    inputs = {
        "tone_tags":           tone_tags or [],
        "custom_instructions": (custom_instructions or "").strip(),
        "writing_samples":     (writing_samples or "").strip(),
    }
    user = (
        f"以下是输入数据：\n"
        f"```json\n{json.dumps(inputs, ensure_ascii=False, indent=2)}\n```\n\n"
        f"请按系统提示输出结构化 JSON。"
    )

    try:
        resp = await llm_provider.text_chat(
            prompt=user, system_prompt=_STYLE_STRUCTURE_SYSTEM, session_id=session_id,
        )
    except Exception as e:
        logger.warning(f"[StyleStructure] LLM 调用失败: {e}")
        return _empty_structured()

    raw = (resp.completion_text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].lstrip("json").strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"[StyleStructure] JSON 解析失败: {e} | 原始: {raw[:300]}")
        return _empty_structured()
    if not isinstance(parsed, dict):
        return _empty_structured()

    def _str_list(v) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()]

    def _clamp_int(v, lo: int, hi: int, default: int) -> int:
        try:
            n = int(v)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    def _clamp_float(v, lo: float, hi: float, default: float) -> float:
        try:
            n = float(v)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    sl = str(parsed.get("sentence_length", "medium")).strip().lower()
    if sl not in ("short", "medium", "long"):
        sl = "medium"
    voice = str(parsed.get("voice", "neutral")).strip().lower()
    if voice not in ("we", "team", "neutral"):
        voice = "neutral"

    # Always merge global banned phrases — the model is instructed to include
    # them, but enforce as a hard contract in case it slips.
    banned = _str_list(parsed.get("banned_phrases"))
    for p in _GLOBAL_BANNED_PHRASES:
        if p not in banned:
            banned.append(p)

    return {
        "sentence_length":       sl,
        "formality":             _clamp_int(parsed.get("formality"), 1, 5, 3),
        "voice":                 voice,
        "emoji_density":         _clamp_float(parsed.get("emoji_density"), 0.0, 1.0, 0.3),
        "preferred_transitions": _str_list(parsed.get("preferred_transitions")),
        "banned_phrases":        banned,
        "signature_phrases":     _str_list(parsed.get("signature_phrases"))[:10],
        "tone_summary":          str(parsed.get("tone_summary", "") or "").strip(),
        "extras":                str(parsed.get("extras", "") or "").strip(),
    }


def _coerce_profile(raw) -> dict:
    """Normalize an sp-loaded dict (or None / wrong type) into the profile shape.

    Includes the structured_profile field if present. Used by both sync and
    async readers so the shape is identical regardless of access path.
    """
    if not isinstance(raw, dict):
        return _empty_profile()
    structured = raw.get("structured_profile")
    if not isinstance(structured, dict):
        structured = None
    return {
        "tone_tags":           list(raw.get("tone_tags", [])),
        "custom_instructions": str(raw.get("custom_instructions", "")),
        "writing_samples":     str(raw.get("writing_samples", "")),
        "updated_at":          str(raw.get("updated_at", "")),
        "structured_profile":  structured,
    }


def get_manager_style_sync(open_id: str) -> dict:
    """Sync variant for card action handlers (handle_card_action_sync).

    The sync card dispatcher can't await — needed so open_style_config can
    return the pre-filled config card immediately (patches in place via the
    P2CardActionTriggerResponse) instead of through an async pipeline that
    DMs a new card. Uses sp.get (sync) under the hood.
    """
    try:
        raw = sp.get(
            _SP_KEY_FMT.format(open_id=open_id),
            None,
            scope=_SP_SCOPE,
            scope_id=_SP_SCOPE_ID,
        )
    except Exception as e:
        logger.warning(f"[ManagerStyle] sp 同步读取失败: {e}")
        return _empty_profile()
    return _coerce_profile(raw)


async def get_manager_style(open_id: str) -> dict:
    """Return the saved profile, or an empty default if none exists."""
    try:
        raw = await sp.get_async(
            scope=_SP_SCOPE,
            scope_id=_SP_SCOPE_ID,
            key=_SP_KEY_FMT.format(open_id=open_id),
            default=None,
        )
    except Exception as e:
        logger.warning(f"[ManagerStyle] sp 读取失败: {e}")
        return _empty_profile()
    return _coerce_profile(raw)


async def save_manager_style(
    open_id: str,
    *,
    tone_tags: list[str],
    custom_instructions: str,
    writing_samples: str,
    llm_provider=None,
) -> dict:
    """Persist the profile. Returns the cleaned dict for confirmation display.

    When llm_provider is provided, also fires the structuring stage:
    converts (tone_tags + custom_instructions + writing_samples) into
    actuator-level dials and caches the result under structured_profile.
    The write stage of report generation will read the cached structured
    dict instead of re-paying this LLM call every report.

    Failure-soft: if structuring fails, structured_profile stays None and
    render_style_for_prompt falls back to the raw-fields path. Save itself
    still succeeds.
    """
    # Never trust raw form_value — filter tags to the known catalog so a
    # stale card option doesn't silently inject a junk tag into the prompt.
    cleaned_tags = [t for t in (tone_tags or []) if t in STYLE_TAG_CATALOG]
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

    profile: dict = {
        "tone_tags":           cleaned_tags,
        "custom_instructions": (custom_instructions or "").strip(),
        "writing_samples":     (writing_samples or "").strip(),
        "updated_at":          now,
        "structured_profile":  None,
    }

    # Eager structuring (one LLM call here saves one per report).
    if llm_provider is not None:
        try:
            structured = await structure_style_profile(
                cleaned_tags,
                profile["custom_instructions"],
                profile["writing_samples"],
                llm_provider,
                session_id=f"wrsbot_style_struct:{open_id}",
            )
            profile["structured_profile"] = structured
            logger.info(
                f"[StyleStructure] open_id={open_id} → "
                f"len={profile['structured_profile'].get('sentence_length')}, "
                f"formality={profile['structured_profile'].get('formality')}, "
                f"voice={profile['structured_profile'].get('voice')}, "
                f"signatures={len(profile['structured_profile'].get('signature_phrases', []))}, "
                f"banned={len(profile['structured_profile'].get('banned_phrases', []))}"
            )
        except Exception as e:
            logger.warning(f"[StyleStructure] 跳过结构化（保留原始字段）: {e}")

    try:
        await sp.put_async(
            scope=_SP_SCOPE,
            scope_id=_SP_SCOPE_ID,
            key=_SP_KEY_FMT.format(open_id=open_id),
            value=profile,
        )
    except Exception as e:
        logger.error(f"[ManagerStyle] sp 写入失败: {e}")
    return profile


async def clear_manager_style(open_id: str) -> bool:
    """Remove the saved profile. Returns True if a profile was present."""
    key = _SP_KEY_FMT.format(open_id=open_id)
    try:
        existing = await sp.get_async(
            scope=_SP_SCOPE, scope_id=_SP_SCOPE_ID, key=key, default=None,
        )
        if existing:
            # sp has no documented delete API — overwrite with None acts as
            # a tombstone, and get_manager_style treats non-dict as empty.
            await sp.put_async(
                scope=_SP_SCOPE, scope_id=_SP_SCOPE_ID, key=key, value=None,
            )
            return True
    except Exception as e:
        logger.warning(f"[ManagerStyle] sp 清除失败: {e}")
    return False


def render_style_for_prompt(profile: dict | None) -> str:
    """Render a profile dict as a prompt block, or '' if empty.

    Preferred path: read `structured_profile` (actuator-level dials cached
    at save time) — gives the write stage concrete knobs (sentence_length,
    formality, banned_phrases, etc.) the model can directly apply.

    Fallback path: render the raw fields (tone_tags / custom_instructions /
    writing_samples) as natural-language. Used when structured_profile is
    missing (older save predates structuring, or structuring LLM failed).

    Empty-string return lets callers skip injecting an empty section into
    the prompt entirely (cleaner than a header with nothing under it).
    """
    if not profile:
        return ""

    structured = profile.get("structured_profile")
    if isinstance(structured, dict) and structured:
        return _render_structured(structured)
    return _render_raw(profile)


def _render_structured(s: dict) -> str:
    """Render the actuator-level dials as a prompt block for the write stage."""
    parts: list[str] = []

    if s.get("tone_summary"):
        parts.append(f"**语气总结：** {s['tone_summary']}")

    parts.append(
        "**风格参数：**\n"
        f"- 句长偏好：{s.get('sentence_length', 'medium')}\n"
        f"- 正式度：{s.get('formality', 3)}/5\n"
        f"- 主语风格：{s.get('voice', 'neutral')}\n"
        f"- emoji 密度：{s.get('emoji_density', 0.3)}（0=无 / 1=每条）"
    )

    if s.get("signature_phrases"):
        parts.append(
            "**特征词汇 / 句式（可自然融入，不要堆砌）：**\n"
            + "、".join(s["signature_phrases"])
        )

    if s.get("preferred_transitions"):
        parts.append(
            "**偏好过渡词：** " + "、".join(s["preferred_transitions"])
        )

    if s.get("banned_phrases"):
        parts.append(
            "**禁用词汇（绝对不可出现）：**\n"
            + "、".join(s["banned_phrases"])
        )

    if s.get("extras"):
        parts.append(f"**其他原文指令（按要求执行）：**\n{s['extras']}")

    return "\n\n".join(parts)


def _render_raw(profile: dict) -> str:
    """Fallback: render the raw (unstructured) fields as natural-language.

    Used when no structured_profile is cached. Less precise than the
    structured path because the write stage has to interpret tags
    instead of reading actuator dials, but it's better than no style at all.
    """
    parts: list[str] = []
    if profile.get("tone_tags"):
        parts.append("语气标签：" + "、".join(profile["tone_tags"]))
    if profile.get("custom_instructions"):
        parts.append("自定义指令：\n" + profile["custom_instructions"])
    if profile.get("writing_samples"):
        # Fence the sample so the LLM treats it as VOICE anchor only, not
        # as facts to merge into the report — critical to prevent old data
        # from a sample paragraph leaking into the current week's output.
        parts.append(
            "管理者本人撰写样本（仅参考语气与措辞，不得复用其中事实）：\n"
            f"---SAMPLE BEGIN---\n{profile['writing_samples']}\n---SAMPLE END---"
        )
    return "\n\n".join(parts)
