# PS5 Linux black-screen fix — CachyOS / Arch

Fixes the black-screen-on-boot bug that hits many monitors when running Linux on PS5. **One file. One command.**

## Install

Boot CachyOS on your PS5 (any working state — even via SSH if the screen is dead), then:

```sh
curl -LO https://raw.githubusercontent.com/MeatThrob/PS5-linux-black-screen-fix/main/cachyos/ps5-autoedid-cachyos-install.sh
sudo bash ps5-autoedid-cachyos-install.sh
sudo reboot
```

That's it. The single `.sh` contains everything (the EDID logic, the monitor database, the mkinitcpio hooks). No clone, no folder, no other downloads.

## What it does

Normalizes the connected monitor's EDID at the **earliest possible boot stage** — inside the initramfs, before `amdgpu` probes the display. Once installed it works automatically on every boot.

Boot-fix only. No GUI. No screen-dim. For the full Ubuntu version with a display-switcher GUI, see [`../ubuntu/`](../ubuntu/).

## Recovery

If something goes wrong, SSH in and:

```sh
sudo /boot/efi/safe-boot-cachyos.sh && sudo reboot
```
