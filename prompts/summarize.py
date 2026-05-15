#=====================================================
#  prompts/summarize.py — Summarization Prompt
#=====================================================
#
#  Responsibilities:
#    - Define the LLM system prompt for extracting and
#      structuring individual weekly reports into a
#      consolidated department summary draft
#    - Build the user-turn prompt from raw submission data
#
#  Output structure expected from LLM:
#    1. 本周KPI与业务进展
#    2. 重点项目进展
#    3. 风险与阻塞项
#    4. 下周计划
#
#  Does NOT contain:
#    - Style rewriting logic (lives in prompts/rewrite.py)
#    - Persona injection (lives in prompts/rewrite.py)
#    - API calls
#=====================================================

## SYSTEM_PROMPT
# SYSTEM_PROMPT: str
#     # instructs LLM to act as a department report summarizer
#     # defines the 4-section output structure
#     # includes anti-hallucination rules (only use provided content)
#     # includes anti-AI writing rules (no filler phrases)

## build_prompt
# build_prompt(submissions: List[WeeklySubmission]) -> str
#     # format all submissions into a single user-turn prompt
#     # structure: one block per submitter (name + raw content)
#     # instruct LLM to consolidate into the 4-section structure
