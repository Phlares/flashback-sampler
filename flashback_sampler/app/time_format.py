"""
Shared time-formatting helpers.

The app displays three kinds of timestamps:

- Position readouts (playhead, buffered/capacity, selection length):
  always centisecond precision, M:SS.cc format. Used on every label
  that could reference a sub-second position.
- Timeline tick labels: condensed "0.5", "1.3", "M:SS" — lives in
  waveform_view._format_timeline_label, separate because tick labels
  compete for pixel space and can't afford .cc everywhere.
- Preset durations: show as M:SS.cc for consistency with positions,
  even though they're always whole seconds (so the ".00" is stable).

This module is the only place `format_time_cs` lives — widgets and
the main window all import from here so format drift is impossible.
"""

from __future__ import annotations


def format_time_cs(seconds: float) -> str:
    """
    Format `seconds` as `M:SS.cc` (centisecond precision).

    Examples:
        0.0     -> "0:00.00"
        0.5     -> "0:00.50"
        1.234   -> "0:01.23"
        59.999  -> "1:00.00"    (rounds cleanly to next minute)
        60.0    -> "1:00.00"
        125.678 -> "2:05.68"
        900.0   -> "15:00.00"

    Negative inputs are clamped to 0.
    """
    s = max(0.0, float(seconds))
    # Round to 10 ms first so "59.999" becomes "1:00.00" not "0:60.00"
    total_cs = int(round(s * 100))
    m = total_cs // 6000
    rem_cs = total_cs - m * 6000
    sec = rem_cs // 100
    cs = rem_cs - sec * 100
    return f"{m:d}:{sec:02d}.{cs:02d}"


def format_time_signed_cs(seconds: float) -> str:
    """
    Like format_time_cs but prefixes a minus sign for negative inputs.
    Used for rotary hub readouts where the user sees "-2:17.50" as
    "anchor is two minutes, seventeen-and-a-half seconds ago."
    """
    if seconds < 0:
        return "-" + format_time_cs(-seconds)
    return format_time_cs(seconds)
