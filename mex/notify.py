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
import html
import os

import requests

API = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram rejects the whole message with HTTP 400 when parse_mode=HTML and the
# body contains a tag it does not know. Exception text routinely carries "<", ">"
# and "&" -- an HTTP error body, a URL with query params, a repr with angle
# brackets -- so any value that did not come from these templates is escaped
# before it is interpolated. Without this the failure alert is itself rejected,
# precisely when the data feed is down and the alert is the only thing left.
def esc(x) -> str:
    return html.escape(str(x), quote=False)


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


def _wib(iso: str) -> str:
    """UTC ISO timestamp -> dd-mm-yyyy HH:MM WIB (UTC+7), what the user reads."""
    import datetime as _dt
    t = _dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return (t.astimezone(_dt.timezone(_dt.timedelta(hours=7)))
            .strftime("%d-%m-%Y %H:%M WIB"))


def signal_message(p, ctx, symbol, source, sent_delay_min, atr_mult=None):
    """The actionable message. Layout is fixed by the user.

    `atr_mult` is Params.atr_sl_mult, passed in by the caller. Deriving it from
    r_est / atr instead silently prints "0.0 x ATR" whenever the ATR is missing
    from ctx -- a wrong instruction rather than a visible error -- so the
    authoritative value wins and the derivation is only the fallback.
    """
    side = "LONG" if p["side"] > 0 else "FADE SHORT"
    icon = "🟢" if p["side"] > 0 else "🔴"
    atr = ctx.get("atr14")
    mult = atr_mult if atr_mult else ((p["r_est"] / atr) if atr else 0.0)
    r = _f(p["r_est"])
    # Kept even though it is not in the template: acting on a stale signal is the
    # one failure this channel can actually cause, and it only appears when real.
    late = (f"\n\n⚠️ <b>Delivered {sent_delay_min:.0f} min late</b> — "
            "check the entry zone before acting."
            if sent_delay_min and sent_delay_min > 45 else "")
    return f"""{icon} <b>MEX SIGNAL — {side}</b>
{symbol} · 4H · {_wib(p['signal_bar'])}

🎯 <b>Entry zone</b>
{_f(p['zone_low'])} — {_f(p['zone_high'])}
reference: {_f(p['ref_price'])}
valid until: {_wib(p['expires_at'])}

🛑 <b>Trailing Stop: {_f(p['callback_pct_est'], 2)}%</b>
ATR: {_f(atr)} ({_f(ctx.get('atr_pct_of_price'), 2)}% of price)
* Trailing Stop formula: {_f(mult, 1)} × ATR ÷ price × 100
* 1R = {_f(mult, 1)} × {_f(atr)} = {r} USDT
different entry price? TS = {r} ÷ entry price × 100

📐 <b>Position size</b>
Entry = (risk% × capital) ÷ {r}

📊 <b>Signal bar context:</b>
RSI {_f(ctx.get('rsi'), 1)} · ΔRSI(5) {_f(ctx.get('rsi_roc'), 1)}
volume {_f(ctx.get('vol_ratio'), 2)}× average
breakout +{_f(ctx.get('breakout_margin_pct'), 2)}% above 20-bar high{late}"""


def entry_message(pos, symbol, source):
    side = "LONG" if pos.side > 0 else "FADE SHORT"
    direction = "di BAWAH" if pos.side > 0 else "di ATAS"
    atr = getattr(pos, "atr_at_entry", 0.0) or 0.0
    mult = (pos.r_usdt / atr) if atr else 0.0
    return f"""📌 <b>ENTRY TERCATAT — {side}</b>
<code>{symbol} · {pos.entry_bar[:16].replace('T', ' ')} UTC</code>

  harga referensi : <b>{_f(pos.entry_price)}</b>
  ATR di bar ini  : {_f(atr)}   (ATR bergeser tiap lilin)
  1R = {_f(mult, 1)} × {_f(atr)} = <b>{_f(pos.r_usdt)} USDT</b>
  callback rate   : <b>{_f(pos.callback_pct, 2)}%</b>
    = {_f(pos.r_usdt)} ÷ {_f(pos.entry_price)} × 100
  stop awal       : {_f(pos.stop_initial)} ({direction} entry)

<i>Angka ini yang dipakai untuk menilai forward test.
Kalau fill Anda berbeda, catat di kolom actual_fill_price.</i>
<i>sumber: {source} · id: {pos.signal_id}</i>"""


def exit_message(t, symbol, source):
    win = t["result_R"] >= 0
    icon = "✅" if win else "🛑"
    arrow = "📈" if win else "📉"
    return f"""{icon} <b>EXIT — {t['side'].upper()}</b>
<code>{symbol} · {_wib(t['exit_bar_utc'])}</code>

{arrow} <b>Reference result: {t['ret_pct']:+.2f}% = {t['result_R']:+.2f} R</b>
entry {_f(t['entry_price'])} → exit {_f(t['exit_price'])}
held {t['bars_held']} bars ({t['hours_held']:.0f} hours)

📊 MFE {t['mfe_pct']:+.2f}% · MAE {t['mae_pct']:+.2f}%
gave back {t['giveback_pct']:.2f} pp from peak

📝 <b>Your numbers will differ.</b> Log them:
actual_fill_price · actual_exit_price
id: {t['signal_id']}"""


def heartbeat_message(s):
    pos = s.get("position")
    if pos:
        posline = (f"  posisi TERBUKA: {'LONG' if pos['side'] > 0 else 'SHORT'} "
                   f"sejak {pos['entry_bar'][:16].replace('T', ' ')}\n"
                   f"  entry {_f(pos['entry_price'])} · stop sekarang {_f(pos['trail'])} "
                   f"· berjalan {s.get('unrealised_R', 0):+.2f} R")
    else:
        posline = "  posisi: tidak ada (menunggu sinyal)"
    warn = ("" if s["data_ok"]
            else "\n⚠️ <b>DATA BERMASALAH</b> — " + esc(str(s.get("error", ""))[:200]))
    # A mirror that has been quietly refusing rows for a week is invisible in the
    # CSVs and shows up nowhere else. One line a day is what makes it findable.
    mirror = s.get("mirror_24h")
    mirror_line = f"\n  mirror Sheets 24 jam: {esc(mirror)}" if mirror else ""
    stuck = s.get("outbox_pending", 0)
    stuck_line = (f"\n⚠️ <b>{stuck} pesan belum terkirim</b> — masih dicoba ulang tiap run."
                  if stuck else "")
    expired = s.get("signals_30d_expired", 0)
    sig = f"{s['signals_30d']} sinyal"
    if expired:
        sig += f" ({expired} hangus sebelum terkirim)"
    return f"""\U0001F493 <b>MEX forward test — hidup</b>
<code>{s['now'][:16].replace('T', ' ')} UTC</code>

  bar terakhir diproses: {s['last_bar'][:16].replace('T', ' ') if s['last_bar'] else '-'}
  sumber data: {esc(s['source'])}{mirror_line}
{posline}

  30 hari terakhir: {sig} · {s['trades_30d']} transaksi selesai
  total sejak mulai: {s['trades_total']} transaksi · {s['sum_R']:+.2f} R

<i>Pesan ini muncul 1× sehari hanya untuk memastikan repo masih jalan.
Sinyal dikirim terpisah, hanya kalau memang ada.</i>{stuck_line}{warn}"""


def alert_message(kind, detail):
    return (f"⚠️ <b>MEX — {esc(kind)}</b>\n\n<code>{esc(str(detail)[:600])}</code>\n\n"
            "<i>Sinyal mungkin tertunda sampai ini beres.</i>")
