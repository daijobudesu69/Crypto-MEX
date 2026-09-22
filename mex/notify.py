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
import math
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
        # Type only, never the message. A requests exception embeds the full
        # request URL, and that URL contains the bot token:
        #   ConnectionError: ... Max retries exceeded with url: /bot<TOKEN>/send...
        # ledger._push() and sheets.append() already follow this rule; this was
        # the one call site that did not.
        print(f"[notify] telegram gagal: {type(e).__name__}")
        return False


# --------------------------------------------------------------------------- #
# message templates
# --------------------------------------------------------------------------- #
def _f(x, n=None):
    """Format a number for a human.

    With an explicit `n` this is a plain fixed-decimal format, which is what
    percentages, multipliers and RSI want.

    Without one it adapts to magnitude, because two decimals is only right for
    an instrument priced like ETH. On DOGE at ~0.09 it rendered the entry zone
    as "0.09 — 0.09" (both bounds identical), the ATR as "0.00", and then told
    the reader to size with "Entry = (risk% × capital) ÷ 0.00" -- an instruction
    to divide by zero. On XRP it printed "1R = 1.5 × 0.03 = 0.05", a formula
    that does not produce the number beside it.

    Anything from 10 upwards keeps the original two decimals, so every number
    ETH has ever printed -- price, ATR, 1R -- renders byte-identically and the
    running forward test does not change appearance. Below 10 the precision
    grows with the magnitude, and trailing zeros are dropped so nothing reads
    like "3.2000". Two decimals are always kept, so a price never looks like an
    integer.
    """
    if x is None:
        return "-"
    v = float(x)
    if n is not None:
        return f"{v:,.{n}f}"
    a = abs(v)
    if a >= 10:
        dec = 2
    elif a >= 1:
        dec = 4
    elif a > 0:
        # Scale the precision to the value instead of stopping at a fixed depth.
        # A fixed ladder just moves the original bug somewhere smaller: at eight
        # decimals anything under 1e-8 still prints as "0.00", which is a
        # non-zero number rendered as zero -- exactly what made the DOGE signal
        # unusable. Capped at 12 so the string stays readable.
        dec = min(12, max(6, 5 - int(math.floor(math.log10(a)))))
    else:
        dec = 2
    s = f"{v:,.{dec}f}"
    if "." in s:
        whole, _, frac = s.rstrip("0").partition(".")
        # Keep two decimals minimum: a price should not render as an integer.
        s = f"{whole}.{(frac + '00')[:2] if len(frac) < 2 else frac}"
    return s


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
    mult = atr_mult if atr_mult else (
        (p["r_est"] / ctx["atr14"]) if ctx.get("atr14") else 0.0)
    # ATR is derived back out of 1R and the multiplier rather than read from
    # sig_ctx. 1R is mult x ATR by construction, so this is the same number --
    # but strategy.context() rounds what it stores to six decimals, which on a
    # coin priced near 0.09 is enough that the printed formula stops adding up:
    # "1.5 x 0.001768 = 0.00265229". Deriving it keeps the line self-consistent.
    # ETH is unaffected: 60.5532 / 1.5 still prints as 40.37.
    atr = (p["r_est"] / mult) if mult else ctx.get("atr14")
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


def _posline(sym, pos, unreal):
    tag = f"  {sym.replace('USDT', ''):<5}"
    if not pos:
        return f"{tag} —  (menunggu sinyal)"
    return (f"{tag} {'LONG ' if pos['side'] > 0 else 'SHORT'} sejak "
            f"{pos['entry_bar'][:16].replace('T', ' ')}\n"
            f"        entry {_f(pos['entry_price'])} · stop {_f(pos['trail'])} "
            f"· {unreal:+.2f} R")


def heartbeat_message(s):
    # `positions` is {symbol: {"position": …, "unrealised_R": …}}. The older
    # single-symbol shape is still accepted so a caller that has not been
    # updated cannot silently produce an empty message.
    positions = s.get("positions")
    if positions is None:
        positions = {s.get("symbol", "ETHUSDT"): {
            "position": s.get("position"), "unrealised_R": s.get("unrealised_R", 0)}}
    lines = [_posline(sym, v.get("position"), v.get("unrealised_R", 0) or 0)
             for sym, v in positions.items()]
    n_open = sum(1 for v in positions.values() if v.get("position"))
    posline = (f"  posisi terbuka: {n_open} dari {len(positions)}\n"
               + "\n".join(lines))
    down = s.get("symbols_down") or []
    if down:
        posline += ("\n⚠️ <b>data tidak terjangkau:</b> "
                    + esc(", ".join(down)))
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
