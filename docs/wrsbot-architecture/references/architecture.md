# WRSbot implementation reference

## Purpose and ownership

WRSbot is an AstrBot plugin for Feishu/Lark department-weekly-report workflows. It provides employee/manager entry cards, department folder binding, organization-contact caching, weekly-source discovery, and generated/re-written/submitted department summaries.

AstrBot owns platform configuration, the Lark `app_id`/`app_secret`, message receiving, provider configuration, and the initialized Lark API client. WRSbot reuses that client; do not create a second credential configuration in plugin `.env`.

## File map

| Area | Primary files | Responsibility |
| --- | --- | --- |
| Plugin entry/routing | `main.py` | Commands, all-message triggers, service/pipeline wiring |
| Contact directory | `services/contact.py` | Department/member cache, manager set, contact wrappers |
| Cards | `services/lark_card.py` | Card templates, CardKit callback router, direct card sending |
| File discovery | `services/drive.py`, `services/doc.py`, `services/bitable.py` | Drive listing, Doc reads/writes, Bitable rows/fields |
| Report input/output helpers | `services/report.py` | Submission check, extract/plan/write LLM functions |
| Workflow orchestration | `services/pipelines/report.py` | Generate, rewrite, submit pipelines |
| Common pipeline helpers | `services/pipelines/_base.py` | Shared Lark/provider helpers and global LLM semaphore |
| User/manager views | `services/pipelines/views.py` | Admin/employee views and reminders |
| Configuration | `.env`, `utils/env_config.py` | Department folder tokens and URL generation |

## Initialization

1. AstrBot creates configured platform adapters, including Lark.
2. WRSbot `initialize()` finds the Lark adapter's `lark_api` and builds the card, contact, file, and pipeline services.
3. `ContactService.start_cache()` reads the organization tree and starts a five-hour refresh loop.
4. `LarkCardService.inject_into_dispatcher()` registers `p2.card.action.trigger` with the Lark SDK dispatcher.

Startup should log `部门缓存已更新：N 个部门，M 名成员，K 名负责人`.

## Contact and roles

### Cache shape

Each cache entry is:

```python
{"dept": department, "manager": user_or_none, "members": [user, ...]}
```

`ContactService._do_refresh()` builds `_leader_ids` from `entry["manager"].open_id`. `is_manager(open_id)` is synchronous and is safe for card callbacks. A local `WRSBOT_TEST_ADMIN_OPEN_IDS` override may exist in ignored plugin `.env` for temporary testing; remove it before production.

### Organization discovery

`list_root_departments()` calls Feishu with `parent_department_id="0"`, `fetch_child=True`, and pagination. `get_org_tree()` lists direct members for every returned department, then also queries members directly under root department `0`. Root-only users are represented by a synthetic `根部门` entry.

If the cache has low counts after this traversal, Feishu is returning only partial directory data to the application. A `200 OK` does not prove that the entire organization is visible.

### Role behavior

- Managers: can access manager cards and generate reports once folder bindings are complete.
- Employees: receive employee cards/views.
- Natural-language `generate_summary`: silently returns for non-managers.
- Natural-language `get_writing_folder`: can serve an employee's department folder.
- Card callbacks must use cached role data; they are synchronous.

## Lark transport and cards

### Inbound path

Lark socket mode delivers `im.message.receive_v1` to AstrBot's Lark adapter. It is normalized into `LarkMessageEvent`, then AstrBot's event bus runs commands, plugin handlers, and normal LLM processing. A log containing `[MRSbot(lark)] ...: <message>` proves inbound delivery.

Optional events such as `im.chat.access_event.bot_p2p_chat_entered_v1` and `im.message.message_read_v1` are not handled by the adapter. Remove them from Event Subscriptions if they create `processor not found` errors.

### Sending rule

`open_id` is app-scoped. A direct send with `receive_id_type="open_id"` can fail with `99992361: open_id cross app` if it uses a client from a different Lark app/adapter.

For a response triggered by an inbound message:

1. use `event.bot`, not merely a plugin-cached `self.lark_api`;
2. reply with the inbound `message_id` where feasible.

`230002: Bot/User can NOT be out of the chat` is another signal that the sending client is not a participant in the source chat.

### Card flow

`LarkCardService.handle_card_action_sync()` routes `p2.card.action.trigger` by `action_key`.

- `wrsbot_command_list`: role-specific command list.
- `wrsbot_start`: manager requires bound folders, then schedules admin pipeline; employee schedules user pipeline.
- Other actions handle binding, style, report, reminder, and document views.

Async pipeline work is scheduled with `asyncio.create_task()`; the callback returns a card/toast immediately.

## Data and folder flow

Department bindings use ignored plugin `.env` keys:

```text
DEPT_FOLDER_<open_department_id_with_hyphens_replaced>=<folder_token>
```

`utils/env_config.py` owns parsing, persistence, and URL generation. Do not write this format elsewhere.

Weekly source discovery starts from a manager's department and bound Drive folder:

- Bitable records become `(employee_name, raw_report)` inputs.
- Doc content currently becomes one `("团队", full_doc_text)` input.

## Summary-generation pipeline

`ReportPipelines.generate()` runs:

```text
manager + department + folder
→ find weekly Bitable/Doc source
→ read raw source
→ check submissions
→ extract per employee/source (parallel)
→ plan department report (one LLM call)
→ write final Markdown (streaming LLM call)
→ persist draft
→ patch/send action card
```

`services/report.py` owns the stages:

- `check_submissions()`: LLM determination of submitted/not-submitted.
- `extract_employee_facts()`: one raw report → structured facts.
- `plan_report()`: deduplicate, group by project, rank, return `ReportPlan` JSON.
- `write_report_stream()`: render the plan without inventing/removing/replacing plan facts.

### Performance and scale

For `N` accepted Bitable reports, one generation is approximately:

```text
1 submission-check call + N extract calls + 1 plan call + 1 write call
```

Extract calls run under the process-wide `_LLM_SEMAPHORE = asyncio.Semaphore(8)`, in waves of eight. Plan and write are serial. Source data is currently read once for generation and again inside submission checking.

The design targets 30–50 employees per department. At 100+ employees, add a hierarchy:

```text
employee extract → team rollup → department plan → final write
```

This prevents a single plan prompt from receiving an oversized all-employee facts payload.

## Natural language and commands

`main.py::natural_report_trigger` handles report-like messages through a keyword prefilter and LLM intent classifier:

- `generate_summary`: manager only.
- `get_writing_folder`: eligible employee.
- `none`/low confidence: returns without stopping normal AstrBot flow.

Slash commands use AstrBot command handling. Group commands use a dedicated dispatcher because group mentions can otherwise reach the ordinary LLM path.

## Configuration and diagnostics

### Lark

Keep Lark credentials in AstrBot `data/cmd_config.json`, not plugin `.env`. DM receiving requires published `im.message.receive_v1` and matching application-identity message permissions.

### Proxy

The installed `httpx` client rejects `socks://127.0.0.1:...`. Use a real HTTP/mixed endpoint (`http://127.0.0.1:PORT`) or a supported `socks5://` setup. Process-level proxy variables affect update checks and Feishu HTTP calls.

### High-signal logs

| Log/result | Meaning |
| --- | --- |
| `HTTP Request ... 200 OK` | Feishu HTTP call succeeded; inspect returned data/counts separately. |
| `0 个部门，0 名成员` | Feishu returned no visible organization data for the query. |
| `open_id cross app` | Sending client and recipient ID are from different Lark app contexts. |
| `Bot/User can NOT be out of the chat` | Sending client is not a member of the source chat. |
| `processor not found` | Unsupported subscribed optional event. |

## Safe change checklist

1. Locate the behavior boundary: Lark transport, plugin routing, contact cache, card callback, pipeline, or Feishu API response.
2. Preserve inbound event context for replies and card actions.
3. Keep Feishu API calls behind service methods.
4. Test Bitable and Doc report-input paths for report changes.
5. Compile edited modules, restart AstrBot, and validate a real Lark message/card action.
