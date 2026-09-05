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


def test_merge_state():
    from tools.merge_state import merge

    ours = {"last_bar": "2026-09-02T20:00:00+00:00", "position": {"side": 1},
            "sent_ids": ["SIGNAL:a"], "outbox": [{"key": "SIGNAL:b"}]}
    theirs = {"last_bar": "2026-09-02T16:00:00+00:00", "position": None,
              "sent_ids": ["SIGNAL:c"], "outbox": [{"key": "SIGNAL:d"}]}

    m = merge(ours, theirs)
    check("last_bar yang menang adalah yang paling baru",
          m["last_bar"] == "2026-09-02T20:00:00+00:00", m["last_bar"])
    check("position ikut dari sisi last_bar terbaru", m["position"] == {"side": 1})
    check("sent_ids digabung dari kedua sisi",
          sorted(m["sent_ids"]) == ["SIGNAL:a", "SIGNAL:c"], m["sent_ids"])
    check("outbox digabung dari kedua sisi",
          sorted(x["key"] for x in m["outbox"]) == ["SIGNAL:b", "SIGNAL:d"])

    # urutan argumen tidak boleh mengubah hasil -- ini inti bug "--theirs"
    m2 = merge(theirs, ours)
    check("hasil merge tidak bergantung sisi rebase",
          m2["last_bar"] == m["last_bar"] and m2["position"] == m["position"])

    # a message already delivered by the other side must not be re-queued
    m3 = merge({"last_bar": "2026-09-02T20:00:00+00:00", "sent_ids": ["SIGNAL:b"],
                "outbox": []},
               {"last_bar": "2026-09-02T16:00:00+00:00", "sent_ids": [],
                "outbox": [{"key": "SIGNAL:b"}]})
    check("pesan yang sudah terkirim di satu sisi tidak masuk outbox lagi",
          m3["outbox"] == [], m3["outbox"])

    # Kalau tanggal heartbeat hilang saat merge, tick 10 menit berikutnya akan
    # mengirim heartbeat kedua di hari yang sama.
    m4 = merge({"last_bar": "2026-09-05T00:00:00+00:00"},
               {"last_bar": "2026-09-04T20:00:00+00:00",
                "last_heartbeat_date": "2026-09-05"})
    check("tanggal heartbeat bertahan walau ada di sisi yang kalah",
          m4.get("last_heartbeat_date") == "2026-09-05", m4.get("last_heartbeat_date"))
    m5 = merge({"last_bar": "2026-09-05T00:00:00+00:00",
                "last_heartbeat_date": "2026-09-04"},
               {"last_bar": "2026-09-04T20:00:00+00:00",
                "last_heartbeat_date": "2026-09-05"})
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
    for t in (test_html_escaping, test_signal_message_uses_authoritative_multiplier,
              test_state_atomicity_and_corruption, test_csv_header_rotation,
              test_outbox, test_idle_run_logging, test_heartbeat_schedule,
              test_merge_state, test_config_rejects_retired_keys,
              test_datafeed_guards):
        print(f"\n[{t.__name__}]")
        t()
    print(f"\n{len(PASS)} lulus, {len(FAIL)} gagal")
    sys.exit(1 if FAIL else 0)
