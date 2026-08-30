"""Telegram delivery.

Two rules drive the design:

  * Signals go out only when something actually happened. A run that finds no new
    signal sends nothing at all -- weeks of silence are the expected case.
  * A run must never fail because Telegram is unreachable. The CSV ledger is the
    record; Telegram is a convenience. Delivery failures are logged and the run
    continues green, otherwise a messaging outage would look like a strategy
    outage.

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as repository secrets. With either
missing, messages are printed to the job log instead, so the pipeline can be
exercised end-to-end before the bot exists.
"""
from . import compat  # noqa: F401
import os

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
                and os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def send(text: str) -> bool:
    """Return True if Telegram accepted the message. Never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        print("[notify] Telegram not configured -- message below was not sent\n")
        print(text)
        return False
    try:
        r = requests.post(
            API.format(token=token),
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=25,
        )
        if r.status_code >= 400:
            print(f"[notify] telegram HTTP {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[notify] telegram failed: {type(e).__name__}: {e}")
        return False


# --------------------------------------------------------------------------- #
# message templates
# --------------------------------------------------------------------------- #
def _f(x, n=2):
    return "-" if x is None else f"{float(x):,.{n}f}"


def signal_message(p, ctx, symbol, source, sent_delay_min):
    side = "LONG" if p["side"] > 0 else "FADE SHORT"
    icon = "\U0001F7E2" if p["side"] > 0 else "\U0001F534"
    direction = "di BAWAH" if p["side"] > 0 else "di ATAS"
    late = ("\n⚠️ <b>Terkirim terlambat "
            f"{sent_delay_min:.0f} menit</b> — cek zona entry sebelum masuk."
            if sent_delay_min and sent_delay_min > 45 else "")
    return f"""{icon} <b>SINYAL MEX — {side}</b>
<code>{symbol} · 4H · {p['signal_bar'][:16].replace('T', ' ')} UTC</code>

<b>Zona entry</b> (sinyal batal di luar ini)
  {_f(p['zone_low'])}  —  {_f(p['zone_high'])}
  referensi: <b>{_f(p['ref_price'])}</b>
  berlaku sampai: {p['expires_at'][:16].replace('T', ' ')} UTC

<b>Stop — pakai Trailing Stop, bukan stop biasa</b>
  callback rate: <b>{_f(p['callback_pct_est'], 2)}%</b>
  1R = {_f(p['r_est'])} USDT ({_f(p['callback_pct_est'], 2)}% dari harga)
  level awal ≈ {_f(p['stop_est'])} ({direction} entry)

<b>Ukuran posisi</b>
  qty = (risiko% × modal) ÷ {_f(p['r_est'])}
  risiko 1% → kerugian maksimum ≈ 1R

<b>Konteks bar sinyal</b>
  RSI {_f(ctx.get('rsi'), 1)} · ΔRSI(5) {_f(ctx.get('rsi_roc'), 1)}
  volume {_f(ctx.get('vol_ratio'), 2)}× rata-rata
  breakout +{_f(ctx.get('breakout_margin_pct'), 2)}% di atas high 20 bar
  ATR {_f(ctx.get('atr14'))} ({_f(ctx.get('atr_pct_of_price'), 2)}% harga)

<i>sumber data: {source} · id: {p['signal_id']}</i>
<i>Tanpa TP. Trailing stop yang menutup posisi.</i>{late}"""


def entry_message(pos, symbol, source):
    side = "LONG" if pos.side > 0 else "FADE SHORT"
    direction = "di BAWAH" if pos.side > 0 else "di ATAS"
    return f"""\U0001F4CC <b>ENTRY TERCATAT — {side}</b>
<code>{symbol} · {pos.entry_bar[:16].replace('T', ' ')} UTC</code>

  harga referensi : <b>{_f(pos.entry_price)}</b>
  1R              : {_f(pos.r_usdt)} USDT
  callback rate   : <b>{_f(pos.callback_pct, 2)}%</b>
  stop awal       : {_f(pos.stop_initial)} ({direction} entry)

<i>Angka ini yang dipakai untuk menilai forward test.
Kalau fill Anda berbeda, catat di kolom actual_fill_price.</i>
<i>sumber: {source} · id: {pos.signal_id}</i>"""


def exit_message(t, symbol, source):
    win = t["result_R"] >= 0
    icon = "✅" if win else "\U0001F6D1"
    return f"""{icon} <b>EXIT — {t['side'].upper()}</b>
<code>{symbol} · {t['exit_bar_utc'][:16].replace('T', ' ')} UTC</code>

  entry  {_f(t['entry_price'])}  →  exit  {_f(t['exit_price'])}
  hasil  <b>{t['ret_pct']:+.2f}%  =  {t['result_R']:+.2f} R</b>
  ditahan {t['bars_held']} bar ({t['hours_held']:.0f} jam)

  puncak terbaik (MFE) {t['mfe_pct']:+.2f}%
  titik terburuk (MAE) {t['mae_pct']:+.2f}%
  dikembalikan dari puncak: {t['giveback_pct']:.2f} pp

<i>sumber: {source} · id: {t['signal_id']}</i>"""


def heartbeat_message(s):
    pos = s.get("position")
    if pos:
        posline = (f"  posisi TERBUKA: {'LONG' if pos['side'] > 0 else 'SHORT'} "
                   f"sejak {pos['entry_bar'][:16].replace('T', ' ')}\n"
                   f"  entry {_f(pos['entry_price'])} · stop sekarang {_f(pos['trail'])} "
                   f"· berjalan {s.get('unrealised_R', 0):+.2f} R")
    else:
        posline = "  posisi: tidak ada (menunggu sinyal)"
    warn = "" if s["data_ok"] else "\n⚠️ <b>DATA BERMASALAH</b> — " + str(s.get("error", ""))[:200]
    return f"""\U0001F493 <b>MEX forward test — hidup</b>
<code>{s['now'][:16].replace('T', ' ')} UTC</code>

  bar terakhir diproses: {s['last_bar'][:16].replace('T', ' ') if s['last_bar'] else '-'}
  sumber data: {s['source']}
{posline}

  30 hari terakhir: {s['signals_30d']} sinyal · {s['trades_30d']} transaksi selesai
  total sejak mulai: {s['trades_total']} transaksi · {s['sum_R']:+.2f} R

<i>Pesan ini muncul 1× sehari hanya untuk memastikan repo masih jalan.
Sinyal dikirim terpisah, hanya kalau memang ada.</i>{warn}"""


def alert_message(kind, detail):
    return (f"⚠️ <b>MEX — {kind}</b>\n\n<code>{str(detail)[:600]}</code>\n\n"
            "<i>Sinyal mungkin tertunda sampai ini beres.</i>")
