"""Platform-specific audio I/O backends.

flashback_sampler/core/ is pure Python + numpy and has no platform
dependencies. Anything that touches Windows-specific APIs, macOS
AudioTap, Linux PipeWire, etc. lives here so core tests can run
headless on any OS.

M12 adds `win32_process_loopback` — native per-process WASAPI
loopback via ActivateAudioInterfaceAsync + ctypes COM vtable glue.
"""
