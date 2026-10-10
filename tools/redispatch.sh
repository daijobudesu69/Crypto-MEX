#!/usr/bin/env bash
# Menyalakan run signal.yml berikutnya dari DALAM run yang sedang berakhir.
#
# Dulu estafet antar run bergantung pada cron GitHub, yang di repo ini hanya
# jalan ~25% dari slotnya: hampir tiap hari ada jeda 1-4 jam tanpa watcher
# (9 Okt: 12:03 -> 14:18 UTC, terdeteksi watchdog). Dispatch lewat API tidak
# melewati penjadwal cron, dan concurrency group `mex-state` membuat run baru
# ANTRE sampai run lama selesai, jadi tidak pernah ada dua watcher sekaligus.
#
# workflow_dispatch dari GITHUB_TOKEN diizinkan (satu dari dua pengecualian
# aturan "event dari GITHUB_TOKEN tidak memicu workflow"); butuh `actions: write`.
#
# Dipakai: GITHUB_TOKEN=... bash tools/redispatch.sh
# Exit 0 kalau GitHub menerima (HTTP 204), selain itu 1 -- pemanggil yang
# memutuskan apa artinya. Kegagalannya tidak fatal: cron dan watchdog tetap
# jaring pengaman.
set -uo pipefail

: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY kosong}"
REF="${GITHUB_REF_NAME:-main}"
WORKFLOW="${REDISPATCH_WORKFLOW:-signal.yml}"

if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "[redispatch] GITHUB_TOKEN kosong, dilewati"
  exit 1
fi

code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -X POST \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "${GITHUB_API_URL:-https://api.github.com}/repos/${GITHUB_REPOSITORY}/actions/workflows/${WORKFLOW}/dispatches" \
  -d "{\"ref\":\"${REF}\",\"inputs\":{\"mode\":\"loop\"}}") || code="curl-gagal"

if [ "$code" = "204" ]; then
  echo "[redispatch] ${WORKFLOW} dijadwalkan lagi (antre sampai run ini selesai)"
  exit 0
fi
echo "[redispatch] GAGAL: HTTP ${code} -- estafet jatuh ke cron / watchdog"
exit 1
