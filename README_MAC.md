# PS5 Display Wizard — macOS notes

See the main [README.md](README.md) for the full overview. This page
covers the macOS-specific bits.

## Install

```bash
python3 ps5_display_wizard_mac.py
```

Requirements:

- Python 3.10+ with Tkinter.
  - python.org installer ships it.
  - Homebrew users: `brew install python-tk@3.13` (match your Python
    version).
  - Verify: `python3 -m tkinter` should pop open a small test window.
- No third-party Python packages.

## What's different from the Linux version

| | macOS | Linux |
| --- | --- | --- |
| EDID source | `ioreg` (IODisplayEDID + DisplayHints) | `/sys/class/drm/*/edid` |
| USB detection | `/Volumes` + `diskutil list` (handles hidden EFI partitions) | `/proc/mounts` |
| GUI toolkit | Tkinter | GTK 3 |
| Hidden boot partitions | Auto-mounts via admin prompt | Already mounted by Nautilus / udisks |

The bake / cpio / cmdline / revert code is identical between platforms,
so an EDID file baked on macOS works the same as one baked on Linux.

## USB detection on macOS

A typical PS5 Linux install is a GPT drive with:

- An EFI System Partition (FAT32, the **boot** partition holding
  `bzImage`, `initrd.img`, `cmdline.txt`).
- An ext4 partition (the OS root).

macOS does not auto-mount EFI System Partitions, and it can't read ext4 at
all. The wizard handles this by:

1. Listing every external volume already mounted under `/Volumes`.
2. Calling `diskutil list` to find unmounted partitions on external
   drives.
3. Mounting the EFI partition — using an admin prompt if needed.
4. Surfacing the ext4 partition as informational (you'd need macFUSE +
   ext4fuse to write to it from macOS).

Internal drives are excluded from every list.

## Caveats

- **macOS may cache EDIDs** under
  `/System/Library/Displays/Contents/Resources/Overrides/`. If a scan
  returns the wrong EDID, unplug + replug the monitor after boot to
  force a fresh DDC read.
- **HDMI adapters that translate EDID** (some KVMs, USB-C docks) will
  hand the Mac their *translated* EDID, which is what the wizard reads.
  Plug direct for cleanest results.
- **No native code signing.** Gatekeeper lets Python scripts run, but if
  you ever bundle this with `py2app` for distribution you'll need to
  notarize.
  
  
  Done cleanly. Summary:

File	Before	After
bzImage	10.5MB (7.0.2)	12.0MB (7.0.5) ← matches /boot/vmlinuz-7.0.5
initrd.img	23.4MB (7.0.2)	25.6MB (7.0.5) ← matches /boot/initrd.img-7.0.5
bzImage.705	—	NEW backup (12.0MB)
initrd.img.705	—	NEW backup (25.6MB)
bzImage.702	10.5MB	unchanged (rollback)
initrd.img.702	23.4MB	unchanged (rollback)
cmdline.txt	170B w/ stripped EDID	unchanged
edid/	3 files	unchanged
safe-boot.sh	1941B	unchanged
ps5-autoedid-install.sh	in root	moved to /removed/
All sha256s verified. Unmounted cleanly, safe to eject.

To boot 7.0.5: reboot the PS5 through your normal Linux launch flow. The loader will pick up the new bzImage and initrd.img automatically (same filenames, same paths).

If it fails to boot (black screen, panic, hang): pull the USB, mount on another machine (Mac/laptop), run:


cp bzImage.702 bzImage
cp initrd.img.702 initrd.img
…and you're back to 7.0.2.

After it boots 7.0.5 successfully, verify with uname -r (should print 7.0.5), then we can redo the xhci force-bind experiment with the richer spcie_* API exports available in this kernel and start writing the actual shim driver.


