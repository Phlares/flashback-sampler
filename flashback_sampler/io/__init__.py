"""Platform-specific audio I/O backends.

flashback_sampler/core/ is pure Python + numpy and has no platform
dependencies. Anything that touches Windows-specific APIs, macOS
AudioTap, Linux PipeWire, etc. lives here so core tests can run
headless on any OS.

Per-process WASAPI loopback runs on the Zig core (`core/native.py`,
`core/native_capture.py`) — see `core/WasapiBackend.zig`. This package
is currently empty; it stays as the seam for future platform-specific
I/O that isn't a Zig-core concern.
"""
