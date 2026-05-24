# PS5 Display Wizard — Project Status

Current state of the project as of the latest commit. New contributors
or new dev sessions should read this end-to-end before changing code.

## Goal

Make a black-screen PS5 Linux install come up reliably on whatever
monitor or TV the user has plugged in. The fix is a clean EDID baked
into the loader USB; the wizard is the GUI that produces it.

## What's built

Two wizards, one shared engine.

| File | Lines | Role |
| --- | --- | --- |
| `ps5_display_wizard_mac.py` | ~1700 | macOS Tkinter GUI |
| `ps5_display_wizard.py` | ~1450 | Linux GTK 3 GUI |
| `README.md` | — | User-facing entry point |
| `README_MAC.md` | — | macOS-specific notes |

The EDID builder (`build_synthetic_edid`), per-monitor stripper
(`strip_edid_to_minimal`), cpio writer (`build_edid_cpio`), and bake
logic (`bake_edid_into_usb`, `revert_usb_to_stock`) are byte-for-byte
identical across both wizards. Anything baked on one is interchangeable
with the other.

The non-portable pieces are EDID acquisition and USB detection:

| Concern | macOS | Linux |
| --- | --- | --- |
| EDID source | `ioreg -lw0` scan for `EDID`/`IODisplayEDID` keys | `/sys/class/drm/card*/edid` |
| Force fresh read | (n/a — IOKit doesn't cache like sysfs) | Write `"detect"` to `/sys/class/drm/<conn>/force` before reading |
| External-volume listing | `diskutil info -plist`, `Internal: False` filter | `/sys/block/<dev>/removable` and USB-in-device-path |
| Hidden EFI partition mount | `diskutil mount`, falling back to admin prompt via `osascript` | Already mounted by udisks |

## User-facing flows

The UI has exactly three bake buttons, in this order:

1. **🛡 Bake Universal (Safe 1080p60)** — synthesizes a 1920x1080@60Hz
   SDR EDID and bakes it. No scan needed. Works on almost any display
   ever made because 1080p60 is the universal fallback mode.

2. **🖥 Bake Current Monitor** — uses the currently-scanned monitor's
   EDID, stripped to its 128-byte base block. Preserves native
   resolution.

3. **🎯 Bake Custom Mode** — synthetic EDID with PS5-spec resolution +
   refresh dropdowns. Identity bytes come from either the History row
   currently selected, or the current scan.

The PS5 mode list (must match Sony's HDMI output spec, see
`RESOLUTION_REFRESH_OPTIONS`):

| Resolution | Refresh rates |
| --- | --- |
| 720p | 60 |
| 1080p | 60, 120 |
| 1440p | 60, 120 |
| 4K | 60 |

4K@120 is deliberately excluded — it requires a CTA-861 extension
block, and extensions are exactly what trips the MN864739 converter.

## Audio

Every synthetic EDID baked by the wizard is **128 bytes only** — no
extension blocks of any kind. The PS5's MN864739 DP→HDMI converter fails
link training when the EDID contains ANY extension block, even a minimal
CEA-861 Basic Audio block. This is a hard hardware constraint; no
workaround exists short of bypassing the converter entirely.

As a result, HDMI audio via EDID advertisement is not available. The
kernel's snd_hda_intel codec will not see audio capability from a baked
EDID. This matches the upstream ps5-linux known-issues list ("HDMI audio
output does not work on some monitors"). The display fix takes priority.

The per-monitor stripper (`strip_edid_to_minimal`) also returns 128 bytes
with extension count zeroed, consistent with the above constraint.

## USB detection

`find_ps5_volumes()` returns a unified list of:

- Already-mounted external volumes that hold the loader files.
- Unmounted external partitions visible via `diskutil` (macOS only) or
  `/proc/mounts` (Linux). On macOS, FAT/EFI partitions are auto-mounted
  via `diskutil`, falling back to an admin password prompt via
  `osascript` when macOS hides EFI partitions from unprivileged users.

The picker dialog is shown **every time** — even with exactly one
candidate — so the user explicitly confirms before any write happens.
Internal disks are always excluded.

The picker columns are: Device, Volume, Brand, Size, Filesystem, Score,
Match reasons.

## File-system layout on the USB after a bake

```
<USB>/edid/<slug>-full.bin     256-byte EDID (base block + audio ext)
<USB>/edid/<slug>-stripped.bin 128-byte EDID (per-monitor stripper only)
<USB>/cmdline.txt              edited with drm.edid_firmware= and video=
<USB>/cmdline.txt.tv           first-run backup of stock cmdline
<USB>/initrd.img               original + appended cpio fragment
<USB>/initrd.img.tv            first-run backup of stock initrd
<USB>/safe-boot.sh             SSH recovery script (idempotent)
```

Both `.tv` files are written exactly once and never overwritten. They
are the only recovery path that survives a wizard misuse.

## Verified primitives

These were validated against a live ASUS VG27WQ at 2560x1440 plus a
SanDisk Ultra 123 GB PS5 Linux USB (`disk6` on macOS).

- macOS EDID read: detects VG27WQ at 2560x1440@60Hz, max_refresh 144Hz,
  HDR=True, ext_count=1. SHA256 `cbe71fff84c2f075f76f554c65cf4d39…`.
- macOS USB detect: finds the EFI `boot` partition (`/dev/disk6s2`,
  FAT32) and surfaces the ext4 root partition (`disk6s1`) as
  informational.
- All 6 synthetic modes (720p60, 1080p60, 1080p120, 1440p60, 1440p120,
  4K60) build to a 256-byte EDID with both base-block and extension
  checksums summing to 0 mod 256. SHA256s are stable and identical
  between macOS and Linux wizards.
- Privilege escalation prompt: `osascript` admin prompt fires when
  macOS refuses an unprivileged `diskutil mount` of the EFI partition.

## The kernel whitelist (THE most important fact in this project)

The PS5 Linux amdgpu driver has a function `isHdmiModeValid()` in
`drivers/gpu/drm/amd/display/amdgpu_dm/amdgpu_dm.c` (ps5-linux-patches)
that hard-rejects every display mode outside this list:

| VIC | Mode | Pclk (kHz) |
| --- | --- | --- |
| 16 | 1920×1080 @ 60 | 148500 |
| 63 | 1920×1080 @ 120 | 297000 (kernel ≥7.0.3) |
| — | 2560×1440 @ 60 | **exactly 241500 or 241700** (`==` check) |
| 97 | 3840×2160 @ 60 | 594000 (FLAVA3 auto-subsamples to 4:2:0) |

Anything else returns `MODE_ERROR`. The screen stays black. The wizard's
`_validate_edid_against_whitelist()` enforces this on every bake.

**Why CVT-RB v2 fails for 1440p:** CVT-RB gives 241250 kHz, not 241500.
The whitelist uses `==` not range-match. We ship CEA-861 canonical
timings (241500 kHz) for every advertised mode.

## The MN864739 / FLAVA3 chip (verified facts)

- DP→HDMI converter on PS5 phat CFI-1000. HDMI 2.0 only — no FRL, no
  HDMI 2.1, no native 4K120, no native HDR pipeline.
- The chip has TMDS-only output (4 differential lanes, no FRL signaling
  pins). HDMI 2.0 max is ~594 MHz TMDS (4K60).
- Black-screen failures are NOT chip errata — they are `isHdmiModeValid`
  rejections in the amdgpu DCN driver, plus deliberate workarounds for
  the chip's broken DP-link-training sequence (see
  `dcn10_link_encoder_enable_dp_output` which is `#ifdef`ed out entirely
  for `CONFIG_X86_PS5`).
- DP link is hardcoded to HBR3×4 (link_dp_capability.c). DPCD writes for
  unsupported addresses are silently skipped. FEC is disabled.
- 2026-05-17 commit `fe852d3c` "fix link training failure" comments out
  `configParamFlava3Pre()` + `configLinkTrainingFlava3()` — letting the
  Sony-OS pre-set state persist instead of re-issuing I2C bringup.

## HDR / 120Hz / 4K reality (as of kernel 7.0.8)

- **HDR: NOT in the kernel yet.** Still on the TODO list in the
  ps5-linux-patches README. `setHdmiBasicVideoConfigFlava3` has no DRM
  InfoFrame programming. EDID with HDR Static Metadata block won't help
  because the TX-side InfoFrame path doesn't exist.
- **120Hz: only 1080p120 works** (VIC 63, added in commit 23375e09 on
  2026-05-06). 1440p120 and 4K120 are not on the whitelist.
- **VRR: not supported.** Requires HDMI 2.1 FRL the converter can't do.
- **4K60 works** because FLAVA3 silently switches to YCbCr 4:2:0 when
  programmed for VIC 97 (writes to register 0x70c0=0xdc, 0x7072=0x01,
  0x7074=0x07 in `setHdmiBasicVideoConfigFlava3`).

## Known limitations

- **Hot-swap on PS5 itself is not solved.** Upstream ps5-linux kernel
  patches `#ifdef` out `amdgpu_dm_hpd_init`. A bake change requires a
  full reboot.
- **ext4 partitions are read-only on macOS.** Bakes go to the FAT
  partition only. The M.2 rootfs's `/lib/firmware/edid/` must contain
  the EDID file separately (it does, from the Phase 2 manual install).
- **Sysfs may cache the previous monitor's EDID** after hot-swap. The
  Linux wizard writes `"detect"` to `/sys/class/drm/<conn>/force` before
  reading; best-effort.
- **GNOME's display settings panel breaks the screen.** GNOME saves
  `~/.config/monitors.xml` and re-applies it after login. If it picks a
  mode outside the whitelist (which it usually does), the screen goes
  black on every subsequent boot. `safe-boot.sh` wipes this file as
  part of recovery.

## Abandoned approaches (do not retry without new information)

See [archive/](archive/) for the full background. Briefly:

- **Native PS5 ELF reading EDID via libSceVideoOut.** Every entry point
  (sceVideoOutOpen, sceVideoOutSysOpenInternal, sceVideoOutGetMonitorInfo,
  sceVideoOutSysGetMonitorInfo_) rejects calls from a homebrew context
  on FW 4.03 with `0x80290001` (INVALID_VALUE) or returns a bogus
  pseudo-handle `0x4e100100`. Holds true even after ucred escalation to
  `0x4800000000010003` + all-0xFF caps. The video-out session is owned
  exclusively by `SceShellUI`.
- **`/dev/hdmi` ioctl probing on PS5.** Brute-forced every ioctl type ×
  num × IOR/IOWR × sizes 128–2048; no EDID magic header ever appeared
  in any response.

## Development setup

- Python 3.10+ on both platforms.
- macOS: Tkinter (bundled with python.org installers, or
  `brew install python-tk@<version>`).
- Linux: PyGObject + GTK 3 (`sudo apt install python3-gi gir1.2-gtk-3.0`).
- No third-party Python packages anywhere; everything is stdlib.

Both wizards have a startup guard that prints copy-paste install
commands if the GUI toolkit is missing.

## Headless modes

```bash
python3 ps5_display_wizard.py --scan        # list connected EDIDs as JSON
python3 ps5_display_wizard.py --list-usb    # list detected PS5 USBs
python3 ps5_display_wizard_mac.py --scan
python3 ps5_display_wizard_mac.py --list-usb
```

Useful for scripting bakes from CI or testing the detection logic without
opening the GUI.

## How to extend safely

The bake pipeline has a strict contract:

1. The 128-byte base block always validates: magic `00 FF FF FF FF FF FF 00`
   at bytes 0-7, checksum sum-of-128 = 0 mod 256.
2. Any extension block is also 128 bytes with its own checksum.
3. `cmdline.txt` rewrite preserves every token that isn't
   `drm.edid_firmware=` or `video=DP-1`. Audio tokens (none currently
   from upstream ps5-linux-loader) and other kernel params survive.
4. Backups (`*.tv`) are written exactly once on first bake and never
   overwritten. Revert always succeeds if the user hasn't deleted them
   manually.

Before merging any change to the EDID builder, run the validation suite:

```bash
cd ps5-display-wizard
python3 -c "
import importlib.util, hashlib
spec = importlib.util.spec_from_file_location('w', 'ps5_display_wizard_mac.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ok = True
for (w,h), hzs in m.RESOLUTION_REFRESH_OPTIONS.items():
    for hz in hzs:
        e = m.build_synthetic_edid(w, h, hz)
        bc = sum(e[:128]) % 256
        ec = sum(e[128:]) % 256
        if len(e) != 256 or bc or ec: ok = False
        print(f'{w}x{h}@{hz}Hz len={len(e)} base_chk={bc} ext_chk={ec}')
print('ALL VALID' if ok else 'FAIL')
"
```

Both wizards should print the same SHA256 for every mode. If they
diverge, the byte-identical invariant is broken and the fix is wrong.
