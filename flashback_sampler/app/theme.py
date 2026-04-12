"""
Erebus palette and Qt stylesheet helpers.

Locked in by the frontend-design skill pass — see the plan addendum for
full rationale and component specs. Tokens here are the canonical source;
widgets import from this module rather than hard-coding hex values.
"""

from __future__ import annotations

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
    specific styling lives in the widgets themselves (paintEvent) and
    gets layered in across M5–M8.
    """
    t = EREBUS
    return f"""
    QWidget {{
        background-color: {t['chassis']};
        color: {t['cream']};
        font-family: "Consolas", "Cascadia Mono", "Courier New", monospace;
        font-size: 10pt;
    }}
    QMainWindow {{
        background-color: {t['chassis']};
    }}
    QLabel {{
        background-color: transparent;
        color: {t['cream']};
    }}
    QLabel[role="label"] {{
        color: {t['bone']};
        letter-spacing: 1px;
        text-transform: uppercase;
        font-size: 8pt;
    }}
    QLabel[role="readout"] {{
        color: {t['cream']};
        font-size: 24pt;
        font-weight: bold;
    }}
    QPushButton {{
        background-color: {t['plate']};
        color: {t['cream']};
        border: 1px solid {t['hairline']};
        border-radius: 6px;
        padding: 10px 18px;
        min-height: 32px;
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
    QPushButton[variant="primary"] {{
        color: {t['ember']};
        border-bottom: 2px solid {t['ember']};
    }}
    QPushButton[variant="primary"]:hover {{
        border-bottom: 2px solid {t['ember_hot']};
    }}
    QPushButton[variant="primary"]:pressed {{
        border-bottom: 2px solid {t['ember_deep']};
    }}
    QListWidget {{
        background-color: {t['plate']};
        border: 1px solid {t['hairline']};
        border-radius: 8px;
        color: {t['cream']};
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
        font-size: 9pt;
    }}
    """
