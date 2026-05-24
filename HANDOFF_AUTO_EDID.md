# Auto-EDID Boot Handler — ✅ VERIFIED WORKING (2026-05-24)

The auto-EDID system is fully installed and confirmed working on PS5 Linux
kernel 7.0.5 with ASUS VG27WQ (2560×1440@60Hz). Screen came up on first
reboot after install.

---

## What it does (vs. the Mac wizard)

Both produce the same result — a clean 128-byte stripped EDID, pclk pinned
to the kernel whitelist — but:

| | Mac wizard | Auto-EDID boot handler |
|---|---|---|
| When it runs | Manually, on Mac | Every boot, automatically |
| EDID source | macOS IOKit (frozen snapshot) | Live sysfs at boot time |
| Works with any monitor | Only the one you baked for | Yes — reads whatever is connected |
| Needs Mac to change monitors | Yes | No |

The Mac wizard is now a manual fallback only. The boot handler has replaced it
for normal use.

---

## Verified final state on PS5 (2026-05-24)

```
/boot/efi/cmdline.txt
  root=LABEL=ubuntu2604-m2 rw rootwait console=ttyTitania0 console=tty0
  mitigations=off idle=halt preempt=full
  drm.edid_firmware=DP-1:edid/auto.bin
  video=DP-1:e
  firmware_class.path=/run/firmware
  snd_hda_intel.enable_dp_mst=0

/boot/efi/cmdline.txt.mon   = same as above (safe-boot restores to auto-EDID)
/boot/efi/cmdline.txt.tv    = stock baseline (no EDID — last resort)

/boot/efi/edid/             = auto.bin ONLY (128 B, AUS VG27WQ, pclk=241500kHz)
/lib/firmware/edid/         = auto.bin ONLY (identical)

/boot/efi/initrd.img        = 33 MB — kernel 7.0.5 + initramfs hook baked in
/boot/efi/initrd.img.702    = 23 MB — 7.0.2 backup (DO NOT DELETE)
/boot/efi/initrd.img.tv     = 23 MB — stock backup (DO NOT DELETE)
```

No stale monitor-specific EDID files anywhere. One file, one cmdline reference.

---

## What runs on every boot

**Layer 1 — initramfs init-top hook** (before M.2 mounts, before amdgpu probes):
- `/etc/initramfs-tools/scripts/init-top/ps5-autoedid`
- Reads `/sys/class/drm/card0-DP-1/edid` from sysfs
- Normalizes DTD pclk to nearest kernel whitelist value
- Writes `/run/firmware/edid/auto.bin`
- `firmware_class.path=/run/firmware` tells amdgpu to find it there

**Layer 2 — systemd unit** (safety net, fires before display-manager):
- `/etc/systemd/system/ps5-autoedid.service` → `/usr/local/sbin/ps5-autoedid`
- Same logic, writes `/lib/firmware/edid/auto.bin`
- Triggers DRM reprobe so amdgpu picks it up if Layer 1 was too early

**Kernel whitelist** (both layers enforce this):
| pclk (kHz) | Mode |
|---|---|
| 148500 | 1920×1080 @ 60Hz |
| 241500 | 2560×1440 @ 60Hz |
| 241700 | 2560×1440 @ 60Hz (alt) |
| 297000 | 1920×1080 @ 120Hz |
| 594000 | 3840×2160 @ 60Hz |

Any other pclk gets rounded to the nearest whitelist value automatically.

---

## If the screen goes black

```bash
ssh danny@192.168.50.240
sudo /boot/efi/safe-boot.sh
```

`safe-boot.sh` wipes `~/.config/monitors.xml` and restores from `.mon` backup
(which now points at `auto.bin` — not the old static EDID). Screen back in ~30s.

See `KNOWN_WORKING.md` for the full recovery playbook.

---

## If you need to re-run the installer (e.g. after kernel update)

```bash
ssh danny@192.168.50.240
# USB must be mounted first:
sudo mkdir -p /boot/efi && sudo mount /dev/sdb2 /boot/efi
sudo bash /boot/efi/ps5-autoedid-install.sh
```

The installer is idempotent — safe to run multiple times.

---

## Verify it's working post-boot

```bash
ssh danny@192.168.50.240
cat /var/log/ps5-autoedid.log
# Should show: live EDID captured, pclk normalized, auto.bin written
```
