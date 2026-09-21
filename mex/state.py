"""Shape of state/position.json, and the one-way migration into it.

v1 (single symbol) kept the state machine's three fields at the top level:

    {"last_bar": ..., "position": ..., "pending": ..., "sent_ids": [...], ...}

v2 nests them per symbol, because four symbols each run their own independent
state machine and must never share a `last_bar`:

    {"schema": 2,
     "symbols": {"ETHUSDT": {"last_bar":…, "position":…, "pending":…}, …},
     "sent_ids": [...], "outbox": [...], "last_heartbeat_date": …}

Delivery stays global on purpose. The outbox is one queue to Telegram and
sent_ids is one record of what reached it; splitting them per symbol would make
_flush() have to reason about ordering across four lists for no benefit.

WHY THE DEDUP KEY HAD TO CHANGE
strategy._sid() builds a signal id from the bar and the side only -- no symbol.
That was unambiguous while the repo ran one instrument. It is not now: across
ETH/DOGE/XRP/SOL in the 2024-2026 backtest, 148 of 439 signals shared an id with
another symbol (20240325T1200-L belonged to DOGE, XRP and SOL at once). With the
old key, `SIGNAL:20240325T1200-L`, the first symbol's message would mark the id
delivered and the other two would be dropped as duplicates -- silently, with no
error anywhere. So the key now carries the symbol: `SIGNAL:DOGEUSDT:2024…-L`.

That rename is exactly why migration must rewrite the history too. sent_ids and
the outbox hold v1 keys; leaving them alone would make every already-delivered
message look unknown, and the next run would send them all again.
"""
from . import compat  # noqa: F401

SCHEMA = 2
SLOT = ("last_bar", "position", "pending")


def dedup_key(kind: str, symbol: str, signal_id: str) -> str:
    return f"{kind}:{symbol}:{signal_id}"


def _requalify(key: str, symbol: str) -> str:
    """Turn a v1 key (KIND:id) into a v2 key (KIND:SYMBOL:id). Idempotent."""
    if key.count(":") >= 2:
        return key                      # already carries a symbol
    kind, _, sid = key.partition(":")
    return dedup_key(kind, symbol, sid) if sid else key


def is_v2(st: dict) -> bool:
    return bool(st) and st.get("schema") == SCHEMA and "symbols" in st


def migrate(st: dict, primary: str) -> dict:
    """v1 -> v2. Idempotent, and never invents state that was not there.

    An empty dict is left empty so the caller's bootstrap path still recognises
    a first-ever run; migrating it into {"symbols": {}} would look like a repo
    that had already started and stayed flat.
    """
    if not st or is_v2(st):
        return st

    out = {k: v for k, v in st.items() if k not in SLOT}
    out["schema"] = SCHEMA
    out["symbols"] = dict(st.get("symbols") or {})

    # Only claim a slot for the primary symbol if v1 actually held one. A state
    # file with no last_bar never got past bootstrap, and fabricating a slot
    # from it would hand the state machine a None it does not expect.
    if st.get("last_bar") is not None:
        out["symbols"][primary] = {k: st.get(k) for k in SLOT}

    out["sent_ids"] = [_requalify(k, primary) for k in (st.get("sent_ids") or [])]
    box = []
    for m in (st.get("outbox") or []):
        m = dict(m)
        if "key" in m:
            m["key"] = _requalify(m["key"], primary)
        m.setdefault("symbol", primary)
        box.append(m)
    out["outbox"] = box
    return out


def slot(st: dict, symbol: str) -> dict:
    """That symbol's state machine fields, creating an empty slot if needed."""
    return st.setdefault("symbols", {}).setdefault(
        symbol, {"last_bar": None, "position": None, "pending": None})


def open_positions(st: dict) -> dict:
    """{symbol: position dict} for every symbol currently holding one."""
    return {s: v["position"] for s, v in (st.get("symbols") or {}).items()
            if v.get("position")}
