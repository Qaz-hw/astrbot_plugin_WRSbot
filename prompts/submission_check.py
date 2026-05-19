#=====================================================
#  prompts/submission_check.py — Submission Check Prompt
#=====================================================
#
#  Responsibilities:
#    - Build the LLM prompt for checking who has submitted
#      this week's weekly report based on file content
#    - Return structured JSON: {submitted, not_submitted}
#
#  Does NOT contain:
#    - API calls
#    - LLM invocation
#    - Bitable / Doc reading logic
#=====================================================

def build_submission_check_prompt(
    members: list[dict],
    content: str,
    file_type: str,
) -> str:
    """Build the LLM prompt for submission checking.

    members:   list of {name, open_id, job_title} from org tree
    content:   raw text of the weekly report file (bitable serialized or doc plaintext)
    file_type: "bitable" | "docx" | "doc" — used to give the LLM format context
    """
    member_lines = "\n".join(
        f"- {m['name']}" + (f"（{m['job_title']}）" if m.get("job_title") else "")
        for m in members
    )

    if file_type == "bitable":
        format_hint = "多维表格（每条 [记录 N] 代表一份提交，字段名和值已展开）"
    else:
        format_hint = "飞书文档纯文本（各成员报告可能以姓名、章节或段落区分）"

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
