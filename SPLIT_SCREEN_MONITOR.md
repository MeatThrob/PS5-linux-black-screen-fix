# Monitor Split Screen / Dual Input — ASUS VG27WQ

## Does the VG27WQ support it?

**Yes.** The ASUS VG27WQ has built-in PbP (Picture-by-Picture) and PiP
(Picture-in-Picture) via the OSD menu (GamePlus or Input settings).

---

## Ways to run 2 computers on 1 monitor

### Option 1 — Built-in PbP / PiP (no extra hardware)
Connect both computers to separate inputs on the VG27WQ:

| Input | Computer |
|-------|----------|
| DisplayPort | PS5 (Linux) |
| HDMI | Mac / PC |

Then in the OSD: **Input Select → PbP / PiP** to activate side-by-side
or picture-in-picture mode.

> **PS5 Linux caveat:** the PS5's DP→HDMI converter (MN864739 / FLAVA3)
> only drives the **DP-1** output. The baked EDID must match the
> connected input. In PbP mode the monitor negotiates a different EDID
> per input — the baked EDID still applies to the DP-1 side.

### Option 2 — ASUS KVM (not on VG27WQ)
The VG27WQ does **not** have a built-in KVM switch. That feature is on
the ASUS ProArt / TUF KVM series.

### Option 3 — External HDMI/DP KVM switch
Any hardware KVM switch works. Connect:
- KVM Port A → PS5 HDMI output
- KVM Port B → Mac/PC
- KVM output → VG27WQ HDMI or DP input

One button toggles between machines. No software needed.

### Option 4 — Software only (no screen split)
- **Apple Universal Control** — Mac + iPad on one keyboard/mouse, no
  screen sharing.
- **Microsoft Garage Mouse Without Borders** — Windows only.
- **Barrier / Synergy** — cross-platform KVM over LAN. Controls
  keyboard/mouse across machines but each machine uses its own display
  unless combined with PbP.

---

## Recommended setup for PS5 Linux dev work

```
VG27WQ DisplayPort ← PS5 (Linux, baked EDID, 2560×1440@60)
VG27WQ HDMI        ← Mac (native, full resolution)
OSD PbP            ← side-by-side when you need both visible
```

Switch inputs via OSD or the monitor's input-select button. No extra
hardware required.

---

## Notes for the Linux instance of Claude on this machine

- The PS5 Display Wizard lives at:
  `/Volumes/external GAME drive/ps5-display-wizard/`
- The PS5 IP is `192.168.50.240` (user: `danny`)
- The USB boot partition mounts at `/run/media/danny/X10 Pro` on the PS5
  (NOT at `/boot/efi` — udisks auto-mounts it there)
- The installer script is at:
  `/Volumes/boot/ps5-autoedid-install.sh` on the Mac side, and needs
  to be copied to the USB so the PS5 can run it via SSH:
  ```bash
  # From the PS5 (SSH):
  sudo bash "/run/media/danny/X10 Pro/ps5-autoedid-install.sh"
  ```
- See `KNOWN_WORKING.md` for the full recovery playbook and kernel
  whitelist before touching anything.
