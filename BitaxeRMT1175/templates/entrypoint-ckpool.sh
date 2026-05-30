#!/bin/sh
set -eu

BTC_ADDR="$(grep -m1 '"btcaddress"' /config/ckpool.conf | sed 's/.*"btcaddress"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/')"

echo "[BitaxeRMT1175] Starting ckpool for address: ${BTC_ADDR}"
echo "[BitaxeRMT1175] btcsig: BitaxeRMT_Mods"

exec /usr/local/bin/ckpool -c /config/ckpool.conf -C /config/ckpool.args
