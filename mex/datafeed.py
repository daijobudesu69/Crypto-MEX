"""Live 4H OHLCV for ETHUSDT perpetual, from sources reachable on GitHub Actions.

Binance's own trading API (fapi.binance.com) answers HTTP 451 from GitHub-hosted
runners -- they sit on US IP ranges that Binance geo-blocks -- and times out from
the author's home ISP. Two sources are reachable from both places:

  1. data-api.binance.vision  -- Binance's public SPOT mirror. Measured against
     the Binance USD-M perp archive over 3636 bars (Jan 2025 - Aug 2026) it
     reproduces 110 of 115 long signals (96%), median close error 0.046%,
     median ATR error 1.91%.
  2. api.gateio.ws            -- Gate.io ETH_USDT perp. Better prices (0.008%
     close, 0.88% ATR) but only 102 of 115 signals (89%).

Primary is the Binance mirror because signal agreement matters more than price
precision here; Gate.io is the failover. Whichever answered is recorded on every
row we log, because this substitution is a real source of tracking error between
the forward test and the backtest and must stay visible.
"""
from . import compat  # noqa: F401
import time
from dataclasses import dataclass

import numpy as np
import pandas as pd
import requests

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
SYMBOL = "ETHUSDT"
GATE_CONTRACT = "ETH_USDT"
INTERVAL = "4h"
BAR = pd.Timedelta(INTERVAL)

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


def _get(url, params, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=UA)
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


def _from_binance_spot(symbol=SYMBOL, interval=INTERVAL, limit=1000):
    raw = _get(BINANCE_SPOT, dict(symbol=symbol, interval=interval, limit=limit))
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


def _from_gate(contract=GATE_CONTRACT, interval=INTERVAL, limit=1000):
    raw = _get(GATE_FUTURES, dict(contract=contract, interval=interval, limit=limit))
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


SOURCES = [("binance_spot_mirror", _from_binance_spot), ("gate_io_perp", _from_gate)]


def fetch(limit: int = 1000, prefer: str | None = None) -> Feed:
    """Fetch 4H bars, dropping the still-forming last bar. Falls back in order."""
    order = SOURCES
    if prefer:
        order = sorted(SOURCES, key=lambda s: s[0] != prefer)
    errors = []
    for name, fn in order:
        try:
            df = fn(limit=limit)
            df = drop_unclosed(df)
            sanity_check(df)
            return Feed(df=df, source=name, fetched_at=pd.Timestamp.now(tz="UTC"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    raise RuntimeError("all data sources failed -> " + " | ".join(errors))


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
