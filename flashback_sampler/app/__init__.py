"""Qt application layer for flashback-sampler.

Everything in this package may import PySide6. The core audio logic
(`flashback_sampler.core.*`) must not — the separation lets us port the
audio core to a C++/JUCE VST without untangling Qt.
"""
