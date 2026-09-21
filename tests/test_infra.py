"""Offline tests for the delivery and state machinery around the strategy.

test_strategy.py guards the signals. This file guards everything that carries
them: Telegram escaping, the outbox, atomic state writes, CSV schema drift and
the rebase state merge. Each test below corresponds to a defect that was found
in the 2026-09-03 infrastructure audit, so a regression here means one of those
failures has come back.

No network, no clock dependence beyond explicit timestamps.
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mex.compat  # noqa: F401,E402

import pandas as pd  # noqa: E402

from mex import ledger, notify  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + detail}")


# --------------------------------------------------------------------------- #
def test_html_escaping():
    """A failure alert must survive parse_mode=HTML.

    Telegram rejects the entire message with HTTP 400 when it meets a tag it
    does not know, so an unescaped exception took down the one message that
    reports the data feed being down.
    """
    err = RuntimeError("451 <b>blocked</b> & <html> retry=3")
    msg = notify.alert_message("data feed gagal", err)
    check("alert_message meng-escape < > & dari teks exception",
          "<b>blocked</b>" not in msg and "&lt;b&gt;blocked" in msg, msg[:120])
    check("alert_message menyisakan tag template-nya sendiri",
          "<code>" in msg and "<b>MEX" in msg)

    hb = notify.heartbeat_message({
        "now": "2026-09-03T00:00:00", "last_bar": "2026-09-03T00:00:00",
        "source": "test", "position": None, "data_ok": False,
        "error": "ConnectTimeout: <urllib3.conn> & more",
        "signals_30d": 0, "trades_30d": 0, "trades_total": 0, "sum_R": 0.0})
    check("heartbeat meng-escape teks error",
          "<urllib3.conn>" not in hb and "&lt;urllib3.conn&gt;" in hb, hb[-160:])


def test_number_format_survives_cheap_coins():
    """Two fixed decimals is only right for an instrument priced like ETH.

    On DOGE at ~0.089 the signal message rendered the entry zone as
    "0.09 — 0.09" (both bounds identical), the ATR as "0.00", and then told the
    reader to size the position with "÷ 0.00" -- an instruction to divide by
    zero. On XRP it printed "1R = 1.5 × 0.03 = 0.05", a formula that does not
    produce the number printed beside it. Neither is actionable.
    """
    from mex.notify import _f, signal_message

    # Every value ETH has ever printed must render exactly as it used to, or
    # the running forward test's messages would change appearance mid-test.
    for v, expect in [(2665.95, "2,665.95"), (40.368824, "40.37"),
                      (60.5532, "60.55"), (2605.3968, "2,605.40")]:
        check(f"ETH {v} tetap dicetak {expect}", _f(v) == expect, _f(v))

    check("harga DOGE tidak lagi terpotong jadi 0.09",
          _f(0.089220) == "0.08922", _f(0.089220))
    check("ATR DOGE tidak lagi tampil nol", _f(0.0017681) == "0.0017681")
    check("1R DOGE tidak lagi nol -- ini yang bikin instruksi bagi nol",
          float(_f(0.00265215).replace(",", "")) > 0, _f(0.00265215))
    check("nilai persis nol tetap dicetak 0.00", _f(0.0) == "0.00")
    check("dua desimal minimum dipertahankan", _f(3.2) == "3.20", _f(3.2))
    check("n eksplisit tetap fixed-decimal", _f(2.345, 1) == "2.3")

    # The printed formula must actually produce the printed result.
    atr = 0.031228
    check("1.5 × ATR yang dicetak = 1R yang dicetak",
          _f(atr) == "0.031228" and _f(1.5 * atr) == "0.046842",
          f"{_f(atr)} -> {_f(1.5 * atr)}")

    p = {"side": 1, "signal_bar": "2026-09-19T16:00:00+00:00", "ref_price": 0.089220,
         "zone_low": 0.087894, "zone_high": 0.090546,
         "expires_at": "2026-09-20T00:00:00+00:00",
         "callback_pct_est": 2.97, "r_est": 0.00265215}
    msg = signal_message(p, {"atr14": 0.0017681}, "DOGEUSDT", "test", 0.0,
                         atr_mult=1.5)
    check("zona entry DOGE punya batas atas dan bawah yang BERBEDA",
          "0.087894 — 0.090546" in msg,
          [l for l in msg.split("\n") if "—" in l])
    check("pesan DOGE tidak memuat pembagian dengan nol",
          "÷ 0.00\n" not in msg and "= 0.00 USDT" not in msg)


def test_signal_message_uses_authoritative_multiplier():
    """The printed formula must not be derived from a value that can be missing."""
    p = {"side": 1, "signal_bar": "2026-09-02T20:00:00+00:00", "ref_price": 4321.5,
         "zone_low": 4300.0, "zone_high": 4340.0,
         "expires_at": "2026-09-03T04:00:00+00:00",
         "callback_pct_est": 2.11, "r_est": 91.2}
    msg = notify.signal_message(p, {"atr14": None}, "ETHUSDT", "test", 0.0,
                                atr_mult=1.5)
    check("rumus memakai atr_sl_mult walau ATR hilang dari ctx",
          "1.5 × ATR" in msg, [l for l in msg.split("\n") if "formula" in l])


# --------------------------------------------------------------------------- #
def test_state_atomicity_and_corruption():
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "position.json")
        ledger.write_json(path, {"last_bar": "2026-09-02T20:00:00+00:00"})
        check("write_json tidak meninggalkan file .tmp",
              not os.path.exists(path + ".tmp"))
        check("write_json bisa dibaca kembali",
              ledger.read_json(path, {})["last_bar"] == "2026-09-02T20:00:00+00:00")

        # A run killed mid-write used to leave exactly this.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"last_bar": "2026-09-02T20:00:00+00:00", "posi')
        raised = False
        try:
            ledger.read_json(path, {})
        except ledger.StateCorrupt:
            raised = True
        check("state rusak melempar StateCorrupt, bukan diam-diam jadi default",
              raised, "read_json mengembalikan default -> bootstrap ulang, "
                      "posisi terbuka hilang")

        check("file hilang tetap mengembalikan default",
              ledger.read_json(os.path.join(d, "nope.json"), {"x": 1}) == {"x": 1})
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_csv_header_rotation():
    """A changed column list must rotate the log, not corrupt it."""
    d = tempfile.mkdtemp()
    old_events = ledger.EVENTS
    try:
        ledger.EVENTS = os.path.join(d, "events.csv")
        with open(ledger.EVENTS, "w", newline="", encoding="utf-8") as fh:
            fh.write("logged_at_utc,event,signal_id\n2026-01-01,SIGNAL,old-1\n")
        ledger.log_event({"logged_at_utc": "2026-09-03", "event": "SIGNAL",
                          "signal_id": "new-1", "side": "long"})
        check("log lama diarsipkan saat header berubah",
              os.path.exists(os.path.join(d, "events.v1.csv")))
        fresh = pd.read_csv(ledger.EVENTS)
        check("log baru bisa dibaca pandas", len(fresh) == 1, f"{len(fresh)} baris")
        check("log baru memakai skema sekarang",
              list(fresh.columns) == ledger.EVENT_COLS)
        archived = pd.read_csv(os.path.join(d, "events.v1.csv"))
        check("baris historis tetap utuh di arsip",
              len(archived) == 1 and archived["signal_id"].iloc[0] == "old-1")
    finally:
        ledger.EVENTS = old_events
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
def test_outbox():
    import run_signal

    real_send, real_conf = notify.send, notify.configured
    notify.configured = lambda: True
    try:
        # 1. delivery failure keeps the message and reports it
        notify.send = lambda text: False
        st = {"sent_ids": [], "outbox": [{
            "key": "SIGNAL:s1", "kind": "SIGNAL", "signal_id": "s1", "text": "x",
            "expires_at": (pd.Timestamp.now(tz="UTC") + pd.Timedelta("4h")).isoformat(),
            "queued_at": "", "attempts": 0}]}
        sent, failed, dropped = run_signal._flush(st)
        check("kirim gagal -> pesan tetap di outbox untuk dicoba ulang",
              (sent, failed, dropped) == (0, 1, 0) and len(st["outbox"]) == 1,
              f"sent={sent} failed={failed} outbox={len(st['outbox'])}")
        check("percobaan dihitung", st["outbox"][0]["attempts"] == 1)

        # 2. the retry on the next run succeeds and the message leaves the outbox
        notify.send = lambda text: True
        sent, failed, dropped = run_signal._flush(st)
        check("run berikutnya berhasil mengirim ulang",
              (sent, failed) == (1, 0) and st["outbox"] == [])
        check("id tercatat sebagai terkirim", st["sent_ids"] == ["SIGNAL:s1"])

        # 3. a replayed bar must not resend
        st["outbox"] = [{"key": "SIGNAL:s1", "kind": "SIGNAL", "signal_id": "s1",
                         "text": "x", "expires_at":
                         (pd.Timestamp.now(tz="UTC") + pd.Timedelta("4h")).isoformat(),
                         "queued_at": "", "attempts": 0}]
        calls = []
        notify.send = lambda text: calls.append(text) or True
        sent, failed, dropped = run_signal._flush(st)
        check("pesan yang sudah pernah terkirim tidak dikirim dua kali",
              calls == [] and sent == 0, f"{len(calls)} pengiriman ulang")

        # 4. expired while queued -> dropped, never sent late
        st = {"sent_ids": [], "outbox": [{
            "key": "SIGNAL:s2", "kind": "SIGNAL", "signal_id": "s2", "text": "x",
            "expires_at": (pd.Timestamp.now(tz="UTC") - pd.Timedelta("1h")).isoformat(),
            "queued_at": "", "attempts": 0}]}
        sent, failed, dropped = run_signal._flush(st)
        check("sinyal yang hangus di outbox dibuang, bukan dikirim basi",
              (sent, failed, dropped) == (0, 0, 1) and st["outbox"] == [])

        # 5. without a bot token the run must stay green, not queue forever
        notify.configured = lambda: False
        notify.send = real_send
        st = {"sent_ids": [], "outbox": [{
            "key": "SIGNAL:s3", "kind": "SIGNAL", "signal_id": "s3", "text": "x",
            "expires_at": (pd.Timestamp.now(tz="UTC") + pd.Timedelta("4h")).isoformat(),
            "queued_at": "", "attempts": 0}]}
        sent, failed, dropped = run_signal._flush(st)
        check("tanpa token Telegram pesan dicetak dan run tetap hijau",
              failed == 0 and st["outbox"] == [])
    finally:
        notify.send, notify.configured = real_send, real_conf


# --------------------------------------------------------------------------- #
def test_idle_run_logging():
    """The watcher polls every 10 min; only meaningful runs may write a row.

    Suppressing too much would hide the forward test's own liveness record;
    suppressing nothing would add 144 rows and 144 commits a day.
    """
    import run_signal

    d = tempfile.mkdtemp()
    old_runs, old_env = ledger.RUNS, os.environ.get("MEX_QUIET_IDLE")
    try:
        ledger.RUNS = os.path.join(d, "runs.csv")
        os.environ["MEX_QUIET_IDLE"] = "1"
        idle = {"bars_processed": 0, "events_emitted": 0, "status": "ok"}

        check("tanpa runs.csv, baris pertama selalu ditulis",
              run_signal._should_log_run(idle))

        ledger.log_run({**idle, "run_at_utc": pd.Timestamp.now(tz="UTC").isoformat()})
        check("run idle tepat setelah baris terakhir ditahan",
              not run_signal._should_log_run(idle))
        check("run yang memproses bar selalu dicatat",
              run_signal._should_log_run({**idle, "bars_processed": 1}))
        check("run yang menghasilkan event selalu dicatat",
              run_signal._should_log_run({**idle, "events_emitted": 1}))
        check("run gagal selalu dicatat",
              run_signal._should_log_run({**idle, "status": "data_error"}))
        check("run dengan pesan tersangkut selalu dicatat",
              run_signal._should_log_run({**idle, "status": "delivery_error"}))

        # an hour later the idle row is allowed through again
        ledger.RUNS = os.path.join(d, "old.csv")
        stale = (pd.Timestamp.now(tz="UTC") - pd.Timedelta("90min")).isoformat()
        ledger.log_run({**idle, "run_at_utc": stale})
        check("setelah lewat 60 menit, baris idle ditulis lagi",
              run_signal._should_log_run(idle))

        del os.environ["MEX_QUIET_IDLE"]
        ledger.RUNS = os.path.join(d, "runs.csv")
        check("tanpa MEX_QUIET_IDLE semua run dicatat seperti semula",
              run_signal._should_log_run(idle))

        # position.json juga tidak boleh ditulis ulang kalau isinya sama.
        # Menahan baris runs.csv saja tidak cukup: updated_at yang berubah tiap
        # run tetap membuat file berbeda, dan pemantau tetap commit tiap 10 menit.
        base = {"last_bar": "2026-09-04T00:00:00+00:00", "position": None,
                "pending": None, "sent_ids": ["SIGNAL:a"], "outbox": [],
                "updated_at": "2026-09-04T04:03:14+00:00"}
        fp = run_signal._fingerprint
        check("fingerprint mengabaikan updated_at",
              fp(base) == fp({**base, "updated_at": "2026-09-04T09:99:99+00:00"}))
        check("fingerprint berubah saat last_bar maju",
              fp(base) != fp({**base, "last_bar": "2026-09-04T04:00:00+00:00"}))
        check("fingerprint berubah saat ada pesan terkirim",
              fp(base) != fp({**base, "sent_ids": ["SIGNAL:a", "ENTRY:a"]}))
        check("fingerprint berubah saat outbox terisi",
              fp(base) != fp({**base, "outbox": [{"key": "SIGNAL:b"}]}))
        check("fingerprint berubah saat posisi terbuka",
              fp(base) != fp({**base, "position": {"side": 1}}))
        check("fingerprint stabil terhadap urutan kunci",
              fp(base) == fp(dict(reversed(list(base.items())))))
    finally:
        ledger.RUNS = old_runs
        if old_env is None:
            os.environ.pop("MEX_QUIET_IDLE", None)
        else:
            os.environ["MEX_QUIET_IDLE"] = old_env
        shutil.rmtree(d, ignore_errors=True)


def test_heartbeat_schedule():
    """Kapan heartbeat harian jatuh tempo.

    Dulu ini diputuskan cron 00:07 UTC, dan cron itu meleset ~4 jam SETIAP hari
    (terukur 4j02m-4j10m empat hari berturut-turut). Sekarang loop pemantau yang
    memanggil, tiap 10 menit, jadi logikanya harus menolak 143 dari 144 panggilan
    harian tanpa menyentuh jaringan -- dan tidak boleh dobel.
    """
    import run_heartbeat as hb

    now = pd.Timestamp.now(tz="UTC")
    today = now.strftime("%Y-%m-%d")
    yday = (now - pd.Timedelta("1D")).strftime("%Y-%m-%d")
    old_env = os.environ.get("MEX_FORCE_HEARTBEAT")
    old_target = hb.TARGET_UTC
    try:
        os.environ.pop("MEX_FORCE_HEARTBEAT", None)
        hb.TARGET_UTC = "00:00"

        check("belum pernah kirim -> jatuh tempo", hb._due({})[0])
        check("kemarin sudah kirim -> jatuh tempo hari ini",
              hb._due({"last_heartbeat_date": yday})[0])
        due, why = hb._due({"last_heartbeat_date": today})
        check("sudah kirim hari ini -> TIDAK dikirim lagi", not due, why)

        # target di masa depan hari ini -> belum waktunya
        hb.TARGET_UTC = "23:59"
        due, why = hb._due({"last_heartbeat_date": yday})
        expect_wait = now < now.normalize() + pd.Timedelta("23h59min")
        check("sebelum jam target -> menunggu", (not due) == expect_wait, why)

        # force menembus kedua penjaga
        os.environ["MEX_FORCE_HEARTBEAT"] = "1"
        check("MEX_FORCE_HEARTBEAT menembus penjaga",
              hb._due({"last_heartbeat_date": today})[0])

        os.environ.pop("MEX_FORCE_HEARTBEAT", None)
        hb.TARGET_UTC = "bukan-jam"
        check("target rusak tidak membuat heartbeat berhenti selamanya",
              hb._due({"last_heartbeat_date": yday})[0])
    finally:
        hb.TARGET_UTC = old_target
        if old_env is None:
            os.environ.pop("MEX_FORCE_HEARTBEAT", None)
        else:
            os.environ["MEX_FORCE_HEARTBEAT"] = old_env


def _v2(sym_bars: dict, **rest) -> dict:
    """Build a v2 state: {symbol: last_bar} plus whatever else the test needs."""
    return {"schema": 2, "symbols": {s: {"last_bar": b, "position": None,
                                         "pending": None}
                                     for s, b in sym_bars.items()}, **rest}


def test_state_migration():
    """v1 -> v2 must not lose a live signal, and must not resend a delivered one.

    The production state at the time of the change held an unexpired pending
    signal and ten delivered ids in the v1 key format. Dropping the pending
    would lose a trade the forward test was about to record; leaving the ids in
    the old format would make every one of them look unknown to the dedup check
    and send them all a second time.
    """
    from mex import state

    v1 = {
        "last_bar": "2026-09-21T00:00:00+00:00",
        "position": None,
        "pending": {"side": 1, "signal_id": "20260921T0000-L",
                    "expires_at": "2026-09-21T08:00:00+00:00", "notified": True},
        "started_at": "2026-08-30T13:51:04+00:00",
        "engine_version": "mex-fwd-1.1.0",
        "sent_ids": ["SIGNAL:20260903T1200-L", "ENTRY:20260903T1200-L",
                     "SIGNAL:20260921T0000-L"],
        "outbox": [{"key": "ENTRY:20260921T0000-L", "kind": "ENTRY"}],
        "last_heartbeat_date": "2026-09-21",
    }
    m = state.migrate(v1, "ETHUSDT")

    check("schema ditandai v2", m.get("schema") == 2)
    check("last_bar pindah ke slot simbol utama",
          m["symbols"]["ETHUSDT"]["last_bar"] == v1["last_bar"])
    check("pending yang masih hidup TIDAK hilang saat migrasi",
          m["symbols"]["ETHUSDT"]["pending"]["signal_id"] == "20260921T0000-L")
    check("kunci lama di akar sudah tidak ada",
          not any(k in m for k in ("last_bar", "position", "pending")))
    check("sent_ids diberi prefix simbol",
          m["sent_ids"] == ["SIGNAL:ETHUSDT:20260903T1200-L",
                            "ENTRY:ETHUSDT:20260903T1200-L",
                            "SIGNAL:ETHUSDT:20260921T0000-L"], m["sent_ids"])
    check("kunci outbox ikut diubah, jadi tidak terkirim dua kali",
          m["outbox"][0]["key"] == "ENTRY:ETHUSDT:20260921T0000-L")
    check("outbox mewarisi simbolnya", m["outbox"][0]["symbol"] == "ETHUSDT")
    check("field global lain dipertahankan",
          m["last_heartbeat_date"] == "2026-09-21"
          and m["started_at"] == v1["started_at"])

    again = state.migrate(m, "ETHUSDT")
    check("migrasi idempoten -- dijalankan dua kali hasilnya sama", again == m)

    # The other delicate moment: migrating while a position is OPEN. Losing it
    # would abandon a live trailing stop, which is the single worst thing this
    # state file can do.
    live = dict(v1, position={"side": 1, "signal_id": "20260918T1600-L",
                              "entry_price": 2600.0, "r_usdt": 55.0,
                              "trail": 2570.0, "callback_pct": 2.1},
                pending=None)
    lm = state.migrate(live, "ETHUSDT")
    eth = lm["symbols"]["ETHUSDT"]
    check("posisi terbuka ikut pindah utuh saat migrasi",
          eth["position"] == live["position"], eth["position"])
    check("trailing stop posisi itu tidak berubah nilainya",
          eth["position"]["trail"] == 2570.0)
    check("open_positions() menemukannya",
          state.open_positions(lm) == {"ETHUSDT": live["position"]})

    check("state kosong tetap kosong supaya bootstrap tetap dikenali",
          state.migrate({}, "ETHUSDT") == {})
    never = state.migrate({"sent_ids": [], "outbox": []}, "ETHUSDT")
    check("v1 yang belum pernah punya last_bar tidak dibuatkan slot palsu",
          never["symbols"] == {}, never["symbols"])


def test_dedup_key_is_per_symbol():
    """The defect this prevents: 148 of 439 backtest signals across
    ETH/DOGE/XRP/SOL shared an id, because strategy._sid() has no symbol in it.
    With a v1 key the second symbol's message is dropped as a duplicate."""
    from mex import state

    sid = "20240325T1200-L"          # real collision: DOGE, XRP and SOL
    keys = {state.dedup_key("SIGNAL", s, sid)
            for s in ("DOGEUSDT", "XRPUSDT", "SOLUSDT")}
    check("tiga simbol dengan signal_id sama menghasilkan 3 kunci berbeda",
          len(keys) == 3, keys)
    check("kunci memuat simbolnya",
          state.dedup_key("SIGNAL", "DOGEUSDT", sid) == "SIGNAL:DOGEUSDT:" + sid)
    check("requalify idempoten",
          state._requalify("SIGNAL:ETHUSDT:" + sid, "DOGEUSDT")
          == "SIGNAL:ETHUSDT:" + sid)


def test_entry_message_is_scoped_to_its_symbol():
    """The end-to-end path, not just the key builder.

    Replay can only exercise ENTRY on signals that have already expired, and an
    expired signal is deliberately never announced -- so the send path for a
    LIVE entry is not covered by any historical replay. This drives _handle()
    directly with exactly the state the pending ETH signal will be in when its
    next bar closes, and then checks the case that motivated the whole change:
    a different symbol carrying the SAME signal id must not be swallowed.
    """
    import run_signal
    from mex.strategy import Params, Position

    pos = Position(side=1, signal_id="20260921T0000-L",
                   signal_bar="2026-09-21T00:00:00+00:00",
                   entry_bar="2026-09-21T04:00:00+00:00",
                   entry_price=2670.0, r_usdt=60.55, callback_pct=2.27,
                   stop_initial=2609.45, trail=2609.45, hi_water=2700.0,
                   lo_water=2660.0, notified=True, ref_price=2665.95,
                   atr_at_entry=40.37, sig_ctx={"atr14": 40.37})
    ev = {"event": "ENTRY", "bar": pd.Timestamp("2026-09-21T04:00:00+00:00"),
          "pos": pos, "ctx": {"atr14": 40.37}, "pending": {"ref_price": 2665.95}}

    old_events, old_trades = ledger.EVENTS, ledger.TRADES
    d = tempfile.mkdtemp()
    try:
        ledger.EVENTS = os.path.join(d, "events.csv")
        ledger.TRADES = os.path.join(d, "trades.csv")

        out = run_signal._handle(ev, "ETHUSDT", "test", Params(), {"sent_ids": []})
        check("ENTRY yang sudah diumumkan sinyalnya menghasilkan 1 pesan",
              len(out) == 1, f"{len(out)} pesan")
        check("kunci ENTRY memuat simbol",
              out and out[0]["key"] == "ENTRY:ETHUSDT:20260921T0000-L",
              out[0]["key"] if out else "")
        check("pesan membawa simbolnya", out and out[0]["symbol"] == "ETHUSDT")
        check("teks pesan menyebut simbol", out and "ETHUSDT" in out[0]["text"])

        already = {"sent_ids": ["ENTRY:ETHUSDT:20260921T0000-L"]}
        check("ENTRY yang sudah terkirim tidak diulang",
              run_signal._handle(ev, "ETHUSDT", "test", Params(), already) == [])
        other = run_signal._handle(ev, "DOGEUSDT", "test", Params(), already)
        check("simbol LAIN dengan signal_id sama TIDAK ikut terblokir",
              len(other) == 1 and other[0]["key"] == "ENTRY:DOGEUSDT:20260921T0000-L",
              other[0]["key"] if other else "kosong -- pesan hilang, ini bug lamanya")
    finally:
        ledger.EVENTS, ledger.TRADES = old_events, old_trades
        shutil.rmtree(d, ignore_errors=True)


def test_symbols_wired_end_to_end():
    from mex import datafeed
    from mex.config import load

    cfg = load()
    check("config mengekspos daftar simbol", cfg["symbols"] == datafeed.SYMBOLS)
    check("empat simbol terdaftar", len(datafeed.SYMBOLS) == 4, datafeed.SYMBOLS)
    check("simbol utama tetap ETHUSDT dan ada di daftar",
          datafeed.SYMBOL == "ETHUSDT" and "ETHUSDT" in datafeed.SYMBOLS)
    check("tiap simbol punya kontrak Gate.io untuk failover",
          all(s in datafeed.GATE for s in datafeed.SYMBOLS),
          [s for s in datafeed.SYMBOLS if s not in datafeed.GATE])
    check("tidak ada simbol duplikat", len(set(datafeed.SYMBOLS)) == 4)
    raised = False
    try:
        datafeed.fetch(symbol="TIDAKADAUSDT")
    except RuntimeError:
        raised = True
    check("simbol tanpa pemetaan Gate.io ditolak, bukan diam-diam kehilangan failover",
          raised)


def test_merge_state():
    from tools.merge_state import merge

    ours = _v2({"ETHUSDT": "2026-09-02T20:00:00+00:00"},
               sent_ids=["SIGNAL:ETHUSDT:a"], outbox=[{"key": "SIGNAL:ETHUSDT:b"}])
    ours["symbols"]["ETHUSDT"]["position"] = {"side": 1}
    theirs = _v2({"ETHUSDT": "2026-09-02T16:00:00+00:00"},
                 sent_ids=["SIGNAL:ETHUSDT:c"], outbox=[{"key": "SIGNAL:ETHUSDT:d"}])

    m = merge(ours, theirs)
    eth = m["symbols"]["ETHUSDT"]
    check("last_bar yang menang adalah yang paling baru",
          eth["last_bar"] == "2026-09-02T20:00:00+00:00", eth["last_bar"])
    check("position ikut dari sisi last_bar terbaru", eth["position"] == {"side": 1})
    check("sent_ids digabung dari kedua sisi",
          sorted(m["sent_ids"]) == ["SIGNAL:ETHUSDT:a", "SIGNAL:ETHUSDT:c"],
          m["sent_ids"])
    check("outbox digabung dari kedua sisi",
          sorted(x["key"] for x in m["outbox"])
          == ["SIGNAL:ETHUSDT:b", "SIGNAL:ETHUSDT:d"])

    # urutan argumen tidak boleh mengubah hasil -- ini inti bug "--theirs"
    m2 = merge(theirs, ours)
    check("hasil merge tidak bergantung sisi rebase",
          m2["symbols"]["ETHUSDT"] == eth)

    # Inti multi-simbol: tiap simbol dinilai SENDIRI. Mengambil seluruh dict
    # symbols dari satu sisi akan memundurkan state machine simbol lain, dan
    # bar yang diulang merusak trailing stop-nya.
    a = _v2({"ETHUSDT": "2026-09-05T00:00:00+00:00",
             "DOGEUSDT": "2026-09-04T00:00:00+00:00"})
    b = _v2({"ETHUSDT": "2026-09-04T20:00:00+00:00",
             "DOGEUSDT": "2026-09-05T04:00:00+00:00"})
    m6 = merge(a, b)
    check("tiap simbol mengambil last_bar terbarunya SENDIRI",
          (m6["symbols"]["ETHUSDT"]["last_bar"] == "2026-09-05T00:00:00+00:00"
           and m6["symbols"]["DOGEUSDT"]["last_bar"] == "2026-09-05T04:00:00+00:00"),
          {s: v["last_bar"] for s, v in m6["symbols"].items()})

    # Simbol yang hanya ada di satu sisi (baru di-bootstrap) tidak boleh hilang.
    m7 = merge(_v2({"ETHUSDT": "2026-09-05T00:00:00+00:00"}),
               _v2({"ETHUSDT": "2026-09-04T00:00:00+00:00",
                    "SOLUSDT": "2026-09-05T00:00:00+00:00"}))
    check("simbol yang baru ada di satu sisi tetap terbawa",
          "SOLUSDT" in m7["symbols"], sorted(m7["symbols"]))

    # Saat deploy, satu sisi bisa masih v1 karena job lama belum mati.
    v1_side = {"last_bar": "2026-09-05T08:00:00+00:00", "position": None,
               "pending": None, "sent_ids": ["SIGNAL:x"], "outbox": []}
    m8 = merge(v1_side, _v2({"ETHUSDT": "2026-09-05T00:00:00+00:00"}))
    check("sisi v1 ikut dimigrasikan sebelum digabung, tidak dibuang",
          m8["symbols"]["ETHUSDT"]["last_bar"] == "2026-09-05T08:00:00+00:00",
          m8["symbols"]["ETHUSDT"]["last_bar"])
    check("sent_ids dari sisi v1 ikut diberi prefix",
          "SIGNAL:ETHUSDT:x" in m8["sent_ids"], m8["sent_ids"])

    # a message already delivered by the other side must not be re-queued
    m3 = merge(_v2({"ETHUSDT": "2026-09-02T20:00:00+00:00"},
                   sent_ids=["SIGNAL:ETHUSDT:b"], outbox=[]),
               _v2({"ETHUSDT": "2026-09-02T16:00:00+00:00"},
                   sent_ids=[], outbox=[{"key": "SIGNAL:ETHUSDT:b"}]))
    check("pesan yang sudah terkirim di satu sisi tidak masuk outbox lagi",
          m3["outbox"] == [], m3["outbox"])

    # Kalau tanggal heartbeat hilang saat merge, tick 10 menit berikutnya akan
    # mengirim heartbeat kedua di hari yang sama.
    m4 = merge(_v2({"ETHUSDT": "2026-09-05T00:00:00+00:00"}),
               _v2({"ETHUSDT": "2026-09-04T20:00:00+00:00"},
                   last_heartbeat_date="2026-09-05"))
    check("tanggal heartbeat bertahan walau ada di sisi yang kalah",
          m4.get("last_heartbeat_date") == "2026-09-05", m4.get("last_heartbeat_date"))
    m5 = merge(_v2({"ETHUSDT": "2026-09-05T00:00:00+00:00"},
                   last_heartbeat_date="2026-09-04"),
               _v2({"ETHUSDT": "2026-09-04T20:00:00+00:00"},
                   last_heartbeat_date="2026-09-05"))
    check("tanggal heartbeat terbaru yang menang",
          m5.get("last_heartbeat_date") == "2026-09-05", m5.get("last_heartbeat_date"))


# --------------------------------------------------------------------------- #
def test_config_rejects_retired_keys():
    from mex.config import load, RETIRED
    from mex import datafeed

    d = tempfile.mkdtemp()
    try:
        for key in ("symbol", "timeframe", "bootstrap_flat"):
            path = os.path.join(d, f"{key}.yaml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"prefer_source: binance_spot_mirror\n{key}: ETHUSDT\n")
            raised = False
            try:
                load(path)
            except ValueError:
                raised = True
            check(f"config.yaml menolak kunci mati '{key}'", raised,
                  "kunci yang tidak tersambung ke apa pun harus gagal keras")
        check("semua kunci pensiun terdaftar",
              set(RETIRED) == {"symbol", "timeframe", "bootstrap_flat"})

        path = os.path.join(d, "ok.yaml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("prefer_source: binance_spot_mirror\nstrategy:\n  vol_mult: 1.5\n")
        cfg = load(path)
        check("symbol dibaca dari datafeed, bukan dari config",
              cfg["symbol"] == datafeed.SYMBOL == "ETHUSDT")
        check("timeframe dibaca dari datafeed", cfg["timeframe"] == datafeed.INTERVAL)
        check("bar tersedia sebagai Timedelta", cfg["bar"] == pd.Timedelta("4h"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_datafeed_guards():
    from mex import datafeed

    df = pd.DataFrame({
        "ts": pd.to_datetime(["2026-09-01T00:00Z", "2026-09-01T00:00Z"], utc=True),
        "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
        "close": [1.0, 1.0], "volume": [1.0, 1.0]})
    raised = ""
    try:
        datafeed.sanity_check(df, min_bars=1)
    except RuntimeError as e:
        raised = str(e)
    check("sanity_check menolak timestamp duplikat", "duplicate" in raised, raised)
    check("status yang tidak mungkin pulih tidak diulang",
          {451, 429, 418, 403}.issubset(datafeed.NO_RETRY_STATUS))


if __name__ == "__main__":
    print("test_infra.py")
    for t in (test_html_escaping, test_number_format_survives_cheap_coins,
              test_signal_message_uses_authoritative_multiplier,
              test_state_atomicity_and_corruption, test_csv_header_rotation,
              test_outbox, test_idle_run_logging, test_heartbeat_schedule,
              test_state_migration, test_dedup_key_is_per_symbol,
              test_entry_message_is_scoped_to_its_symbol,
              test_symbols_wired_end_to_end,
              test_merge_state, test_config_rejects_retired_keys,
              test_datafeed_guards):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
