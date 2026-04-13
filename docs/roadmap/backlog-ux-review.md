# UX backlog + full-review milestone (pre-VST / pre-multiplatform)

Queued UI fixes + the full UX review milestone that **blocks**
any VST3 / OBS plugin packaging and any macOS / Linux capture
backend work. Nothing cross-platform starts until the review is
done.

## Queued UX items (deferred until the scoped review)

### Z-order bleed: checkout button label + rotary hint get buried under Track 2
After raising the window minimum and fixing the rotary cluster clip, the CHECK OUT button label and the "DBL-CLICK = NOW" hint below the rotary still get rendered underneath the Track 2 CheckoutTrack widget at some resize points. It's a z-order / layout overlap, not a paint clip. The transport row's right-col CHECK OUT button and the rotary hint label both end up under Track 2's widget rect.

**Why:** the vertical budget math in the stacked layout lets Track 2 expand past where the transport row thinks its bottom is, so the transport widgets get painted, then Track 2 paints over them.

**How to apply:** during the next visual-polish pass, stop using additive `addWidget(..., stretch=2)` layout without bounding the growth. Either (a) give Track 2 a fixed-ish height with internal scaling, or (b) put the transport row in its own bounded container, or (c) switch from stacked to a grid layout with explicit row weights.

### Duration picker: replace 8-preset cluster
The current 8-cell preset cluster (0:15, 0:30, 1:00, 2:00, 3:00, 5:00, 10:00, MAX) is visually heavy for the job. User prefers either:
- Left/right arrow-key stepping through values
- A subtle slider / gauge that matches the Erebus recessed-screen aesthetic

**Why:** duration is a single scalar, not 8 orthogonal options — the 8 tactile pads look important but aren't. And the block takes real estate the transport row can't spare.

**How to apply:** when the UX review kicks off, prototype two replacements and pick one. Keep the underlying preset list as snap points on whatever new control lands (so typing or arrow-keying still lands on round values).

## Full UX review milestone (before VST / multi-platform)

**Scope:** sit down with every feature that has shipped (M0–M12.1), walk the golden flows end-to-end, and produce a revised wireframe that moves bits around with full knowledge of what the app actually does. The original 960×520 landscape wireframes from the frontend-design pass were designed around a narrower feature set.

**Explicit dependency:** nothing about VST3 / OBS plugin packaging or macOS / Linux capture backends should start until this review is done. Packaging around the wrong layout locks in mistakes that become painful to unwind when you're shipping inside a DAW channel strip.

**Why:** layout decisions at this stage (which track goes where, how many slots fit, where checkout list lives, what the transport cluster actually needs) will dictate the VST3 editor dimensions and the OBS dock shape. Get them right once on the native desktop build, then port.

**How to apply:** when the user signals "let's do the UX review", invoke the frontend-design skill with the full current feature list (not the M0 spec). Document the revised wireframe in `docs/wireframes/` before touching code.
