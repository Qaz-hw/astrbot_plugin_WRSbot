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
#=====================================================

from datetime import datetime, timezone, timedelta
from astrbot.api import logger, sp


# Predefined tag catalog — must match the option keys configured in the
# Feishu card builder so form_value round-trips cleanly. Tailored to
# weekly report summarization, NOT generic chat tone.
#
# TODO [Feishu builder]: when authoring the style config card, set the
# multi-select "tone_tags_input" options to EXACTLY these strings (the
# Chinese label is also used as the value/key for simplicity). Any tag
# the card sends that isn't in this list is silently dropped by
# save_manager_style — that's intentional, but it means a typo in the
# builder = the tag never persists.
#
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


def _empty_profile() -> dict:
    return {
        "tone_tags":           [],
        "custom_instructions": "",
        "writing_samples":     "",
        "updated_at":          "",
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

    if not isinstance(raw, dict):
        return _empty_profile()

    return {
        "tone_tags":           list(raw.get("tone_tags", [])),
        "custom_instructions": str(raw.get("custom_instructions", "")),
        "writing_samples":     str(raw.get("writing_samples", "")),
        "updated_at":          str(raw.get("updated_at", "")),
    }


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

    if not isinstance(raw, dict):
        return _empty_profile()

    return {
        "tone_tags":           list(raw.get("tone_tags", [])),
        "custom_instructions": str(raw.get("custom_instructions", "")),
        "writing_samples":     str(raw.get("writing_samples", "")),
        "updated_at":          str(raw.get("updated_at", "")),
    }


async def save_manager_style(
    open_id: str,
    *,
    tone_tags: list[str],
    custom_instructions: str,
    writing_samples: str,
) -> dict:
    """Persist the profile. Returns the cleaned dict for confirmation display."""
    # Never trust raw form_value — filter tags to the known catalog so a
    # stale card option doesn't silently inject a junk tag into the prompt.
    cleaned_tags = [t for t in (tone_tags or []) if t in STYLE_TAG_CATALOG]
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

    profile = {
        "tone_tags":           cleaned_tags,
        "custom_instructions": (custom_instructions or "").strip(),
        "writing_samples":     (writing_samples or "").strip(),
        "updated_at":          now,
    }
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
    """Convert a profile dict into a prompt block, or '' if the profile is empty.

    Empty-string return lets callers skip injecting an empty section into
    the prompt entirely (cleaner than a header with nothing under it).
    """
    if not profile:
        return ""

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
