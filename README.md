# WRSbot — Feishu Weekly Report Summary Bot

A [AstrBot](https://github.com/AstrBotDevs/AstrBot) plugin for automating weekly report collection and summarization inside Feishu (Lark) workspaces.

> **Author:** Justin Lee (Qaz-hw)
> **Version:** v1.0.0
> **Platform:** Feishu / Lark (飞书)

---

## Features

### Weekly Report

| Command | Description |
| --- | --- |
| `/创建周报总结` | Generate a weekly report summary for the sender |

### Diagnostics & Testing

| Command | Description |
| --- | --- |
| `/whoami` | Dump all available sender, message, session, platform, and group info |
| `/test_llm <question>` | Send a question directly to the LLM and return the raw response |
| `/helloworld` | Basic connectivity test; echoes back the sender's name and message |
| `/关于WRSbot` | Display a short description of what WRSbot is |

---

## Feishu API Permissions

This bot requires the following Feishu Open Platform scopes.

### Tenant-level (application identity)

| Scope | Purpose |
| --- | --- |
| `contact:contact.base:readonly` | Read contact basic info |
| `contact:department.base:readonly` | Read department basic info |
| `contact:department.organize:readonly` | Read department org structure |
| `contact:functional_role:readonly` | Read functional roles |
| `contact:user.basic_profile:readonly` | Read user profile (name, avatar, email) |
| `contact:user.job_level:readonly` | Read user job level |
| `bitable:app` | Read/write Bitable (multi-dimensional tables) |
| `bitable:app:readonly` | Read-only Bitable |
| `docs:document:export` | Export cloud documents |
| `docs:event.document_deleted:read` | Subscribe to document deleted events |
| `docs:event.document_edited:read` | Subscribe to document edited events |
| `drive:file:download` | Download Drive files |
| `drive:file:readonly` | Read Drive file metadata |
| `drive:file:view_record:readonly` | Read file view history |
| `im:app_feed_card:write` | Write app feed cards |
| `im:chat:readonly` | Read group/chat info |
| `im:message` | Send and receive messages (full) |
| `im:message.group_at_msg.include_bot:readonly` | Read group @bot messages |
| `im:message.group_at_msg:readonly` | Read group @messages |
| `im:message.p2p_msg:readonly` | Read P2P (direct) messages |
| `im:message:send_as_bot` | Send messages as bot |
| `im:message:send_multi_depts` | Send messages to multiple departments |
| `sheets:spreadsheet` | Read/write Sheets spreadsheets |
| `space:document.event:read` | Read knowledge space document events |

### User-level (OAuth authorization)

| Scope | Purpose |
| --- | --- |
| `docx:document:readonly` | Read Docx documents on behalf of a user |

---

## Requirements

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) v4.5.0+
- A Feishu application with the scopes listed above
- An LLM provider configured in AstrBot (for `/test_llm` and `/创建周报总结`)

---

## Setup

1. Install this plugin via the AstrBot plugin manager or by cloning into your `data/plugins/` directory.
2. Configure your Feishu app credentials in the AstrBot admin panel under **Platform → Feishu**.
3. Configure an LLM provider in the AstrBot admin panel under **Provider**.
4. (Optional) Set up a Persona with your desired system prompt under **人格** in the admin panel.

---

## Links

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
- [Feishu Open Platform](https://open.feishu.cn)
- [Plugin Repo](https://github.com/Qaz-hw/astrbot_plugin_WRSbot)
