#=====================================================
#  services/bitable.py — Feishu Bitable Service
#=====================================================
#
#  Responsibilities:
#    - Wrap lark_oapi Bitable API with domain-level operations
#    - Fetch records from a Bitable table with pagination
#    - Discover tables and field definitions for setup
#    - Return clean Python dicts (not raw API objects)
#
#  Does NOT contain:
#    - LLM logic
#    - Report generation logic
#    - Card sending logic
#    - Date filtering (caller filters client-side after fetch)
#=====================================================

from lark_oapi.api.bitable.v1 import (
    ListAppTableRequest,
    ListAppTableFieldRequest,
    ListAppTableRecordRequest,
    UpdateAppTableRecordRequest,
    AppTableRecord,
)
from astrbot.api import logger


class BitableService:
    def __init__(self, lark_api):
        self.lark_api = lark_api

    async def list_tables(self, app_token: str) -> list[dict]:
        """List all tables in a Bitable app. Returns [{table_id, name}]."""
        req = ListAppTableRequest.builder().app_token(app_token).build()
        resp = await self.lark_api.bitable.v1.app_table.alist(req)
        if not resp.success():
            raise RuntimeError(f"[Bitable] list_tables 失败: code={resp.code} msg={resp.msg}")
        return [
            {"table_id": t.table_id or "", "name": t.name or ""}
            for t in (resp.data.items or [])
        ]

    async def list_fields(self, app_token: str, table_id: str) -> list[dict]:
        """List all field definitions for a table. Returns [{field_id, field_name, field_type}]."""
        req = (
            ListAppTableFieldRequest.builder()
            .app_token(app_token)
            .table_id(table_id)
            .build()
        )
        resp = await self.lark_api.bitable.v1.app_table_field.alist(req)
        if not resp.success():
            raise RuntimeError(f"[Bitable] list_fields 失败: code={resp.code} msg={resp.msg}")
        return [
            {
                "field_id":   f.field_id or "",
                "field_name": f.field_name or "",
                "field_type": f.type or 0,
            }
            for f in (resp.data.items or [])
        ]

    async def list_records(self, app_token: str, table_id: str, page_size: int = 100) -> list[dict]:
        """Fetch all records from a Bitable table with automatic pagination.

        Returns a flat list of {record_id, fields} where fields is the raw
        key→value dict from the API (field names as keys, mixed value types).
        Callers are responsible for interpreting field types.
        """
        records = []
        page_token: str | None = None

        while True:
            builder = (
                ListAppTableRecordRequest.builder()
                .app_token(app_token)
                .table_id(table_id)
                .page_size(page_size)
            )
            if page_token:
                builder = builder.page_token(page_token)

            resp = await self.lark_api.bitable.v1.app_table_record.alist(builder.build())
            if not resp.success():
                raise RuntimeError(
                    f"[Bitable] list_records 失败: code={resp.code} msg={resp.msg}"
                )

            for r in (resp.data.items or []):
                records.append({"record_id": r.record_id or "", "fields": r.fields or {}})

            if not resp.data.has_more:
                break
            page_token = resp.data.page_token

        logger.debug(f"[Bitable] list_records: app={app_token} table={table_id} → {len(records)} 条记录")
        return records

    async def find_summary_record(self, app_token: str, table_id: str) -> str | None:
        """Find the record_id of the '部门总结' row in a weekly table.

        Scans all records and returns the record_id of the first one whose
        fields contain a value that includes '部门总结'. Returns None if not found.
        """
        records = await self.list_records(app_token, table_id)
        for rec in records:
            for val in rec["fields"].values():
                text = ""
                if isinstance(val, str):
                    text = val
                elif isinstance(val, dict):
                    text = val.get("text") or val.get("name") or ""
                elif isinstance(val, list) and val:
                    first = val[0]
                    text = first.get("text") or first.get("name") or "" if isinstance(first, dict) else str(first)
                if "部门总结" in text:
                    logger.debug(f"[Bitable] 找到部门总结行: record_id={rec['record_id']}")
                    return rec["record_id"]
        logger.warning(f"[Bitable] 未找到部门总结行: app={app_token} table={table_id}")
        return None

    async def update_record(self, app_token: str, table_id: str, record_id: str, fields: dict) -> None:
        """Update a single record's fields in a Bitable table."""
        body = AppTableRecord.builder().fields(fields).build()
        req = (
            UpdateAppTableRecordRequest.builder()
            .app_token(app_token)
            .table_id(table_id)
            .record_id(record_id)
            .request_body(body)
            .build()
        )
        resp = await self.lark_api.bitable.v1.app_table_record.aupdate(req)
        if not resp.success():
            raise RuntimeError(
                f"[Bitable] update_record 失败: code={resp.code} msg={resp.msg}"
            )
        logger.debug(f"[Bitable] update_record: record_id={record_id} fields={list(fields.keys())}")

    @staticmethod
    def records_to_text(records: list[dict]) -> str:
        """Serialize bitable records to a readable text block for LLM consumption.

        Each record becomes one numbered paragraph. Field values that are lists
        (e.g. Person, MultiSelect) are joined with commas. Other types are str()'d.
        """
        lines = []
        for i, rec in enumerate(records, 1):
            field_parts = []
            for key, val in rec["fields"].items():
                if isinstance(val, list):
                    # Person field: [{"id": "ou_xxx", "name": "张三"}, ...]
                    # MultiSelect:  [{"text": "选项A"}, ...]
                    text_val = ", ".join(
                        v.get("name") or v.get("text") or str(v)
                        for v in val
                        if isinstance(v, dict)
                    ) or str(val)
                elif isinstance(val, dict):
                    text_val = val.get("text") or val.get("name") or str(val)
                else:
                    text_val = str(val)
                field_parts.append(f"{key}: {text_val}")
            lines.append(f"[记录 {i}]\n" + "\n".join(field_parts))
        return "\n\n".join(lines)
