#!/usr/bin/env python3
"""
Public Pool Payout Daemon
PROP + 25% finder's fee, 1% pool fee, 101-conf maturity queue.
"""
import json, os, time, subprocess, sys, logging, traceback

POOL_ADDRESS = "bc1qq4qw93k7n42wqf0srtxytnhgzgq09uamsrrk8w"
OPERATOR_ADDRESS = "bc1qtyn8yjwqa7g0dvf9stn83p2dxxqqtnjhqrnvrm"
BOOSTER_ADDRESS = "bc1qp38nkx5upgx0h0ja2608kqf7wgfkmzfgljshke"
DATA_DIR = "/var/lib/5tratumos/apps/publicpool/data"
WWW_DIR = os.path.join(DATA_DIR, "pool/www")
USERS_DIR = os.path.join(WWW_DIR, "users")
BOOSTER_USERS_DIR = os.path.join(DATA_DIR, "pool/www-booster/users")
LEDGER_FILE = os.path.join(DATA_DIR, "payout_ledger.json")
POOL_STATUS_FILE = os.path.join(DATA_DIR, "payout.status")

DOCKER = "/usr/bin/docker"
BTC_CLI = [DOCKER, "exec", "5tratumos-axebtc-bitcoind-1",
           "bitcoin-cli", "-rpcuser=btc",
           "-rpcpassword=nSdNSDfJY9-PQuajGEMCwaUc", "-rpcport=28332",
           "-rpcwallet=publicpool"]

POOL_FEE = 0.01
FINDER_FEE = 0.25
PROP_SHARE = 1.0 - POOL_FEE - FINDER_FEE
CONFIRMATIONS_NEEDED = 101
POLL_INTERVAL = 15
HEARTBEAT_INTERVAL = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/publicpool-payout.log")
    ]
)
log = logging.getLogger("payout")


def rpc(method, *args):
    def fmt(a):
        if isinstance(a, bool): return "true" if a else "false"
        return str(a)
    cmd = BTC_CLI + [method] + [fmt(a) for a in args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("RPC error %s: %s", method, r.stderr.strip())
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout.strip()


def read_worker_data():
    workers = {}
    def _read_from(users_dir):
        if not os.path.isdir(users_dir):
            return
        for fname in os.listdir(users_dir):
            path = os.path.join(users_dir, fname)
            try:
                with open(path) as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            for w in data.get("worker", []):
                wn = w["workername"]
                wdata = {
                    "shares": w["shares"],
                    "bestshare": w.get("bestshare", 0),
                    "lastshare": w.get("lastshare", 0),
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


def load_ledger():
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return {"pending": [], "last_height": 0, "snapshots": {}}


def save_ledger(ledger):
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


def get_chain_tip():
    info = rpc("getblockchaininfo")
    if info:
        return info.get("blocks", 0)
    return 0


def check_new_block(ledger):
    balance = rpc("getbalance")
    if balance is None:
        return ledger, None
    last_bal = ledger.get("last_balance", 0.0)
    if balance <= last_bal:
        return ledger, balance
    reward = balance - last_bal
    if reward < 1.0:
        return ledger, balance
    utxos = rpc("listunspent")
    if not utxos:
        return ledger, balance
    utxos.sort(key=lambda u: u.get("confirmations", 999))
    newest = utxos[-1]
    txid = newest.get("txid", "")
    txinfo = rpc("gettransaction", txid)
    if not txinfo:
        return ledger, balance
    block_hash = txinfo.get("blockhash", "")
    if not block_hash:
        return ledger, balance
    block_info = rpc("getblock", block_hash)
    if not block_info:
        return ledger, balance
    height = block_info.get("height", 0)
    log.info("Block found! Height: %d, Reward: %.8f, TX: %s", height, reward, txid)
    return ledger, balance, reward, height, block_hash


def identify_finder(before, after, net_diff):
    candidates = []
    for wname, a in after.items():
        b = before.get(wname, {"bestshare": 0})
        jump = a["bestshare"] - b["bestshare"]
        if jump >= net_diff * 0.9:
            candidates.append((jump, wname))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    for wname, a in after.items():
        b = before.get(wname, {"shares": 0})
        diff = a["shares"] - b["shares"]
        if diff > 0:
            candidates.append((diff, wname))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def calc_payouts(workers_before, workers_after, total_reward, finder):
    round_shares = {}
    for wname, a in workers_after.items():
        b = workers_before.get(wname, {"shares": 0})
        diff = a["shares"] - b["shares"]
        if diff > 0:
            round_shares[wname] = diff
    if not round_shares:
        return {}, 0.0, False
    user_shares = {wn: s for wn, s in round_shares.items()
                   if not wn.lower().startswith(BOOSTER_ADDRESS.lower())}
    booster_shares = {wn: s for wn, s in round_shares.items()
                      if wn.lower().startswith(BOOSTER_ADDRESS.lower())}
    total_user = sum(user_shares.values())
    if total_user == 0:
        return {}, 0.0, False
    pool_fee = round(total_reward * POOL_FEE, 8)
    remaining = total_reward - pool_fee
    finder_bonus = round(remaining * FINDER_FEE, 8)
    prop_pool = round(remaining - finder_bonus, 8)
    booster_found = finder and finder.lower().startswith(BOOSTER_ADDRESS.lower())
    payouts = {}
    for wname, shares in user_shares.items():
        prop = (shares / total_user) * prop_pool
        if booster_found:
            bonus = (shares / total_user) * finder_bonus
        else:
            bonus = finder_bonus if wname == finder else 0
        amt = round(prop + bonus, 8)
        if amt >= 0.0001:
            payouts[wname] = amt
    return payouts, pool_fee, booster_found


def execute_payout(pending, chain_height):
    if pending["paid"]:
        return False
    since = chain_height - pending["height"]
    if since < CONFIRMATIONS_NEEDED:
        return False
    outputs = {}
    total_out = 0.0
    fee_est = 0.00003
    for wname, amt in pending["payouts"].items():
        addr = wname.split(".")[0]
        outputs[addr] = amt
        total_out += amt
    if pending["pool_fee"] > 0:
        outputs[OPERATOR_ADDRESS] = pending["pool_fee"]
        total_out += pending["pool_fee"]
    if not outputs:
        return False
    adjusted = {}
    for addr, amt in outputs.items():
        fee_share = round((amt / total_out) * fee_est, 8)
        final = round(amt - fee_share, 8)
        if final >= 0.0001:
            adjusted[addr] = final
    if not adjusted:
        return False
    log.info("Executing payout for block %d: %s", pending["height"], json.dumps(adjusted, indent=2))
    result = rpc("sendmany", "", json.dumps(adjusted))
    if result:
        pending["paid"] = True
        pending["txid"] = result
        pending["paid_at_height"] = chain_height
        log.info("Payout txid: %s", result)
        return True
    log.error("sendmany failed for block %d", pending["height"])
    return False


def update_pool_status(ledger):
    info = rpc("getblockchaininfo")
    net_diff = info.get("difficulty", 0) if info else 0
    net_height = info.get("blocks", 0) if info else 0
    balance = rpc("getbalance") or 0
    workers = read_worker_data()
    total_shares = sum(w["shares"] for w in workers.values())
    worker_count = len(workers)
    pending_count = sum(1 for p in ledger["pending"] if not p["paid"])
    paid_count = sum(1 for p in ledger["pending"] if p["paid"])
    status = {
        "runtime": int(time.time()),
        "workers": worker_count,
        "hashrate": 0,
        "balance": balance,
        "blocks_found": paid_count,
        "pending_payouts": pending_count,
        "network_difficulty": net_diff,
        "network_height": net_height,
        "pool_address": POOL_ADDRESS,
        "operator_address": OPERATOR_ADDRESS,
        "pool_fee_pct": POOL_FEE * 100,
        "finder_fee_pct": FINDER_FEE * 100,
        "maturity_blocks": CONFIRMATIONS_NEEDED,
    }
    try:
        os.makedirs(os.path.dirname(POOL_STATUS_FILE), exist_ok=True)
        with open(POOL_STATUS_FILE, "w") as f:
            json.dump(status, f)
    except Exception as e:
        log.warning("Failed to write pool status: %s", e)


def main():
    log.info("=== Public Pool Payout Daemon ===")
    log.info("Pool: %s | Operator: %s | Fee: %.1f%% | Finder: %.0f%%",
             POOL_ADDRESS, OPERATOR_ADDRESS, POOL_FEE * 100, FINDER_FEE * 100)
    ledger = load_ledger()
    chain_height = get_chain_tip()
    last_heartbeat = 0
    workers_before = read_worker_data()
    ledger.setdefault("last_balance", 0.0)
    if "round_start_shares" not in ledger:
        ledger["round_start_shares"] = {wn: w for wn, w in workers_before.items()}

    while True:
        try:
            now = time.time()
            chain_height = get_chain_tip()

            result = check_new_block(ledger)
            if len(result) == 6:
                ledger, balance, reward, height, block_hash = result
                workers_after = read_worker_data()
                info = rpc("getblockchaininfo")
                net_diff = info.get("difficulty", 0) if info else 0
                finder = identify_finder(workers_before, workers_after, net_diff)
                log.info("Finder: %s", finder or "unknown")
                payouts, pool_fee, booster_found = calc_payouts(workers_before, workers_after, reward, finder)
                entry = {
                    "height": height,
                    "block_hash": block_hash,
                    "reward": reward,
                    "pool_fee": pool_fee,
                    "finder": finder or "",
                    "booster_found": booster_found,
                    "payouts": payouts,
                    "paid": False,
                    "txid": None,
                    "paid_at_height": None,
                }
                ledger["pending"].append(entry)
                ledger["last_balance"] = balance
                log.info("Queued payout for block %d: %.8f BTC (%d workers, %.8f fee)",
                         height, reward, len(payouts), pool_fee)
                workers_before = workers_after
                ledger["round_start_shares"] = {wn: w for wn, w in workers_before.items()}

            elif len(result) == 2:
                ledger, balance = result
                ledger["last_balance"] = balance
            else:
                result, balance = result[0], result[1] if len(result) > 1 else 0

            for p in ledger["pending"]:
                execute_payout(p, chain_height)

            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                pending = sum(1 for p in ledger["pending"] if not p["paid"])
                paid = sum(1 for p in ledger["pending"] if p["paid"])
                log.info("Heartbeat - height=%d balance=%.8f pending=%d paid=%d workers=%d",
                         chain_height, ledger.get("last_balance", 0), pending, paid, len(workers_before))
                last_heartbeat = now

            save_ledger(ledger)
            update_pool_status(ledger)
            time.sleep(POLL_INTERVAL)

        except Exception as e:
            log.error("Error: %s\n%s", e, traceback.format_exc())
            time.sleep(POLL_INTERVAL * 2)


if __name__ == "__main__":
    main()
