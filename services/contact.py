#=====================================================
#  services/contact.py — Feishu Contact Service
#=====================================================
#
#  Responsibilities:
#    - Wrap lark_oapi Contact v3 API
#    - List departments and their members
#    - Resolve user profiles by open_id
#    - Provide structured org tree for display and report logic
#
#  Does NOT contain:
#    - Card logic
#    - Report generation logic
#=====================================================

import asyncio
import datetime
import os
from dataclasses import dataclass
from types import SimpleNamespace

from lark_oapi.api.contact.v3 import (
    ListDepartmentRequest,
    ListUserRequest,
    GetUserRequest,
)
from astrbot.api import logger

_CACHE_REFRESH_INTERVAL = 5 * 60 * 60  # 5 hours in seconds

# The explicit functional-test administrator is deliberately given this
# management context, regardless of their ordinary Feishu membership.
_TEST_ADMIN_MANAGER_DEPT_NAME = "技术部"


@dataclass(frozen=True)
class OrgDepartmentNode:
    """A department plus its direct parent, children, manager, and members."""

    dept: object
    manager: object | None
    members: tuple[object, ...]
    parent_department_id: str | None
    child_department_ids: tuple[str, ...]


@dataclass(frozen=True)
class OrgHierarchySnapshot:
    """App-scoped indexed organization hierarchy produced by one refresh."""

    nodes_by_department_id: dict[str, OrgDepartmentNode]
    root_department_ids: tuple[str, ...]
    department_ids_by_member: dict[str, tuple[str, ...]]


class ContactService:
    def __init__(self, lark_api):
        self.lark_api = lark_api
        # Contact data and open_ids are scoped to a Lark app.  Keep an
        # independent cache per adapter instead of sharing the first adapter's
        # organization tree with every app in this AstrBot process.
        self._lark_api_by_open_id: dict[str, object] = {}
        self._org_tree_cache_by_api: dict[int, list[dict]] = {}
        # Kept alongside the legacy flat cache until callers are migrated to
        # hierarchy-aware queries.
        self._org_hierarchy_cache_by_api: dict[int, OrgHierarchySnapshot] = {}
        self._leader_ids_by_api: dict[int, frozenset[str]] = {}
        # Temporary local override for functional testing.  Lark delivers an
        # app-scoped open_id rather than a reliable display name in events.
        self._test_admin_open_ids = frozenset(
            open_id.strip()
            for open_id in os.getenv("WRSBOT_TEST_ADMIN_OPEN_IDS", "").split(",")
            if open_id.strip()
        )
        self._cache_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

    def bind_lark_api(self, open_id: str, lark_api, *, schedule_refresh: bool = True) -> None:
        """Remember which Lark app owns an inbound user's app-scoped open_id."""
        if open_id and lark_api:
            self._lark_api_by_open_id[open_id] = lark_api
            # The first event from a newly seen app must populate that app's
            # cache before role/folder logic can safely use it.
            if schedule_refresh and self._api_key(lark_api) not in self._org_tree_cache_by_api:
                try:
                    asyncio.get_running_loop().create_task(
                        self._do_refresh(open_id, lark_api=lark_api)
                    )
                except RuntimeError:
                    # No loop during construction; start_cache handles the
                    # startup/default adapter in that case.
                    pass

    def _resolve_lark_api(self, open_id: str = "", lark_api=None):
        return lark_api or self._lark_api_by_open_id.get(open_id) or self.lark_api

    @staticmethod
    def _api_key(lark_api) -> int:
        return id(lark_api)

    def get_cached_org_tree_sync(self, open_id: str = "", lark_api=None) -> list[dict]:
        """Synchronous cache read for card-action handlers."""
        api = self._resolve_lark_api(open_id, lark_api)
        return self._org_tree_cache_by_api.get(self._api_key(api), [])

    def get_cached_org_hierarchy_sync(
        self, open_id: str = "", lark_api=None
    ) -> OrgHierarchySnapshot | None:
        """Return the hierarchy cache for this Lark app, if it is ready."""
        api = self._resolve_lark_api(open_id, lark_api)
        return self._org_hierarchy_cache_by_api.get(self._api_key(api))

    def is_manager(self, open_id: str) -> bool:
        """Synchronous role check — safe to call from card action handlers."""
        api = self._resolve_lark_api(open_id)
        leaders = self._leader_ids_by_api.get(self._api_key(api), frozenset())
        return open_id in leaders or open_id in self._test_admin_open_ids

    def is_test_admin(self, open_id: str) -> bool:
        """Whether the explicit local functional-test override grants this role."""
        return open_id in self._test_admin_open_ids

    def _managed_depts_from_tree(self, open_id: str, org_tree: list[dict]) -> list[dict]:
        managed = [
            entry for entry in org_tree
            if entry.get("manager") and entry["manager"].open_id == open_id
        ]
        if managed or open_id not in self._test_admin_open_ids:
            return managed

        # Temporary functional-test route: the configured test administrator
        # manages 技术部.  Do not derive this from the user's ordinary Feishu
        # membership — that membership can be AI产品应用 and is unrelated to
        # the explicit manager override.
        override_dept = next(
            (
                entry for entry in org_tree
                if (getattr(entry.get("dept"), "name", "") or "").strip()
                == _TEST_ADMIN_MANAGER_DEPT_NAME
            ),
            None,
        )
        if override_dept:
            dept = override_dept["dept"]
            logger.warning(
                f"[Contact] 临时测试管理员使用指定部门作为管理上下文: "
                f"open_id={open_id} dept={dept.name or ''} "
                f"open_dept_id={getattr(dept, 'open_department_id', '') or ''}"
            )
            return [override_dept]

        # If the target department is not yet present in the cache, retain the
        # old fallback so the test route remains usable while it is incomplete.
        root = next(
            (entry for entry in org_tree
             if getattr(entry.get("dept"), "open_department_id", "") == "0"),
            None,
        )
        return [root] if root else org_tree[:1]

    def get_managed_depts_sync(self, open_id: str) -> list[dict]:
        """Synchronous role/dept lookup for card callback handlers."""
        return self._managed_depts_from_tree(open_id, self.get_cached_org_tree_sync(open_id))

    async def get_managed_depts(self, open_id: str) -> list[dict]:
        """Return actual leader departments, with the explicit test fallback."""
        return self._managed_depts_from_tree(
            open_id, await self.get_cached_org_tree(open_id)
        )

    # ── API wrappers ─────────────────────────────────────────────────────────

    async def list_root_departments(self, *, lark_api=None) -> list:
        """List every department below the organization root.

        ``fetch_child=True`` asks Feishu to recurse through nested
        departments.  The former implementation only returned the root's
        immediate children, so employees in a nested department were missed.
        """
        departments = []
        page_token = None
        while True:
            builder = (
                ListDepartmentRequest.builder()
                .parent_department_id("0")
                .fetch_child(True)
                .user_id_type("open_id")
                .page_size(50)
            )
            if page_token:
                builder.page_token(page_token)
            api = self._resolve_lark_api(lark_api=lark_api)
            resp = await api.contact.v3.department.alist(builder.build())
            if not resp.success():
                raise RuntimeError(f"code={resp.code} msg={resp.msg}")
            departments.extend(resp.data.items or [])
            if not getattr(resp.data, "has_more", False):
                return departments
            page_token = getattr(resp.data, "page_token", None)
            if not page_token:
                return departments

    async def list_department_members(self, open_department_id: str, *, lark_api=None) -> list:
        """List every direct member in a department, across all pages."""
        members = []
        page_token = None
        while True:
            builder = (
                ListUserRequest.builder()
                .department_id(open_department_id)
                .user_id_type("open_id")
                .page_size(50)
            )
            if page_token:
                builder.page_token(page_token)
            api = self._resolve_lark_api(lark_api=lark_api)
            resp = await api.contact.v3.user.alist(builder.build())
            if not resp.success():
                raise RuntimeError(f"code={resp.code} msg={resp.msg}")
            members.extend(resp.data.items or [])
            if not getattr(resp.data, "has_more", False):
                return members
            page_token = getattr(resp.data, "page_token", None)
            if not page_token:
                return members

    async def get_user(self, open_id: str, *, lark_api=None):
        """Fetch a single user profile by open_id. Returns user object or None."""
        req = (
            GetUserRequest.builder()
            .user_id(open_id)
            .user_id_type("open_id")
            .build()
        )
        api = self._resolve_lark_api(open_id, lark_api)
        resp = await api.contact.v3.user.aget(req)
        if not resp.success() or not resp.data or not resp.data.user:
            return None
        return resp.data.user
 
    
    async def get_user_profile_dump(self, open_id: str, *, lark_api=None) -> str:
        """Fetch user by open_id and return a formatted profile string for display.

        Always returns a string — never raises. Errors are embedded in the output.
        """
        try:
            user = await self.get_user(open_id, lark_api=lark_api)
        except Exception as e:
            return f"获取用户失败: {e}"
        if not user:
            return f"未找到用户 open_id={open_id}"

        _GENDER   = {0: "未知", 1: "男", 2: "女", 3: "其他"}
        _EMP_TYPE = {1: "正式", 2: "实习", 3: "外包", 4: "劳务", 5: "顾问"}

        status = user.status
        status_parts = []
        if status:
            if getattr(status, "is_activated", None): status_parts.append("激活")
            if getattr(status, "is_frozen",    None): status_parts.append("冻结")
            if getattr(status, "is_resigned",  None): status_parts.append("离职")
            if getattr(status, "is_unjoin",    None): status_parts.append("未加入")

        join_ts  = getattr(user, "join_time", None)
        join_str = datetime.datetime.fromtimestamp(join_ts).strftime("%Y-%m-%d") if join_ts else "N/A"

        lines = [
            "══ 飞书用户档案 ══",
            "",
            "── 身份 ──",
            f"open_id       : {user.open_id or 'N/A'}",
            f"user_id       : {user.user_id or 'N/A'}",
            f"union_id      : {user.union_id or 'N/A'}",
            f"employee_no   : {getattr(user, 'employee_no', '') or 'N/A'}",
            f"employee_type : {_EMP_TYPE.get(getattr(user, 'employee_type', 0), 'N/A')}",
            "",
            "── 基本信息 ──",
            f"name          : {user.name or 'N/A'}",
            f"en_name       : {getattr(user, 'en_name', '') or 'N/A'}",
            f"gender        : {_GENDER.get(getattr(user, 'gender', 0), 'N/A')}",
            f"email         : {getattr(user, 'email', '') or 'N/A'}",
            f"mobile        : {getattr(user, 'mobile', '') or 'N/A'}",
            "",
            "── 职务 ──",
            f"job_title     : {getattr(user, 'job_title', '') or 'N/A'}",
            f"city          : {getattr(user, 'city', '') or 'N/A'}",
            f"work_station  : {getattr(user, 'work_station', '') or 'N/A'}",
            f"join_time     : {join_str}",
            "",
            "── 组织 ──",
            f"department_ids: {user.department_ids[1:] or []}",
            f"is_tenant_mgr : {bool(getattr(user, 'is_tenant_manager', False))}",
            f"status        : {', '.join(status_parts) if status_parts else 'N/A'}",
        ]
        return "\n".join(lines)

    @staticmethod
    def format_org_tree_dump(org_tree: list[dict], source: str = "live") -> str:
        """Return a formatted string representation of an org tree for display.

        source: label shown in the header, e.g. 'live' or 'cached'.
        """
        lines = [f"══ 飞书通讯录（{source}，根部门共 {len(org_tree)} 个）══", ""]
        for entry in org_tree:
            dept    = entry["dept"]
            manager = entry["manager"]
            members = entry["members"]

            dept_id      = dept.department_id or ""
            member_count = getattr(dept, "member_count", "?")
            lines.append(f"📁 {dept.name}  (id: {dept_id}, 成员数: {member_count})")

            if manager:
                mgr_title = getattr(manager, "job_title", "") or ""
                mgr_line  = f"  👑 负责人: {manager.name}"
                if mgr_title:
                    mgr_line += f"  [{mgr_title}]"
                lines.append(mgr_line)

            if not members:
                lines.append("  └─ （暂无成员）")
            else:
                for i, u in enumerate(members):
                    prefix    = "  └─" if i == len(members) - 1 else "  ├─"
                    job_title = getattr(u, "job_title", "") or ""
                    job_level = getattr(u, "job_level_name", "") or ""
                    details   = " | ".join(x for x in [job_title, job_level] if x)
                    line      = f"{prefix} {u.name}"
                    if details:
                        line += f"  [{details}]"
                    lines.append(line)
            lines.append("")

        if not org_tree:
            lines.append("未找到任何部门，请确认应用通讯录权限已授权。")
        return "\n".join(lines)

    @staticmethod
    def format_org_hierarchy_dump(
        hierarchy: OrgHierarchySnapshot, source: str = "cached"
    ) -> str:
        """Format the cached parent/child hierarchy for developer inspection."""
        nodes = hierarchy.nodes_by_department_id
        lines = [f"══ 飞书通讯录（{source}，层级部门共 {len(nodes)} 个）══", ""]
        visited: set[str] = set()

        def append_node(department_id: str, prefix: str, is_last: bool) -> None:
            if department_id in visited:
                lines.append(f"{prefix}{'└─' if is_last else '├─'} [循环引用] {department_id}")
                return
            visited.add(department_id)
            node = nodes[department_id]
            dept = node.dept
            connector = "└─" if is_last else "├─"
            manager_name = getattr(node.manager, "name", "") or "未设置"
            lines.append(
                f"{prefix}{connector} 📁 {getattr(dept, 'name', '') or department_id} "
                f"(负责人: {manager_name}，直属成员: {len(node.members)})"
            )
            child_prefix = prefix + ("   " if is_last else "│  ")
            for index, child_id in enumerate(node.child_department_ids):
                append_node(child_id, child_prefix, index == len(node.child_department_ids) - 1)

        for index, department_id in enumerate(hierarchy.root_department_ids):
            append_node(department_id, "", index == len(hierarchy.root_department_ids) - 1)
        for department_id in nodes:
            if department_id not in visited:
                append_node(department_id, "", True)
        if not nodes:
            lines.append("未找到任何部门，请确认应用通讯录权限已授权。")
        return "\n".join(lines)

    # ── Org tree cache ───────────────────────────────────────────────────────

    async def get_cached_org_tree(self, open_id: str = "", *, lark_api=None) -> list[dict]:
        """Return the cached org tree. Blocks if a refresh is in progress."""
        async with self._cache_lock:
            api = self._resolve_lark_api(open_id, lark_api)
            cached = self._org_tree_cache_by_api.get(self._api_key(api))
            if cached is None:
                raise RuntimeError("Org tree cache not initialized yet")
            return cached

    async def get_cached_org_hierarchy(
        self, open_id: str = "", *, lark_api=None
    ) -> OrgHierarchySnapshot:
        """Return the hierarchy cached for this Lark app."""
        async with self._cache_lock:
            api = self._resolve_lark_api(open_id, lark_api)
            cached = self._org_hierarchy_cache_by_api.get(self._api_key(api))
            if cached is None:
                raise RuntimeError("Org hierarchy cache not initialized yet")
            return cached

    @staticmethod
    def _build_org_hierarchy(org_tree: list[dict]) -> OrgHierarchySnapshot:
        """Build parent/child and member indexes from the legacy flat tree.

        The public hierarchy key is ``open_department_id``.  Feishu sometimes
        returns a parent using its internal ``department_id``, so both IDs are
        indexed while resolving parent links.
        """
        pending: dict[str, tuple[dict, str | None]] = {}
        aliases: dict[str, str] = {}
        for entry in org_tree:
            dept = entry["dept"]
            department_id = (
                getattr(dept, "open_department_id", "")
                or getattr(dept, "department_id", "")
            )
            if not department_id or department_id in pending:
                continue
            raw_parent_id = getattr(dept, "parent_department_id", "") or None
            pending[department_id] = (entry, raw_parent_id)
            aliases[department_id] = department_id
            internal_id = getattr(dept, "department_id", "") or ""
            if internal_id:
                aliases.setdefault(internal_id, department_id)

        child_ids: dict[str, list[str]] = {department_id: [] for department_id in pending}
        parents: dict[str, str | None] = {}
        root_ids: list[str] = []
        for department_id, (_, raw_parent_id) in pending.items():
            parent_id = aliases.get(raw_parent_id or "")
            # "0" is the virtual organization root. Missing or unknown
            # parents are safely shown as roots rather than hidden.
            if raw_parent_id == "0" or not parent_id or parent_id == department_id:
                parents[department_id] = None
                root_ids.append(department_id)
            else:
                parents[department_id] = parent_id
                child_ids[parent_id].append(department_id)

        memberships: dict[str, list[str]] = {}
        nodes: dict[str, OrgDepartmentNode] = {}
        for department_id, (entry, _) in pending.items():
            members = tuple(entry.get("members") or [])
            for member in members:
                member_open_id = getattr(member, "open_id", "") or ""
                if member_open_id:
                    memberships.setdefault(member_open_id, []).append(department_id)
            nodes[department_id] = OrgDepartmentNode(
                dept=entry["dept"],
                manager=entry.get("manager"),
                members=members,
                parent_department_id=parents[department_id],
                child_department_ids=tuple(child_ids[department_id]),
            )

        return OrgHierarchySnapshot(
            nodes_by_department_id=nodes,
            root_department_ids=tuple(root_ids),
            department_ids_by_member={
                member_id: tuple(department_ids)
                for member_id, department_ids in memberships.items()
            },
        )

    async def _do_refresh(self, open_id: str = "", *, lark_api=None) -> None:
        """Fetch a fresh org tree and replace the cache under the lock."""
        logger.info("[Contact] 开始刷新部门缓存...")
        api = self._resolve_lark_api(open_id, lark_api)
        try:
            fresh = await self.get_org_tree(lark_api=api)
        except Exception as e:
            logger.error(f"[Contact] 部门缓存刷新失败: {e}")
            return
        hierarchy = self._build_org_hierarchy(fresh)
        leader_ids = frozenset(
            e["manager"].open_id
            for e in fresh
            if e["manager"] and e["manager"].open_id
        )
        async with self._cache_lock:
            key = self._api_key(api)
            self._org_tree_cache_by_api[key] = fresh
            self._org_hierarchy_cache_by_api[key] = hierarchy
            self._leader_ids_by_api[key] = leader_ids
        dept_count   = len(fresh)
        member_count = sum(len(e["members"]) for e in fresh)
        logger.info(
            f"[Contact] 部门缓存已更新：{dept_count} 个部门，{member_count} 名成员，"
            f"{len(leader_ids)} 名负责人"
        )

    async def _refresh_loop(self) -> None:
        """Background task: refresh org tree every _CACHE_REFRESH_INTERVAL seconds."""
        while True:
            await asyncio.sleep(_CACHE_REFRESH_INTERVAL)
            apis = {id(self.lark_api): self.lark_api}
            apis.update({id(api): api for api in self._lark_api_by_open_id.values()})
            for api in apis.values():
                await self._do_refresh(lark_api=api)

    async def start_cache(self, lark_apis: list | None = None) -> None:
        """Launch the refresh loop and warm each configured Lark adapter.

        Each adapter has an app/tenant-scoped contact directory.  Warming the
        complete platform-adapter list uses the same adapter-specific refresh
        path as an inbound Feishu event, without guessing which adapter the
        first user will use after a restart.
        """
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        api_source = [self.lark_api] if lark_apis is None else lark_apis
        startup_apis = list({id(api): api for api in api_source if api}.values())
        logger.info(
            f"[Contact] 后台缓存刷新任务已启动（间隔 {_CACHE_REFRESH_INTERVAL // 3600} 小时），正在预热 {len(startup_apis)} 个飞书应用通讯录缓存"
        )
        # Do not block plugin startup on complete organization-directory
        # scans. _do_refresh handles API failures and only publishes a
        # complete result to each adapter's cache.
        for api in startup_apis:
            asyncio.create_task(self._do_refresh(lark_api=api))

    def stop_cache(self) -> None:
        """Cancel the background refresh task. Call from plugin terminate()."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            logger.info("[Contact] 后台缓存刷新任务已停止")

    # ── Composite queries ────────────────────────────────────────────────────

    async def get_org_tree(self, *, lark_api=None) -> list[dict]:
        """Return structured data for every department in the organization.

        Each entry: {dept, manager, members}
          dept    — department object
          manager — user object of dept leader, or None if unset
          members — list of user objects

        Per-department member/manager errors are logged and skipped.
        """
        api = self._resolve_lark_api(lark_api=lark_api)
        departments = await self.list_root_departments(lark_api=api)
        result = []
        for dept in departments:
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            leader_open_id = getattr(dept, "leader_user_id", "") or ""

            try:
                members = await self.list_department_members(open_dept_id, lark_api=api)
            except Exception as e:
                logger.warning(f"[Contact] 获取部门成员失败 {dept.name}: {e}")
                members = []

            manager = None
            if leader_open_id:
                try:
                    manager = await self.get_user(leader_open_id, lark_api=api)
                except Exception:
                    pass

            result.append({"dept": dept, "manager": manager, "members": members})

        # Users can be assigned directly to the organization root instead of a
        # named department.  Preserve them in the same entry shape so employee
        # views can still locate them.
        try:
            root_members = await self.list_department_members("0", lark_api=api)
        except Exception as e:
            logger.warning(f"[Contact] 获取根部门成员失败: {e}")
            root_members = []
        if root_members:
            result.append(
                {
                    "dept": SimpleNamespace(
                        department_id="0",
                        open_department_id="0",
                        name="根部门",
                        member_count=len(root_members),
                        leader_user_id="",
                    ),
                    "manager": None,
                    "members": root_members,
                }
            )
        return result

    async def list_all_members(self, *, lark_api=None) -> tuple[list[dict], list[str]]:
        """Return (contacts, errors) — flat deduped member list across all root departments.

        contacts: [{name, job_title, open_id}]
        errors:   per-department failure messages (non-fatal)
        """
        api = self._resolve_lark_api(lark_api=lark_api)
        departments = await self.list_root_departments(lark_api=api)
        seen = set()
        contacts = []
        errors = []
        for dept in departments:
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            try:
                members = await self.list_department_members(open_dept_id, lark_api=api)
            except Exception as e:
                errors.append(f"部门「{dept.name}」获取成员失败: {e}")
                continue
            for u in members:
                if u.open_id not in seen:
                    seen.add(u.open_id)
                    contacts.append({
                        "name": u.name,
                        "job_title": getattr(u, "job_title", "") or "",
                        "open_id": u.open_id,
                    })
        return contacts, errors
