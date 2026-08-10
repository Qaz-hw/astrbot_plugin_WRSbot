#=====================================================
#  utils/env_config.py — Department Folder Binding Config
#=====================================================
#
#  Responsibilities:
#    - Read / write per-department Feishu folder tokens to .env
#    - Extract folder token from a Feishu folder URL
#    - Build the user-facing folder URL from token + tenant prefix
#    - Dump all bindings cross-referenced against org tree (dev check)
#
#  Key format in .env:
#    DEPT_FOLDER_{open_department_id with hyphens → underscores}
#    e.g.  DEPT_FOLDER_od_c5a40c187b6a50163de9c30b4dbe84b4=fldcnXXX
#
#  Tenant URL prefix:
#    Tenant subdomains differ per Feishu tenant but stay constant per
#    deployment, so we keep a single prefix and just append the token.
#    Override via FEISHU_DRIVE_FOLDER_URL_PREFIX in .env if your tenant
#    isn't the default below.
#
#  Does NOT contain:
#    - Card logic
#    - Contact API calls
#    - Any AstrBot imports
#=====================================================

import os
import re
from pathlib import Path

from dotenv import set_key, unset_key, dotenv_values

_ENV_PATH = Path(__file__).parent.parent / ".env"
_DEPT_FOLDER_PREFIX = "DEPT_FOLDER_"
_DEPT_DAILY_REPORT_FOLDER_PREFIX = "DEPT_DAILY_REPORT_FOLDER_"

# Tenant-specific folder URL prefix. Trailing slash required. Override via
# .env to support a different Feishu tenant subdomain or larksuite.com.
FEISHU_DRIVE_FOLDER_URL_PREFIX = os.getenv(
    "FEISHU_DRIVE_FOLDER_URL_PREFIX",
    "https://ucnfx592kr5a.feishu.cn/drive/folder/",
)


# ── Key helpers ──────────────────────────────────────────────────────────────

def _to_env_key(open_dept_id: str) -> str:
    """Convert open_department_id to a valid env var key."""
    return _DEPT_FOLDER_PREFIX + open_dept_id.replace("-", "_")


def _from_env_key(env_key: str) -> str:
    """Reverse _to_env_key: env key → open_department_id."""
    raw = env_key[len(_DEPT_FOLDER_PREFIX):]
    # restore the 'od-' prefix hyphens: first two underscores back to hyphens
    # od_xxxx → od-xxxx
    return raw.replace("_", "-", 1)


def _from_daily_report_env_key(env_key: str) -> str:
    """Reverse a PMbot daily-report folder key back to department ID."""
    raw = env_key[len(_DEPT_DAILY_REPORT_FOLDER_PREFIX):]
    return raw.replace("_", "-", 1)


# ── Public API ───────────────────────────────────────────────────────────────

def get_dept_folder_token(open_dept_id: str) -> str | None:
    """Return the stored folder token for a department, or None if unset."""
    return os.getenv(_to_env_key(open_dept_id)) or None


def set_dept_folder_token(open_dept_id: str, token: str) -> None:
    """Persist a folder token for a department to .env and apply to current process."""
    key = _to_env_key(open_dept_id)
    set_key(_ENV_PATH, key, token)
    os.environ[key] = token


def _to_daily_report_env_key(open_dept_id: str) -> str:
    """Return the isolated .env key for a department's PMbot daily-report folder."""
    return _DEPT_DAILY_REPORT_FOLDER_PREFIX + open_dept_id.replace("-", "_")


def get_dept_daily_report_folder_token(open_dept_id: str) -> str | None:
    """Return the PMbot daily-report folder token for a department, if bound."""
    return os.getenv(_to_daily_report_env_key(open_dept_id)) or None


def set_dept_daily_report_folder_token(open_dept_id: str, token: str) -> None:
    """Persist a PMbot daily-report folder token without changing weekly bindings."""
    key = _to_daily_report_env_key(open_dept_id)
    set_key(_ENV_PATH, key, token)
    os.environ[key] = token


def get_dept_folder_url(open_dept_id: str) -> str | None:
    """Return the user-facing folder URL for a department.

    Built from FEISHU_DRIVE_FOLDER_URL_PREFIX + stored token. Returns None
    only when no token is bound. URLs reconstruct deterministically — the
    tenant subdomain is constant per deployment — so we don't store the
    URL itself, just the token.
    """
    token = get_dept_folder_token(open_dept_id)
    if not token:
        return None
    return FEISHU_DRIVE_FOLDER_URL_PREFIX + token


def delete_dept_folder_token(open_dept_id: str) -> bool:
    """Remove a single department's folder token from .env and the current process.

    Returns True if a key was removed, False if it wasn't set.
    """
    key = _to_env_key(open_dept_id)
    existed = key in (dotenv_values(_ENV_PATH) or {}) or key in os.environ
    unset_key(_ENV_PATH, key)
    os.environ.pop(key, None)
    return existed


def clear_all_dept_bindings() -> int:
    """Remove every DEPT_FOLDER_* key from .env and the current process env.

    Returns the number of bindings deleted. Reads from both the .env file
    and os.environ so we catch keys that exist in only one place.
    """
    file_keys = {
        k for k in (dotenv_values(_ENV_PATH) or {}).keys()
        if k.startswith(_DEPT_FOLDER_PREFIX)
    }
    process_keys = {
        k for k in list(os.environ.keys())
        if k.startswith(_DEPT_FOLDER_PREFIX)
    }
    all_keys = file_keys | process_keys
    for key in all_keys:
        unset_key(_ENV_PATH, key)
        os.environ.pop(key, None)
    return len(all_keys)


def extract_folder_token_from_url(url: str) -> str | None:
    """Extract folder token from a Feishu folder URL.

    Supports:
      https://xxx.feishu.cn/drive/folder/fldcnABCDEFG
      https://xxx.larksuite.com/drive/folder/fldcnABCDEFG
    Returns None if the URL doesn't match the expected pattern.
    """
    m = re.search(r"/folder/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def list_all_dept_bindings() -> list[dict]:
    """Return all dept folder bindings currently stored in env.

    Each entry: {env_key, open_dept_id, folder_token}
    Useful for cross-referencing with org tree.
    """
    return [
        {
            "env_key": key,
            "open_dept_id": _from_env_key(key),
            "folder_token": val,
        }
        for key, val in os.environ.items()
        if key.startswith(_DEPT_FOLDER_PREFIX) and val
    ]


def list_all_dept_daily_report_bindings() -> list[dict]:
    """Return every PMbot daily-report folder binding stored in the environment."""
    return [
        {
            "env_key": key,
            "open_dept_id": _from_daily_report_env_key(key),
            "folder_token": val,
        }
        for key, val in os.environ.items()
        if key.startswith(_DEPT_DAILY_REPORT_FOLDER_PREFIX) and val
    ]


# ── Dev display ──────────────────────────────────────────────────────────────

def dump_dept_bindings(org_tree: list[dict], hierarchy=None) -> str:
    """Format a hierarchy-aware weekly/daily binding diagnostic.

    ``hierarchy`` is ContactService's cached OrgHierarchySnapshot.  The flat
    fallback keeps this developer command useful before a new cache refresh.
    """
    if hierarchy:
        nodes = hierarchy.nodes_by_department_id
        root_ids = hierarchy.root_department_ids
    else:
        nodes = {}
        root_ids = []
        for entry in org_tree:
            dept = entry["dept"]
            department_id = getattr(dept, "open_department_id", "") or ""
            if department_id:
                nodes[department_id] = entry
                root_ids.append(department_id)

    all_ids = set(nodes)
    weekly_bound = sum(bool(get_dept_folder_token(dept_id)) for dept_id in all_ids)
    daily_bound = sum(bool(get_dept_daily_report_folder_token(dept_id)) for dept_id in all_ids)
    total = len(all_ids)
    lines = [
        "══ 部门文件夹绑定状态（组织树）══",
        f"部门: {total} | 周报: {weekly_bound}/{total} | 日报: {daily_bound}/{total}",
        "图例: WR=周报文件夹 | DR=PMbot 日报文件夹",
        "",
    ]

    def _entry_parts(department_id: str):
        node = nodes[department_id]
        if hierarchy:
            return node.dept, node.manager, node.child_department_ids
        return node["dept"], node.get("manager"), ()

    def _binding_text(token: str | None) -> str:
        return f"✅ {token}" if token else "❌ 未绑定"

    visited: set[str] = set()

    def _append_branch(department_id: str, prefix: str, is_last: bool) -> None:
        if department_id in visited:
            lines.append(f"{prefix}{'└─' if is_last else '├─'} [循环引用] {department_id}")
            return
        visited.add(department_id)
        dept, manager, child_ids = _entry_parts(department_id)
        name = getattr(dept, "name", "") or department_id
        manager_name = getattr(manager, "name", "") or "未设置"
        weekly = _binding_text(get_dept_folder_token(department_id))
        daily = _binding_text(get_dept_daily_report_folder_token(department_id))
        connector = "└─" if is_last else "├─"
        lines.append(
            f"{prefix}{connector} {name}  [负责人: {manager_name}]\n"
            f"{prefix}{'   ' if is_last else '│  '}   WR {weekly} | DR {daily}"
        )
        child_prefix = prefix + ("   " if is_last else "│  ")
        for index, child_id in enumerate(child_ids):
            _append_branch(child_id, child_prefix, index == len(child_ids) - 1)

    for index, department_id in enumerate(root_ids):
        _append_branch(department_id, "", index == len(root_ids) - 1)

    # Defensive fallback: show any node omitted by malformed parent data.
    for department_id in all_ids - visited:
        _append_branch(department_id, "", True)

    weekly_orphans = [
        binding for binding in list_all_dept_bindings()
        if binding["open_dept_id"] not in all_ids
    ]
    daily_orphans = [
        binding for binding in list_all_dept_daily_report_bindings()
        if binding["open_dept_id"] not in all_ids
    ]
    if weekly_orphans or daily_orphans:
        lines.extend(["", "── 孤立绑定（不在当前通讯录）──"])
        for binding in weekly_orphans:
            lines.append(f"WR {binding['open_dept_id']} → {binding['folder_token']}")
        for binding in daily_orphans:
            lines.append(f"DR {binding['open_dept_id']} → {binding['folder_token']}")

    if not all_ids:
        lines.append("通讯录缓存尚未加载，无法交叉验证。")
    return "\n".join(lines)
