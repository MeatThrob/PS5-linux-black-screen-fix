# PS5 Display Wizard

Bakes a converter-safe EDID into the PS5 Linux boot USB so the kernel uses
your monitor reliably on every cold boot. macOS and Linux GUIs.

## Why this exists

The PS5's HDMI port is driven by an **MN864739 DP→HDMI converter**. The
kernel never sees the monitor directly — it sees `DP-1`, and the converter
translates. On many monitors the converter fails link training when the
monitor's EDID advertises HDR10, BT.2020, 12-bit colour, VRR, or refresh
rates the converter doesn't support cleanly. Symptom: black screen, or
works one boot in N.

The fix is to feed the kernel a known-clean EDID via
`drm.edid_firmware=DP-1:edid/<file>.bin`, injected into the loader's
initrd as a cpio fragment. The kernel reads our file instead of querying
the monitor over DDC, and the converter never sees anything it can't pass
through.

This wizard automates the whole thing.

## Three ways to use it

| Button | Use when |
| --- | --- |
| **🛡 Bake Universal (Safe 1080p60)** | You want any screen to work first try. No scan needed. |
| **🖥 Bake Current Monitor** | You've scanned a monitor and want its native resolution, stripped of HDR/VRR. |
| **🎯 Bake Custom Mode** | You want a specific resolution + refresh rate from the PS5 spec list. |

The PS5 modes the custom dropdown offers are 720p60, 1080p60, 1080p120,
1440p60, 1440p120, and 4K60 — the same modes Sony's firmware emits.

## What gets written to the USB

```
<USB>/edid/<slug>-full.bin   the 256-byte EDID (base block + audio extension)
<USB>/cmdline.txt            edited to add drm.edid_firmware= and video=
<USB>/cmdline.txt.tv         first-run backup of the stock cmdline
<USB>/initrd.img             original + appended cpio fragment with our EDID
<USB>/initrd.img.tv          first-run backup of the stock initrd
<USB>/safe-boot.sh           recovery script you can SSH in and run
```

The `.tv` files are written exactly once and never overwritten, so revert
always works.

## Audio

Every EDID baked by this wizard carries a minimal CEA-861 extension block
that advertises **Basic Audio supported** and nothing else — no HDR, no
VRR, no extra video modes that would trip the converter. PulseAudio /
PipeWire pick up HDMI audio normally; you should not see "Dummy Output".

## Requirements

### macOS

- Python 3.10+ from python.org (or Homebrew with `python-tk`).
- Tkinter (bundled with python.org installers).
- No third-party packages.

### Linux

- Python 3.10+
- PyGObject + GTK 3 bindings:
  - Debian / Ubuntu: `sudo apt install python3-gi gir1.2-gtk-3.0`
  - Fedora: `sudo dnf install python3-gobject gtk3`
  - Arch: `sudo pacman -S python-gobject gtk3`

## Running it

```bash
python3 ps5_display_wizard_mac.py   # macOS
python3 ps5_display_wizard.py       # Linux
```

Workflow:

1. **Scan Connected Monitor(s)** — fills the Connected list. On Linux this
   forces a fresh DRM probe so a hot-swapped monitor is picked up
   correctly.
2. **Detect PS5 USB** — finds every external volume that holds the PS5
   loader files. macOS handles unmounted EFI partitions by prompting for
   admin access to mount them. You always get a picker so you can confirm
   which drive to bake into.
3. Choose a bake mode (Universal, Current Monitor, or Custom).
4. Eject the USB, plug it into the PS5, boot Linux.

If the picture is bad after boot: plug the USB back in and bake
Universal. You're back to a known-good state in one click.

## Recovery from a bad bake

The wizard never writes outside `cmdline.txt`, `initrd.img`, and the
`edid/` directory. Both `cmdline.txt` and `initrd.img` have `.tv` backups
written on first bake. If you can SSH into the PS5 from another machine,
`sudo /boot/efi/safe-boot.sh` restores the originals and kexecs.

## Headless modes

```bash
python3 ps5_display_wizard.py --scan        # list connected EDIDs
python3 ps5_display_wizard.py --list-usb    # list detected PS5 USBs
python3 ps5_display_wizard.py --help
```

## Caveat: running on the PS5 itself

If the PS5 boots with `drm.edid_firmware=DP-1:edid/<file>.bin` already
active, sysfs reflects the forced EDID, not the monitor's real one. To
read the real EDID after a forced bake:

- Run the wizard on a normal Linux PC / macOS host with the monitor
  plugged in, OR
- Revert to the TV baseline first (`sudo /boot/efi/safe-boot.sh`), then
  re-scan on the PS5.

## History

Saved to `~/.config/ps5-display-wizard/monitors.json`. Open the
**📂 History** panel to view, save the current scan, bake a saved entry,
or export a portable JSON.

## How the bake works (for the curious)

- `build_synthetic_edid(w, h, hz, identity_source=...)` writes a 128-byte
  base block with CVT-RB v2 timings, plus a 128-byte CEA-861 extension
  containing only the Basic Audio flag.
- `strip_edid_to_minimal(real_edid)` is the per-monitor path: keep
  byte 0–127 of the real EDID, zero out the extension count, fix the
  checksum.
- `build_edid_cpio(name, bytes)` produces a `newc` cpio fragment
  containing `lib/firmware/edid/<name>` and `usr/lib/firmware/edid/<name>`
  (cover both /usr-merged and split layouts).
- The kernel's concatenated-cpio feature means we can append our fragment
  to the existing initrd without decompression/repacking — later cpio
  entries overlay earlier ones.

## License

CC0 / public domain. Fork freely; upstream the fixes that work for you.
