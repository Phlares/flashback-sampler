from flashback_sampler.platform import capabilities as cap


def test_current_os_is_one_of_three():
    assert cap.current_os() in (cap.WINDOWS, cap.MACOS, cap.LINUX)


def test_loopback_supported_matches_windows():
    # Loopback is the Windows-only seam today.
    assert cap.loopback_supported() == (cap.current_os() == cap.WINDOWS)


def test_tray_supported_returns_bool():
    # Value depends on the display environment; just confirm the contract.
    assert isinstance(cap.tray_supported(), bool)
