## Change

<!-- One or two sentences: what changes and why. Link the issue: Closes #N. -->

## Checks

- [ ] A test fails without this change and passes with it.
- [ ] `zig build test`, `uv run pytest tests/unit`, `uv run pyright` and `uv run prek run --all-files --hook-stage manual` pass.
- [ ] Engine changes keep the audio thread free of locks, allocation and failure paths.
- [ ] New dependencies have an allowed license (see CONTRIBUTING.md), or a decision record.

## AI use

<!-- Tool and extent, or "none". See AI_POLICY.md. -->
