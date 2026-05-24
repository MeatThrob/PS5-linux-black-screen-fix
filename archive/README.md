# Archived design docs

These files reflect earlier explorations and are kept for historical
context only. The current project state is documented in the top-level
[README.md](../README.md) and [PROJECT_STATUS.md](../PROJECT_STATUS.md).

- **PHASE_0_RESEARCH.md** — original feasibility study for a native PS5
  homebrew ELF that would read EDID via Sony's `libSceVideoOut` APIs.
  This approach was abandoned: the video-out APIs reject every call from
  a homebrew context on FW 4.03, even after ucred escalation, because
  the single video-out session is owned by `SceShellUI`. The host-side
  EDID read (macOS / Linux GUI) replaced this plan.

- **PHASE_1_BUILD_STATUS.md** — snapshot of in-progress work from an
  earlier dev session. Superseded by PROJECT_STATUS.md.
