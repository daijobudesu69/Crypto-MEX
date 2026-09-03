"""Runtime configuration, editable without touching code."""
from . import compat  # noqa: F401
import os

import yaml

from . import datafeed
from .strategy import Params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = os.path.join(ROOT, "config.yaml")

ENGINE_VERSION = "mex-fwd-1.1.0"

TOP_LEVEL = {"prefer_source", "strategy"}

# Keys that once existed here but were wired to nothing. Rejecting them by name
# means an old config.yaml fails loudly at startup instead of appearing to work:
#   symbol / timeframe  -- fetch() never took either; see mex/datafeed.py
#   bootstrap_flat      -- read into cfg and then never consulted anywhere
RETIRED = {
    "symbol": "instrumen dikunci di mex/datafeed.py (SYMBOL). Hapus baris ini.",
    "timeframe": "timeframe dikunci di mex/datafeed.py (INTERVAL). Hapus baris ini.",
    "bootstrap_flat": "bootstrap selalu flat; opsi ini tidak pernah dibaca. Hapus baris ini.",
}


def load(path: str = DEFAULT) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    for key, why in RETIRED.items():
        if key in cfg:
            raise ValueError(f"config.yaml: '{key}' sudah tidak dipakai -- {why}")
    unknown_top = set(cfg) - TOP_LEVEL
    if unknown_top:
        raise ValueError(f"config.yaml: kunci tidak dikenal {sorted(unknown_top)}")

    s = cfg.get("strategy", {})
    unknown = set(s) - set(Params.__dataclass_fields__)
    if unknown:
        raise ValueError(f"config.yaml: unknown strategy keys {sorted(unknown)}")
    cfg["params"] = Params(**s)

    cfg.setdefault("prefer_source", "binance_spot_mirror")
    # Read-only, so callers have one place to ask and cannot disagree with the feed.
    cfg["symbol"] = datafeed.SYMBOL
    cfg["timeframe"] = datafeed.INTERVAL
    cfg["bar"] = datafeed.BAR
    return cfg
