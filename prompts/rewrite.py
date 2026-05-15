#=====================================================
#  prompts/rewrite.py — Style Rewrite Prompt
#=====================================================
#
#  Responsibilities:
#    - Define the LLM system prompt for rewriting a structured
#      draft into the manager's personal reporting style
#    - Inject ManagerPersona fields into the prompt at runtime
#    - Enforce anti-AI writing rules
#
#  Anti-AI rules enforced:
#    - No filler phrases ("总体来说", "首先其次", "在当今快节奏")
#    - No motivational or exaggerated positivity
#    - No academic essay structure
#    - Preferred vocabulary: 推进, 落地, 对齐, 复盘, 跟进, 输出, 闭环
#
#  Does NOT contain:
#    - Summarization logic (lives in prompts/summarize.py)
#    - API calls
#    - Persona storage (lives in models/config.py + AstrBot sp)
#=====================================================

## SYSTEM_PROMPT
# SYSTEM_PROMPT: str
#     # instructs LLM to act as a professional report editor
#     # defines tone: concise, objective, managerial, workplace-oriented
#     # includes full anti-AI writing rules
#     # instructs LLM to preserve all factual content from the draft

## build_prompt
# build_prompt(draft: str, persona: ManagerPersona) -> str
#     # inject persona.dept_name into context
#     # inject persona.style_keywords as preferred vocabulary list
#     # inject persona.tone_description as style guidance
#     # optionally append persona.example_sentences as writing reference
#     # append the draft to rewrite
