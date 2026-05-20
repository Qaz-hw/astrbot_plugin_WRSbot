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
        [PIPELINE] Persist form_value from the style config card to sp.

        Called from the save_manager_style card action via create_task —
        the sync action handler returns its toast immediately while this
        runs. Tags are filtered against STYLE_TAG_CATALOG inside
        save_manager_style to drop any junk that slipped through the card.

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
        try:
            profile = await save_manager_style(
                open_id,
                tone_tags=tone_tags,
                custom_instructions=custom_instructions,
                writing_samples=writing_samples,
            )
            logger.info(
                f"[StyleConfig] 已保存 open_id={open_id} "
                f"tags={profile['tone_tags']} "
                f"custom_len={len(profile['custom_instructions'])} "
                f"sample_len={len(profile['writing_samples'])}"
            )
        except Exception as e:
            logger.error(f"[StyleConfig] 保存风格失败: {e}")
