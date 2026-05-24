# Auto-EDID on every boot — design

The wizard's current model is "user runs the wizard once, USB has the
EDID baked in, every subsequent boot uses that exact EDID." This breaks
the moment the user plugs in a different monitor.

**Goal**: every boot, the connected monitor's EDID gets read, normalized
to the kernel's whitelist, and baked into the boot chain — without manual
intervention, without modifying ps5-linux-loader.

## Why we can't modify the loader

The user is on an upstream-distributed ps5-linux-loader (community-built,
not under our control). Patching the loader source means forking it,
shipping a custom build, getting users to switch loaders. Out of scope.

## Why the existing wizard fails this goal

The wizard runs on the user's Mac/Linux PC, requires manual intervention,
and produces a snapshot of one EDID at one point in time. Plug in a
different monitor → black screen → user has to go back to the Mac and
re-bake.

## What the kernel actually does at boot (from ps5-linux-patches/linux.patch)

This is the verified boot sequence on ps5-linux. The window we own is
explicit:

1. **ps5-linux-loader (Sony-OS context)** loads `bzImage`, `initrd.img`,
   `cmdline.txt` from the FAT32 partition. **No video-out interaction.**
2. **kexec** into the kernel.
3. **Kernel decompresses + boots**, extracts concatenated initramfs cpio
   archives into rootfs (RAM-backed).
4. **Kernel module init runs**: amdgpu probes. It calls
   `drm_edid_override_get` which consults:
   a. A file at `lib/firmware/edid/<name>` (where `<name>` comes from
      `drm.edid_firmware=DP-1:<name>` in cmdline), OR
   b. The `real_edid` global, populated asynchronously by
      `hdmi_notification_handler` when the ICC HDMI service pushes the
      raw EDID up from the southbridge MCU.
5. **`/init` (initramfs init script) runs as PID 1.** ← OUR WINDOW
6. `/init` mounts M.2 ext4 (root=LABEL=ubuntu2604-m2).
7. `switch_root` to M.2 rootfs.
8. systemd / GNOME come up. By this point amdgpu has already decided on
   a mode and the connector is either alive or black.

**Key fact**: step 4 (amdgpu probe) blocks on the EDID source for a
short timeout, then proceeds with whatever it has. If our `/init`
intervention is fast enough, we can place a firmware file before amdgpu's
timeout fires, OR force a re-probe after we have a good EDID.

## The design — initramfs `/init` hook

Embed a small shell+python helper INTO the initramfs (the same initramfs
the wizard already appends a cpio fragment to). On every boot:

```
[Phase A — pre-amdgpu-probe (best-effort)]
  1. Mount sysfs, procfs early
  2. Read /sys/class/drm/card0-DP-1/edid (kernel may already have it
     from the ICC HDMI service notification — happens parallel to module
     load, often before amdgpu picks a mode)
  3. If a valid EDID was read:
     a. Strip to 128 B base block, normalize DTD pclk to whitelist
     b. Write to /run/firmware/edid/auto.bin
     c. Echo "auto.bin" to a sysfs trigger that makes drm reload the
        firmware (echo "detect" > /sys/class/drm/card0-DP-1/force)
     d. Done — amdgpu will pick this EDID on its next mode-set attempt

[Phase B — post-switch-root cleanup]
  4. /init continues normal boot
  5. systemd unit on M.2 verifies the boot used Phase A's EDID, archives
     it for diagnostics, and triggers another reprobe if needed
```

### Critical design rules

1. **No file accumulation.** Every boot writes EXACTLY ONE EDID file at
   `/run/firmware/edid/auto.bin`. `/run` is tmpfs — vanishes on reboot.
   On the M.2 rootfs side, the systemd unit also keeps only ONE file
   at `/lib/firmware/edid/auto.bin` (overwrites every boot).
2. **No cmdline mutation.** The cmdline stays static:
   `drm.edid_firmware=DP-1:edid/auto.bin firmware_class.path=/run/firmware`.
   The contents of `auto.bin` change every boot, but the cmdline pointer
   never does. Eliminates the "10 different cmdline entries" risk.
3. **Validate before placing.** Run the same
   `_validate_edid_against_whitelist()` check. If the live EDID's pclk
   isn't in the whitelist, synthesize a replacement with our canonical
   CEA timings, copying the monitor's identity bytes (mfr, product,
   serial, name descriptor) so userspace shows the right monitor name.
4. **Fail loud but safe.** If the EDID read fails or validation fails,
   the script DOES NOT place a broken EDID. It falls back to
   `amdgpu.force_1080p=1` and lets the universal-safe path take over.
5. **Idempotent.** Running the script twice in a row produces the same
   end state. No accumulation, no half-states.

## Why this path is achievable (vs. the loader-patch alternative)

| Concern | Loader patch | Initramfs init script |
| --- | --- | --- |
| Requires modifying upstream loader | YES (blocker) | NO |
| Works on all firmware versions | Needs per-fw offsets | NO — kernel sysfs is stable |
| Can read live EDID | YES via kread | YES via /sys/class/drm |
| Race window | Tight (pre-kexec) | Reasonable (~100ms pre-amdgpu) |
| Can fall back to safe mode on failure | Hard | Easy |
| User can install without admin on PS5 OS | NO | YES (just re-bake the USB once) |
| Distribution | Custom loader build | Standard wizard bake |

The initramfs approach is **strictly within the wizard's existing
capabilities**: we already append cpio fragments to initrd. We just
append a different cpio that contains `/init.d/00-ps5-display-wizard`
instead of (or in addition to) the EDID file.

## What ships in the initramfs

A single shell script `ps5-display-wizard-autoedid.sh` (~200 lines)
embedded via cpio at `lib/dracut/hooks/pre-mount/00-ps5-display-wizard.sh`
(or equivalent path for the user's initramfs generator — needs detection).

Companion: a tiny static Python interpreter is NOT viable in initramfs.
The script has to be pure POSIX shell + busybox tools. EDID parsing
in shell is awkward but doable: `dd` reads bytes, `printf` does hex
math, `od` formats. ~50 lines for parse, ~30 for synthesis, ~20 for
filesystem placement.

Alternative: ship a tiny static C binary compiled with musl that does
EDID parse/synthesize/validate in ~5KB. Cleaner. The wizard already
knows how to build the right cpio.

## Verifying it worked on next boot

A systemd unit on the M.2 rootfs writes a stamp file at
`/var/log/ps5-display-wizard-autoedid.log` with:
- Live EDID sha256 (raw)
- Active EDID sha256 (post-normalize)
- Picked mode (from `/sys/class/drm/card0-DP-1/modes`)
- Validator output

User can `cat` this file post-boot to see what happened. If a future
black-screen happens, this log is the first diagnostic.

## Open questions to resolve in implementation

1. **Does the kernel's firmware loader actually re-check `/run/firmware/`
   when `force "detect"` is echoed?** Untested. If not, we need to
   bind-mount our file over the expected `/lib/firmware/edid/` path
   instead. Either works.

2. **Is sysfs EDID available BEFORE amdgpu binds the connector?** From
   the kernel docs: yes — the ICC HDMI notification fires independently
   of amdgpu. But timing is unverified. May need to busy-wait up to
   ~500ms for the sysfs file to appear.

3. **Which initramfs generator does ubuntu2604 use?** ps5-ubuntu2604.img
   uses... need to inspect. If it's dracut, hook path is
   `/lib/dracut/hooks/`. If initramfs-tools, it's `/etc/initramfs-tools/`.
   The wizard's cpio append needs to target the right path.

4. **Multi-output handling.** If the user ever connects two displays at
   once, only DP-1 is the real output (HDMI-A-1 etc. are unused). Hard-
   code DP-1 in the script.

## Phasing

**Phase 1**: write the initramfs shell script. Test on the Mac wizard
side by injecting it into initrd.img and verifying the cpio extracts
correctly. (No PS5 needed.)

**Phase 2**: SSH onto the live PS5 with a working screen. Manually
copy the script into the initramfs at the right path. Reboot. Check
`/var/log/ps5-display-wizard-autoedid.log` post-boot.

**Phase 3**: integrate into the wizard's bake. Add a checkbox: "Bake
auto-EDID hook (recommended)". Default ON. When ON, the bake appends
both the safe-fallback EDID AND the init hook.

**Phase 4**: turn the static-EDID bake into a deprecated option. The
auto-EDID hook makes the static bake obsolete.
