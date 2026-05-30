#!/bin/sh
set -eu

echo '[BitaxeRMT1175] 1175d entrypoint starting'

if ! command -v 1175d >/dev/null 2>&1 && ! command -v elevenseventyfived >/dev/null 2>&1; then
  echo '[BitaxeRMT1175] ERROR: 1175d not found'
  exit 127
fi

CMD="$(command -v 1175d 2>/dev/null || command -v elevenseventyfived 2>/dev/null)"

extra=''
if [ -f /data/.reindex-chainstate ]; then
  echo '[BitaxeRMT1175] Reindex requested (chainstate).'
  rm -f /data/.reindex-chainstate || true
  extra='-reindex-chainstate'
fi

dbcache="${BTC_DBCACHE_MB:-}"
if [ -z "${dbcache}" ]; then
  mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  mem_mb="$((mem_kb / 1024))"
  dbcache="$((mem_mb / 8))"
  if [ "$dbcache" -lt 256 ]; then dbcache=256; fi
  if [ "$dbcache" -gt 12288 ]; then dbcache=12288; fi
fi

echo "[BitaxeRMT1175] Using dbcache=${dbcache}MB"
echo "[BitaxeRMT1175] Exec: ${CMD} -datadir=/data -conf=/data/bitcoin.conf -printtoconsole -dbcache=${dbcache} ${extra}"
exec "${CMD}" -datadir=/data -conf=/data/bitcoin.conf -printtoconsole -dbcache="${dbcache}" ${extra}
