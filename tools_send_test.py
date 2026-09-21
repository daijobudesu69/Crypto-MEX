"""Send one real signal per symbol to Telegram on demand, to check formatting.

Renders the most recent genuine signal in each symbol's data rather than dummy
values, so what arrives is exactly what a live alert will look like. Marked as a
test at the top so it can never be mistaken for a tradeable alert.

One message per symbol, not one combined message: a live alert is always about a
single instrument, and a combined digest would check a layout that never ships.
This is also the only place the low-priced symbols get eyeballed end to end --
DOGE at ~0.089 is where two fixed decimals used to collapse the entry zone to
"0.09 — 0.09" and tell the reader to divide by zero.

MEX_TEST_SYMBOLS overrides the list, e.g. MEX_TEST_SYMBOLS=DOGEUSDT to check one.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

from mex import datafeed, notify  # noqa: E402
from mex.config import load  # noqa: E402
from mex.strategy import compute_features, step  # noqa: E402

BANNER = ("\U0001F9EA <b>TEST — bukan sinyal aktif</b>\n"
          "<i>Sinyal nyata terakhir, dikirim manual untuk mengecek tampilan.</i>\n\n")


def last_signal(symbol, cfg):
    """The most recent SIGNAL event in this symbol's available history."""
    feed = datafeed.fetch(limit=1000, prefer=cfg["prefer_source"], symbol=symbol)
    f = compute_features(feed.df, cfg["params"])
    ts = pd.DatetimeIndex(feed.df["ts"])
    pos = pending = last = None
    for i in range(len(feed.df)):
        pos, pending, events = step(f, ts, i, cfg["params"], pos, pending)
        for ev in events:
            if ev["event"] == "SIGNAL":
                last = ev
    return last, feed.source


def main() -> int:
    cfg = load()
    override = os.environ.get("MEX_TEST_SYMBOLS", "").strip()
    symbols = [s.strip() for s in override.split(",") if s.strip()] or cfg["symbols"]

    sent, failed, none_found = 0, 0, []
    for sym in symbols:
        try:
            last, source = last_signal(sym, cfg)
        except Exception as e:  # noqa: BLE001
            # One unreachable feed must not stop the others from being checked.
            print(f"[test] {sym}: GAGAL ambil data -- {type(e).__name__}: {e}")
            failed += 1
            continue
        if last is None:
            print(f"[test] {sym}: tidak ada sinyal di data yang tersedia")
            none_found.append(sym)
            continue
        body = notify.signal_message(last["pending"], last["ctx"], sym, source, 0.0,
                                     atr_mult=cfg["params"].atr_sl_mult)
        ok = notify.send(BANNER + body)
        # Without a bot token notify.send() prints the message and returns
        # False. That is the documented way to exercise the pipeline before the
        # bot exists -- run_signal._flush() treats it the same way -- so it must
        # not be counted as a delivery failure or this script is red by design.
        if not notify.configured():
            print(f"[test] {sym}: Telegram tidak dikonfigurasi, pesan dicetak saja")
            sent += 1
            continue
        sent += ok
        failed += not ok
        print(f"[test] {sym}: terkirim={ok} "
              f"bar={last['pending']['signal_bar']} sumber={source}")

    print(f"[test] selesai: {sent} terkirim, {failed} gagal"
          + (f", tanpa sinyal: {','.join(none_found)}" if none_found else ""))
    # Nothing to send is not a failure; a send that was attempted and refused is.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
