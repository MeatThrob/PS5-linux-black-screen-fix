#!/usr/bin/env python3
"""PS5 Display Wizard — macOS GUI.

Read a connected monitor's EDID, strip it to a converter-safe form (or
generate a synthetic one for a chosen mode), and bake it into the PS5
Linux loader USB. See README.md for the full picture.

Headless modes:
    python3 ps5_display_wizard_mac.py --scan
    python3 ps5_display_wizard_mac.py --list-usb
"""

import os
import re
import sys
import json
import time
import shutil
import hashlib
import subprocess
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except ImportError as e:
    msg = (
        "This wizard needs Tkinter, which is missing from your Python install.\n"
        "\n"
        "Fixes (pick one):\n"
        "  • Install Python from https://www.python.org/downloads/  (Tkinter\n"
        "    is bundled).\n"
        "  • Homebrew users:\n"
        f"        brew install python-tk@{sys.version_info.major}.{sys.version_info.minor}\n"
        "  • Confirm: `python3 -m tkinter` should open a small test window.\n"
        "\n"
        f"Underlying error: {e}\n"
    )
    print(msg, file=sys.stderr)
    sys.exit(1)


APP = "ps5-display-wizard"
VERSION = "0.1-mac"

# macOS convention for app data
CONFIG_DIR = Path.home() / "Library" / "Application Support" / APP
HISTORY_FILE = CONFIG_DIR / "monitors.json"


# ─── EDID parse / strip — identical to Linux version ─────────────────────

def parse_edid(data):
    info = {
        "manufacturer": "?", "model": "?", "serial_text": "",
        "primary_mode": "?", "primary_pclk_khz": 0,
        "size_bytes": len(data), "has_extension": False, "ext_count": 0,
        "has_hdr": False, "has_vrr": False, "max_refresh_hz": 0,
    }
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return info
    raw = (data[8] << 8) | data[9]
    info["manufacturer"] = "".join([
        chr(((raw >> 10) & 0x1F) + ord("A") - 1),
        chr(((raw >> 5) & 0x1F) + ord("A") - 1),
        chr((raw & 0x1F) + ord("A") - 1),
    ])
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
                vmax = block[6]
                info["max_refresh_hz"] = max(info["max_refresh_hz"], vmax)
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
    info["ext_count"] = data[126]
    info["has_extension"] = info["ext_count"] > 0
    for i in range(info["ext_count"]):
        ext_off = 128 * (i + 1)
        ext = data[ext_off:ext_off + 128]
        if len(ext) < 128 or ext[0] != 0x02:
            continue
        dtd_off = ext[2]
        if dtd_off < 4:
            continue
        idx = 4
        while idx < dtd_off and idx < 128:
            tag_byte = ext[idx]
            block_len = tag_byte & 0x1F
            block_tag = (tag_byte & 0xE0) >> 5
            block = ext[idx + 1: idx + 1 + block_len]
            if block_tag == 7 and len(block) >= 1:
                subtag = block[0]
                if subtag == 6:
                    info["has_hdr"] = True
                if subtag == 0x4D:
                    info["has_vrr"] = True
            idx += 1 + block_len
    return info


def strip_edid_to_minimal(edid):
    if len(edid) < 128:
        raise ValueError("EDID too short")
    base = bytearray(edid[:128])
    base[126] = 0
    s = sum(base[:127]) & 0xFF
    base[127] = (256 - s) & 0xFF
    return bytes(base)


# ─── Synthetic EDID builder ─────────────────────────────────────────────
#
# Timings come from one of two sources:
#   * CEA-861-D / DMT standard tables — for canonical 60Hz modes that every
#     monitor advertises. These are exact and widely tested.
#   * CVT-RB v2 (Coordinated Video Timings — Reduced Blanking 2, VESA CVT
#     1.2 Annex B) — computed for higher refresh rates. Modern monitors
#     advertise these for >60Hz.
#
# The DTD pclk field is 16 bits × 10 kHz, so maximum pclk = 655350 kHz.
# Modes above that physically cannot fit in a base-block DTD.
#
# IMPORTANT: the supported modes mirror Sony's published PS5 (CFI-1000)
# HDMI output spec exactly — same resolutions+refreshes the stock OS can
# negotiate. No 90Hz, no 144Hz, no 165Hz, no 8K. The PS5's MN864739
# converter is part of an HDMI 2.1 pipeline, so high-pclk modes (4K@120,
# 1440p@120) are within hardware spec.
#
# Source: https://www.playstation.com/en-us/support/hardware/ps5-4k-resolution-guide/

# Canonical timings — every entry below has a pixel clock that the PS5
# Linux kernel's isHdmiModeValid() whitelist accepts. ANY other pclk for
# these resolutions causes amdgpu_dm to return MODE_ERROR → black screen.
#
# isHdmiModeValid() lives in drivers/gpu/drm/amd/display/amdgpu_dm/amdgpu_dm.c
# of ps5-linux-patches. It accepts:
#   - VIC 16 (1080p60)  → pclk 148500
#   - VIC 63 (1080p120) → pclk 297000     (kernel ≥7.0.3)
#   - 1440p60 with pclk ∈ {241500, 241700} kHz (hardcoded equality check)
#   - VIC 97 (2160p60)  → pclk 594000     (auto-subsampled to 4:2:0 by FLAVA3)
# Everything else returns false.
#
# (pclk_khz, hblank, hso, hsp, vblank, vso, vsp, h_pos, v_pos)
_CEA_60HZ_MODES = {
    (1280,  720,   60): ( 74250,  370, 110,  40,  30,  5,  5,  True,  True),  # CEA VIC 4
    (1920, 1080,   60): (148500,  280,  88,  44,  45,  4,  5,  True,  True),  # CEA VIC 16
    (1920, 1080,  120): (297000,  280,  88,  44,  45,  4,  5,  True,  True),  # CEA VIC 63 (kernel ≥7.0.3)
    (2560, 1440,   60): (241500,  160,  48,  32,  41,  3,  5,  True,  True),  # CEA VIC 110; pclk MUST be 241500 for whitelist
    (3840, 2160,   60): (594000,  560, 176,  88,  90,  8, 10,  True,  True),  # CEA VIC 97
}

# Resolution → refresh-rate list. ONLY modes the PS5 Linux kernel's
# isHdmiModeValid() whitelist accepts. 1440p120, 4K120, anything not on
# this list will MODE_ERROR at amdgpu_dm and black-screen.
#
# 1080p120 requires kernel ≥7.0.3 (ps5-linux-patches commit 23375e09).
# 1440p120, 4K120, and all HDR/VRR modes require driver work that is
# not yet implemented upstream. See KNOWN_WORKING.md and PROJECT_STATUS.md.
RESOLUTION_REFRESH_OPTIONS = {
    # 720p removed: VIC 4 (pclk 74250) is NOT in isHdmiModeValid's whitelist.
    # Use 1080p60 as the universal fallback instead.
    ( 1920, 1080): [60, 120],
    ( 2560, 1440): [60],
    ( 3840, 2160): [60],
}


def _cvt_rb2_timings(width, height, refresh_hz):
    """Compute CVT-RB v2 timings for a given resolution + refresh.

    Reference: VESA CVT 1.2 standard, Annex B (Reduced Blanking version 2).
    CVT-RB v2 uses fixed horizontal blanking (80 px) and computes vertical
    blanking from a minimum vertical-blanking time of 460 μs.

    Returns the same 9-tuple shape as _CEA_60HZ_MODES entries.
    """
    H_BLANK = 80
    H_SYNC = 32
    H_FRONT = 8
    V_SYNC = 8
    V_FRONT = 3
    MIN_VBLANK_USEC = 460
    MIN_VBLANK_LINES = 6

    htotal = width + H_BLANK
    # Solve iteratively: vblank depends on h_freq, h_freq depends on vtotal,
    # vtotal depends on vblank. Converges in <5 iterations.
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
    # CVT-RB v2 sync polarity: H+, V-
    return (pclk_khz, H_BLANK, H_FRONT, H_SYNC, vblank, V_FRONT, V_SYNC, True, False)


def _resolve_timings(width, height, refresh_hz):
    """Return DTD timings for the requested mode. Prefer CEA-861-D 60Hz
    canonical timings where they exist; fall back to CVT-RB v2 computation."""
    key = (int(width), int(height), int(refresh_hz))
    if key in _CEA_60HZ_MODES:
        return _CEA_60HZ_MODES[key]
    return _cvt_rb2_timings(width, height, refresh_hz)


def build_synthetic_edid(width, height, refresh_hz, monitor_name=None,
                          identity_source=None):
    """Build a 128-byte synthetic EDID claiming exactly one display mode
    (width × height @ refresh_hz), SDR, sRGB, no extensions.

    The kernel uses the DTD as the preferred-and-only timing, so the PS5's
    MN864739 converter only ever has to negotiate that one mode — exactly
    what we want for a reliable boot.

    `identity_source` — optional bytes() of a real scanned EDID. When given,
    bytes 8-17 (manufacturer ID, product code, serial number, week, year)
    are copied from it, so Linux sees the EDID labeled as the actual
    monitor (e.g. AUS VG27WQ) rather than the generic Wizard label. The
    timings still come from our chosen mode — only identity is preserved.

    Returns bytes (128 long). Raises ValueError if the mode's pclk exceeds
    the 655 MHz DTD field limit (e.g. 4K@90+).
    """
    pclk, hblank, hso, hsp, vblank, vso, vsp, h_pos, v_pos = \
        _resolve_timings(width, height, refresh_hz)
    pclk_10khz = pclk // 10
    if pclk_10khz > 0xFFFF:
        raise ValueError(
            f"{width}x{height}@{refresh_hz}Hz needs pclk={pclk} kHz which "
            f"exceeds the 655350 kHz DTD limit. Mode cannot be represented "
            f"in a base-block EDID.")

    e = bytearray(128)

    # ── Bytes 0-7: EDID magic ────────────────────────────────────────────
    e[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"

    # ── Bytes 8-17: identity (manufacturer, product, serial, week, year) ─
    # Default: WZD wizard marker. Overridden if identity_source given.
    def pnp(a, b, c):
        v = (((ord(a)-64)&0x1F)<<10) | (((ord(b)-64)&0x1F)<<5) | ((ord(c)-64)&0x1F)
        return bytes([(v>>8)&0xFF, v&0xFF])
    if identity_source and len(identity_source) >= 18 and \
            identity_source[:8] == b"\x00\xff\xff\xff\xff\xff\xff\x00":
        # Copy bytes 8-17 from the real EDID: mfg-id, product, serial, week, year.
        e[8:18] = identity_source[8:18]
    else:
        e[8:10] = pnp('W', 'Z', 'D')
        # Bytes 10-11: product code — encode resolution for traceability
        # Pack height/9 into low byte so 1080→0x78, 1440→0xA0, 2160→0xF0, 720→0x50
        prod = ((refresh_hz & 0xFF) << 8) | ((height // 9) & 0xFF)
        e[10:12] = prod.to_bytes(2, "little")
        e[12:16] = b"\x00\x00\x00\x00"        # serial
        e[16] = 1                          # week
        e[17] = 36                         # year - 1990 = 2026
    e[18] = 1; e[19] = 4                  # EDID 1.4
    e[20] = 0x80 | (0x01 << 4) | 0x02     # digital, 8bpc, HDMI-a
    # Image size in cm (approximate — gives the OS something for DPI calc)
    # Picked roughly: 16:9 panel, slightly varies by class but mostly cosmetic
    if width <= 1280:
        e[21] = 35; e[22] = 20            # ~16" class
    elif width <= 1920:
        e[21] = 53; e[22] = 30            # ~24" class
    elif width <= 2560:
        e[21] = 60; e[22] = 34            # ~27" class
    else:
        e[21] = 89; e[22] = 50            # ~40" class
    e[23] = 120                            # gamma 2.2
    e[24] = 0x06 | 0x04                    # sRGB | preferred-native

    # Bytes 25-34: sRGB chromaticity primaries
    e[25:35] = bytes([0xee, 0x95, 0xa3, 0x54, 0x4c, 0x99, 0x26, 0x0f, 0x50, 0x54])

    # Bytes 35-37: established timings. Always advertise 640x480@60 so the
    # kernel has an obvious VESA fallback if our DTD ever fails to parse.
    e[35] = 0x20
    e[36] = 0x00
    e[37] = 0x00

    # Bytes 38-53: standard timings — leave all unused
    for i in range(38, 54, 2):
        e[i] = 0x01; e[i+1] = 0x01

    # ── Bytes 54-71: DTD #1, the only display mode we advertise ──────────
    # pclk (16-bit LE, ×10 kHz)
    e[54] = pclk_10khz & 0xFF
    e[55] = (pclk_10khz >> 8) & 0xFF
    # Horizontal active (low 8) / blanking (low 8) / split byte upper-4-bits
    e[56] = width & 0xFF
    e[57] = hblank & 0xFF
    e[58] = ((width >> 8) & 0x0F) << 4 | ((hblank >> 8) & 0x0F)
    # Vertical active / blanking
    e[59] = height & 0xFF
    e[60] = vblank & 0xFF
    e[61] = ((height >> 8) & 0x0F) << 4 | ((vblank >> 8) & 0x0F)
    # Sync offsets and pulse widths
    e[62] = hso & 0xFF
    e[63] = hsp & 0xFF
    e[64] = ((vso & 0x0F) << 4) | (vsp & 0x0F)
    e[65] = (((hso >> 8) & 0x03) << 6) | (((hsp >> 8) & 0x03) << 4) | \
            (((vso >> 4) & 0x03) << 2) | ((vsp >> 4) & 0x03)
    # Image size in mm — use byte 21/22 (cm) × 10 for rough match
    h_mm = e[21] * 10
    v_mm = e[22] * 10
    e[66] = h_mm & 0xFF
    e[67] = v_mm & 0xFF
    e[68] = ((h_mm >> 8) & 0x0F) << 4 | ((v_mm >> 8) & 0x0F)
    e[69] = 0; e[70] = 0  # no border pixels
    # Features byte: digital separate sync (bits 4-3 = 11), then h/v polarity
    flags = 0x18  # bit 4 = 1 (digital sep), bit 3 = 1
    if v_pos: flags |= 0x04
    if h_pos: flags |= 0x02
    e[71] = flags

    # ── Bytes 72-89: monitor range limits descriptor (0xFD) ──────────────
    e[72] = 0x00; e[73] = 0x00; e[74] = 0x00; e[75] = 0xFD; e[76] = 0x00
    # Vmin, Vmax: bracket the requested refresh ±1 Hz
    vmin = max(50, refresh_hz - 1)
    vmax = refresh_hz + 1
    # Hmin/Hmax: derive from total line count and pclk. Rough estimates fine.
    htotal = width + hblank
    vtotal = height + vblank
    h_freq_khz = pclk // htotal if htotal else 60
    hmin = max(20, h_freq_khz - 5)
    hmax = h_freq_khz + 5
    pclk_10mhz = (pclk + 9999) // 10000  # round up to 10 MHz units
    e[77] = vmin; e[78] = vmax; e[79] = hmin; e[80] = hmax; e[81] = pclk_10mhz
    e[82] = 0x00  # no extended timing info
    e[83] = 0x0A
    for i in range(84, 90):
        e[i] = 0x20

    # ── Bytes 90-107: monitor name descriptor (0xFC) ─────────────────────
    e[90:95] = bytes([0x00, 0x00, 0x00, 0xFC, 0x00])
    if monitor_name is None:
        monitor_name = f"Wizard {width}x{height}"
    name = monitor_name.encode("ascii", "replace")[:12] + b"\n"
    e[95:95+len(name)] = name
    for i in range(95+len(name), 108):
        e[i] = 0x20

    # ── Bytes 108-125: dummy/padding descriptor (tag 0x10) ───────────────
    e[108:113] = bytes([0x00, 0x00, 0x00, 0x10, 0x00])
    for i in range(113, 126):
        e[i] = 0x20

    # ── Byte 126: extension count = 0 ────────────────────────────────────
    # The PS5's MN864739 DP→HDMI converter fails link training when the EDID
    # contains ANY extension blocks (even a minimal CEA-861 audio block).
    # Keep this 128-byte base-block only. No exceptions.
    e[126] = 0

    # ── Byte 127: base-block checksum (sum of bytes 0..127 ≡ 0 mod 256) ──
    s = sum(e[:127]) & 0xFF
    e[127] = (256 - s) & 0xFF

    return bytes(e)


def build_safe_1080p_edid():
    """The Universal Safe EDID: synthetic 1920x1080 @ 60 Hz, SDR sRGB."""
    return build_synthetic_edid(1920, 1080, 60, monitor_name="Wizard 1080p")


SAFE_1080P_EDID = build_safe_1080p_edid()


# ─── macOS EDID acquisition ──────────────────────────────────────────────

def scan_connected_monitors():
    """Use `ioreg` to find every connected display's EDID.

    On Intel Macs EDID lives under `AppleDisplay` class objects as a property
    named `IODisplayEDID`. On Apple Silicon (M1/M2/M3/...) the IOKit hierarchy
    changed — display EDIDs are exposed by framebuffer / IOAVDisplay classes
    as a property simply named `EDID` (no `IODisplay` prefix), and also
    appear inside `Metadata` dicts and `DisplayHints` blobs.

    To cover both, we dump the full ioreg tree and grep for either property
    name. We dedupe by EDID hash so the same physical monitor showing up at
    multiple paths only counts once. Returns list of {connector, edid, info}.
    """
    try:
        out = subprocess.check_output(
            ["ioreg", "-lw0"],
            text=True, stderr=subprocess.DEVNULL)
    except Exception as e:
        return [{"_error": f"ioreg failed: {e}"}]

    # Match any "EDID" or "IODisplayEDID" assignment, then optionally the
    # path/class name to the left for a connector hint.
    monitors = []
    seen = set()
    # Pattern: capture leading class hint + the hex EDID value.
    pattern = re.compile(
        r'(?:"(IODisplayEDID|EDID)"|"Metadata"\s*=\s*\{[^}]*?"EDID"|EDID)\s*=\s*<([0-9a-fA-F\s]+)>')
    for m in pattern.finditer(out):
        hex_clean = re.sub(r"[^0-9a-fA-F]", "", m.group(2))
        if len(hex_clean) < 256:  # need at least 128 bytes = 256 hex chars
            continue
        try:
            edid = bytes.fromhex(hex_clean)
        except ValueError:
            continue
        # Sanity: must start with the EDID magic header
        if edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
            continue
        sha = hashlib.sha256(edid).hexdigest()
        if sha in seen:
            continue
        seen.add(sha)

        # Pull a friendly product name from the line just above the match
        before = out[max(0, m.start() - 800):m.start()]
        prod_m = re.search(r'"ProductName"\s*=\s*"([^"]+)"', before)
        connector = f"macOS:{prod_m.group(1)}" if prod_m else "macOS:Display"

        info = parse_edid(edid)
        info["connector"] = connector
        info["edid_sha256"] = sha
        monitors.append({"connector": connector, "edid": edid, "info": info})
    return monitors


# ─── macOS USB / Linux-partition detection ──────────────────────────────

def _bytes_to_gb_str(n_bytes):
    """Human-friendly size string. Accepts int or None."""
    if not n_bytes:
        return "?"
    gb = n_bytes / 1_000_000_000
    if gb < 1:
        return f"{n_bytes / 1_000_000:.0f} MB"
    if gb < 1000:
        return f"{gb:.1f} GB"
    return f"{gb / 1000:.1f} TB"


def find_ps5_volumes():
    """Find every external volume that looks like a PS5 Linux boot USB.

    Combines two detection paths into one:
      • Volumes already mounted under /Volumes/ (filesystem-agnostic)
      • Partitions surfaced by diskutil but NOT mounted (typically the
        FAT EFI 'boot' partition macOS hides on a fully-partitioned drive).
        Attempts to mount these via diskutil, escalating to an admin
        prompt if needed.

    Returns a list of dicts, externals only, never auto-picks anything:
      {
        "path":         "/Volumes/boot",     # mount point, or "" if unreadable
        "device":       "/dev/disk6s2",      # device node
        "media_name":   "Ultra",             # vendor/product hint
        "volume_name":  "boot",              # filesystem label
        "size_str":     "523 MB",            # human-readable
        "fstype":       "FAT32",             # MS-DOS FAT32, ExFAT, ext4, ...
        "score":        9,                   # 0 = informational only
        "match":        "bzImage + initrd.img + cmdline.txt",
        "note":         "Mounted by wizard.", # operator-facing context
        "mounted_by_us": True / False,
        "is_external":  True,
      }
    Sorted highest-score first, then alphabetical by device.
    """
    out = []
    seen_paths = set()

    # ── Path A: already-mounted volumes under /Volumes ───────────────────
    vols = Path("/Volumes")
    if vols.is_dir():
        for entry in sorted(vols.iterdir()):
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            mount_info = _diskutil_info_plist(str(entry))
            # Skip internal disks (Macintosh HD, etc).
            if mount_info and mount_info.get("Internal", False):
                continue
            score, why = score_ps5_usb(entry)
            fstype = mac_volume_fs(entry) or "?"
            device = ""
            media = ""
            size_str = "?"
            volume_name = entry.name
            if mount_info:
                device = f"/dev/{mount_info.get('DeviceIdentifier', '')}"
                # Friendly product name comes from the parent disk (e.g. "Ultra"),
                # but the size we report is THIS partition, not the whole drive.
                parent_id = mount_info.get("ParentWholeDisk", "")
                if parent_id:
                    parent = _diskutil_info_plist(parent_id) or {}
                    media = parent.get("MediaName", "") or \
                            parent.get("IORegistryEntryName", "")
                else:
                    media = mount_info.get("MediaName", "")
                size_str = _bytes_to_gb_str(mount_info.get("TotalSize"))
                volume_name = mount_info.get("VolumeName", entry.name) or entry.name
            if score == 0:
                # Surface as informational only if the user wants to see
                # everything plugged in. Skip purely-internal scratch mounts.
                continue
            seen_paths.add(str(entry))
            out.append({
                "path":         str(entry),
                "device":       device,
                "media_name":   media,
                "volume_name":  volume_name,
                "size_str":     size_str,
                "fstype":       fstype,
                "score":        score,
                "match":        why if isinstance(why, str) else " + ".join(why),
                "note":         "Already mounted.",
                "mounted_by_us": False,
                "is_external":  True,
            })

    # ── Path B: partitions on external disks (mounted or not) ────────────
    plist = _diskutil_list_plist()
    if plist:
        all_parts = []
        for ad in plist.get("AllDisksAndPartitions", []):
            all_parts.extend(ad.get("Partitions", []))
            # APFS containers carry their volumes under a different key
            for apfs in ad.get("APFSVolumes", []):
                all_parts.append(apfs)
        seen_ids = set()
        for part in all_parts:
            ident = part.get("DeviceIdentifier", "")
            if not ident or ident in seen_ids:
                continue
            seen_ids.add(ident)
            info = _diskutil_info_plist(ident)
            if not info:
                continue
            # Hard filter: externals only.
            if info.get("Internal", False):
                continue
            # Hint: APFS internal stuff (Preboot, Recovery, etc) is internal.
            content = info.get("Content", "")
            fs_personality = info.get("FilesystemName", "")
            fs_type = info.get("FilesystemType", "")
            volume_name = info.get("VolumeName", "") or ""
            mount_point = info.get("MountPoint", "")
            parent_id = info.get("ParentWholeDisk", "")
            parent = _diskutil_info_plist(parent_id) if parent_id else {}
            media = (parent or {}).get("MediaName", "") if parent else \
                    info.get("MediaName", "")
            size_str = _bytes_to_gb_str(info.get("TotalSize"))

            # Skip APFS containers (the disk5-style synthesized wrappers).
            if "Container" in content or fs_personality == "" and \
                    "APFS Container" in content:
                continue

            # Branch B1: Linux Filesystem partitions (ext4). Can't read on
            # macOS without ext4fuse; still surface so user knows it's there.
            if content == "Linux Filesystem" or "linux" in content.lower():
                if mount_point in seen_paths:
                    continue
                out.append({
                    "path":         "",
                    "device":       f"/dev/{ident}",
                    "media_name":   media or "Linux",
                    "volume_name":  volume_name or "linux",
                    "size_str":     size_str,
                    "fstype":       fs_personality or "ext4/Linux",
                    "score":        0,
                    "match":        "ext4/Linux partition (read-only on macOS)",
                    "note":         ("Cannot bake into ext4 from macOS. "
                                     "Install macFUSE + ext4fuse, or run the "
                                     "Linux build of the wizard."),
                    "mounted_by_us": False,
                    "is_external":  True,
                })
                continue

            # Branch B2: FAT/exFAT partitions — the loader 'boot' partition
            # lives here. Try to mount unmounted ones.
            is_fat = (fs_type in ("msdos", "exfat") or
                      "FAT" in fs_personality.upper() or
                      fs_personality.lower().startswith("ms-dos"))
            if not is_fat:
                continue
            mounted_by_us = False
            if not mount_point:
                need_sudo = False
                try:
                    subprocess.check_output(
                        ["diskutil", "mount", ident],
                        stderr=subprocess.STDOUT, timeout=10)
                    mounted_by_us = True
                    info2 = _diskutil_info_plist(ident) or {}
                    mount_point = info2.get("MountPoint", "")
                except subprocess.CalledProcessError:
                    need_sudo = True
                except Exception:
                    continue
                if need_sudo:
                    try:
                        cmd = (f'do shell script "/usr/sbin/diskutil mount '
                               f'{ident}" with administrator privileges')
                        subprocess.check_output(
                            ["osascript", "-e", cmd],
                            stderr=subprocess.STDOUT, timeout=60)
                        mounted_by_us = True
                        info2 = _diskutil_info_plist(ident) or {}
                        mount_point = info2.get("MountPoint", "")
                    except Exception:
                        out.append({
                            "path":         "",
                            "device":       f"/dev/{ident}",
                            "media_name":   media or "?",
                            "volume_name":  volume_name or "?",
                            "size_str":     size_str,
                            "fstype":       fs_personality or "FAT",
                            "score":        0,
                            "match":        "FAT partition (mount refused)",
                            "note":         (f"Could not auto-mount /dev/{ident}. "
                                             f"Try: sudo diskutil mount {ident}"),
                            "mounted_by_us": False,
                            "is_external":  True,
                        })
                        continue
            if not mount_point or mount_point in seen_paths:
                # Either still unmounted or already covered by Path A.
                if mounted_by_us and not mount_point:
                    continue
                if mount_point in seen_paths:
                    continue
            score, why = score_ps5_usb(mount_point)
            if score == 0:
                # FAT partition exists but no loader files. Be polite and
                # unmount what we mounted just to inspect.
                if mounted_by_us:
                    try:
                        subprocess.check_output(["diskutil", "unmount", ident],
                                                stderr=subprocess.DEVNULL, timeout=5)
                    except Exception:
                        pass
                continue
            seen_paths.add(mount_point)
            out.append({
                "path":         mount_point,
                "device":       f"/dev/{ident}",
                "media_name":   media or "?",
                "volume_name":  volume_name or Path(mount_point).name,
                "size_str":     size_str,
                "fstype":       fs_personality or "FAT",
                "score":        score,
                "match":        why if isinstance(why, str) else " + ".join(why),
                "note":         ("Mounted by wizard." if mounted_by_us
                                 else "Already mounted."),
                "mounted_by_us": mounted_by_us,
                "is_external":  True,
            })

    # Highest score first, then alphabetical by device for stable ordering.
    return sorted(out, key=lambda c: (-c["score"], c.get("device", "")))


# Convenience filters over find_ps5_volumes() for callers that only want
# one slice (the headless --list-usb mode in particular).

def find_ps5_usbs():
    """Return only the already-mounted external candidates with loader files."""
    return [v for v in find_ps5_volumes()
            if v["score"] > 0 and v["note"] == "Already mounted."]


def find_ps5_linux_partitions():
    """Return externally-detected partitions including not-yet-mounted FAT
    'boot' partitions and informational ext4 rows."""
    return [v for v in find_ps5_volumes()
            if v["note"] != "Already mounted."]


def mac_volume_fs(path):
    """Return short FS type via `diskutil info` (MS-DOS FAT32, ExFAT, …)."""
    try:
        out = subprocess.check_output(
            ["diskutil", "info", str(path)], text=True,
            stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return None
    m = re.search(r"File System Personality:\s*(.+)", out)
    if m:
        v = m.group(1).strip()
        # Normalize common values
        if "FAT32" in v: return "FAT32"
        if "FAT16" in v: return "FAT16"
        if "ExFAT" in v.upper() or "exFAT" in v: return "ExFAT"
        if "MS-DOS" in v: return "MSDOS"
        return v
    return None


def _diskutil_list_plist():
    """Return parsed plist output of `diskutil list -plist`. None on error."""
    try:
        out = subprocess.check_output(
            ["diskutil", "list", "-plist", "external"],
            stderr=subprocess.DEVNULL, timeout=8)
    except Exception:
        return None
    try:
        import plistlib
        return plistlib.loads(out)
    except Exception:
        return None


def _diskutil_info_plist(identifier):
    """Return parsed `diskutil info -plist <id>` output. None on error."""
    try:
        out = subprocess.check_output(
            ["diskutil", "info", "-plist", identifier],
            stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return None
    try:
        import plistlib
        return plistlib.loads(out)
    except Exception:
        return None


def find_ps5_linux_partitions():
    """Scan all external disk partitions (mounted or NOT) for ones that
    look like a PS5 Linux loader install — specifically the FAT/EFI 'boot'
    partition that holds bzImage + initrd.img + cmdline.txt.

    macOS does NOT auto-mount EFI System Partitions, so the PS5 Linux boot
    partition is usually invisible to /Volumes scanning even when the disk
    is physically plugged in. This function uses `diskutil list` to find
    it on the partition level, attempts to mount it read-write, and scores
    its contents the same way find_ps5_usbs() does for already-mounted
    volumes.

    Also surfaces ext4 'Linux Filesystem' partitions (the OS root) for
    informational purposes — macOS can't read those without ext4fuse, so
    the wizard can't bake into them, but at least the user sees they exist.

    Returns list of dicts with the same shape as find_ps5_usbs() entries,
    plus a 'note' field explaining if a manual mount was performed.
    """
    candidates = []
    plist = _diskutil_list_plist()
    if not plist:
        return candidates
    all_partitions = []
    for ad in plist.get("AllDisksAndPartitions", []):
        all_partitions.extend(ad.get("Partitions", []))
    seen_ids = set()
    for part in all_partitions:
        ident = part.get("DeviceIdentifier", "")
        if not ident or ident in seen_ids:
            continue
        seen_ids.add(ident)
        # Look up detailed info for this partition
        info = _diskutil_info_plist(ident)
        if not info:
            continue
        # Partition type label & filesystem
        content = info.get("Content", "")               # e.g. "EFI", "Linux Filesystem"
        fs_personality = info.get("FilesystemName", "") # e.g. "MS-DOS FAT32"
        fs_type = info.get("FilesystemType", "")        # e.g. "msdos", "exfat", ""
        volume_name = info.get("VolumeName", "")
        mount_point = info.get("MountPoint", "")

        # Branch 1 — Linux Filesystem partition (ext4 etc). Can't read from
        # macOS without ext4fuse, but surface it so user knows it's there.
        if content == "Linux Filesystem" or "linux" in content.lower():
            candidates.append({
                "path": "",  # unreadable on macOS
                "device": f"/dev/{ident}",
                "fstype": fs_personality or content or "Linux",
                "score": 0,
                "match": "ext4/Linux partition — needs ext4fuse on macOS",
                "note": ("Detected but not readable on macOS. To bake into this, "
                         "either install macFUSE + ext4fuse, or run the Linux "
                         "wizard from a Linux machine."),
                "mounted_by_us": False,
            })
            continue

        # Branch 2 — FAT/exFAT partitions (the EFI 'boot' partition where
        # bzImage / initrd.img / cmdline.txt actually live on a PS5 Linux
        # USB). Try to mount it if it isn't already.
        is_fat = (fs_type in ("msdos", "exfat") or
                  "FAT" in fs_personality.upper() or
                  fs_personality.lower().startswith("ms-dos"))
        if not is_fat:
            continue
        mounted_by_us = False
        if not mount_point:
            # First try without sudo. macOS auto-mount permissions vary by
            # partition type — regular FAT volumes mount fine for the user,
            # EFI System Partitions need root since Ventura.
            need_sudo = False
            try:
                subprocess.check_output(
                    ["diskutil", "mount", ident],
                    stderr=subprocess.STDOUT, timeout=10)
                mounted_by_us = True
                info2 = _diskutil_info_plist(ident) or {}
                mount_point = info2.get("MountPoint", "")
            except subprocess.CalledProcessError as exc:
                need_sudo = True
            except Exception:
                continue
            # If unprivileged mount refused, try via osascript with the
            # macOS-native admin password prompt.
            if need_sudo:
                try:
                    cmd = (f'do shell script "/usr/sbin/diskutil mount '
                           f'{ident}" with administrator privileges')
                    subprocess.check_output(
                        ["osascript", "-e", cmd],
                        stderr=subprocess.STDOUT, timeout=60)
                    mounted_by_us = True
                    info2 = _diskutil_info_plist(ident) or {}
                    mount_point = info2.get("MountPoint", "")
                except subprocess.CalledProcessError:
                    candidates.append({
                        "path": "",
                        "device": f"/dev/{ident}",
                        "fstype": fs_personality or "FAT",
                        "score": 0,
                        "match": "FAT/EFI partition (mount refused)",
                        "note": (f"Could not auto-mount /dev/{ident} even with "
                                 f"admin privileges. Try in Terminal: "
                                 f"sudo diskutil mount {ident}"),
                        "mounted_by_us": False,
                    })
                    continue
                except Exception:
                    continue
        if not mount_point:
            continue
        score, why = score_ps5_usb(mount_point)
        if score == 0:
            # FAT partition exists but doesn't have the loader files. Skip,
            # don't pollute the candidate list.
            if mounted_by_us:
                # Be polite — unmount what we mounted only to inspect
                try:
                    subprocess.check_output(["diskutil", "unmount", ident],
                                            stderr=subprocess.DEVNULL, timeout=5)
                except Exception:
                    pass
            continue
        candidates.append({
            "path": mount_point,
            "device": f"/dev/{ident}",
            "fstype": fs_personality or "FAT",
            "score": score,
            "match": " + ".join(why),
            "note": ("Mounted by wizard." if mounted_by_us else
                     "Already mounted."),
            "mounted_by_us": mounted_by_us,
            "volume_name": volume_name,
        })
    return sorted(candidates, key=lambda c: -c["score"])


def score_ps5_usb(mnt):
    score, why = 0, []
    p = Path(mnt)
    if (p / "bzImage").is_file(): score += 3; why.append("bzImage")
    if (p / "initrd.img").is_file(): score += 3; why.append("initrd.img")
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
    if (p / "kexec.sh").is_file(): score += 1; why.append("kexec.sh")
    if (p / "edid").is_dir(): score += 1; why.append("edid/")
    return score, ", ".join(why)


# ─── cpio newc builder + bake/revert (identical to Linux) ────────────────

def cpio_newc_entry(name, data, mode, mtime=None):
    if mtime is None:
        mtime = int(time.time())
    name_b = name.encode("utf-8") + b"\x00"
    fields = [0, mode, 0, 0, 1, mtime, len(data), 0, 0, 0, 0, len(name_b), 0]
    hdr = b"070701" + b"".join(f"{v:08x}".encode("ascii") for v in fields)
    entry = hdr + name_b
    while len(entry) % 4:
        entry += b"\x00"
    entry += data
    while len(entry) % 4:
        entry += b"\x00"
    return entry


def build_edid_cpio(edid_name, edid_bytes):
    """Place the EDID at BOTH lib/firmware/edid/ and usr/lib/firmware/edid/
    so the wizard's output works on /usr-merged and non-merged systems."""
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
    p = Path(usb_path) / "safe-boot.sh"
    p.write_text(SAFE_BOOT_SCRIPT)
    try:
        p.chmod(0o755)
    except OSError:
        pass
    return p


# Pixel-clock whitelist enforced by isHdmiModeValid() in the PS5 Linux kernel.
# Any baked EDID whose DTD pclk is not in this set will MODE_ERROR and the
# display will stay black. See KNOWN_WORKING.md for the full citation.
_WHITELIST_PCLK_KHZ = {148500, 241500, 241700, 297000, 594000}


def _validate_edid_against_whitelist(edid_bytes):
    """Inspect a baked EDID and return human-readable warnings for anything
    the PS5 Linux kernel will reject. Empty list = OK to bake."""
    warnings = []
    if len(edid_bytes) != 128:
        warnings.append(f"EDID is {len(edid_bytes)} B; converter requires 128 B (no extensions). "
                        "Black screen likely.")
        return warnings
    if edid_bytes[:8] != bytes([0, 255, 255, 255, 255, 255, 255, 0]):
        warnings.append("EDID magic header is wrong; this file is not a valid EDID.")
        return warnings
    if sum(edid_bytes) % 256 != 0:
        warnings.append(f"EDID base-block checksum is invalid (sum%256 = {sum(edid_bytes)%256}).")
    if edid_bytes[126] != 0:
        warnings.append(f"EDID declares {edid_bytes[126]} extension block(s); PS5 converter "
                        "requires 0 extensions. Black screen likely.")
    # Parse the first DTD at offset 54 and check pclk against the whitelist
    pclk = int.from_bytes(edid_bytes[54:56], "little") * 10  # → kHz
    if pclk and pclk not in _WHITELIST_PCLK_KHZ:
        warnings.append(f"EDID DTD pixel clock is {pclk} kHz; kernel's isHdmiModeValid() "
                        f"only accepts {sorted(_WHITELIST_PCLK_KHZ)}. Black screen likely.")
    return warnings


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
    # Back up cmdline once
    if not cmdline_bak.is_file():
        shutil.copy2(cmdline, cmdline_bak)
        log.append("backed up cmdline.txt → cmdline.txt.tv")
    # Restore stock initrd (remove any previous cpio append)
    if initrd_bak.is_file():
        shutil.copy2(initrd_bak, initrd)
        log.append("restored stock initrd.img from initrd.img.tv")
    # Build clean cmdline from stock backup, stripping any previous display params
    base_cmdline = cmdline_bak.read_text().strip()
    tokens = [t for t in base_cmdline.split()
              if not t.startswith("drm.edid_firmware=")
              and not t.startswith("video=DP-1")
              and not t.startswith("amdgpu.force_1080p=")
              and not t.startswith("snd_hda_intel.enable_dp_mst=")]
    tokens.append("amdgpu.force_1080p=1")
    tokens.append("snd_hda_intel.enable_dp_mst=0")
    cmdline.write_text(" ".join(tokens) + "\n")
    log.append(f"wrote cmdline.txt with amdgpu.force_1080p=1")
    sb = write_safe_boot_script(usb_path)
    log.append(f"wrote {sb} (SSH recovery: sudo /boot/efi/safe-boot.sh)")
    try:
        os.sync()
    except Exception:
        pass
    log.append("DONE — eject (Finder → drag to Trash), plug into PS5, boot.")
    return log


def bake_edid_into_usb(usb_path, edid_name, edid_full_bytes, strip=True):
    log = []
    usb = Path(usb_path)
    edid_dir = usb / "edid"
    edid_dir.mkdir(exist_ok=True)
    # IDEMPOTENT BAKE — re-baking REPLACES, never accumulates. Wipe every
    # existing EDID first so only the active file remains on the stick.
    for old in list(edid_dir.glob("*.bin")):
        try:
            old.unlink()
            log.append(f"removed stale {old.name}")
        except OSError:
            pass
    blob = strip_edid_to_minimal(edid_full_bytes) if strip else edid_full_bytes
    out_name = f"{edid_name}-stripped.bin" if strip else f"{edid_name}-full.bin"
    (edid_dir / out_name).write_bytes(blob)
    log.append(f"wrote {edid_dir/out_name} ({len(blob)} B)")
    # Validate against the kernel whitelist before we ever ship this EDID
    _warnings = _validate_edid_against_whitelist(blob)
    for w in _warnings:
        log.append(f"WARN: {w}")
    cmdline = usb / "cmdline.txt"
    cmdline_bak = usb / "cmdline.txt.tv"
    if not cmdline.is_file():
        raise FileNotFoundError(f"cmdline.txt not found at {cmdline}")
    if not cmdline_bak.is_file():
        shutil.copy2(cmdline, cmdline_bak)
        log.append("backed up cmdline.txt → cmdline.txt.tv")
    initrd = usb / "initrd.img"
    initrd_bak = usb / "initrd.img.tv"
    if not initrd.is_file():
        raise FileNotFoundError(f"initrd.img not found at {initrd}")
    if not initrd_bak.is_file():
        shutil.copy2(initrd, initrd_bak)
        log.append("backed up initrd.img → initrd.img.tv")
    # Append EDID as a cpio fragment to initrd so the kernel can load it
    # via drm.edid_firmware= before the root filesystem is available.
    cpio_frag = build_edid_cpio(out_name, blob)
    base_bytes = initrd_bak.read_bytes()
    initrd.write_bytes(base_bytes + cpio_frag)
    log.append(f"appended {len(cpio_frag)} B cpio (lib/firmware/edid/{out_name}) → initrd.img")
    # Build cmdline from the stock .tv baseline, stripping any previous
    # display/audio params we manage so re-running is always idempotent.
    base_cmdline = cmdline_bak.read_text().strip()
    tokens = [t for t in base_cmdline.split()
              if not t.startswith("drm.edid_firmware=")
              and not t.startswith("video=DP-1")
              and not t.startswith("amdgpu.force_1080p=")
              and not t.startswith("snd_hda_intel.enable_dp_mst=")]
    tokens.append(f"drm.edid_firmware=DP-1:edid/{out_name}")
    tokens.append("video=DP-1:e")
    tokens.append("snd_hda_intel.enable_dp_mst=0")
    cmdline.write_text(" ".join(tokens) + "\n")
    log.append(f"wrote new cmdline.txt with drm.edid_firmware=DP-1:edid/{out_name}")
    sb = write_safe_boot_script(usb_path)
    log.append(f"wrote {sb} (in-PS5 SSH recovery: sudo /boot/efi/safe-boot.sh)")
    try:
        os.sync()
    except Exception:
        pass
    log.append("DONE — eject in Finder (drag to Trash or right-click → Eject), plug into PS5, boot.")
    return log


def revert_usb_to_stock(usb_path):
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
        log.append("DONE — eject and plug into PS5.")
    return log


# ─── History DB ──────────────────────────────────────────────────────────

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
        "edid_hex": mon["edid"].hex(),
        "added_at": int(time.time()),
        "connector": mon["info"].get("connector", ""),
        "worked_on_ps5": None,
        "scanned_on": "macos",
    })
    save_history(items)


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


# ─── Tkinter UI ──────────────────────────────────────────────────────────

class WizardApp:
    def __init__(self, root):
        self.root = root
        root.title(f"PS5 Display Wizard v{VERSION}")
        root.geometry("980x720")

        self._monitors = []
        self._selected_usb = None

        # Header
        header = ttk.Frame(root)
        header.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(header, text="PS5 Linux Display Wizard",
                  font=("Helvetica", 18, "bold")).pack(anchor="w")
        ttk.Label(header,
                  text=("Scan your monitor's EDID and bake it into the PS5 boot USB.\n"
                        "Forces the kernel to use a stripped EDID that the MN864739 HDMI converter can handle."),
                  foreground="#555", justify="left").pack(anchor="w")

        # Top controls row
        ctrl = ttk.Frame(root)
        ctrl.pack(fill="x", padx=12, pady=6)
        ttk.Button(ctrl, text="🔍  Scan Connected Monitor(s)",
                   command=self.on_scan).pack(side="left", padx=3)
        ttk.Button(ctrl, text="💾  Detect PS5 USB",
                   command=self.on_detect_usb).pack(side="left", padx=3)
        ttk.Button(ctrl, text="📁  Browse for USB…",
                   command=self.on_browse_usb).pack(side="left", padx=3)
        self.lbl_usb = ttk.Label(ctrl, text="USB: (none selected)",
                                  foreground="#555")
        self.lbl_usb.pack(side="left", padx=10)

        # Monitor list
        ttk.Label(root, text="Connected monitor(s)",
                  font=("Helvetica", 12, "bold")).pack(anchor="w", padx=12, pady=(8, 2))
        mon_frame = ttk.Frame(root)
        mon_frame.pack(fill="x", padx=12)
        mon_cols = ("connector", "mfg", "model", "primary", "max_hz", "flags")
        self.mon_tree = ttk.Treeview(mon_frame, columns=mon_cols, show="headings",
                                      height=4)
        for c, w in zip(mon_cols, [180, 60, 180, 150, 70, 110]):
            self.mon_tree.heading(c, text=c.replace("_", " ").title())
            self.mon_tree.column(c, width=w, anchor="w")
        self.mon_tree.pack(fill="x", side="left", expand=True)
        s1 = ttk.Scrollbar(mon_frame, orient="vertical", command=self.mon_tree.yview)
        s1.pack(side="right", fill="y")
        self.mon_tree.configure(yscrollcommand=s1.set)

        # ── Bake action row ──────────────────────────────────────────────
        ttk.Label(root, text="Bake into the PS5 USB",
                  font=("Helvetica", 12, "bold")).pack(anchor="w", padx=12, pady=(8, 2))
        act_row = ttk.Frame(root)
        act_row.pack(fill="x", padx=12, pady=4)
        ttk.Button(act_row, text="🛡  Bake Universal (Safe 1080p60)",
                   command=self.on_bake_universal).pack(side="left", padx=3)
        ttk.Button(act_row, text="🖥  Bake Current Monitor",
                   command=self.on_bake_scanned).pack(side="left", padx=3)
        # History panel is hidden by default — toggle reveals it AND its
        # "bake from history" / save / export buttons.
        self._hist_visible = False
        self._hist_toggle_btn = ttk.Button(
            act_row, text="📂  History",
            command=self._on_history_toggle)
        self._hist_toggle_btn.pack(side="left", padx=3)

        # ── Collapsible history panel (hidden by default) ───────────────
        self._hist_panel = ttk.Frame(root)
        # Don't pack yet — only when toggled visible.
        hist_cols = ("label", "model", "primary", "max_hz", "flags", "sha")
        self.hist_tree = ttk.Treeview(
            self._hist_panel, columns=hist_cols, show="headings", height=6)
        for c, w in zip(hist_cols, [160, 180, 150, 70, 110, 180]):
            self.hist_tree.heading(c, text=c.replace("_", " ").title())
            self.hist_tree.column(c, width=w, anchor="w")
        self.hist_tree.pack(fill="both", side="top", expand=True, padx=12, pady=(8, 4))
        hist_btn_row = ttk.Frame(self._hist_panel)
        hist_btn_row.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Button(hist_btn_row, text="🖥  Bake from History",
                   command=self.on_bake_history).pack(side="left", padx=3)
        ttk.Button(hist_btn_row, text="➕  Save Current Monitor",
                   command=self.on_save_history).pack(side="left", padx=3)
        ttk.Button(hist_btn_row, text="📤  Export (JSON)",
                   command=self.on_export).pack(side="left", padx=3)

        # Custom resolution + refresh row
        custom_row = ttk.Frame(root)
        custom_row.pack(fill="x", padx=12, pady=4)
        ttk.Label(custom_row, text="🎯 Custom mode:").pack(side="left", padx=(0, 6))
        self._res_var = tk.StringVar()
        self._rate_var = tk.StringVar()
        # Build resolution display strings from RESOLUTION_REFRESH_OPTIONS
        self._res_labels = {
            "720p (1280×720)":  (1280,  720),
            "1080p (1920×1080)": (1920, 1080),
            "1440p (2560×1440)": (2560, 1440),
            "4K (3840×2160)":    (3840, 2160),
        }
        res_combo = ttk.Combobox(custom_row, textvariable=self._res_var,
                                  values=list(self._res_labels.keys()),
                                  state="readonly", width=20)
        res_combo.pack(side="left", padx=3)
        res_combo.set("1080p (1920×1080)")
        res_combo.bind("<<ComboboxSelected>>", self._on_res_change)
        self._rate_combo = ttk.Combobox(custom_row, textvariable=self._rate_var,
                                         state="readonly", width=8)
        self._rate_combo.pack(side="left", padx=3)
        ttk.Button(custom_row, text="🎯  Bake Custom Mode",
                   command=self.on_bake_custom).pack(side="left", padx=3)
        ttk.Label(custom_row,
                  text="(matches PS5 official output spec)",
                  foreground="#888").pack(side="left", padx=(8, 0))
        # Populate refresh combo for the initial resolution
        self._on_res_change()

        # Log
        ttk.Label(root, text="Activity log",
                  font=("Helvetica", 12, "bold")).pack(anchor="w", padx=12, pady=(8, 2))
        self.log_text = scrolledtext.ScrolledText(root, height=10, font=("Menlo", 10))
        self.log_text.pack(fill="both", expand=False, padx=12, pady=(0, 12))
        self.log_text.configure(state="disabled")

        self._refresh_history()
        self._log(f"ps5-display-wizard v{VERSION} ready. Click Scan to begin.")

    def _log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ── Handlers ──────────────────────────────────────────────────────

    def on_scan(self):
        # Wipe the previous result list AND the visible rows first, then
        # force Tk to flush so the UI never shows stale entries during scan.
        for r in self.mon_tree.get_children():
            self.mon_tree.delete(r)
        self._monitors = []
        self.root.update_idletasks()
        ts = time.strftime("%H:%M:%S")
        self._log(f"scan: starting fresh scan at {ts}")
        results = scan_connected_monitors()
        if not results:
            self._log("scan: no displays found via ioreg.")
            return
        for r in results:
            if r.get("_error"):
                self._log("scan: " + r["_error"])
                continue
            self._monitors.append(r)
            i = r["info"]
            flags = []
            if i["has_hdr"]: flags.append("HDR")
            if i["has_vrr"]: flags.append("VRR")
            if i["has_extension"]: flags.append(f"+{i['ext_count']}ext")
            self.mon_tree.insert("", "end", values=(
                r["connector"], i["manufacturer"], i["model"],
                i["primary_mode"], i["max_refresh_hz"] or "?",
                " ".join(flags) if flags else "-"
            ))
            self._log(f"scan: {r['connector']} → {i['manufacturer']} {i['model']} "
                      f"{i['primary_mode']} sha256:{i['edid_sha256'][:16]} "
                      f"size:{i['size_bytes']}B")

    def on_detect_usb(self):
        """Detect the PS5 Linux boot USB on any external drive — whether the
        loader files live on an already-mounted FAT volume OR on the hidden
        EFI partition of a fully-partitioned Linux install.

        Always shows a confirmation picker before selecting, even when there
        is only one candidate. Internal disks are excluded."""
        self._log("usb: scanning external drives…")
        vols = find_ps5_volumes()
        bakeable = [v for v in vols if v["score"] > 0]
        info_only = [v for v in vols if v["score"] == 0]
        for v in info_only:
            self._log(f"  [info] {v['device']} ({v['media_name']}, "
                      f"{v['fstype']}, {v['size_str']}) — {v['match']}")
            if v["note"]:
                self._log(f"         {v['note']}")
        if not bakeable:
            messagebox.showinfo(
                "No PS5 boot USB found",
                "No external drive holds the PS5 Linux loader files "
                "(bzImage + initrd.img + cmdline.txt).\n\n"
                "Plug in your PS5 USB, then click Detect again — or use "
                "Browse to pick a volume manually.")
            return
        chosen = self._pick_volume_from_list(bakeable)
        if not chosen:
            self._log("usb: no selection made.")
            return
        self._selected_usb = chosen["path"]
        self.lbl_usb.config(
            text=f"USB: {chosen['volume_name']}  "
                 f"({chosen['media_name']}, {chosen['fstype']}, "
                 f"{chosen['size_str']})  →  {chosen['path']}")
        self._log(f"usb: selected {chosen['device']} mounted at {chosen['path']} "
                  f"({chosen['media_name']}, {chosen['volume_name']}, "
                  f"{chosen['fstype']}, {chosen['size_str']}, match: {chosen['match']})")
        if chosen.get("mounted_by_us"):
            self._log("  note: the wizard auto-mounted this partition; it "
                      "stays mounted until you eject or reboot.")

    def _pick_volume_from_list(self, vols):
        """Modal picker that ALWAYS shows the available volumes, even with
        only one candidate. The user must explicitly confirm.
        Returns the chosen dict or None on cancel."""
        win = tk.Toplevel(self.root)
        win.title("Confirm PS5 USB")
        win.transient(self.root)
        win.grab_set()
        intro = ("Confirm which volume holds your PS5 Linux loader. "
                 "Only external drives are listed. "
                 "Double-click a row or press OK to confirm.")
        tk.Label(win, text=intro, justify="left", padx=12, pady=10,
                 wraplength=720).pack(anchor="w")
        cols = ("device", "name", "brand", "size", "fs", "score", "match")
        widths = (110, 140, 140, 80, 90, 60, 280)
        headings = ("Device", "Volume", "Brand", "Size",
                    "Filesystem", "Score", "Match reasons")
        tree = ttk.Treeview(win, columns=cols, show="headings",
                            height=min(8, max(3, len(vols))))
        for c, w, h in zip(cols, widths, headings):
            tree.heading(c, text=h)
            tree.column(c, width=w, anchor="w")
        for v in vols:
            tree.insert("", "end", values=(
                v["device"], v["volume_name"], v["media_name"],
                v["size_str"], v["fstype"], v["score"], v["match"]))
        tree.pack(fill="both", expand=True, padx=12, pady=6)
        tree.selection_set(tree.get_children()[0])

        chosen = {"value": None}
        def confirm():
            sel = tree.selection()
            if not sel: return
            idx = tree.index(sel[0])
            chosen["value"] = vols[idx]
            win.destroy()
        def cancel():
            win.destroy()
        row = ttk.Frame(win); row.pack(pady=8)
        ttk.Button(row, text="OK", command=confirm).pack(side="left", padx=4)
        ttk.Button(row, text="Cancel", command=cancel).pack(side="left", padx=4)
        tree.bind("<Double-1>", lambda _e: confirm())
        win.wait_window()
        return chosen["value"]

    def on_browse_usb(self):
        """Show every external mounted volume — even ones without the
        loader files — so the user can pick a USB that's freshly formatted
        or set up differently. Internal drives are excluded."""
        vols = self._list_external_volumes()
        if not vols:
            messagebox.showinfo(
                "No external volumes",
                "No external drives are currently mounted on this Mac. "
                "Plug a USB stick in and try again.")
            return
        chosen = self._pick_volume_from_list(vols)
        if not chosen:
            self._log("usb: browse cancelled.")
            return
        self._selected_usb = chosen["path"]
        self.lbl_usb.config(
            text=f"USB: {chosen['volume_name']}  "
                 f"({chosen['media_name']}, {chosen['fstype']}, "
                 f"{chosen['size_str']})  →  {chosen['path']}")
        if chosen["score"] == 0:
            self._log(f"usb: browsed to {chosen['path']} — note this volume "
                      f"does NOT yet have loader files; revert/bake will fail "
                      f"until bzImage + initrd.img + cmdline.txt are present.")
        else:
            self._log(f"usb: browsed to {chosen['path']} ({chosen['match']})")

    def _list_external_volumes(self):
        """Return every external mounted volume on this Mac as picker dicts,
        whether or not it holds the PS5 loader files. Empty list if there
        are none. Used by Browse so user can pick a blank USB."""
        out = []
        vols = Path("/Volumes")
        if not vols.is_dir():
            return out
        for entry in sorted(vols.iterdir()):
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            mount_info = _diskutil_info_plist(str(entry))
            if not mount_info:
                continue
            if mount_info.get("Internal", False):
                continue
            ident = mount_info.get("DeviceIdentifier", "")
            device = f"/dev/{ident}" if ident else ""
            parent_id = mount_info.get("ParentWholeDisk", "")
            if parent_id:
                parent = _diskutil_info_plist(parent_id) or {}
                media = parent.get("MediaName", "") or \
                        parent.get("IORegistryEntryName", "")
            else:
                media = mount_info.get("MediaName", "")
            volume_name = mount_info.get("VolumeName", entry.name) or entry.name
            score, why = score_ps5_usb(entry)
            out.append({
                "path":         str(entry),
                "device":       device,
                "media_name":   media,
                "volume_name":  volume_name,
                "size_str":     _bytes_to_gb_str(mount_info.get("TotalSize")),
                "fstype":       mac_volume_fs(entry) or "?",
                "score":        score,
                "match":        why if score > 0 else "(no loader files yet)",
                "note":         "Already mounted.",
                "mounted_by_us": False,
                "is_external":  True,
            })
        return out

    def _selected_scanned(self):
        sel = self.mon_tree.selection()
        if not sel:
            return None
        connector = self.mon_tree.item(sel[0])["values"][0]
        return next((m for m in self._monitors if m["connector"] == connector), None)

    def _selected_history(self):
        sel = self.hist_tree.selection()
        if not sel:
            return None
        sha_short = self.hist_tree.item(sel[0])["values"][5]
        for h in load_history():
            if h.get("edid_sha256", "").startswith(sha_short):
                return h
        return None

    def on_save_history(self):
        mon = self._selected_scanned()
        if not mon:
            messagebox.showinfo("Pick a monitor", "Select a row in 'Connected monitor(s)' first.")
            return
        add_monitor_to_history(mon)
        self._refresh_history()
        self._log(f"history: saved {mon['info']['manufacturer']} {mon['info']['model']}")

    def on_export(self):
        items = load_history()
        if not items:
            messagebox.showinfo("Empty", "History is empty.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile="ps5-display-wizard-history.json",
            title="Export history")
        if not path:
            return
        Path(path).write_text(json.dumps(items, indent=2))
        self._log(f"exported history → {path}")

    def _refresh_history(self):
        for r in self.hist_tree.get_children():
            self.hist_tree.delete(r)
        for h in load_history():
            flags = []
            if h.get("has_hdr"): flags.append("HDR")
            if h.get("has_vrr"): flags.append("VRR")
            self.hist_tree.insert("", "end", values=(
                h.get("label", ""), h.get("model", ""),
                h.get("primary_mode", ""), h.get("max_refresh_hz") or 0,
                " ".join(flags) if flags else "-",
                (h.get("edid_sha256") or "")[:16]
            ))

    def _on_history_toggle(self):
        """Show/hide the saved-monitors history panel."""
        if self._hist_visible:
            self._hist_panel.pack_forget()
            self._hist_visible = False
            self._hist_toggle_btn.config(text="📂  History")
        else:
            self._hist_panel.pack(fill="both", expand=True, padx=12, pady=(2, 6))
            self._hist_visible = True
            self._hist_toggle_btn.config(text="📂  History (hide)")
            self._refresh_history()

    def _on_res_change(self, _evt=None):
        """Refresh-rate dropdown contents follow the selected resolution."""
        label = self._res_var.get() or "1080p (1920×1080)"
        res = self._res_labels.get(label)
        if not res:
            return
        rates = RESOLUTION_REFRESH_OPTIONS.get(res, [60])
        rate_strs = [f"{r} Hz" for r in rates]
        self._rate_combo["values"] = rate_strs
        # Default to 60Hz if available, else first
        default = "60 Hz" if 60 in rates else rate_strs[0]
        self._rate_var.set(default)

    def on_bake_custom(self):
        """Bake a synthetic EDID for a user-chosen resolution + refresh rate.

        Identity (manufacturer / model / serial bytes) is taken from:
          1) the History row that's currently selected (if the panel is
             open and a row is highlighted), otherwise
          2) the currently-scanned monitor from the Connected list.
        Either way, Linux ends up labelling the display correctly. Timings
        come from our chosen mode (stripped of HDR/VRR/extensions for the
        MN864739 converter)."""
        if not self._selected_usb:
            messagebox.showinfo("Pick a USB", "Click Detect or Browse first.")
            return

        # Identity source: prefer History selection if visible and chosen.
        identity_edid = None
        mfg = "?"; model = "?"
        if self._hist_visible:
            h = self._selected_history()
            if h:
                identity_edid = bytes.fromhex(h["edid_hex"])
                mfg = h.get("manufacturer", "?")
                model = h.get("model", "?")
        if identity_edid is None:
            mon = self._selected_scanned()
            if mon:
                identity_edid = mon["edid"]
                info = mon["info"]
                mfg = info.get("manufacturer", "?")
                model = info.get("model", "?")
        if identity_edid is None:
            messagebox.showinfo(
                "Pick a monitor",
                "Custom bake uses a monitor's identity (manufacturer, model, "
                "serial) so Linux labels the display correctly.\n\n"
                "Either scan a connected monitor, OR open History and select "
                "a saved one.\n\n"
                "If you have no monitor to scan or in history, use "
                "Bake Universal (Safe 1080p60).")
            return

        label = self._res_var.get()
        res = self._res_labels.get(label)
        rate_str = self._rate_var.get().strip()
        if not res or not rate_str:
            messagebox.showinfo("Pick a mode",
                                "Choose a resolution and refresh rate first.")
            return
        try:
            refresh = int(rate_str.split()[0])
        except ValueError:
            messagebox.showerror("Bad refresh", f"Could not parse refresh: {rate_str!r}")
            return
        w, h = res
        ok = messagebox.askyesno(
            "Bake Custom EDID?",
            f"Bake a synthetic EDID for {mfg} {model} claiming exactly\n"
            f"  {w}x{h}@{refresh}Hz SDR\n\n"
            f"into:\n  {self._selected_usb}\n\n"
            f"The EDID will carry the monitor's identity (so Linux recognizes "
            f"it as {model}) but only advertise this one safe timing — no HDR, "
            f"no VRR, no extensions. HDMI audio is preserved.\n\n"
            f"If your screen can't actually do {w}x{h}@{refresh}Hz the display "
            f"will stay black; re-run this wizard with Bake Universal (Safe "
            f"1080p60) to recover.")
        if not ok:
            return
        try:
            edid = build_synthetic_edid(
                w, h, refresh,
                monitor_name=model[:12] if model and model != "?" else None,
                identity_source=identity_edid)
            slug = slugify(f"{mfg}-{model}-{w}x{h}-{refresh}hz")
            for ln in bake_edid_into_usb(self._selected_usb, slug, edid, strip=False):
                self._log("bake: " + ln)
            messagebox.showinfo(
                "Done",
                f"Custom {mfg} {model} {w}x{h}@{refresh}Hz EDID baked.\n\n"
                f"Eject the USB and move it to the PS5. If the display doesn't "
                f"come up, plug the USB back in and pick Bake Universal.")
        except Exception as e:
            self._log(f"bake: ERROR {e}")
            messagebox.showerror("Bake failed", str(e))

    def on_bake_universal(self):
        """Write amdgpu.force_1080p=1 to cmdline.txt — the official ps5-linux
        black screen fix. No EDID file, no cpio append, no video= parameter.
        Works on every display that supports 1080p."""
        if not self._selected_usb:
            messagebox.showinfo("Pick a USB", "Click Detect or Browse first.")
            return
        ok = messagebox.askyesno(
            "Bake Universal Safe Mode?",
            f"Apply the universal 1080p fix to:\n"
            f"  {self._selected_usb}\n\n"
            f"This sets amdgpu.force_1080p=1 in cmdline.txt — the official "
            f"ps5-linux black screen fix. No display scan needed. Every TV and "
            f"monitor supports 1080p60, so the PS5 Linux display will come up "
            f"on first boot regardless of which display is plugged in.\n\n"
            f"After the screen works, use the custom bake buttons to set your "
            f"native resolution.")
        if not ok:
            return
        try:
            logs = bake_universal_cmdline(self._selected_usb)
            for ln in logs:
                self._log("bake: " + ln)
            messagebox.showinfo(
                "Done",
                "Universal Safe Mode applied. Eject the USB and plug into PS5.\n\n"
                "Your display should come up at 1080p60.")
        except Exception as e:
            self._log(f"bake: ERROR {e}")
            messagebox.showerror("Bake failed", str(e))

    def on_bake_scanned(self):
        mon = self._selected_scanned()
        if not mon:
            messagebox.showinfo("Pick a monitor", "Select a row in 'Connected monitor(s)'.")
            return
        if not self._selected_usb:
            messagebox.showinfo("Pick a USB", "Click Detect or Browse first.")
            return
        i = mon["info"]
        ok = messagebox.askyesno(
            "Bake EDID?",
            f"Bake {i['manufacturer']} {i['model']} into:\n"
            f"  {self._selected_usb}\n\n"
            f"Will strip the EDID to a 128-byte safe block and edit "
            f"cmdline.txt + initrd.img. Backups are created first run.")
        if not ok:
            return
        try:
            label = slugify(f"{i['manufacturer']}-{i['model']}")
            for ln in bake_edid_into_usb(self._selected_usb, label, mon["edid"], strip=True):
                self._log("bake: " + ln)
            messagebox.showinfo("Done", "Bake complete. Eject the USB and move it to the PS5.")
        except Exception as e:
            self._log(f"bake: ERROR {e}")
            messagebox.showerror("Bake failed", str(e))

    def on_bake_history(self):
        h = self._selected_history()
        if not h:
            messagebox.showinfo("Pick", "Select a row in 'Saved monitors (history)'.")
            return
        if not self._selected_usb:
            messagebox.showinfo("Pick a USB", "Click Detect or Browse first.")
            return
        edid_bytes = bytes.fromhex(h["edid_hex"])
        label = slugify(h.get("label") or f"{h.get('manufacturer','')}-{h.get('model','')}")
        ok = messagebox.askyesno(
            "Bake EDID from history?",
            f"Bake {h.get('label','?')} into:\n  {self._selected_usb}\n\n"
            f"EDID sha: {h.get('edid_sha256','?')[:16]}")
        if not ok:
            return
        try:
            for ln in bake_edid_into_usb(self._selected_usb, label, edid_bytes, strip=True):
                self._log("bake: " + ln)
            messagebox.showinfo("Done", "Bake complete. Eject and move to PS5.")
        except Exception as e:
            self._log(f"bake: ERROR {e}")
            messagebox.showerror("Bake failed", str(e))

def main():
    if "--scan" in sys.argv:
        for m in scan_connected_monitors():
            if m.get("_error"):
                print("ERROR:", m["_error"]); continue
            i = m["info"]
            print(f"{m['connector']}\t{i['manufacturer']} {i['model']}\t"
                  f"{i['primary_mode']}\tmaxHz={i['max_refresh_hz']}\t"
                  f"size={i['size_bytes']}\tsha256:{i['edid_sha256'][:16]}")
        return
    if "--list-usb" in sys.argv:
        for u in find_ps5_usbs():
            print(f"{u['path']}\tfs={u['fstype']}\tscore={u['score']}\t{u['match']}")
        return
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return
    root = tk.Tk()
    WizardApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
