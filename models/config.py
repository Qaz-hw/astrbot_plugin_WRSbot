#=====================================================
#  models/config.py — Configuration Data Models
#=====================================================
#
#  Responsibilities:
#    - Define typed data structures for plugin configuration
#      and per-manager persona settings
#    - Pure data only — no API calls, no AstrBot imports
#
#  Storage:
#    ManagerPersona → stored in AstrBot sp, keyed by manager open_id
#                     key pattern: "persona_{manager_open_id}"
#    WRSBotConfig   → stored in AstrBot sp under scope "wrsbot"
#
#  Models:
#    ManagerPersona   — per-manager LLM writing style configuration
#    WRSBotConfig     — global plugin configuration fields
#=====================================================

## Dependencies
# from dataclasses import dataclass, field
# from typing import List

## ManagerPersona
# @dataclass
# class ManagerPersona:
#     manager_open_id: str
#     dept_name: str                    # e.g. "研发部", "产品部"
#     style_keywords: List[str]         # e.g. ["推进", "落地", "对齐", "闭环"]
#     tone_description: str             # free text — "简洁，客观，管理汇报风格"
#     example_sentences: List[str]      # optional past report snippets for LLM reference

## WRSBotConfig
# @dataclass
# class WRSBotConfig:
#     bitable_app_token: str            # Feishu Bitable app token
#     bitable_table_id: str             # target table within the Bitable app
#     report_folder_token: str          # Feishu Drive folder for output docs
#     source_type: str                  # "bitable" | "doc"
