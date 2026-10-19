"""Readers for the `.alc` sidecars the alc and drag-export tests check."""

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from pathlib import Path


def read_clip(path: Path) -> ET.Element:
    """The first `AudioClip` element of a gzipped `.alc` file."""
    with gzip.open(path, "rb") as fh:
        return next(ET.fromstring(fh.read()).iter("AudioClip"))


def clip_value(clip: ET.Element, path: str, attr: str = "Value") -> str:
    el = clip.find(path)
    assert el is not None, path
    value = el.get(attr)
    assert value is not None, (path, attr)
    return value
