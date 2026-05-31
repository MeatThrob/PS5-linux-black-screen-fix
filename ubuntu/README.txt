MeatHandler V7 — PS5 Linux display fix + monitor switcher GUI
=============================================================

Changelog
---------
  V7:
    - Fix: monitor name showed "Unknown monitor". The GUI now reads the
      EDID model-name (0xFC descriptor) correctly, with a manufacturer
      fallback, so monitors absent from the DB still show a real label
      (e.g. "TCL 43S310R").
    - Fix: the GUI no longer prompts for a sudo/admin password every time.
      The polkit policy now grants the seat-active local desktop user
      (allow_active=yes) and matches the exact helper pkexec runs.
    - Fix: removed a dead X11 `xrandr` underscan call that hung the
      installer/boot on Wayland. The 1440p@120 centering passthrough guard
      makes it unnecessary.
    - Fix: Screen Dim live preview now works while dragging the slider.
      The GUI runs as root via pkexec; the preview D-Bus call is now made
      as the logged-in user so it reaches their session dim daemon.
    - Screen Dim darkness is capped at 90% (screen never goes fully black;
      a 10% brightness floor is enforced in both the GUI and the daemon).
    - Screen Dim scales the display's colour-corrected (ICC) gamma ramp
      per channel instead of a flat linear ramp, keeping colour neutral.
  V6: integrated Screen Dim (dim-on-inactivity) into MeatHandler.
  V3: EDID normalize + external-USB-boot fix + monitor switcher GUI.

What it does
------------
1. Fixes the black-screen-on-resolution/FPS-change in PS5 Linux Display
   Settings by normalizing every monitor's EDID to what the FLAVA3 HDMI
   bridge will actually accept. Runs on every boot — works with any
   monitor automatically.
2. Fixes the external-USB-boot black-screen on PS5 Linux 7.0.10 by
   deferring amdgpu autoload until the ICC EDID notification has arrived.
3. Adds a GUI in the Linux app menu ("MeatHandler Display") that lets
   you pick any previously-connected monitor + resolution and apply it.
4. Screen Dim: a dim-on-inactivity feature. After a configurable idle
   time the whole screen smoothly dims to a configurable darkness; any
   input restores it. It dims by scaling the display gamma ramp (the
   same software method gammastep / f.lux / GNOME Night Light use) and
   never touches the HDMI backlight/DPMS path — that path crashes PS5
   Linux. OLED panels benefit from the darkened pixels. Adjust darkness
   + idle time in the GUI's "Screen Dim" section. The dim runs as a
   per-user service (meathandler-dim.service) inside the desktop session
   and restores full brightness if it is stopped.

Files in this folder
--------------------
  ps5-autoedid-v2-install.sh   — installer (runs on the PS5 as root)
  monitor_db.py                — monitor capability database (3,376 entries)
  edid_synth.py                — EDID synthesizer library
  monitor_history.py           — persistent monitor history library
  meathandler_apply.py         — apply backend (called by the GUI)
  meathandler_gui.py           — the GTK3 GUI
  meathandler_dim.py           — Screen Dim overlay daemon (systemd --user)
  meathandler.desktop          — desktop launcher entry
  README.txt                   — this file

Install
-------
  1. Boot Linux on the PS5 (any working state).
  2. Copy this folder onto the PS5.
  3. cd into the folder.
  4. chmod +x ps5-autoedid-v2-install.sh
  5. sudo bash ps5-autoedid-v2-install.sh
  6. Reboot.

After reboot:
  - EDID is normalized at boot (no more black screen on display switch)
  - The "MeatHandler Display" app appears in your Linux app menu next to
    "Displays". Open it to pick from previously-seen monitors and switch
    resolution / refresh rate on the fly.

How the GUI works
-----------------
Every time you boot with a monitor plugged in, MeatHandler records it
into the history file. Open the GUI to:
  - See all monitors you've ever booted with
  - Pick one to switch to
  - Choose a resolution (only PS5-kernel-accepted modes are shown:
    1080p@60, 1080p@120, 1440p@60, 4K@60)
  - Click Apply — the screen may go black briefly. Unplug your HDMI
    cable, plug into the new monitor — it should light up immediately.
  - 30-second auto-revert if you don't click "Keep this setting"

Verify
------
  cat /var/log/ps5-autoedid.log
  cat /var/log/meathandler-apply.log

Recovery (if a future boot black-screens)
-----------------------------------------
  ssh in: sudo /boot/efi/safe-boot.sh

Notes on PS5 hardware limits
----------------------------
The PS5 HDMI bridge (FLAVA3 / MN864739) is firmware-limited. These modes
are HARD-BLOCKED by the kernel gate and no software can enable them:
  - 1440p @ 120 Hz
  - 4K @ 120 Hz
  - 4K @ 90 Hz
These modes WILL work:
  - 1080p @ 60 Hz / 1080p @ 120 Hz (any monitor)
  - 1440p @ 60 Hz (any 1440p monitor)
  - 4K @ 60 Hz (any 4K monitor)

Requires
--------
The installer auto-installs: python3-gi, gir1.2-gtk-3.0, policykit-1
(needed for the GUI to write firmware paths with root via pkexec).
