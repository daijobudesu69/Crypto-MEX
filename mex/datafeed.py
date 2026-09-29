"""Live 4H OHLCV for the forward-test perps, from sources reachable on GitHub Actions.

Since mex-fwd-2.2.0 the primary source is Hyperliquid's own perp candles: the
13-coin universe was chosen from a backtest on exactly that data
(backtest/hyperliquid/REPORT.md), and orders are meant to be placed there, so
the forward test now watches the same bars it will trade. Two failovers remain:

  1. api.hyperliquid.xyz     -- Hyperliquid perp, POST /info candleSnapshot.
  2. api.gateio.ws           -- Gate.io USDT perp.
  3. data-api.binance.vision -- Binance SPOT mirror (not listed for every coin).

Binance's trading API (fapi.binance.com) is still unusable: HTTP 451 from
GitHub-hosted runners, timeouts from the author's ISP.

Venues disagree on signals, and the difference is almost entirely VOLUME, not
price: on 4,997 identical bars Hyperliquid and Binance perp shared only 64-69%
of signals while closes differed by ~0.03% (backtest/h2h/REPORT.md). A failover
therefore changes which signals fire. Whichever source answered is recorded on
every logged row so those rows can be separated out later.
"""
from . import compat  # noqa: F401
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests

HYPERLIQUID = "https://api.hyperliquid.xyz/info"
BINANCE_SPOT = "https://data-api.binance.vision/api/v3/klines"
GATE_FUTURES = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"

UA = {"User-Agent": "Crypto-MEX-forward-test/1.0 (+github.com/daijobudesu69/Crypto-MEX)"}

# The instrument this repo is locked to. These are the single source of truth:
# every label written to a CSV or a Telegram message is read from here, so the
# label can never disagree with the data that was actually fetched.
#
# config.yaml used to carry `symbol` and `timeframe`, but fetch() took neither --
# setting symbol: BTCUSDT there relabelled every row while still downloading ETH
# (verified: BTC 77,833 vs the 2,391 actually returned). Keys that look like
# controls but are wired to nothing are worse than no keys at all, so they were
# removed from config.yaml and config.load() now rejects them outright.
# SYMBOL stays the primary instrument -- the one the strategy was validated on,
# and the one a caller gets when it does not ask for a specific symbol.
#
# INSTRUMENTS is the forward-test universe: per symbol, the ticker on each
# source and a SCALE that converts that source into one common unit. Prices are
# multiplied by it and volume divided, so every source reports the same price
# for the same coin. Without it a failover mid-trade is a disaster, not a
# tracking error: Hyperliquid quotes SHIB per 1000 coins (kSHIB ~0.006) and
# Gate.io per coin (~0.000006), so the first Gate.io bar would sit 1000x below
# the trailing stop and close the position instantly. The volume filter is a
# ratio to its own average, so rescaling volume cannot change a signal.
#
# The 13 coins are the "kuat" and "lumayan" groups of the 30-coin Hyperliquid
# backtest (top market cap ex BTC/stablecoin), cut off at NEAR. MNT and HYPE
# have no usable Binance spot history, so they have one failover instead of two.
SYMBOL = "ETHUSDT"
INSTRUMENTS = {
    # symbol          hyperliquid      gate.io perp          binance spot
    "ETHUSDT":      {"hyperliquid": ("ETH", 1), "gate_io_perp": ("ETH_USDT", 1),
                     "binance_spot_mirror": ("ETHUSDT", 1)},
    "DOGEUSDT":     {"hyperliquid": ("DOGE", 1), "gate_io_perp": ("DOGE_USDT", 1),
                     "binance_spot_mirror": ("DOGEUSDT", 1)},
    "XRPUSDT":      {"hyperliquid": ("XRP", 1), "gate_io_perp": ("XRP_USDT", 1),
                     "binance_spot_mirror": ("XRPUSDT", 1)},
    "SOLUSDT":      {"hyperliquid": ("SOL", 1), "gate_io_perp": ("SOL_USDT", 1),
                     "binance_spot_mirror": ("SOLUSDT", 1)},
    # HYPEUSDT on Binance spot had 22 bars on 2026-09-28 -- far short of the
    # 300-bar warmup -- so it would only ever fail sanity_check().
    "HYPEUSDT":     {"hyperliquid": ("HYPE", 1), "gate_io_perp": ("HYPE_USDT", 1)},
    "TAOUSDT":      {"hyperliquid": ("TAO", 1), "gate_io_perp": ("TAO_USDT", 1),
                     "binance_spot_mirror": ("TAOUSDT", 1)},
    "MNTUSDT":      {"hyperliquid": ("MNT", 1), "gate_io_perp": ("MNT_USDT", 1)},
    "SUIUSDT":      {"hyperliquid": ("SUI", 1), "gate_io_perp": ("SUI_USDT", 1),
                     "binance_spot_mirror": ("SUIUSDT", 1)},
    "1000SHIBUSDT": {"hyperliquid": ("kSHIB", 1), "gate_io_perp": ("SHIB_USDT", 1000),
                     "binance_spot_mirror": ("SHIBUSDT", 1000)},
    "DOTUSDT":      {"hyperliquid": ("DOT", 1), "gate_io_perp": ("DOT_USDT", 1),
                     "binance_spot_mirror": ("DOTUSDT", 1)},
    "ENAUSDT":      {"hyperliquid": ("ENA", 1), "gate_io_perp": ("ENA_USDT", 1),
                     "binance_spot_mirror": ("ENAUSDT", 1)},
    "LINKUSDT":     {"hyperliquid": ("LINK", 1), "gate_io_perp": ("LINK_USDT", 1),
                     "binance_spot_mirror": ("LINKUSDT", 1)},
    "NEARUSDT":     {"hyperliquid": ("NEAR", 1), "gate_io_perp": ("NEAR_USDT", 1),
                     "binance_spot_mirror": ("NEARUSDT", 1)},
}
SYMBOLS = list(INSTRUMENTS)
GATE = {s: v["gate_io_perp"][0] for s, v in INSTRUMENTS.items() if "gate_io_perp" in v}
INTERVAL = "4h"
BAR = pd.Timedelta(INTERVAL)

# Every symbol needs the primary AND at least one failover, or it silently has
# no fallback and only finds out when the primary goes down. Fail at import.
_missing = [s for s, v in INSTRUMENTS.items()
            if "hyperliquid" not in v or "gate_io_perp" not in v]
if _missing:
    raise RuntimeError(f"INSTRUMENTS tidak lengkap untuk {_missing}; failover tidak ada")

# Nothing is gained by retrying these: a geo-block, a bad symbol or a malformed
# request answers the same way every time, and retrying 429 is how a client
# earns Binance's 418 ban. Anything else -- timeouts, resets, 5xx -- is worth
# another attempt.
NO_RETRY_STATUS = frozenset({400, 401, 403, 404, 418, 429, 451})


@dataclass
class Feed:
    df: pd.DataFrame
    source: str
    fetched_at: pd.Timestamp
    # Carried so a caller can never attribute one symbol's bars to another --
    # the exact confusion that made `symbol` in config.yaml dangerous.
    symbol: str = SYMBOL


def _get(url, params, timeout=30, retries=3, body=None):
    """GET `params`, or POST `body` as JSON when one is given (Hyperliquid)."""
    last = None
    for i in range(retries):
        try:
            if body is None:
                r = requests.get(url, params=params, timeout=timeout, headers=UA)
            else:
                r = requests.post(url, json=body, timeout=timeout, headers=UA)
            if r.status_code in NO_RETRY_STATUS:
                raise RuntimeError(f"HTTP {r.status_code} (tidak diulang)")
            r.raise_for_status()
            return r.json()
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def _from_hyperliquid(ticker, interval=INTERVAL, limit=1000):
    end = pd.Timestamp.now(tz="UTC")
    start = end - (limit + 1) * BAR
    raw = _get(HYPERLIQUID, None, body={
        "type": "candleSnapshot",
        "req": {"coin": ticker, "interval": interval,
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000)}})
    if not raw:
        raise RuntimeError("hyperliquid returned no rows")
    df = pd.DataFrame(raw)
    out = pd.DataFrame({
        "ts": pd.to_datetime(df["t"].astype("int64"), unit="ms", utc=True),
        "open": df["o"].astype(float), "high": df["h"].astype(float),
        "low": df["l"].astype(float), "close": df["c"].astype(float),
        "volume": df["v"].astype(float),
    })
    return out.sort_values("ts").reset_index(drop=True)


def _from_binance_spot(ticker, interval=INTERVAL, limit=1000):
    raw = _get(BINANCE_SPOT, dict(symbol=ticker, interval=interval, limit=limit))
    if not raw:
        raise RuntimeError("binance spot mirror returned no rows")
    df = pd.DataFrame(raw, columns=[
        "ot", "open", "high", "low", "close", "volume",
        "ct", "qv", "n", "tb", "tq", "ig"])
    out = pd.DataFrame({
        "ts": pd.to_datetime(df["ot"].astype("int64"), unit="ms", utc=True),
        **{c: df[c].astype(float) for c in ("open", "high", "low", "close", "volume")},
    })
    return out.sort_values("ts").reset_index(drop=True)


def _from_gate(ticker, interval=INTERVAL, limit=1000):
    raw = _get(GATE_FUTURES, dict(contract=ticker, interval=interval, limit=limit))
    if not raw:
        raise RuntimeError("gate.io returned no rows")
    df = pd.DataFrame(raw)
    out = pd.DataFrame({
        "ts": pd.to_datetime(df["t"].astype("int64"), unit="s", utc=True),
        "open": df["o"].astype(float), "high": df["h"].astype(float),
        "low": df["l"].astype(float), "close": df["c"].astype(float),
        "volume": df["v"].astype(float),
    })
    return out.sort_values("ts").reset_index(drop=True)


SOURCES = [("hyperliquid", _from_hyperliquid), ("gate_io_perp", _from_gate),
           ("binance_spot_mirror", _from_binance_spot)]


def rescale(df: pd.DataFrame, scale: float) -> pd.DataFrame:
    """Convert one source's quote into the symbol's common unit (see INSTRUMENTS)."""
    if scale == 1:
        return df
    df = df.copy()
    df[["open", "high", "low", "close"]] *= scale
    df["volume"] /= scale
    return df


def fetch(limit: int = 1000, prefer: str | None = None,
          symbol: str | None = None) -> Feed:
    """Fetch 4H bars for `symbol`, dropping the still-forming last bar.

    `symbol` defaults to SYMBOL, so every existing caller keeps its behaviour
    unchanged. Sources are tried in order -- `prefer` first -- skipping any the
    symbol is not listed on, and the first that passes sanity_check() wins.
    """
    symbol = symbol or SYMBOL
    spec = INSTRUMENTS.get(symbol)
    if spec is None:
        raise RuntimeError(f"{symbol}: tidak terdaftar di INSTRUMENTS")
    order = SOURCES
    if prefer:
        order = sorted(SOURCES, key=lambda s: s[0] != prefer)
    errors = []
    for name, fn in order:
        if name not in spec:
            continue
        ticker, scale = spec[name]
        try:
            df = rescale(fn(ticker, limit=limit), scale)
            df = drop_unclosed(df)
            sanity_check(df)
            return Feed(df=df, source=name, fetched_at=pd.Timestamp.now(tz="UTC"),
                        symbol=symbol)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    raise RuntimeError(f"{symbol}: semua sumber data gagal -> " + " | ".join(errors))


def drop_unclosed(df: pd.DataFrame) -> pd.DataFrame:
    """Keep closed bars only: a bar opening at t closes at t + BAR."""
    now = pd.Timestamp.now(tz="UTC")
    return df[df["ts"] + BAR <= now].reset_index(drop=True)


def sanity_check(df: pd.DataFrame, min_bars: int = 300) -> None:
    """Refuse to trade off a series that is short, gappy, stale or malformed."""
    if len(df) < min_bars:
        raise RuntimeError(f"only {len(df)} bars, need >= {min_bars} for warmup")
    # Duplicates first: date_range().difference() below compares sets, so a
    # repeated timestamp hides behind a set that still looks complete while every
    # positional index in step() has already shifted by one.
    dup = df["ts"].duplicated()
    if dup.any():
        raise RuntimeError(f"{int(dup.sum())} duplicate bars, "
                           f"first at {df['ts'][dup].iloc[0]}")
    gaps = pd.date_range(df["ts"].iloc[0], df["ts"].iloc[-1], freq=BAR).difference(
        pd.DatetimeIndex(df["ts"]))
    if len(gaps):
        raise RuntimeError(f"{len(gaps)} missing bars, first at {gaps[0]}")
    age = pd.Timestamp.now(tz="UTC") - df["ts"].iloc[-1]
    if age > 3 * BAR:
        raise RuntimeError(f"stale feed: newest closed bar is {age} old")
    bad = df[(df["high"] < df["low"]) | (df["high"] < df["open"]) | (df["high"] < df["close"])
             | (df["low"] > df["open"]) | (df["low"] > df["close"])
             | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)]
    if len(bad):
        raise RuntimeError(f"{len(bad)} malformed OHLC bars, first at {bad['ts'].iloc[0]}")
    if not np.isfinite(df[["open", "high", "low", "close", "volume"]].to_numpy(float)).all():
        raise RuntimeError("non-finite values in OHLCV")
