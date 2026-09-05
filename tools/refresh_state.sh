#!/usr/bin/env bash
# Sinkronkan working tree dengan origin SEBELUM run_signal.py membaca state.
#
# Kenapa perlu: job pemantau melakukan checkout sekali di awal, lalu hidup 5,5
# jam. Saat pergantian job, job baru bisa checkout SEBELUM push terakhir job
# lama mendarat -- lalu membaca position.json yang basi dan memproses ulang bar
# yang sudah selesai. Terukur 2x dalam 24 jam:
#
#   20:06  run 33878095203  last_bar 16:00  bars_processed=1   <- job lama
#   20:16  run 33911360144  last_bar 16:00  bars_processed=1   <- job baru, bar SAMA
#
# Kebetulan bar-bar itu tidak menghasilkan event, jadi tidak ada kerusakan. Tapi
# kalau bar yang diproses ulang menghasilkan SIGNAL, `sent_ids` di checkout basi
# juga belum berisi id-nya -- dan pesan Telegram akan terkirim DUA KALI.
#
# Dua penjaga di bawah membuat reset ini tidak pernah membuang pekerjaan:
#   1. ada commit lokal yang belum ter-push  -> lewati (save_state.sh yang urus)
#   2. ada perubahan state/ belum ter-commit -> lewati (jangan buang data run ini)
set -uo pipefail

BRANCH="${GITHUB_REF_NAME:-main}"

if ! git fetch -q origin "${BRANCH}" 2>/dev/null; then
  echo "[refresh] fetch gagal; lanjut dengan state lokal"
  exit 0
fi

if [ -n "$(git status --porcelain state/ 2>/dev/null)" ]; then
  echo "[refresh] ada perubahan state/ belum ter-commit; tidak disentuh"
  exit 0
fi

if [ -n "$(git rev-list "origin/${BRANCH}..HEAD" 2>/dev/null)" ]; then
  echo "[refresh] ada commit lokal belum ter-push; tidak disentuh"
  exit 0
fi

local_sha="$(git rev-parse HEAD)"
origin_sha="$(git rev-parse "origin/${BRANCH}")"
if [ "$local_sha" = "$origin_sha" ]; then
  echo "[refresh] sudah sinkron"
  exit 0
fi

git reset -q --hard "origin/${BRANCH}"
echo "[refresh] state disinkronkan ${local_sha:0:8} -> ${origin_sha:0:8}"
