"""Resolve a rebase conflict in state/position.json on the merits, not by side.

The workflows used to settle this with `git checkout --theirs`, on the reasoning
that the current run "has processed the most recent bar". In the retry path that
is backwards: our push was rejected *because* another run pushed first, so the
other side is usually the newer one. Forcing ours could walk `last_bar`
backwards, and a bar that has already been processed would then be replayed.

So decide on content instead of on which side of the rebase a version sits:

  last_bar          the later of the two, and position/pending are taken from
                    that same side -- they are one consistent snapshot of the
                    state machine and must not be mixed across versions.
  sent_ids          union. A message either side has delivered must never be
                    delivered again, so dropping half the list would resend.
  outbox            union by key, minus anything the merged sent_ids covers.
                    Losing a queued message loses a signal; keeping a delivered
                    one sends it twice.

Usage (inside a conflicted rebase):  python tools/merge_state.py state/position.json
Falls back to whichever side parses if the other is missing or unreadable.
"""
import json
import subprocess
import sys


def _stage(n: int, path: str):
    """Read one conflict stage: 2 = ours (upstream), 3 = theirs (being replayed)."""
    try:
        raw = subprocess.run(["git", "show", f":{n}:{path}"],
                             capture_output=True, check=True).stdout
        return json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[merge_state] stage {n} tidak terbaca: {type(e).__name__}")
        return None


def merge(a: dict, b: dict) -> dict:
    """Merge two state snapshots. Neither argument is modified."""
    if a is None:
        return b
    if b is None:
        return a
    newer, older = (a, b) if str(a.get("last_bar") or "") >= str(b.get("last_bar") or "") else (b, a)

    out = dict(newer)
    seen, sent = set(), []
    for sid in list(a.get("sent_ids") or []) + list(b.get("sent_ids") or []):
        if sid not in seen:
            seen.add(sid)
            sent.append(sid)
    out["sent_ids"] = sent

    outbox, keys = [], set()
    for m in list(a.get("outbox") or []) + list(b.get("outbox") or []):
        k = m.get("key")
        if k in keys or k in seen:
            continue
        keys.add(k)
        outbox.append(m)
    out["outbox"] = outbox

    # The later date wins: if either side has already sent today's heartbeat,
    # losing that fact would send a second one on the next 10-minute tick.
    hb = [d for d in (a.get("last_heartbeat_date"), b.get("last_heartbeat_date")) if d]
    if hb:
        out["last_heartbeat_date"] = max(hb)

    print(f"[merge_state] last_bar {older.get('last_bar')} + {newer.get('last_bar')}"
          f" -> {out.get('last_bar')}; sent_ids={len(sent)}; outbox={len(outbox)}")
    return out


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "state/position.json"
    ours, theirs = _stage(2, path), _stage(3, path)
    if ours is None and theirs is None:
        print("[merge_state] kedua sisi tidak terbaca, konflik tidak diselesaikan")
        return 1
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(merge(ours, theirs), fh, indent=2, default=str)
    subprocess.run(["git", "add", path], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
