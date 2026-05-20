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


# ── Public API ───────────────────────────────────────────────────────────────

def get_dept_folder_token(open_dept_id: str) -> str | None:
    """Return the stored folder token for a department, or None if unset."""
    return os.getenv(_to_env_key(open_dept_id)) or None


def set_dept_folder_token(open_dept_id: str, token: str) -> None:
    """Persist a folder token for a department to .env and apply to current process."""
    key = _to_env_key(open_dept_id)
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


# ── Dev display ──────────────────────────────────────────────────────────────

def dump_dept_bindings(org_tree: list[dict]) -> str:
    """Format all dept→folder bindings cross-referenced with org tree names.

    Intended for developer terminal checks via bot command.
    org_tree: output of ContactService.get_cached_org_tree()
    """
    dept_name_map: dict[str, str] = {}
    for entry in org_tree:
        dept = entry["dept"]
        open_id = getattr(dept, "open_department_id", "") or ""
        if open_id:
            dept_name_map[open_id] = dept.name or open_id

    lines = ["══ 部门文件夹绑定状态 ══", ""]

    bound = list_all_dept_bindings()
    bound_ids = {b["open_dept_id"] for b in bound}

    for entry in org_tree:
        dept = entry["dept"]
        open_id = getattr(dept, "open_department_id", "") or ""
        token = get_dept_folder_token(open_id)
        status = f"✅ {token}" if token else "❌ 未绑定"
        lines.append(f"{dept.name:<20s}  →  {status}")

    orphans = [b for b in bound if b["open_dept_id"] not in dept_name_map]
    if orphans:
        lines.append("")
        lines.append("── 孤立绑定（部门已不在通讯录中）──")
        for b in orphans:
            lines.append(f"{b['open_dept_id']}  →  {b['folder_token']}")

    if not org_tree:
        lines.append("通讯录缓存尚未加载，无法交叉验证。")

    return "\n".join(lines)
