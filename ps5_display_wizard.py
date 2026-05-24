#!/usr/bin/env python3
"""PS5 Display Wizard — Linux GUI.

Read a connected monitor's EDID, strip it to a converter-safe form (or
generate a synthetic one for a chosen mode), and bake it into the PS5
Linux loader USB. See README.md for the full picture.

Only writes to: <USB>/cmdline.txt, <USB>/initrd.img, <USB>/edid/, plus
the pre-bake backups <USB>/cmdline.txt.tv and <USB>/initrd.img.tv. The
host system's own boot partition is never touched.

Headless modes:
    python3 ps5_display_wizard.py --scan
    python3 ps5_display_wizard.py --list-usb
"""

import os
import re
import sys
import json
import time
import shutil
import hashlib
import struct
import subprocess
from pathlib import Path

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib, Pango  # noqa: E402
except (ImportError, ValueError) as e:
    msg = (
        "This wizard needs PyGObject + GTK 3 bindings, which are missing.\n"
        "\n"
        "Install on Debian/Ubuntu:\n"
        "    sudo apt install python3-gi gir1.2-gtk-3.0\n"
        "\n"
        "Install on Fedora:\n"
        "    sudo dnf install python3-gobject gtk3\n"
        "\n"
        "Install on Arch:\n"
        "    sudo pacman -S python-gobject gtk3\n"
        "\n"
        f"Underlying error: {e}\n"
    )
    print(msg, file=sys.stderr)
    sys.exit(1)

APP = "ps5-display-wizard"
VERSION = "0.1"
CONFIG_DIR = Path(os.path.expanduser(f"~/.config/{APP}"))
HISTORY_FILE = CONFIG_DIR / "monitors.json"


# ────────────────────────────────────────────────────────────────────────
# EDID acquisition + parsing
# ────────────────────────────────────────────────────────────────────────

def scan_connected_monitors(force_redetect=True):
    """Return list of {connector, edid_bytes, info} for every connected
    DRM display on this host. Linux-only: reads /sys/class/drm.

    `force_redetect` (default True): write "detect" to each connector's
    /sys/class/drm/<conn>/force file before reading. This forces the kernel
    to re-probe the connector over DDC and refresh its cached EDID. Without
    this, hot-swapping a monitor on the PS5 (where the MN864739 converter
    blocks HPD events) leaves sysfs holding the previous monitor's EDID.
    Requires root; silently skipped on permission error.
    """
    out = []
    drm_root = Path("/sys/class/drm")
    if not drm_root.is_dir():
        return out

    # Step 1: trigger a fresh HPD probe on every connector. Writing "detect"
    # to <connector>/force tells the kernel to re-read EDID from the
    # monitor over DDC. Best-effort — needs CAP_SYS_ADMIN; on failure we
    # fall back to whatever sysfs currently has.
    if force_redetect:
        for entry in drm_root.iterdir():
            name = entry.name
            if not name.startswith("card") or "-" not in name:
                continue
            force_f = entry / "force"
            if not force_f.is_file():
                continue
            try:
                # "detect" = re-run probe; "on"/"off" force connector state.
                force_f.write_text("detect")
            except OSError:
                pass  # not root or sysfs read-only; carry on with stale value
        # Give the kernel a beat to update its cached EDID after the probe.
        time.sleep(0.2)

    # Step 2: read each connected display's current EDID.
    for entry in sorted(drm_root.iterdir()):
        name = entry.name
        if not name.startswith("card") or "-" not in name:
            continue
        status_f = entry / "status"
        edid_f = entry / "edid"
        if not status_f.is_file() or not edid_f.is_file():
            continue
        try:
            status = status_f.read_text().strip()
        except OSError:
            continue
        if status != "connected":
            continue
        try:
            edid = edid_f.read_bytes()
        except OSError:
            continue
        if not edid or len(edid) < 128:
            continue
        info = parse_edid(edid)
        info["connector"] = name
        info["edid_sha256"] = hashlib.sha256(edid).hexdigest()
        out.append({"connector": name, "edid": edid, "info": info})
    return out


def parse_edid(data):
    """Extract user-facing fields from an EDID byte string."""
    info = {
        "manufacturer": "?",
        "model": "?",
        "serial_text": "",
        "primary_mode": "?",
        "primary_pclk_khz": 0,
        "size_bytes": len(data),
        "has_extension": False,
        "ext_count": 0,
        "has_hdr": False,
        "has_vrr": False,
        "max_refresh_hz": 0,
    }
    if len(data) < 128:
        return info
    if data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return info  # not a valid EDID header

    # Manufacturer ID (bytes 8-9, big-endian, 3 x 5-bit chars + A=1)
    raw = (data[8] << 8) | data[9]
    info["manufacturer"] = "".join([
        chr(((raw >> 10) & 0x1F) + ord("A") - 1),
        chr(((raw >> 5) & 0x1F) + ord("A") - 1),
        chr((raw & 0x1F) + ord("A") - 1),
    ])

    # Descriptor blocks: 4 x 18 bytes at offsets 54/72/90/108.
    # Type 0xFC = monitor name. Type 0xFF = serial text. Type 0xFD = range.
    for off in (54, 72, 90, 108):
        block = data[off:off + 18]
        if len(block) != 18:
            continue
        if block[0] == 0 and block[1] == 0 and block[2] == 0:
            tag = block[3]
            payload = block[5:].split(b"\n", 1)[0].rstrip()
            try:
                txt = payload.decode("ascii", "replace").strip()
            except Exception:
                txt = ""
            if tag == 0xFC and txt:
                info["model"] = txt
            elif tag == 0xFF and txt:
                info["serial_text"] = txt
            elif tag == 0xFD:
                # range descriptor: max vertical refresh at byte 5 (after type prefix bytes)
                vmax = block[6]
                info["max_refresh_hz"] = max(info["max_refresh_hz"], vmax)

    # Primary DTD = offset 54 if it's not a 00-00-00 descriptor
    dtd = data[54:72]
    if dtd[0] or dtd[1]:
        pclk_khz = ((dtd[1] << 8) | dtd[0]) * 10
        h_active = dtd[2] | ((dtd[4] & 0xF0) << 4)
        v_active = dtd[5] | ((dtd[7] & 0xF0) << 4)
        h_blank = dtd[3] | ((dtd[4] & 0x0F) << 8)
        v_blank = dtd[6] | ((dtd[7] & 0x0F) << 4)
        total = (h_active + h_blank) * (v_active + v_blank)
        refresh = (pclk_khz * 1000 / total) if (total and pclk_khz) else 0
        info["primary_mode"] = f"{h_active}x{v_active}@{refresh:.0f}Hz"
        info["primary_pclk_khz"] = pclk_khz
        if refresh > info["max_refresh_hz"]:
            info["max_refresh_hz"] = int(round(refresh))

    # Extension blocks (byte 126 in base = count)
    info["ext_count"] = data[126]
    info["has_extension"] = info["ext_count"] > 0

    # Scan extension blocks for HDR / VRR markers (CTA-861)
    for i in range(info["ext_count"]):
        ext_off = 128 * (i + 1)
        ext = data[ext_off:ext_off + 128]
        if len(ext) < 128 or ext[0] != 0x02:
            continue  # not CTA-861
        # DTD offset; data block collection runs from byte 4 to dtd_off
        dtd_off = ext[2]
        if dtd_off < 4:
            continue
        idx = 4
        while idx < dtd_off and idx < 128:
            tag_byte = ext[idx]
            block_len = tag_byte & 0x1F
            block_tag = (tag_byte & 0xE0) >> 5
            block = ext[idx + 1: idx + 1 + block_len]
            # Extended Tag = 7, then sub-tag at block[0]
            if block_tag == 7 and len(block) >= 1:
                subtag = block[0]
                if subtag == 6:  # HDR Static Metadata Data Block
                    info["has_hdr"] = True
                if subtag == 0:  # Video Capability (often has VRR-ish flags)
                    pass
                if subtag == 0x4D:  # VRR Data Block (proposed/various)
                    info["has_vrr"] = True
            idx += 1 + block_len
    return info


def strip_edid_to_minimal(edid):
    """Return a 128-byte EDID derived from `edid` with no extension blocks
    and a recomputed checksum. The primary DTD is preserved, so the
    monitor's native resolution is still advertised — but HDR10/BT.2020/
    12-bit/VRR/higher-refresh blocks (which live in extensions) are gone.
    This is the workaround that fixes the MN864739 converter."""
    if len(edid) < 128:
        raise ValueError("EDID too short")
    base = bytearray(edid[:128])
    base[126] = 0  # zero extension count
    s = sum(base[:127]) & 0xFF
    base[127] = (256 - s) & 0xFF
    return bytes(base)


# ─── Synthetic EDID builder ─────────────────────────────────────────────
# Mirrors the engine in ps5_display_wizard_mac.py — see that file for the
# full design rationale and CVT-RB v2 references.

# Pixel-clock whitelist enforced by isHdmiModeValid() in ps5-linux-patches.
# Any DTD pclk not on this list returns MODE_ERROR → black screen. See
# KNOWN_WORKING.md for the full whitelist (VIC 16/63/97 plus 1440p60 at
# exactly 241500 or 241700 kHz).
_CEA_60HZ_MODES = {
    (1280,  720,   60): ( 74250,  370, 110,  40,  30,  5,  5,  True,  True),  # CEA VIC 4
    (1920, 1080,   60): (148500,  280,  88,  44,  45,  4,  5,  True,  True),  # CEA VIC 16
    (1920, 1080,  120): (297000,  280,  88,  44,  45,  4,  5,  True,  True),  # CEA VIC 63 (kernel ≥7.0.3)
    (2560, 1440,   60): (241500,  160,  48,  32,  41,  3,  5,  True,  True),  # CEA VIC 110; pclk MUST be 241500
    (3840, 2160,   60): (594000,  560, 176,  88,  90,  8, 10,  True,  True),  # CEA VIC 97
}

# Only refresh rates the PS5 Linux kernel's isHdmiModeValid() accepts.
# 1440p120, 4K120, HDR, VRR require driver work that is not upstream yet.
RESOLUTION_REFRESH_OPTIONS = {
    # 720p removed: pclk 74250 is not in isHdmiModeValid's whitelist.
    ( 1920, 1080): [60, 120],
    ( 2560, 1440): [60],
    ( 3840, 2160): [60],
}


def _cvt_rb2_timings(width, height, refresh_hz):
    """VESA CVT 1.2 Annex B (Reduced Blanking v2) computation.
    Returns a 9-tuple matching _CEA_60HZ_MODES entries."""
    H_BLANK = 80; H_SYNC = 32; H_FRONT = 8
    V_SYNC = 8;  V_FRONT = 3
    MIN_VBLANK_USEC = 460
    MIN_VBLANK_LINES = 6
    htotal = width + H_BLANK
    vblank = MIN_VBLANK_LINES
    for _ in range(10):
        vtotal = height + vblank
        h_freq_hz = htotal * vtotal * refresh_hz / vtotal if vtotal else 0
        new_vblank = max(MIN_VBLANK_LINES,
                         int((MIN_VBLANK_USEC * h_freq_hz / 1_000_000) + 0.999))
        if new_vblank == vblank:
            break
        vblank = new_vblank
    vtotal = height + vblank
    pclk_khz = round(htotal * vtotal * refresh_hz / 1000)
    return (pclk_khz, H_BLANK, H_FRONT, H_SYNC, vblank, V_FRONT, V_SYNC, True, False)


def _resolve_timings(width, height, refresh_hz):
    key = (int(width), int(height), int(refresh_hz))
    if key in _CEA_60HZ_MODES:
        return _CEA_60HZ_MODES[key]
    return _cvt_rb2_timings(width, height, refresh_hz)


def build_synthetic_edid(width, height, refresh_hz, monitor_name=None,
                          identity_source=None):
    """Build a 128-byte synthetic EDID claiming exactly one display mode.

    `identity_source` — optional bytes() of a real scanned EDID. When given,
    bytes 8-17 (mfg, product, serial, week, year) are copied from it so
    Linux labels the display as the actual monitor."""
    pclk, hblank, hso, hsp, vblank, vso, vsp, h_pos, v_pos = \
        _resolve_timings(width, height, refresh_hz)
    pclk_10khz = pclk // 10
    if pclk_10khz > 0xFFFF:
        raise ValueError(
            f"{width}x{height}@{refresh_hz}Hz needs pclk={pclk} kHz which "
            f"exceeds the 655350 kHz DTD limit. Cannot fit in base-block EDID.")

    e = bytearray(128)
    e[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    def pnp(a, b, c):
        v = (((ord(a)-64)&0x1F)<<10) | (((ord(b)-64)&0x1F)<<5) | ((ord(c)-64)&0x1F)
        return bytes([(v>>8)&0xFF, v&0xFF])
    if identity_source and len(identity_source) >= 18 and \
            identity_source[:8] == b"\x00\xff\xff\xff\xff\xff\xff\x00":
        e[8:18] = identity_source[8:18]
    else:
        e[8:10] = pnp('W', 'Z', 'D')
        prod = ((refresh_hz & 0xFF) << 8) | ((height // 9) & 0xFF)
        e[10:12] = prod.to_bytes(2, "little")
        e[12:16] = b"\x00\x00\x00\x00"
        e[16] = 1; e[17] = 36
    e[18] = 1; e[19] = 4
    e[20] = 0x80 | (0x01 << 4) | 0x02
    if width <= 1280:
        e[21] = 35; e[22] = 20
    elif width <= 1920:
        e[21] = 53; e[22] = 30
    elif width <= 2560:
        e[21] = 60; e[22] = 34
    else:
        e[21] = 89; e[22] = 50
    e[23] = 120
    e[24] = 0x06 | 0x04
    e[25:35] = bytes([0xee, 0x95, 0xa3, 0x54, 0x4c, 0x99, 0x26, 0x0f, 0x50, 0x54])
    e[35] = 0x20; e[36] = 0x00; e[37] = 0x00
    for i in range(38, 54, 2):
        e[i] = 0x01; e[i+1] = 0x01
    e[54] = pclk_10khz & 0xFF
    e[55] = (pclk_10khz >> 8) & 0xFF
    e[56] = width & 0xFF
    e[57] = hblank & 0xFF
    e[58] = ((width >> 8) & 0x0F) << 4 | ((hblank >> 8) & 0x0F)
    e[59] = height & 0xFF
    e[60] = vblank & 0xFF
    e[61] = ((height >> 8) & 0x0F) << 4 | ((vblank >> 8) & 0x0F)
    e[62] = hso & 0xFF
    e[63] = hsp & 0xFF
    e[64] = ((vso & 0x0F) << 4) | (vsp & 0x0F)
    e[65] = (((hso >> 8) & 0x03) << 6) | (((hsp >> 8) & 0x03) << 4) | \
            (((vso >> 4) & 0x03) << 2) | ((vsp >> 4) & 0x03)
    h_mm = e[21] * 10
    v_mm = e[22] * 10
    e[66] = h_mm & 0xFF
    e[67] = v_mm & 0xFF
    e[68] = ((h_mm >> 8) & 0x0F) << 4 | ((v_mm >> 8) & 0x0F)
    e[69] = 0; e[70] = 0
    flags = 0x18
    if v_pos: flags |= 0x04
    if h_pos: flags |= 0x02
    e[71] = flags
    e[72] = 0x00; e[73] = 0x00; e[74] = 0x00; e[75] = 0xFD; e[76] = 0x00
    vmin = max(50, refresh_hz - 1)
    vmax = refresh_hz + 1
    htotal_local = width + hblank
    h_freq_khz = pclk // htotal_local if htotal_local else 60
    hmin = max(20, h_freq_khz - 5)
    hmax = h_freq_khz + 5
    pclk_10mhz = (pclk + 9999) // 10000
    e[77] = vmin; e[78] = vmax; e[79] = hmin; e[80] = hmax; e[81] = pclk_10mhz
    e[82] = 0x00
    e[83] = 0x0A
    for i in range(84, 90): e[i] = 0x20
    e[90:95] = bytes([0x00, 0x00, 0x00, 0xFC, 0x00])
    if monitor_name is None:
        monitor_name = f"Wizard {width}x{height}"
    name = monitor_name.encode("ascii", "replace")[:12] + b"\n"
    e[95:95+len(name)] = name
    for i in range(95+len(name), 108): e[i] = 0x20
    e[108:113] = bytes([0x00, 0x00, 0x00, 0x10, 0x00])
    for i in range(113, 126): e[i] = 0x20
    # Extension count = 0. The PS5's MN864739 DP→HDMI converter fails link
    # training when the EDID has ANY extension blocks. 128-byte base only.
    e[126] = 0
    s = sum(e[:127]) & 0xFF
    e[127] = (256 - s) & 0xFF
    return bytes(e)


def build_safe_1080p_edid():
    """The Universal Safe EDID: synthetic 1920x1080 @ 60 Hz, SDR sRGB."""
    return build_synthetic_edid(1920, 1080, 60, monitor_name="Wizard 1080p")


SAFE_1080P_EDID = build_safe_1080p_edid()


# ────────────────────────────────────────────────────────────────────────
# USB target detection (PS5 boot stick on this host)
# ────────────────────────────────────────────────────────────────────────

# Filesystem types that can never be a boot USB partition.
_SKIP_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "tmpfs", "devpts", "cgroup", "cgroup2",
    "bpf", "rpc_pipefs", "tracefs", "debugfs", "fusectl", "configfs",
    "securityfs", "pstore", "mqueue", "autofs", "binfmt_misc",
    "hugetlbfs", "nsfs", "overlay", "squashfs", "ramfs",
}


def _block_disk_name(dev_path):
    """Resolve '/dev/sdb2' → 'sdb', '/dev/nvme0n1p1' → 'nvme0n1', etc.
    Returns just the basename of the parent block device, or '' on failure."""
    name = os.path.basename(dev_path)
    if not name:
        return ""
    # nvme0n1p1 → strip pN at end; sda1 → strip trailing digits.
    if "nvme" in name and "p" in name:
        # nvme0n1p1 → nvme0n1
        return re.sub(r"p\d+$", "", name)
    return re.sub(r"\d+$", "", name)


def _read_sysfs(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _block_metadata(disk_name):
    """Return (is_external, brand_name, size_bytes) for a /sys/block/<name>."""
    base = f"/sys/block/{disk_name}"
    if not os.path.isdir(base):
        return (False, "", 0)
    # Removable: /sys/block/<n>/removable = "1" for USB sticks, "0" for SATA.
    # External heuristic: removable OR USB transport via /sys/block/<n>/device.
    removable = _read_sysfs(f"{base}/removable", "0") == "1"
    # Walk the device link to look for usb in its ancestry.
    is_usb = False
    try:
        real = os.path.realpath(f"{base}/device")
        is_usb = "/usb" in real
    except OSError:
        pass
    is_external = removable or is_usb
    vendor = _read_sysfs(f"{base}/device/vendor")
    model = _read_sysfs(f"{base}/device/model")
    brand = " ".join(filter(None, (vendor, model))) or disk_name
    # size is in 512-byte sectors
    try:
        sectors = int(_read_sysfs(f"{base}/size", "0"))
    except ValueError:
        sectors = 0
    size_bytes = sectors * 512
    return (is_external, brand, size_bytes)


def _bytes_to_size_str(n_bytes):
    if not n_bytes:
        return "?"
    gb = n_bytes / 1_000_000_000
    if gb < 1:
        return f"{n_bytes / 1_000_000:.0f} MB"
    if gb < 1000:
        return f"{gb:.1f} GB"
    return f"{gb / 1000:.1f} TB"


def find_ps5_volumes():
    """Find every EXTERNAL mounted volume that looks like a PS5 Linux boot
    USB. Linux-side reader for /proc/mounts. Returns dicts with the same
    shape as the macOS find_ps5_volumes():
      {path, device, media_name, volume_name, size_str, fstype,
       score, match, note, mounted_by_us, is_external}
    Internal drives (the host's root disk, swap, etc.) are excluded."""
    out = []
    try:
        with open("/proc/mounts") as f:
            mount_lines = f.read().splitlines()
    except OSError:
        return out
    seen_paths = set()
    for ln in mount_lines:
        parts = ln.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1], parts[2]
        mnt = mnt.replace(r"\040", " ").replace(r"\011", "\t")
        if fstype in _SKIP_FSTYPES:
            continue
        if mnt in seen_paths:
            continue
        seen_paths.add(mnt)
        # Externals only.
        if not dev.startswith("/dev/"):
            continue
        disk_name = _block_disk_name(dev)
        is_external, brand, size_bytes = _block_metadata(disk_name)
        if not is_external:
            continue
        score, why = score_ps5_usb(mnt)
        if score == 0:
            continue  # External, but doesn't hold loader files. Skip.
        # Volume label: try /sys, fall back to basename of mount point.
        label = os.path.basename(mnt.rstrip("/")) or mnt
        out.append({
            "path":         mnt,
            "device":       dev,
            "media_name":   brand,
            "volume_name":  label,
            "size_str":     _bytes_to_size_str(size_bytes),
            "fstype":       fstype,
            "score":        score,
            "match":        why if isinstance(why, str) else ", ".join(why),
            "note":         "Already mounted.",
            "mounted_by_us": False,
            "is_external":  True,
        })
    return sorted(out, key=lambda c: (-c["score"], c.get("device", "")))


def find_ps5_usbs():
    """Alias for find_ps5_volumes(); kept for headless callers."""
    return find_ps5_volumes()


def score_ps5_usb(mnt):
    """Heuristic score for whether a mounted path is a PS5 Linux boot USB."""
    score, why = 0, []
    p = Path(mnt)
    if (p / "bzImage").is_file():
        score += 3; why.append("bzImage")
    if (p / "initrd.img").is_file():
        score += 3; why.append("initrd.img")
    cmdline = p / "cmdline.txt"
    if cmdline.is_file():
        score += 3; why.append("cmdline.txt")
        try:
            content = cmdline.read_text(errors="replace")
            if "Titania" in content or "ttyTitania" in content:
                score += 5; why.append("PS5 console token")
            if "ubuntu2604" in content or "ubuntu" in content.lower():
                score += 1
        except OSError:
            pass
    if (p / "kexec.sh").is_file():
        score += 1; why.append("kexec.sh")
    if (p / "edid").is_dir():
        score += 1; why.append("edid/")
    return score, ", ".join(why)


# ────────────────────────────────────────────────────────────────────────
# cpio "newc" archive builder — for appending EDID to initrd
# ────────────────────────────────────────────────────────────────────────

def cpio_newc_entry(name, data, mode, mtime=None):
    """Build one entry in the 'newc' cpio format (0x070701)."""
    if mtime is None:
        mtime = int(time.time())
    name_b = name.encode("utf-8") + b"\x00"
    namesize = len(name_b)
    filesize = len(data)
    fields = [
        0,            # c_ino
        mode,         # c_mode
        0,            # c_uid
        0,            # c_gid
        1,            # c_nlink (1 is fine for files, dirs typically use 2)
        mtime,        # c_mtime
        filesize,     # c_filesize
        0, 0,         # c_devmajor, c_devminor
        0, 0,         # c_rdevmajor, c_rdevminor
        namesize,     # c_namesize (includes trailing NUL)
        0,            # c_chksum (unused for newc)
    ]
    hdr = b"070701" + b"".join(f"{v:08x}".encode("ascii") for v in fields)
    entry = hdr + name_b
    while len(entry) % 4:
        entry += b"\x00"
    entry += data
    while len(entry) % 4:
        entry += b"\x00"
    return entry


def build_edid_cpio(edid_name, edid_bytes):
    """Build a cpio fragment containing the EDID at BOTH
    lib/firmware/edid/<edid_name> AND usr/lib/firmware/edid/<edid_name>.

    Why both paths: stock Ubuntu initramfs (what update-initramfs builds
    on a /usr-merged system) places files at usr/lib/firmware/edid/.
    Older non-/usr-merged systems use lib/firmware/edid/. The kernel's
    firmware loader searches both. Writing both costs ~250 B and means
    the wizard's output works regardless of the target's initramfs
    layout. Belt and suspenders."""
    out = bytearray()
    for dirname in ("lib", "lib/firmware", "lib/firmware/edid",
                    "usr", "usr/lib", "usr/lib/firmware",
                    "usr/lib/firmware/edid"):
        out += cpio_newc_entry(dirname, b"", mode=0o040755)
    for filepath in (f"lib/firmware/edid/{edid_name}",
                     f"usr/lib/firmware/edid/{edid_name}"):
        out += cpio_newc_entry(filepath, edid_bytes, mode=0o100644)
    out += cpio_newc_entry("TRAILER!!!", b"", mode=0)
    while len(out) % 512:
        out += b"\x00"
    return bytes(out)


SAFE_BOOT_SCRIPT = """#!/bin/sh
# safe-boot.sh — restore the PS5 to known-good cmdline + initrd, then kexec.
# Written by ps5-display-wizard. Run from the PS5 over SSH if a baked EDID
# breaks the display:
#     ssh danny@<your-ps5-ip>
#     sudo /boot/efi/safe-boot.sh
# Then the screen comes back on whatever was working before the bake.
set -e
BOOT=/boot/efi
if [ -f "$BOOT/cmdline.txt.tv" ]; then
    cp "$BOOT/cmdline.txt.tv" "$BOOT/cmdline.txt"
    echo "safe-boot: restored cmdline.txt from .tv backup"
fi
if [ -f "$BOOT/initrd.img.tv" ]; then
    cp "$BOOT/initrd.img.tv" "$BOOT/initrd.img"
    echo "safe-boot: restored initrd.img from .tv backup"
fi
sync
if [ -x "$BOOT/kexec.sh" ]; then
    exec "$BOOT/kexec.sh"
else
    echo "safe-boot: kexec.sh not found — reboot the PS5 manually"
    exit 1
fi
"""


def write_safe_boot_script(usb_path):
    """Drop safe-boot.sh onto the USB so the user can recover via SSH
    from the PS5 itself, not just from the host PC that ran the wizard."""
    p = Path(usb_path) / "safe-boot.sh"
    p.write_text(SAFE_BOOT_SCRIPT)
    try:
        p.chmod(0o755)
    except OSError:
        pass
    return p


# ────────────────────────────────────────────────────────────────────────
# Bake / revert operations on the USB
# ────────────────────────────────────────────────────────────────────────

def bake_universal_cmdline(usb_path):
    """Official ps5-linux black screen fix: set amdgpu.force_1080p=1 in
    cmdline.txt and restore stock initrd. No EDID file, no cpio append,
    no video=DP-1: parameter — exactly as documented in the ps5-linux README."""
    log = []
    usb = Path(usb_path)
    cmdline = usb / "cmdline.txt"
    cmdline_bak = usb / "cmdline.txt.tv"
    initrd = usb / "initrd.img"
    initrd_bak = usb / "initrd.img.tv"
    if not cmdline.is_file():
        raise FileNotFoundError(f"cmdline.txt not found at {cmdline}")
    if not cmdline_bak.is_file():
        shutil.copy2(cmdline, cmdline_bak)
        log.append("backed up cmdline.txt → cmdline.txt.tv")
    if initrd_bak.is_file():
        shutil.copy2(initrd_bak, initrd)
        log.append("restored stock initrd.img from initrd.img.tv")
    base_cmdline = cmdline_bak.read_text().strip()
    tokens = [t for t in base_cmdline.split()
              if not t.startswith("drm.edid_firmware=")
              and not t.startswith("video=DP-1")
              and not t.startswith("amdgpu.force_1080p=")
              and not t.startswith("snd_hda_intel.enable_dp_mst=")]
    tokens.append("amdgpu.force_1080p=1")
    tokens.append("snd_hda_intel.enable_dp_mst=0")
    cmdline.write_text(" ".join(tokens) + "\n")
    log.append("wrote cmdline.txt with amdgpu.force_1080p=1")
    try:
        os.sync()
    except Exception:
        pass
    log.append("DONE — eject safely, plug into PS5, boot.")
    return log


_WHITELIST_PCLK_KHZ = {148500, 241500, 241700, 297000, 594000}


def _validate_edid_against_whitelist(edid_bytes):
    """Return list of warnings; empty list means the kernel will accept it."""
    warnings = []
    if len(edid_bytes) != 128:
        warnings.append(f"EDID is {len(edid_bytes)} B; converter requires 128 B (no extensions). "
                        "Black screen likely.")
        return warnings
    if edid_bytes[:8] != bytes([0, 255, 255, 255, 255, 255, 255, 0]):
        warnings.append("EDID magic header is wrong; not a valid EDID.")
        return warnings
    if sum(edid_bytes) % 256 != 0:
        warnings.append(f"EDID checksum invalid (sum%256={sum(edid_bytes)%256}).")
    if edid_bytes[126] != 0:
        warnings.append(f"EDID declares {edid_bytes[126]} extension(s); converter requires 0.")
    pclk = int.from_bytes(edid_bytes[54:56], "little") * 10
    if pclk and pclk not in _WHITELIST_PCLK_KHZ:
        warnings.append(f"DTD pclk {pclk} kHz not in kernel whitelist "
                        f"{sorted(_WHITELIST_PCLK_KHZ)} — will MODE_ERROR.")
    return warnings


def bake_edid_into_usb(usb_path, edid_name, edid_full_bytes, strip=True):
    """Write the EDID to <usb>/edid/, append it inside a cpio to
    initrd.img, and edit cmdline.txt to point at it. Backs up
    cmdline.txt + initrd.img on first run.

    IDEMPOTENT — wipes any prior EDID files in <usb>/edid/ so only the
    active one remains. Re-baking REPLACES, never accumulates.

    Returns a list of human-readable log lines."""
    log = []
    usb = Path(usb_path)
    edid_dir = usb / "edid"
    edid_dir.mkdir(exist_ok=True)

    # Wipe stale EDIDs first — only ever ONE active file on the stick
    for old in list(edid_dir.glob("*.bin")):
        try:
            old.unlink()
            log.append(f"removed stale {old.name}")
        except OSError:
            pass

    # Prepare the EDID bytes to actually use
    blob = strip_edid_to_minimal(edid_full_bytes) if strip else edid_full_bytes
    out_name = f"{edid_name}-stripped.bin" if strip else f"{edid_name}-full.bin"
    out_path = edid_dir / out_name
    out_path.write_bytes(blob)
    log.append(f"wrote {out_path} ({len(blob)} B)")
    for w in _validate_edid_against_whitelist(blob):
        log.append(f"WARN: {w}")

    # Backup cmdline.txt and initrd.img if not already present
    cmdline = usb / "cmdline.txt"
    cmdline_bak = usb / "cmdline.txt.tv"
    if not cmdline.is_file():
        raise FileNotFoundError(f"cmdline.txt not found at {cmdline}")
    if not cmdline_bak.is_file():
        shutil.copy2(cmdline, cmdline_bak)
        log.append(f"backed up cmdline.txt → cmdline.txt.tv")

    initrd = usb / "initrd.img"
    initrd_bak = usb / "initrd.img.tv"
    if not initrd.is_file():
        raise FileNotFoundError(f"initrd.img not found at {initrd}")
    if not initrd_bak.is_file():
        shutil.copy2(initrd, initrd_bak)
        log.append(f"backed up initrd.img → initrd.img.tv")

    # Build a cpio with the stripped EDID and APPEND to a copy of the
    # ORIGINAL initrd. We work from the backup so re-running this on
    # an already-baked stick stays idempotent.
    cpio_frag = build_edid_cpio(out_name, blob)
    base_bytes = initrd_bak.read_bytes()
    new_initrd_bytes = base_bytes + cpio_frag
    initrd.write_bytes(new_initrd_bytes)
    log.append(f"appended {len(cpio_frag)} B cpio (lib/firmware/edid/{out_name}) → initrd.img "
               f"(new size {len(new_initrd_bytes)} B)")

    # Edit cmdline.txt: take the TV-baseline as the starting point, remove
    # any prior drm.edid_firmware= or video=DP-1: tokens, then append ours.
    base_cmdline = cmdline_bak.read_text().strip()
    tokens = [t for t in base_cmdline.split()
              if not t.startswith("drm.edid_firmware=")
              and not t.startswith("video=DP-1")
              and not t.startswith("amdgpu.force_1080p=")
              and not t.startswith("snd_hda_intel.enable_dp_mst=")]
    tokens.append(f"drm.edid_firmware=DP-1:edid/{out_name}")
    tokens.append("video=DP-1:e")
    tokens.append("snd_hda_intel.enable_dp_mst=0")
    new_cmdline = " ".join(tokens) + "\n"
    cmdline.write_text(new_cmdline)
    log.append(f"wrote new cmdline.txt with drm.edid_firmware=DP-1:edid/{out_name}")

    # Drop the SSH-recovery script. Idempotent: overwrites any prior copy
    # so the script always points at the current .tv backups.
    sb = write_safe_boot_script(usb_path)
    log.append(f"wrote {sb} (in-PS5 SSH recovery: sudo /boot/efi/safe-boot.sh)")

    # fsync the directory so writes hit the FAT32
    try:
        os.sync()
    except Exception:
        pass

    log.append("DONE — eject USB safely, plug into PS5, boot.")
    return log


def revert_usb_to_stock(usb_path):
    """Restore cmdline.txt.tv → cmdline.txt and initrd.img.tv → initrd.img.
    Idempotent: missing backup = nothing to do."""
    log = []
    usb = Path(usb_path)
    for name in ("cmdline.txt", "initrd.img"):
        live = usb / name
        bak = usb / f"{name}.tv"
        if bak.is_file():
            shutil.copy2(bak, live)
            log.append(f"restored {name} from {name}.tv")
        else:
            log.append(f"no backup {name}.tv — nothing to revert for {name}")
    try:
        os.sync()
    except Exception:
        pass
    if log:
        log.append("DONE — eject USB safely, plug into PS5, boot.")
    return log


# ────────────────────────────────────────────────────────────────────────
# History DB
# ────────────────────────────────────────────────────────────────────────

def load_history():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:
        return []


def save_history(items):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(items, indent=2))


def add_monitor_to_history(mon, label=""):
    items = load_history()
    edid_hex = mon["edid"].hex()
    # Dedup on EDID hash
    sha = mon["info"]["edid_sha256"]
    items = [i for i in items if i.get("edid_sha256") != sha]
    items.append({
        "label": label or f"{mon['info']['manufacturer']} {mon['info']['model']}",
        "manufacturer": mon["info"]["manufacturer"],
        "model": mon["info"]["model"],
        "primary_mode": mon["info"]["primary_mode"],
        "max_refresh_hz": mon["info"]["max_refresh_hz"],
        "has_hdr": mon["info"]["has_hdr"],
        "has_vrr": mon["info"]["has_vrr"],
        "edid_sha256": sha,
        "edid_size": len(mon["edid"]),
        "edid_hex": edid_hex,
        "added_at": int(time.time()),
        "connector": mon["info"].get("connector", ""),
        "worked_on_ps5": None,  # user can set later
    })
    save_history(items)


# ────────────────────────────────────────────────────────────────────────
# GTK UI
# ────────────────────────────────────────────────────────────────────────

class WizardWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=f"PS5 Display Wizard v{VERSION}")
        self.set_default_size(960, 640)
        self.set_border_width(10)

        self._monitors = []  # last scan result
        self._usbs = []      # last USB scan result

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(outer)

        title = Gtk.Label()
        title.set_markup(
            "<span size='x-large' weight='bold'>PS5 Linux Display Wizard</span>\n"
            "<span size='small'>Scan your monitor's EDID and bake it into the PS5 boot USB.\n"
            "Forces the kernel to use a stripped EDID that the MN864739 HDMI converter can handle.</span>"
        )
        title.set_justify(Gtk.Justification.LEFT)
        title.set_xalign(0)
        outer.pack_start(title, False, False, 0)

        # ── Row 1: Scan + USB controls
        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.pack_start(ctrl, False, False, 0)

        self.btn_scan = Gtk.Button(label="🔍  Scan Connected Monitor(s)")
        self.btn_scan.connect("clicked", self.on_scan_clicked)
        ctrl.pack_start(self.btn_scan, False, False, 0)

        self.btn_find_usb = Gtk.Button(label="💾  Detect PS5 USB")
        self.btn_find_usb.connect("clicked", self.on_find_usb_clicked)
        ctrl.pack_start(self.btn_find_usb, False, False, 0)

        self.btn_browse_usb = Gtk.Button(label="📁  Browse for USB…")
        self.btn_browse_usb.connect("clicked", self.on_browse_usb_clicked)
        ctrl.pack_start(self.btn_browse_usb, False, False, 0)

        self.lbl_usb = Gtk.Label(label="USB: (none selected)")
        self.lbl_usb.set_xalign(0)
        ctrl.pack_start(self.lbl_usb, True, True, 0)

        # ── Row 2: Scanned monitors list
        outer.pack_start(self._sectionhdr("Connected monitor(s)"), False, False, 0)
        self.store_mon = Gtk.ListStore(str, str, str, str, str, str, int)
        # cols: Connector, Mfg, Model, Primary, MaxRefresh, Flags, row-index
        self.view_mon = Gtk.TreeView(model=self.store_mon)
        for i, col in enumerate(["Connector", "Mfg", "Model", "Primary",
                                  "Max Hz", "Flags"]):
            self.view_mon.append_column(Gtk.TreeViewColumn(col, Gtk.CellRendererText(), text=i))
        sel = self.view_mon.get_selection()
        sel.set_mode(Gtk.SelectionMode.SINGLE)
        scroll1 = Gtk.ScrolledWindow()
        scroll1.set_min_content_height(120)
        scroll1.add(self.view_mon)
        outer.pack_start(scroll1, False, False, 0)

        # ── Row 3: Bake action row ──────────────────────────────────────
        outer.pack_start(self._sectionhdr("Bake into the PS5 USB"), False, False, 0)
        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.pack_start(action_row, False, False, 0)

        self.btn_bake_universal = Gtk.Button(label="🛡  Bake Universal (Safe 1080p60)")
        self.btn_bake_universal.connect("clicked", self.on_bake_universal_clicked)
        action_row.pack_start(self.btn_bake_universal, False, False, 0)

        self.btn_bake_scanned = Gtk.Button(label="🖥  Bake Current Monitor")
        self.btn_bake_scanned.connect("clicked", self.on_bake_scanned_clicked)
        action_row.pack_start(self.btn_bake_scanned, False, False, 0)

        # History toggle — panel is hidden until clicked.
        self._hist_visible = False
        self.btn_hist_toggle = Gtk.Button(label="📂  History")
        self.btn_hist_toggle.connect("clicked", self.on_history_toggle)
        action_row.pack_start(self.btn_hist_toggle, False, False, 0)

        # ── Collapsible history panel (hidden by default) ───────────────
        self.hist_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        # Will be packed in/out by on_history_toggle.
        self.store_hist = Gtk.ListStore(str, str, str, int, str, str)
        self.view_hist = Gtk.TreeView(model=self.store_hist)
        for i, col in enumerate(["Label", "Model", "Primary",
                                  "Max Hz", "Flags", "EDID sha"]):
            self.view_hist.append_column(
                Gtk.TreeViewColumn(col, Gtk.CellRendererText(), text=i))
        scroll_hist = Gtk.ScrolledWindow()
        scroll_hist.set_min_content_height(140)
        scroll_hist.add(self.view_hist)
        self.hist_panel.pack_start(scroll_hist, True, True, 0)
        hist_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.btn_bake_hist = Gtk.Button(label="🖥  Bake from History")
        self.btn_bake_hist.connect("clicked", self.on_bake_history_clicked)
        hist_btn_row.pack_start(self.btn_bake_hist, False, False, 0)
        self.btn_save_hist = Gtk.Button(label="➕  Save Current Monitor")
        self.btn_save_hist.connect("clicked", self.on_save_history_clicked)
        hist_btn_row.pack_start(self.btn_save_hist, False, False, 0)
        self.btn_export = Gtk.Button(label="📤  Export (JSON)")
        self.btn_export.connect("clicked", self.on_export_clicked)
        hist_btn_row.pack_start(self.btn_export, False, False, 0)
        self.hist_panel.pack_start(hist_btn_row, False, False, 0)
        # Remember the outer container so the toggle can pack/unpack.
        self._outer_box = outer

        # ── Custom resolution + refresh row
        custom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.pack_start(custom_row, False, False, 0)
        custom_row.pack_start(Gtk.Label(label="🎯 Custom mode:"), False, False, 0)
        self._res_labels = [
            ("720p (1280×720)",   (1280,  720)),
            ("1080p (1920×1080)", (1920, 1080)),
            ("1440p (2560×1440)", (2560, 1440)),
            ("4K (3840×2160)",    (3840, 2160)),
        ]
        self.res_combo = Gtk.ComboBoxText()
        for lbl, _ in self._res_labels:
            self.res_combo.append_text(lbl)
        self.res_combo.set_active(1)   # default 1080p
        self.res_combo.connect("changed", self._on_res_changed)
        custom_row.pack_start(self.res_combo, False, False, 0)
        self.rate_combo = Gtk.ComboBoxText()
        custom_row.pack_start(self.rate_combo, False, False, 0)
        self.btn_bake_custom = Gtk.Button(label="🎯  Bake Custom Mode")
        self.btn_bake_custom.connect("clicked", self.on_bake_custom_clicked)
        custom_row.pack_start(self.btn_bake_custom, False, False, 0)
        custom_row.pack_start(
            Gtk.Label(label="(matches PS5 official output spec)"),
            False, False, 0)
        # Populate refresh combo for initial resolution
        self._on_res_changed(self.res_combo)

        # ── Row 5: Log
        outer.pack_start(self._sectionhdr("Activity log"), False, False, 0)
        self.log_buf = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buf)
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll_log = Gtk.ScrolledWindow()
        scroll_log.set_min_content_height(160)
        scroll_log.add(self.log_view)
        outer.pack_start(scroll_log, True, True, 0)

        self._selected_usb = None
        self._refresh_history_view()
        self._log(f"ps5-display-wizard v{VERSION} ready. "
                  f"Click Scan to read the connected monitor's EDID.")

    def _sectionhdr(self, text):
        lbl = Gtk.Label()
        lbl.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>")
        lbl.set_xalign(0)
        return lbl

    def _log(self, msg):
        end = self.log_buf.get_end_iter()
        self.log_buf.insert(end, msg.rstrip() + "\n")
        adj = self.log_view.get_parent().get_vadjustment() if self.log_view.get_parent() else None
        if adj:
            GLib.idle_add(lambda: adj.set_value(adj.get_upper()))

    def _info(self, primary, secondary=""):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.OK, text=primary)
        if secondary:
            dlg.format_secondary_text(secondary)
        dlg.run(); dlg.destroy()

    def _error(self, primary, secondary=""):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.OK, text=primary)
        if secondary:
            dlg.format_secondary_text(secondary)
        dlg.run(); dlg.destroy()

    def _confirm(self, primary, secondary=""):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.YES_NO, text=primary)
        if secondary:
            dlg.format_secondary_text(secondary)
        r = dlg.run(); dlg.destroy()
        return r == Gtk.ResponseType.YES

    # ── Handlers ──────────────────────────────────────────────────────

    def on_scan_clicked(self, _btn):
        # Always start from a blank slate. Drop the previous result list AND
        # the visible store rows BEFORE running the scan, so a slow scan
        # can't leave stale rows visible.
        self._monitors = []
        self.store_mon.clear()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        ts = time.strftime("%H:%M:%S")
        self._log(f"scan: starting fresh scan at {ts} "
                  f"(forces DRM connector re-probe)")
        self._monitors = scan_connected_monitors(force_redetect=True)
        if not self._monitors:
            self._log("scan: no connected DRM monitors found.")
            return
        for m in self._monitors:
            i = m["info"]
            flags = []
            if i["has_hdr"]: flags.append("HDR")
            if i["has_vrr"]: flags.append("VRR")
            if i["has_extension"]: flags.append(f"+{i['ext_count']}ext")
            flags_s = " ".join(flags) if flags else "-"
            self.store_mon.append([
                m["connector"], i["manufacturer"], i["model"],
                i["primary_mode"], str(i["max_refresh_hz"]) if i["max_refresh_hz"] else "?",
                flags_s, 0
            ])
            self._log(f"scan: {m['connector']} → {i['manufacturer']} {i['model']} "
                      f"{i['primary_mode']} sha256:{i['edid_sha256'][:16]} "
                      f"size:{i['size_bytes']}B")

    def on_find_usb_clicked(self, _btn):
        """Detect external volumes that hold the PS5 Linux loader files.
        Internal drives are excluded. Always shows a picker so the user
        explicitly confirms which volume to bake into."""
        self._log("usb: scanning external mounts…")
        self._usbs = find_ps5_volumes()
        if not self._usbs:
            self._log("usb: no PS5 Linux boot USB detected on external mounts.")
            self._info(
                "No PS5 boot USB detected",
                "No external mount holds the PS5 Linux loader files "
                "(bzImage + initrd.img + cmdline.txt). Plug the USB in "
                "(or mount it) and try again, or use Browse.")
            return
        chosen = self._pick_volume_from_list(self._usbs)
        if not chosen:
            self._log("usb: no selection made.")
            return
        self._selected_usb = chosen["path"]
        self.lbl_usb.set_text(
            f"USB: {chosen['volume_name']}  "
            f"({chosen['media_name']}, {chosen['fstype']}, "
            f"{chosen['size_str']})  →  {chosen['path']}")
        self._log(f"usb: selected {chosen['device']} at {chosen['path']} "
                  f"({chosen['media_name']}, {chosen['volume_name']}, "
                  f"{chosen['fstype']}, {chosen['size_str']}, "
                  f"match: {chosen['match']})")

    def _pick_volume_from_list(self, vols):
        """Modal picker for confirming the PS5 USB target. Always shown,
        even when there's only one candidate."""
        dlg = Gtk.Dialog(
            title="Confirm PS5 USB",
            parent=self, flags=Gtk.DialogFlags.MODAL)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "OK",     Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(6); box.set_margin_top(8); box.set_margin_bottom(8)
        box.set_margin_start(12); box.set_margin_end(12)
        box.add(Gtk.Label(
            label=("Confirm which external volume to bake into. "
                   "Internal drives are excluded. Double-click a row "
                   "or press OK."),
            xalign=0))
        store = Gtk.ListStore(str, str, str, str, str, int, str)
        for v in vols:
            store.append([
                v["device"], v["volume_name"], v["media_name"],
                v["size_str"], v["fstype"], v["score"], v["match"]
            ])
        view = Gtk.TreeView(model=store)
        for i, title in enumerate(("Device", "Volume", "Brand", "Size",
                                    "Filesystem", "Score", "Match reasons")):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
            col.set_resizable(True)
            view.append_column(col)
        view.set_size_request(760, 200)
        view.get_selection().select_iter(store.get_iter_first())
        # Double-click confirms.
        view.connect("row-activated",
                     lambda *_a: dlg.response(Gtk.ResponseType.OK))
        box.add(view)
        dlg.show_all()
        chosen = None
        if dlg.run() == Gtk.ResponseType.OK:
            sel = view.get_selection()
            model, it = sel.get_selected()
            if it is not None:
                idx = int(model.get_path(it).to_string())
                chosen = vols[idx]
        dlg.destroy()
        return chosen

    def on_browse_usb_clicked(self, _btn):
        """Show every external mounted volume on this machine — even those
        without the loader files yet — and let the user pick one. Internal
        drives are excluded."""
        vols = self._list_external_volumes()
        if not vols:
            self._info("No external volumes",
                       "No external drives are currently mounted. "
                       "Plug a USB stick in and try again.")
            return
        chosen = self._pick_volume_from_list(vols)
        if not chosen:
            self._log("usb: browse cancelled.")
            return
        self._selected_usb = chosen["path"]
        self.lbl_usb.set_text(
            f"USB: {chosen['volume_name']}  "
            f"({chosen['media_name']}, {chosen['fstype']}, "
            f"{chosen['size_str']})  →  {chosen['path']}")
        if chosen["score"] == 0:
            self._log(f"usb: browsed to {chosen['path']} — this volume does "
                      f"NOT yet have loader files; bake will fail until "
                      f"bzImage + initrd.img + cmdline.txt are present.")
        else:
            self._log(f"usb: browsed to {chosen['path']} ({chosen['match']})")

    def _list_external_volumes(self):
        """Return every external mounted volume on this Linux host, whether
        or not it holds the PS5 loader files. Used by Browse so a user can
        pick a freshly-formatted USB before populating it."""
        out = []
        try:
            with open("/proc/mounts") as f:
                lines = f.read().splitlines()
        except OSError:
            return out
        seen_paths = set()
        for ln in lines:
            parts = ln.split()
            if len(parts) < 3:
                continue
            dev, mnt, fstype = parts[0], parts[1], parts[2]
            mnt = mnt.replace(r"\040", " ").replace(r"\011", "\t")
            if fstype in _SKIP_FSTYPES:
                continue
            if mnt in seen_paths:
                continue
            if not dev.startswith("/dev/"):
                continue
            seen_paths.add(mnt)
            disk_name = _block_disk_name(dev)
            is_external, brand, size_bytes = _block_metadata(disk_name)
            if not is_external:
                continue
            score, why = score_ps5_usb(mnt)
            label = os.path.basename(mnt.rstrip("/")) or mnt
            out.append({
                "path":         mnt,
                "device":       dev,
                "media_name":   brand,
                "volume_name":  label,
                "size_str":     _bytes_to_size_str(size_bytes),
                "fstype":       fstype,
                "score":        score,
                "match":        (why if isinstance(why, str) else ", ".join(why))
                                if score > 0 else "(no loader files yet)",
                "note":         "Already mounted.",
                "mounted_by_us": False,
                "is_external":  True,
            })
        return sorted(out, key=lambda c: (-c["score"], c.get("device", "")))

    def on_save_history_clicked(self, _btn):
        sel = self.view_mon.get_selection()
        model, it = sel.get_selected()
        if not it:
            self._info("Pick a monitor row first.")
            return
        row = model[it]
        connector = row[0]
        mon = next((m for m in self._monitors if m["connector"] == connector), None)
        if not mon:
            return
        add_monitor_to_history(mon)
        self._refresh_history_view()
        self._log(f"history: saved {mon['info']['manufacturer']} {mon['info']['model']}")

    def on_export_clicked(self, _btn):
        items = load_history()
        if not items:
            self._info("History is empty.")
            return
        dlg = Gtk.FileChooserDialog(title="Export history",
                                    parent=self,
                                    action=Gtk.FileChooserAction.SAVE)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                        "Save", Gtk.ResponseType.OK)
        dlg.set_current_name("ps5-display-wizard-history.json")
        if dlg.run() == Gtk.ResponseType.OK:
            path = dlg.get_filename()
            Path(path).write_text(json.dumps(items, indent=2))
            self._log(f"exported history → {path}")
        dlg.destroy()

    def _refresh_history_view(self):
        self.store_hist.clear()
        for h in load_history():
            flags = []
            if h.get("has_hdr"): flags.append("HDR")
            if h.get("has_vrr"): flags.append("VRR")
            self.store_hist.append([
                h.get("label", ""),
                h.get("model", ""),
                h.get("primary_mode", ""),
                int(h.get("max_refresh_hz") or 0),
                " ".join(flags) if flags else "-",
                (h.get("edid_sha256") or "")[:16],
            ])

    def _selected_history_item(self):
        sel = self.view_hist.get_selection()
        model, it = sel.get_selected()
        if not it:
            return None
        sha_short = model[it][5]
        for h in load_history():
            if h.get("edid_sha256", "").startswith(sha_short):
                return h
        return None

    def _selected_scanned_monitor(self):
        sel = self.view_mon.get_selection()
        model, it = sel.get_selected()
        if not it:
            return None
        connector = model[it][0]
        return next((m for m in self._monitors if m["connector"] == connector), None)

    def _on_res_changed(self, combo):
        """Repopulate refresh combo when resolution changes."""
        idx = combo.get_active()
        if idx < 0 or idx >= len(self._res_labels):
            return
        _label, res = self._res_labels[idx]
        rates = RESOLUTION_REFRESH_OPTIONS.get(res, [60])
        self.rate_combo.remove_all()
        for r in rates:
            self.rate_combo.append_text(f"{r} Hz")
        # default to 60Hz if available, else first
        default_idx = rates.index(60) if 60 in rates else 0
        self.rate_combo.set_active(default_idx)

    def on_bake_custom_clicked(self, _btn):
        """Bake a synthetic EDID for a user-chosen resolution + refresh rate.

        Identity bytes come from:
          1) the History row currently selected (if the History panel is
             open and a row is highlighted), otherwise
          2) the currently scanned monitor.
        Either way Linux ends up labelling the display correctly. Timings
        come from our chosen mode (HDR / VRR / extensions stripped)."""
        if not self._selected_usb:
            self._info("Select a PS5 USB first (Detect or Browse).")
            return

        identity_edid = None
        mfg = "?"; model = "?"
        if self._hist_visible:
            h = self._selected_history_item()
            if h:
                identity_edid = bytes.fromhex(h["edid_hex"])
                mfg = h.get("manufacturer", "?")
                model = h.get("model", "?")
        if identity_edid is None:
            mon = self._selected_scanned_monitor()
            if mon:
                identity_edid = mon["edid"]
                info = mon["info"]
                mfg = info.get("manufacturer", "?")
                model = info.get("model", "?")
        if identity_edid is None:
            self._info(
                "Pick a monitor",
                "Custom bake needs a monitor identity. Scan a connected "
                "monitor, OR open History and select a saved one. "
                "Otherwise use Bake Universal (Safe 1080p60).")
            return

        res_idx = self.res_combo.get_active()
        if res_idx < 0 or res_idx >= len(self._res_labels):
            self._info("Pick a resolution.")
            return
        _label, (w, h) = self._res_labels[res_idx]
        rate_text = self.rate_combo.get_active_text() or ""
        if not rate_text:
            self._info("Pick a refresh rate.")
            return
        try:
            refresh = int(rate_text.split()[0])
        except ValueError:
            self._error("Bad refresh", f"Could not parse: {rate_text!r}")
            return
        if not self._confirm(
            f"Bake {mfg} {model} {w}x{h}@{refresh}Hz custom EDID?",
            f"USB: {self._selected_usb}\n\n"
            f"The EDID will carry the monitor's identity (so Linux recognizes "
            f"it as {model}) but only advertise this one safe timing — no HDR, "
            f"no VRR, no extensions. HDMI audio is preserved.\n\n"
            f"If your screen can't actually do {w}x{h}@{refresh}Hz the display "
            f"will stay black; re-run with Bake Universal to recover."):
            return
        try:
            edid = build_synthetic_edid(
                w, h, refresh,
                monitor_name=model[:12] if model and model != "?" else None,
                identity_source=identity_edid)
            slug = slugify(f"{mfg}-{model}-{w}x{h}-{refresh}hz")
            for ln in bake_edid_into_usb(self._selected_usb, slug, edid, strip=False):
                self._log("bake: " + ln)
            self._info(
                f"Custom {mfg} {model} {w}x{h}@{refresh}Hz baked.",
                "Eject the USB safely and plug into PS5. If the display "
                "doesn't come up, re-run with Bake Universal.")
        except Exception as e:
            self._error("Bake failed", str(e))
            self._log(f"bake: ERROR {e}")

    def on_bake_universal_clicked(self, _btn):
        """Apply amdgpu.force_1080p=1 to cmdline.txt — the official ps5-linux
        black screen fix. No display scan, no EDID file, no cpio append."""
        if not self._selected_usb:
            self._info("Select a PS5 USB first (Detect or Browse).")
            return
        if not self._confirm(
            "Bake Universal Safe Mode?",
            f"USB: {self._selected_usb}\n\n"
            "Sets amdgpu.force_1080p=1 in cmdline.txt — the official ps5-linux "
            "black screen fix. No display scan needed. Every TV and monitor "
            "supports 1080p60, so the PS5 Linux display will come up on first boot.\n\n"
            "Afterwards you can use the custom bake buttons for native resolution."):
            return
        try:
            log_lines = bake_universal_cmdline(self._selected_usb)
            for ln in log_lines:
                self._log("bake: " + ln)
            self._info(
                "Universal Safe Mode applied.",
                "Eject the USB safely and plug into PS5. Display should come up at 1080p60.")
        except Exception as e:
            self._error("Bake failed", str(e))
            self._log(f"bake: ERROR {e}")

    def on_bake_scanned_clicked(self, _btn):
        mon = self._selected_scanned_monitor()
        if not mon:
            self._info("Pick a row under 'Connected monitor(s)' first, "
                       "or click Scan.")
            return
        if not self._selected_usb:
            self._info("Select a PS5 USB first (Detect or Browse).")
            return
        info = mon["info"]
        if not self._confirm(
            f"Bake {info['manufacturer']} {info['model']} into the USB?",
            f"USB: {self._selected_usb}\n"
            f"EDID will be stripped to a minimal 1440p/4K-safe 128-byte block.\n"
            f"Backups will be created (cmdline.txt.tv, initrd.img.tv)."):
            return
        try:
            label = slugify(f"{info['manufacturer']}-{info['model']}")
            log_lines = bake_edid_into_usb(self._selected_usb, label, mon["edid"], strip=True)
            for ln in log_lines:
                self._log("bake: " + ln)
            self._info("Bake complete.", "Eject the USB safely and plug into PS5.")
        except Exception as e:
            self._error("Bake failed", str(e))
            self._log(f"bake: ERROR {e}")

    def on_bake_history_clicked(self, _btn):
        h = self._selected_history_item()
        if not h:
            self._info("Pick a row under 'Saved monitors (history)'.")
            return
        if not self._selected_usb:
            self._info("Select a PS5 USB first (Detect or Browse).")
            return
        edid_bytes = bytes.fromhex(h["edid_hex"])
        label = slugify(h.get("label") or f"{h.get('manufacturer','')}-{h.get('model','')}")
        if not self._confirm(
            f"Bake {h.get('label','?')} from history into the USB?",
            f"USB: {self._selected_usb}\nEDID sha: {h.get('edid_sha256','?')[:16]}"):
            return
        try:
            log_lines = bake_edid_into_usb(self._selected_usb, label, edid_bytes, strip=True)
            for ln in log_lines:
                self._log("bake: " + ln)
            self._info("Bake complete.", "Eject the USB safely and plug into PS5.")
        except Exception as e:
            self._error("Bake failed", str(e))
            self._log(f"bake: ERROR {e}")

    def on_history_toggle(self, _btn):
        """Show/hide the saved-monitors history panel."""
        if self._hist_visible:
            self._outer_box.remove(self.hist_panel)
            self._hist_visible = False
            self.btn_hist_toggle.set_label("📂  History")
        else:
            self._outer_box.pack_start(self.hist_panel, True, True, 0)
            self.hist_panel.show_all()
            self._hist_visible = True
            self.btn_hist_toggle.set_label("📂  History (hide)")
            self._refresh_history_view()


def slugify(s):
    out = []
    for ch in s.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_.":
            out.append("-")
    cleaned = "".join(out).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or "monitor"


def main():
    # Quick non-GUI smoke modes for headless testing
    if "--scan" in sys.argv:
        for m in scan_connected_monitors():
            i = m["info"]
            print(f"{m['connector']}\t{i['manufacturer']} {i['model']}\t"
                  f"{i['primary_mode']}\tmaxHz={i['max_refresh_hz']}\t"
                  f"size={i['size_bytes']}\tsha256:{i['edid_sha256'][:16]}")
        return
    if "--list-usb" in sys.argv:
        for u in find_ps5_usbs():
            print(f"{u['path']}\tscore={u['score']}\t{u['match']}")
        return
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        print("Flags: --scan, --list-usb, --help")
        return
    win = WizardWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
