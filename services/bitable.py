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
)


class BitableService:
    def __init__(self, lark_api):
        self.lark_api = lark_api

    async def list_tables(self, app_token: str) -> list:
        # TODO: call bitable.v1.app_table.alist with app_token
        # TODO: return [{table_id, name}] for all tables in the app
        raise NotImplementedError

    async def list_fields(self, app_token: str, table_id: str) -> list:
        # TODO: call bitable.v1.app_table_field.alist with app_token + table_id
        # TODO: return [{field_id, field_name, field_type}]
        raise NotImplementedError

    async def list_records(self, app_token: str, table_id: str, page_size: int = 100) -> list:
        # TODO: loop on page_token until exhausted (pagination)
        # TODO: call bitable.v1.app_table_record.alist each iteration
        # TODO: return flat list of {record_id, fields: {field_name: value}}
        raise NotImplementedError
