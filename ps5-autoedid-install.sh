#!/bin/bash
# ps5-autoedid-install.sh — installs the auto-EDID hook into the M.2 rootfs.
set -e

BOOT=/boot/efi
HELPER=/usr/local/sbin/ps5-autoedid
UNIT=/etc/systemd/system/ps5-autoedid.service
LOG=/var/log/ps5-autoedid.log

if [ "$(id -u)" -ne 0 ]; then
    echo "must be run as root: sudo bash $0" >&2
    exit 1
fi

echo "ps5-autoedid-install: writing $HELPER..."

cat > "$HELPER" <<'HELPER_EOF'
#!/bin/bash
set -u
LOG=/var/log/ps5-autoedid.log
SYSFS_EDID=/sys/class/drm/card0-DP-1/edid
SYSFS_FORCE=/sys/class/drm/card0-DP-1/force
FW_DIR=/lib/firmware/edid
FW_FILE="$FW_DIR/auto.bin"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

mkdir -p "$FW_DIR"
exec >> "$LOG" 2>&1

log "=== ps5-autoedid run starting ==="

for i in $(seq 1 30); do
    if [ -s "$SYSFS_EDID" ]; then break; fi
    sleep 0.1
done
if [ ! -s "$SYSFS_EDID" ]; then
    log "sysfs EDID empty after 3s — leaving existing auto.bin in place"
    exit 0
fi

RAW=$(mktemp)
cp "$SYSFS_EDID" "$RAW"
RAW_SIZE=$(stat -c%s "$RAW")
RAW_SHA=$(sha256sum "$RAW" | cut -d' ' -f1)
log "live EDID: $RAW_SIZE bytes, sha256=$RAW_SHA"

python3 - "$RAW" "$FW_FILE" <<'PY_EOF'
import sys, struct, os, hashlib
src, dst = sys.argv[1], sys.argv[2]
data = open(src,'rb').read()
if len(data) < 128:
    print(f"FAIL: EDID is {len(data)} bytes, need >=128")
    sys.exit(2)
if data[:8] != bytes([0,255,255,255,255,255,255,0]):
    if data[0] == 0x01 and data[1:8] == bytes([255,255,255,255,255,255,0]):
        data = b'\x00' + data[1:]
        print("note: fixed PS5 byte-0 quirk")
    else:
        print(f"FAIL: bad EDID magic: {data[:8].hex()}")
        sys.exit(3)

base = bytearray(data[:128])
WHITELIST = [148500, 241500, 241700, 297000, 594000]
pclk = int.from_bytes(base[54:56], 'little') * 10
nearest = min(WHITELIST, key=lambda v: abs(v - pclk))
if pclk != nearest:
    new_pclk_raw = nearest // 10
    base[54] = new_pclk_raw & 0xFF
    base[55] = (new_pclk_raw >> 8) & 0xFF
    print(f"normalized DTD pclk: {pclk} -> {nearest} kHz")
base[126] = 0
s = sum(base[:127]) & 0xFF
base[127] = (256 - s) & 0xFF
if sum(base) % 256 != 0:
    print("FAIL: checksum did not converge")
    sys.exit(5)
mfr_raw = (base[8] << 8) | base[9]
mfr = ''.join(chr(((mfr_raw >> shift) & 0x1F) + ord('A') - 1) for shift in (10, 5, 0))
product = (base[11] << 8) | base[10]
out_sha = hashlib.sha256(bytes(base)).hexdigest()
print(f"identity: {mfr} 0x{product:04x}")
print(f"final EDID: 128 bytes, ext=0, pclk={int.from_bytes(base[54:56],'little')*10} kHz, sha256={out_sha}")
tmp = dst + '.tmp'
open(tmp,'wb').write(bytes(base))
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

log "=== ps5-autoedid run complete ==="
HELPER_EOF

chmod +x "$HELPER"

echo "ps5-autoedid-install: writing $UNIT..."

cat > "$UNIT" <<'UNIT_EOF'
[Unit]
Description=PS5 Display Wizard — auto EDID normalizer
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

echo "ps5-autoedid-install: enabling unit..."
systemctl daemon-reload
systemctl enable ps5-autoedid.service

INITRAMFS_HOOK=/etc/initramfs-tools/hooks/ps5-autoedid
INITRAMFS_SCRIPT=/etc/initramfs-tools/scripts/init-top/ps5-autoedid

if [ -d /etc/initramfs-tools ]; then
    echo "ps5-autoedid-install: writing initramfs hook..."
    cat > "$INITRAMFS_HOOK" <<'HOOK_EOF'
#!/bin/sh
PREREQ=""
prereqs() { echo "$PREREQ"; }
case "$1" in prereqs) prereqs; exit 0 ;; esac
. /usr/share/initramfs-tools/hook-functions
copy_exec /usr/bin/sha256sum || true
copy_exec /bin/dd
copy_exec /usr/bin/python3 || true
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

mkdir -p "$OUT_DIR"
log() { echo "[initramfs] $*" >> "$LOG"; echo "ps5-autoedid: $*" > /dev/kmsg 2>/dev/null || true; }

i=0
while [ $i -lt 20 ]; do
    [ -s "$SYSFS_EDID" ] && break
    sleep 0.1
    i=$((i+1))
done

if [ ! -s "$SYSFS_EDID" ]; then
    log "no sysfs EDID after 2s — skipping"
    exit 0
fi

cp "$SYSFS_EDID" /run/edid-live.bin
log "live EDID: $(wc -c < /run/edid-live.bin) bytes"

if command -v python3 >/dev/null 2>&1; then
    python3 - /run/edid-live.bin "$OUT" <<'PY'
import sys, os
src, dst = sys.argv[1], sys.argv[2]
data = open(src,'rb').read()
if len(data) < 128: sys.exit(2)
if data[0] == 0x01 and data[1:8] == bytes([255]*7):
    data = b'\x00' + data[1:]
base = bytearray(data[:128])
WHITELIST = [148500, 241500, 241700, 297000, 594000]
pclk = int.from_bytes(base[54:56], 'little') * 10
nearest = min(WHITELIST, key=lambda v: abs(v - pclk))
new_raw = nearest // 10
base[54] = new_raw & 0xFF
base[55] = (new_raw >> 8) & 0xFF
base[126] = 0
s = sum(base[:127]) & 0xFF
base[127] = (256 - s) & 0xFF
tmp = dst + '.tmp'
open(tmp,'wb').write(bytes(base))
os.replace(tmp, dst)
PY
    RC=$?
else
    dd if=/run/edid-live.bin of="$OUT" bs=128 count=1 status=none
    RC=0
    log "WARN: python3 not in initramfs — used dd fallback"
fi

[ $RC -eq 0 ] && log "wrote $OUT" || log "normalize failed rc=$RC"
INIT_EOF
    chmod +x "$INITRAMFS_SCRIPT"

    echo "ps5-autoedid-install: regenerating initramfs..."
    update-initramfs -u

    if [ -f /boot/initrd.img ]; then
        cp /boot/initrd.img "$BOOT/initrd.img"
        echo "  copied new initrd -> $BOOT/initrd.img"
    fi
fi

# Update cmdline.txt to point at auto.bin
CMDLINE="$BOOT/cmdline.txt"
CMDLINE_BAK="$BOOT/cmdline.txt.tv"
if [ -f "$CMDLINE" ] && [ -f "$CMDLINE_BAK" ]; then
    BASE=$(tr -d '\n' < "$CMDLINE_BAK")
    NEW=$(echo "$BASE" | tr ' ' '\n' | grep -v '^drm\.edid_firmware=' | grep -v '^video=DP-1' | grep -v '^amdgpu\.force_1080p=' | grep -v '^firmware_class\.path=' | tr '\n' ' ' | sed 's/  */ /g; s/ $//')
    NEW="$NEW drm.edid_firmware=DP-1:edid/auto.bin video=DP-1:e firmware_class.path=/run/firmware snd_hda_intel.enable_dp_mst=0"
    echo "$NEW" > "$CMDLINE"
    echo "ps5-autoedid-install: updated $CMDLINE -> auto.bin"
    echo "  new cmdline: $NEW"
fi

echo "ps5-autoedid-install: running first EDID capture now..."
"$HELPER" && echo "  capture OK" || echo "  capture noted issues — check $LOG"

if [ -f /lib/firmware/edid/auto.bin ]; then
    mkdir -p "$BOOT/edid"
    cp /lib/firmware/edid/auto.bin "$BOOT/edid/auto.bin"
    echo "  mirrored auto.bin -> $BOOT/edid/auto.bin"
fi

echo ""
echo "=== ps5-autoedid-install: DONE ==="
echo "  Verify: cat $LOG"
echo "  Recover: sudo $BOOT/safe-boot.sh"
