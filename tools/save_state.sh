#!/usr/bin/env bash
# Commit dan push isi state/ ke branch ini, dengan retry dan penggabungan yang
# benar saat run lain menulis duluan.
#
# Diekstrak dari signal.yml/heartbeat.yml supaya bisa dipanggil berkali-kali dari
# dalam loop pemantau -- dan supaya logika konflik yang rapuh ini hanya ada di
# SATU tempat, bukan disalin di dua workflow yang lalu berbeda diam-diam.
#
# Dipakai: bash tools/save_state.sh "pesan commit"
set -uo pipefail

MSG="${1:-state: $(date -u +%Y-%m-%dT%H:%MZ)}"
BRANCH="${GITHUB_REF_NAME:-main}"

git config user.name  "mex-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add state/
if git diff --cached --quiet; then
  # Tidak ada perubahan BARU -- tapi belum tentu tidak ada yang perlu di-push.
  # Kalau iterasi loop sebelumnya sempat commit lalu gagal push 5x, commit itu
  # masih menggantung di lokal. Keluar 0 di sini akan meninggalkannya tak
  # ter-push SELAMANYA sambil melaporkan sukses -- persis kelas kegagalan yang
  # "rebase --skip" dulu lakukan. Jadi cek dulu apakah HEAD di depan origin.
  git fetch -q origin "${BRANCH}" || true
  if [ -z "$(git rev-list "origin/${BRANCH}..HEAD" 2>/dev/null)" ]; then
    echo "[save_state] tidak ada perubahan state"
    exit 0
  fi
  echo "[save_state] ada commit yang belum ter-push dari percobaan sebelumnya"
else
  git commit -q -m "${MSG} [skip ci]"
fi

for i in 1 2 3 4 5; do
  if git push -q origin "HEAD:${BRANCH}"; then
    echo "[save_state] state tersimpan"
    exit 0
  fi
  echo "[save_state] push ditolak; run lain menulis duluan, menggabungkan (percobaan $i)"
  git fetch -q origin "${BRANCH}"
  if ! git rebase "origin/${BRANCH}"; then
    # CSV digabung otomatis oleh merge=union di .gitattributes. position.json
    # diselesaikan berdasarkan ISI (last_bar terbaru menang, sent_ids dan outbox
    # digabung), bukan berdasarkan sisi rebase.
    if git ls-files -u state/position.json | grep -q .; then
      python tools/merge_state.py state/position.json
    fi
    git add state/
    if git grep -qI --cached '^<<<<<<< ' -- state/ 2>/dev/null; then
      echo "::error::penanda konflik tersisa di state/, dibatalkan"
      git rebase --abort || true
      exit 1
    fi
    # JANGAN pernah "rebase --skip": itu membuang commit state run ini sementara
    # loop tetap melaporkan sukses -- ledger dan Telegram jadi tidak sinkron.
    if ! GIT_EDITOR=true git rebase --continue; then
      git rebase --abort || true
      sleep $((i * 3))
      continue
    fi
  fi
  sleep $((i * 3))
done

echo "::error::gagal menyimpan state setelah 5 percobaan"
exit 1
