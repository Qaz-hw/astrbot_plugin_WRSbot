#=====================================================
#  utils/text.py — Text Processing Utilities
#=====================================================
#
#  Responsibilities:
#    - String truncation for display-safe output
#    - Feishu share URL token extraction
#    - Command string normalization
#    - Light text cleaning for LLM input preparation
#
#  All functions are pure — no external dependencies
#  beyond Python's standard library.
#=====================================================

## truncate
# truncate(text: str, max_len: int, suffix: str = "...") -> str
#     # truncate text to max_len characters
#     # append suffix if truncated
#     # safe for Chinese multi-byte characters

## extract_feishu_token
# extract_feishu_token(url: str) -> str
#     # extract the token from a Feishu share URL
#     # handles both folder URLs and doc URLs
#     # strips query params if present
#     # e.g. "https://...feishu.cn/drive/folder/AbCdEf?..." → "AbCdEf"

## normalize_command
# normalize_command(text: str, prefix: str) -> str
#     # remove command prefix from a message string
#     # strip whitespace and normalize internal spaces
#     # e.g. "set_doc_folder  AbCdEf  " → "AbCdEf"

## clean_for_llm
# clean_for_llm(text: str) -> str
#     # light cleaning before feeding text into LLM
#     # collapse excessive newlines
#     # strip zero-width characters and control chars
