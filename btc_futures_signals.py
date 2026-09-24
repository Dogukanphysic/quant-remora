"""Pure, optional Futures entry profiles over the shared Bollinger diagnostics.

The default ``trend`` profile preserves the Spot interpreter's decision.
``responsive`` also admits a confirmed lower-band reclaim without requiring
the 50/200 EMA trend gate.  This module does not calculate indicators, change
position sizing or exits, or interact with an exchange.
"""
from __future__ import annotations


SIGNAL_PROFILES = frozenset({"trend", "responsive"})
_BOOLEAN_FIELDS = (
    "enter", "lower_zone_reached", "lower_touched", "upper_touched",
    "volume_confirmed", "histogram_rising", "recovery_confirmed",
    "trend_confirmed", "squeeze", "breakout", "reclaim_exit", "breakout_exit",
)
_RECLAIM_FIELDS = (
    "lower_zone_reached", "recovery_confirmed", "histogram_rising",
)


def apply_profile(band: dict, profile: str = "trend") -> dict:
    """Return a new decision, preserving every other raw diagnostic.

    Only literal booleans are accepted for supplied decision flags.  Missing
    reclaim diagnostics cannot enable the optional entry.  Malformed or
    inconsistent base decisions raise ``ValueError`` instead of being coerced
    into a trade, including when the base ``enter`` flag is already true.
    """
    if not isinstance(profile, str) or profile not in SIGNAL_PROFILES:
        raise ValueError("Signal profile must be trend or responsive")
    if not isinstance(band, dict):
        raise ValueError("Bollinger diagnostics must be a dict")
    if "enter" not in band or "entry_regime" not in band:
        raise ValueError("Bollinger decision requires enter and entry_regime")
    for field in _BOOLEAN_FIELDS:
        if field in band and type(band[field]) is not bool:
            raise ValueError(f"Bollinger {field} must be a boolean")

    regime = band["entry_regime"]
    if regime is not None and (not isinstance(regime, str) or not regime.strip()):
        raise ValueError("Bollinger entry_regime must be a nonempty string or None")
    if band["enter"] is not (regime is not None):
        raise ValueError("Bollinger enter and entry_regime disagree")

    result = dict(band, signal_profile=profile)
    if (profile == "responsive" and band["enter"] is False and
            all(band.get(field) is True for field in _RECLAIM_FIELDS)):
        result.update(enter=True, entry_regime="responsive_reclaim")
    return result
