# PS5 Linux black-screen fix — CachyOS / Arch

A boot-fix-only port of the PS5 Linux EDID normalizer for **CachyOS** and **Arch Linux**. Fixes the black-screen-on-display-init that hits many monitors when running Linux on PS5.

## What it does

Normalizes (or passes through) the connected monitor's EDID at the **earliest possible boot stage** — inside the initramfs, before `amdgpu` probes the display. This prevents the PS5 HDMI bridge (FLAVA3 / MN864739) from rejecting the monitor's native EDID and black-screening.

**Scope (intentional):**

- Boot-fix only. No GUI. No screen-dim. No runtime resolution switcher.
- Works automatically with any monitor that has worked on the PS5 once.
- Locked to a single connected monitor at boot (the one plugged in when the initramfs runs).

For the full-featured Ubuntu version (with display-switcher GUI, screen dim, etc.), see [`../ubuntu/`](../ubuntu/).

## Why this exists

The PS5 has no HDMI hot-plug detect. `amdgpu` sees the monitor's EDID exactly once, from the firmware loader. If that EDID has timings the PS5 kernel gate (`isHdmiModeValid` in `ps5/hdmi.c`) rejects, the screen never lights up.

Fixing this in userspace (Settings → Displays, `xrandr`, etc.) is **too late** — the kernel has already failed to bring the display up. The fix must run in the initramfs, before `amdgpu` loads. That's exactly what this installer wires up.

## How to install

Boot CachyOS on your PS5 to any working state (even a black-screen boot — you can SSH in and run this from a working machine on your LAN). Then:

```sh
cd cachyos
chmod +x ps5-autoedid-cachyos-install.sh
sudo ./ps5-autoedid-cachyos-install.sh
sudo reboot
```

The installer:

1. Installs `python3` if missing (via `pacman -S --needed --noconfirm` — never `-Sy` / `-Syu`, to avoid CachyOS partial-upgrade breakage).
2. Drops the EDID normalizer at `/usr/local/sbin/ps5-autoedid` + monitor capability database at `/usr/local/lib/ps5-autoedid/monitor_db.py`.
3. Installs a systemd oneshot (`ps5-autoedid.service`) that runs before the display manager, as a fallback.
4. Installs **mkinitcpio hooks** at `/etc/initcpio/install/ps5-autoedid` and `/etc/initcpio/hooks/ps5-autoedid` — this is the real fix.
5. Patches `/etc/mkinitcpio.conf`:
   - Adds `amdgpu` to `MODULES=()`.
   - Adds `ps5-autoedid` to `HOOKS=()` after `kms`.
   - **Removes `autodetect`** (PS5-Linux requirement — `autodetect` can strip `amdgpu` if it isn't loaded at build time).
   - Bakes `/lib/firmware/edid/auto.bin` into `FILES=()`.
6. Captures the current monitor's EDID, runs the passthrough-or-normalize logic, writes `auto.bin`.
7. Regenerates the initramfs and deploys it per the PS5-Linux multi-distro kexec contract:
   - `/boot/efi/initrd-cachyos.img` (what `kexec-cachyos.sh` reads)
   - `/boot/efi/cmdline-cachyos.txt` (with `drm.edid_firmware=DP-1:edid/auto.bin`, `video=DP-1:e`, `firmware_class.path=/lib/firmware:/run/firmware`)
8. Detects external-SSD vs internal NVMe and applies the deferred-`amdgpu`-load fix only when needed.
9. Drops `/boot/efi/safe-boot-cachyos.sh` so you can revert if anything goes wrong.

## Verify

After reboot:

```sh
cat /var/log/ps5-autoedid.log
```

You should see one of two outcomes for your monitor:

- **`PASSTHROUGH: native mode passes gate [...]`** — the monitor's native preferred mode is already PS5-kernel-compatible; the EDID is written through unchanged (preserves centering, IT-Content flag, etc.).
- **`REWRITE: native rejected [...]`** followed by `normalized DTD pclk: X -> Y kHz` — the EDID was rewritten to a kernel-accepted timing; the screen will now light up.

## Recovery

If you can't see the screen after install, SSH in and:

```sh
sudo /boot/efi/safe-boot-cachyos.sh
sudo reboot
```

This restores the pre-install `cmdline-cachyos.txt`. The initramfs hook is harmless without the cmdline override (it just writes `/run/firmware/edid/auto.bin` that the kernel never reads).

## What's verified vs what isn't

- **Verified:** the EDID normalize / passthrough / VIC 63 injection logic is byte-for-byte the same as the Ubuntu version, which is verified on real PS5 hardware (kernel 7.0.5) with multiple monitors.
- **Community testing needed:** the CachyOS-specific wiring (mkinitcpio hooks, pacman dep install, kexec deploy to `/boot/efi/initrd-cachyos.img`) follows the verified PS5-Linux multi-distro boot contract from [github.com/ps5-linux/ps5-linux-image](https://github.com/ps5-linux/ps5-linux-image) but has not yet been tested on a live CachyOS PS5 install. Report issues at the repo or the PS5-Linux Discord.

## PS5 kernel mode gate (for the curious)

The PS5's HDMI bridge accepts only a fixed set of timings (`isHdmiModeValid()` in `ps5/hdmi.c`, kernel 7.0.5):

| VIC | Resolution | Refresh | Pclk    |
|-----|------------|---------|---------|
| 16  | 1920×1080  | 60 Hz   | 148500  |
| 63  | 1920×1080  | 120 Hz  | 297000  |
| 97  | 3840×2160  | 60 Hz   | 594000  |
| 118 | 3840×2160  | 120 Hz  | 1188000 |
| —   | 2560×1440  | 60 Hz   | 241500/241700 |
| —   | 2560×1440  | 120 Hz  | 497750/592250 |

Any other timing → kernel rejects → black screen. The normalizer rewrites the EDID's DTD slot 0 to land on one of these, or passes through unchanged when it already does.

Only **1080p@120 (VIC 63)** is DTD-injectable; 4K@120 / 4K@90 clocks overflow the EDID DTD's 16-bit pclk field.
