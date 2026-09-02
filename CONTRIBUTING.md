# Contributing

flashback-sampler is a personal project. I work on it in my free time and I review every change myself. Read this page before you open anything.

## The one rule

Understand the code you submit. If you cannot explain what a line does and why it is there, do not send it. This applies with or without AI tools. Plausible code that nobody understands is worse than no code.

## Where things go

- **Issues** are for work that is ready to do: a bug with a repro, or a feature we have agreed on. Open one when you have those.
- **Discussions** are for everything before that: ideas, questions, "would you take a PR for X".
- **Pull requests** address an existing issue. A PR with no issue behind it may sit until we have talked about it, or get closed.

## Bug reports

State the problem in the first sentence. Then:

- Steps to reproduce.
- What you expected.
- What happened instead.
- Windows version, audio device, sample rate, and the app version (the release tag).

Silence from a capture source is usually the source, not the app. Check that the app you are recording renders locally (Spotify Connect and casting deliver silence to loopback).

## Pull requests

- One issue per PR. Keep it small.
- Add a failing test first, then make it pass. Every fix carries a test that fails without it.
- Run before you push:

```powershell
zig build --build-file core/build.zig -Doptimize=ReleaseSafe
zig fmt --check core/src/
zig build --build-file core/build.zig test
pytest tests/unit -q
```

- Feature branches target `dev`. `main` moves when a batch is ready.
- Commit messages: first line states the change. Body says why, not what.

## The engine (`core/`)

- Zero external Zig dependencies. The Zig version is pinned in `core/build.zig.zon`; bumps are their own PR.
- `Ring.write` and every audio-thread loop have no lock, no allocation, and no failure path. A change that breaks that is wrong regardless of what it fixes.
- ReleaseSafe ships. Bounds checks stay on.
- Comments explain constraints the code cannot show. They are not noise; do not strip them.
- Each engine PR description carries a short "Zig concepts in this PR" section.

Standing decisions live in [`docs/decisions/`](docs/decisions/). Read them before you propose a change to how things fit together.

## Style

Short sentences. One fact per sentence. Plain words. No sales tone. Code comments carry constraints, not narration.
