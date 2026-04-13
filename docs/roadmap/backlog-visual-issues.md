# Visual / layout backlog

Queued for the next visual-polish pass. Noted after M13 landed.
Fix before any public packaging.

## Status

- M13 landed the first round of scaling fixes: rotary cluster
  bounded, waveform gutter widened, window minimum bumped to
  960×860. Those landed in commit `d2fc565`.
- The items below are what's **still broken** at the new minimum
  or regressed after M13's fixes.

## Still broken

1. **CHECK OUT button label + rotary hint z-order bleed.** After
   the rotary cluster was bounded, the transport row's CHECK OUT
   label and "DBL-CLICK = NOW" hint now render underneath Track 2
   at some resize points. It's a z-order / layout overlap, not a
   paint clip. Root cause: vertical budget math lets Track 2 grow
   past the transport row's allotted area.

   **Fix:** stop using additive `addWidget(..., stretch=2)` on
   both tracks without bounding growth. Either give Track 2 a
   fixed-ish height with internal scaling, or put transport in a
   bounded container, or switch the main layout from stacked to
   a QGridLayout with explicit row weights.

## Notes from the pre-M13 report (partially addressed)

Original report from the user after M12 landed, for context:

1. **Floating center text clips upward.** Rotary hub readout / central transport label cut off at its top edge when the window was short. **Addressed in M13** via the bounded `rotary_host` widget and window minimum bump.

2. **Track 1 waveform bleeds into its own timeline.** Bottom edge of live buffer waveform overlapping the time-axis strip. **Addressed in M13** via wider timeline gutter + clamped `safe_half` in `WaveformView.paintEvent`.

3. **General scaling seams.** **Partially addressed in M13.** The CHECK OUT / hint z-order bleed above is the remaining thread.

**Why:** visual polish of an Erebus-themed DAW-adjacent UI is load-bearing for the VST/OBS plugin credibility goal. Shipped UI needs to survive ~25% downward resize without layout breakage.

**How to apply:** when the next visual-polish milestone kicks off, start with these three items — they're the concrete reproducers. Root cause: hard-coded rects in paintEvents that should be computed from the widget's current size + reserved margins.
