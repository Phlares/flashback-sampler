# Monaspace fonts

Bundled subset of the **Monaspace** type family from GitHub Next. Used by the
Erebus visual system for all labels, readouts, and body text — the app's
discipline rule is "no sans-serif", and Monaspace is our canonical stack.

## Files

| File | Role | Used for |
|---|---|---|
| `MonaspaceKrypton-Regular.otf` | display | small mono numbers |
| `MonaspaceKrypton-Medium.otf`  | display | default display weight |
| `MonaspaceKrypton-Bold.otf`    | display | large numeric readouts (rotary hub, time) |
| `MonaspaceNeon-Medium.otf`     | label | all-caps labels, captions, menu text, status bar |
| `MonaspaceArgon-Regular.otf`   | body | tooltip / dialog prose |

## License

Monaspace is released under the **SIL Open Font License, Version 1.1**
(SIL OFL 1.1). Upstream source: https://github.com/githubnext/monaspace

The OFL permits redistribution and embedding in applications without royalty.
Full license text: https://openfontlicense.org/open-font-license-official-text/

If you bump to a newer Monaspace release, pull the OTFs from the release zip
at https://github.com/githubnext/monaspace/releases and drop them in this
directory — `flashback_sampler/app/theme.py::load_fonts()` picks them up on
app start by family name, no further code changes needed.

## Fallback

If this directory is empty or the files are missing, the app falls back to
`Consolas` (Windows), `SF Mono` (macOS), `DejaVu Sans Mono` (Linux), then
`Courier New`. The app will still run; only typography will be less
distinctive.
