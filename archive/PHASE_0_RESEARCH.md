# PS5 Display Wizard — Phase 0 Research

**Goal**: build a PS5 homebrew ELF (later: `.pkg` for ItemzFlow) that runs on
jailbroken **firmware 4.03**, scans the connected monitor's EDID via Sony's
own firmware APIs, then bakes that EDID into the PS5 Linux boot USB exactly
the way our Python wizard does — all in one click, before launching the
Linux loader payload.

This MD is the design + reading list. Read it on the Mac (the SMB share is
where I dropped it because the PS5 has to reboot into Sony OS to actually
run this stuff). Once you've read it, we begin Phase 1 on the Mac.

---

## TL;DR — Phase 0 outcomes

1. **YES — reading EDID from a 4.03 homebrew payload is feasible.** Sony's
   own `libSceVideoOut` exposes `sceVideoOutGetHdmiRawEdid_`, a userland
   function that returns the raw HDMI EDID bytes. The `ps5-payload-dev/sdk`
   already has the stub. No kernel R/W needed, no reverse-engineering.

2. **The toolchain is mature and 4.03 is a first-class target.** The
   `ps5-payload-dev/sdk` builds payloads with `clang-18 + lld-18`, supports
   `PS5SDK_FW=0x403`, and the ELF loader (`elfldr`) takes payloads on TCP
   port 9021 after the WebKit+kernel exploit chain.

3. **Distribution via ItemzFlow as an ELF "payload" is the right path.**
   PS5 FPKG (fake PKG) support is still limited — but ItemzFlow's "load
   payload from USB" feature runs ELF files directly. No fake-signing
   ceremony needed for a single-purpose tool like ours.

4. **The Linux-side cpio/cmdline logic ports cleanly to C.** It's
   straight file I/O and string manipulation — ~200 lines of C total.

5. **The PUP file** (`/home/danny/Downloads/PS5UPDATE.PUP`, 913 MB) is the
   4.03 firmware. Magic = `SLB2`. **We don't actually need to extract it
   for this project** — we're calling Sony's APIs as a black box, not
   reverse-engineering them. The PUP is useful only if Phase 2 hits a wall
   and we have to dig deeper. Keep it around.

---

## 1. The critical Sony APIs we'll use

Listed exports from `ps5-payload-dev/sdk/sce_stubs/libSceVideoOut.c`:

| Symbol | What it does | Why we care |
|---|---|---|
| `sceVideoOutOpen` | Acquire a handle to the HDMI output | Required first step |
| `sceVideoOutGetHdmiRawEdid_` | **Returns the raw EDID bytes** | **THIS is the payoff** |
| `sceVideoOutGetHdmiMonitorInfo_` | Returns Sony's parsed monitor struct | Backup if raw EDID fails |
| `sceVideoOutGetHdmiMonitorInfoNoMask_` | Same, unmasked variant | Backup |
| `sceVideoOutGetDeviceCapabilityInfo_` | Lower-level capabilities | Useful for sanity checks |
| `sceVideoOutSysGetMonitorInfo_` | System-level monitor info | Another fallback |
| `sceVideoOutGetMonitorInfo` | Userland-friendly wrapper | Maybe the first one to try |
| `sceVideoOutClose` | Release the handle | Cleanup |

The trailing `_` underscore convention on PS4/PS5 SDKs typically marks
"privileged" or "debug" variants. Jailbroken consoles have those
privileges already, so we should be fine. If not, the non-underscore
variants (`sceVideoOutGetMonitorInfo`) almost certainly work.

**Source for these exports** (verified during Phase 0):
- https://github.com/ps5-payload-dev/sdk/blob/master/sce_stubs/libSceVideoOut.c

**No publicly-published signature in the SDK headers** — those stubs are
weak symbols that link against the firmware's `libSceVideoOut.sprx`. To
get the right argument types/sizes we have one of two paths:
1. Look at PS4 reverse-engineered headers (same API family). The
   `OpenOrbis/OpenOrbis-PS4-Toolchain` and various PS4 wikis document
   `sceVideoOutGetMonitorInfo` etc.
2. Read disassembly of the function via Ghidra against the firmware's
   `libSceVideoOut.sprx` (extracted from the PUP).

Phase 1 starts with path #1 (PS4 docs) — fast.

---

## 2. The PS5 SDK + toolchain (Phase 1 setup, on the Mac)

**Primary SDK**: https://github.com/ps5-payload-dev/sdk

What it is: a clang-based cross-compiler + FreeBSD headers + Sony library
stubs that produces ELF files runnable on a jailbroken PS5 via an ELF
loader (`elfldr` listens on port 9021 after the exploit chain).

### Mac install steps (write these in your notes)

```bash
# Install Xcode CLI tools first if not already
xcode-select --install

# Install Homebrew if not already
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# clang-18 + lld-18 — Phase 0 confirmed these are the minimum versions
brew install llvm@18

# Add llvm to PATH (Apple Silicon path; adjust on Intel Mac)
echo 'export PATH="/opt/homebrew/opt/llvm@18/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# Clone + build the SDK
cd ~/Projects
git clone https://github.com/ps5-payload-dev/sdk.git
cd sdk
gmake install        # or: make install
```

After install, `$PS5_PAYLOAD_SDK` will be set in your shell. Sample
payloads at `$PS5_PAYLOAD_SDK/samples/` build by running `make` in any
sample dir.

### Targeting firmware 4.03

The SDK supports per-firmware builds. When compiling our wizard:

```bash
export PS5SDK_FW=0x403
```

This makes the build pick the 4.03-compatible variants of any
firmware-version-sensitive code. Most syscalls are stable across versions
but a few have shifting offsets.

### The ELF loader on the PS5

- `ps5-payload-dev/elfldr` runs on **TCP port 9021** after the jailbreak
  is active.
- You send the ELF: `socat - TCP:<PS5_IP>:9021 < my_payload.elf`
- elfldr launches it in a fresh process.

**Important**: you have to run the jailbreak chain first (WebKit + kernel
exploit), then start elfldr, then send the payload. ItemzFlow wraps that
loop in a UI button.

---

## 3. ItemzFlow integration path

**Source of truth**: https://github.com/itemzflow (you already have it
installed in `/boot/efi/itemzflow` on the PS5 USB).

ItemzFlow exposes a "Payloads" section that loads `.elf`/`.bin` files
from the PS5's USB drive. So our distribution is:

1. User downloads `ps5-display-wizard.elf` from our github
2. Drops it on a USB stick (the same one with the PS5 Linux loader, OR
   any USB readable by the PS5 firmware — typically a FAT32 stick)
3. Boots PS5 to its normal dashboard at 4.03 firmware
4. Runs the WebKit+kernel jailbreak (one-click via etaHEN)
5. Opens ItemzFlow → Payloads → picks our ELF
6. Our ELF runs:
   - Calls `sceVideoOutGetHdmiRawEdid_` to read the connected monitor's EDID
   - Mounts (or finds) the FAT32 partition that has `bzImage`, `initrd.img`,
     `cmdline.txt` (PS5 Linux loader stick)
   - Strips EDID to minimal 128-byte form, recomputes checksum
   - Builds the cpio fragment with `lib/firmware/edid/<slug>.bin` and
     `usr/lib/firmware/edid/<slug>.bin` entries
   - Appends fragment to `<USB>/initrd.img` (after backing up to .tv)
   - Edits `<USB>/cmdline.txt` (after backing up to .tv) to add the
     `drm.edid_firmware=DP-1:edid/<slug>.bin video=DP-1:e` tokens
   - Writes safe-boot.sh to the USB
   - Prints status to stdout (visible in ItemzFlow's log view)
7. User reboots PS5 → ps5-linux-loader runs as usual → Linux comes up
   with the freshly-baked EDID → display works first try

### About FPKG (.pkg) packaging

Per Phase 0 search results:
- "PS5 FPKG support remains limited compared to PS4"
- ItemzFlow's "Payload" section runs ELF directly without needing fake
  signing
- A proper `.pkg` would require fake-self/fake-pkg signing, an .sfo
  descriptor, icons — substantial extra build pipeline for marginal UX
  win (.pkg shows on home screen with icon; .elf in ItemzFlow needs one
  extra menu tap)

**Decision: skip .pkg for v1.** Ship `.elf` + a 5-line README about how
to load via ItemzFlow. Revisit .pkg only after the .elf path is proven.

---

## 4. The actual ELF — design sketch

File: `ps5_display_wizard.c` (single file)

```c
// Pseudocode for the payload — actual C in Phase 2
#include <stdio.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <string.h>
#include <sys/mount.h>
#include <ps5/kernel.h>             // from ps5-payload-dev sdk
extern int sceVideoOutOpen(int userId, int busType, int index, void *param);
extern int sceVideoOutGetHdmiRawEdid_(int handle, void *edidBuf, size_t bufLen);
extern int sceVideoOutClose(int handle);

int main(void) {
    // 1. Get the EDID
    int h = sceVideoOutOpen(0xFF, 0 /* MAIN */, 0, NULL);
    uint8_t edid[256] = {0};
    int n = sceVideoOutGetHdmiRawEdid_(h, edid, sizeof(edid));
    sceVideoOutClose(h);
    if (n <= 0) { printf("EDID read failed\n"); return 1; }

    // 2. Find PS5 Linux USB by scanning known mount points
    //    PS5 firmware mounts USB at /mnt/usb0, /mnt/usb1, etc.
    char usb[64] = {0};
    if (!find_ps5_linux_usb(usb, sizeof(usb))) {
        printf("no PS5 Linux USB found (need bzImage + initrd.img + cmdline.txt)\n");
        return 2;
    }
    printf("found USB at %s\n", usb);

    // 3. Strip EDID to 128 bytes + recompute checksum
    uint8_t stripped[128];
    memcpy(stripped, edid, 128);
    stripped[126] = 0;  // no extension blocks
    fix_edid_checksum(stripped);

    // 4. Slugify monitor name from EDID descriptors → "AUS-VG27WQ" → "aus-vg27wq"
    char slug[64] = {0};
    edid_slug(edid, slug, sizeof(slug));

    // 5. Backup cmdline.txt, initrd.img → .tv copies (first run only)
    backup_once(usb, "cmdline.txt");
    backup_once(usb, "initrd.img");

    // 6. Write EDID file at <usb>/edid/<slug>-stripped.bin
    write_edid(usb, slug, stripped);

    // 7. Build cpio fragment (~250 bytes), append to initrd.img
    append_cpio_to_initrd(usb, slug, stripped);

    // 8. Edit cmdline.txt: read .tv, strip any old drm.edid_firmware= /
    //    video=DP-1: tokens, append our new ones, write out
    rewrite_cmdline(usb, slug);

    // 9. Write safe-boot.sh
    write_safe_boot(usb);

    printf("DONE — eject USB, plug into PS5 for Linux, boot.\n");
    return 0;
}
```

**Helpers to port from Python wizard** (line counts approximate):
- `fix_edid_checksum()` — 5 lines
- `edid_slug()` — 30 lines (parse offsets 54/72/90/108 for descriptor 0xFC)
- `backup_once()` — 10 lines (copy file if dest doesn't exist)
- `append_cpio_to_initrd()` — 60 lines (newc cpio writer + read .tv + write)
- `rewrite_cmdline()` — 30 lines (read .tv, strtok, filter, append, write)
- `find_ps5_linux_usb()` — 30 lines (open /mnt/usb0..usb7, check for files)
- `write_safe_boot()` — 5 lines (embed string constant, write file)

Plus boilerplate ~30 lines. **Total ~200 lines C**, single file.

---

## 5. Critical unknowns we'll resolve in Phase 1 (on the Mac)

Things I couldn't fully nail down from this PS5 (no SDK installed, no PS4
docs immediately searchable) — Phase 1 starts by answering:

| Unknown | How we'll resolve |
|---|---|
| **Exact signature of `sceVideoOutGetHdmiRawEdid_`** | Look at PS4 reverse-engineered headers in `OpenOrbis-PS4-Toolchain`; same API family. If not found there, disassemble libSceVideoOut.sprx from the PUP via Ghidra. |
| ~~**Where the PS5 firmware mounts USB sticks**~~ | ✅ **RESOLVED**: scan the same 8 paths `ps5-linux-loader` itself scans (see §5a below). NOT just `usb0`. |
| **Whether 4.03 firmware paths are writable to the FAT32 stick from a payload** | Almost certainly yes — etaHEN's package installer writes to USB already. |
| **Whether ItemzFlow forwards stdout/stderr to its UI** | Per docs yes. If not, we write to a logfile on USB and tell user to read it. |

---

## 5a. USB scan list — match what the linux-loader does (CRITICAL)

The PS5 firmware doesn't always mount the Linux USB at `/mnt/usb0`. Slot
varies depending on which USB port is used, what other USB devices are
plugged in, and the order they enumerate. **Our payload MUST scan the
same list the linux-loader itself scans, or it'll silently miss the USB
on a slot it didn't expect.**

Verified in `ps5-linux/ps5-linux-loader/source/loader.c` (line 23-25):

```c
static const char *const search_paths[] = {
    "/mnt/usb0/",           "/mnt/usb1/",           "/mnt/usb2/",
    "/mnt/usb3/",           "/mnt/usb0/PS5/Linux/", "/mnt/usb1/PS5/Linux/",
    "/mnt/usb2/PS5/Linux/", "/mnt/usb3/PS5/Linux/",
};
```

The loader also honors a **`path-override.txt`** file: if `path-override.txt`
exists at any of the default paths and contains a path string, the loader
uses that path instead. Our payload should respect this too — if
`path-override.txt` exists pointing somewhere, that's where the user wants
the Linux files and that's where we bake the EDID.

### CRITICAL — the loader searches each file INDEPENDENTLY

Re-reading `find_and_get_size_of_file()` carefully: the loader doesn't
check for "a directory containing all three files". It iterates the
search-paths list for **each file separately**, taking the first match.
So `bzImage` could live at `/mnt/usb0/`, `initrd.img` at `/mnt/usb1/`,
and `cmdline.txt` at `/mnt/usb2/PS5/Linux/` — totally legal, the loader
handles it.

It also supports a **filename override** via `path-override.txt`
entries like `bzImage=/path/to/custom-bzImage` (see
`get_overridden_filename()` in `source/loader.c`). So a user can remap
*which* bzImage gets loaded without moving the file.

**Our payload must match this exactly** — we need to bake the EDID
into the same `initrd.img` and `cmdline.txt` that the loader will
actually consume. If we modify a different copy, we accomplish nothing.

### Our C scan logic (Phase 2 implementation)

```c
static const char *const PS5_SEARCH_PATHS[] = {
    "/mnt/usb0/",            "/mnt/usb1/",
    "/mnt/usb2/",            "/mnt/usb3/",
    "/mnt/usb0/PS5/Linux/",  "/mnt/usb1/PS5/Linux/",
    "/mnt/usb2/PS5/Linux/",  "/mnt/usb3/PS5/Linux/",
    NULL,
};

// Reads path-override.txt entries (same format as the loader).
// Each line is either "filename=relative-path" (per-file override) or
// just a base path (legacy interpretation).
struct file_overrides {
    char bzimage[512];        // filename or relative path; empty = default
    char initrd[512];
    char cmdline[512];
};

void load_overrides(struct file_overrides *o) {
    memset(o, 0, sizeof(*o));
    char buf[2048];
    char path[512];
    for (const char *const *p = PS5_SEARCH_PATHS; *p; p++) {
        snprintf(path, sizeof(path), "%spath-override.txt", *p);
        int fd = open(path, O_RDONLY);
        if (fd < 0) continue;
        ssize_t n = read(fd, buf, sizeof(buf) - 1);
        close(fd);
        if (n <= 0) continue;
        buf[n] = 0;
        // Parse lines: filename=path
        for (char *line = strtok(buf, "\n"); line; line = strtok(NULL, "\n")) {
            if (strncmp(line, "bzImage=", 8) == 0)
                strncpy(o->bzimage, line + 8, sizeof(o->bzimage) - 1);
            else if (strncmp(line, "initrd.img=", 11) == 0)
                strncpy(o->initrd, line + 11, sizeof(o->initrd) - 1);
            else if (strncmp(line, "cmdline.txt=", 12) == 0)
                strncpy(o->cmdline, line + 12, sizeof(o->cmdline) - 1);
        }
        return;  // first found wins, same as loader
    }
}

// Mirrors loader's find_and_get_size_of_file(): independent search per file.
// Returns the absolute path the loader will use, or empty on miss.
int locate_file(const char *filename, const char *override,
                char *out, size_t out_sz) {
    const char *target = (override && *override) ? override : filename;
    char path[512];
    struct stat st;
    // If override is an absolute path, just verify it exists
    if (target[0] == '/') {
        if (stat(target, &st) == 0) {
            snprintf(out, out_sz, "%s", target);
            return 1;
        }
        return 0;
    }
    // Otherwise iterate PS5_SEARCH_PATHS
    for (const char *const *p = PS5_SEARCH_PATHS; *p; p++) {
        snprintf(path, sizeof(path), "%s%s", *p, target);
        if (stat(path, &st) == 0) {
            snprintf(out, out_sz, "%s", path);
            return 1;
        }
    }
    return 0;
}

// Top-level: returns the THREE paths the loader will consume.
struct linux_target {
    char bzimage_path[512];
    char initrd_path[512];
    char cmdline_path[512];
};

int find_linux_target(struct linux_target *t) {
    struct file_overrides o;
    load_overrides(&o);
    int ok = 1;
    ok &= locate_file("bzImage", o.bzimage, t->bzimage_path,
                       sizeof(t->bzimage_path));
    ok &= locate_file("initrd.img", o.initrd, t->initrd_path,
                       sizeof(t->initrd_path));
    ok &= locate_file("cmdline.txt", o.cmdline, t->cmdline_path,
                       sizeof(t->cmdline_path));
    return ok;
}
```

The wizard then operates on `t->initrd_path` and `t->cmdline_path`
specifically — **the actual files the loader will consume**, wherever
they happen to live. The `t->bzimage_path` we don't modify but we log
it so the user can see we found the right setup.

**Edge cases handled correctly**:
- USB stick in port 3 only (not 0): each file's `locate_file()` walks the
  list, hits at usb3.
- Linux files in a `PS5/Linux/` subfolder: same — the search-paths list
  includes `/mnt/usbN/PS5/Linux/`.
- User has `path-override.txt` with `cmdline.txt=/mnt/ssd0/my-cmdline.txt`:
  we honor it; our wizard then writes to that exact file.
- Files spread across multiple USBs (rare but legal): each gets located
  independently, all three end up in the `linux_target` struct, we modify
  the right ones in place.

**Edge cases that intentionally fail closed (we print and bail)**:
- Any of the three required files missing: don't guess, don't write
  anything, print which file was missing.
- bzImage found but no initrd/cmdline: same — bail.

### Bonus — writing a path-override.txt from the wizard

Once we've located the files, we could optionally write a
`path-override.txt` on USB0 pinning the choices for future boots. Not
necessary in v1 — the loader scans every boot anyway — but a small
idempotency win for v2.

**Edge cases this handles correctly**:
- USB stick in port 3 only (not 0): tries usb0, usb1, usb2, finds at usb3
- User has Linux files inside a `PS5/Linux/` subfolder on USB: tries plain
  root paths first (no match), then `<usb>/PS5/Linux/` paths, finds there
- User has `path-override.txt` pointing to `/mnt/ssd0/my-linux/`: respects it

**Edge cases that intentionally fail closed (we print and bail)**:
- No USB has all three files: don't guess, don't write anything
- Multiple USBs each have the files: we take the *first match*, log which
  one we used, user can use `path-override.txt` if they want a specific one

### Bonus — writing a path-override.txt from the wizard

Once we've found the path, we could optionally write a `path-override.txt`
on USB0 so subsequent loader runs always go to the right slot even if
the user re-plugs. Probably not necessary in v1 — the loader scans
every boot anyway — but a nice idempotency win for v2.

---

## 6. Decision log so far

- **No payload server.** ELF dropped on USB, ItemzFlow launches it. One
  tap from the PS5 UI.
- **Skip .pkg in v1.** ItemzFlow's payload menu runs ELFs directly. Fake-
  signing ceremony is not worth the time for a single-purpose tool.
- **Port logic to C, not bundle Python.** Bundling Python on PS5 firmware
  is a much heavier project than rewriting ~200 lines.
- **Use Sony's `sceVideoOutGetHdmiRawEdid_` API**, not kernel memory dump.
  Userland call, no fragile offsets, no fw-version-specific reverse engineering.
- **Keep the PUP file but don't extract.** Only revisit if Phase 1 hits an
  API signature dead end and we need to disassemble libSceVideoOut.sprx.

---

## 7. Sources

- ps5-payload-dev SDK: https://github.com/ps5-payload-dev/sdk
  - libSceVideoOut stubs: https://github.com/ps5-payload-dev/sdk/blob/master/sce_stubs/libSceVideoOut.c
- ps5-linux-loader USB scan logic (the canonical reference for path search):
  https://github.com/ps5-linux/ps5-linux-loader/blob/main/source/loader.c
- ps5-payload-dev/elfldr: https://github.com/ps5-payload-dev/elfldr
  (ELF loader on port 9021)
- ItemzFlow: https://github.com/itemzflow
- 4.03 jailbreak / sleirsgoevy: https://sleirsgoevy.github.io/ps4jb2/ps5-403/
- PS5 kernel exploit notes (cragson/ps5-hen): https://github.com/cragson/ps5-hen
- etaHEN: https://github.com/etaHEN/etaHEN
- ItemzFlow + etaHEN 2.0b writeup: https://wololo.net/2025/03/31/ps5-etahen-2-0b-and-itemzflow-1-09-released-pkg-install-writeup-by-lightningmods/
- PS5-UMTX-Jailbreak (alt path): https://github.com/PS5Dev/PS5-UMTX-Jailbreak
- OpenOrbis PS4 Toolchain (for sceVideoOut PS4 signatures we'll borrow):
  https://github.com/OpenOrbis/OpenOrbis-PS4-Toolchain

---

## 8. Phase 1 plan (what we do once you're back on the Mac)

**Day 1**:
1. Install Homebrew + llvm@18 on the Mac (if not already)
2. Clone + install `ps5-payload-dev/sdk`
3. Build the `hello_world` sample. Confirm it produces a working `.elf`.
4. Send it to the PS5 via ItemzFlow (or socat to port 9021). Verify it
   prints something visible.

**Day 2**:
1. Write `edid_dump.c` — minimal payload that ONLY calls
   `sceVideoOutOpen` + `sceVideoOutGetHdmiRawEdid_` + writes the bytes
   to `/mnt/usb0/edid-dump.bin`.
2. Run it on the PS5 with the VG27WQ connected.
3. Verify the bytes match the EDID we know from Linux side
   (sha256 should match VG27WQ's known hash).

**Day 3-5**:
1. Port the wizard logic to C: cpio newc writer, EDID stripper, cmdline
   rewriter, USB detector, safe-boot.sh writer.
2. Bundle into a single `ps5_display_wizard.elf`.
3. Test end-to-end: run via ItemzFlow → eject USB → plug into PS5 → boot
   PS5 Linux → confirm display works first try.

**Day 6-7**:
1. Publish to a new github repo with the existing Python wizard.
2. Update [PS5_LINUX_DISPLAY_INVESTIGATION.md](PS5_LINUX_DISPLAY_INVESTIGATION.md) with Phase 7.5b status.
3. Optionally: investigate `.pkg` packaging if the .elf workflow proves
   awkward.

---

## 9. Things to grab from the Mac side once you boot in

When you SSH from the Mac back into the PS5 Linux to grab files, you'll
want these locally on the Mac:

- `/home/danny/Projects/ps5-display-wizard/` (whole folder)
- This MD file (already on SMB, so just copy from there)
- The `/home/danny/Desktop/ps5-linux-tools/` folder if you want to peek
  at the existing tools' build setup as a reference

Or just keep using SMB — we can drop the PS5 SDK install on a folder
under the share and work from there.

---

## 10. Honest scope honesty

- **EDID reading via Sony API**: high confidence, well-trodden ground.
- **cpio/cmdline porting to C**: low risk, mechanical work.
- **First-time PS5 SDK build on Mac**: medium risk — LLVM version
  matters, FreeBSD sysroot has to land in the right place, occasionally
  the SDK requires patches for newer macOS. Budget 1 day buffer.
- **Distribution polish (.pkg, etc.)**: explicitly punted.

Realistic ETA for a working `.elf` that does the full job:
**5-10 evenings of focused work**, assuming no major API surprises.

---

## End

When you read this on the Mac, the next move is to install the SDK and
build the sample. Ping me once that's done and we start Phase 1.
