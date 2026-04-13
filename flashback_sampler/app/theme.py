"""
Erebus palette, font loading, and Qt stylesheet helpers.

Locked in by the frontend-design skill pass — see the plan addendum for
full rationale and component specs. Tokens here are the canonical source;
widgets import from this module rather than hard-coding hex values.
"""

from __future__ import annotations

from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────
# Fonts — Monaspace family (Krypton / Neon / Argon), SIL OFL 1.1
# ─────────────────────────────────────────────────────────────────────────
#
# All type in the Erebus chassis is monospaced. Discipline rule: no
# sans-serif. If the Monaspace OTFs are bundled in ./fonts/ they load
# and the stylesheet references them by family name. If they're absent
# the app falls back to a platform-available mono (Consolas on Windows,
# SF Mono on macOS, DejaVu Sans Mono on Linux) so the build never fails.

FONTS_DIR = Path(__file__).resolve().parent / "fonts"

# Family names we WANT (as they appear after QFontDatabase loads them)
MONASPACE_DISPLAY = "Monaspace Krypton"  # hub readouts, big numbers
MONASPACE_LABEL = "Monaspace Neon"       # UPPER labels, captions
MONASPACE_BODY = "Monaspace Argon"        # comfortable prose

# Fallback stack — matches the spirit (mono, neutral) on each platform
MONO_FALLBACK = '"Consolas", "SF Mono", "DejaVu Sans Mono", "Courier New", monospace'

# Resolved at runtime in load_fonts(); initially the fallbacks so
# base_stylesheet() works even before load_fonts() is called.
_resolved_display: str = f'{MONO_FALLBACK}'
_resolved_label: str = f'{MONO_FALLBACK}'
_resolved_body: str = f'{MONO_FALLBACK}'


def load_fonts(app=None) -> dict[str, str]:
    """
    Register any bundled Monaspace font files with QFontDatabase and
    resolve the three family names we reference from QSS. Safe to call
    multiple times and safe to call if the font files are missing —
    returns the best-available resolution every time.

    Pass the QApplication instance only if you want to log via
    print(); otherwise the call is silent and idempotent.
    """
    global _resolved_display, _resolved_label, _resolved_body

    try:
        from PySide6.QtGui import QFontDatabase
    except ImportError:  # pragma: no cover
        return {
            "display": _resolved_display,
            "label": _resolved_label,
            "body": _resolved_body,
        }

    loaded_families: set[str] = set()
    if FONTS_DIR.is_dir():
        for p in sorted(FONTS_DIR.glob("*.otf")) + sorted(FONTS_DIR.glob("*.ttf")):
            font_id = QFontDatabase.addApplicationFont(str(p))
            if font_id >= 0:
                for fam in QFontDatabase.applicationFontFamilies(font_id):
                    loaded_families.add(fam)

    def _pick(preferred: str) -> str:
        if preferred in loaded_families:
            return f'"{preferred}", {MONO_FALLBACK}'
        return MONO_FALLBACK

    _resolved_display = _pick(MONASPACE_DISPLAY)
    _resolved_label = _pick(MONASPACE_LABEL)
    _resolved_body = _pick(MONASPACE_BODY)

    return {
        "display": _resolved_display,
        "label": _resolved_label,
        "body": _resolved_body,
    }


def font_family(role: str) -> str:
    """
    Return the CSS/QSS font-family stack for one of the three roles.
    Valid roles: 'display', 'label', 'body'. Unknown roles fall through
    to the body stack.
    """
    if role == "display":
        return _resolved_display
    if role == "label":
        return _resolved_label
    return _resolved_body


EREBUS: dict[str, str] = {
    # Surfaces — 3 tiers of elevation + 1 void
    "void":            "#08070a",
    "chassis":         "#0e0d10",
    "plate":           "#161418",
    "ridge":           "#1e1b20",

    # Text — warm cream hierarchy
    "cream":           "#f2eddf",
    "bone":            "#a8a398",
    "ash":             "#5a5652",

    # Structural hairlines (rendered via rgba — Qt supports it in QSS)
    "hairline_faint":  "rgba(242, 237, 223, 0.06)",
    "hairline":        "rgba(242, 237, 223, 0.12)",
    "hairline_strong": "rgba(242, 237, 223, 0.22)",

    # The accent — ONE color, used sparingly
    "ember":           "#ff5a1f",
    "ember_hot":       "#ff8a3d",
    "ember_deep":      "#c73a0d",
    "rec":             "#ff2a1c",

    # Waveform ink — monochrome, never orange
    "signal":          "#e8e0d2",
    "signal_dim":      "#8a857a",
    "signal_rms":      "#b0aa9a",

    # VU meter thermal (the one place thermal lives)
    "meter_floor":     "#241510",
    "meter_low":       "#5f2812",
    "meter_mid":       "#c04614",
    "meter_hot":       "#ff7a1e",
    "meter_peak":      "#ffc400",
    "meter_clip":      "#ff2a1c",
}


def base_stylesheet() -> str:
    """
    Minimal Qt stylesheet applied to QApplication. Establishes chassis
    background, cream text, and secondary widget surfaces. Component-
    specific styling lives in the widgets themselves (paintEvent).

    Font families reference the resolved Monaspace stacks; if Monaspace
    is not bundled the stacks fall through to the platform mono default.
    TactileButton widgets do their own custom paint and ignore most of
    the QPushButton rules here — the rules remain for QMessageBox /
    QDialog buttons that use stock QPushButton.
    """
    t = EREBUS
    body = _resolved_body
    label = _resolved_label
    display = _resolved_display
    return f"""
    QWidget {{
        background-color: {t['chassis']};
        color: {t['cream']};
        font-family: {body};
        font-size: 10pt;
    }}
    QMainWindow {{
        background-color: {t['chassis']};
    }}
    QMenuBar {{
        background-color: {t['chassis']};
        color: {t['cream']};
        font-family: {label};
        font-size: 9pt;
        padding: 2px 6px;
        border-bottom: 1px solid {t['hairline_faint']};
    }}
    QMenuBar::item {{
        background: transparent;
        padding: 4px 10px;
    }}
    QMenuBar::item:selected {{
        background-color: {t['plate']};
        color: {t['ember']};
    }}
    QMenu {{
        background-color: {t['plate']};
        color: {t['cream']};
        font-family: {label};
        font-size: 9pt;
        border: 1px solid {t['hairline']};
        padding: 4px 0;
    }}
    QMenu::item {{
        padding: 6px 20px 6px 28px;
    }}
    QMenu::item:selected {{
        background-color: {t['ridge']};
        color: {t['ember']};
    }}
    QMenu::separator {{
        height: 1px;
        background: {t['hairline_faint']};
        margin: 4px 10px;
    }}
    QLabel {{
        background-color: transparent;
        color: {t['cream']};
        font-family: {body};
    }}
    QLabel[role="label"] {{
        color: {t['bone']};
        font-family: {label};
        letter-spacing: 2px;
        text-transform: uppercase;
        font-size: 8pt;
    }}
    QLabel[role="readout"] {{
        color: {t['cream']};
        font-family: {display};
        font-size: 22pt;
        font-weight: 700;
        letter-spacing: -1px;
    }}
    QPushButton {{
        background-color: {t['plate']};
        color: {t['cream']};
        border: 1px solid {t['hairline']};
        border-radius: 6px;
        padding: 10px 18px;
        min-height: 32px;
        font-family: {label};
        font-size: 9pt;
    }}
    QPushButton:hover {{
        background-color: {t['ridge']};
    }}
    QPushButton:pressed {{
        background-color: {t['void']};
    }}
    QPushButton:disabled {{
        color: {t['ash']};
        background-color: {t['plate']};
    }}
    QListWidget {{
        background-color: {t['plate']};
        border: 1px solid {t['hairline']};
        border-radius: 8px;
        color: {t['cream']};
        font-family: {body};
        outline: none;
    }}
    QListWidget::item {{
        padding: 6px 10px;
        border: none;
    }}
    QListWidget::item:selected {{
        background-color: {t['ridge']};
        color: {t['ember']};
    }}
    QStatusBar {{
        background-color: {t['chassis']};
        color: {t['bone']};
        border-top: 1px solid {t['hairline_faint']};
        font-family: {label};
        font-size: 8pt;
    }}
    """
