# AGENTS.md — 5tratumOS Mining Pool Infrastructure

## GitHub

- **Repo**: `https://github.com/Crypto13Vault/5tratumos-store`
- **Token**: `${GITHUB_TOKEN}` (full access, no expiry, set in env)

```bash
gh repo clone Crypto13Vault/5tratumos-store
git remote add origin https://Crypto13Vault:${GITHUB_TOKEN}@github.com/Crypto13Vault/5tratumos-store.git
git push -u origin main
```

## Access

```
Host:  192.168.0.200
User:  forge
Pass:  Domi191219
SSH:   sshpass -p 'Domi191219' ssh -o StrictHostKeyChecking=no forge@192.168.0.200
Sudo:  echo 'Domi191219' | sudo -S <command>
```

## RPC Credentials

| Service | Container | User | Password | Port |
|---|---|---|---|---|
| BTC bitcoind | `5tratumos-axebtc-bitcoind-1` | `btc` | `nSdNSDfJY9-PQuajGEMCwaUc` | 28332 |
| BCH node | `5tratumos-axebch-bchn-1` | `bch` | `sLcQX8qcj-H7pnt3B_5dxLin` | 28332 |
| BCH wallet | `public-bch-wallet` | `bchpool` | `PublicPool2026!` | 28336 |

BTC/BCH dashboard and payout daemons use `docker exec` + `bitcoin-cli` (hardcoded).  
BCH wallet uses `-datadir=/data` which reads `/data/bitcoin.conf` automatically.

## Architecture

```
5tratumOS (192.168.0.200) — Docker + systemd + Caddy
├── publicpool (BTC Block Party) — public PROP pool
│   ├── public-ckpool-btc      port 28334 (stratum)
│   ├── server.py              port 8080 (dashboard)
│   ├── payout.py              systemd: publicpool-payout
│   └── /var/lib/5tratumos/apps/publicpool/
├── publicpool-bch (BCH Block Party) — public PROP pool
│   ├── public-ckpool-bch      port 28335 (stratum)
│   ├── public-bch-wallet     port 28336 (zquestz image)
│   ├── server.py              port 8081 (dashboard)
│   ├── payout.py              systemd: publicpool-bch-payout
│   └── /var/lib/5tratumos/apps/publicpool-bch/
├── axebtc — private BTC pool (ckpool 7890, app 21215)
├── axebch — private BCH pool (ckpool 4567, node stratum 28333, app 21212)
├── axebc2 — private BTC-alt pool (ckpool 2345, app 21219)
├── bitaxe-dashboard — ASIC fleet monitor (port 5050, Docker)
├── Caddy — TLS terminator (80/443/3334/3335)
└── Overlay nginx — 5tratumOS portal (8083, LAN/Tailscale only)
```

## Containers

| Name | Image | Purpose |
|---|---|---|
| `public-ckpool-btc` | `ghcr.io/willitmod/docker-ckpool-solo:590fb2a` | BTC main stratum (port 28334) |
| `public-ckpool-btc-booster` | same image | BTC BOOSTER rental stratum (port 28338, 1M diff) |
| `public-ckpool-bch` | same image | BCH main stratum (port 28335) |
| `public-ckpool-bch-booster` | same image | BCH BOOSTER rental stratum (port 28337, 1M diff) |
| `public-bch-wallet` | `zquestz/bitcoin-cash-node:latest` | BCH wallet (no P2P, RPC only) |
| `bitaxe-dashboard-dashboard-1` | `bitaxe-dashboard-dashboard` | ASIC fleet dashboard |
| `5tratumos-axebtc-bitcoind-1` | `ghcr.io/willitmod/axebtc-bitcoind-switch:0.7.44` | BTC full node (pruned 500MB) |
| `5tratumos-axebch-bchn-1` | `ghcr.io/willitmod/bitcoin-cash-node:v29.0.0-wm3` | BCH full node (pruned) |

All private pool containers (`5tratumos-axebtc-*`, `5tratumos-axebch-*`, `5tratumos-axebc2-*`) are managed by 5tratumOS — do NOT edit directly.

## Key Paths

| Component | Path |
|---|---|
| BTC ckpool config | `/var/lib/5tratumos/apps/publicpool/data/pool/config/ckpool.conf` |
| BTC dashboard | `/var/lib/5tratumos/apps/publicpool/dashboard/server.py` |
| BTC dashboard frontend | `/var/lib/5tratumos/apps/publicpool/dashboard/index.html` |
| BTC payout daemon | `/var/lib/5tratumos/apps/publicpool/payout.py` |
| BTC payout ledger | `/var/lib/5tratumos/apps/publicpool/data/payout_ledger.json` |
| BTC ckpool entrypoint | `/var/lib/5tratumos/apps/publicpool/entrypoint.sh` |
| BCH ckpool config | `/var/lib/5tratumos/apps/publicpool-bch/data/pool/config/ckpool.conf` |
| BCH dashboard | `/var/lib/5tratumos/apps/publicpool-bch/dashboard/server.py` |
| BCH payout daemon | `/var/lib/5tratumos/apps/publicpool-bch/payout.py` |
| BCH payout ledger | `/var/lib/5tratumos/apps/publicpool-bch/data/payout_ledger.json` |
| BCH docker compose | `/var/lib/5tratumos/apps/publicpool-bch/docker-compose.yml` |
| BCH wallet config | `/var/lib/5tratumos/apps/publicpool-bch/wallet/bitcoin.conf` |
| Caddy config | `/etc/caddy/Caddyfile` |
| Caddy logs | `/var/log/caddy/` |
| Payout logs | `/var/log/publicpool-payout.log` |
| Overlay nginx | `/opt/5tratumos/overlay/nginx/default.conf` |
| Bitaxe dashboard | `/opt/5tratumos/store/custom-5tratumos-store/bitaxe-dashboard/bitaxe_dashboard.py` |
| Private pool compose | `/opt/5tratumos/apps/{axebtc,axebch,axebc2}/docker-compose.yml` |
| Private pool data | `/var/lib/5tratumos/apps/{axebtc,axebch,axebc2}/data/` |
| Local working copy | `/tmp/current_axedash.py` (SCP to remote for deploy) |

## Services

```bash
# Dashboard APIs
sudo systemctl restart publicpool-dashboard      # BTC dashboard (port 8080)
sudo systemctl restart publicpool-bch-dashboard  # BCH dashboard (port 8081)

# Payout daemons
sudo systemctl restart publicpool-payout         # BTC payout
sudo systemctl restart publicpool-bch-payout     # BCH payout

# TLS proxy
sudo systemctl restart caddy

# Docker containers
docker restart public-ckpool-btc
docker restart public-ckpool-bch
docker restart public-bch-wallet
docker restart bitaxe-dashboard-dashboard-1
```

## Payout Model

1% pool fee → 25% finder bonus (of remaining 99%) → 75% PROP split among all workers. 101-block maturity queue. Ledger at `payout_ledger.json` tracks pending payouts across restarts.

- BTC operator: `bc1qtyn8yjwqa7g0dvf9stn83p2dxxqqtnjhqrnvrm`
- BCH operator: `bitcoincash:qpkwxxwz9qg9muskdtj6j503wadqpy6yhqy2r60x89`

## Networking

| Port | Service | Public |
|---|---|---|
| 80 | BTC dashboard (HTTP) | Router-forwarded |
| 443 | BTC dashboard (HTTPS) | Router-forwarded |
| 3334 | BTC stratum (TLS) | Router-forwarded |
| 28334 | BTC stratum (TCP) | Router-forwarded |
| 3335 | BCH stratum (TLS) | Router-forwarded |
| 28335 | BCH stratum (TCP) | Router-forwarded |
| 5050 | Bitaxe dashboard | No |
| 8080 | BTC dashboard API | No (via Caddy :80/:443) |
| 8081 | BCH dashboard API | No (via Caddy) |
| 8083 | Overlay portal | LAN/Tailscale only (not router-forwarded) |
| 28336 | BCH wallet RPC | No |
| 28338 | BTC rental stratum (1M diff, TCP) | Router-forwarded |
| 28337 | BCH rental stratum (1M diff, TCP) | Router-forwarded |
| 28338 | BTC rental stratum (1M diff, TCP) | Router-forwarded |
| 28337 | BCH rental stratum (1M diff, TCP) | Router-forwarded |

Domains: `bitaxermt.xyz` (BTC, `@` A record), `bch.bitaxermt.xyz` (BCH) → `81.107.193.88`  
IP direct: `http://81.107.193.88` (HTTPS on bare IP fails — no Let's Encrypt certs for IPs)

Router port forwards:
- `192.168.0.200:28337-28338` → `28337-28338` TCP (BCH + BTC rental)

## ckpool Config

`btcd.notify: true` deployed (ZMQ instant block notifications). Rental ports in listener config.

| Setting | BTC | BCH |
|---|---|---|
| Stratum port | 28334 | 28335 |
| Rental stratum port | 28338 (1M startdiff) | 28337 (1M startdiff) |
| mindiff | 5600 | 5600 |
| startdiff | 12500 | 12500 |
| maxdiff | 250000 | 250000 |
| update_interval | 30s | 30s |
| notify | true (ZMQ) | true (ZMQ) |
| clean_jobs | false | false |
| ZMQ | bitcoind:28335 | bchn:28334 |

`maxdiff: 250000` on main port prevents rental boot-loop (CoinRider requires diff >= 1M). Rental port uses `maxdiff: 10000000` (10M).  
`notify: true` uses ZMQ for instant block notifications — without it, ckpool polls via RPC every `update_interval`, causing 30s stale share window when a new block is found. **Deployed on both pools.**  
`clean_jobs: false` keeps old stratum jobs valid — miners' shares from previous jobs are still accepted, zero stales from normal job rotation. Only actual block template changes (new block found) cause stales. Only actual block template changes (new block found) cause stales.

## Gotchas

- **ckpool entrypoint** monitors config checksum every 2s — can fail silently. Run `docker restart public-ckpool-btc` / `public-ckpool-bch` to force reload
- **ckpool image ENTRYPOINT is `ckpool`** not `/bin/sh` — must use `--entrypoint /bin/sh` and pass `/entrypoint.sh` as CMD. Without this override, ckpool runs with no args and fails with "unable to open ckpool.conf"
- **Bitaxe firmware v2.14.0-dirty** lacks TLS — use `stratum+tcp://` not `stratum+tls://`
- **Three firmware API versions**: v2.6.5 (strings, no `networkDifficulty`/`hashRate_1m`/`hashrateMonitor`), v2.12.2 (numeric, all fields), v2.14.0 (numeric, all fields)
- **ASIC Domains**: v2.12.2+ exposes `hashrateMonitor.asics[].domains` as *bare float arrays*, not objects. Backend normalizes to `{hr, frequency, errorCount}` for JS bar rendering
- **Network difficulty colour**: v2.6.5 ASICs fetch ~125T from pool API (`_NETWORK_DIFF_CACHE` populated at startup). `or 1` fallback prevents red false-positives
- **Colour thresholds**: session_best >= 100M → blue, >= 1G → gold, >= network difficulty (~125T) → red (block)
- **BCH wallet**: `zquestz/bitcoin-cash-node` in wallet-only mode (`listen=0`, `connect=0`). Wallet auto-loaded via `wallet=publicpool-bch` in `bitcoin.conf`. Health check uses `-rpcwallet=publicpool-bch`
- **BCH payout daemon**: dual RPC — `rpc_node()` for blockchain (bchn container), `rpc_wallet()` for wallet ops (public-bch-wallet container)
- **Payout daemon** writes `payout.status` (not `pool.status`) — ckpool overwrites `pool.status` on its own cycle
- **Benchmark refactored**: hill-climbing algorithm (94% expected vs actual), not brute-force sweep. Expected hashrate displayed in progress
- **Private pools** (axebtc/axebch/axebc2) are managed by 5tratumOS — do NOT edit their configs directly
- **BM1370 version rolling**: 20-bit hardware ceiling (mask `1ffffe00`); register 0xA4 stores 16-bit `versions_to_roll = mask >> 13`; `1ffffe00 >> 13 = 0xFFFF` saturates it
- **Dashboard frontend**: vanilla JS, no frameworks, CSS variables for neon theme. Stats refresh every 15s. Single HTML file — no build step
- **BOOSTED badge**: When BOOSTER is active, `⚡ BOOSTED` badge appears next to pool title + green `↗` arrow on BOOSTER card. `updateBoosterCard()` toggles `boosted-badge` and `booster-arrow` display
- **ABooster password**: Rental ports use `ABooster` as stratum password convention (ckpool doesn't enforce — all passwords accepted). Documented for internal reference only, not on public dashboard
- **File injection**: SCP to remote → `docker exec -i sh -c "cat > ..." < /tmp/file` (docker cp silently fails for persistence)
- **Search space at 5 TH/s / 210s**: covers ~23% of 2^52 space; need ~900s for 100%
- **Round shares before first block**: `round_start_shares` saved in ledger at daemon start captures ALL accumulated shares. Before any block is found (`pending == []`), `compute_round_shares()` is NOT applied — full accumulation shown. After first block, subtraction kicks in for proper round boundaries. Guard in both `get_round_info()` and `get_address_info()`: `if ledger.get("pending", []):`
- **`read_all_workers()` vs `read_workers()`**: `read_all_workers()` reads ckpool JSON without 300s inactivity filter — used for round-share calculations. `read_workers()` (filtered, active-only) used for live hashrate display and leaderboard
- **Round Shares panel**: `/api/round` endpoint returns all addresses' round-accumulated shares, percentages, estimated PROP; "ROUND" button on leaderboard opens slide-out panel. Round shares only reset on block find (or daemon restart)
- **Party Best Share refresh**: `index.html` line 347 has `if (!lastAddr)` guard that prevents `stat-bestshare` from updating when viewing an address. If a worker finds a new best share, the party best share stat stays stale until page refresh. Fix: remove the guard so party best share always updates on every refresh cycle regardless of `lastAddr`
- **Rental ports (28338/28337)**: 1M startdiff/mindiff, 10M maxdiff. Rentals mine to BOOSTER address — already excluded from leaderboard, round shares, and worker listings. Rental hashrate included in pool total hashrate. Router-forwards required.
- **ckpool-solo only binds port 3333** — the `listener` array's `port` field is ignored by this fork. Second listener entries do NOT bind. Second ckpool container required for separate port with different difficulty.
- **Booster worker data merge**: `read_worker_data()` (payout) and `read_workers()`/`read_all_workers()` (dashboard) read from BOTH `www/users` and `www-booster/users` directories, merging by workername (sum shares, max bestshare/lastshare). Same workername in both dirs = shares get summed.

## Dashboard Frontend

- Vanilla JS, inline styles, CSS variables for neon theme
- Stats auto-refresh every 15s via `loadStats()` + `lookup(lastAddr)`
- `fmtHr()`, `fmtCompact()`, `fmtUSD()` formatting helpers
- Worker table sorted alphabetically via `localeCompare()`
- Share count from ASIC API (`shares_accepted`), accumulated difficulty in parens
- Workers inactive >300s filtered from display
- Address lookup swaps hero stats from pool-wide to user-specific
- Block toast: hidden by default, shown when `best_session_diff >= network_diff`, dismiss-on-click

## Bitaxe Mobile App (Kotlin, sideload APK)

```
┌─────────────────────────────────────────────────────┐
│  Android App (Kotlin)                               │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ QR Scan  │→ │ Pair API │→ │ WebView + SSE     │  │
│  │ (ML Kit) │  │ exchange │  │ (real-time dash)  │  │
│  └──────────┘  └──────────┘  └───────────────────┘  │
│  ┌──────────────────────────────────────────────┐   │
│  │ FCM Service (block/overheat push alerts)     │   │
│  └──────────────────────────────────────────────┘   │
└────────────────────┬────────────────────────────────┘
                     │ Tailscale (100.x.x.x)
┌────────────────────▼────────────────────────────────┐
│  Server (Flask — bitaxe_dashboard.py additions)     │
│  ┌─────────┐ ┌──────────┐ ┌─────┐ ┌────────────┐  │
│  │/api/pair│ │/api/sse  │ │/auth│ │/api/fcm    │  │
│  │QR +token│ │SSE stream│ │middl│ │push notify │  │
│  └─────────┘ └──────────┘ └─────┘ └────────────┘  │
│  Tailscale IP: auto-detected via `tailscale ip -4`  │
└─────────────────────────────────────────────────────┘
```

### Server-Side Additions (Phase 1)

| Endpoint | Purpose |
|---|---|
| `GET /api/pair/generate` | Returns `{host, port, token, qr_data_url}` — Tailscale IP auto-detected |
| `POST /api/pair/exchange` | App sends token, gets back `{session_key}` |
| `GET /api/events` | SSE stream — events: `hashrate`, `block_found`, `overheat`, `throttle` |
| `POST /api/fcm/register` | App registers FCM token for push notifications |
| `POST /api/pair/revoke` | Revoke a paired device by session key |
| `GET /api/pair/devices` | List all paired devices |

- Auth middleware: `Authorization: Bearer <session_key>` header on all `/api/*` (pair endpoints exempt)
- Session keys stored in `data/paired_devices.json` (revocable)
- FCM tokens stored in `data/fcm_tokens.json`
- Push triggers: block found (`best_session_diff >= network_diff`), overheat (`temp >= THROTTLE_HIGH`)
- QR payload: `bitaxe://pair?host=<tailscale_ip>&port=5050&token=<otp>`
- Tailscale IP: `subprocess.check_output(["tailscale", "ip", "-4"])` — fallback to LAN IP if unavailable
- Dependencies: `firebase-admin`, `qrcode` (added to Dockerfile pip install)

### Android App (Phase 2)

- Package: `xyz.bitaxermt.dashboard`
- Min SDK: 26 (Android 8.0)
- Distribution: sideload APK (not Play Store)
- Key components:

| Component | Library | Purpose |
|---|---|---|
| QR Scanner | ML Kit Barcode Scanning | CameraX + barcode detection |
| WebView | Android WebView | Loads dashboard, injects auth header |
| SSE | OkHttp + coroutines | Persistent connection, auto-reconnect |
| Push | Firebase Cloud Messaging | Block/overheat alerts |
| Storage | EncryptedSharedPreferences | Session key, host, port |
| Build | Gradle KTS | APK output for sideload |

### Pair Flow

1. Server generates one-time token (HMAC, 5-min expiry) + QR code
2. Settings UI shows "Pair Device" button → displays QR inline
3. App scans QR → POSTs token to `/api/pair/exchange` → gets session key
4. Session key stored in EncryptedSharedPreferences
5. WebView loads `http://<tailscale_ip>:5050` with `Authorization: Bearer <key>` header
6. SSE connection established for real-time updates
7. FCM token registered for push notifications

## BOOSTER

Pool-operated mining address that redistributes all earnings (PROP + finder bonus) proportionally to all users.

| Property | BTC | BCH |
|---|---|---|
| Address | `bc1qp38nkx5upgx0h0ja2608kqf7wgfkmzfgljshke` | `bitcoincash:qr2t46sjw4hqdggvxszkadgr39kge6x9a5dvltpneq` |
| Label | `booster` | `booster` |

- **PROP calc**: user shares / total_user_shares (booster shares excluded from denominator — users are not diluted)
- **Finder bonus**: if BOOSTER finds block → finder_bonus × (shares_i / total_user) for each user (instead of single finder)
- **Dashboard card**: red dot (inactive) / green dot (active), hashrate, workers, best share
- **Theme switch**: `.booster-active` class on `<body>` turns all accents (cyan/magenta/amber) to `#00ff88`
- **Block badge**: blocks found by BOOSTER show a "BOOSTER 🏆" badge in the finder column
- **API**: `d.booster` object with `hashrate`, `shares`, `workers`, `worker_count`, `active`, `bestshare`, `bestshare_worker`, `share_pct`
- **Booster excluded** from `address_leaderboard` (not a real user)
- **Booster `booster_found`** passed in block data payload

### Deployment

```bash
# Files to edit locally then SCP + sudo-cp:
# BTC: payout.py, dashboard/server.py, dashboard/index.html
# BCH: payout.py, dashboard/server.py, dashboard/index.html
# Local working copies:
#   /tmp/opencode/booster/btc/  (BTC payout.py, server.py, index.html)
#   /tmp/opencode/booster/bch/  (BCH payout.py, server.py, index.html)
# Restart after deploy:
sudo systemctl restart publicpool-payout
sudo systemctl restart publicpool-bch-payout
sudo systemctl restart publicpool-dashboard
sudo systemctl restart publicpool-bch-dashboard

# IMPORTANT: ckpool-solo only binds port 3333 — listener port config is ignored.
# BOOSTER rental ports use a SECOND ckpool container per coin with separate config.
# Correct docker runs:
docker run -d --name public-ckpool-btc --restart unless-stopped \
  --network 5tratumos-axebtc_default --network-alias ckpool \
  --entrypoint /bin/sh \
  -v /var/lib/5tratumos/apps/publicpool/data/pool/config:/config:ro \
  -v /var/lib/5tratumos/apps/publicpool/data/pool/www:/www \
  -v /var/lib/5tratumos/apps/publicpool/entrypoint.sh:/entrypoint.sh:ro \
  -p 28334:3333 \
  ghcr.io/willitmod/docker-ckpool-solo:590fb2a \
  /entrypoint.sh

docker run -d --name public-ckpool-btc-booster --restart unless-stopped \
  --network 5tratumos-axebtc_default \
  --entrypoint /bin/sh \
  -v /var/lib/5tratumos/apps/publicpool/data/pool/config/ckpool-booster.conf:/config/ckpool.conf:ro \
  -v /var/lib/5tratumos/apps/publicpool/data/pool/www-booster:/www-booster \
  -v /var/lib/5tratumos/apps/publicpool/booster-entrypoint.sh:/entrypoint.sh:ro \
  -p 28338:3333 \
  ghcr.io/willitmod/docker-ckpool-solo:590fb2a \
  /entrypoint.sh

docker run -d --name public-ckpool-bch --restart unless-stopped \
  --network 5tratumos-axebch_default \
  --entrypoint /bin/sh \
  -v /var/lib/5tratumos/apps/publicpool-bch/data/pool/config:/config:ro \
  -v /var/lib/5tratumos/apps/publicpool-bch/data/pool/www:/www \
  -v /var/lib/5tratumos/apps/publicpool-bch/entrypoint.sh:/entrypoint.sh:ro \
  -p 28335:3333 \
  ghcr.io/willitmod/docker-ckpool-solo:590fb2a \
  /entrypoint.sh

docker run -d --name public-ckpool-bch-booster --restart unless-stopped \
  --network 5tratumos-axebch_default \
  --entrypoint /bin/sh \
  -v /var/lib/5tratumos/apps/publicpool-bch/data/pool/config/ckpool-booster.conf:/config/ckpool.conf:ro \
  -v /var/lib/5tratumos/apps/publicpool-bch/data/pool/www-booster:/www-booster \
  -v /var/lib/5tratumos/apps/publicpool-bch/booster-entrypoint.sh:/entrypoint.sh:ro \
  -p 28337:3333 \
  ghcr.io/willitmod/docker-ckpool-solo:590fb2a \
  /entrypoint.sh
```

Deploy order: SCP files → sudo-cp → restart dashboard(s) + payout(s) in the right dependency order (payout first, then dashboard if both changed). Verify with `curl -s http://localhost:{8080,8081}/api/round | python3 -m json.tool`.

### Booster ckpool configs

Each booster ckpool has its own config file at `ckpool-booster.conf` (e.g. `/var/lib/5tratumos/apps/publicpool/data/pool/config/ckpool-booster.conf`) with:
- `btcaddress` set to the BOOSTER address
- `mindiff/startdiff: 1000000`, `maxdiff: 10000000`
- Separate `www` dir (`/www-booster`) — dashboard/payout code reads from BOTH main and booster user dirs via `read_worker_data()` (payout) and `read_workers()`/`read_all_workers()` (dashboard), merging worker data and summing shares for identical workernames.
- Uses the same bitcoind/bitcoincashd node as the main ckpool
