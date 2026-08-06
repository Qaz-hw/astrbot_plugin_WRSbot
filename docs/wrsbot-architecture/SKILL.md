---
name: wrsbot-architecture
description: Implement, debug, or extend the WRSbot AstrBot/Feishu weekly-report plugin. Use when changing its report-generation pipeline, Feishu contact or card flows, roles, folder bindings, LLM prompts, or AstrBot integration behavior.
---

# WRSbot architecture

Read [references/architecture.md](references/architecture.md) before changing behavior.

## Working rules

1. Treat AstrBot as the owner of Lark credentials and the Lark API client. Do not add duplicate App ID or App Secret values to this plugin.
2. Preserve the service boundaries: `main.py` routes events/commands, `services/` owns Feishu API operations, and `services/pipelines/` orchestrates workflows.
3. For a reply triggered by a Lark message, use the Lark client from that event and reply in its message/chat context. Do not assume a cached global client or an `open_id` is valid across Lark app instances.
4. Keep manager authorization cache-based and synchronous for card callbacks. The cache is rebuilt from the organization tree; do not perform slow contact lookups in synchronous card-action handlers.
5. Preserve the report pipeline's fact integrity: extraction structures facts, planning groups/ranks/deduplicates, writing renders without inventing facts.
6. Before changing performance behavior, identify the LLM call count and stage ordering. The map-reduce pipeline makes one extract call per report, then plan and write calls.
7. Validate Python edits with `python3 -m py_compile` for each modified module. Restart AstrBot to apply plugin/config changes and observe the startup contact-cache log.

## Route by task

| Task | Read first |
| --- | --- |
| Role, department, contact cache, employee lookup | `references/architecture.md` → Contact and roles |
| Welcome cards, CardKit buttons, reply/send failures | `references/architecture.md` → Lark transport and cards |
| Summary generation, latency, prompts, scaling | `references/architecture.md` → Summary-generation pipeline |
| Folder/file discovery, Bitable/Doc report input/output | `references/architecture.md` → Data and folder flow |
| Startup or configuration failures | `references/architecture.md` → Configuration and diagnostics |

## Do not assume

- A successful Feishu HTTP request means the app can see the whole organization. Inspect the contact-cache counts and the organization API result.
- Lark `open_id` values can be used by any Lark application. They are app-scoped for direct sends.
- An optional Lark lifecycle/read event is required for message handling. AstrBot only needs `im.message.receive_v1` for inbound messages; unsupported optional events only create SDK noise.
- A report-generation action is a single model request. It is a multi-stage workflow.
