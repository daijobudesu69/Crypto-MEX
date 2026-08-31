"""Send one real signal to Telegram on demand, to check formatting end to end.

Renders the most recent genuine signal in the data rather than dummy values, so
what arrives is exactly what a live alert will look like. Marked as a test at the
top so it can never be mistaken for a tradeable alert.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

from mex import datafeed, notify  # noqa: E402
from mex.config import load  # noqa: E402
from mex.strategy import compute_features, step  # noqa: E402

cfg = load()
p = cfg["params"]
feed = datafeed.fetch(limit=1000, prefer=cfg["prefer_source"])
df, source = feed.df, feed.source
f = compute_features(df, p)
ts = pd.DatetimeIndex(df["ts"])

pos = pending = last = None
for i in range(len(df)):
    pos, pending, events = step(f, ts, i, p, pos, pending)
    for ev in events:
        if ev["event"] == "SIGNAL":
            last = ev

if last is None:
    print("tidak ada sinyal di data yang tersedia")
    sys.exit(1)

body = notify.signal_message(last["pending"], last["ctx"], cfg["symbol"], source, 0.0)
banner = ("\U0001F9EA <b>TEST — bukan sinyal aktif</b>\n"
          "<i>Sinyal nyata terakhir, dikirim manual untuk mengecek tampilan.</i>\n\n")
ok = notify.send(banner + body)
print(f"[test] terkirim={ok}  bar={last['pending']['signal_bar']}  sumber={source}")
sys.exit(0 if ok else 1)
