"""Regression tests for ContactService's additive hierarchy cache."""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


def _install_runtime_stubs() -> None:
    try:
        import astrbot.api  # noqa: F401
    except ModuleNotFoundError:
        astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
        api = types.ModuleType("astrbot.api")
        api.logger = logging.getLogger("test.contact")
        sys.modules["astrbot.api"] = api
        astrbot.api = api

    try:
        import lark_oapi.api.contact.v3  # noqa: F401
    except ModuleNotFoundError:
        lark_oapi = sys.modules.setdefault("lark_oapi", types.ModuleType("lark_oapi"))
        lark_api = sys.modules.setdefault("lark_oapi.api", types.ModuleType("lark_oapi.api"))
        contact = sys.modules.setdefault(
            "lark_oapi.api.contact", types.ModuleType("lark_oapi.api.contact")
        )
        v3 = types.ModuleType("lark_oapi.api.contact.v3")
        v3.ListDepartmentRequest = object
        v3.ListUserRequest = object
        v3.GetUserRequest = object
        sys.modules["lark_oapi.api.contact.v3"] = v3
        lark_oapi.api = lark_api
        lark_api.contact = contact
        contact.v3 = v3


_install_runtime_stubs()

from services.contact import ContactService  # noqa: E402


def _dept(name: str, open_id: str, parent: str = "0", internal_id: str = ""):
    return types.SimpleNamespace(
        name=name,
        open_department_id=open_id,
        department_id=internal_id or open_id,
        parent_department_id=parent,
    )


def _user(open_id: str, name: str):
    return types.SimpleNamespace(open_id=open_id, name=name)


class ContactHierarchyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = ContactService._build_org_hierarchy([
            {
                "dept": _dept("技术部", "tech", internal_id="internal_tech"),
                "manager": _user("tech_lead", "Tech Lead"),
                "members": [_user("justin", "Justin"), _user("shared", "Shared")],
            },
            {
                # Feishu parent reference uses the parent's internal ID here.
                "dept": _dept("AI产品应用", "ai", parent="internal_tech"),
                "manager": _user("ai_lead", "AI Lead"),
                "members": [_user("shared", "Shared")],
            },
            {
                "dept": _dept("算法组", "algo", parent="ai"),
                "manager": None,
                "members": [_user("algo_member", "Algorithm Member")],
            },
        ])

    def test_builds_the_complete_parent_child_path(self):
        self.assertEqual(self.snapshot.root_department_ids, ("tech",))
        self.assertEqual(
            self.snapshot.nodes_by_department_id["tech"].child_department_ids,
            ("ai",),
        )
        self.assertEqual(
            self.snapshot.nodes_by_department_id["ai"].parent_department_id,
            "tech",
        )
        self.assertEqual(
            self.snapshot.nodes_by_department_id["ai"].child_department_ids,
            ("algo",),
        )

    def test_preserves_all_of_a_users_direct_department_memberships(self):
        self.assertEqual(
            self.snapshot.department_ids_by_member["shared"],
            ("tech", "ai"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
