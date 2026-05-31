#!/bin/bash
# ps5-autoedid-v2-install.sh — installs MeatHandler v2 auto-EDID with 120Hz monitor DB
# Replaces the v1 handler. Copy monitor_db.py to the PS5 first (scp it),
# OR let this installer embed it inline.
set -e

BOOT=/boot/efi
HELPER=/usr/local/sbin/ps5-autoedid
HELPER_DB=/usr/local/lib/ps5-autoedid/monitor_db.py
UNIT=/etc/systemd/system/ps5-autoedid.service
LOG=/var/log/ps5-autoedid.log

if [ "$(id -u)" -ne 0 ]; then
    echo "must be run as root: sudo bash $0" >&2
    exit 1
fi

mkdir -p "$(dirname "$HELPER_DB")"

# ─── Copy monitor_db.py + GUI libraries (must exist alongside this script) ───
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/monitor_db.py" ]; then
    cp "$SCRIPT_DIR/monitor_db.py" "$HELPER_DB"
    echo "ps5-autoedid-v2-install: installed monitor_db.py -> $HELPER_DB"
else
    echo "WARNING: monitor_db.py not found next to installer — 120Hz lookup will fall back to 60Hz" >&2
fi

# Install GUI library modules alongside monitor_db.py so the boot handler
# can import them (record_boot on every boot) and the GUI can load them.
for mod in edid_synth.py monitor_history.py meathandler_apply.py meathandler_dim.py; do
    if [ -f "$SCRIPT_DIR/$mod" ]; then
        cp "$SCRIPT_DIR/$mod" "/usr/local/lib/ps5-autoedid/$mod"
        echo "ps5-autoedid-v2-install: installed $mod -> /usr/local/lib/ps5-autoedid/"
    else
        echo "WARNING: $mod not found next to installer — GUI features disabled" >&2
    fi
done

# Install the GUI itself + desktop launcher entry
if [ -f "$SCRIPT_DIR/meathandler_gui.py" ]; then
    install -m 0755 "$SCRIPT_DIR/meathandler_gui.py" /usr/local/bin/meathandler-gui-py

    # Root-side helper: this is the EXACT program pkexec executes, so its
    # path must match exec.path in com.meathandler.display.policy. pkexec
    # scrubs the environment, so the user's Wayland/DBus session vars are
    # passed in as arguments and re-exported here before launching the GUI.
    cat > /usr/local/bin/meathandler-gui-root <<'GUI_ROOT_EOF'
#!/bin/bash
# MeatHandler GUI root helper — invoked via pkexec (runs as root).
# argv: WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS \
#       XDG_CURRENT_DESKTOP XDG_SESSION_TYPE -- [extra gui args...]
export WAYLAND_DISPLAY="$1"
export XDG_RUNTIME_DIR="$2"
export DBUS_SESSION_BUS_ADDRESS="$3"
export XDG_CURRENT_DESKTOP="$4"
export XDG_SESSION_TYPE="$5"
shift 5
[ "$1" = "--" ] && shift
export GDK_BACKEND=wayland
export PYTHONPATH=/usr/local/lib/ps5-autoedid
exec /usr/bin/python3 /usr/local/bin/meathandler-gui-py "$@"
GUI_ROOT_EOF
    chmod +x /usr/local/bin/meathandler-gui-root

    # User-facing launcher: captures the session env (pkexec strips it) and
    # elevates the root helper. Because exec.path now matches this helper and
    # allow_active=yes, the seat-active desktop user is NOT prompted.
    cat > /usr/local/bin/meathandler-gui <<'GUI_LAUNCHER_EOF'
#!/bin/bash
# MeatHandler GUI launcher (Wayland-aware). pkexec elevates the root helper,
# forwarding the user's Wayland + DBus session env as positional args.
exec pkexec /usr/local/bin/meathandler-gui-root \
    "$WAYLAND_DISPLAY" \
    "$XDG_RUNTIME_DIR" \
    "$DBUS_SESSION_BUS_ADDRESS" \
    "$XDG_CURRENT_DESKTOP" \
    "$XDG_SESSION_TYPE" \
    -- "$@"
GUI_LAUNCHER_EOF
    chmod +x /usr/local/bin/meathandler-gui
    echo "ps5-autoedid-v2-install: installed GUI -> /usr/local/bin/meathandler-gui (+root helper)"
fi

if [ -f "$SCRIPT_DIR/meathandler.desktop" ]; then
    # Back up the stock GNOME displays entry (once) before overwriting it.
    if [ -f /usr/share/applications/gnome-display-panel.desktop ] && \
       [ ! -f /usr/share/applications/gnome-display-panel.desktop.meathandler-bak ]; then
        cp /usr/share/applications/gnome-display-panel.desktop \
           /usr/share/applications/gnome-display-panel.desktop.meathandler-bak
        echo "ps5-autoedid-v2-install: backed up stock gnome-display-panel.desktop"
    fi
    # Overwrite the stock GNOME Settings Displays panel entry with ours so
    # GNOME Settings launches MeatHandler when the user clicks "Displays".
    install -m 0644 "$SCRIPT_DIR/meathandler.desktop" \
        /usr/share/applications/gnome-display-panel.desktop

    # Secondary entry (hidden from app grid via NoDisplay=true) so the
    # standalone MeatHandler binary still has a launcher record.
    cat > /usr/share/applications/meathandler.desktop <<'EOF'
[Desktop Entry]
Name=MeatHandler Display (standalone)
Exec=/usr/local/bin/meathandler-gui
Icon=preferences-desktop-display
Type=Application
NoDisplay=true
Categories=Settings;HardwareSettings;
EOF

    # Refresh the desktop database + icon cache so GNOME picks it up
    update-desktop-database /usr/share/applications 2>/dev/null || true
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor 2>/dev/null || true
    echo "ps5-autoedid-v2-install: registered .desktop entry (overrode gnome-display-panel)"
fi

# Install the polkit policy so admins/sudo group don't get prompted
# repeatedly when invoking the GUI via pkexec.
if [ -f "$SCRIPT_DIR/com.meathandler.display.policy" ]; then
    install -m 0644 "$SCRIPT_DIR/com.meathandler.display.policy" \
        /usr/share/polkit-1/actions/com.meathandler.display.policy
    echo "ps5-autoedid-v2-install: installed polkit policy"
fi

# Install GTK3/PyGObject if missing (apt is present on every PS5 Linux base image)
if ! python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null; then
    echo "ps5-autoedid-v2-install: installing GTK3/PyGObject for the GUI..."
    apt-get install -y python3-gi gir1.2-gtk-3.0 policykit-1 || \
        echo "WARNING: apt install failed — install manually: sudo apt install python3-gi gir1.2-gtk-3.0 policykit-1" >&2
fi

echo "ps5-autoedid-v2-install: writing $HELPER..."

cat > "$HELPER" <<'HELPER_EOF'
#!/bin/bash
set -u
LOG=/var/log/ps5-autoedid.log
SYSFS_EDID=/sys/class/drm/card0-DP-1/edid
SYSFS_FORCE=/sys/class/drm/card0-DP-1/force
FW_DIR=/lib/firmware/edid
FW_FILE="$FW_DIR/auto.bin"
DB=/usr/local/lib/ps5-autoedid/monitor_db.py

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

mkdir -p "$FW_DIR"
exec >> "$LOG" 2>&1

log "=== ps5-autoedid v2 run starting ==="

# sysfs files always report size=0 via stat — must use wc -c
for i in $(seq 1 30); do
    if [ $(wc -c < "$SYSFS_EDID" 2>/dev/null || echo 0) -gt 0 ]; then break; fi
    sleep 0.1
done
if [ $(wc -c < "$SYSFS_EDID" 2>/dev/null || echo 0) -eq 0 ]; then
    log "sysfs EDID empty after 3s — leaving existing auto.bin in place"
    exit 0
fi

RAW=$(mktemp)
cp "$SYSFS_EDID" "$RAW"
RAW_SIZE=$(wc -c < "$RAW")
RAW_SHA=$(sha256sum "$RAW" | cut -d' ' -f1)
log "live EDID: $RAW_SIZE bytes, sha256=$RAW_SHA"

python3 - "$RAW" "$FW_FILE" "$DB" <<'PY_EOF'
import sys, os, hashlib

src, dst, db_path = sys.argv[1], sys.argv[2], sys.argv[3]

# ── PS5 kernel mode gate — isHdmiModeValid() in ps5/hdmi.c ──────────────────
# Verified byte-for-byte against the running 7.0.5 kernel (hdmi.c:1199-1221):
#   VIC 16  — 1920x1080 @ 60Hz   (pclk=148500kHz)
#   VIC 63  — 1920x1080 @ 120Hz  (pclk=297000kHz)  ← the only injectable 120Hz
#   VIC 97  — 3840x2160 @ 60Hz   (pclk=594000kHz)
#   VIC 118 — 3840x2160 @ 120Hz  (pclk=1188000kHz) ← in gate, but pclk overflows
#             the 16-bit EDID DTD field, so it can't be expressed in a base block
#   1440p special case (by resolution, VIC ignored):
#             mode->clock in {241500, 241700} (60Hz) OR {497750, 592250} (120Hz)
#
# IMPORTANT: 1440p@120Hz IS accepted by this kernel (clocks 497750/592250).
# A monitor whose NATIVE preferred mode is one of the above is handled by the
# stock kernel directly — the PASSTHROUGH GUARD below leaves its EDID untouched.
#
# WHAT STILL CANNOT BE INJECTED VIA A BASE-BLOCK DTD (16-bit pclk limit):
#   4K@120Hz    — pclk=1188000kHz overflows the EDID DTD 16-bit pclk/10 field
#   4K@90Hz     — pclk ~765-891MHz overflows the same field
#
# Therefore: DTD *injection* only helps 1080p monitors. For 1440p/4K the DB
# lookup is still useful for logging/documentation but cannot enable 120Hz.

# ── Load monitor DB ──────────────────────────────────────────────────────────
get_max_hdmi_hz = None
if os.path.exists(db_path):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("monitor_db", db_path)
        mdb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mdb)
        get_max_hdmi_hz = mdb.get_max_hdmi_hz
        print(f"monitor_db loaded ({len(mdb.MONITOR_DB)} entries): {db_path}")
    except Exception as e:
        print(f"WARNING: could not load monitor_db: {e} — falling back to 60Hz")
else:
    print(f"monitor_db not found at {db_path} — falling back to 60Hz")

# ── Read and validate live EDID ──────────────────────────────────────────────
data = open(src, 'rb').read()
if len(data) < 128:
    print(f"FAIL: EDID is {len(data)} bytes, need >=128")
    sys.exit(2)

# PS5 MN864739 byte-0 quirk fix
if data[:8] != bytes([0,255,255,255,255,255,255,0]):
    if data[0] == 0x01 and data[1:8] == bytes([255,255,255,255,255,255,0]):
        data = b'\x00' + data[1:]
        print("note: fixed PS5 byte-0 quirk")
    else:
        print(f"FAIL: bad EDID magic: {data[:8].hex()}")
        sys.exit(3)

base = bytearray(data[:128])

# ── Decode EDID identity ─────────────────────────────────────────────────────
mfr_raw = (base[8] << 8) | base[9]
mfr = ''.join(chr(((mfr_raw >> shift) & 0x1F) + ord('A') - 1) for shift in (10, 5, 0))
product = (base[11] << 8) | base[10]
print(f"EDID identity: mfr={mfr} product=0x{product:04x}")

# ── Extract monitor name from descriptor blocks ──────────────────────────────
model_name = ""
for offset in (54, 72, 90, 108):
    block = base[offset:offset+18]
    if block[0] == 0 and block[1] == 0 and block[2] == 0 and block[3] == 0xFC:
        model_name = block[5:18].decode('ascii', errors='replace').rstrip('\n').strip()
        print(f"monitor name descriptor: '{model_name}'")
        break

if not model_name:
    print("WARNING: no 0xFC name descriptor in EDID — DB match impossible")

# ── DB lookup ────────────────────────────────────────────────────────────────
db_hz = 60
db_matched = False
if get_max_hdmi_hz and (model_name or product):
    db_hz = get_max_hdmi_hz(mfr, model_name, product)
    db_matched = (db_hz != 60)  # get_max_hdmi_hz returns 60 for unknown monitors
    if db_matched:
        print(f"DB MATCH: {mfr} '{model_name}' pid=0x{product:04x} -> hdmi_hz={db_hz}")
    else:
        print(f"DB: {mfr} '{model_name}' pid=0x{product:04x} not found — defaulting to 60Hz")
elif get_max_hdmi_hz:
    print("DB: no model name or product ID in EDID — cannot match")
else:
    print("DB: not loaded — defaulting to 60Hz")

# ── Record this monitor in history (for GUI to offer it later) ──────────────
try:
    import importlib.util as _ilu
    _mhspec = _ilu.spec_from_file_location("monitor_history",
                                            "/usr/local/lib/ps5-autoedid/monitor_history.py")
    if _mhspec is not None:
        _mh = _ilu.module_from_spec(_mhspec)
        _mhspec.loader.exec_module(_mh)
        _db_match = {}
        if get_max_hdmi_hz and (model_name or product):
            entry = None
            try:
                entry = mdb.lookup_by_model(mfr, model_name) if model_name else None
            except Exception:
                entry = None
            if entry is None and product:
                key = mdb.PRODUCT_ID_INDEX.get((mfr.lower(), product))
                if key is not None:
                    entry = mdb.MONITOR_DB.get(key)
                    _db_match["key"] = key[1]
            if entry is not None:
                _db_match.update({
                    "hdmi_hz": entry.get("hdmi_hz", 60),
                    "max_hz": entry.get("max_hz", 60),
                    "resolutions": entry.get("resolutions", []),
                })
        _mh.record_boot(mfr, product, model_name or "Monitor", data, _db_match)
        print(f"history: recorded boot for {mfr} 0x{product:04x} '{model_name}'")
except Exception as _e:
    print(f"history: record_boot skipped ({_e})")

# ── Read native resolution from DTD slot 0 ──────────────────────────────────
native_w = base[56] | ((base[58] & 0xF0) << 4)
native_h = base[59] | ((base[61] & 0xF0) << 4)
print(f"native resolution from DTD: {native_w}x{native_h}")

# ── PASSTHROUGH GUARD ────────────────────────────────────────────────────────
# If the monitor's NATIVE preferred mode (DTD slot 0) already passes the PS5
# kernel's isHdmiModeValid() gate, the stock kernel can drive it directly with
# the monitor's own EDID — no rewrite needed. Rewriting in that case is pure
# downside: stripping the CTA extension block loses the IT-Content/no-underscan
# flag, so amdgpu applies a default underscan and the image is mis-centered.
#
# The gate (verified byte-for-byte against ps5-linux hdmi.c isHdmiModeValid):
#   1440p (2560x1440): clock in {241500, 241700, 497750, 592250}
#   else: VIC in {16 (1080p60), 63 (1080p120), 97 (4K60), 118 (4K120)}
# drm_match_cea_mode() keys on resolution+timing, so we match the native DTD
# pclk against the canonical clock for each gate-accepted VIC.
def _native_pref_passes_gate(b):
    pclk = int.from_bytes(b[54:56], 'little') * 10  # kHz, as the kernel sees mode->clock
    h_act = b[56] | ((b[58] & 0xF0) << 4)
    v_act = b[59] | ((b[61] & 0xF0) << 4)
    # 1440p special-case (kernel checks resolution + clock, ignores VIC)
    if h_act == 2560 and v_act == 1440:
        return pclk in (241500, 241700, 497750, 592250), f"1440p clock={pclk}"
    # Non-1440p: must be one of the four whitelisted VICs. Match by canonical
    # (resolution, pclk) — the same identity drm_match_cea_mode resolves to a VIC.
    VIC_GATE = {
        (1920, 1080, 148500): 16,   # 1080p60
        (1920, 1080, 297000): 63,   # 1080p120
        (3840, 2160, 594000): 97,   # 4K60
        (3840, 2160, 1188000): 118, # 4K120
    }
    vic = VIC_GATE.get((h_act, v_act, pclk))
    if vic is not None:
        return True, f"VIC {vic} ({h_act}x{v_act} pclk={pclk})"
    return False, f"{h_act}x{v_act} pclk={pclk} (no gate match)"

_passes, _why = _native_pref_passes_gate(base)
if _passes:
    # Native EDID already works on the stock kernel. Write it through verbatim
    # (ALL blocks incl. CTA extension), so centering/underscan stay as the
    # distro intended. This is the coexistence path: MeatHandler does nothing
    # to monitors that aren't broken.
    native_sha = hashlib.sha256(data).hexdigest()
    tmp = dst + '.tmp'
    open(tmp, 'wb').write(data)
    os.replace(tmp, dst)
    print(f"PASSTHROUGH: native preferred mode passes kernel gate [{_why}] — "
          f"writing native EDID untouched ({len(data)} bytes, ext_blocks="
          f"{base[126]}, sha256={native_sha})")
    print(f"wrote {dst}")
    sys.exit(0)
else:
    print(f"REWRITE: native preferred mode rejected by kernel gate [{_why}] — "
          f"normalizing EDID to prevent black screen")

# ── Normalize 60Hz DTD pclk to nearest kernel-accepted value ─────────────────
# isHdmiModeValid() accepts: 1440p={241500,241700}, VIC16=148500, VIC63=297000, VIC97=594000
# We pin to the nearest valid pclk so the base 60Hz mode is always accepted.
PCLK_60HZ = [148500, 241500, 241700, 297000, 594000]
pclk_raw_60 = int.from_bytes(base[54:56], 'little') * 10
nearest_60 = min(PCLK_60HZ, key=lambda v: abs(v - pclk_raw_60))
if pclk_raw_60 != nearest_60:
    nr = nearest_60 // 10
    base[54] = nr & 0xFF
    base[55] = (nr >> 8) & 0xFF
    print(f"normalized DTD pclk: {pclk_raw_60} -> {nearest_60} kHz")
else:
    print(f"DTD pclk already accepted: {pclk_raw_60} kHz")

# Strip all extension blocks — MN864739 fails with any CTA extension block
base[126] = 0

def checksum(buf):
    s = sum(buf[:127]) & 0xFF
    buf[127] = (256 - s) & 0xFF
    return buf

base = checksum(base)
if sum(base) % 256 != 0:
    print("FAIL: base checksum did not converge")
    sys.exit(5)

# ── 120Hz DTD injection — 1080p ONLY ────────────────────────────────────────
# isHdmiModeValid() accepts VIC 63 (1920x1080@120Hz, pclk=297000kHz).
# This is the ONLY resolution/rate where DTD injection actually unlocks 120Hz.
# 1440p@120Hz and 4K@120Hz are blocked by the kernel gate regardless of EDID.
inject_120 = (db_hz >= 120) and (native_w == 1920) and (native_h == 1080)

if db_hz >= 120 and native_w != 1920:
    res_label = f"{native_w}x{native_h}"
    if native_w == 3840:
        print(f"NOTE: {res_label} — 4K@120Hz impossible (pclk overflow + kernel gate). "
              f"Kernel offers 4K@60Hz via VIC 97.")
    elif native_w == 2560:
        print(f"NOTE: {res_label} — kernel gate blocks 1440p@120Hz (isHdmiModeValid checks "
              f"1440p clock==241500/241700 only). Best available: 1440p@60Hz.")
    else:
        print(f"NOTE: {res_label} — no 120Hz path for this resolution. Keeping 60Hz.")

if inject_120:
    # CTA-861 VIC 63: 1920x1080@120Hz
    # h_total=2200 (1920+280), v_total=1125 (1080+45)
    # 297,000,000 / (2200 * 1125) = 120.000 Hz exactly
    H_ACTIVE, H_BLANK = 1920, 280
    V_ACTIVE, V_BLANK = 1080, 45
    H_FP, H_SW = 88, 44   # CTA-861 table values for VIC 63
    V_FP, V_SW = 4, 5
    PCLK_120 = 297000

    pclk_raw = PCLK_120 // 10   # = 29700, fits in 16 bits
    dtd = bytearray(18)
    dtd[0]  = pclk_raw & 0xFF
    dtd[1]  = (pclk_raw >> 8) & 0xFF
    dtd[2]  = H_ACTIVE & 0xFF
    dtd[3]  = H_BLANK & 0xFF
    dtd[4]  = ((H_ACTIVE >> 4) & 0xF0) | ((H_BLANK >> 8) & 0x0F)
    dtd[5]  = V_ACTIVE & 0xFF
    dtd[6]  = V_BLANK & 0xFF
    dtd[7]  = ((V_ACTIVE >> 4) & 0xF0) | ((V_BLANK >> 8) & 0x0F)
    dtd[8]  = H_FP & 0xFF
    dtd[9]  = H_SW & 0xFF
    dtd[10] = ((V_FP & 0x0F) << 4) | (V_SW & 0x0F)
    dtd[11] = ((H_FP >> 8) & 0x03) << 6 | ((H_SW >> 8) & 0x03) << 4 | \
              ((V_FP >> 4) & 0x03) << 2 | ((V_SW >> 4) & 0x03)
    # Carry image size from original DTD (bytes 66-68 = base[54+12..54+14])
    dtd[12] = base[66]
    dtd[13] = base[67]
    dtd[14] = base[68]
    dtd[15] = 0     # h border
    dtd[16] = 0     # v border
    dtd[17] = 0x1E  # flags: progressive, no stereo, digital separate sync, vsync+, hsync+

    # Slot 0 → 120Hz VIC 63 DTD  (kernel uses first DTD for preferred mode)
    # Slot 1 → original 60Hz DTD (preserved as fallback)
    orig_dtd = bytes(base[54:72])
    base[54:72] = dtd
    base[72:90] = orig_dtd
    base = checksum(base)
    if sum(base) % 256 != 0:
        print("FAIL: post-injection checksum failed")
        sys.exit(6)
    print(f"INJECTED VIC 63 (1080p@120Hz): pclk={PCLK_120}kHz slot0=120Hz slot1={nearest_60}kHz")
else:
    if not inject_120 and db_hz < 120:
        print(f"60Hz: DB says {db_hz}Hz, no injection needed")

out_sha = hashlib.sha256(bytes(base)).hexdigest()
print(f"final EDID: 128 bytes, ext=0, sha256={out_sha}")

tmp = dst + '.tmp'
open(tmp, 'wb').write(bytes(base))
os.replace(tmp, dst)
print(f"wrote {dst}")
PY_EOF
RC=$?
rm -f "$RAW"
if [ $RC -ne 0 ]; then
    log "EDID normalization failed (rc=$RC) — NOT replacing existing EDID"
    exit 0
fi

FINAL_SHA=$(sha256sum "$FW_FILE" | cut -d' ' -f1)
log "wrote $FW_FILE sha256=$FINAL_SHA"

if [ -w "$SYSFS_FORCE" ]; then
    echo detect > "$SYSFS_FORCE" 2>/dev/null && log "triggered DRM detect"
fi
if command -v udevadm >/dev/null; then
    udevadm trigger --subsystem-match=drm --action=change >/dev/null 2>&1 || true
fi

# NOTE: an xrandr-based underscan override used to live here. It is removed
# deliberately:
#   1. DEAD ON WAYLAND. PS5 Linux 7.0.5 runs GNOME/Mutter on Wayland, so there
#      is no X server. Worse, leftover /tmp/.X11-unix/X* sockets (from Xwayland
#      or prior sessions) accept the connect() but never complete the X
#      handshake, so each `xrandr` call BLOCKS indefinitely — hanging this
#      helper at boot and at install time (observed: install wedged on
#      `xrandr --output DP-1 --set underscan off`). `2>/dev/null || true`
#      does NOT save us because xrandr blocks rather than erroring.
#   2. OBSOLETE. The underscan shift was caused by stripping the CTA-861
#      extension block (losing the IT-Content / no-underscan flag). The
#      PASSTHROUGH GUARD above now leaves a gate-passing native EDID fully
#      intact (extension block included), so no underscan is ever introduced
#      for those monitors — making the xrandr workaround unnecessary.

log "=== ps5-autoedid v2 run complete ==="
HELPER_EOF

chmod +x "$HELPER"

echo "ps5-autoedid-v2-install: writing systemd unit..."

cat > "$UNIT" <<'UNIT_EOF'
[Unit]
Description=MeatHandler v2 — PS5 auto EDID normalizer with 120Hz DB
DefaultDependencies=no
After=systemd-tmpfiles-setup.service
Before=display-manager.service graphical.target multi-user.target
ConditionPathExists=/sys/class/drm/card0-DP-1

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/ps5-autoedid
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT_EOF

echo "ps5-autoedid-v2-install: enabling unit..."
systemctl daemon-reload
systemctl enable ps5-autoedid.service

INITRAMFS_HOOK=/etc/initramfs-tools/hooks/ps5-autoedid
INITRAMFS_SCRIPT=/etc/initramfs-tools/scripts/init-top/ps5-autoedid

if [ -d /etc/initramfs-tools ]; then
    echo "ps5-autoedid-v2-install: writing initramfs hook..."
    cat > "$INITRAMFS_HOOK" <<'HOOK_EOF'
#!/bin/sh
PREREQ=""
prereqs() { echo "$PREREQ"; }
case "$1" in prereqs) prereqs; exit 0 ;; esac
. /usr/share/initramfs-tools/hook-functions
copy_exec /usr/bin/sha256sum || true
copy_exec /bin/dd
copy_exec /usr/bin/python3 || true
# Copy monitor DB into initramfs
if [ -f /usr/local/lib/ps5-autoedid/monitor_db.py ]; then
    mkdir -p "${DESTDIR}/usr/local/lib/ps5-autoedid"
    cp /usr/local/lib/ps5-autoedid/monitor_db.py "${DESTDIR}/usr/local/lib/ps5-autoedid/"
fi
HOOK_EOF
    chmod +x "$INITRAMFS_HOOK"

    cat > "$INITRAMFS_SCRIPT" <<'INIT_EOF'
#!/bin/sh
PREREQ=""
prereqs() { echo "$PREREQ"; }
case "$1" in prereqs) prereqs; exit 0 ;; esac

SYSFS_EDID=/sys/class/drm/card0-DP-1/edid
OUT_DIR=/run/firmware/edid
OUT=/run/firmware/edid/auto.bin
LOG=/run/ps5-autoedid-initramfs.log
DB=/usr/local/lib/ps5-autoedid/monitor_db.py

mkdir -p "$OUT_DIR"
log() { echo "[initramfs] $*" >> "$LOG"; echo "ps5-autoedid: $*" > /dev/kmsg 2>/dev/null || true; }

# sysfs files always report size=0 via stat — must use wc -c
i=0
while [ $i -lt 20 ]; do
    [ $(wc -c < "$SYSFS_EDID" 2>/dev/null || echo 0) -gt 0 ] && break
    sleep 0.1
    i=$((i+1))
done

if [ $(wc -c < "$SYSFS_EDID" 2>/dev/null || echo 0) -eq 0 ]; then
    log "no sysfs EDID after 2s — skipping"
    exit 0
fi

cp "$SYSFS_EDID" /run/edid-live.bin
log "live EDID: $(wc -c < /run/edid-live.bin) bytes"

if command -v python3 >/dev/null 2>&1; then
    python3 - /run/edid-live.bin "$OUT" "$DB" <<'PY'
import sys, os

src, dst, db_path = sys.argv[1], sys.argv[2], sys.argv[3]

# Load monitor DB — same logic as systemd helper
get_max_hdmi_hz = None
if os.path.exists(db_path):
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("monitor_db", db_path)
        mdb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mdb)
        get_max_hdmi_hz = mdb.get_max_hdmi_hz
    except Exception:
        pass

data = open(src, 'rb').read()
if len(data) < 128: sys.exit(2)
if data[0] == 0x01 and data[1:8] == bytes([255]*7):
    data = b'\x00' + data[1:]

base = bytearray(data[:128])

# Decode identity
mfr_raw = (base[8] << 8) | base[9]
mfr = ''.join(chr(((mfr_raw >> shift) & 0x1F) + ord('A') - 1) for shift in (10, 5, 0))

# Extract model name from 0xFC descriptor
model_name = ""
for offset in (54, 72, 90, 108):
    block = base[offset:offset+18]
    if block[0]==0 and block[1]==0 and block[2]==0 and block[3]==0xFC:
        model_name = block[5:18].decode('ascii', errors='replace').rstrip('\n').strip()
        break

# Extract product ID for fallback lookup
product = (base[11] << 8) | base[10]

# DB lookup — only meaningful for 1080p monitors (1440p/4K blocked by kernel gate)
db_hz = 60
if get_max_hdmi_hz and (model_name or product):
    db_hz = get_max_hdmi_hz(mfr, model_name, product)

native_w = base[56] | ((base[58] & 0xF0) << 4)
native_h = base[59] | ((base[61] & 0xF0) << 4)

# ── PASSTHROUGH GUARD (must mirror the systemd helper) ───────────────────────
# If the native preferred mode already passes the kernel gate, the stock kernel
# can drive it directly — write the native EDID through UNTOUCHED (full, incl.
# CTA extension block) so centering/underscan are preserved. Only rewrite when
# the native mode would black-screen. Gate verified against hdmi.c:1199-1221.
def _native_pref_passes_gate(b):
    pclk = int.from_bytes(b[54:56], 'little') * 10
    h_act = b[56] | ((b[58] & 0xF0) << 4)
    v_act = b[59] | ((b[61] & 0xF0) << 4)
    if h_act == 2560 and v_act == 1440:
        return pclk in (241500, 241700, 497750, 592250)
    VIC_GATE = {(1920, 1080, 148500), (1920, 1080, 297000),
                (3840, 2160, 594000), (3840, 2160, 1188000)}
    return (h_act, v_act, pclk) in VIC_GATE

if _native_pref_passes_gate(base):
    tmp = dst + '.tmp'
    open(tmp, 'wb').write(data)   # full native EDID, all blocks, untouched
    os.replace(tmp, dst)
    sys.exit(0)

# Normalize 60Hz DTD pclk to kernel-accepted value
PCLK_60HZ = [148500, 241500, 241700, 297000, 594000]
pclk_60 = int.from_bytes(base[54:56], 'little') * 10
nearest_60 = min(PCLK_60HZ, key=lambda v: abs(v - pclk_60))
nr = nearest_60 // 10
base[54] = nr & 0xFF
base[55] = (nr >> 8) & 0xFF
base[126] = 0

def checksum(buf):
    s = sum(buf[:127]) & 0xFF
    buf[127] = (256 - s) & 0xFF
    return buf

base = checksum(base)

# Inject VIC 63 (1080p@120Hz) only — the sole kernel-gated 120Hz path
# isHdmiModeValid() accepts: VIC16 (1080@60), VIC63 (1080@120), VIC97 (4K@60)
# 1440p@120Hz and 4K@120Hz are blocked by the kernel gate regardless of EDID
if db_hz >= 120 and native_w == 1920 and native_h == 1080:
    dtd = bytearray(18)
    # VIC 63: 1920x1080@120Hz — pclk=297000kHz, h_total=2200, v_total=1125 = 120.000Hz
    # Byte values verified: pclk_raw=29700=0x7404 LE
    dtd[0]=0x04; dtd[1]=0x74                             # pclk=297000kHz
    dtd[2]=0x80; dtd[3]=0x18; dtd[4]=0x71               # h_active=1920, h_blank=280
    dtd[5]=0x38; dtd[6]=0x2D; dtd[7]=0x40               # v_active=1080, v_blank=45
    dtd[8]=0x58; dtd[9]=0x2C; dtd[10]=0x45; dtd[11]=0x00  # h_fp=88,h_sw=44,v_fp=4,v_sw=5
    dtd[12]=base[66]; dtd[13]=base[67]; dtd[14]=base[68]  # preserve image size mm
    dtd[15]=0; dtd[16]=0; dtd[17]=0x1E                  # flags: progressive, separate sync, vsync+, hsync+
    orig_dtd = bytes(base[54:72])
    base[54:72] = dtd
    base[72:90] = orig_dtd
    base = checksum(base)

tmp = dst + '.tmp'
open(tmp, 'wb').write(bytes(base))
os.replace(tmp, dst)
PY
    RC=$?
else
    dd if=/run/edid-live.bin of="$OUT" bs=128 count=1 status=none
    RC=0
    log "WARN: python3 not in initramfs — used dd fallback (60Hz only)"
fi

[ $RC -eq 0 ] && log "wrote $OUT" || log "normalize failed rc=$RC"
INIT_EOF
    chmod +x "$INITRAMFS_SCRIPT"

    echo "ps5-autoedid-v2-install: regenerating initramfs..."
    update-initramfs -u

    if [ -f /boot/initrd.img ]; then
        cp /boot/initrd.img "$BOOT/initrd.img"
        echo "  copied new initrd -> $BOOT/initrd.img"
    fi
fi

# ── 7.0.10 USB-boot black-screen fix: defer amdgpu autoload ─────────────────
# Root cause: amdgpu_pci_probe defers on spcie_is_initialized(), but that
# flag flips when the Sony ICC channel is set up — NOT when PS5 firmware
# has actually delivered the EDID payload via async ICC notification
# (hdmi.c hdmi_notification_handler, msg->data[1]==0x02 sets real_edid).
# PS5 has no HDMI HPD (#ifndef CONFIG_X86_PS5 around amdgpu_dm_hpd_init),
# so amdgpu's only source of EDID is real_edid. On NVMe (nvme=m), initramfs
# overhead delays amdgpu load enough that real_edid is populated when probe
# fires. On USB (usb-storage/uas/xhci all built-in), root mounts faster
# than the ICC notification arrives → real_edid==NULL → black screen.
#
# Fix: blacklist amdgpu from kernel autoload and let userspace load it
# after rootdelay=3 — by then the ICC notification has long since fired
# and real_edid is populated. Harmless on NVMe boots.
ROOT_DEV=$(findmnt -no SOURCE / 2>/dev/null || true)
case "$ROOT_DEV" in
    /dev/nvme*)
        BOOT_FIXES=""
        echo "ps5-autoedid-v2-install: NVMe boot ($ROOT_DEV) — no rootdelay needed"
        ;;
    *)
        # USB/SD/unknown — apply the late-amdgpu-load fix
        BOOT_FIXES="rootdelay=3 modprobe.blacklist=amdgpu"
        mkdir -p /etc/modules-load.d
        echo amdgpu > /etc/modules-load.d/amdgpu-late.conf
        echo "ps5-autoedid-v2-install: external/USB boot ($ROOT_DEV) — deferring amdgpu autoload"
        ;;
esac

# Stage auto.bin inside /lib/firmware AS WELL as /run/firmware so the
# deferred-probe race can't lose to a not-yet-populated /run/firmware.
# (initramfs hook below also copies it into the initramfs image.)
mkdir -p /lib/firmware/edid
if [ -f /lib/firmware/edid/auto.bin ]; then
    log_msg="auto.bin already at /lib/firmware/edid/auto.bin"
elif [ -f "$BOOT/edid/auto.bin" ]; then
    cp "$BOOT/edid/auto.bin" /lib/firmware/edid/auto.bin
    log_msg="staged auto.bin -> /lib/firmware/edid/auto.bin"
fi

# Update cmdline.txt — drm.edid_firmware override + dual firmware path so
# either /lib/firmware (initramfs-baked) or /run/firmware (post-pivot) wins.
CMDLINE="$BOOT/cmdline.txt"
CMDLINE_BAK="$BOOT/cmdline.txt.tv"
if [ -f "$CMDLINE" ] && [ -f "$CMDLINE_BAK" ]; then
    BASE=$(tr -d '\n' < "$CMDLINE_BAK")
    NEW=$(echo "$BASE" | tr ' ' '\n' \
        | grep -v '^drm\.edid_firmware=' \
        | grep -v '^video=DP-1' \
        | grep -v '^amdgpu\.force_1080p=' \
        | grep -v '^firmware_class\.path=' \
        | grep -v '^rootdelay=' \
        | grep -v '^rootwait$' \
        | grep -v '^module_blacklist=' \
        | grep -v '^modprobe\.blacklist=' \
        | tr '\n' ' ' | sed 's/  */ /g; s/ $//')
    NEW="$NEW drm.edid_firmware=DP-1:edid/auto.bin video=DP-1:e firmware_class.path=/lib/firmware:/run/firmware snd_hda_intel.enable_dp_mst=0 $BOOT_FIXES"
    NEW=$(echo "$NEW" | sed 's/  */ /g; s/ $//')
    echo "$NEW" > "$CMDLINE"
    echo "ps5-autoedid-v2-install: updated $CMDLINE -> auto.bin"
    echo "  new cmdline: $NEW"
fi

echo "ps5-autoedid-v2-install: running first EDID capture now..."
"$HELPER" && echo "  capture OK" || echo "  capture noted issues — check $LOG"

if [ -f /lib/firmware/edid/auto.bin ]; then
    mkdir -p "$BOOT/edid"
    cp /lib/firmware/edid/auto.bin "$BOOT/edid/auto.bin"
    echo "  mirrored auto.bin -> $BOOT/edid/auto.bin"
fi

# ---------------------------------------------------------------------------
# Screen Dim — install the per-user overlay daemon as a systemd --user service.
# The daemon must run inside the graphical session (to draw the overlay), so it
# is a --user unit, NOT a system unit. We drop the unit into the system-wide
# user unit dir and enable+start it for the invoking desktop user.
# ---------------------------------------------------------------------------
DIM_DAEMON=/usr/local/lib/ps5-autoedid/meathandler_dim.py
USER_UNIT_DIR=/etc/systemd/user
if [ -f "$DIM_DAEMON" ]; then
    mkdir -p "$USER_UNIT_DIR"
    cat > "$USER_UNIT_DIR/meathandler-dim.service" <<'DIMUNIT_EOF'
[Unit]
Description=MeatHandler Screen Dim overlay (dim-on-inactivity)
# Only meaningful inside a graphical session.
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/lib/ps5-autoedid/meathandler_dim.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
DIMUNIT_EOF
    echo "ps5-autoedid-v2-install: installed dim user unit -> $USER_UNIT_DIR/meathandler-dim.service"

    # Figure out the desktop user (the one who invoked sudo), and enable+start
    # the unit in their --user manager. Falls back gracefully if unknown.
    DESK_USER="${SUDO_USER:-${PKEXEC_USER:-}}"
    if [ -z "$DESK_USER" ]; then
        DESK_USER=$(logname 2>/dev/null || true)
    fi
    if [ -n "$DESK_USER" ] && [ "$DESK_USER" != "root" ]; then
        DESK_UID=$(id -u "$DESK_USER" 2>/dev/null || true)
        if [ -n "$DESK_UID" ]; then
            # Seed a default config so the daemon has values on first start.
            DESK_HOME=$(getent passwd "$DESK_USER" | cut -d: -f6)
            CFG_DIR="$DESK_HOME/.config/meathandler"
            mkdir -p "$CFG_DIR"
            if [ ! -f "$CFG_DIR/dim.conf" ]; then
                cat > "$CFG_DIR/dim.conf" <<'CFG_EOF'
[dim]
enabled = true
darkness = 0.700
idle_seconds = 30
CFG_EOF
            fi
            chown -R "$DESK_USER":"$DESK_USER" "$DESK_HOME/.config/meathandler" 2>/dev/null || true

            # Enable for future logins, and start now if the user has a running
            # session bus. Run as the user against their systemd --user manager.
            sudo -u "$DESK_USER" XDG_RUNTIME_DIR="/run/user/$DESK_UID" \
                systemctl --user daemon-reload 2>/dev/null || true
            sudo -u "$DESK_USER" XDG_RUNTIME_DIR="/run/user/$DESK_UID" \
                systemctl --user enable meathandler-dim.service 2>/dev/null \
                && echo "ps5-autoedid-v2-install: enabled dim for $DESK_USER" \
                || echo "ps5-autoedid-v2-install: dim will enable on next login for $DESK_USER"
            sudo -u "$DESK_USER" XDG_RUNTIME_DIR="/run/user/$DESK_UID" \
                systemctl --user start meathandler-dim.service 2>/dev/null \
                && echo "ps5-autoedid-v2-install: started dim now" \
                || echo "ps5-autoedid-v2-install: dim starts on next login"
        fi
    else
        echo "ps5-autoedid-v2-install: could not detect desktop user — enable dim manually:"
        echo "    systemctl --user enable --now meathandler-dim.service"
    fi
fi

echo ""
echo "=== ps5-autoedid-v2-install: DONE ==="
echo "  Verify: cat $LOG"
echo "  Recover: sudo $BOOT/safe-boot.sh"
echo "  Screen Dim: adjust in the MeatHandler GUI (Screen Dim section),"
echo "              or edit ~/.config/meathandler/dim.conf"
