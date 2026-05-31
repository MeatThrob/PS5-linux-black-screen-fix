"""
monitor_history.py — MeatHandler persistent monitor history

Records every monitor seen during PS5 Linux boot so the GUI can offer a
"select from previously connected monitors" list. Storage is JSON, atomic
writes, with a /var fallback to per-user config when not root.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SYSTEM_PATH = Path("/var/lib/meathandler/history.json")
_USER_PATH = Path.home() / ".config" / "meathandler" / "history.json"


def _history_path() -> Path:
    """Return /var path if writable (or creatable), else per-user fallback."""
    try:
        _SYSTEM_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Test writability by touching the dir
        probe = _SYSTEM_PATH.parent / ".write_probe"
        probe.write_text("")
        probe.unlink()
        return _SYSTEM_PATH
    except (PermissionError, OSError):
        _USER_PATH.parent.mkdir(parents=True, exist_ok=True)
        return _USER_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load() -> list[dict[str, Any]]:
    path = _history_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _save_atomic(entries: list[dict[str, Any]]) -> None:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".history.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_boot(mfr_code: str, product_id: int, model_name: str,
                edid_raw_bytes: bytes, db_match: dict | None = None) -> dict:
    """Upsert a monitor entry by (mfr_code, product_id). Returns the entry."""
    mfr_code = (mfr_code or "").upper().strip()
    product_id = int(product_id)
    model_name = (model_name or "").strip()
    edid_sha = hashlib.sha256(edid_raw_bytes).hexdigest()
    now = _now_iso()

    entries = _load()
    for entry in entries:
        if entry.get("mfr_code") == mfr_code and entry.get("product_id") == product_id:
            entry["last_seen"] = now
            entry["boot_count"] = int(entry.get("boot_count", 0)) + 1
            entry["model_name"] = model_name or entry.get("model_name", "")
            entry["edid_sha256"] = edid_sha
            entry["edid_raw_hex"] = edid_raw_bytes.hex()
            if db_match is not None:
                entry["db_match"] = db_match
            _save_atomic(entries)
            return entry

    entry = {
        "first_seen": now,
        "last_seen": now,
        "boot_count": 1,
        "mfr_code": mfr_code,
        "product_id": product_id,
        "model_name": model_name,
        "edid_sha256": edid_sha,
        "edid_raw_hex": edid_raw_bytes.hex(),
        "db_match": db_match or {},
    }
    entries.append(entry)
    _save_atomic(entries)
    return entry


def get_history() -> list[dict]:
    """Return all entries sorted by last_seen descending (most recent first)."""
    entries = _load()
    entries.sort(key=lambda e: e.get("last_seen", ""), reverse=True)
    return entries


def find_entry(mfr_code: str, product_id: int) -> dict | None:
    mfr_code = (mfr_code or "").upper().strip()
    product_id = int(product_id)
    for entry in _load():
        if entry.get("mfr_code") == mfr_code and entry.get("product_id") == product_id:
            return entry
    return None


def find_by_edid_sha(sha: str) -> dict | None:
    sha = (sha or "").lower()
    for entry in _load():
        if entry.get("edid_sha256", "").lower() == sha:
            return entry
    return None


def delete_entry(mfr_code: str, product_id: int) -> bool:
    mfr_code = (mfr_code or "").upper().strip()
    product_id = int(product_id)
    entries = _load()
    new_entries = [
        e for e in entries
        if not (e.get("mfr_code") == mfr_code and e.get("product_id") == product_id)
    ]
    if len(new_entries) == len(entries):
        return False
    _save_atomic(new_entries)
    return True


def prune(max_entries: int = 20) -> None:
    entries = _load()
    if len(entries) <= max_entries:
        return
    entries.sort(key=lambda e: e.get("last_seen", ""), reverse=True)
    _save_atomic(entries[:max_entries])
