# Contributing

flashback-sampler is a personal project. I work on it in my free time and I review every change myself. Read this page before you open anything.

## The one rule

Understand the code you submit. If you cannot explain what a line does and why it is there, do not send it. This applies with or without AI tools; see the [AI policy](AI_POLICY.md). Plausible code that nobody understands is worse than no code.

## How to contribute right now

Issues are the way in.

- **Bug:** use the bug form. It asks for the steps, the expected and actual result, and your setup.
- **Idea:** use the idea form. Say what you cannot do today, not only the fix you have in mind.
- **Security problem:** do not open an issue. Use "Report a vulnerability" on the Security tab. See [SECURITY.md](SECURITY.md).

Pull requests are invite-only for now. When I accept an issue (label `accepted`), I either fix it or invite you to. An `accepted` issue is ready to work on: the problem is understood and the approach is agreed.

If an open issue already matches yours, add a 👍 reaction instead of a "+1" comment.

## Bug reports

State the problem in the first sentence. Then:

- Steps to reproduce.
- What you expected.
- What happened instead.
- Windows version, audio device, sample rate, and the app version (the release tag).

Silence from a capture source is usually the source, not the app. Check that the app you are recording renders locally (Spotify Connect and casting deliver silence to loopback).

## If I invite you to send a pull request

- One issue per PR. Keep it small.
- Add a failing test first, then make it pass. Every fix carries a test that fails without it.
- Install the dev tools: `uv sync --extra dev --all-groups`, then `uv run prek install` for the commit hooks.
- Run before you push:

```powershell
zig build --build-file core/build.zig -Doptimize=ReleaseSafe
zig build --build-file core/build.zig test
uv run pytest tests/unit -q
uv run pyright
uv run prek run --all-files --hook-stage manual
```

- Branches target `main`. Releases come from `v*` tags.
- Commit messages: first line states the change. Body says why, not what.
- Fill in the pull request template.

## The engine (`core/`)

- Zero external Zig dependencies. The Zig version is pinned in `core/build.zig.zon`; bumps are their own PR.
- `Ring.write` and every audio-thread loop have no lock, no allocation, and no failure path. A change that breaks that is wrong regardless of what it fixes.
- ReleaseSafe ships. Bounds checks stay on.
- Comments explain constraints the code cannot show. They are not noise; do not strip them.
- Each engine PR description carries a short "Zig concepts in this PR" section.

Standing decisions live in [`docs/decisions/`](docs/decisions/). Read them before you propose a change to how things fit together.

## Dependencies

New dependencies need a license on this list: MIT, MIT-0, BSD-2-Clause, BSD-3-Clause, 0BSD, ISC, Apache-2.0, PSF-2.0, Zlib, CC0-1.0, Unlicense. Anything else needs a pinned `name==version` entry in `[tool.licenses.exceptions]` in `pyproject.toml` and a decision record in `docs/decisions/` that says why. The CI `licenses` job enforces this. PySide6 (LGPL-3.0) has one: [0012](docs/decisions/0012-pyside6-lgpl.md).

## Style

Short sentences. One fact per sentence. Plain words. No sales tone. Code comments carry constraints, not narration.

## License of contributions

By contributing, you agree that your contribution is licensed under the MIT license of this project.

## Conduct

Everyone in this project follows the [code of conduct](CODE_OF_CONDUCT.md).
