"""Runtime configuration, editable without touching code."""
from . import compat  # noqa: F401
import os

import yaml

from .strategy import Params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = os.path.join(ROOT, "config.yaml")

ENGINE_VERSION = "mex-fwd-1.0.0"


def load(path: str = DEFAULT) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    s = cfg.get("strategy", {})
    cfg["params"] = Params(**{k: v for k, v in s.items() if k in Params.__dataclass_fields__})
    unknown = set(s) - set(Params.__dataclass_fields__)
    if unknown:
        raise ValueError(f"config.yaml: unknown strategy keys {sorted(unknown)}")
    cfg.setdefault("symbol", "ETHUSDT")
    cfg.setdefault("prefer_source", "binance_spot_mirror")
    cfg.setdefault("bootstrap_flat", True)
    return cfg
