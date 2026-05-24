# KNOWN WORKING — read this first when the screen breaks

This is the **recovery playbook**. If a bake just bricked your display, jump
to [Black-screen recovery](#black-screen-recovery) below.

Technical "why" lives in [PROJECT_STATUS.md](PROJECT_STATUS.md). This doc
is the cheat sheet.

---

## The one-line summary

**PS5 Linux's amdgpu kernel has a hardcoded whitelist of exactly four
allowed display modes.** Any EDID whose "preferred mode" is outside this
whitelist returns `MODE_ERROR` and the connector refuses to come up.
Source: `isHdmiModeValid()` in
`drivers/gpu/drm/amd/display/amdgpu_dm/amdgpu_dm.c` of ps5-linux-patches.

| VIC | Resolution | Pixel clock | Notes |
| --- | --- | --- | --- |
| 16  | 1920×1080 @ 60Hz  | 148.500 MHz | The safest. Always works. |
| 63  | 1920×1080 @ 120Hz | 297.000 MHz | Added in kernel 7.0.3+. |
| —   | 2560×1440 @ 60Hz  | **exactly 241500 or 241700 kHz** | Pclk-matched, not VIC. |
| 97  | 3840×2160 @ 60Hz  | 594.000 MHz | FLAVA3 auto-subsamples to 4:2:0. |

**Anything outside this list = black screen.** No HDR. No VRR. No 1440p120.
No 4K120. Not yet. Those need driver code that doesn't exist in upstream
ps5-linux-patches yet (as of kernel v7.0.8, the current target).

---

## The known-good USB state (verified on ASUS VG27WQ + PS5 4.03)

This is the byte-for-byte working state. Snapshot it now while it works.

```
/boot/efi/bzImage              = 10,552,320 B  (kernel 7.0.2)
/boot/efi/bzImage.702          = 10,552,320 B  (identical backup)
/boot/efi/initrd.img           = 23,366,246 B  (stock initrd + 2048B EDID cpio)
/boot/efi/initrd.img.702       = 23,366,246 B  (identical backup)
/boot/efi/initrd.img.tv        = 23,364,198 B  (stock initrd, no EDID — the baseline)
/boot/efi/cmdline.txt          = drm.edid_firmware=DP-1:edid/vg27wq-stripped.bin video=DP-1:e
/boot/efi/cmdline.txt.mon      = (identical — last-known-working baseline)
/boot/efi/cmdline.txt.tv       = root=LABEL=ubuntu2604-m2 rw rootwait console=ttyTitania0 console=tty0 mitigations=off idle=halt preempt=full
/boot/efi/edid/vg27wq-stripped.bin   = 128 B, checksum 0, 0 extensions, AUS 2b27 identity
/boot/efi/safe-boot.sh         = SSH recovery script
/boot/efi/kexec.sh             = stock loader's kexec wrapper
```

**On the M.2 rootfs (also part of the working state — DO NOT LOSE):**
```
/lib/firmware/edid/vg27wq-stripped.bin  = same 128 B file
```

The M.2 copy is **essential**. The kernel firmware loader runs AFTER
`switch_root`, so it looks for the EDID file on the M.2 rootfs, not in
initramfs. If the M.2 copy is missing, the bake fails with `err=-2`
(ENOENT) in dmesg and amdgpu falls back to DDC.

---

## Hard rules (do not break these)

1. **EDID extension count MUST be 0.** No HDR data block, no VRR, no
   audio data block, no extra DTDs. The kernel's amdgpu mode selector
   will pick a mode from any extension block, and that mode will fail
   `isHdmiModeValid()`.

2. **DTD pixel clock MUST be exactly one of:** `148500`, `241500`,
   `241700`, `297000`, `594000` kHz. The wizard's `build_synthetic_edid()`
   uses CEA-861-D canonical timings, which produces these values. **Never
   use CVT-RB v2** (those give 241250 kHz for 1440p60 → fails `==` check).

3. **Never change resolution from GNOME's display settings.** GNOME saves
   `~/.config/monitors.xml` on the M.2 rootfs and re-applies it after
   login. If the saved mode is outside the whitelist, the screen goes
   black on every subsequent boot. To change resolution: re-bake the
   EDID and reboot.

4. **Do not bake an EDID for a monitor you can't currently see.** The
   baked EDID's pclk has to match a sink the converter can actually
   drive. Bake the 4K TV's EDID and then plug into a 1080p screen →
   converter fails → black screen.

5. **Never delete `.tv` files.** They are the only stock backup.

6. **Never delete `.702` files.** They are the last-known-working state.

7. **Don't update PS5 firmware past 6.02.** ps5-linux drops support at
   ≥6.50.

---

## Black-screen recovery

### Path A — just reboot

If you didn't change anything in display settings, try a clean reboot.
The kernel re-reads EDID on every boot. Many black-screens fix themselves.

### Path B — SSH in and run safe-boot.sh

If reboot doesn't fix it (GNOME saved a broken `monitors.xml`):

```bash
ssh danny@<your-ps5-ip>
sudo /boot/efi/safe-boot.sh
```

`safe-boot.sh` does three things:
1. Wipes `~/.config/monitors.xml`, `~/.config/dconf/user`, `~/.config/xrandr/`,
   `~/.config/kscreen/*`, `~/.config/wlr/` on the M.2 rootfs.
2. Restores `/boot/efi/cmdline.txt` and `/boot/efi/initrd.img` from `.mon`
   (preferred) or `.tv` (fallback).
3. `kexec`s into the freshly-restored state.

Working screen in ~30 seconds.

### Path C — restore the USB from a Mac/PC

Plug the USB into a Mac:

```bash
# Mount EFI partition (macOS hides it)
osascript -e 'do shell script "diskutil mount disk4s2" with administrator privileges'

cd /Volumes/boot
cp cmdline.txt.mon cmdline.txt    # working cmdline
cp initrd.img.702 initrd.img      # working initrd
cp bzImage.702 bzImage            # working kernel
```

Eject, plug into PS5, boot.

If `.mon` doesn't exist, use `.tv` instead (stock baseline, no EDID baked).
You'll get a screen via DDC and can re-bake from there.

### Path D — nothing else worked

The M.2 rootfs is in a state USB alone can't fix:

1. **Hold Shift at boot** → GRUB → recovery mode → root shell →
   `rm -f /home/danny/.config/monitors.xml` → reboot.
2. **Boot a Ubuntu live USB**, mount M.2, delete `monitors.xml`.
3. **Reinstall ps5-linux** from the GitHub instructions (~10 min).

---

## Why force-1080p works on 1080p but breaks 1440p+

The asymmetry you noticed has a clean explanation:

| Setup | What amdgpu picks | Pclk | `isHdmiModeValid()` | Result |
| --- | --- | --- | --- | --- |
| 1080p monitor + force-1080p EDID | VIC 16 | 148500 | true (case 16) | ✅ |
| 1440p monitor + force-1080p EDID | Monitor's real 1440p DTD via DDC | usually 241250 (CVT-RB) | **false** (≠241500) | ❌ |
| 1440p monitor + force-1440p EDID | Our synthetic 1440p DTD | 241500 | true (pclk match) | ✅ |
| 1080p monitor + force-1440p EDID | VIC 16 fallback | 148500 | true (case 16) | ✅ |

Baking a **1440p-CEA-pclk EDID** is more robust than baking 1080p because
both whitelist conditions can pass.

---

## What the wizard guarantees vs. what's on you

**Wizard guarantees (post-fix in this version):**
- All synthetic EDIDs are 128 bytes, 0 extensions.
- DTD pclks pinned to {148500, 241500, 297000, 594000} kHz.
- **Idempotent bake**: re-baking REPLACES, never accumulates. Old
  EDID files in `<USB>/edid/` are deleted; only the active one stays.
- `.tv` and `.702` backups created on first bake, never overwritten.
- `safe-boot.sh` regenerated on every bake.
- Universal Safe Mode uses official ps5-linux `amdgpu.force_1080p=1`,
  no `drm.edid_firmware=` or `video=DP-1:`.

**You still need to:**
- Plug the correct monitor in before booting.
- Avoid GNOME's display settings panel.
- Never delete `.tv` or `.702` files.

---

## Quick reference: which bake to use

| Situation | Use |
| --- | --- |
| New monitor I haven't seen before | 🛡 **Universal** (`amdgpu.force_1080p=1`) |
| ASUS VG27WQ or any 1440p IPS at native | 🎯 **Custom 2560×1440 @ 60Hz** |
| 1080p monitor at native | 🎯 **Custom 1920×1080 @ 60Hz** or Universal |
| 4K TV (already physically tested at 60Hz) | 🎯 **Custom 3840×2160 @ 60Hz** |
| 1440p / 4K at 120Hz | **NOT SUPPORTED** — kernel doesn't allow. Use 60Hz. |
| HDR | **NOT SUPPORTED** — kernel HDR pipeline not implemented. |

---

## Auto-EDID on every boot (optional, recommended)

The wizard now ships `ps5-autoedid-install.sh` on the USB's FAT32 root.
After your screen is working, SSH into the PS5 and run it ONCE:

```bash
ssh danny@<ps5-ip>
sudo bash /boot/efi/ps5-autoedid-install.sh
```

This installs **two** auto-EDID layers on the M.2 rootfs:

1. **initramfs-tools `init-top` hook** — runs in initramfs BEFORE the
   M.2 mounts and BEFORE amdgpu's deferred firmware probe. Reads
   `/sys/class/drm/card0-DP-1/edid` from the kernel-resident ICC-pushed
   EDID, normalizes the DTD pclk to the kernel whitelist (148500 /
   241500 / 297000 / 594000 kHz), writes ONE file at
   `/run/firmware/edid/auto.bin`. The cmdline adds
   `firmware_class.path=/run/firmware` so amdgpu finds it.
2. **systemd unit `ps5-autoedid.service`** — runs on every boot
   (`multi-user.target`) as a safety net. Same logic but writes to
   `/lib/firmware/edid/auto.bin` and triggers a DRM reprobe. Catches
   the case where the initramfs hook didn't fire (kernel rebuilt without
   regenerating initrd, etc.).

**Key properties of the installer:**
- The cmdline.txt is rewritten ONCE to point at `edid/auto.bin`. The
  filename never changes — only the file *contents* change each boot.
  No "10 different cmdline references" risk.
- The EDID file is REPLACED every boot, never accumulated. Only one
  active EDID at any time.
- If the live EDID is unreadable or invalid, the installer leaves the
  previous `auto.bin` in place and logs the failure. The screen stays
  in whatever state it was in.
- All logs go to `/var/log/ps5-autoedid.log`.

**Recovery from a failed auto-EDID setup:**
- Same as any black screen — `safe-boot.sh` restores from `.tv`/`.mon`
  backups and bypasses the auto-edid path entirely.
- To uninstall: `sudo systemctl disable ps5-autoedid.service &&
  sudo rm /etc/initramfs-tools/scripts/init-top/ps5-autoedid &&
  sudo update-initramfs -u`.

---

## End

When this doc and the wizard disagree, **this doc wins.** Technical
details in [PROJECT_STATUS.md](PROJECT_STATUS.md). Auto-EDID design and
rationale in [AUTO_EDID_DESIGN.md](AUTO_EDID_DESIGN.md).
