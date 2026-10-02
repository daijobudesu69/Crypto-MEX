"""Runtime configuration, editable without touching code."""
from . import compat  # noqa: F401
import os

import yaml

from . import datafeed
from .strategy import Params

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = os.path.join(ROOT, "config.yaml")

# 2.0.0: state/position.json moved from one instrument to four (schema 2), and
# the Telegram dedup key gained a symbol. Both are breaking changes to the state
# file, so the version has to move with them -- every logged row carries it, and
# that is what lets rows written before and after the change be told apart.
# 2.1.0: behaviour fixes only, no state-schema change -- last_bar is now
# forward-only, the Sheets mirror matches columns by name, and a signal dropped
# from the outbox silences its own entry/exit. Rows logged before and after are
# told apart by this string, same as every previous change.
# 2.2.0: universe 4 -> 13 symbols and primary source Binance spot mirror ->
# Hyperliquid perp. Strategy rules untouched; state schema unchanged (new
# symbols bootstrap flat into their own slots).
# 2.3.0: universe 13 -> 10 symbols after the OOS tests (backtest/oos/): NEAR,
# DOT, LINK, 1000SHIB out, XLM in. Strategy rules untouched; dropped symbols
# had no open or pending trade; XLM bootstraps flat in its own slot.
ENGINE_VERSION = "mex-fwd-2.3.0"

TOP_LEVEL = {"prefer_source", "strategy", "execution"}
EXECUTION_KEYS = {"venue", "margin_mode", "leverage", "capital_usd",
                  "agent_secret", "agent_valid_until", "account_address", "agent_address",
                  "max_drawdown_pct"}

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

    ex = cfg.get("execution") or {}
    unknown = set(ex) - EXECUTION_KEYS
    if unknown:
        raise ValueError(f"config.yaml: unknown execution keys {sorted(unknown)}")
    if ex:
        from .execution import HL_MAX_LEVERAGE
        if ex.get("venue") != "hyperliquid" or ex.get("margin_mode") != "isolated":
            raise ValueError("config.yaml: execution hanya mendukung "
                             "venue: hyperliquid, margin_mode: isolated")
        lev, cap = ex.get("leverage"), ex.get("capital_usd")
        if not isinstance(lev, int) or lev < 1:
            raise ValueError(f"config.yaml: execution.leverage harus bilangan bulat >= 1, bukan {lev!r}")
        if not isinstance(cap, (int, float)) or cap <= 0:
            raise ValueError(f"config.yaml: execution.capital_usd harus > 0, bukan {cap!r}")
        dd = ex.get("max_drawdown_pct")
        if dd is not None and (isinstance(dd, bool) or not isinstance(dd, (int, float))
                               or not 0 < dd < 100):
            raise ValueError(f"config.yaml: execution.max_drawdown_pct harus di antara 0 "
                             f"dan 100, bukan {dd!r}")
        # One coin with a lower cap would otherwise fail only when it signals.
        too_high = {s: m for s, m in HL_MAX_LEVERAGE.items()
                    if s in datafeed.SYMBOLS and lev > m}
        if too_high:
            raise ValueError(f"config.yaml: leverage {lev}x melebihi maksimum "
                             f"Hyperliquid untuk {too_high}")
        missing = [s for s in datafeed.SYMBOLS if s not in HL_MAX_LEVERAGE]
        if missing:
            raise ValueError(f"execution.HL_MAX_LEVERAGE belum memetakan {missing}")
        import re as _re
        for key in ("account_address", "agent_address"):
            if key in ex and not _re.fullmatch(r"0x[0-9a-fA-F]{40}", str(ex[key])):
                raise ValueError(f"config.yaml: execution.{key} bukan alamat 0x... "
                                 f"40 karakter hex: {ex[key]!r}")
        if ex.get("account_address") and ex.get("agent_address") and \
                ex["account_address"].lower() == ex["agent_address"].lower():
            raise ValueError("config.yaml: account_address dan agent_address sama -- "
                             "agent harus alamat API wallet, bukan akun utama")
        if "agent_valid_until" in ex:
            import datetime as _dt
            # YAML already parses 2027-03-28 as a date; a quoted or malformed
            # value must fail here, not silently disable the expiry reminder.
            if not isinstance(ex["agent_valid_until"], _dt.date):
                raise ValueError("config.yaml: execution.agent_valid_until harus "
                                 f"tanggal YYYY-MM-DD, bukan {ex['agent_valid_until']!r}")
    cfg["execution"] = ex or None

    cfg.setdefault("prefer_source", "hyperliquid")
    # fetch() treats an unknown name as "no preference", so a typo here used to
    # quietly move the forward test onto whichever source happened to be first.
    known = [name for name, _ in datafeed.SOURCES]
    if cfg["prefer_source"] not in known:
        raise ValueError(f"config.yaml: prefer_source '{cfg['prefer_source']}' "
                         f"tidak dikenal, pilih salah satu dari {known}")
    # Read-only, so callers have one place to ask and cannot disagree with the feed.
    # `symbol` remains the primary instrument (the one the strategy was validated
    # on); `symbols` is the full forward-test universe. Both are still owned by
    # datafeed.py -- config.yaml deliberately has no say, which is why the
    # retired `symbol` key above must stay rejected.
    cfg["symbol"] = datafeed.SYMBOL
    cfg["symbols"] = list(datafeed.SYMBOLS)
    cfg["timeframe"] = datafeed.INTERVAL
    cfg["bar"] = datafeed.BAR
    return cfg
