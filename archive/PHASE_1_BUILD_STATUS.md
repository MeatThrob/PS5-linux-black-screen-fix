# PS5 Display Wizard — Phase 1 Build Status (Catch-Up Doc)

**Purpose**: bring any new Claude session (or human) fully up to speed on
what's been built so far on both the **Linux side (PS5 itself)** and the
**Mac mini side**. Read this AFTER [PHASE_0_RESEARCH.md](PHASE_0_RESEARCH.md)
if you want the original feasibility/architecture context.

Last updated: 2026-05-15 evening session.

---

## What exists in the wizard right now (both platforms)

| File | Size | What it is |
|---|---|---|
| `ps5_display_wizard.py` | 51 KB | Linux GTK 3 wizard (runs on the PS5 itself or any Linux PC) |
| `ps5_display_wizard_mac.py` | 60 KB | macOS Tkinter wizard (runs on the Mac mini M2 or any Mac) |
| `README.md` | 6 KB | Linux-side usage docs |
| `README_MAC.md` | 5 KB | macOS-side usage docs |

The two wizards share architecture but use platform-native APIs for the two
non-portable concerns: **EDID reading** and **USB/partition detection**.
Bake/revert/cpio/strip logic is byte-for-byte identical so EDIDs baked from
either platform are interchangeable.

---

## Features now implemented (NEW since PHASE_0_RESEARCH.md)

### 1. Custom bake with monitor identity preservation

Earlier we only had "strip the real EDID to 128 bytes". The Mac side now
has a **`build_synthetic_edid()`** path that builds an EDID from scratch
with a user-chosen single timing (e.g. 2560x1440@120Hz CVT-RB).

Critical fix on top: **`build_synthetic_edid(identity_source=<bytes>)`**
copies bytes 8-17 (manufacturer / product / serial / week / year) from the
real scanned monitor's EDID into the synthetic one. So Linux sees
`AUS VG27WQ at 1440p@120Hz`, not `WZD Wizard at 1440x60Hz`. The converter
only cares about the timing block; it doesn't read the identity bytes. But
Linux's display settings / `xrandr` use the identity bytes for the label.

**`on_bake_custom`** now REQUIRES a scanned monitor selection. Without one,
the dialog points the user at "Scan first" or "use Universal Safe instead."

### 2. Filesystem-agnostic USB detection

Both wizards previously filtered to FAT32/exFAT only. This rejected real
PS5 Linux setups where the boot files are on an **EFI System Partition**
(which is FAT but registered as ESP, and Linux installs often use this).
Now: any volume containing all three of `bzImage`, `initrd.img`,
`cmdline.txt` qualifies, regardless of FS.

The wizard still SHOWS the filesystem type in the UI label so the user
can see whether they're picking exFAT, FAT32, or whatever. We just don't
hard-filter.

### 3. Multi-USB picker dialog

When `find_ps5_usbs()` returns 2+ candidates (e.g. user has the PS5 Linux
USB plus a Linux-installer USB plugged in at the same time), the wizards
pop a modal picker showing Path / Filesystem / Score / Match reasons for
each. User picks which one to bake into. The auto-select still happens for
the single-candidate case.

### 4. NEW Mac-only button: 🔓 Detect Linux Partition

**The problem this solves**: a real PS5 Linux install puts the loader
files on the **EFI System Partition** which macOS doesn't auto-mount
(ESPs are hidden by design). So the boot partition's there on the disk —
visible to `diskutil list` — but invisible to `/Volumes/` and therefore to
the regular Detect PS5 USB button.

**What the new button does** (in `find_ps5_linux_partitions()` +
`on_detect_linux_partition`):

1. Runs `diskutil list -plist` for the machine-readable partition table.
2. Finds every partition regardless of mount status.
3. For FAT/exFAT partitions:
   a. If unmounted, tries unprivileged `diskutil mount`.
   b. If macOS refuses (ESP case), falls back to
      `osascript -e 'do shell script ... with administrator privileges'`
      which pops the macOS native admin password prompt.
   c. Once mounted, verifies the three loader files exist.
4. ext4 partitions (the Linux OS root) are surfaced as info-only in the
   Activity log — `"needs ext4fuse on macOS"` — so the user knows where
   their distro root is but the wizard doesn't try to bake into it.
5. If multiple FAT partitions qualify, the same multi-candidate picker
   dialog opens.

**Three USB-finding buttons in the Mac wizard now:**

| Button | Finds | When to use |
|---|---|---|
| 💾 Detect PS5 USB | Already-mounted volumes under `/Volumes/` with the three loader files at root | Plain USB sticks with loader files at root (standard PS5 Linux loader setup) |
| 🔓 Detect Linux Partition (NEW) | Real disk partitions via `diskutil` (mounted or not). Auto-mounts FAT/EFI partitions, prompting for admin password if needed | Fully-partitioned Linux installs where the boot partition is an EFI partition macOS won't auto-mount |
| 📂 Browse for USB... | Manual folder picker | Custom mount points, advanced workflows |

(Linux wizard doesn't need a Linux-partition button — Linux can read
ext4/FAT/etc natively, so the standard Detect PS5 USB already covers it.)

### 5. Reentrancy guards (both platforms, prior session)

Single shared "busy" flag across all three action buttons (Add Monitor /
Re-Apply from History / Revert). When any action runs, all three buttons
turn grey. The flag is set before any modal dialog opens, so double-clicks
during the confirm-dialog window are rejected (with a log line). Released
in a `finally:` block, so even on error the buttons re-enable.

### 6. "Already added" detection (both platforms, prior session)

Before any bake, `is_already_baked(usb_path, slug, edid_bytes, strip)`
checks if the would-be-baked EDID is already on the USB:
- Hash match on the `<USB>/edid/<slug>-stripped.bin` file
- Substring match on `drm.edid_firmware=DP-1:edid/<slug>-stripped.bin` in cmdline.txt

States detected:
- **Fully added** → log "already added" + dialog "Re-bake anyway?"
- **Partial** (file present but cmdline doesn't reference) → log warning, proceeds normally
- **Partial** (cmdline references but file missing/different) → same

### 7. Device-info display (brand/model/size)

USB detect/browse now shows: `SanDisk Ultra · 114.6 GB · label:boot · vfat · removable · @ /boot/efi`

Linux: reads `/sys/block/<dev>/device/{vendor,model}`, `/sys/block/<dev>/size`,
`/dev/disk/by-label/`.
Mac: parses `diskutil info` Device/Media Name + Total Size + Volume Name + Protocol.

---

## 2026-05-15 evening: KERNEL UPGRADED TO 7.0.5 (pending reboot)

**Status**: kernel 7.0.5 installed on rootfs, deployed to USB, ready for reboot.

### What's on the USB right now

```
/boot/efi/bzImage              = vmlinuz-7.0.5 (NEW)          11.4 MB
/boot/efi/bzImage.702          = vmlinuz-7.0.2 (backup)       10.0 MB
/boot/efi/initrd.img           = initrd-7.0.5 with all EDIDs  24.4 MB
/boot/efi/initrd.img.702       = initrd-7.0.2 (mid-fix state)
/boot/efi/initrd.img.tv        = pre-EDID-hook initrd (Phase 1 baseline)
/boot/efi/cmdline.txt          = uses vg27wq-stripped.bin (safe 1440p60)
/boot/efi/cmdline.txt.tv       = TV baseline (no EDID forcing)
/boot/efi/cmdline.txt.mon      = monitor variant
/boot/efi/safe-boot.sh         = SSH recovery script
/boot/efi/edid/                = all 3 EDID variants present
```

### What's in the new initrd (7.0.5)

`lsinitramfs /boot/initrd.img-7.0.5` confirms:
- `usr/lib/firmware/edid/vg27wq-stripped.bin` (the active one, per cmdline)
- `usr/lib/firmware/edid/vg27wq-full.bin`
- `usr/lib/firmware/edid/aus-vg27wq-2560x1440-120hz-full.bin`

All three are also on the rootfs at `/lib/firmware/edid/` so cmdline can
reference any of them and it'll resolve.

### What 7.0.5 changes vs 7.0.2

Key code change in `drivers/gpu/drm/amd/display/amdgpu_dm/amdgpu_dm.c`:
```c
bool isHdmiModeValid(const struct drm_display_mode *mode, int force_1080p) {
    u8 vic = drm_match_cea_mode(mode);
    if (force_1080p) return vic == 16;
    if (mode->hdisplay == 2560 && mode->vdisplay == 1440)
        return mode->clock == 241500 || mode->clock == 241700;
    switch (vic) {
    case 16:  /* 1080p60 */
    case 63:  /* 1080p120 */   ← NEW
    case 97:  /* 2160p60 */
        return true;
    }
    return false;
}
```

So on 7.0.5, **1080p120 should work** if the EDID declares VIC 63 in its
CTA extension, OR if added at runtime via xrandr. With the stripped EDID
in use, the user will need to xrandr-add 1080p120 explicitly:

```bash
DISPLAY=:0 xrandr --newmode "1080p120" 297.00 1920 2008 2052 2200 1080 1084 1089 1125 +hsync +vsync
DISPLAY=:0 xrandr --addmode DP-1 1080p120
DISPLAY=:0 xrandr --output DP-1 --mode 1080p120
```

This uses VIC 63's standard CTA-861 timing (297 MHz pixel clock).

### Why 1440p120 still won't work on 7.0.5

The new validator above does NOT include the 1440p120 pixel clock. The
1440p branch is:
```c
return mode->clock == 241500 || mode->clock == 241700;
```
Only 1440p60. 1440p120 (470.96 MHz CVT-RB) still returns MODE_ERROR.

Adding 1440p120 requires (a) modifying this function and (b) adding the
I2C mode-set sequence in `drivers/ps5/hdmi.c` for 1440p120. That's the
patch work scoped for the next few days.

### Reboot runbook

On the PS5:
```bash
sudo reboot
```

Expected after reboot:
- 7.0.5 boots
- Display comes up at 1440p60 (stripped EDID, known good)
- `uname -r` will show `7.0.5`
- Then user can xrandr-add 1080p120 to test

If display goes black or unstable:
- From a phone/Mac: `ssh danny@192.168.50.240`
- Run: `sudo /boot/efi/safe-boot.sh` (restores TV baseline cmdline + initrd, kexecs)
- Or: rollback to 7.0.2 kernel by:
  ```bash
  sudo cp /boot/efi/bzImage.702 /boot/efi/bzImage
  sudo cp /boot/efi/initrd.img.702 /boot/efi/initrd.img
  sudo /boot/efi/kexec.sh
  ```

### What was preserved (NO data loss)

- /home/danny: untouched
- /etc: untouched
- All installed apps (Lutris, AgentVision wizard, etc.): untouched
- 7.0.2 kernel still in /boot/ and on USB as .702 backup
- All EDIDs and configs preserved

---

## CRITICAL: PS5 Linux boot chain — fundamental constraint for the wizard

**This is the most important piece of information for anyone working on
this project.** The wizard's "bake EDID into USB from a Mac" model only
partially works, and understanding why requires understanding the boot.

### The actual boot chain (verified from ps5-linux-loader/source/loader.c)

1. **PS5 firmware boot** → ps5-linux-loader (the HV exploit + custom
   bootloader) takes over.
2. **Loader reads from the FAT32 USB stick** (`/dev/sda2` mounted at
   `/boot/efi` once Linux is up — but at this stage the loader reads it
   directly via the PS5 firmware's filesystem APIs). It loads three
   files into memory:
   - `bzImage` (the kernel)
   - `initrd.img` (the initial ramdisk)
   - `cmdline.txt` (boot parameters)
3. **Loader kexecs the kernel** with the in-memory initrd and cmdline.
   See `loader.c` lines 200-240 — bzImage and initrd are loaded into
   pages via `install_page_syscore`, no filesystem read after this point.
4. **Kernel boots**, kernel extracts initramfs cpio archives (handles
   concatenated ones — that's why the wizard's cpio-append technically
   works).
5. **Kernel reads cmdline**, finds `root=LABEL=ubuntu2604-m2`.
6. **Kernel mounts the M.2 ext4 rootfs** (`/dev/nvme0n1p1`) as `/`
   via standard `mount()` syscall on the partition matching the label.
7. **switch_root**: the initramfs is unmounted; the rootfs replaces `/`.
   **At this point the initramfs view is gone.**
8. **kernel modules load**, including `amdgpu`, which probes DP-1 and
   requests its EDID firmware **after switch_root** — meaning the
   firmware loader searches the M.2 ROOTFS's `/lib/firmware/edid/`,
   NOT the now-discarded initramfs.

### The fundamental bug in the wizard

The Python wizard's bake function does:
1. Appends a cpio fragment to `<USB>/initrd.img` containing the EDID at
   `lib/firmware/edid/X` and `usr/lib/firmware/edid/X`
2. Edits `<USB>/cmdline.txt` to add `drm.edid_firmware=DP-1:edid/X`

**Step 1 puts the file in the initramfs, but the firmware loader runs
AFTER switch_root.** So even though the kernel extracts our cpio
fragment correctly during initramfs unpacking, by the time amdgpu fires
the firmware request, the M.2 rootfs is what's mounted at `/`, and that
rootfs has its own `/lib/firmware/edid/` directory that doesn't contain
our new file.

### Empirical evidence of the bug

dmesg shows:
```
[    9.78] amdgpu 0000:20:00.0: Direct firmware load for edid/aus-vg27wq-2560x1440-120hz-full.bin failed with error -2
[    9.78] amdgpu 0000:20:00.0: [drm] *ERROR* [CONNECTOR:65:DP-1] Requesting EDID firmware "edid/aus-vg27wq-2560x1440-120hz-full.bin" failed (err=-2)
```

`-2 = ENOENT`. The kernel asks for the file, the firmware loader looks
in rootfs `/lib/firmware/edid/`, doesn't find it, returns ENOENT.

Verified file locations:
- **USB `/boot/efi/edid/aus-vg27wq-2560x1440-120hz-full.bin`** → present (128 B, correct content)
- **USB `/boot/efi/initrd.img`** → 2048 B larger than `.tv` backup, fragment extracts correctly to `lib/firmware/edid/aus-vg27wq-2560x1440-120hz-full.bin` AND `usr/lib/firmware/edid/...`
- **Rootfs `/lib/firmware/edid/`** → only `vg27wq-stripped.bin` (the manually-installed Phase 2 baseline) and `vg27wq-full.bin`. **NO `aus-vg27wq-2560x1440-120hz-full.bin`**
- **Rootfs `/boot/initrd.img-7.0.2`** (the rootfs's own initrd, used by update-initramfs) → only has the original Phase 2 vg27wq-{full,stripped} files

So the wizard's cpio-append works mechanically, but the file lives only
in the initramfs which is discarded before the firmware loader fires.

### How Phase 2 (the manual VG27WQ 1440p@60 fix) actually worked

Back in Phase 2 we did three things:
1. Copied EDID files to `/lib/firmware/edid/` on the rootfs (M.2 SSD)
2. Created an initramfs-tools hook at
   `/etc/initramfs-tools/hooks/edid-firmware` that copies those files
   into the initramfs at build time
3. Ran `update-initramfs -u -k 7.0.2` which rebuilt
   `/boot/initrd.img-7.0.2` (rootfs) AND we then manually copied that to
   `/boot/efi/initrd.img` (USB)

That setup works because **the files exist in BOTH places** —
initramfs AND rootfs. The firmware loader, running after switch_root,
finds them on the rootfs.

The wizard's bake only does the initramfs half. **That's why VG27WQ at
60Hz still works** (those rootfs files are still there from Phase 2),
**but VG27WQ at 120Hz fails** (the wizard added a new file to the
initramfs but never to the rootfs).

### CRITICAL ADDENDUM: even fixing the rootfs-copy gap, 120Hz still won't work

**The PS5 Linux kernel has its own pixel-clock ceiling that's independent of
EDID.** Empirically verified tonight via xrandr live-mode test:

```
xrandr --newmode 2560x1440_120 470.96 ...  # CVT-RB modeline for 1440p@120
xrandr --addmode DP-1 2560x1440_120         # accepted
xrandr --output DP-1 --mode 2560x1440_120   # silently downgrades

Resulting state:
DP-1 connected primary 2560x1440+0+0 ...
   2560x1440      59.91*+
   2560x1440_120  89.90       ← kernel renamed our 120 → 90 (~352 MHz pclk cap)
```

DP link state at the time of test:
```
$ sudo cat /sys/kernel/debug/dri/0000:20:00.0/DP-1/link_settings
Current:  4  0x1e  0    Verified:  4  0x14  16   Reported:  4  0x1e  16
```
That's 4 lanes at HBR3 (8.1 Gbps/lane = 32.4 Gbps raw, ~25 Gbps effective).
**The link can carry 1440p@120 (~9 Gbps) with ridiculous headroom.** The
bottleneck is the kernel's amdgpu/display pixel-clock cap, not the link.

### Why the kernel caps at ~340-352 MHz on PS5

From the `ps5-linux-patches/linux.patch` analysis (PHASE_0_RESEARCH.md):

```c
// drivers/gpu/drm/amd/display/dc/dcn201/dcn201_link_encoder.c
+#ifndef CONFIG_X86_PS5
+    /* Ignore this to prevent blackscreen. */
+    .fec_set_enable = enc2_fec_set_enable,
+    .fec_set_ready = enc2_fec_set_ready,
+#endif

// drivers/gpu/drm/amd/display/dc/dio/dcn10/dcn10_link_encoder.c
+#ifdef CONFIG_X86_PS5
+    /* Ignore this to prevent link training failure. */
+    return;
+#endif
```

**On PS5: FEC is disabled AND the DP link training is short-circuited.**
The kernel reports the link as HBR3 but never actually negotiates it.
Without proper link training and without FEC, the driver's internal mode
validator clamps the pixel clock to something it knows is "safe" for the
broken link path. Empirically that ceiling is around 340-352 MHz —
matching the "Bump the HDMI clock to 340MHz" upstream patch from Jan 2026.

The upstream ps5-linux-patches README **explicitly lists 120Hz as a TODO**:
> "hdmi converter improvments: hdr, rgb range, 120hz"

So 1440p@120 on PS5 Linux **is currently not achievable in user-space**.
It needs kernel patches:
1. Implement proper DP link training for the MN864739 converter
2. Re-enable FEC where the converter actually supports it
3. Raise the HDMI pixel clock cap above 340 MHz

The stock PS5 firmware drives 1440p@120 because Sony's HDMI 2.1 driver
stack does all of the above. The Linux port doesn't yet.

### What this means for the wizard

The wizard's "bake a 1440p@120 EDID into the USB" feature **does its job
correctly** — produces a valid 128-byte EDID with the right primary DTD,
appends to initrd, edits cmdline. But the kernel's pixel-clock cap means
that even if the EDID is loaded perfectly, the actual displayed mode will
be downgraded to ~90 Hz. The user sees "120Hz baked" but display shows
"60Hz" because the driver clamps.

**Honest user-facing message the wizard should display when the user
selects 120Hz at 1440p (or any rate above 60 on PS5):**

```
⚠ This refresh rate is NOT YET SUPPORTED on PS5 Linux.

The PS5 Linux kernel caps the HDMI pixel clock at ~340 MHz due to
incomplete DP link training and disabled FEC on the MN864739 converter.
Even with the right EDID, the displayed refresh rate will be capped
around 90Hz at 1440p.

This is an upstream kernel issue, not an EDID issue. Track it at:
https://github.com/ps5-linux/ps5-linux-patches (TODO list)

For now, the best 1440p refresh rate on PS5 Linux is 60Hz.
```

### What WILL work today

- **1440p @ 60Hz**: proven, reliable, the current baseline. EDID-baking
  fixes the converter's EDID handshake issue, and 60Hz pixel clock
  (~241 MHz CVT-RB) is well under the kernel cap.
- **1080p @ 60Hz**: works on every monitor as the safe fallback.
- **Any resolution under ~340 MHz pixel clock**: works.

### What WON'T work today (kernel-level blocker)

- **1440p @ anything above ~85Hz**
- **4K @ 60Hz** (~533 MHz CVT-RB, above the cap)
- **4K @ 120Hz** (~1118 MHz, way above the cap, also above HDMI 2.0)
- Anything requiring HDMI 2.0+ pixel rates

### Action item for the wizard project

The `RESOLUTION_REFRESH_OPTIONS` dict in `ps5_display_wizard_mac.py`
currently lists:
```python
RESOLUTION_REFRESH_OPTIONS = {
    ( 1920, 1080): [60, 120],
    ( 2560, 1440): [60, 120],
    ...
}
```

This is **aspirational** — it matches what PS5 stock firmware supports,
not what the Linux kernel currently delivers. The wizard should either:

a) Add a "kernel-capable" filter that grays out options above the
   current ceiling, or
b) Show a warning ⚠ next to options that exceed the cap, like
   "120Hz (kernel limit — will display ~90Hz)", or
c) Leave the options visible (since they DO work on stock PS5
   firmware and will work on Linux once kernel patches land) but make
   the "this is aspirational" message clear in the bake confirmation.

**Recommendation: option (b)**. Users should be able to bake the
forward-looking EDID so when upstream patches land they don't need to
re-bake. But they should know the current behavior.

---

### Solution paths (ranked)

1. **Auto-sync systemd unit on the rootfs** (RECOMMENDED for v2):
   Install once (one-time setup, requires root on PS5). On every boot,
   the unit copies any `*.bin` from `/boot/efi/edid/` to
   `/lib/firmware/edid/` and triggers a DRM connector re-probe. After
   this is installed, the Mac-side wizard's USB bake "just works"
   without any rootfs access from the Mac.

2. **First-boot installer that the wizard puts on USB**: wizard writes
   `<USB>/install-rootfs-sync.sh` next to the EDID files. On the PS5,
   user runs it once via SSH: `sudo bash /boot/efi/install-rootfs-sync.sh`.
   It sets up option 1's systemd unit. After that, all future wizard
   bakes auto-apply.

3. **Run wizard on the PS5 itself**: simplest but defeats the
   cross-platform model. Wizard on the PS5 has root access and can
   write directly to `/lib/firmware/edid/`. Already works via this
   path — the Linux Python wizard could be extended to detect "I'm
   running on a PS5" and do the rootfs copy automatically.

4. **Have the wizard SSH into the PS5 from the Mac**: messy, requires
   network setup, credentials. Probably not the right pattern.

### Immediate fix for the user's current 120Hz attempt

The wizard's USB-side bake is correct. To make it actually take effect:

```bash
# On the PS5 over SSH (or in a local terminal):
sudo cp /boot/efi/edid/aus-vg27wq-2560x1440-120hz-full.bin /lib/firmware/edid/
sudo update-initramfs -u -k 7.0.2     # rebuilds /boot/initrd.img-7.0.2 with the new file
sudo cp /boot/initrd.img-7.0.2 /boot/efi/initrd.img
sync
reboot
```

After that, on next boot:
- `/lib/firmware/edid/` has the new file (rootfs)
- `/boot/efi/initrd.img` has the new file (initramfs)
- cmdline already references it
- amdgpu's firmware request will succeed
- If the converter can drive 120Hz at all, you'll see it as an available mode

### What this means for the wizard's docs

The README files need a section that says: **"This wizard bakes the
EDID into the USB initramfs. For it to take effect on a PS5 Linux
install where the kernel mounts the M.2 rootfs, you must ALSO ensure
the file is on the rootfs at `/lib/firmware/edid/`."** Either by:
- Manual copy via SSH after each bake (annoying)
- Running the wizard on the PS5 itself (defeats the Mac use case)
- Installing the auto-sync systemd unit (one-time setup, future bakes
  auto-apply)

The cleanest UX: ship a one-time installer script that the user runs on
the PS5 once. Document it as "PS5 Linux first-time setup". After that
all wizard bakes via USB take effect on next boot.

---

## Known issue discovered tonight (NOT YET FIXED)

User reported: "I baked this monitor's EDID into this Linux distro at
1440p 120Hz but I'm only seeing 59Hz in display settings."

**Diagnostic findings from `/boot/efi` on the live PS5**:

```
/boot/efi/cmdline.txt → drm.edid_firmware=DP-1:edid/aus-vg27wq-2560x1440-120hz-full.bin
/boot/efi/edid/aus-vg27wq-2560x1440-120hz-full.bin → 128 bytes
/sys/class/drm/card0-DP-1/edid → 256 bytes (sha 24910f14a230f6c7)
xrandr modes for 2560x1440 → 59.91 Hz only
```

**What this means**:

1. The on-disk firmware file is 128 bytes — that's a base-only EDID (no
   CTA extension blocks where 120Hz timings live). The "120hz" in the
   filename is just a label, not actual timing data inside the file.
   Primary DTD in the base block is 2560x1440@60 (the monitor's native).

2. The kernel reports a 256-byte EDID in sysfs. That's NOT our 128-byte
   firmware file. The kernel fell back to reading the real EDID via DDC
   (which happened to work this boot). So `drm.edid_firmware=` is NOT
   actually loading the file.

3. The most likely cause: **the EDID file isn't actually inside
   initrd.img**. Even with the cmdline line and the firmware file on USB,
   the kernel firmware loader reads from initramfs at early boot, not
   from the FAT32 partition directly. We append a cpio fragment to
   initrd.img during bake — but if the file size diff is only 2048 B and
   no extraction shows the file, the bake-time cpio append may have a
   bug.

4. Even if the firmware loaded correctly, the converter ceiling matters
   for actual 120Hz output. Upstream ps5-linux-patches TODO still lists
   "120hz" as un-implemented. The MN864739 may not drive 1440p@120 (~379
   MHz CVT-RB) reliably.

**Where to start the next session on this**:

```bash
# Verify cpio fragment was actually appended to initrd.img
diff <(stat -c%s /boot/efi/initrd.img) <(stat -c%s /boot/efi/initrd.img.tv)
# Extract just the appended portion and verify the EDID file is there
tail -c +$(( $(stat -c%s /boot/efi/initrd.img.tv) + 1 )) /boot/efi/initrd.img > /tmp/frag.cpio
cpio -idmu --quiet < /tmp/frag.cpio 2>&1
ls -la lib/firmware/edid/ usr/lib/firmware/edid/ 2>&1
# Check kernel dmesg for firmware-loader errors
sudo dmesg | grep -iE "firmware|edid|aus-vg27wq" | head -30
```

If the cpio fragment IS there and the kernel is still using DDC, the issue
is the kernel firmware loader not finding the path or rejecting the file.
If the cpio fragment is NOT there, the bake's cpio-append code is broken
in some way we haven't caught yet.

---

## Open architectural questions for the next session

1. **`build_synthetic_edid` with custom timings — does the wizard's
   "Custom Bake" UI actually let the user specify the refresh rate?**
   Per the .rtf transcript, identity preservation was added but I haven't
   verified the UI exposes a refresh-rate selector. If user picked
   "1440p 120Hz" in custom mode, the synthetic EDID's primary DTD should
   declare a CVT-RB 2560x1440@120Hz timing (pclk ~379 MHz). Verify in
   the current Mac wizard source.

2. **Why is initrd.img only 2048 B bigger than initrd.img.tv?**
   The wizard's bake should append a cpio fragment containing both
   `lib/firmware/edid/X` AND `usr/lib/firmware/edid/X` plus directory
   entries. With both paths + a 128-byte EDID, the fragment should be
   somewhere around 512-1024 bytes. 2048 B IS plausible (with padding).
   So the file IS there in initrd. So why isn't the kernel loading it?

3. **PS5-payload-side homebrew ELF** (the PHASE_0 plan): NOT STARTED.
   That work is still scoped but blocked on user being on the Mac with
   the PS5 SDK installed. Phase 0 doc has the full plan.

---

## File-by-file change log (since PHASE_0_RESEARCH.md)

### `ps5_display_wizard_mac.py` (60 KB current, was ~25 KB)
- `build_synthetic_edid()` extended with `identity_source` arg
- `on_bake_custom()` requires a scanned monitor selection
- `find_ps5_usbs()` — FS filter removed
- `find_ps5_linux_partitions()` — NEW; scans `diskutil list -plist`
- `on_detect_linux_partition` — NEW handler + UI button
- osascript admin-mount path for ESP partitions
- `usb_picker_dialog()` — multi-candidate picker
- Identity-preservation: bytes 8-17 from real EDID when caller passes
  `identity_source`
- Week/year preservation: only set defaults when NOT using identity source

### `ps5_display_wizard.py` (51 KB current, was ~25 KB)
- `find_ps5_usbs()` — FS filter relaxed (excludes only obvious
  non-storage mounts like tmpfs/proc/sysfs)
- Multi-candidate GTK picker dialog matching the Mac version
- `build_synthetic_edid()` mirror of Mac changes
- `on_bake_custom_clicked` requires scanned-monitor selection

### `README.md` — STALE — doesn't mention any of the above. Needs update.
### `README_MAC.md` — STALE — doesn't mention the Detect Linux Partition button. Needs update.

---

## What the next Claude session (Mac side) should know

1. **Three buttons live in the Mac wizard now**: Detect PS5 USB,
   Detect Linux Partition, Browse. Don't suggest adding a fourth without
   reading why each exists (above).

2. **`build_synthetic_edid` is the path for custom refresh rates**.
   Strip-only mode (the original behavior) keeps the monitor's primary
   DTD which is usually 60Hz native. To get 120Hz/144Hz, the user must
   use Custom Bake mode which writes a new DTD with the chosen timing.

3. **The PS5's MN864739 converter may not support 120Hz at all** even
   with a properly-crafted EDID. Upstream ps5-linux-patches TODO says
   "120hz" is un-implemented. Manage user expectations: 1440p@60 is
   reliable; 1440p@120 is best-effort.

4. **Test before bake-and-pray**: the wizard's pre-bake `is_already_baked`
   check tells the user if their bake is a duplicate. If they're trying
   the same monitor at different refresh rates, the slug needs to differ
   (e.g. `vg27wq-60` vs `vg27wq-120`) so they don't overwrite each other.

5. **The "59Hz only after 120Hz bake" issue** in this session likely
   means either:
   a. The cpio fragment didn't land in initrd at the kernel-readable
      path, OR
   b. The kernel loaded our 128-byte file but treated it as base-only
      EDID with native 60Hz DTD (because that's what 128 bytes means),
      OR
   c. The converter's hardware ceiling caps refresh at ~60Hz at 1440p.
   Resolution path is in "Where to start the next session" above.

---

## What the next Claude session (Linux/PS5 side) should know

1. Wizard is up at PID-of-the-day (check `pgrep ps5_display_wizard.py`).
   If not, launch with:
   ```bash
   DISPLAY=:0 WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 \
     GDK_BACKEND=wayland,x11 nohup python3 \
     /home/danny/Projects/ps5-display-wizard/ps5_display_wizard.py \
     >/tmp/wiz.log 2>&1 &
   ```

2. **The wizard on the PS5 itself reads the FORCED EDID** from sysfs,
   not the real monitor EDID, because `drm.edid_firmware=` is in
   cmdline. To scan a NEW monitor on the PS5 you must revert first:
   `sudo /boot/efi/safe-boot.sh`.

3. **Live VG27WQ EDID hashes for sanity checking**:
   - Stripped (working baseline): `07c36164fccfc019` (128 B)
   - Full (real): `cbe71fff84c2f075f76f554c65cf4d39` (256 B)
   - Current sysfs (real-via-DDC): `24910f14a230f6c7` (256 B)

4. The full ongoing investigation lives at
   [/home/danny/Downloads/AgentVision_linux-main/PS5_LINUX_DISPLAY_INVESTIGATION.md](file:///home/danny/Downloads/AgentVision_linux-main/PS5_LINUX_DISPLAY_INVESTIGATION.md)
   on the PS5. Section 8 is the most recent (Phase 5 success + Phase 7
   refresh-rate climb plan).

---

## Auto-memory entries Claude should keep current

If you're a future Claude session reading this and you have access to
the auto-memory system at
`/home/danny/.claude/projects/-home-danny-Downloads-AgentVision-linux-main/memory/`:

- `project_ps5_display_investigation.md` — should reflect Phase 1 status
  (custom bake with identity preservation works; refresh-rate climb is
  next phase)
- `reference_ps5_linux_repos.md` — already mentions ps5-payload-dev/sdk
  and itemzflow as Phase 2+ targets
- `user_ps5_linux_dev.md` — user is on Ubuntu 26.04 on PS5 phat, has
  WiFi USB adapter at 192.168.50.240, prefers to do PS5-side work on
  the Mac mini once we get to the SDK phase
- `feedback_save_before_risky_display_changes.md` — still active rule

---

## End

The "live" state of the wizard sources is whatever's at `ps5_display_wizard.py`
and `ps5_display_wizard_mac.py` in this directory. If those diverge from
what this doc describes, the SOURCE wins. Update this doc when you change
behavior.
