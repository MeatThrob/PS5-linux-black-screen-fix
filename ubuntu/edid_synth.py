"""
edid_synth.py — MeatHandler EDID synthesizer

Builds a valid 128-byte base EDID block from a monitor's capabilities, with
the preferred-mode DTD set to whatever the PS5 kernel's isHdmiModeValid()
gate will actually accept. No extension block (the FLAVA3 retimer fails
with them).

Imported by the GUI; no CLI, no main().
"""

from __future__ import annotations


# CTA-861 timing tables for the four PS5-gate-accepted modes.
# Each: (h_active, h_blank, h_fp, h_sw, v_active, v_blank, v_fp, v_sw, pclk_khz)
_TIMINGS = {
    "1080p60":  (1920, 280, 88, 44, 1080, 45, 4, 5, 148500),   # VIC 16
    "1080p120": (1920, 280, 88, 44, 1080, 45, 4, 5, 297000),   # VIC 63
    "1440p60":  (2560, 160, 48, 32, 1440, 41, 3, 5, 241500),   # 1440p gate
    "2160p60":  (3840, 560, 176, 88, 2160, 90, 8, 10, 594000), # VIC 97
}

_MODE_META = {
    "1080p60":  {"label": "1080p @ 60 Hz",  "width": 1920, "height": 1080, "rate": 60,  "vic": 16, "pclk_khz": 148500},
    "1080p120": {"label": "1080p @ 120 Hz", "width": 1920, "height": 1080, "rate": 120, "vic": 63, "pclk_khz": 297000},
    "1440p60":  {"label": "1440p @ 60 Hz",  "width": 2560, "height": 1440, "rate": 60,  "vic": 0,  "pclk_khz": 241500},
    "2160p60":  {"label": "2160p @ 60 Hz",  "width": 3840, "height": 2160, "rate": 60,  "vic": 97, "pclk_khz": 594000},
}


def _encode_mfr(mfr_code: str) -> bytes:
    """Encode 3-letter manufacturer code as two bytes (5 bits per letter)."""
    code = (mfr_code or "AAA").upper().strip()[:3].ljust(3, "A")
    vals = [(ord(c) - ord("A") + 1) & 0x1F for c in code]
    raw = (vals[0] << 10) | (vals[1] << 5) | vals[2]
    return bytes([(raw >> 8) & 0xFF, raw & 0xFF])


def _build_dtd(mode_key: str, h_size_mm: int = 600, v_size_mm: int = 340) -> bytes:
    h_act, h_blank, h_fp, h_sw, v_act, v_blank, v_fp, v_sw, pclk_khz = _TIMINGS[mode_key]
    pclk_raw = pclk_khz // 10  # fits in 16 bits for all gate-accepted modes

    dtd = bytearray(18)
    dtd[0] = pclk_raw & 0xFF
    dtd[1] = (pclk_raw >> 8) & 0xFF
    dtd[2] = h_act & 0xFF
    dtd[3] = h_blank & 0xFF
    dtd[4] = ((h_act >> 4) & 0xF0) | ((h_blank >> 8) & 0x0F)
    dtd[5] = v_act & 0xFF
    dtd[6] = v_blank & 0xFF
    dtd[7] = ((v_act >> 4) & 0xF0) | ((v_blank >> 8) & 0x0F)
    dtd[8] = h_fp & 0xFF
    dtd[9] = h_sw & 0xFF
    dtd[10] = ((v_fp & 0x0F) << 4) | (v_sw & 0x0F)
    dtd[11] = (
        (((h_fp >> 8) & 0x03) << 6)
        | (((h_sw >> 8) & 0x03) << 4)
        | (((v_fp >> 4) & 0x03) << 2)
        | ((v_sw >> 4) & 0x03)
    )
    dtd[12] = h_size_mm & 0xFF
    dtd[13] = v_size_mm & 0xFF
    dtd[14] = ((h_size_mm >> 4) & 0xF0) | ((v_size_mm >> 8) & 0x0F)
    dtd[15] = 0
    dtd[16] = 0
    dtd[17] = 0x1E  # progressive, digital separate sync, vsync+, hsync+
    return bytes(dtd)


def _build_name_desc(model_name: str) -> bytes:
    """0xFC monitor name descriptor — 13 ASCII chars, 0x0A terminator if short, 0x20 pad."""
    desc = bytearray(18)
    desc[0:5] = bytes([0x00, 0x00, 0x00, 0xFC, 0x00])
    payload = (model_name or "Monitor").encode("ascii", errors="replace")[:13]
    if len(payload) < 13:
        payload = payload + b"\x0A" + (b"\x20" * (12 - len(payload)))
    desc[5:18] = payload
    return bytes(desc)


def _build_range_desc(vmin: int, vmax: int, hmin: int = 30, hmax: int = 160,
                      max_pclk_10mhz: int = 119) -> bytes:
    """0xFD display range limits descriptor."""
    desc = bytearray(18)
    desc[0:5] = bytes([0x00, 0x00, 0x00, 0xFD, 0x00])
    desc[5] = vmin & 0xFF
    desc[6] = vmax & 0xFF
    desc[7] = hmin & 0xFF
    desc[8] = hmax & 0xFF
    desc[9] = max_pclk_10mhz & 0xFF
    desc[10] = 0x00  # default GTF
    desc[11:18] = bytes([0x0A, 0x20, 0x20, 0x20, 0x20, 0x20, 0x20])
    return bytes(desc)


def _pick_preferred_mode(capabilities: dict) -> str:
    res = capabilities.get("resolutions", []) or []
    hdmi_hz = int(capabilities.get("hdmi_hz", 60) or 60)
    has = lambda s: s in res
    if hdmi_hz >= 120 and has("1920x1080"):
        return "1080p120"
    if has("3840x2160"):
        return "2160p60"
    if has("2560x1440"):
        return "1440p60"
    return "1080p60"


def synthesize_edid(mfr_code: str, product_id: int, model_name: str,
                    capabilities: dict, serial_str: str = "") -> bytes:
    """Build a valid 128-byte base EDID for the given monitor.

    The preferred-mode DTD (slot 0) is picked from capabilities and is always
    one of the four modes the PS5 kernel's isHdmiModeValid() gate accepts.
    Slot 1 is always 1080p60 as a universal fallback.
    """
    capabilities = capabilities or {}
    base = bytearray(128)

    # Header magic
    base[0:8] = bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00])
    # Manufacturer + product ID
    base[8:10] = _encode_mfr(mfr_code)
    pid = int(product_id) & 0xFFFF
    base[10] = pid & 0xFF
    base[11] = (pid >> 8) & 0xFF
    # Serial number (numeric if possible)
    serial_int = 0
    if serial_str:
        digits = "".join(c for c in serial_str if c.isdigit())
        if digits:
            try:
                serial_int = int(digits[-9:]) & 0xFFFFFFFF
            except ValueError:
                serial_int = 0
    base[12:16] = serial_int.to_bytes(4, "little")
    # Week / year (week 4 of 2024)
    base[16] = 0x04
    base[17] = 2024 - 1990  # = 34
    # EDID version 1.3
    base[18] = 0x01
    base[19] = 0x03
    # Basic display: digital, 8bpc, max image 60cm x 34cm, gamma 2.2
    base[20] = 0x80
    base[21] = 60
    base[22] = 34
    base[23] = 0x78
    # Feature support: standby/suspend/off, RGB, std sRGB, preferred timing in DTD0
    base[24] = 0xEA
    # Color characteristics — standard sRGB
    base[25:35] = bytes([0xEE, 0x95, 0xA3, 0x54, 0x4C, 0x99, 0x26, 0x0F, 0x50, 0x54])
    # Established timings 1 + 2 + manufacturer reserved (640x480, 800x600, 1024x768)
    base[35] = 0x21
    base[36] = 0x08
    base[37] = 0x00
    # Standard timings (8 slots × 2 bytes). Slot 0 = 1920x1080@60.
    base[38] = (1920 // 8) - 31  # = 209 = 0xD1
    base[39] = 0xC0              # 16:9, 60Hz
    base[40] = (1024 // 8) - 31
    base[41] = 0x40
    base[42] = (800 // 8) - 31
    base[43] = 0x40
    base[44:54] = bytes([0x01, 0x01] * 5)  # remaining slots unused

    # DTDs
    preferred = _pick_preferred_mode(capabilities)
    base[54:72] = _build_dtd(preferred)
    base[72:90] = _build_dtd("1080p60")  # universal fallback

    # Name descriptor
    base[90:108] = _build_name_desc(model_name)

    # Range limits — use capabilities to size vmax + max pclk
    vmax = max(60, int(capabilities.get("max_hz", 120) or 120))
    if vmax < 60:
        vmax = 60
    if vmax > 240:
        vmax = 240
    max_pclk_10mhz = 119  # 1190 MHz — matches typical HDMI 2.1 EDIDs
    base[108:126] = _build_range_desc(vmin=48, vmax=vmax, max_pclk_10mhz=max_pclk_10mhz)

    # Extension blocks: 0 (CRITICAL — FLAVA3 retimer fails with any extension)
    base[126] = 0

    # Checksum
    s = sum(base[:127]) & 0xFF
    base[127] = (256 - s) & 0xFF

    return bytes(base)


def get_modes_for_monitor(capabilities: dict) -> list[dict]:
    """Return PS5-gate-accepted modes only.

    Always includes 1080p@60. Adds 1080p@120 if hdmi_hz >= 120. Adds 4K@60 if
    3840x2160 is in resolutions. Adds 1440p@60 if 2560x1440 is in resolutions.
    Never returns 1440p@120 or 4K@120 (kernel-rejected).
    """
    capabilities = capabilities or {}
    res = capabilities.get("resolutions", []) or []
    hdmi_hz = int(capabilities.get("hdmi_hz", 60) or 60)

    modes: list[dict] = []
    modes.append(dict(_MODE_META["1080p60"]))
    if hdmi_hz >= 120:
        modes.append(dict(_MODE_META["1080p120"]))
    if "2560x1440" in res:
        modes.append(dict(_MODE_META["1440p60"]))
    if "3840x2160" in res:
        modes.append(dict(_MODE_META["2160p60"]))
    return modes
