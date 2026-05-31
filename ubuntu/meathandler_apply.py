"""
meathandler_apply.py — MeatHandler apply backend (called by the GUI)

Synthesizes an EDID for a history entry (optionally with a different preferred
mode), writes it to the firmware paths the kernel reads, triggers a DRM
re-detect, and bounces the VT to force a full modeset. The GUI calls this on
a worker thread.

Wayland-native: on GNOME 50 / mutter (Ubuntu 26.04), display configuration is
owned by the compositor; there is no X11-style mode-set hook. Mode selection is
driven entirely by what the EDID exposes — the user's desired mode is baked
into DTD slot 0 (the preferred-mode slot) via edid_synth, and mutter picks
that as the active mode after re-probing DRM. After the VT bounce we nudge
mutter with a best-effort D-Bus GetCurrentState call so GNOME Settings sees
the new mode list immediately.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import edid_synth  # type: ignore[no-redef]
except Exception:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import edid_synth  # type: ignore
    except Exception:
        edid_synth = None  # GUI will surface the error


_FW_PATHS = [
    Path("/run/firmware/edid/auto.bin"),
    Path("/lib/firmware/edid/auto.bin"),
    Path("/boot/efi/edid/auto.bin"),
]
_DRM_CONNECTOR_STATUS = Path("/sys/class/drm/card0-DP-1/status")
_DRM_CONNECTOR_FORCE = Path("/sys/class/drm/card0-DP-1/force")
_DRM_CONNECTOR_EDID = Path("/sys/class/drm/card0-DP-1/edid")

_SYSTEM_LOG = Path("/var/log/meathandler-apply.log")
_USER_LOG = Path.home() / ".local" / "share" / "meathandler" / "apply.log"

_MUTTER_DEST = "org.gnome.Mutter.DisplayConfig"
_MUTTER_PATH = "/org/gnome/Mutter/DisplayConfig"
_MUTTER_METHOD_GET = "org.gnome.Mutter.DisplayConfig.GetCurrentState"


def _log_path() -> Path:
    try:
        _SYSTEM_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _SYSTEM_LOG.open("a", encoding="utf-8") as f:
            f.write("")
        return _SYSTEM_LOG
    except (PermissionError, OSError):
        _USER_LOG.parent.mkdir(parents=True, exist_ok=True)
        return _USER_LOG


def _log_line(buf: list[str], msg: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{stamp}] {msg}"
    buf.append(line)
    try:
        with _log_path().open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _atomic_write_bytes(path: Path, data: bytes, log: list[str]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".auto.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            _log_line(log, f"wrote {path} ({len(data)} bytes)")
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except PermissionError as e:
        _log_line(log, f"PERMISSION DENIED writing {path}: {e}")
        return False
    except OSError as e:
        _log_line(log, f"ERROR writing {path}: {e}")
        return False


def _run(cmd: list[str], log: list[str], timeout: float = 5.0,
         env: dict[str, str] | None = None) -> int:
    """Run a command (no shell), log it, return exit code (-1 on exception)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        _log_line(log, f"$ {' '.join(cmd)} -> rc={result.returncode}")
        if result.stdout.strip():
            _log_line(log, f"  stdout: {result.stdout.strip()[:200]}")
        if result.stderr.strip():
            _log_line(log, f"  stderr: {result.stderr.strip()[:200]}")
        return result.returncode
    except FileNotFoundError:
        _log_line(log, f"$ {cmd[0]} not found, skipping")
        return -1
    except subprocess.TimeoutExpired:
        _log_line(log, f"$ {' '.join(cmd)} TIMEOUT")
        return -1
    except Exception as e:
        _log_line(log, f"$ {' '.join(cmd)} ERROR {e}")
        return -1


def _trigger_drm_redetect(log: list[str]) -> None:
    written = False
    if _DRM_CONNECTOR_STATUS.exists():
        try:
            _DRM_CONNECTOR_STATUS.write_text("detect\n")
            _log_line(log, f"wrote 'detect' to {_DRM_CONNECTOR_STATUS}")
            written = True
        except OSError as e:
            _log_line(log, f"could not write {_DRM_CONNECTOR_STATUS}: {e}")
    if not written and _DRM_CONNECTOR_FORCE.exists():
        try:
            _DRM_CONNECTOR_FORCE.write_text("detect\n")
            _log_line(log, f"wrote 'detect' to {_DRM_CONNECTOR_FORCE}")
        except OSError as e:
            _log_line(log, f"could not write {_DRM_CONNECTOR_FORCE}: {e}")
    _run(["udevadm", "trigger", "--subsystem-match=drm", "--action=change"], log)


def _vt_bounce(log: list[str]) -> None:
    if not Path("/dev/tty1").exists():
        _log_line(log, "no /dev/tty1, skipping VT bounce")
        return
    _run(["chvt", "1"], log)
    _run(["sleep", "0.5"], log)
    _run(["chvt", "7"], log)


def _resolve_session_user() -> tuple[str | None, int | None]:
    """If running under sudo/pkexec, return (username, uid) of the original
    user whose session bus we want to address. Otherwise return (None, None)."""
    name = os.environ.get("SUDO_USER") or os.environ.get("PKEXEC_USER")
    if not name:
        return None, None
    uid_str = os.environ.get("SUDO_UID") or os.environ.get("PKEXEC_UID")
    try:
        uid = int(uid_str) if uid_str else None
    except ValueError:
        uid = None
    if uid is None:
        try:
            import pwd  # stdlib, kept local to avoid import-time fail on minimal builds
            uid = pwd.getpwnam(name).pw_uid
        except Exception:
            uid = None
    return name, uid


def _notify_mutter_redetect(log: list[str]) -> None:
    """Best-effort: nudge mutter to re-read DRM state so GNOME Settings sees
    the new mode list immediately. Never raises; logs the outcome.

    On Wayland the compositor owns display state. A GetCurrentState call on
    org.gnome.Mutter.DisplayConfig forces mutter to refresh its view. We do
    NOT issue an ApplyMonitorsConfig because:
      (a) the chosen mode is already baked into DTD slot 0 of the EDID we
          just wrote, so mutter will pick it as the preferred mode anyway;
      (b) ApplyMonitorsConfig needs a fresh `serial` from GetCurrentState
          and a mode_id string whose exact format mutter auto-derives from
          the EDID — adding fragility for no benefit.
    """
    base_cmd = [
        "gdbus", "call", "--session",
        "--dest", _MUTTER_DEST,
        "--object-path", _MUTTER_PATH,
        "--method", _MUTTER_METHOD_GET,
    ]

    sudo_user, sudo_uid = _resolve_session_user()

    if sudo_user is None:
        # Already running as the desktop user — just call.
        _run(base_cmd, log, timeout=4.0)
        return

    # Running as root via sudo/pkexec. Try the cleanest path first: hand the
    # call back to the original user via runuser, which restores their HOME
    # and login env so gdbus can find the session bus.
    runuser_env = dict(os.environ)
    if sudo_uid is not None:
        runuser_env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{sudo_uid}")
        runuser_env.setdefault(
            "DBUS_SESSION_BUS_ADDRESS",
            f"unix:path=/run/user/{sudo_uid}/bus",
        )

    rc = _run(
        ["runuser", "-u", sudo_user, "--"] + base_cmd,
        log,
        timeout=4.0,
        env=runuser_env,
    )
    if rc == 0:
        return

    # Fallback: try the call directly with the synthesised session bus
    # address. Works on systems where /run/user/<uid>/bus is the standard
    # session bus socket (default on modern systemd-logind setups).
    if sudo_uid is None:
        _log_line(log, "mutter notify fallback skipped: no uid resolved")
        return
    direct_env = dict(os.environ)
    direct_env["XDG_RUNTIME_DIR"] = f"/run/user/{sudo_uid}"
    direct_env["DBUS_SESSION_BUS_ADDRESS"] = (
        f"unix:path=/run/user/{sudo_uid}/bus"
    )
    _run(base_cmd, log, timeout=4.0, env=direct_env)


def _needs_resynth(entry: dict, desired_mode: dict | None) -> bool:
    """Return True if the stored EDID's preferred mode differs from desired."""
    if desired_mode is None:
        return False
    raw_hex = entry.get("edid_raw_hex", "")
    if not raw_hex:
        return True
    try:
        raw = bytes.fromhex(raw_hex)
        if len(raw) < 72:
            return True
        # DTD slot 0 at offset 54
        pclk = int.from_bytes(raw[54:56], "little") * 10
        h_act = raw[56] | ((raw[58] & 0xF0) << 4)
        v_act = raw[59] | ((raw[61] & 0xF0) << 4)
        return not (h_act == desired_mode["width"]
                    and v_act == desired_mode["height"]
                    and pclk == desired_mode["pclk_khz"])
    except (ValueError, KeyError):
        return True


def apply_monitor(history_entry: dict, desired_mode: dict | None = None) -> dict:
    """Apply a monitor's EDID and optional preferred-mode override.

    Wayland-native flow:
      1. Synthesize a fresh EDID with the desired mode in DTD slot 0
         (preferred), or reuse the stored EDID if it already matches.
      2. Atomically write the EDID to the firmware paths the kernel reads.
      3. Trigger a DRM re-detect on card0-DP-1 + udevadm change event.
      4. VT bounce (chvt 1 -> sleep -> chvt 7) to force a full modeset.
      5. Best-effort D-Bus nudge so mutter re-reads display state.

    Mode application is implicit: mutter picks DTD slot 0 (preferred mode)
    as the active mode after the redetect, so the user's desired mode wins
    purely by virtue of where edid_synth placed it.

    Returns:
        {"success": bool, "edid_sha": str, "applied_mode": str | None, "log": list[str]}
    """
    log: list[str] = []
    _log_line(log, f"=== apply_monitor: {history_entry.get('model_name','?')} "
                   f"({history_entry.get('mfr_code')}/{history_entry.get('product_id')}) ===")

    if desired_mode:
        _log_line(log, f"desired_mode: {desired_mode.get('label')} "
                       f"({desired_mode.get('width')}x{desired_mode.get('height')}@{desired_mode.get('rate')})")

    if _needs_resynth(history_entry, desired_mode):
        if edid_synth is None:
            _log_line(log, "FAIL: edid_synth module not available — cannot re-synth EDID")
            return {"success": False, "edid_sha": "", "applied_mode": None, "log": log}
        try:
            edid_bytes = edid_synth.synthesize_edid(
                history_entry.get("mfr_code", ""),
                int(history_entry.get("product_id", 0)),
                history_entry.get("model_name", "Monitor"),
                history_entry.get("db_match", {}),
            )
            _log_line(log, f"synthesized fresh EDID ({len(edid_bytes)} bytes)")
        except Exception as e:
            _log_line(log, f"FAIL: edid_synth error: {e}")
            return {"success": False, "edid_sha": "", "applied_mode": None, "log": log}
    else:
        raw_hex = history_entry.get("edid_raw_hex", "")
        if not raw_hex:
            _log_line(log, "FAIL: history entry has no edid_raw_hex and no resynth needed")
            return {"success": False, "edid_sha": "", "applied_mode": None, "log": log}
        try:
            edid_bytes = bytes.fromhex(raw_hex)
        except ValueError as e:
            _log_line(log, f"FAIL: edid_raw_hex decode error: {e}")
            return {"success": False, "edid_sha": "", "applied_mode": None, "log": log}
        _log_line(log, f"reusing stored EDID ({len(edid_bytes)} bytes)")

    edid_sha = hashlib.sha256(edid_bytes).hexdigest()
    _log_line(log, f"edid sha256: {edid_sha[:16]}...")

    wrote_any = False
    for path in _FW_PATHS:
        if _atomic_write_bytes(path, edid_bytes, log):
            wrote_any = True

    if not wrote_any:
        _log_line(log, "FAIL: could not write EDID to any firmware path (need root?)")
        return {"success": False, "edid_sha": edid_sha, "applied_mode": None, "log": log}

    _trigger_drm_redetect(log)
    _vt_bounce(log)
    _notify_mutter_redetect(log)

    # On Wayland we don't directly set a mode; mutter picks DTD0 as the
    # active mode. Report what we asked for so the GUI can display it.
    applied_mode: str | None = None
    if desired_mode:
        try:
            applied_mode = (
                f"{int(desired_mode['width'])}x{int(desired_mode['height'])}"
                f"@{int(desired_mode['rate'])}"
            )
        except (KeyError, ValueError, TypeError) as e:
            _log_line(log, f"applied_mode label skipped: {e}")

    _log_line(log, "=== apply_monitor complete ===")
    return {
        "success": True,
        "edid_sha": edid_sha,
        "applied_mode": applied_mode,
        "log": log,
    }
