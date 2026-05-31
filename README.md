# PS5 Linux black-screen fix

One-file installers that fix the black-screen-on-boot bug when running Linux on PS5. Per-distro, no clone required.

## Pick your distro

### CachyOS / Arch

```sh
curl -LO https://raw.githubusercontent.com/MeatThrob/PS5-linux-black-screen-fix/main/cachyos/ps5-autoedid-cachyos-install.sh
sudo bash ps5-autoedid-cachyos-install.sh
sudo reboot
```

### Ubuntu 26.04

Tested on real PS5 hardware. Includes a display-switcher GUI and screen-dim.
See [`ubuntu/`](./ubuntu/).

### Bazzite / Batocera

Coming soon.

## How it works

PS5 has no HDMI hot-plug detect. `amdgpu` sees the monitor's EDID exactly once, from the firmware loader. If that EDID has timings the PS5 kernel's `isHdmiModeValid()` gate rejects, the screen never lights up. The fix normalizes the EDID at the earliest boot stage — inside the initramfs, before `amdgpu` loads.

Works automatically on every boot. Locked to the monitor that's plugged in when the initramfs runs.

## Recovery

Every installer drops a `safe-boot-<distro>.sh` on `/boot/efi`. If the screen dies, SSH in from another machine and run it.
