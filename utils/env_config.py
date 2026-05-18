#=====================================================
#  utils/env_config.py — Department Folder Binding Config
#=====================================================
#
#  Responsibilities:
#    - Read / write per-department Feishu folder tokens to .env
#    - Extract folder token from a Feishu folder URL
#    - Dump all bindings cross-referenced against org tree (dev check)
#
#  Key format in .env:
#    DEPT_FOLDER_{open_department_id with hyphens → underscores}
#    e.g.  DEPT_FOLDER_od_c5a40c187b6a50163de9c30b4dbe84b4=fldcnXXX
#
#  Does NOT contain:
#    - Card logic
#    - Contact API calls
#    - Any AstrBot imports
#=====================================================

import os
import re
from pathlib import Path

from dotenv import set_key

_ENV_PATH = Path(__file__).parent.parent / ".env"
_DEPT_FOLDER_PREFIX = "DEPT_FOLDER_"


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
