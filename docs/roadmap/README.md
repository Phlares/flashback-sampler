# flashback-sampler roadmap

Forward-looking plans and backlog. Everything here used to live in
local Claude Code workspace state (`~/.claude/plans/` and per-project
memory files) which is gitignored and machine-local. Moved into the
repo so a new machine / new contributor / future self can pick up the
thread without reconstructing it from commit messages.

## Files

- **[`original-plan.md`](original-plan.md)** — the full plan document
  from the M0 planning session (2026-04-12). Historical reference;
  most of M0–M11 is shipped and some architectural decisions have
  shifted. Keep for provenance, don't edit.
- **[`backlog-visual-issues.md`](backlog-visual-issues.md)** — layout
  bugs caught post-M13. The three items reported by the user; two are
  fixed, one (z-order bleed) is pending.
- **[`backlog-ux-review.md`](backlog-ux-review.md)** — queued UX items
  + the full-review milestone. **Blocks** VST3 / OBS packaging and
  macOS / Linux capture backends until complete.

## Shipped milestones

(Cross-referenced with commit hashes so you can `git show` any of them.)

| Milestone | Commit | Summary |
|---|---|---|
| M0–M11 | various | Ring buffer, checkout manager, scrub player, Erebus shell, live buffer track, checkout track + transport, device picker + config persistence, Erebus visual polish, M9 backlog (B1–B5), Shape B multi-source refactor, per-slot routing, per-slot capture spec, chip source labels + clip source caption |
| **M12.1** | `61f4360` | Native Windows per-process WASAPI loopback foundation (ctypes COM glue, `ActivateAudioInterfaceAsync`, `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`) |
| **M12.2** | `ec008cf` | Format negotiation (float32→PCM16 fallback chain), int16 capture path, `CaptureSource.last_error()` protocol |
| **M13** | `d2fc565` | Arm/roll master transport, per-slot focused clip memory, checkout-vs-trim routing, error surfacing to chips, low-res layout polish, settings dialog audit |
| **M14** | `a3de5c5` | PyInstaller Windows onedir packaging scaffold |

## Top-of-queue

1. **Visual polish round 2** — the one remaining z-order bleed in
   `backlog-visual-issues.md`. Keep it small, don't bundle.
2. **UX review milestone** (see `backlog-ux-review.md`) — this is the
   big one. Sit down with the frontend-design skill, walk every golden
   flow against the *current* feature set (not the M0 spec), and
   produce revised wireframes in `docs/wireframes/`. Output must land
   before any of the items below start.
3. **Duration picker replacement** — left/right arrow stepping or a
   subtle slider replacing the 8-preset cluster. Deferred into the UX
   review.

## Explicitly blocked on the UX review

- VST3 plugin packaging (JUCE wrapper or similar)
- OBS dock plugin
- macOS AudioTap backend (macOS 14.4+ API)
- Linux PipeWire sink-input monitoring backend
- Any "onefile" / shippable Windows packaging push

## Not in scope (still)

- Raspberry Pi hardware path (`hardware/encoder.py` stays as-is)
- Per-app isolation that depends on VB-CABLE or VoiceMeeter
- Telemetry, cloud sync, accounts
