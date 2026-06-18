#!/usr/bin/env python3
"""BCH Pool dashboard API server."""

import http.server
import json
import os
import subprocess
import time
import urllib.parse
import re
import urllib.request

HOST = "0.0.0.0"
PORT = 8081

DATA_DIR = "/var/lib/5tratumos/apps/publicpool-bch/data"
POOL_STATUS_DIR = os.path.join(DATA_DIR, "pool/www/pool")
USERS_DIR = os.path.join(DATA_DIR, "pool/www/users")
LEDGER_FILE = os.path.join(DATA_DIR, "payout_ledger.json")
HTML_FILE = "/var/lib/5tratumos/apps/publicpool-bch/dashboard/index.html"

DOCKER = "/usr/bin/docker"
BCH_NODE_CLI = [DOCKER, "exec", "5tratumos-axebch-bchn-1",
                "bitcoin-cli", "-rpcuser=bch",
                "-rpcpassword=sLcQX8qcj-H7pnt3B_5dxLin", "-rpcport=28332"]
BCH_WALLET_CLI = [DOCKER, "exec", "public-bch-wallet",
                  "bitcoin-cli", "-datadir=/data",
                  "-rpcwallet=publicpool-bch"]

POOL_ADDRESS = "bitcoincash:qpmty3tp5mn4c9m9shtm62ztc27hgxxv8u6pe22s33"
OPERATOR_ADDRESS = "bitcoincash:qpkwxxwz9qg9muskdtj6j503wadqpy6yhqy2r60x89"
BOOSTER_ADDRESS = "bitcoincash:qr2t46sjw4hqdggvxszkadgr39kge6x9a5dvltpneq"
BOOSTER_USERS_DIR = os.path.join(DATA_DIR, "pool/www-booster/users")
POOL_DIFF = 5600
CACHE_TTL = 10
ASIC_IPS = {
    "101": {"name": "Gamma"},
    "103": {"name": "Supra"},
    "104": {"name": "Ultra1"},
    "105": {"name": "Ultra2"},
    "106": {"name": "Ultra3"},
    "107": {"name": "Ultra4"},
    "108": {"name": "Ultra5"},
}

_cache = {"pool": {}, "workers": {}, "blocks": [], "ledger": {}, "rpc": {}, "bch_price": 0, "time": 0}


def rpc_node(method, *args):
    cmd = BCH_NODE_CLI + [method] + [str(a) for a in args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout.strip()


def rpc_wallet(method, *args):
    cmd = BCH_WALLET_CLI + [method] + [str(a) for a in args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout.strip()


def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return None


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def fetch_bch_price():
    try:
        req = urllib.request.Request(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin-cash&vs_currencies=usd",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            return data.get("bitcoin-cash", {}).get("usd", 0)
    except Exception:
        return _cache.get("bch_price", 0)


def read_pool_status():
    data = {}
    path = os.path.join(POOL_STATUS_DIR, "pool.status")
    content = read_file(path)
    if not content:
        return data
    for line in content.strip().split("\n"):
        try:
            data.update(json.loads(line))
        except json.JSONDecodeError:
            pass
    return data


def fetch_asic_shares():
    result = {}
    for ip_suffix, info in ASIC_IPS.items():
        try:
            req = urllib.request.Request(f"http://192.168.0.{ip_suffix}/api/system/info",
                headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as r:
                d = json.loads(r.read())
            result[info["name"]] = {
                "shares_accepted": d.get("sharesAccepted", 0),
                "shares_rejected": d.get("sharesRejected", 0),
                "hashrate": d.get("hashRate", 0),
                "diff": d.get("stratumSuggestedDifficulty", d.get("poolDifficulty", 0)),
            }
        except Exception:
            pass
    return result


def read_workers():
    workers = {}
    now = time.time()
    def _read_from(users_dir):
        if not os.path.isdir(users_dir):
            return
        for fname in os.listdir(users_dir):
            data = read_json(os.path.join(users_dir, fname))
            if not data:
                continue
            for w in data.get("worker", []):
                lastshare = int(w.get("lastshare", 0))
                if now - lastshare > 300:
                    continue
                wn = w["workername"]
                acc = int(w.get("shares", 0))
                wdata = {
                    "shares": acc,
                    "submissions": acc // POOL_DIFF,
                    "bestshare": int(w.get("bestshare", 0)),
                    "lastshare": lastshare,
                    "hashrate1m": parse_hashrate(w.get("hashrate1m", "0")),
                    "hashrate5m": parse_hashrate(w.get("hashrate5m", "0")),
                }
                if wn in workers:
                    workers[wn]["shares"] += wdata["shares"]
                    workers[wn]["bestshare"] = max(workers[wn]["bestshare"], wdata["bestshare"])
                    workers[wn]["lastshare"] = max(workers[wn]["lastshare"], wdata["lastshare"])
                    workers[wn]["hashrate1m"] += wdata["hashrate1m"]
                    workers[wn]["hashrate5m"] += wdata["hashrate5m"]
                else:
                    workers[wn] = wdata
    _read_from(USERS_DIR)
    _read_from(BOOSTER_USERS_DIR)
    return workers

def read_all_workers():
    workers = {}
    def _read_from(users_dir):
        if not os.path.isdir(users_dir):
            return
        for fname in os.listdir(users_dir):
            data = read_json(os.path.join(users_dir, fname))
            if not data:
                continue
            for w in data.get("worker", []):
                wn = w["workername"]
                acc = int(w.get("shares", 0))
                wdata = {
                    "shares": acc,
                    "submissions": acc // POOL_DIFF,
                    "bestshare": int(w.get("bestshare", 0)),
                    "lastshare": int(w.get("lastshare", 0)),
                }
                if wn in workers:
                    workers[wn]["shares"] += wdata["shares"]
                    workers[wn]["bestshare"] = max(workers[wn]["bestshare"], wdata["bestshare"])
                    workers[wn]["lastshare"] = max(workers[wn]["lastshare"], wdata["lastshare"])
                else:
                    workers[wn] = wdata
    _read_from(USERS_DIR)
    _read_from(BOOSTER_USERS_DIR)
    return workers


def read_ledger():
    return read_json(LEDGER_FILE) or {"pending": [], "last_height": 0, "snapshots": {}}


def parse_hashrate(v):
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return 0.0
    v = v.strip().upper()
    m = re.search(r"^([0-9.]+)\s*([KMGTP]?)", v)
    if not m:
        return 0.0
    num = float(m.group(1))
    suffix = m.group(2)
    multipliers = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}
    return num * multipliers.get(suffix, 1)

def fetch_all():
    now = time.time()
    if now - _cache["time"] < CACHE_TTL:
        return
    pool = read_pool_status()
    workers = read_workers()
    ledger = read_ledger()
    asic_data = fetch_asic_shares()
    info = rpc_node("getblockchaininfo")
    net_hash = rpc_node("getnetworkhashps")
    balance = rpc_wallet("getbalance")
    price = fetch_bch_price()
    blocks = []
    for p in ledger.get("pending", []):
        blocks.append({
            "height": p["height"],
            "reward": p["reward"],
            "reward_usd": round(p["reward"] * price, 2),
            "finder": p.get("finder", "") or "unknown",
            "finder_bonus": round(p["reward"] * (1.0 - 0.01) * 0.25, 8),
            "pool_fee": p["pool_fee"],
            "pool_fee_usd": round(p["pool_fee"] * price, 2),
            "num_workers": len(p["payouts"]),
            "paid": p["paid"],
            "txid": p.get("txid"),
            "paid_at_height": p.get("paid_at_height"),
            "booster_found": p.get("booster_found", False),
        })
    blocks.sort(key=lambda b: b["height"], reverse=True)
    _cache.update({
        "pool": pool,
        "workers": workers,
        "asic_data": asic_data,
        "blocks": blocks,
        "ledger": ledger,
        "bch_price": price,
        "pool_diff": POOL_DIFF,
        "rpc": {
            "height": info.get("blocks", 0) if info else 0,
            "difficulty": info.get("difficulty", 0) if info else 0,
            "network_hashrate": round(net_hash, 2) if net_hash else 0,
            "balance": balance or 0,
            "balance_usd": round((balance or 0) * price, 2),
        },
        "time": now,
    })


def get_address_info(addr):
    fetch_all()
    workers = _cache["workers"]
    all_workers = read_all_workers()
    ledger = _cache["ledger"]
    if ledger.get("pending", []):
        round_start = ledger.get("round_start_shares", {})
        all_workers = compute_round_shares(all_workers, round_start)
    price = _cache["bch_price"]
    asic_data = _cache.get("asic_data", {})
    matched_workers = {}
    total_shares = 0
    total_submissions = 0
    addr_lower = addr.lower().split(".")[0]
    for wname, wdata in workers.items():
        wbase = wname.lower().split(".")[0]
        if wbase == addr_lower:
            matched_workers[wname] = wdata
            total_submissions += wdata["submissions"]
    for wname in matched_workers:
        wn = wname.split(".")[-1] if "." in wname else wname
        if wn in asic_data:
            matched_workers[wname]["asic"] = asic_data[wn]
    total_shares = sum(w["shares"] for wn, w in all_workers.items()
                      if wn.lower().split(".")[0] == addr_lower)
    all_shares_round = sum(w["shares"] for wn, w in all_workers.items()
                          if not wn.lower().split(".")[0] == BOOSTER_ADDRESS.lower()) or 1
    block_reward = 3.125
    prop_share = (1.0 - 0.01) * 0.75
    finder_share = 1.0 - 0.01
    user_ratio = total_shares / all_shares_round
    est_prop = round(user_ratio * block_reward * prop_share, 8)
    est_finder_bonus = round(block_reward * finder_share * 0.25, 8)
    est_if_find = round(est_prop + est_finder_bonus, 8)

    payout_history = []
    for p in ledger.get("pending", []):
        for wname, amt in p.get("payouts", {}).items():
            wbase = wname.lower().split(".")[0]
            if wbase == addr_lower:
                payout_history.append({
                    "block": p["height"],
                    "amount": amt,
                    "amount_usd": round(amt * price, 2),
                    "paid": p["paid"],
                    "txid": p.get("txid"),
                })
    est_pending = 0
    for p in ledger.get("pending", []):
        if not p["paid"]:
            for wname, wdata in matched_workers.items():
                wshares = wdata["shares"]
                prop_pool = p["reward"] * prop_share
                est = (wshares / all_shares_round) * prop_pool
                if wname == p.get("finder"):
                    est += p["reward"] * finder_share * 0.25
                est_pending += est
    return {
        "address": addr,
        "workers": matched_workers,
        "total_shares": total_shares,
        "total_submissions": total_submissions,
        "share_pct": round(user_ratio * 100, 4),
        "estimated_prop": est_prop,
        "estimated_prop_usd": round(est_prop * price, 2),
        "estimated_if_find": est_if_find,
        "estimated_if_find_usd": round(est_if_find * price, 2),
        "estimated_pending": round(est_pending, 8),
        "estimated_pending_usd": round(est_pending * price, 2),
        "total_paid": round(sum(h["amount"] for h in payout_history if h["paid"]), 8),
        "total_paid_usd": round(sum(h["amount"] for h in payout_history if h["paid"]) * price, 2),
        "payout_history": payout_history,
        "block_reward": block_reward,
        "pool_diff": POOL_DIFF,
    }


def compute_round_shares(all_workers, round_start):
    result = {}
    for wname, wdata in all_workers.items():
        start_info = round_start.get(wname, {"shares": 0})
        start_shares = start_info.get("shares", 0) if isinstance(start_info, dict) else 0
        rs = max(0, wdata["shares"] - start_shares)
        if rs > 0:
            result[wname] = dict(wdata)
            result[wname]["shares"] = rs
    return result

def get_round_info():
    fetch_all()
    all_workers = read_all_workers()
    ledger = _cache["ledger"]
    if ledger.get("pending", []):
        round_start = ledger.get("round_start_shares", {})
        all_workers = compute_round_shares(all_workers, round_start)
    price = _cache["bch_price"]
    block_reward = 3.125
    prop_share = (1.0 - 0.01) * 0.75
    addr_groups = {}
    for wname, wdata in all_workers.items():
        base = wname.split(".")[0].lower()
        if base == BOOSTER_ADDRESS.lower():
            continue
        if base not in addr_groups:
            addr_groups[base] = {"shares": 0, "worker_names": set()}
        addr_groups[base]["shares"] += wdata["shares"]
        label = wname.split(".")[-1] if "." in wname else wname
        addr_groups[base]["worker_names"].add(label)
    total_shares = sum(g["shares"] for g in addr_groups.values()) or 1
    entries = []
    for addr, info in addr_groups.items():
        pct = info["shares"] / total_shares
        entries.append({
            "address": addr,
            "workers": sorted(info["worker_names"]),
            "worker_count": len(info["worker_names"]),
            "shares": info["shares"],
            "pct": round(pct * 100, 4),
            "estimated_prop": round(pct * block_reward * prop_share, 8),
            "estimated_prop_usd": round(pct * block_reward * prop_share * price, 2),
        })
    entries.sort(key=lambda x: x["shares"], reverse=True)
    last_height = max((p["height"] for p in ledger.get("pending", [])), default=0)
    return {
        "total_shares": total_shares,
        "last_block_height": last_height,
        "entries": entries,
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.serve_html()
        elif path == "/api/pool":
            self.api_full()
        elif path == "/api/round":
            self.send_json(get_round_info())
        elif path.startswith("/api/address/"):
            addr = path[len("/api/address/"):]
            self.send_json(get_address_info(addr))
        else:
            self.send_error(404)

    def api_full(self):
        fetch_all()
        data = {
            "pool": {k: parse_hashrate(v) if k.startswith("hashrate") else v for k, v in _cache["pool"].items()},
            "workers": _cache["workers"],
            "asic_data": _cache.get("asic_data", {}),
            "blocks": _cache["blocks"],
            "rpc": _cache["rpc"],
            "bch_price": _cache["bch_price"],
            "pool_address": POOL_ADDRESS,
            "operator_address": OPERATOR_ADDRESS,
            "pool_diff": POOL_DIFF,
        }
        total_hr = sum(w.get("hashrate1m", 0) for w in data["workers"].values())
        BLOCK_REWARD = 3.125
        booster_hr = 0
        booster_shares = 0
        booster_workers = []
        booster_bestshare = 0
        booster_bestshare_worker = ""
        addr_groups = {}
        for wname, wdata in data["workers"].items():
            base = wname.split(".")[0].lower()
            if base == BOOSTER_ADDRESS.lower():
                booster_hr += wdata.get("hashrate1m", 0)
                booster_shares += wdata.get("shares", 0)
                label = wname.split(".")[-1] if "." in wname else wname
                booster_workers.append(label)
                bs = wdata.get("bestshare", 0)
                if bs > booster_bestshare:
                    booster_bestshare = bs
                    booster_bestshare_worker = label
                continue
            if base not in addr_groups:
                addr_groups[base] = {"hashrate": 0, "shares": 0, "submissions": 0, "worker_names": [], "bestshare": 0}
            addr_groups[base]["hashrate"] += wdata.get("hashrate1m", 0)
            addr_groups[base]["shares"] += wdata.get("shares", 0)
            addr_groups[base]["submissions"] += wdata.get("submissions", 0)
            label = wname.split(".")[-1] if "." in wname else wname
            addr_groups[base]["worker_names"].append(label)
            bs = wdata.get("bestshare", 0)
            if bs > addr_groups[base]["bestshare"]:
                addr_groups[base]["bestshare"] = bs
        total_all_shares = sum(g["shares"] for g in addr_groups.values())
        lb = []
        for addr, info in addr_groups.items():
            pct = info["shares"] / total_all_shares if total_all_shares > 0 else 0
            exp = pct * BLOCK_REWARD * 0.99 * 0.75
            lb.append({
                "address": addr,
                "short": addr[-4:],
                "hashrate": info["hashrate"],
                "shares": info["shares"],
                "submissions": info["submissions"],
                "workers": sorted(info["worker_names"]),
                "worker_count": len(info["worker_names"]),
                "pool_pct": round(pct * 100, 2),
                "expected_payout": round(exp, 8),
                "expected_payout_usd": round(exp * _cache.get("bch_price", 0), 2),
                "bestshare": info["bestshare"],
            })
        lb.sort(key=lambda x: x["expected_payout"], reverse=True)
        data["address_leaderboard"] = lb
        data["pool"]["hashrate1m"] = total_hr
        data["booster"] = {
            "hashrate": booster_hr,
            "shares": booster_shares,
            "workers": sorted(booster_workers),
            "worker_count": len(booster_workers),
            "active": len(booster_workers) > 0,
            "bestshare": booster_bestshare,
            "bestshare_worker": booster_bestshare_worker,
            "share_pct": round(booster_shares / (total_all_shares + booster_shares) * 100, 2) if (total_all_shares + booster_shares) > 0 else 0,
        }
        # unfiltered best share for the round (bypasses 300s idle filter)
        round_best = 0
        round_best_worker = ""
        def _scan_best(users_dir):
            nonlocal round_best, round_best_worker
            if not os.path.isdir(users_dir):
                return
            for fname in os.listdir(users_dir):
                try:
                    with open(os.path.join(users_dir, fname)) as f:
                        _data = json.load(f)
                except:
                    continue
                for w in _data.get("worker", []):
                    bs = int(w.get("bestshare", 0))
                    if bs > round_best:
                        round_best = bs
                        round_best_worker = w.get("workername", "")
        _scan_best(USERS_DIR)
        _scan_best(BOOSTER_USERS_DIR)
        data["pool"]["bestshare_round"] = round_best
        data["pool"]["bestshare_worker"] = round_best_worker
        self.send_json(data)

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def send_html(self, code=200, body=""):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def serve_html(self):
        content = read_file(HTML_FILE)
        if not content:
            self.send_error(500)
            return
        self.send_html(body=content)


def serve():
    srv = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"BCH Pool dashboard on {HOST}:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    serve()
