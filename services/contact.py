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

from lark_oapi.api.contact.v3 import (
    ListDepartmentRequest,
    ListUserRequest,
    GetUserRequest,
)
from astrbot.api import logger

_CACHE_REFRESH_INTERVAL = 5 * 60 * 60  # 5 hours in seconds


class ContactService:
    def __init__(self, lark_api):
        self.lark_api = lark_api
        self._org_tree_cache: list[dict] | None = None
        self._leader_ids: frozenset[str] = frozenset()
        self._cache_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

    def is_manager(self, open_id: str) -> bool:
        """Synchronous role check — safe to call from card action handlers."""
        return open_id in self._leader_ids

    # ── API wrappers ─────────────────────────────────────────────────────────

    async def list_root_departments(self) -> list:
        """List all root-level departments. Returns list of department objects."""
        req = (
            ListDepartmentRequest.builder()
            .parent_department_id("0")
            .user_id_type("open_id")
            .page_size(50)
            .build()
        )
        resp = await self.lark_api.contact.v3.department.alist(req)
        if not resp.success():
            raise RuntimeError(f"code={resp.code} msg={resp.msg}")
        return resp.data.items or []

    async def list_department_members(self, open_department_id: str) -> list:
        """List all members in a department. Returns list of user objects."""
        req = (
            ListUserRequest.builder()
            .department_id(open_department_id)
            .user_id_type("open_id")
            .page_size(50)
            .build()
        )
        resp = await self.lark_api.contact.v3.user.alist(req)
        if not resp.success():
            raise RuntimeError(f"code={resp.code} msg={resp.msg}")
        return resp.data.items or []

    async def get_user(self, open_id: str):
        """Fetch a single user profile by open_id. Returns user object or None."""
        req = (
            GetUserRequest.builder()
            .user_id(open_id)
            .user_id_type("open_id")
            .build()
        )
        resp = await self.lark_api.contact.v3.user.aget(req)
        if not resp.success() or not resp.data or not resp.data.user:
            return None
        return resp.data.user
 
    
    async def get_user_profile_dump(self, open_id: str) -> str:
        """Fetch user by open_id and return a formatted profile string for display.

        Always returns a string — never raises. Errors are embedded in the output.
        """
        try:
            user = await self.get_user(open_id)
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

    # ── Org tree cache ───────────────────────────────────────────────────────

    async def get_cached_org_tree(self) -> list[dict]:
        """Return the cached org tree. Blocks if a refresh is in progress."""
        async with self._cache_lock:
            if self._org_tree_cache is None:
                raise RuntimeError("Org tree cache not initialized yet")
            return self._org_tree_cache

    async def _do_refresh(self) -> None:
        """Fetch a fresh org tree and replace the cache under the lock."""
        logger.info("[Contact] 开始刷新部门缓存...")
        try:
            fresh = await self.get_org_tree()
        except Exception as e:
            logger.error(f"[Contact] 部门缓存刷新失败: {e}")
            return
        leader_ids = frozenset(
            e["manager"].open_id
            for e in fresh
            if e["manager"] and e["manager"].open_id
        )
        async with self._cache_lock:
            self._org_tree_cache = fresh
            self._leader_ids = leader_ids
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
            await self._do_refresh()

    async def start_cache(self) -> None:
        """Initial load + launch background refresh loop. Call from plugin initialize()."""
        await self._do_refresh()
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        logger.info(
            f"[Contact] 后台缓存刷新任务已启动（间隔 {_CACHE_REFRESH_INTERVAL // 3600} 小时）"
        )

    def stop_cache(self) -> None:
        """Cancel the background refresh task. Call from plugin terminate()."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            logger.info("[Contact] 后台缓存刷新任务已停止")

    # ── Composite queries ────────────────────────────────────────────────────

    async def get_org_tree(self) -> list[dict]:
        """Return structured org tree for all root departments.

        Each entry: {dept, manager, members}
          dept    — department object
          manager — user object of dept leader, or None if unset
          members — list of user objects

        Per-department member/manager errors are logged and skipped.
        """
        departments = await self.list_root_departments()
        result = []
        for dept in departments:
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            leader_open_id = getattr(dept, "leader_user_id", "") or ""

            try:
                members = await self.list_department_members(open_dept_id)
            except Exception as e:
                logger.warning(f"[Contact] 获取部门成员失败 {dept.name}: {e}")
                members = []

            manager = None
            if leader_open_id:
                try:
                    manager = await self.get_user(leader_open_id)
                except Exception:
                    pass

            result.append({"dept": dept, "manager": manager, "members": members})
        return result

    async def list_all_members(self) -> tuple[list[dict], list[str]]:
        """Return (contacts, errors) — flat deduped member list across all root departments.

        contacts: [{name, job_title, open_id}]
        errors:   per-department failure messages (non-fatal)
        """
        departments = await self.list_root_departments()
        seen = set()
        contacts = []
        errors = []
        for dept in departments:
            open_dept_id = getattr(dept, "open_department_id", "") or ""
            try:
                members = await self.list_department_members(open_dept_id)
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

