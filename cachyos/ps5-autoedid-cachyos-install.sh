#!/bin/bash
# ps5-autoedid-cachyos-install.sh
# ───────────────────────────────────────────────────────────────────────────
# PS5 Linux black-screen-on-display-init fix — CachyOS / Arch port.
#
# WHAT THIS DOES
#   Normalizes (or passes through) the connected monitor's EDID at the
#   EARLIEST possible boot stage — inside the initramfs, before amdgpu
#   probes the display. This prevents the PS5 HDMI bridge (FLAVA3 /
#   MN864739) from rejecting the monitor's native EDID and black-screening.
#
# SCOPE (intentional)
#   * Boot-fix only. No GUI. No screen-dim. No runtime resolution switcher.
#   * Works automatically with any monitor that has worked on the PS5 once.
#   * Locked to a single connected monitor at boot (the one plugged in when
#     the initramfs runs).
#
# WHY IT'S A KERNEL-PATCH-LEVEL FIX, NOT A USERSPACE ONE
#   The PS5 has no HDMI hot-plug detect. amdgpu sees the EDID exactly once,
#   from the firmware loader. If that EDID has timings the PS5 kernel gate
#   (isHdmiModeValid in ps5/hdmi.c) rejects, the screen never lights up.
#   Fixing this in userspace (gnome-control-center, xrandr, etc.) is too
#   late — the kernel has already failed to bring the display up. The fix
#   must run in the initramfs, before amdgpu loads, which is exactly what
#   this installer wires up.
#
# DISTRO TARGET
#   CachyOS (primary) and Arch Linux (compatible). Uses mkinitcpio +
#   pacman. For Ubuntu (initramfs-tools / apt) see ../ubuntu/.
#
# PS5-LINUX BOOT CONTRACT (verified against github.com/ps5-linux/ps5-linux-image)
#   PS5 Linux boots via kexec from the FAT32 partition at /boot/efi:
#       kexec -l /boot/efi/bzImage \
#             --initrd=/boot/efi/initrd-<distro>.img \
#             --command-line="$(cat /boot/efi/cmdline-<distro>.txt)"
#   This installer therefore deploys to:
#       /boot/efi/initrd-cachyos.img       (per-distro initrd)
#       /boot/efi/cmdline-cachyos.txt      (per-distro kernel cmdline)
#   and does NOT touch the Ubuntu defaults (initrd.img / cmdline.txt).
#
# RECOVERY
#   sudo /boot/efi/safe-boot-cachyos.sh
# ───────────────────────────────────────────────────────────────────────────
set -e

BOOT=/boot/efi
HELPER=/usr/local/sbin/ps5-autoedid
HELPER_LIB=/usr/local/lib/ps5-autoedid
HELPER_DB="$HELPER_LIB/monitor_db.py"
UNIT=/etc/systemd/system/ps5-autoedid.service
LOG=/var/log/ps5-autoedid.log

# Kernel cmdline target — per PS5-Linux multi-distro kexec contract
CMDLINE_FILE="$BOOT/cmdline-cachyos.txt"
CMDLINE_BAK="$BOOT/cmdline-cachyos.txt.bak"
INITRD_TARGET="$BOOT/initrd-cachyos.img"

# mkinitcpio hook names (must match files we write below)
MKINIT_INSTALL_HOOK=/etc/initcpio/install/ps5-autoedid
MKINIT_RUNTIME_HOOK=/etc/initcpio/hooks/ps5-autoedid

# ───────────────────────────────────────────────────────────────────────────
# Sanity
# ───────────────────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "must be run as root: sudo bash $0" >&2
    exit 1
fi

if ! command -v mkinitcpio >/dev/null 2>&1; then
    echo "ERROR: mkinitcpio not found. This installer is for CachyOS / Arch." >&2
    echo "For Ubuntu, use ../ubuntu/ps5-autoedid-v2-install.sh instead." >&2
    exit 1
fi

if ! command -v pacman >/dev/null 2>&1; then
    echo "ERROR: pacman not found. CachyOS / Arch only." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -f "$SCRIPT_DIR/monitor_db.py" ]; then
    echo "ERROR: monitor_db.py not found next to this installer." >&2
    echo "       Run the installer from the cachyos/ folder containing both files." >&2
    exit 1
fi

# Confirm we're actually on a PS5 install (rather than wrecking someone's laptop)
if [ ! -e /sys/class/drm/card0-DP-1 ]; then
    echo "WARNING: /sys/class/drm/card0-DP-1 does not exist." >&2
    echo "         This is the PS5's HDMI bridge output. If you are not on a" >&2
    echo "         PS5, this installer will configure things you do not want." >&2
    read -r -p "Continue anyway? [y/N] " ans
    case "$ans" in
        y|Y|yes|YES) ;;
        *) echo "aborted"; exit 1 ;;
    esac
fi

mkdir -p "$HELPER_LIB"

# ───────────────────────────────────────────────────────────────────────────
# Dependencies — python only.
# Use pacman -S --needed (idempotent, no DB sync — explicitly NOT -Sy/-Syu
# because partial upgrades on CachyOS v3 repos can break glibc/pacman).
# ───────────────────────────────────────────────────────────────────────────
echo "ps5-autoedid-cachyos: checking dependencies..."
NEEDS=""
command -v python3 >/dev/null 2>&1 || NEEDS="$NEEDS python"
if [ -n "$NEEDS" ]; then
    echo "  installing:$NEEDS"
    # shellcheck disable=SC2086
    pacman -S --needed --noconfirm $NEEDS
else
    echo "  python3 already present"
fi

# ───────────────────────────────────────────────────────────────────────────
# Install monitor_db.py
# ───────────────────────────────────────────────────────────────────────────
cp "$SCRIPT_DIR/monitor_db.py" "$HELPER_DB"
echo "ps5-autoedid-cachyos: installed monitor_db.py -> $HELPER_DB"

# ───────────────────────────────────────────────────────────────────────────
# Write the systemd-time helper (mirror of Ubuntu version, identical EDID
# logic, runs early on EVERY boot in case the initramfs hook was skipped).
# ───────────────────────────────────────────────────────────────────────────
echo "ps5-autoedid-cachyos: writing $HELPER..."
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

log "=== ps5-autoedid (cachyos) run starting ==="

# sysfs files report size=0 via stat; wc -c gives the truth
for i in $(seq 1 30); do
    [ "$(wc -c < "$SYSFS_EDID" 2>/dev/null || echo 0)" -gt 0 ] && break
    sleep 0.1
done
if [ "$(wc -c < "$SYSFS_EDID" 2>/dev/null || echo 0)" -eq 0 ]; then
    log "sysfs EDID empty after 3s — leaving existing auto.bin in place"
    exit 0
fi

RAW=$(mktemp)
cp "$SYSFS_EDID" "$RAW"
log "live EDID: $(wc -c < "$RAW") bytes, sha256=$(sha256sum "$RAW" | cut -d' ' -f1)"

python3 - "$RAW" "$FW_FILE" "$DB" <<'PY_EOF'
import sys, os, hashlib

src, dst, db_path = sys.argv[1], sys.argv[2], sys.argv[3]

# ── PS5 kernel mode gate (ps5/hdmi.c isHdmiModeValid, kernel 7.0.5) ─────────
#   VIC 16  — 1920x1080 @ 60Hz   (pclk=148500kHz)
#   VIC 63  — 1920x1080 @ 120Hz  (pclk=297000kHz)  ← injectable
#   VIC 97  — 3840x2160 @ 60Hz   (pclk=594000kHz)
#   VIC 118 — 3840x2160 @ 120Hz  (in gate, but pclk overflows DTD field)
#   1440p (2560x1440): clock in {241500, 241700, 497750, 592250}
# ────────────────────────────────────────────────────────────────────────────

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
        print(f"WARNING: monitor_db load failed: {e} — defaulting to 60Hz")
else:
    print(f"monitor_db not found at {db_path} — defaulting to 60Hz")

data = open(src, 'rb').read()
if len(data) < 128:
    print(f"FAIL: EDID is {len(data)} bytes, need >=128")
    sys.exit(2)

# PS5 MN864739 byte-0 quirk fix
if data[:8] != bytes([0,255,255,255,255,255,255,0]):
    if data[0] == 0x01 and data[1:8] == bytes([255]*7):
        data = b'\x00' + data[1:]
        print("note: fixed PS5 byte-0 quirk")
    else:
        print(f"FAIL: bad EDID magic: {data[:8].hex()}")
        sys.exit(3)

base = bytearray(data[:128])

# Identity + name
mfr_raw = (base[8] << 8) | base[9]
mfr = ''.join(chr(((mfr_raw >> shift) & 0x1F) + ord('A') - 1) for shift in (10, 5, 0))
product = (base[11] << 8) | base[10]
print(f"EDID identity: mfr={mfr} product=0x{product:04x}")

model_name = ""
for offset in (54, 72, 90, 108):
    block = base[offset:offset+18]
    if block[0]==0 and block[1]==0 and block[2]==0 and block[3]==0xFC:
        model_name = block[5:18].decode('ascii', errors='replace').rstrip('\n').strip()
        print(f"monitor name descriptor: '{model_name}'")
        break

db_hz = 60
if get_max_hdmi_hz and (model_name or product):
    db_hz = get_max_hdmi_hz(mfr, model_name, product)
    if db_hz != 60:
        print(f"DB MATCH: {mfr} '{model_name}' pid=0x{product:04x} -> hdmi_hz={db_hz}")

native_w = base[56] | ((base[58] & 0xF0) << 4)
native_h = base[59] | ((base[61] & 0xF0) << 4)
print(f"native resolution from DTD: {native_w}x{native_h}")

# ── PASSTHROUGH GUARD ──────────────────────────────────────────────────────
# If the monitor's native preferred mode already passes the PS5 kernel gate,
# write the original EDID through verbatim (all blocks incl. CTA extension)
# so centering / IT-Content / no-underscan flags are preserved.
def native_pref_passes_gate(b):
    pclk = int.from_bytes(b[54:56], 'little') * 10  # kHz
    h_act = b[56] | ((b[58] & 0xF0) << 4)
    v_act = b[59] | ((b[61] & 0xF0) << 4)
    if h_act == 2560 and v_act == 1440:
        return pclk in (241500, 241700, 497750, 592250), f"1440p clock={pclk}"
    GATE = {
        (1920, 1080, 148500): 16,   # 1080p60
        (1920, 1080, 297000): 63,   # 1080p120
        (3840, 2160, 594000): 97,   # 4K60
        (3840, 2160, 1188000): 118, # 4K120 (clock overflows EDID DTD, but in gate)
    }
    vic = GATE.get((h_act, v_act, pclk))
    if vic is not None:
        return True, f"VIC {vic} ({h_act}x{v_act} pclk={pclk})"
    return False, f"{h_act}x{v_act} pclk={pclk} (no gate match)"

passes, why = native_pref_passes_gate(base)
if passes:
    sha = hashlib.sha256(data).hexdigest()
    tmp = dst + '.tmp'
    open(tmp, 'wb').write(data)
    os.replace(tmp, dst)
    print(f"PASSTHROUGH: native mode passes gate [{why}] — "
          f"writing native EDID untouched ({len(data)}b, ext_blocks={base[126]}, sha256={sha})")
    sys.exit(0)
else:
    print(f"REWRITE: native rejected [{why}] — normalizing")

# ── Normalize 60Hz DTD pclk to nearest kernel-accepted value ───────────────
PCLK_60HZ = [148500, 241500, 241700, 297000, 594000]
pclk_raw = int.from_bytes(base[54:56], 'little') * 10
nearest = min(PCLK_60HZ, key=lambda v: abs(v - pclk_raw))
if pclk_raw != nearest:
    nr = nearest // 10
    base[54] = nr & 0xFF
    base[55] = (nr >> 8) & 0xFF
    print(f"normalized DTD pclk: {pclk_raw} -> {nearest} kHz")

# Strip extension blocks — MN864739 fails with CTA ext block present in rewrite path
base[126] = 0

def checksum(buf):
    s = sum(buf[:127]) & 0xFF
    buf[127] = (256 - s) & 0xFF
    return buf

base = checksum(base)

# ── 120Hz DTD injection — 1080p ONLY (the only kernel-gated 120Hz path) ────
if db_hz >= 120 and native_w == 1920 and native_h == 1080:
    # VIC 63: 1920x1080@120 — pclk=297000kHz, h_total=2200, v_total=1125
    H_ACT, H_BLK = 1920, 280
    V_ACT, V_BLK = 1080, 45
    H_FP, H_SW = 88, 44
    V_FP, V_SW = 4, 5
    PCLK = 297000

    pr = PCLK // 10
    dtd = bytearray(18)
    dtd[0]  = pr & 0xFF
    dtd[1]  = (pr >> 8) & 0xFF
    dtd[2]  = H_ACT & 0xFF
    dtd[3]  = H_BLK & 0xFF
    dtd[4]  = ((H_ACT >> 4) & 0xF0) | ((H_BLK >> 8) & 0x0F)
    dtd[5]  = V_ACT & 0xFF
    dtd[6]  = V_BLK & 0xFF
    dtd[7]  = ((V_ACT >> 4) & 0xF0) | ((V_BLK >> 8) & 0x0F)
    dtd[8]  = H_FP & 0xFF
    dtd[9]  = H_SW & 0xFF
    dtd[10] = ((V_FP & 0x0F) << 4) | (V_SW & 0x0F)
    dtd[11] = ((H_FP >> 8) & 0x03) << 6 | ((H_SW >> 8) & 0x03) << 4 | \
              ((V_FP >> 4) & 0x03) << 2 | ((V_SW >> 4) & 0x03)
    dtd[12] = base[66]; dtd[13] = base[67]; dtd[14] = base[68]
    dtd[15] = 0; dtd[16] = 0; dtd[17] = 0x1E

    orig_dtd = bytes(base[54:72])
    base[54:72] = dtd
    base[72:90] = orig_dtd
    base = checksum(base)
    print(f"INJECTED VIC 63 (1080p@120Hz): slot0=120Hz slot1={nearest}kHz")

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

log "wrote $FW_FILE sha256=$(sha256sum "$FW_FILE" | cut -d' ' -f1)"

# Re-trigger DRM detect so the kernel re-reads the new firmware-supplied EDID
if [ -w "$SYSFS_FORCE" ]; then
    echo detect > "$SYSFS_FORCE" 2>/dev/null && log "triggered DRM detect"
fi
command -v udevadm >/dev/null && udevadm trigger --subsystem-match=drm --action=change >/dev/null 2>&1 || true

log "=== ps5-autoedid (cachyos) run complete ==="
HELPER_EOF

chmod +x "$HELPER"

# ───────────────────────────────────────────────────────────────────────────
# systemd unit — runs Before display-manager, in case the initramfs hook
# was skipped (e.g. someone rebuilt initramfs without our hook).
# ───────────────────────────────────────────────────────────────────────────
echo "ps5-autoedid-cachyos: writing systemd unit..."
cat > "$UNIT" <<'UNIT_EOF'
[Unit]
Description=PS5 auto-EDID normalizer (CachyOS / Arch)
DefaultDependencies=no
After=systemd-tmpfiles-setup.service
Before=display-manager.service graphical.target multi-user.target
ConditionPathExists=/sys/class/drm/card0-DP-1

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/ps5-autoedid
RemainAfterExit=yes
TimeoutStartSec=10s

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable ps5-autoedid.service

# ───────────────────────────────────────────────────────────────────────────
# mkinitcpio hooks — the REAL fix lives here.
# install hook: tells mkinitcpio what to copy into the initramfs image
# runtime hook: runs at boot inside the initramfs, BEFORE amdgpu probes
# ───────────────────────────────────────────────────────────────────────────
echo "ps5-autoedid-cachyos: writing mkinitcpio install hook..."
mkdir -p /etc/initcpio/install /etc/initcpio/hooks

cat > "$MKINIT_INSTALL_HOOK" <<'INSTALL_EOF'
#!/bin/bash
# /etc/initcpio/install/ps5-autoedid
build() {
    # Tools the runtime hook needs
    add_binary /usr/bin/python3
    add_binary /usr/bin/sha256sum
    add_binary /usr/bin/dd
    add_binary /usr/bin/cp
    add_binary /usr/bin/mktemp
    add_binary /usr/bin/wc
    add_binary /usr/bin/sleep
    add_binary /usr/bin/cat
    add_binary /usr/bin/mkdir

    # Monitor DB (so we know which monitors support 120Hz)
    if [ -f /usr/local/lib/ps5-autoedid/monitor_db.py ]; then
        add_file /usr/local/lib/ps5-autoedid/monitor_db.py
    fi

    # The runtime hook itself
    add_runscript
}
help() {
    cat <<HELP
This hook captures the PS5 HDMI bridge's sysfs EDID at the earliest boot
stage, normalizes it past the ps5-linux kernel's isHdmiModeValid() gate, and
writes /run/firmware/edid/auto.bin so amdgpu picks it up via the
drm.edid_firmware= kernel cmdline override. Without this hook, monitors with
non-gate-compatible native modes black-screen at boot.
HELP
}
INSTALL_EOF
chmod +x "$MKINIT_INSTALL_HOOK"

echo "ps5-autoedid-cachyos: writing mkinitcpio runtime hook..."
cat > "$MKINIT_RUNTIME_HOOK" <<'RUNTIME_EOF'
#!/usr/bin/ash
# /etc/initcpio/hooks/ps5-autoedid
# Runs inside the initramfs, BEFORE amdgpu loads. Captures sysfs EDID, runs
# the same passthrough-or-normalize Python logic the systemd helper uses,
# and writes /run/firmware/edid/auto.bin where the kernel firmware_class
# path picks it up.

run_hook() {
    msg ":: PS5 auto-EDID: capturing sysfs EDID"

    SYSFS=/sys/class/drm/card0-DP-1/edid
    OUT_DIR=/run/firmware/edid
    OUT=$OUT_DIR/auto.bin
    DB=/usr/local/lib/ps5-autoedid/monitor_db.py

    mkdir -p $OUT_DIR

    # Wait up to 2s for sysfs to populate (size reports 0 — use wc -c)
    i=0
    while [ $i -lt 20 ]; do
        SIZE=$(wc -c < $SYSFS 2>/dev/null || echo 0)
        [ $SIZE -gt 0 ] && break
        sleep 0.1
        i=$((i+1))
    done

    SIZE=$(wc -c < $SYSFS 2>/dev/null || echo 0)
    if [ $SIZE -eq 0 ]; then
        msg ":: PS5 auto-EDID: sysfs empty after 2s — skipping"
        return 0
    fi

    cp $SYSFS /run/edid-live.bin

    if [ -x /usr/bin/python3 ]; then
        /usr/bin/python3 - /run/edid-live.bin $OUT $DB <<'PY'
import sys, os
src, dst, db_path = sys.argv[1], sys.argv[2], sys.argv[3]

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

mfr_raw = (base[8] << 8) | base[9]
mfr = ''.join(chr(((mfr_raw >> shift) & 0x1F) + ord('A') - 1) for shift in (10, 5, 0))

model_name = ""
for offset in (54, 72, 90, 108):
    block = base[offset:offset+18]
    if block[0]==0 and block[1]==0 and block[2]==0 and block[3]==0xFC:
        model_name = block[5:18].decode('ascii', errors='replace').rstrip('\n').strip()
        break

product = (base[11] << 8) | base[10]
db_hz = 60
if get_max_hdmi_hz and (model_name or product):
    db_hz = get_max_hdmi_hz(mfr, model_name, product)

native_w = base[56] | ((base[58] & 0xF0) << 4)
native_h = base[59] | ((base[61] & 0xF0) << 4)

def passes_gate(b):
    pclk = int.from_bytes(b[54:56], 'little') * 10
    h_act = b[56] | ((b[58] & 0xF0) << 4)
    v_act = b[59] | ((b[61] & 0xF0) << 4)
    if h_act == 2560 and v_act == 1440:
        return pclk in (241500, 241700, 497750, 592250)
    GATE = {(1920, 1080, 148500), (1920, 1080, 297000),
            (3840, 2160, 594000), (3840, 2160, 1188000)}
    return (h_act, v_act, pclk) in GATE

if passes_gate(base):
    # PASSTHROUGH: native EDID is gate-compatible — write through untouched
    tmp = dst + '.tmp'
    open(tmp, 'wb').write(data)
    os.replace(tmp, dst)
    sys.exit(0)

# REWRITE: normalize pclk + strip extensions
PCLK_60 = [148500, 241500, 241700, 297000, 594000]
pclk_raw = int.from_bytes(base[54:56], 'little') * 10
nearest = min(PCLK_60, key=lambda v: abs(v - pclk_raw))
nr = nearest // 10
base[54] = nr & 0xFF
base[55] = (nr >> 8) & 0xFF
base[126] = 0

def checksum(buf):
    s = sum(buf[:127]) & 0xFF
    buf[127] = (256 - s) & 0xFF
    return buf

base = checksum(base)

# Inject VIC 63 (1080p@120) only — kernel-gated 120Hz path
if db_hz >= 120 and native_w == 1920 and native_h == 1080:
    dtd = bytearray(18)
    dtd[0]=0x04; dtd[1]=0x74
    dtd[2]=0x80; dtd[3]=0x18; dtd[4]=0x71
    dtd[5]=0x38; dtd[6]=0x2D; dtd[7]=0x40
    dtd[8]=0x58; dtd[9]=0x2C; dtd[10]=0x45; dtd[11]=0x00
    dtd[12]=base[66]; dtd[13]=base[67]; dtd[14]=base[68]
    dtd[15]=0; dtd[16]=0; dtd[17]=0x1E
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
        # Python missing in initramfs — write raw EDID and let amdgpu try
        dd if=/run/edid-live.bin of=$OUT bs=128 count=1 status=none
        RC=0
    fi

    if [ $RC -eq 0 ]; then
        msg ":: PS5 auto-EDID: wrote $OUT"
    else
        msg ":: PS5 auto-EDID: normalize failed (rc=$RC)"
    fi
}
RUNTIME_EOF
chmod +x "$MKINIT_RUNTIME_HOOK"

# ───────────────────────────────────────────────────────────────────────────
# Patch /etc/mkinitcpio.conf:
#   * Add 'ps5-autoedid' to HOOKS (after 'kms')
#   * Remove 'autodetect' (verified PS5-Linux requirement — autodetect can
#     strip amdgpu since it may not be loaded at build time)
#   * Force MODULES=(amdgpu)
#   * Bake /lib/firmware/edid/auto.bin into FILES (so even if the runtime
#     hook fails, a previously-good EDID is still present)
# ───────────────────────────────────────────────────────────────────────────
CONF=/etc/mkinitcpio.conf
[ -f "$CONF" ] || { echo "ERROR: $CONF missing" >&2; exit 1; }
cp "$CONF" "${CONF}.bak.$(date +%s)"
echo "ps5-autoedid-cachyos: patching $CONF (backup created)"

# 1) MODULES: ensure amdgpu present
if grep -qE '^MODULES=\([^)]*amdgpu' "$CONF"; then
    : # already present
elif grep -qE '^MODULES=\(' "$CONF"; then
    # add amdgpu to existing MODULES line
    sed -i -E 's/^(MODULES=\()(.*)(\))/\1\2 amdgpu\3/' "$CONF"
else
    echo 'MODULES=(amdgpu)' >> "$CONF"
fi
# Clean up any leading space if MODULES was empty
sed -i -E 's/^MODULES=\( +/MODULES=(/' "$CONF"

# 2) HOOKS: drop autodetect (PS5-Linux requirement), add ps5-autoedid after kms
sed -i -E 's/(^HOOKS=\([^)]*) autodetect/\1/' "$CONF"
sed -i -E 's/(^HOOKS=\([^)]*)autodetect /\1/' "$CONF"
if ! grep -qE '^HOOKS=\([^)]*ps5-autoedid' "$CONF"; then
    if grep -qE '^HOOKS=\([^)]*kms' "$CONF"; then
        sed -i -E 's/(^HOOKS=\([^)]*kms)/\1 ps5-autoedid/' "$CONF"
    else
        # No kms — append at end (still runs early enough vs filesystems)
        sed -i -E 's/(^HOOKS=\([^)]*)(\))/\1 ps5-autoedid\2/' "$CONF"
    fi
fi
# Collapse any double-spaces
sed -i -E 's/  +/ /g' "$CONF"

# 3) FILES: include the EDID firmware path so it's baked in
EDID_PATH=/lib/firmware/edid/auto.bin
if grep -qE '^FILES=\([^)]*auto\.bin' "$CONF"; then
    : # already there
elif grep -qE '^FILES=\(\)' "$CONF"; then
    sed -i -E "s|^FILES=\\(\\)|FILES=($EDID_PATH)|" "$CONF"
elif grep -qE '^FILES=\(' "$CONF"; then
    sed -i -E "s|^(FILES=\\()(.*)(\\))|\\1\\2 $EDID_PATH\\3|" "$CONF"
else
    echo "FILES=($EDID_PATH)" >> "$CONF"
fi
sed -i -E 's/^FILES=\( +/FILES=(/' "$CONF"

# Ensure /lib/firmware/edid exists so initial build doesn't warn
mkdir -p /lib/firmware/edid
[ -f /lib/firmware/edid/auto.bin ] || dd if=/dev/zero of=/lib/firmware/edid/auto.bin bs=128 count=1 status=none

echo "ps5-autoedid-cachyos: mkinitcpio.conf now:"
grep -E '^(MODULES|HOOKS|FILES)=' "$CONF" | sed 's/^/    /'

# ───────────────────────────────────────────────────────────────────────────
# Run the helper now to capture the current monitor's EDID into auto.bin,
# so the very next initramfs build bakes a real one in.
# ───────────────────────────────────────────────────────────────────────────
echo "ps5-autoedid-cachyos: running first EDID capture..."
"$HELPER" && echo "  capture OK" || echo "  capture noted issues — check $LOG"

# ───────────────────────────────────────────────────────────────────────────
# Regenerate the initramfs and deploy it per the PS5-Linux kexec contract.
#   * mkinitcpio writes /boot/initramfs-<preset>.img
#   * We deploy a copy to /boot/efi/initrd-cachyos.img (what kexec reads)
# ───────────────────────────────────────────────────────────────────────────
echo "ps5-autoedid-cachyos: regenerating initramfs..."
# Latest installed kernel (newest entry in /lib/modules)
KVER=$(ls -1t /lib/modules 2>/dev/null | head -1)
if [ -z "$KVER" ]; then
    echo "ERROR: no kernels found in /lib/modules" >&2
    exit 1
fi
echo "  kernel: $KVER"

# Build directly to a known path (avoids preset mismatches)
GEN_INITRD="/boot/initrd.img-$KVER"
mkinitcpio -k "$KVER" -g "$GEN_INITRD"
echo "  built: $GEN_INITRD"

# Also drop a copy where PS5-Linux's per-distro kexec script expects it
cp "$GEN_INITRD" "$INITRD_TARGET"
echo "  deployed: $INITRD_TARGET"

# ───────────────────────────────────────────────────────────────────────────
# Kernel cmdline — add drm.edid_firmware override + firmware_class path.
# If /boot/efi/cmdline-cachyos.txt already exists, back it up and edit it.
# If not, derive from /boot/efi/cmdline.txt (the Ubuntu default) and rewrite
# the root= label for CachyOS conventions (user can adjust post-install).
# ───────────────────────────────────────────────────────────────────────────
if [ -f "$CMDLINE_FILE" ]; then
    [ -f "$CMDLINE_BAK" ] || cp "$CMDLINE_FILE" "$CMDLINE_BAK"
    BASE=$(tr -d '\n' < "$CMDLINE_BAK")
    echo "ps5-autoedid-cachyos: editing existing $CMDLINE_FILE (backup: $CMDLINE_BAK)"
elif [ -f "$BOOT/cmdline.txt.tv" ]; then
    BASE=$(tr -d '\n' < "$BOOT/cmdline.txt.tv")
    echo "ps5-autoedid-cachyos: seeding $CMDLINE_FILE from $BOOT/cmdline.txt.tv"
elif [ -f "$BOOT/cmdline.txt" ]; then
    BASE=$(tr -d '\n' < "$BOOT/cmdline.txt")
    echo "ps5-autoedid-cachyos: seeding $CMDLINE_FILE from $BOOT/cmdline.txt"
else
    BASE="rw rootwait console=ttyTitania0 console=tty0 mitigations=off idle=halt preempt=full"
    echo "ps5-autoedid-cachyos: no template found — using minimal default"
    echo "  WARN: root= label not set — edit $CMDLINE_FILE before booting" >&2
fi

# External-SSD vs internal NVMe — defer amdgpu autoload only for non-NVMe roots.
# This was the V7 regression on Ubuntu (stripped rootwait); we keep rootwait
# unconditionally here and only add rootdelay for non-NVMe roots.
ROOT_DEV=$(findmnt -no SOURCE / 2>/dev/null || true)
BOOT_FIXES=""
case "$ROOT_DEV" in
    /dev/nvme*)
        echo "ps5-autoedid-cachyos: NVMe root ($ROOT_DEV) — no rootdelay needed"
        ;;
    *)
        BOOT_FIXES="rootdelay=3 modprobe.blacklist=amdgpu"
        mkdir -p /etc/modules-load.d
        echo amdgpu > /etc/modules-load.d/amdgpu-late.conf
        echo "ps5-autoedid-cachyos: non-NVMe root ($ROOT_DEV) — deferring amdgpu autoload"
        ;;
esac

# Strip any prior MeatHandler/Ubuntu-installer tokens and append our own
NEW=$(echo "$BASE" | tr ' ' '\n' \
    | grep -v '^drm\.edid_firmware=' \
    | grep -v '^video=DP-1' \
    | grep -v '^amdgpu\.force_1080p=' \
    | grep -v '^firmware_class\.path=' \
    | grep -v '^rootdelay=' \
    | grep -v '^module_blacklist=' \
    | grep -v '^modprobe\.blacklist=' \
    | tr '\n' ' ' | sed 's/  */ /g; s/ $//')

# CRITICAL: keep rootwait. If for some reason it isn't in BASE, add it.
if ! echo "$NEW" | grep -qw 'rootwait'; then
    NEW="$NEW rootwait"
fi

NEW="$NEW drm.edid_firmware=DP-1:edid/auto.bin video=DP-1:e firmware_class.path=/lib/firmware:/run/firmware snd_hda_intel.enable_dp_mst=0"
[ -n "$BOOT_FIXES" ] && NEW="$NEW $BOOT_FIXES"
NEW=$(echo "$NEW" | sed 's/  */ /g; s/ $//')

echo "$NEW" > "$CMDLINE_FILE"
echo "ps5-autoedid-cachyos: wrote $CMDLINE_FILE"
echo "  new cmdline: $NEW"

# Mirror auto.bin into /boot/efi/edid as well, as a belt-and-suspenders for
# the firmware_class.path search (some loader configs read it from there).
mkdir -p "$BOOT/edid"
cp /lib/firmware/edid/auto.bin "$BOOT/edid/auto.bin"

# ───────────────────────────────────────────────────────────────────────────
# Recovery script — drops a safe-boot helper to revert to baseline.
# ───────────────────────────────────────────────────────────────────────────
SAFE="$BOOT/safe-boot-cachyos.sh"
cat > "$SAFE" <<'SAFE_EOF'
#!/bin/sh
# safe-boot-cachyos.sh — revert ps5-autoedid (cachyos) changes from the
# FAT32 boot partition. Run when the screen is dead post-install:
#     sudo /boot/efi/safe-boot-cachyos.sh
set -e
BOOT=/boot/efi
if [ -f "$BOOT/cmdline-cachyos.txt.bak" ]; then
    cp "$BOOT/cmdline-cachyos.txt.bak" "$BOOT/cmdline-cachyos.txt"
    echo "safe-boot-cachyos: restored cmdline-cachyos.txt from backup"
fi
sync
echo "safe-boot-cachyos: done. Reboot."
SAFE_EOF
chmod +x "$SAFE"
echo "ps5-autoedid-cachyos: installed recovery -> $SAFE"

echo ""
echo "=== ps5-autoedid-cachyos: DONE ==="
echo "  Verify: cat $LOG"
echo "  Recover: sudo $SAFE"
echo "  cmdline: $CMDLINE_FILE"
echo "  initrd:  $INITRD_TARGET"
echo ""
echo "Reboot to apply. On boot the initramfs hook will normalize the EDID"
echo "before amdgpu loads — no more black-screen-on-display-init."
