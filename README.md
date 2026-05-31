# PS5 Linux black-screen fix

A community fix for the black-screen-on-display-init bug that hits many monitors when running Linux on PS5. Per-distro installers, each self-contained.

The PS5 has no HDMI hot-plug detect. `amdgpu` sees the monitor's EDID exactly once, from the firmware loader. If that EDID has timings the PS5 kernel's `isHdmiModeValid()` gate rejects, the screen never lights up. The fix: normalize (or pass through) the EDID at the **earliest possible boot stage** — inside the initramfs, before `amdgpu` loads.

## Distros

| Distro | Folder | Status | Includes |
|---|---|---|---|
| Ubuntu 26.04 | [`ubuntu/`](./ubuntu/) | **Tested on real PS5 hardware** (kernel 7.0.5) | Boot-fix + display-switcher GUI + screen dim |
| CachyOS / Arch | [`cachyos/`](./cachyos/) | Community testing needed | Boot-fix only |
| Bazzite | _coming_ | Community testing needed | Boot-fix only |
| Batocera | _coming_ | Community testing needed | Boot-fix only |

All ports share the same verified EDID-normalize logic (passthrough guard + kernel-gate match + VIC 63 injection for 1080p@120). They differ only in distro-native plumbing: `initramfs-tools` vs `mkinitcpio` vs `dracut`, `apt` vs `pacman` vs `rpm-ostree`, and the per-distro `cmdline-<distro>.txt` / `initrd-<distro>.img` names per the PS5-Linux kexec contract.

## What works after install

- Black-screen-on-display-init is gone.
- 1080p@60, 1080p@120 (any monitor), 1440p@60, 4K@60 work as the monitor allows.
- 1440p@120 and 4K@120 are **kernel-gated** by the PS5 HDMI bridge firmware — no software fix can enable them.

## Recovery

Every installer drops a `safe-boot-<distro>.sh` on the FAT32 boot partition that reverts to the pre-install cmdline if the screen dies. SSH in from another machine and run it.

## Credits

EDID normalization logic developed on Ubuntu 26.04 / PS5 Linux 7.0.5. Boot contract verified against [github.com/ps5-linux/ps5-linux-image](https://github.com/ps5-linux/ps5-linux-image).
