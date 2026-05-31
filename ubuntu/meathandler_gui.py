#!/usr/bin/env python3
"""
MeatHandler GUI - PS5 Display Switcher
GTK3 single-window application for managing previously-connected PS5 displays.
Launches from the Linux app menu next to system Display settings.
"""

import os
import sys
import hashlib
import threading
import traceback
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# GTK3 import guard
# ---------------------------------------------------------------------------
try:
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, GLib, GObject, Gdk
except (ImportError, ValueError) as e:
    print("MeatHandler GUI requires PyGObject + GTK3:")
    print("  sudo apt install python3-gi gir1.2-gtk-3.0")
    raise SystemExit(1)

# ---------------------------------------------------------------------------
# Backend module imports - wrapped so the GUI still parses if modules missing.
# Failures surface in the window at runtime, not at parse time.
# ---------------------------------------------------------------------------
_BACKEND_ERROR = None
monitor_history = None
edid_synth = None
meathandler_apply = None
try:
    # Ensure the script's own dir is on sys.path so sibling modules import.
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import monitor_history  # noqa: E402
    import edid_synth  # noqa: E402
    import meathandler_apply  # noqa: E402
except Exception as e:  # broad on purpose - we want to display ANY failure
    _BACKEND_ERROR = "Backend import failed:\n{}\n\n{}".format(
        e, traceback.format_exc()
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EDID_SYSFS_PATH = "/sys/class/drm/card0-DP-1/edid"
WINDOW_TITLE = "MeatHandler — PS5 Display"
REVERT_COUNTDOWN_SECONDS = 30
DEFAULT_FALLBACK_MODE = "1080p@60"

# ---- Screen Dim (Xbox/Kodi-style overlay) ----
def _session_user_home():
    """Home dir of the user who owns the desktop session. The GUI runs as root
    via pkexec, so we resolve the *invoking* user (PKEXEC_UID/SUDO_UID) rather
    than using root's home for the dim config the user's daemon reads."""
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if uid is not None:
        try:
            import pwd
            return pwd.getpwuid(int(uid)).pw_dir
        except Exception:
            pass
    return os.path.expanduser("~")


DIM_CONFIG_DIR = os.path.join(_session_user_home(), ".config", "meathandler")
DIM_CONFIG_PATH = os.path.join(DIM_CONFIG_DIR, "dim.conf")
DIM_SERVICE = "meathandler-dim.service"
# Failsafe: the slider runs 0..100 %, but 100 % maps to only this much actual
# darkness so the screen NEVER goes fully black (you can always see to undim).
DIM_MAX_DARKNESS = 0.90


def slider_to_darkness(pct):
    """Slider percent (0..100) -> actual darkness (0..DIM_MAX_DARKNESS)."""
    return max(0.0, min(100.0, pct)) / 100.0 * DIM_MAX_DARKNESS


def darkness_to_slider(darkness):
    """Actual darkness -> slider percent (0..100)."""
    if DIM_MAX_DARKNESS <= 0:
        return 0
    return int(round(max(0.0, min(DIM_MAX_DARKNESS, darkness)) / DIM_MAX_DARKNESS * 100))


# (label, seconds) for the idle-timeout dropdown.
DIM_IDLE_CHOICES = [
    ("30 seconds", 30), ("1 minute", 60), ("5 minutes", 300),
    ("10 minutes", 600), ("15 minutes", 900), ("25 minutes", 1500),
    ("30 minutes", 1800), ("1 hour", 3600),
]
DIM_DEFAULTS = {"enabled": True, "darkness": 0.70, "idle_seconds": 30}

CSS = b"""
.active-badge {
    background: #2e7d32;
    color: white;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: bold;
}
.spec-line {
    color: alpha(currentColor, 0.55);
    font-size: 11px;
}
.empty-state {
    color: alpha(currentColor, 0.6);
    font-size: 13px;
}
.mono-log {
    font-family: monospace;
    font-size: 11px;
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _install_css():
    """Inject the small CSS stylesheet into the default screen."""
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


def _read_active_edid_sha256():
    """Read the live EDID from sysfs and return its sha256, or None."""
    try:
        with open(EDID_SYSFS_PATH, "rb") as f:
            data = f.read()
        if not data:
            return None
        return hashlib.sha256(data).hexdigest()
    except Exception:
        return None


def _normalize_history_entry(entry):
    """Map a monitor_history schema entry to the shape the GUI reads.

    monitor_history stores: model_name, last_seen, mfr_code, product_id,
    edid_sha256, db_match. The GUI's row/mode code reads: display_name,
    last_used, preferred_mode. Bridge them here (non-destructively) so a
    correctly-read EDID name like '43S310R' renders instead of "Unknown
    monitor". Falls back to "<MFR> <name>" / "<MFR> 0x<pid>" when the
    0xFC descriptor is blank, so even monitors absent from the DB still
    show a meaningful label.
    """
    e = dict(entry)  # shallow copy; never mutate the stored history dict

    model_name = (e.get("model_name") or "").strip()
    mfr = (e.get("mfr_code") or "").strip()
    if not e.get("display_name"):
        if model_name and mfr:
            e["display_name"] = "{} {}".format(mfr, model_name)
        elif model_name:
            e["display_name"] = model_name
        elif mfr:
            pid = e.get("product_id")
            e["display_name"] = "{} 0x{:04X}".format(mfr, int(pid)) \
                if isinstance(pid, int) else mfr
        # else: leave unset -> caller's "Unknown monitor" fallback

    # Timestamp key bridge
    if not e.get("last_used") and e.get("last_seen"):
        e["last_used"] = e["last_seen"]

    # Preferred-mode bridge: prefer an explicit field, else the DB match's
    # preferred/best mode if present (soft default only; modes list is
    # derived independently from db_match in _populate_modes_for).
    if not e.get("preferred_mode"):
        dbm = e.get("db_match") or {}
        if isinstance(dbm, dict):
            e["preferred_mode"] = dbm.get("preferred_mode") or dbm.get("best_mode")

    return e


def _humanize_last_used(iso_ts):
    """Render an ISO timestamp as 'today' / 'N days ago' / fallback."""
    if not iso_ts:
        return "never"
    try:
        # Accept both naive and aware ISO strings
        if iso_ts.endswith("Z"):
            iso_ts = iso_ts[:-1] + "+00:00"
        ts = datetime.fromisoformat(iso_ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - ts
        days = delta.days
        if days <= 0:
            return "today"
        if days == 1:
            return "yesterday"
        return "{} days ago".format(days)
    except Exception:
        return str(iso_ts)


def _mode_label(mode_dict_or_str):
    """Normalize a mode entry to a label like '1080p @ 120 Hz'."""
    if isinstance(mode_dict_or_str, str):
        # Already a label-ish string, try to prettify "1080p@120"
        s = mode_dict_or_str.strip()
        if "@" in s and "Hz" not in s:
            res, hz = s.split("@", 1)
            return "{} @ {} Hz".format(res.strip(), hz.strip())
        return s
    if isinstance(mode_dict_or_str, dict):
        # Common shapes: {"resolution":"1080p","refresh":120} or
        # {"width":1920,"height":1080,"refresh":120}
        if "label" in mode_dict_or_str:
            return mode_dict_or_str["label"]
        res = mode_dict_or_str.get("resolution")
        if not res:
            w = mode_dict_or_str.get("width")
            h = mode_dict_or_str.get("height")
            if w and h:
                res = "{}x{}".format(w, h)
        hz = (mode_dict_or_str.get("refresh")
              or mode_dict_or_str.get("refresh_hz")
              or mode_dict_or_str.get("hz"))
        if res and hz:
            return "{} @ {} Hz".format(res, hz)
        return str(mode_dict_or_str)
    return str(mode_dict_or_str)


def _mode_key(mode_dict_or_str):
    """Return a stable key like '1080p@120' for matching preferred modes."""
    if isinstance(mode_dict_or_str, str):
        return mode_dict_or_str.replace(" ", "").replace("Hz", "")
    if isinstance(mode_dict_or_str, dict):
        if "key" in mode_dict_or_str:
            return mode_dict_or_str["key"]
        res = mode_dict_or_str.get("resolution")
        if not res:
            w = mode_dict_or_str.get("width")
            h = mode_dict_or_str.get("height")
            if w and h:
                res = "{}x{}".format(w, h)
        hz = (mode_dict_or_str.get("refresh")
              or mode_dict_or_str.get("refresh_hz")
              or mode_dict_or_str.get("hz"))
        if res and hz:
            return "{}@{}".format(res, hz)
    return str(mode_dict_or_str)


# ---------------------------------------------------------------------------
# Screen Dim helpers (overlay daemon config + service control + live preview)
# ---------------------------------------------------------------------------
def dim_read_config():
    """Return the current dim settings dict, falling back to defaults."""
    import configparser
    cfg = dict(DIM_DEFAULTS)
    cp = configparser.ConfigParser()
    try:
        cp.read(DIM_CONFIG_PATH)
        if cp.has_section("dim"):
            cfg["enabled"] = cp.getboolean("dim", "enabled", fallback=cfg["enabled"])
            d = cp.getfloat("dim", "darkness", fallback=cfg["darkness"])
            cfg["darkness"] = max(0.0, min(DIM_MAX_DARKNESS, d))
            cfg["idle_seconds"] = max(5, cp.getint("dim", "idle_seconds",
                                                   fallback=cfg["idle_seconds"]))
    except Exception:
        pass
    return cfg


def dim_write_config(cfg):
    """Persist the dim settings dict to ~/.config/meathandler/dim.conf."""
    import configparser
    os.makedirs(DIM_CONFIG_DIR, exist_ok=True)
    cp = configparser.ConfigParser()
    cp["dim"] = {
        "enabled": "true" if cfg["enabled"] else "false",
        "darkness": "{:.3f}".format(cfg["darkness"]),
        "idle_seconds": str(int(cfg["idle_seconds"])),
    }
    with open(DIM_CONFIG_PATH, "w") as f:
        cp.write(f)
    # If we wrote this as root (pkexec), hand ownership to the session user so
    # their dim daemon can read it and the file-monitor fires for them.
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if uid is not None and os.getuid() == 0:
        try:
            import pwd
            gid = pwd.getpwuid(int(uid)).pw_gid
            os.chown(DIM_CONFIG_DIR, int(uid), gid)
            os.chown(DIM_CONFIG_PATH, int(uid), gid)
        except Exception:
            pass


def _dim_target_user():
    """Return (uid, runtime_dir, bus_addr) for the user whose session owns the
    graphical display. The GUI runs as root via pkexec, so the dim daemon and
    its session bus live under the *invoking* user, not root. The pkexec/sudo
    launcher exports the original ids and the session bus address.
    """
    uid = os.environ.get("PKEXEC_UID") or os.environ.get("SUDO_UID")
    if uid is None:
        uid = str(os.getuid())
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/{}".format(uid)
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS") \
        or "unix:path={}/bus".format(runtime)
    return uid, runtime, bus


def dim_service_action(action):
    """Run `systemctl --user <action> meathandler-dim.service` as the session
    user, even when the GUI itself is running as root via pkexec."""
    import subprocess
    uid, runtime, bus = _dim_target_user()
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = runtime
    env["DBUS_SESSION_BUS_ADDRESS"] = bus
    cmd = ["systemctl", "--user", action, DIM_SERVICE]
    # If we're root but the session belongs to another uid, hop to it so the
    # --user manager we talk to is the user's, not root's.
    if os.getuid() == 0 and str(uid) != "0":
        cmd = ["setpriv", "--reuid", str(uid), "--regid", str(uid),
               "--init-groups", "--"] + cmd
    try:
        subprocess.run(cmd, check=False, env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=8)
    except Exception:
        pass


def _dim_bus():
    """Return a session-bus connection that can reach the user's dim daemon.

    Normal case (GUI runs as the desktop user): use this process's own session
    bus. Root case (GUI launched via pkexec): open the *user's* session bus by
    its address. Cached after first success.
    """
    from gi.repository import Gio
    if getattr(_dim_bus, "_cached", None) is not None:
        return _dim_bus._cached
    conn = None
    # 1) Our own session bus (works whenever DBUS_SESSION_BUS_ADDRESS points at
    #    the daemon's bus -- i.e. GUI running as the desktop user).
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception:
        conn = None
    # 2) Fallback: connect to the target user's bus address explicitly.
    if conn is None:
        try:
            _, _, bus_addr = _dim_target_user()
            conn = Gio.DBusConnection.new_for_address_sync(
                bus_addr,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, None)
        except Exception:
            conn = None
    _dim_bus._cached = conn
    return conn


def dim_daemon_call(method, *dbus_args):
    """Call a method on the running dim daemon (live preview). Best-effort.

    CRITICAL: the GUI runs as ROOT via pkexec, but the dim daemon owns its
    name on the *invoking user's* session bus. D-Bus EXTERNAL auth requires a
    matching uid, so a root process connecting to the user's bus is rejected
    ("The connection is closed") — which silently killed live preview. We
    therefore make the call as the target user via `setpriv` + `gdbus`,
    exactly like dim_service_action(). When already running as that user we
    just call gdbus directly.

    dbus_args are passed through to `gdbus call` as already-formatted strings
    (e.g. "0.45" for a double). No args for PreviewEnd/Reload.
    """
    import subprocess
    uid, runtime, bus = _dim_target_user()
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = runtime
    env["DBUS_SESSION_BUS_ADDRESS"] = bus
    cmd = [
        "gdbus", "call", "--session",
        "--dest", "org.meathandler.Dim",
        "--object-path", "/org/meathandler/Dim",
        "--method", "org.meathandler.Dim.{}".format(method),
    ] + [str(a) for a in dbus_args]
    # Drop from root to the session user so EXTERNAL auth on their bus succeeds.
    if os.getuid() == 0 and str(uid) != "0":
        cmd = ["setpriv", "--reuid", str(uid), "--regid", str(uid),
               "--init-groups", "--"] + cmd
    try:
        r = subprocess.run(cmd, check=False, env=env, timeout=4,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0 and os.environ.get("MEATHANDLER_DEBUG"):
            print("dim_daemon_call({}) rc={} err={}".format(
                method, r.returncode,
                (r.stderr or b"").decode("utf-8", "replace").strip()),
                flush=True)
    except Exception as e:
        if os.environ.get("MEATHANDLER_DEBUG"):
            print("dim_daemon_call({}) failed: {}".format(method, e), flush=True)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MeatHandlerWindow(Gtk.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app, title=WINDOW_TITLE)
        self.set_default_size(560, 680)
        self.set_resizable(True)

        # State
        self.entries = []                 # list of history dicts
        self.active_sha256 = None
        self.active_entry = None          # currently lit-up monitor (pre-apply)
        self.selected_entry = None        # currently highlighted row
        self.current_modes = []           # modes for selected entry
        self.mode_buttons = []            # list of (RadioButton, mode)
        self.revert_timer_id = None
        self.revert_seconds_left = 0
        self.pending_revert_entry = None  # previously active, to revert to
        self.in_apply = False

        # Header bar
        self.headerbar = Gtk.HeaderBar()
        self.headerbar.set_show_close_button(True)
        self.headerbar.set_title("Displays")
        self.headerbar.set_subtitle("PS5 Display Switcher")
        self.set_titlebar(self.headerbar)

        self.spinner = Gtk.Spinner()
        self.headerbar.pack_end(self.spinner)

        # Root vertical layout
        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.root.set_border_width(16)
        self.add(self.root)

        # Backend-error InfoBar at top (only shown if backend failed)
        if _BACKEND_ERROR is not None:
            self._add_backend_error_banner()

        # InfoBar slot (for apply success / revert countdown)
        self.info_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.root.pack_start(self.info_slot, False, False, 0)

        # ---- Your Monitors section ----
        monitors_heading = Gtk.Label(xalign=0)
        monitors_heading.set_markup("<b>Your Monitors</b>")
        self.root.pack_start(monitors_heading, False, False, 0)

        self.monitors_frame = Gtk.Frame()
        self.monitors_frame.set_shadow_type(Gtk.ShadowType.IN)
        self.root.pack_start(self.monitors_frame, True, True, 0)

        self.monitors_scroller = Gtk.ScrolledWindow()
        self.monitors_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.monitors_scroller.set_min_content_height(180)
        self.monitors_frame.add(self.monitors_scroller)

        self.monitors_listbox = Gtk.ListBox()
        self.monitors_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.monitors_listbox.connect("row-selected", self._on_row_selected)
        self.monitors_scroller.add(self.monitors_listbox)

        # ---- Resolution & Refresh section ----
        modes_heading = Gtk.Label(xalign=0)
        modes_heading.set_markup("<b>Resolution &amp; Refresh</b>")
        self.root.pack_start(modes_heading, False, False, 0)

        self.modes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.modes_box.set_margin_start(8)
        self.root.pack_start(self.modes_box, False, False, 0)

        # ---- Screen Dim section ----
        self._build_dim_section()

        # ---- Bottom button row ----
        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.root.pack_end(button_row, False, False, 0)

        self.forget_btn = Gtk.Button(label="Forget Monitor")
        self.forget_btn.connect("clicked", self._on_forget_clicked)
        button_row.pack_start(self.forget_btn, False, False, 0)

        spacer = Gtk.Box()
        button_row.pack_start(spacer, True, True, 0)

        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.connect("clicked", lambda *_: self.close())
        button_row.pack_start(self.cancel_btn, False, False, 0)

        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.get_style_context().add_class("suggested-action")
        self.apply_btn.connect("clicked", self._on_apply_clicked)
        button_row.pack_start(self.apply_btn, False, False, 0)

        # Populate
        self._refresh_monitor_list()

    # -----------------------------------------------------------------------
    # Screen Dim section
    # -----------------------------------------------------------------------
    def _build_dim_section(self):
        cfg = dim_read_config()
        self._dim_loading = True   # suppress handlers during initial set

        heading = Gtk.Label(xalign=0)
        heading.set_markup("<b>Screen Dim</b>")
        heading.set_margin_top(6)
        self.root.pack_start(heading, False, False, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(8)
        self.root.pack_start(box, False, False, 0)

        # Enable toggle
        self.dim_enable = Gtk.CheckButton(
            label="Dim the screen after inactivity (software gamma — no backlight)")
        self.dim_enable.set_active(cfg["enabled"])
        self.dim_enable.connect("toggled", self._on_dim_changed)
        box.pack_start(self.dim_enable, False, False, 0)

        # Darkness slider row
        drow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dlbl = Gtk.Label(label="Darkness", xalign=0)
        dlbl.set_size_request(70, -1)
        drow.pack_start(dlbl, False, False, 0)
        # Slider is a clean 0..100 %. 100 % maps to DIM_MAX_DARKNESS (never full
        # black) via slider_to_darkness(), so it can't black the screen out.
        self.dim_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 5)
        self.dim_scale.set_value(darkness_to_slider(cfg["darkness"]))
        self.dim_scale.set_hexpand(True)
        self.dim_scale.set_draw_value(True)
        self.dim_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self.dim_scale.connect("format-value",
                               lambda s, v: "{:d}%".format(int(v)))
        # Live preview while dragging; commit + end preview on release.
        self.dim_scale.connect("value-changed", self._on_dim_preview)
        self.dim_scale.connect("button-release-event", self._on_dim_preview_end)
        self.dim_scale.connect("key-release-event", self._on_dim_preview_end)
        drow.pack_start(self.dim_scale, True, True, 0)
        box.pack_start(drow, False, False, 0)

        # Idle-timeout dropdown row
        irow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        ilbl = Gtk.Label(label="Dim after", xalign=0)
        ilbl.set_size_request(70, -1)
        irow.pack_start(ilbl, False, False, 0)
        self.dim_idle = Gtk.ComboBoxText()
        active_idx = 0
        for i, (label, secs) in enumerate(DIM_IDLE_CHOICES):
            self.dim_idle.append_text(label)
            if secs == cfg["idle_seconds"]:
                active_idx = i
        self.dim_idle.set_active(active_idx)
        self.dim_idle.connect("changed", self._on_dim_changed)
        irow.pack_start(self.dim_idle, False, False, 0)
        box.pack_start(irow, False, False, 0)

        # Idle dropdown is only meaningful when auto-dim is on; the darkness
        # slider stays usable always so the user can preview a level even with
        # auto-dim off.
        self._dim_widgets = [self.dim_idle]
        self._sync_dim_sensitivity()
        self._dim_loading = False

    def _sync_dim_sensitivity(self):
        on = self.dim_enable.get_active()
        for w in getattr(self, "_dim_widgets", []):
            w.set_sensitive(on)

    def _current_dim_cfg(self):
        idx = self.dim_idle.get_active()
        idx = 0 if idx < 0 else idx
        return {
            "enabled": self.dim_enable.get_active(),
            "darkness": slider_to_darkness(self.dim_scale.get_value()),
            "idle_seconds": DIM_IDLE_CHOICES[idx][1],
        }

    def _commit_dim(self):
        """Persist config and ensure the user dim service matches enabled state.

        The daemon watches the config file and re-applies darkness/idle live, so
        we only need to start it when enabling and stop it when disabling. We
        also nudge it with Reload() so changes apply instantly even if the file
        monitor is slow.
        """
        cfg = self._current_dim_cfg()
        dim_write_config(cfg)
        if cfg["enabled"]:
            dim_service_action("start")
            dim_daemon_call("Reload")
        else:
            dim_service_action("stop")

    def _on_dim_changed(self, *_):
        if getattr(self, "_dim_loading", False):
            return
        self._sync_dim_sensitivity()
        self._commit_dim()

    def _on_dim_preview(self, scale):
        if getattr(self, "_dim_loading", False):
            return
        # Always preview while dragging — even when auto-dim is disabled — so the
        # user can see exactly how dark a given level looks before committing.
        # value-changed fires rapidly during a drag; each Preview spawns a
        # setpriv+gdbus subprocess (root must hop to the user's bus), so we
        # coalesce: remember the latest level and fire at most ~every 60 ms.
        self._dim_pending_level = slider_to_darkness(scale.get_value())
        if getattr(self, "_dim_preview_src", 0):
            return  # a flush is already scheduled; it will pick up the latest
        self._dim_preview_src = GLib.timeout_add(60, self._flush_dim_preview)

    def _flush_dim_preview(self):
        self._dim_preview_src = 0
        level = getattr(self, "_dim_pending_level", None)
        if level is not None:
            # gdbus wants a plain double literal for the 'd' arg.
            dim_daemon_call("Preview", "{:.4f}".format(level))
        return False  # one-shot

    def _on_dim_preview_end(self, *_):
        if getattr(self, "_dim_loading", False):
            return False
        # Cancel any pending throttled preview and send the final level now.
        if getattr(self, "_dim_preview_src", 0):
            GLib.source_remove(self._dim_preview_src)
            self._dim_preview_src = 0
        # Persist the new darkness, then tell the daemon to leave preview.
        self._commit_dim()
        dim_daemon_call("PreviewEnd")
        return False

    # -----------------------------------------------------------------------
    # Backend-missing banner
    # -----------------------------------------------------------------------
    def _add_backend_error_banner(self):
        bar = Gtk.InfoBar()
        bar.set_message_type(Gtk.MessageType.ERROR)
        bar.set_show_close_button(False)
        content = bar.get_content_area()
        label = Gtk.Label(xalign=0)
        label.set_line_wrap(True)
        label.set_markup(
            "<b>Backend modules failed to load.</b>\n"
            "MeatHandler needs monitor_history.py, edid_synth.py, and "
            "meathandler_apply.py in the same directory.\n"
            "<small><tt>{}</tt></small>".format(
                GLib.markup_escape_text(
                    (_BACKEND_ERROR or "").splitlines()[0]
                )
            )
        )
        content.add(label)
        self.root.pack_start(bar, False, False, 0)
        bar.show_all()

    # -----------------------------------------------------------------------
    # Monitor list
    # -----------------------------------------------------------------------
    def _refresh_monitor_list(self):
        """Reload history + active-sha256 and rebuild the ListBox."""
        # Clear existing rows
        for child in self.monitors_listbox.get_children():
            self.monitors_listbox.remove(child)
        self.entries = []
        self.active_entry = None
        self.active_sha256 = _read_active_edid_sha256()

        if monitor_history is None:
            self._show_empty_state(
                "Backend not loaded — monitor history unavailable."
            )
            self._populate_modes_for(None)
            return

        try:
            history = monitor_history.get_history() or []
        except Exception as e:
            self._show_empty_state(
                "Failed to load monitor history:\n{}".format(e)
            )
            self._populate_modes_for(None)
            return

        self.entries = [_normalize_history_entry(h) for h in history]

        if not self.entries:
            self._show_empty_state(
                "No monitors in history yet.\n"
                "Boot the PS5 with MeatHandler installed once to register "
                "your first monitor."
            )
            self._populate_modes_for(None)
            return

        # Build a row per entry
        for entry in self.entries:
            row = self._build_monitor_row(entry)
            self.monitors_listbox.add(row)

        self.monitors_listbox.show_all()

        # Default selection: active monitor if present, else first row
        target_row = None
        for i, entry in enumerate(self.entries):
            if (self.active_sha256 and
                    entry.get("edid_sha256") == self.active_sha256):
                self.active_entry = entry
                target_row = self.monitors_listbox.get_row_at_index(i)
                break
        if target_row is None:
            target_row = self.monitors_listbox.get_row_at_index(0)
        if target_row is not None:
            self.monitors_listbox.select_row(target_row)

    def _show_empty_state(self, message):
        """Replace the listbox content with a centered empty-state label."""
        # Remove listbox rows; add a single non-selectable label-row instead
        for child in self.monitors_listbox.get_children():
            self.monitors_listbox.remove(child)
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        lbl = Gtk.Label(label=message)
        lbl.set_line_wrap(True)
        lbl.set_justify(Gtk.Justification.CENTER)
        lbl.set_margin_top(24)
        lbl.set_margin_bottom(24)
        lbl.set_margin_start(16)
        lbl.set_margin_end(16)
        lbl.get_style_context().add_class("empty-state")
        row.add(lbl)
        self.monitors_listbox.add(row)
        self.monitors_listbox.show_all()

        # Disable buttons that make no sense
        self.apply_btn.set_sensitive(False)
        self.forget_btn.set_sensitive(False)

    def _build_monitor_row(self, entry):
        row = Gtk.ListBoxRow()
        row._mh_entry = entry  # stash for later

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hbox.set_margin_top(8)
        hbox.set_margin_bottom(8)
        hbox.set_margin_start(10)
        hbox.set_margin_end(10)
        row.add(hbox)

        icon = Gtk.Image.new_from_icon_name(
            "preferences-desktop-display", Gtk.IconSize.DND
        )
        hbox.pack_start(icon, False, False, 0)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hbox.pack_start(vbox, True, True, 0)

        # Title row: name + optional [ACTIVE] badge
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(title_row, False, False, 0)

        name = entry.get("display_name") or entry.get("model") or "Unknown monitor"
        name_lbl = Gtk.Label(xalign=0)
        name_lbl.set_markup(
            "<b>{}</b>".format(GLib.markup_escape_text(str(name)))
        )
        title_row.pack_start(name_lbl, False, False, 0)

        if (self.active_sha256 and
                entry.get("edid_sha256") == self.active_sha256):
            badge = Gtk.Label(label="ACTIVE")
            badge.get_style_context().add_class("active-badge")
            title_row.pack_start(badge, False, False, 0)

        # Spec line: "4K 144Hz - last used: today"
        spec_bits = []
        pref = entry.get("preferred_mode")
        if pref:
            spec_bits.append(_mode_label(pref))
        last = _humanize_last_used(entry.get("last_used"))
        spec_bits.append("last used: {}".format(last))
        spec_lbl = Gtk.Label(xalign=0, label=" · ".join(spec_bits))
        spec_lbl.get_style_context().add_class("spec-line")
        vbox.pack_start(spec_lbl, False, False, 0)

        return row

    def _on_row_selected(self, _listbox, row):
        if row is None or not hasattr(row, "_mh_entry"):
            self.selected_entry = None
            self._populate_modes_for(None)
            return
        self.selected_entry = row._mh_entry
        self.apply_btn.set_sensitive(True)
        self.forget_btn.set_sensitive(True)
        self._populate_modes_for(self.selected_entry)

    # -----------------------------------------------------------------------
    # Mode picker
    # -----------------------------------------------------------------------
    def _populate_modes_for(self, entry):
        """Rebuild the right-side radio buttons for the given entry."""
        for child in self.modes_box.get_children():
            self.modes_box.remove(child)
        self.mode_buttons = []
        self.current_modes = []

        if entry is None:
            placeholder = Gtk.Label(
                xalign=0,
                label="(Select a monitor above to see available modes.)"
            )
            placeholder.get_style_context().add_class("spec-line")
            self.modes_box.pack_start(placeholder, False, False, 0)
            self.modes_box.show_all()
            return

        modes = []
        if edid_synth is not None:
            try:
                modes = edid_synth.get_modes_for_monitor(
                    entry.get("db_match")
                ) or []
            except Exception as e:
                err = Gtk.Label(xalign=0)
                err.set_markup(
                    "<i>Failed to get modes: {}</i>".format(
                        GLib.markup_escape_text(str(e))
                    )
                )
                self.modes_box.pack_start(err, False, False, 0)
                self.modes_box.show_all()
                return

        if not modes:
            placeholder = Gtk.Label(
                xalign=0,
                label="(No modes available for this monitor.)"
            )
            placeholder.get_style_context().add_class("spec-line")
            self.modes_box.pack_start(placeholder, False, False, 0)
            self.modes_box.show_all()
            return

        self.current_modes = list(modes)

        # Determine default-selected mode key
        pref = entry.get("preferred_mode")
        default_key = _mode_key(pref) if pref else DEFAULT_FALLBACK_MODE
        available_keys = [_mode_key(m) for m in self.current_modes]
        if default_key not in available_keys:
            # Try a more forgiving fallback to 1080p@60 then first
            if DEFAULT_FALLBACK_MODE in available_keys:
                default_key = DEFAULT_FALLBACK_MODE
            else:
                default_key = available_keys[0]

        group = None
        for mode in self.current_modes:
            label = _mode_label(mode)
            key = _mode_key(mode)
            if group is None:
                btn = Gtk.RadioButton.new_with_label_from_widget(None, label)
                group = btn
            else:
                btn = Gtk.RadioButton.new_with_label_from_widget(group, label)
            btn._mh_mode = mode
            if key == default_key:
                btn.set_active(True)
            self.modes_box.pack_start(btn, False, False, 0)
            self.mode_buttons.append((btn, mode))

        self.modes_box.show_all()

    def _selected_mode(self):
        for btn, mode in self.mode_buttons:
            if btn.get_active():
                return mode
        return None

    # -----------------------------------------------------------------------
    # Apply flow
    # -----------------------------------------------------------------------
    def _set_busy(self, busy):
        self.in_apply = busy
        if busy:
            self.spinner.start()
            self.spinner.show()
        else:
            self.spinner.stop()
            self.spinner.hide()
        for w in (self.apply_btn, self.cancel_btn, self.forget_btn,
                  self.monitors_listbox, self.modes_box):
            w.set_sensitive(not busy)

    def _on_apply_clicked(self, *_):
        if self.selected_entry is None:
            return
        if meathandler_apply is None:
            self._show_error_dialog(
                "Backend not loaded",
                "meathandler_apply module is unavailable."
            )
            return
        mode = self._selected_mode()
        if mode is None:
            self._show_error_dialog(
                "No mode selected",
                "Pick a resolution / refresh rate first."
            )
            return

        # Remember what was active before so we can revert
        self.pending_revert_entry = self.active_entry

        entry = self.selected_entry
        self._set_busy(True)

        def worker():
            try:
                result = meathandler_apply.apply_monitor(entry, mode)
                GLib.idle_add(self._on_apply_done, True, result, None)
            except Exception as e:
                tb = traceback.format_exc()
                GLib.idle_add(self._on_apply_done, False, None,
                              "{}\n\n{}".format(e, tb))

        threading.Thread(target=worker, daemon=True).start()

    def _on_apply_done(self, ok, result, error_text):
        self._set_busy(False)
        if ok:
            self._show_apply_success_bar()
        else:
            # Pull last 10 lines of the apply log, if it exists
            log_tail = self._read_apply_log_tail(10)
            body = (error_text or "Unknown error") + "\n\n"
            if log_tail:
                body += "Apply log (last 10 lines):\n" + log_tail
            self._show_error_dialog("Apply failed", body, monospace=True)
        return False  # don't re-fire idle_add

    def _read_apply_log_tail(self, n):
        """Best-effort read of the apply log's tail."""
        path = None
        if meathandler_apply is not None:
            path = getattr(meathandler_apply, "APPLY_LOG_PATH", None) \
                or getattr(meathandler_apply, "LOG_PATH", None)
        if not path:
            # Common fallbacks
            for cand in ("/var/log/meathandler/apply.log",
                         os.path.expanduser("~/.meathandler/apply.log")):
                if os.path.exists(cand):
                    path = cand
                    break
        if not path or not os.path.exists(path):
            return ""
        try:
            with open(path, "r", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-n:])
        except Exception:
            return ""

    def _show_apply_success_bar(self):
        # Remove any previous infobar
        for child in self.info_slot.get_children():
            self.info_slot.remove(child)

        bar = Gtk.InfoBar()
        bar.set_message_type(Gtk.MessageType.INFO)
        bar.set_show_close_button(False)
        content = bar.get_content_area()

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        msg = Gtk.Label(xalign=0)
        msg.set_line_wrap(True)
        msg.set_text(
            "Applied. Unplug the cable and connect to your new monitor — "
            "it should light up within a few seconds."
        )
        vbox.pack_start(msg, False, False, 0)

        self.countdown_label = Gtk.Label(xalign=0)
        vbox.pack_start(self.countdown_label, False, False, 0)
        content.add(vbox)

        keep_btn = Gtk.Button(label="Keep this setting")
        keep_btn.connect("clicked", self._on_keep_clicked)
        bar.get_action_area().pack_end(keep_btn, False, False, 0)

        self.info_slot.pack_start(bar, False, False, 0)
        self.info_slot.show_all()
        self._current_infobar = bar

        # Start countdown
        self.revert_seconds_left = REVERT_COUNTDOWN_SECONDS
        self._update_countdown_label()
        if self.revert_timer_id is not None:
            GLib.source_remove(self.revert_timer_id)
        self.revert_timer_id = GLib.timeout_add_seconds(
            1, self._on_countdown_tick
        )

    def _update_countdown_label(self):
        if self.revert_seconds_left > 0:
            self.countdown_label.set_markup(
                "<small>Reverting in <b>{}s</b> if you don't click "
                "“Keep this setting”.</small>".format(
                    self.revert_seconds_left
                )
            )
        else:
            self.countdown_label.set_markup(
                "<small>Reverting now…</small>"
            )

    def _on_countdown_tick(self):
        self.revert_seconds_left -= 1
        self._update_countdown_label()
        if self.revert_seconds_left <= 0:
            self.revert_timer_id = None
            self._do_revert()
            return False
        return True

    def _on_keep_clicked(self, *_):
        # Cancel countdown, keep the new setting
        if self.revert_timer_id is not None:
            GLib.source_remove(self.revert_timer_id)
            self.revert_timer_id = None
        # Dismiss the InfoBar
        for child in self.info_slot.get_children():
            self.info_slot.remove(child)
        # Newly applied entry becomes the active entry; refresh list to update
        # the ACTIVE badge.
        self._refresh_monitor_list()

    def _do_revert(self):
        """Timer expired: revert to the previously-active monitor."""
        for child in self.info_slot.get_children():
            self.info_slot.remove(child)
        if self.pending_revert_entry is None or meathandler_apply is None:
            # Nothing to revert to; just refresh list
            self._refresh_monitor_list()
            return
        prev = self.pending_revert_entry
        self.pending_revert_entry = None
        self._set_busy(True)

        def worker():
            try:
                meathandler_apply.apply_monitor(prev)
                GLib.idle_add(self._on_revert_done, True, None)
            except Exception as e:
                tb = traceback.format_exc()
                GLib.idle_add(self._on_revert_done, False,
                              "{}\n\n{}".format(e, tb))

        threading.Thread(target=worker, daemon=True).start()

    def _on_revert_done(self, ok, error_text):
        self._set_busy(False)
        if not ok:
            self._show_error_dialog("Revert failed",
                                    error_text or "Unknown error",
                                    monospace=True)
        self._refresh_monitor_list()
        return False

    # -----------------------------------------------------------------------
    # Forget
    # -----------------------------------------------------------------------
    def _on_forget_clicked(self, *_):
        if self.selected_entry is None:
            return
        entry = self.selected_entry
        name = entry.get("display_name") or entry.get("model") or "this monitor"
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Forget “{}”?".format(name),
        )
        dlg.format_secondary_text(
            "MeatHandler will no longer remember this monitor's preferred "
            "mode. You can re-register it by booting the PS5 with it plugged "
            "in again."
        )
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return

        if monitor_history is None:
            self._show_error_dialog(
                "Backend not loaded",
                "monitor_history module is unavailable."
            )
            return

        try:
            mfr = entry.get("mfr_code") or entry.get("manufacturer")
            pid = entry.get("product_id") or entry.get("product_code")
            monitor_history.delete_entry(mfr, pid)
        except Exception as e:
            self._show_error_dialog("Forget failed", str(e))
            return

        self._refresh_monitor_list()

    # -----------------------------------------------------------------------
    # Error dialog
    # -----------------------------------------------------------------------
    def _show_error_dialog(self, title, body, monospace=False):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        if monospace:
            # Use a scrolled text view for long log output
            sw = Gtk.ScrolledWindow()
            sw.set_min_content_height(200)
            sw.set_min_content_width(420)
            tv = Gtk.TextView()
            tv.set_editable(False)
            tv.set_cursor_visible(False)
            tv.get_style_context().add_class("mono-log")
            tv.get_buffer().set_text(body)
            sw.add(tv)
            box = dlg.get_content_area()
            box.add(sw)
            sw.show_all()
        else:
            dlg.format_secondary_text(body)
        dlg.run()
        dlg.destroy()


# ---------------------------------------------------------------------------
# Application bootstrap
# ---------------------------------------------------------------------------
def on_activate(app):
    _install_css()
    # Reuse existing window if already created (Gtk.Application may activate
    # more than once).
    for w in app.get_windows():
        w.present()
        return
    win = MeatHandlerWindow(app)
    win.show_all()
    # Spinner hidden until needed
    win.spinner.hide()


def main():
    app = Gtk.Application(application_id="com.meathandler.display")
    app.connect("activate", on_activate)
    app.run([])


if __name__ == "__main__":
    main()
