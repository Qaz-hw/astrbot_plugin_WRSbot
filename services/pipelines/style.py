#=====================================================
#  services/pipelines/style.py — Manager style profile pipelines
#=====================================================
#
#  Personal style profile = per-manager dict in sp carrying
#  tone_tags + custom_instructions + writing_samples. Read by
#  ReportPipelines.generate / .rewrite to anchor the LLM to the
#  manager's voice without changing facts.
#
#  Registered on card_service via:
#      set_style_pipelines(open_config=open_style_config,
#                          save=save_manager_style)
#=====================================================

from astrbot.api import logger

from ._base import PipelineBase


class StylePipelines(PipelineBase):
    """Open + save pipelines for the personal style config card."""

    async def open_style_config(self, open_id: str) -> None:
        """
        [PIPELINE] Send the style config card pre-filled with the saved profile.

        Workflow:
            1. Load saved profile from sp (or default empties for first-time)
            2. Send the config card via send_style_config_card with each
               saved field as a template_variable

        Note: this path is used as a fallback / when the action handler
        chooses to DM a fresh card. The current open_style_config card
        action patches the existing card in place via a sync sp read
        (see services/lark_card.py); this async path remains available.

        Args:
            open_id: Feishu user ID of the manager

        Triggered by:
            - open_style_config card action (when wired to use the async path)
        """
        from ...utils.manager_style import get_manager_style
        try:
            profile = await get_manager_style(open_id)
            await self.card_service.send_style_config_card(
                open_id,
                tone_tags=profile["tone_tags"],
                custom_instructions=profile["custom_instructions"],
                writing_samples=profile["writing_samples"],
                updated_at=profile["updated_at"],
            )
        except Exception as e:
            logger.error(f"[StyleConfig] 打开风格配置失败: {e}")

    async def save_manager_style(
        self,
        open_id: str,
        tone_tags: list[str],
        custom_instructions: str,
        writing_samples: str,
    ) -> None:
        """
        [PIPELINE] Persist form_value AND fire eager style structuring.

        Workflow:
            1. Resolve the manager's LLM provider (umo=lark:open_id:<id>)
            2. Call save_manager_style which:
               - filters tone_tags against the catalog
               - fires the structuring LLM call (one shot, ~2-3s)
               - persists raw fields + structured_profile to sp
            3. Log a summary so we can verify the structuring actually ran

        Why fire structuring here (not at generate time):
            - One call per save instead of one per report → cheaper
            - render_style_for_prompt reads cached structured dials so the
              write stage gets concrete knobs (sentence_length, formality,
              banned_phrases) instead of vague natural-language tags

        Failure-soft: if no LLM provider is available OR structuring
        errors, the raw fields are still persisted. render_style_for_prompt
        falls back to raw rendering — degraded but not broken.

        Args:
            open_id:             Feishu user ID of the manager
            tone_tags:           selections from STYLE_TAG_CATALOG
            custom_instructions: free-form prompt to append verbatim
            writing_samples:     paragraphs used as LLM voice anchor

        Triggered by:
            - save_manager_style card action (submit button on the
              style config card's form)
        """
        from ...utils.manager_style import save_manager_style

        provider = self.context.get_using_provider(
            umo=f"lark:open_id:{open_id}"
        )
        if provider is None:
            logger.warning(
                f"[StyleConfig] 未找到 LLM provider: open_id={open_id} "
                f"— 跳过结构化，仅保存原始字段"
            )

        try:
            profile = await save_manager_style(
                open_id,
                tone_tags=tone_tags,
                custom_instructions=custom_instructions,
                writing_samples=writing_samples,
                llm_provider=provider,
            )
            structured = profile.get("structured_profile") or {}
            logger.info(
                f"[StyleConfig] 已保存 open_id={open_id} "
                f"tags={profile['tone_tags']} "
                f"custom_len={len(profile['custom_instructions'])} "
                f"sample_len={len(profile['writing_samples'])} "
                f"structured={'是' if structured else '否'}"
                + (
                    f" (len={structured.get('sentence_length')}, "
                    f"formality={structured.get('formality')}, "
                    f"voice={structured.get('voice')})"
                    if structured else ""
                )
            )
        except Exception as e:
            logger.error(f"[StyleConfig] 保存风格失败: {e}")
