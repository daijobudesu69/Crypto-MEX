"""End-to-end property test: drive the whole pipeline through a hostile day.

test_strategy.py guards the signals and test_infra.py guards each individual
defect the audits found. Neither drives the real drivers end to end, and both
2026-09 audits found bugs that only appear when two mechanisms interact -- a feed
that rewinds while a signal is pending, a run that dies mid-batch, a message that
outlives its own signal in the outbox. Those are exactly the cases nobody writes
a targeted test for, because nobody thinks of them.

So this file does not test a behaviour. It replays many runs under conditions
deliberately worse than production and asserts, after every single run, the
properties that must hold no matter what happened:

  I1  last_bar never moves backwards for any symbol
  I2  no trade is ever filled on its own signal bar (that is lookahead)
  I3  every fill is exactly one bar after its signal
  I4  events.csv never grows a duplicate row
  I5  no ENTRY/EXIT is announced for a signal that was never announced
  I6  no message is delivered twice
  I7  no confirmation reaches the user before its own signal

The hostility is the point: the served feed randomly ends up to two bars short
so the sources disagree constantly, Telegram fails a quarter of the time, and
step() raises at random once the run is warmed up.

Offline and deterministic -- the feed comes from the committed fixture, the clock
is driven, and the RNG is seeded.
"""
import csv
import json
import os
import random
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + detail}")


HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "eth_4h_binance_perp.csv")
BASE = pd.Timestamp("2026-01-01T00:00:00Z")
NB = 560          # bars per symbol
START = 500       # first run sees this many; one more arrives each step
POLLS = 2         # the watcher checks several times per bar


def _series():
    """Four symbols carved from the committed fixture, so signals are real."""
    f = pd.read_csv(FIXTURE)
    f["ts"] = pd.to_datetime(f["ts"], utc=True)
    out = {}
    for sym, off in (("ETHUSDT", 0), ("DOGEUSDT", 700),
                     ("XRPUSDT", 1400), ("SOLUSDT", 2100)):
        d = f.iloc[off:off + NB].reset_index(drop=True).copy()
        d["ts"] = pd.date_range(BASE, periods=len(d), freq="4h")
        out[sym] = d[["ts", "open", "high", "low", "close", "volume"]]
    return out


def test_pipeline_invariants(seed=7):
    import mex.datafeed as datafeed
    from mex import notify

    rng = random.Random(seed)
    series = _series()
    bar = datafeed.BAR

    work = tempfile.mkdtemp()
    cwd = os.getcwd()
    real_now = pd.Timestamp.now
    real_fetch = datafeed.fetch
    real_send, real_conf = notify.send, notify.configured
    env = dict(os.environ)

    os.chdir(work)
    os.makedirs("state", exist_ok=True)
    clock = {"t": None}
    pd.Timestamp.now = staticmethod(lambda tz=None: clock["t"])
    cut = {"n": 0, "jitter": {}}

    def fetch(limit=1000, prefer=None, symbol=None):
        sym = symbol or datafeed.SYMBOL
        n = max(400, cut["n"] - cut["jitter"].get(sym, 0))
        return datafeed.Feed(df=series[sym].iloc[:n].reset_index(drop=True).copy(),
                             source="stub", fetched_at=clock["t"], symbol=sym)

    datafeed.fetch = fetch
    os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
    os.environ["TELEGRAM_CHAT_ID"] = "chat"
    for k in ("GSHEET_WEBHOOK_URL", "GOOGLE_SERVICE_ACCOUNT_JSON",
              "GSHEET_SPREADSHEET_ID"):
        os.environ.pop(k, None)
    notify.send = lambda text: rng.random() > 0.25

    import run_signal
    import run_heartbeat
    run_signal.datafeed = datafeed
    run_heartbeat.datafeed = datafeed
    real_flush, real_step = run_signal._flush, run_signal.step

    delivered = []

    def flush(st):
        before = set(st.get("sent_ids", []))
        out = real_flush(st)
        for k in st.get("sent_ids", []):
            if k not in before:
                delivered.append(k)
        return out

    boom = {"on": False}

    def step(f, ts, i, p, pos, pending):
        if boom["on"] and rng.random() < 0.05:
            raise KeyError("simulasi: entry bar keluar dari jendela")
        return real_step(f, ts, i, p, pos, pending)

    run_signal._flush, run_signal.step = flush, step

    bad, last_seen, runs = [], {}, 0

    def rows(path):
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def audit(tag):
        st = {}
        if os.path.exists("state/position.json"):
            st = json.load(open("state/position.json"))
        for sym, slot in (st.get("symbols") or {}).items():
            lb = slot.get("last_bar")
            if lb is None:
                continue
            if sym in last_seen and pd.Timestamp(lb) < pd.Timestamp(last_seen[sym]):
                bad.append(f"I1 {tag}: {sym} last_bar mundur "
                           f"{last_seen[sym]} -> {lb}")
            last_seen[sym] = max(last_seen.get(sym, lb), lb)

        for t in rows("state/trades.csv"):
            sb = pd.Timestamp(t["signal_bar_utc"])
            eb = pd.Timestamp(t["entry_bar_utc"])
            if eb == sb:
                bad.append(f"I2 {tag}: {t['symbol']} {t['signal_id']} "
                           f"entry di bar sinyalnya sendiri ({sb})")
            elif eb - sb != bar:
                bad.append(f"I3 {tag}: {t['symbol']} {t['signal_id']} "
                           f"jarak signal->entry {eb - sb}, bukan {bar}")

        seen_rows = set()
        for e in rows("state/events.csv"):
            k = (e["event"], e["symbol"], e["signal_id"], e["bar_time_utc"])
            if k in seen_rows:
                bad.append(f"I4 {tag}: baris ledger dobel {k}")
            seen_rows.add(k)

        first = {}
        for idx, k in enumerate(delivered):
            first.setdefault(k, idx)
        for k, idx in first.items():
            kind, _, rest = k.partition(":")
            if kind not in ("ENTRY", "EXIT"):
                continue
            sig = first.get(f"SIGNAL:{rest}")
            if sig is None:
                bad.append(f"I5 {tag}: {k} terkirim padahal sinyalnya tidak")
            elif sig > idx:
                bad.append(f"I7 {tag}: {k} sampai sebelum sinyalnya")
        if len(delivered) != len(first):
            bad.append(f"I6 {tag}: ada pesan terkirim dua kali")

    try:
        for n in range(START, NB + 1):
            cut["n"] = n
            boom["on"] = n > START + 10
            close = series["ETHUSDT"]["ts"].iloc[n - 1] + bar
            for poll in range(POLLS):
                clock["t"] = close + pd.Timedelta(minutes=3 + poll * 90)
                cut["jitter"] = {s: rng.choice([0, 0, 0, 1, 2])
                                 for s in datafeed.SYMBOLS}
                for driver in (run_signal, run_heartbeat):
                    try:
                        driver.main()
                    except Exception as e:  # noqa: BLE001
                        bad.append(f"CRASH {driver.__name__} {n}/{poll}: "
                                   f"{type(e).__name__}: {e}")
                runs += 1
                audit(f"n={n},poll={poll}")
                if bad:
                    break
            if bad:
                break
        ev, tr = rows("state/events.csv"), rows("state/trades.csv")
    finally:
        run_signal._flush, run_signal.step = real_flush, real_step
        datafeed.fetch = real_fetch
        notify.send, notify.configured = real_send, real_conf
        pd.Timestamp.now = real_now
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(env)
        shutil.rmtree(work, ignore_errors=True)

    print(f"  ({runs} run, {len(ev)} event, {len(tr)} transaksi, "
          f"{len(delivered)} pesan terkirim)")
    check("pipeline menjaga semua invarian di bawah feed mundur, "
          "Telegram gagal dan crash acak",
          not bad, (bad[0] if bad else ""))
    # A run that produced nothing would pass vacuously.
    check("skenario benar-benar menghasilkan sinyal dan transaksi",
          len(ev) > 0 and len(tr) > 0, f"{len(ev)} event, {len(tr)} transaksi")


if __name__ == "__main__":
    print("test_pipeline_invariants.py")
    print("\n[test_pipeline_invariants]")
    test_pipeline_invariants()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
