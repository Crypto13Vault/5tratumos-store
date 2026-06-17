#!/usr/bin/env python3
"""Bitaxe Dashboard - Web UI for monitoring and managing Bitaxe ASIC miners."""

import datetime
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from urllib.request import urlopen, Request, URLError
import urllib.parse

from flask import Flask, jsonify, render_template_string, request, Response, stream_with_context

import base64
import hashlib
import hmac
import io
import queue
import secrets
import subprocess

app = Flask(__name__)

# --- History Database ---
HISTORY_DB = os.environ.get("HISTORY_DB", "/app/data/history.db")
HISTORY_INTERVAL = int(os.environ.get("HISTORY_INTERVAL", "600"))

def _init_db():
    os.makedirs(os.path.dirname(HISTORY_DB), exist_ok=True)
    conn = sqlite3.connect(HISTORY_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS history(
        ts INTEGER, ambient REAL, ip TEXT, hostname TEXT,
        temp REAL, vr_temp REAL, frequency INTEGER,
        core_voltage INTEGER, hashrate REAL, power REAL, fan_rpm INTEGER
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_h_ts ON history(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_h_ip ON history(ip)")
    conn.commit()
    conn.close()
    print(f"[*] History DB: {HISTORY_DB}")

def _log_history():
    _init_db()
    discover_miners()
    while True:
        try:
            ambient = _fetch_current_ambient()
            miners = []
            for m in _discovered + STATIC_MINERS:
                d = fetch_bitaxe(m["ip"])
                if not d.get("error"):
                    d["ip"] = m["ip"]
                    miners.append(d)
            conn = sqlite3.connect(HISTORY_DB)
            now = int(time.time())
            rows = [(now, ambient, m["ip"], m.get("hostname",""),
                     m.get("temp",-1), m.get("vr_temp",-1),
                     m.get("frequency",0), m.get("core_voltage",0),
                     m.get("hashrate",0), m.get("power",0), m.get("fan_rpm",0))
                    for m in miners]
            if rows:
                conn.executemany("INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
                conn.commit()
                print(f"[*] History: logged {len(rows)} miners at ts={now} amb={ambient}°C", flush=True)
            conn.close()
        except Exception as e:
            print(f"[!] History log failed: {e}", flush=True)
        time.sleep(HISTORY_INTERVAL)

# --- Settings File ---
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/app/data/settings.json")

def load_settings():
    defaults = {
        "static_ips": "", "auto_discover": True, "scan_subnet": "192.168.0.0/24",
        "weather_lat": "51.53446", "weather_lon": "-2.54698", "ambient_offset": -1,
        "throttle_high": 59, "throttle_low": 52,
        "benchmark_duration": 180, "benchmark_sample_interval": 1, "benchmark_stabilize_time": 30,
    }
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
            defaults.update(data)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults

def save_settings(data):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

_settings = load_settings()

# --- Configuration ---
REFRESH_S = 30
SCAN_SUBNET = os.environ.get("SCAN_SUBNET", _settings.get("scan_subnet", "192.168.0.0/24"))
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "300"))
STATIC_MINERS_JSON = os.environ.get("STATIC_MINERS", "")

BITAXE_API_TIMEOUT = 5
SCAN_PORT = 80
SCAN_THREADS = 50

# --- Weather-Aware Config ---
WEATHER_LAT = os.environ.get("WEATHER_LAT", _settings.get("weather_lat", "51.53446"))
WEATHER_LON = os.environ.get("WEATHER_LON", _settings.get("weather_lon", "-2.54698"))
AMBIENT_OFFSET = int(os.environ.get("AMBIENT_OFFSET", str(_settings.get("ambient_offset", "-1"))))
_weather_forecast = {"max": None, "ts": 0, "error": None}
_weather_lock = threading.Lock()

# --- Network Difficulty Cache (for old firmware that lacks networkDifficulty field) ---
_NETWORK_DIFF_CACHE = {"btc": 0, "bch": 0, "ts": 0}
_NETWORK_DIFF_LOCK = threading.Lock()
POOL_HOST = os.environ.get("POOL_HOST", "192.168.0.200")

def _fetch_network_difficulties():
    try:
        # BTC
        r = urlopen(Request(f"http://{POOL_HOST}:8080/api/pool", headers={"User-Agent": "BitaxeDashboard/1.0"}), timeout=5)
        d = json.loads(r.read().decode())
        btc_diff = d.get("rpc", {}).get("difficulty", 0)
        # BCH
        r2 = urlopen(Request(f"http://{POOL_HOST}:8081/api/pool", headers={"User-Agent": "BitaxeDashboard/1.0"}), timeout=5)
        d2 = json.loads(r2.read().decode())
        bch_diff = d2.get("rpc", {}).get("difficulty", 0)
        with _NETWORK_DIFF_LOCK:
            _NETWORK_DIFF_CACHE["btc"] = btc_diff
            _NETWORK_DIFF_CACHE["bch"] = bch_diff
            _NETWORK_DIFF_CACHE["ts"] = time.time()
    except Exception as e:
        print(f"[!] Failed to fetch network difficulties: {e}", flush=True)

# Populate cache at startup
_fetch_network_difficulties()

STATIC_MINERS = []
if STATIC_MINERS_JSON:
    try:
        STATIC_MINERS = json.loads(STATIC_MINERS_JSON)
    except json.JSONDecodeError:
        pass
# Also add static IPs from settings file
_static_ips_from_settings = _settings.get("static_ips", "").strip()
if _static_ips_from_settings:
    for ip in re.split(r"[,\s]+", _static_ips_from_settings):
        ip = ip.strip()
        if ip and not any(m.get("ip") == ip for m in STATIC_MINERS):
            STATIC_MINERS.append({"ip": ip, "name": ip, "static": True})

# --- Auto-Throttle (per-ASIC profile-based temp regulation) ---
THROTTLE_HIGH = int(os.environ.get("THROTTLE_HIGH", str(_settings.get("throttle_high", "63"))))
THROTTLE_LOW = int(os.environ.get("THROTTLE_LOW", str(_settings.get("throttle_low", "48"))))
THROTTLE_RAMP_COOLDOWN = int(os.environ.get("THROTTLE_RAMP_COOLDOWN", "300"))
THROTTLE_OVERHEAT_COOLDOWN = int(os.environ.get("THROTTLE_OVERHEAT_COOLDOWN", "600"))
THROTTLE_PROFILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Documents", "JSON")

# --- Benchmark ---
BENCHMARK_MAX_VOLTAGE = int(os.environ.get("BENCHMARK_MAX_VOLTAGE", "1300"))
BENCHMARK_MAX_FREQUENCY = int(os.environ.get("BENCHMARK_MAX_FREQUENCY", "900"))
BENCHMARK_START_VOLTAGE = int(os.environ.get("BENCHMARK_START_VOLTAGE", "1150"))
BENCHMARK_START_FREQUENCY = int(os.environ.get("BENCHMARK_START_FREQUENCY", "500"))
BENCHMARK_DURATION = int(os.environ.get("BENCHMARK_DURATION", "180"))
BENCHMARK_SAMPLE_INTERVAL = int(os.environ.get("BENCHMARK_SAMPLE_INTERVAL", "1"))
BENCHMARK_STABILIZE_TIME = int(os.environ.get("BENCHMARK_STABILIZE_TIME", "30"))
BENCHMARK_FREQ_STEP = int(os.environ.get("BENCHMARK_FREQ_STEP", "25"))
BENCHMARK_VOLT_STEP = int(os.environ.get("BENCHMARK_VOLT_STEP", "20"))

_benchmark_running = set()
_benchmark_running_lock = threading.Lock()
_benchmark_progress = {}
_benchmark_progress_lock = threading.Lock()
# Cache ASIC core counts to avoid repeated fetches
_benchmark_asic_cache = {}

_throttle_profiles = {}
_throttle_state = {}
_throttle_loaded = False
_throttle_lock = threading.Lock()
_throttle_disabled = set()
_throttle_disabled_lock = threading.Lock()

# --- Mobile App: Pairing, Auth, SSE, FCM ---
_PAIR_TOKENS = {}
_PAIR_TOKENS_LOCK = threading.Lock()
_PAIR_TOKEN_TTL = 300  # 5 minutes

_PAIRED_DEVICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "paired_devices.json")
_PAIRED_DEVICES = {}
_PAIRED_DEVICES_LOCK = threading.Lock()

_FCM_TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fcm_tokens.json")
_FCM_TOKENS = {}
_FCM_TOKENS_LOCK = threading.Lock()

_SSE_CLIENTS = []
_SSE_LOCK = threading.Lock()

_PAIR_SECRET = secrets.token_hex(32)

_PUSHED_BLOCKS = set()
_PUSHED_OVERHEAT = {}
_PUSH_LOCK = threading.Lock()

def _tailscale_ip():
    env_ip = os.environ.get("TAILSCALE_IP", "").strip()
    if env_ip:
        return env_ip
    host_ip = os.environ.get("DASHBOARD_HOST", "").strip()
    if host_ip:
        return host_ip
    try:
        out = subprocess.check_output(["tailscale", "ip", "-4"], timeout=3, stderr=subprocess.DEVNULL).decode().strip()
        ts = out.split()[0] if out else None
        if ts:
            return ts
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan = s.getsockname()[0]
        s.close()
        return lan
    except Exception:
        return None

def _load_paired_devices():
    global _PAIRED_DEVICES
    with _PAIRED_DEVICES_LOCK:
        try:
            if os.path.exists(_PAIRED_DEVICES_FILE):
                with open(_PAIRED_DEVICES_FILE) as f:
                    _PAIRED_DEVICES = json.load(f)
        except Exception:
            _PAIRED_DEVICES = {}

def _save_paired_devices():
    with _PAIRED_DEVICES_LOCK:
        os.makedirs(os.path.dirname(_PAIRED_DEVICES_FILE), exist_ok=True)
        with open(_PAIRED_DEVICES_FILE, "w") as f:
            json.dump(_PAIRED_DEVICES, f, indent=2)

def _load_fcm_tokens():
    global _FCM_TOKENS
    with _FCM_TOKENS_LOCK:
        try:
            if os.path.exists(_FCM_TOKENS_FILE):
                with open(_FCM_TOKENS_FILE) as f:
                    _FCM_TOKENS = json.load(f)
        except Exception:
            _FCM_TOKENS = {}

def _save_fcm_tokens():
    with _FCM_TOKENS_LOCK:
        os.makedirs(os.path.dirname(_FCM_TOKENS_FILE), exist_ok=True)
        with open(_FCM_TOKENS_FILE, "w") as f:
            json.dump(_FCM_TOKENS, f, indent=2)

def _generate_pair_token():
    token = secrets.token_urlsafe(24)
    with _PAIR_TOKENS_LOCK:
        _PAIR_TOKENS[token] = time.time() + _PAIR_TOKEN_TTL
    return token

def _validate_pair_token(token):
    with _PAIR_TOKENS_LOCK:
        expiry = _PAIR_TOKENS.pop(token, None)
    if expiry and time.time() < expiry:
        return True
    return False

def _generate_session_key():
    return secrets.token_hex(24)

def _check_auth():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        key = auth[7:]
        with _PAIRED_DEVICES_LOCK:
            if key in _PAIRED_DEVICES:
                return True
    return False

def _sse_broadcast(event_type, data):
    payload = json.dumps({"type": event_type, "data": data})
    with _SSE_LOCK:
        dead = []
        for q in _SSE_CLIENTS:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _SSE_CLIENTS.remove(q)

_FCM_INITIALIZED = False

def _init_firebase():
    global _FCM_INITIALIZED
    if _FCM_INITIALIZED:
        return
    try:
        import firebase_admin
        cred_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "firebase-service-account.json")
        if not os.path.exists(cred_path):
            print(f"[!] Firebase: service account not found at {cred_path}")
            return
        cred = firebase_admin.credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        _FCM_INITIALIZED = True
        print("[*] Firebase initialized")
    except Exception as e:
        print(f"[!] Firebase init failed: {e}")

def _send_fcm_push(title, body, data=None):
    if not _FCM_INITIALIZED:
        return
    try:
        import firebase_admin.messaging as messaging
        with _FCM_TOKENS_LOCK:
            tokens = list(_FCM_TOKENS.keys())
        if not tokens:
            return
        msg = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data=data or {},
        )
        for token in tokens:
            msg.token = token
            try:
                messaging.send(msg)
                print(f"[*] FCM push sent: {title}", flush=True)
            except Exception as e:
                if "not registered" in str(e).lower() or "unregistered" in str(e).lower():
                    with _FCM_TOKENS_LOCK:
                        _FCM_TOKENS.pop(token, None)
                    _save_fcm_tokens()
                print(f"[!] FCM push failed: {e}", flush=True)
    except ImportError:
        pass  # firebase-admin not installed
    except Exception as e:
        print(f"[!] FCM push error: {e}", flush=True)

def _fetch_ambient_forecast():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}&longitude={WEATHER_LON}&daily=temperature_2m_max&timezone=auto"
        resp = urlopen(url, timeout=10)
        data = json.loads(resp.read().decode())
        today_max = data["daily"]["temperature_2m_max"][0]
        with _weather_lock:
            _weather_forecast["max"] = today_max
            _weather_forecast["ts"] = time.time()
            _weather_forecast["error"] = None
        print(f"[*] Weather forecast: today's high {today_max}°C")
    except Exception as e:
        with _weather_lock:
            _weather_forecast["error"] = str(e)
        print(f"[!] Weather fetch failed: {e}")

def _ambient_offset():
    if AMBIENT_OFFSET >= 0:
        return AMBIENT_OFFSET
    with _weather_lock:
        forecast = _weather_forecast["max"]
        if forecast is None:
            return 0
    if forecast >= 30:
        return 5
    if forecast >= 25:
        return 3
    if forecast >= 20:
        return 1
    return 0

def _fetch_current_ambient():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}&longitude={WEATHER_LON}&current=temperature_2m&timezone=auto"
        resp = urlopen(url, timeout=10)
        data = json.loads(resp.read().decode())
        return data["current"]["temperature_2m"]
    except Exception:
        return None

def _ensure_throttle_profiles():
    global _throttle_profiles, _throttle_loaded
    if _throttle_loaded:
        return
    with _throttle_lock:
        if _throttle_loaded:
            return
        _throttle_profiles = {}
        if os.path.isdir(THROTTLE_PROFILES_DIR):
            for fname in os.listdir(THROTTLE_PROFILES_DIR):
                if fname.endswith(".json"):
                    ip = fname.replace(".json", "")
                    m = re.search(r'(\d+\.\d+\.\d+\.\d+)', ip)
                    if m:
                        ip = m.group(1)
                    try:
                        with open(os.path.join(THROTTLE_PROFILES_DIR, fname)) as f:
                            data = json.load(f)
                        entries = sorted(data.get("all_results", []), key=lambda x: (x.get("frequency", 0), x.get("coreVoltage", 0)))
                        if entries:
                            _throttle_profiles[ip] = entries
                            _throttle_state[ip] = {"profile_idx": 0, "last_action": 0, "mode": "thermal", "cooldown_until": 0, "planned_idx": None, "real_hash": {}}
                    except Exception as e:
                        print(f"[!] Failed to load throttle profile for {ip}: {e}")
        _throttle_loaded = True
        print(f"[*] Auto-throttle: {len(_throttle_profiles)} ASICs with profiles loaded (all start at idx=0)")

def _apply_throttle(ip, current_temp, current_freq=0, current_volt=0, overheat_mode=0, current_hashrate=0):
    with _throttle_disabled_lock:
        if ip in _throttle_disabled:
            return
    if ip not in _throttle_profiles or current_temp < 0:
        print(f"[d] Throttle skip {ip}: {'not in profiles' if ip not in _throttle_profiles else 'bad temp'}", flush=True)
        return
    profiles = _throttle_profiles[ip]
    if len(profiles) < 2:
        print(f"[d] Throttle skip {ip}: only {len(profiles)} profile(s)", flush=True)
        return
    state = _throttle_state.get(ip)
    if not state:
        print(f"[d] Throttle skip {ip}: no state", flush=True)
        return
    now = time.time()
    idx = state["profile_idx"]
    mode = state.get("mode", "thermal")
    needs_restart = False
    cooldown = state.get("cooldown_until", 0)

    ambient = _ambient_offset()
    eff_high = THROTTLE_HIGH - ambient
    eff_low = THROTTLE_LOW - ambient

    # --- Hashrate collection (stable on current profile for >30s) ---
    if current_hashrate > 0 and idx >= 0:
        dwell = state.get("dwell_start", 0)
        if dwell == 0:
            state["dwell_start"] = now
        elif (now - dwell) > 30:
            rh = state["real_hash"].setdefault(idx, {"count": 0, "total": 0, "max": 0, "temp_sum": 0, "avg_temp": 0})
            rh["count"] += 1
            rh["total"] += current_hashrate
            if current_hashrate > rh["max"]:
                rh["max"] = current_hashrate
            rh["temp_sum"] += current_temp
            rh["avg_temp"] = rh["temp_sum"] / rh["count"]
            state["dwell_start"] = now

    # --- Overheat (hard safety) -> reset, restart, cooldown ---
    overheat = current_temp >= eff_high or overheat_mode == 1
    if overheat:
        mode = "thermal"
        needs_restart = True
        idx = 0
        cooldown = max(cooldown, now + THROTTLE_OVERHEAT_COOLDOWN)

    # --- First ever cycle: always apply idx=0 (no restart) ---
    if state["last_action"] == 0 and not overheat:
        idx = 0
        needs_restart = False

    # --- Real-hashrate optimizer (pick best proven profile within safe temp) ---
    if not overheat and state["last_action"] > 0 and now >= cooldown:
        safe_max_temp = eff_high - 3
        candidates = [(i, rh["max"]) for i, rh in state["real_hash"].items()
                      if rh["count"] >= 3 and rh["avg_temp"] <= safe_max_temp]
        if candidates:
            candidates.sort(key=lambda x: -x[1])
            best = candidates[0][0]
            if best != idx:
                idx = best
                needs_restart = False
            elif current_temp <= eff_low and best < len(profiles) - 1:
                # Explore next profile to collect more data
                nxt = best + 1
                if nxt not in state["real_hash"] or state["real_hash"][nxt]["count"] < 3:
                    if (now - state.get("explore_ts", 0)) > 600:
                        idx = nxt
                        state["explore_ts"] = now
        else:
            # Insufficient real data — gentle ramp to explore
            if current_temp <= eff_low and idx < len(profiles) - 1:
                headroom = eff_high - current_temp
                if headroom >= 15:
                    min_cooldown, step = 60, 3
                elif headroom >= 10:
                    min_cooldown, step = 120, 2
                elif headroom >= 5:
                    min_cooldown, step = THROTTLE_RAMP_COOLDOWN, 1
                else:
                    min_cooldown, step = 999999, 0
                if step and (now - state["last_action"]) >= min_cooldown:
                    idx = min(idx + step, len(profiles) - 1)

    needs_update = (idx != state["profile_idx"] or mode != state.get("mode") or needs_restart)
    if not needs_update:
        if cooldown > state.get("cooldown_until", 0):
            state["cooldown_until"] = cooldown
        return

    profile = profiles[idx]
    freq = int(profile.get("frequency", 400))
    volt = int(profile.get("coreVoltage", 1150))

    # Skip if already at target freq/volt
    if abs(current_freq - freq) <= 5 and abs(current_volt - volt) <= 10:
        print(f"[d] Throttle skip {ip}: already at {freq}MHz/{volt}mV ({current_temp:.0f}C)", flush=True)
        state["profile_idx"] = idx
        state["mode"] = mode
        state["last_action"] = now
        state["cooldown_until"] = cooldown
        return

    try:
        req = Request(f"http://{ip}/api/system",
                     data=json.dumps({"frequency": freq,
                                      "coreVoltage": volt,
                                      "overclockEnabled": 1,
                                      "overheat_mode": 0}).encode(),
                     headers={"Content-Type": "application/json",
                              "User-Agent": "BitaxeDashboard/1.0"},
                     method="PATCH")
        resp = urlopen(req, timeout=BITAXE_API_TIMEOUT)
        status = resp.getcode()
        is_init = (state["last_action"] == 0)
        label = "safemode" if overheat_mode == 1 else ("overheat" if current_temp >= eff_high else ("init" if is_init else "opt"))
        print(f"[*] Throttle {label} {ip} ({current_temp:.0f}C) -> {mode} idx={idx} {freq}MHz @ {volt}mV (HTTP {status})", flush=True)
        if needs_restart:
            time.sleep(1)
            rst = Request(f"http://{ip}/api/system/restart", method="POST",
                          headers={"User-Agent": "BitaxeDashboard/1.0"})
            urlopen(rst, timeout=BITAXE_API_TIMEOUT)
            print(f"[*] Throttle {ip}: restart issued", flush=True)
        state["profile_idx"] = idx
        state["mode"] = mode
        state["last_action"] = now
        state["cooldown_until"] = cooldown
    except Exception as e:
        print(f"[!] Throttle FAILED for {ip}: {e}", flush=True)

# --- Predictive Planner (thermal model + 24h forecast) ---
PLANNER_SAFETY_MARGIN = int(os.environ.get("PLANNER_SAFETY_MARGIN", "3"))
THERMAL_MODEL = {}
_PLAN = []
_PLAN_TS = 0
_PLAN_LOCK = threading.Lock()
_PLAN_HOURLY_FORECAST = []

def _rebuild_thermal_model():
    try:
        conn = sqlite3.connect(HISTORY_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT ip, hostname, frequency, core_voltage, temp, ambient, hashrate FROM history WHERE temp > 0 AND ambient IS NOT NULL AND hashrate > 0 AND temp > ambient"
        ).fetchall()
        conn.close()
    except Exception:
        return
    model = {}
    for r in rows:
        ip = r["ip"]
        delta = r["temp"] - r["ambient"]
        key = f'{r["frequency"]}_{r["core_voltage"]}'
        model.setdefault(ip, {})
        if key not in model[ip]:
            model[ip][key] = {"delta": 0, "count": 0, "freq": r["frequency"], "volt": r["core_voltage"]}
        m = model[ip][key]
        m["delta"] = (m["delta"] * m["count"] + delta) / (m["count"] + 1)
        m["count"] += 1
    global THERMAL_MODEL
    THERMAL_MODEL.clear()
    THERMAL_MODEL.update(model)
    print(f"[*] Thermal model: {sum(len(v) for v in model.values())} entries across {len(model)} ASICs")

def _delta_for_profile(ip, freq, volt):
    entries = THERMAL_MODEL.get(ip, {})
    key = f"{freq}_{volt}"
    if key in entries:
        return entries[key]["delta"]
    best = None
    best_dist = 999999
    for k, v in entries.items():
        dist = abs(v["freq"] - freq) + abs(v["volt"] - volt) / 10
        if dist < best_dist:
            best_dist = dist
            best = v
    return best["delta"] if best else 20

def _fetch_hourly_forecast():
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}&longitude={WEATHER_LON}&hourly=temperature_2m&timezone=auto&forecast_hours=24"
        resp = urlopen(url, timeout=10)
        data = json.loads(resp.read().decode())
        return [{"time": data["hourly"]["time"][i], "temp": data["hourly"]["temperature_2m"][i]} for i in range(len(data["hourly"]["time"]))]
    except Exception as e:
        print(f"[!] Hourly forecast fetch failed: {e}")
        return []

def _compute_plan():
    global _PLAN, _PLAN_TS, _PLAN_HOURLY_FORECAST
    _PLAN_HOURLY_FORECAST = _fetch_hourly_forecast()
    if not _PLAN_HOURLY_FORECAST:
        return
    _rebuild_thermal_model()
    _ensure_throttle_profiles()
    ambient_offset = _ambient_offset()
    eff_high = THROTTLE_HIGH - ambient_offset
    plan = []
    for h in _PLAN_HOURLY_FORECAST:
        hour_ts = int(datetime.datetime.strptime(h["time"], "%Y-%m-%dT%H:%M").timestamp()) if "T" in h["time"] else 0
        forecast_ambient = h["temp"]
        entry = {"ts": hour_ts, "time": h["time"], "forecast_ambient": forecast_ambient, "asics": {}}
        for ip, profiles in _throttle_profiles.items():
            best_idx = 0
            for idx in range(len(profiles) - 1, -1, -1):
                p = profiles[idx]
                freq = int(p.get("frequency", 400))
                volt = int(p.get("coreVoltage", 1150))
                delta = _delta_for_profile(ip, freq, volt)
                predicted_temp = forecast_ambient + delta
                if predicted_temp < eff_high - PLANNER_SAFETY_MARGIN:
                    best_idx = idx
                    break
            entry["asics"][ip] = {"profile_idx": best_idx, "frequency": int(profiles[best_idx].get("frequency", 400)), "core_voltage": int(profiles[best_idx].get("coreVoltage", 1150)), "predicted_temp": round(forecast_ambient + _delta_for_profile(ip, int(profiles[best_idx].get("frequency", 400)), int(profiles[best_idx].get("coreVoltage", 1150))), 1)}
        plan.append(entry)
    _PLAN = plan
    _PLAN_TS = time.time()
    print(f"[*] Plan computed: {len(plan)} hours, {len(_throttle_profiles)} ASICs")

def _plan_executor():
    while True:
        try:
            if time.time() - _PLAN_TS >= 3600:
                _compute_plan()
            if _PLAN:
                now = datetime.datetime.now().replace(minute=0, second=0, microsecond=0)
                current_hour_ts = int(now.timestamp())
                for entry in _PLAN:
                    if entry["ts"] == current_hour_ts:
                        for ip, asic in entry["asics"].items():
                            with _throttle_disabled_lock:
                                if ip in _throttle_disabled:
                                    continue
                            state = _throttle_state.get(ip)
                            if state:
                                planned = asic["profile_idx"]
                                if state.get("planned_idx") != planned:
                                    state["planned_idx"] = planned
                                    state["plan_freq"] = asic["frequency"]
                                    state["plan_volt"] = asic["core_voltage"]
                                    state["plan_predicted"] = asic["predicted_temp"]
                                    print(f"[*] Plan: {ip} -> profile {planned} ({asic['frequency']}MHz) pred={asic['predicted_temp']}°C amb={entry['forecast_ambient']}°C", flush=True)
                        break
        except Exception as e:
            print(f"[!] Plan executor error: {e}", flush=True)
        time.sleep(600)

# --- Benchmark ---

def _benchmark_fetch_core_info(ip):
    cached = _benchmark_asic_cache.get(ip)
    if cached:
        return cached
    try:
        info = fetch_bitaxe(ip)
        if info.get("error"):
            return None
        sc = info.get("asic_count", 40)
        # Try to get asicCount from /api/system/asic
        ac = 1
        try:
            req = Request(f"http://{ip}/api/system/asic", headers={"User-Agent": "BitaxeDashboard/1.0"})
            resp = urlopen(req, timeout=10)
            asic_data = json.loads(resp.read().decode())
            ac = asic_data.get("asicCount", 1)
        except Exception:
            pass
        _benchmark_asic_cache[ip] = (sc, ac)
        return (sc, ac)
    except Exception:
        return None

def _benchmark_expected_hashrate(freq, core_count_info):
    if not core_count_info:
        return freq * 40 / 1000
    sc, ac = core_count_info
    return freq * (sc * ac) / 1000

def _benchmark_worker(ip, **kwargs):
    with _benchmark_running_lock:
        if ip in _benchmark_running:
            return
        _benchmark_running.add(ip)
    with _benchmark_progress_lock:
        _benchmark_progress[ip] = {"running": True, "freq": 0, "volt": 0, "step": 0, "message": "Starting...", "progress_pct": 0, "error": None}
    # Disable throttler for this ASIC
    with _throttle_disabled_lock:
        _throttle_disabled.add(ip)
    # Resolve params: passed kwargs override env vars, env vars override defaults
    start_volt = kwargs.get("start_voltage", BENCHMARK_START_VOLTAGE)
    start_freq = kwargs.get("start_frequency", BENCHMARK_START_FREQUENCY)
    max_volt = kwargs.get("max_voltage", BENCHMARK_MAX_VOLTAGE)
    max_freq = kwargs.get("max_frequency", BENCHMARK_MAX_FREQUENCY)
    duration = kwargs.get("duration", BENCHMARK_DURATION)
    sample_interval = kwargs.get("sample_interval", BENCHMARK_SAMPLE_INTERVAL)
    stabilize_time = kwargs.get("stabilize_time", BENCHMARK_STABILIZE_TIME)
    freq_step = kwargs.get("freq_step", BENCHMARK_FREQ_STEP)
    volt_step = kwargs.get("volt_step", BENCHMARK_VOLT_STEP)
    print(f"[*] Benchmark: started for {ip}", flush=True)
    core_info = _benchmark_fetch_core_info(ip)
    results = []
    current_voltage = start_volt
    current_frequency = start_freq
    step = 0
    try:
        while current_voltage <= max_volt and current_frequency <= max_freq:
            step += 1
            expected_ghs = _benchmark_expected_hashrate(current_frequency, core_info)
            with _benchmark_progress_lock:
                _benchmark_progress[ip].update({"freq": current_frequency, "volt": current_voltage, "expected_ghs": expected_ghs, "step": step, "message": f"Testing {current_frequency}MHz @ {current_voltage}mV (exp {expected_ghs:.1f} GH/s)...", "progress_pct": 0})
            # Apply settings (PATCH only — no restart needed, changes take effect immediately)
            r = bitaxe_action(ip, "settings", {"coreVoltage": current_voltage, "frequency": current_frequency, "overclockEnabled": 1, "overheat_mode": 0})
            if not r.get("ok"):
                with _benchmark_progress_lock:
                    _benchmark_progress[ip]["message"] = f"Failed to set {current_frequency}MHz/{current_voltage}mV"
                print(f"[!] Benchmark {ip}: set settings failed: {r.get('error')}", flush=True)
                break
            # Wait for settings to stabilize
            with _benchmark_progress_lock:
                _benchmark_progress[ip]["message"] = f"Stabilizing {stabilize_time}s... ({current_frequency}MHz @ {current_voltage}mV)"
            time.sleep(stabilize_time)
            # Poll until ASIC is reachable (may have been restarted by throttler)
            for _ in range(stabilize_time):
                info = fetch_bitaxe(ip)
                if not info.get("error"):
                    break
                time.sleep(1)
            # Sample loop
            hash_rates = []
            temperatures = []
            power_values = []
            total_samples = duration // sample_interval
            stopped_early = False
            stop_reason = None
            for sample in range(total_samples):
                info = fetch_bitaxe(ip)
                if info.get("error"):
                    time.sleep(sample_interval)
                    continue
                temp = info.get("temp")
                if temp is not None and temp >= THROTTLE_HIGH - _ambient_offset():
                    stopped_early = True
                    stop_reason = f"Temp {temp}°C >= {THROTTLE_HIGH - _ambient_offset()}°C limit"
                    print(f"[!] Benchmark {ip}: {stop_reason}", flush=True)
                    break
                hr = info.get("hashrate")
                if hr is not None:
                    hash_rates.append(hr)
                if temp is not None:
                    temperatures.append(temp)
                pw = info.get("power")
                if pw is not None:
                    power_values.append(pw)
                with _benchmark_progress_lock:
                    _benchmark_progress[ip]["message"] = f"{current_frequency}MHz @ {current_voltage}mV — sample {sample+1}/{total_samples} temp={temp}°C (exp {expected_ghs:.1f} GH/s)"
                if sample < total_samples - 1:
                    time.sleep(sample_interval)
            if stopped_early:
                with _benchmark_progress_lock:
                    _benchmark_progress[ip]["message"] = f"Thermal stop: {stop_reason} (exp {expected_ghs:.1f} GH/s)"
                if hash_rates and temperatures:
                    sorted_hr = sorted(hash_rates)
                    trimmed_hr = sorted_hr[3:-3] if len(sorted_hr) > 6 else sorted_hr
                    avg_hashrate = sum(trimmed_hr) / len(trimmed_hr)
                    sorted_temps = sorted(temperatures)
                    trimmed_temps = sorted_temps[6:] if len(sorted_temps) > 6 else sorted_temps
                    avg_temp = sum(trimmed_temps) / len(trimmed_temps)
                    results.append({
                        "coreVoltage": current_voltage, "frequency": current_frequency,
                        "averageHashRate": avg_hashrate / 1e9, "averageTemperature": avg_temp
                    })
                break
            if not hash_rates or len(hash_rates) < 3:
                with _benchmark_progress_lock:
                    _benchmark_progress[ip]["message"] = f"Insufficient samples at {current_frequency}MHz"
                print(f"[!] Benchmark {ip}: insufficient samples ({len(hash_rates)})", flush=True)
                break
            sorted_hr = sorted(hash_rates)
            trimmed_hr = sorted_hr[3:-3] if len(sorted_hr) > 6 else sorted_hr
            avg_hashrate = sum(trimmed_hr) / len(trimmed_hr)
            sorted_temps = sorted(temperatures)
            trimmed_temps = sorted_temps[6:] if len(sorted_temps) > 6 else sorted_temps
            avg_temp = sum(trimmed_temps) / len(trimmed_temps)
            efficiency_jth = 0
            if avg_hashrate > 0 and power_values:
                avg_power = sum(power_values) / len(power_values)
                efficiency_jth = avg_power / (avg_hashrate / 1e9 / 1000)
            hr_ghs = avg_hashrate / 1e9
            results.append({
                "coreVoltage": current_voltage, "frequency": current_frequency,
                "averageHashRate": hr_ghs, "averageTemperature": avg_temp,
                "efficiencyJTH": efficiency_jth
            })
            print(f"[*] Benchmark {ip}: {current_frequency}MHz/{current_voltage}mV -> {hr_ghs:.1f} GH/s (exp {expected_ghs:.1f}) @ {avg_temp:.1f}°C", flush=True)
            hashrate_within_tolerance = (hr_ghs >= expected_ghs * 0.94)
            if hashrate_within_tolerance:
                if current_frequency + freq_step <= max_freq:
                    current_frequency += freq_step
                elif current_voltage + volt_step <= max_volt:
                    current_voltage += volt_step
                    current_frequency = start_freq
                    print(f"[*] Benchmark {ip}: max freq at {current_frequency}MHz, trying {current_voltage}mV", flush=True)
                else:
                    print(f"[*] Benchmark {ip}: max freq {max_freq}MHz / max volt {max_volt}mV reached", flush=True)
                    break
            else:
                print(f"[d] Benchmark {ip}: hr {hr_ghs:.1f} < 94% of {expected_ghs:.1f}, backing off", flush=True)
                if current_voltage + volt_step <= max_volt:
                    current_voltage += volt_step
                    current_frequency = max(start_freq, current_frequency - freq_step)
                    print(f"[*] Benchmark {ip}: retry at {current_frequency}MHz / {current_voltage}mV", flush=True)
                else:
                    print(f"[*] Benchmark {ip}: max voltage {max_volt}mV reached", flush=True)
                    break
            # Save intermediate results
            _benchmark_save_results(ip, results)
        # Final save
        _benchmark_save_results(ip, results)
        # Reload profiles
        global _throttle_loaded
        _throttle_loaded = False
        _ensure_throttle_profiles()
        _rebuild_thermal_model()
        _compute_plan()
        with _benchmark_progress_lock:
            _benchmark_progress[ip].update({"running": False, "message": "Benchmark complete", "progress_pct": 100})
        print(f"[*] Benchmark {ip}: completed with {len(results)} results, profiles reloaded, plan recomputed", flush=True)
    except Exception as e:
        with _benchmark_progress_lock:
            _benchmark_progress[ip].update({"running": False, "message": f"Error: {e}", "error": str(e)})
        print(f"[!] Benchmark {ip}: error: {e}", flush=True)
    finally:
        # Restore throttler
        with _throttle_disabled_lock:
            _throttle_disabled.discard(ip)
        with _benchmark_running_lock:
            _benchmark_running.discard(ip)

def _benchmark_save_results(ip, results):
    if not results:
        return
    try:
        top = sorted(results, key=lambda x: x.get("averageHashRate", 0), reverse=True)[:5]
        eff = sorted(results, key=lambda x: x.get("efficiencyJTH", 99999))[:5]
        data = {
            "all_results": results,
            "top_performers": [{"rank": i+1, "coreVoltage": r["coreVoltage"], "frequency": r["frequency"],
                "averageHashRate": r["averageHashRate"], "averageTemperature": r["averageTemperature"],
                "efficiencyJTH": r.get("efficiencyJTH", 0)} for i, r in enumerate(top)],
            "most_efficient": [{"rank": i+1, "coreVoltage": r["coreVoltage"], "frequency": r["frequency"],
                "averageHashRate": r["averageHashRate"], "averageTemperature": r["averageTemperature"],
                "efficiencyJTH": r.get("efficiencyJTH", 0)} for i, r in enumerate(eff)]
        }
        os.makedirs(THROTTLE_PROFILES_DIR, exist_ok=True)
        fpath = os.path.join(THROTTLE_PROFILES_DIR, f"{ip}.json")
        with open(fpath, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[*] Benchmark {ip}: saved {len(results)} results to {fpath}", flush=True)
    except Exception as e:
        print(f"[!] Benchmark {ip}: save failed: {e}", flush=True)

# --- Discovery ---

_discovered = []
_discovery_lock = threading.Lock()
_discovery_ts = 0
_discovery_done = False
_known_miners = []

SCAN_TIMEOUT = 60

_SUFFIXES = {"k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "g": 1e9, "G": 1e9, "t": 1e12, "T": 1e12, "p": 1e15, "P": 1e15, "e": 1e18, "E": 1e18}
def _parse_diff(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        v = v.strip()
        s = v[-1]
        if s in _SUFFIXES:
            return float(v[:-1]) * _SUFFIXES[s]
        return float(v)
    return 0.0

def _probe_host(ip):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex((ip, SCAN_PORT))
        s.close()
        if result != 0:
            return None
        req = Request(f"http://{ip}/api/system/info", headers={"User-Agent": "BitaxeDashboard/1.0"})
        resp = urlopen(req, timeout=BITAXE_API_TIMEOUT)
        data = json.loads(resp.read().decode())
        if data.get("ASICModel") or data.get("asicCount") is not None:
            hostname = data.get("hostname", ip)
            return {"ip": ip, "name": hostname}
    except Exception:
        pass
    return None

def discover_miners():
    global _discovered, _discovery_ts, _discovery_done
    t0 = time.time()
    try:
        net = ipaddress.ip_network(SCAN_SUBNET, strict=False)
    except ValueError:
        return
    hosts = [str(ip) for ip in net.hosts()]
    found = []
    with ThreadPoolExecutor(max_workers=SCAN_THREADS) as pool:
        fs = {pool.submit(_probe_host, ip): ip for ip in hosts}
        done, not_done = wait(fs, timeout=SCAN_TIMEOUT)
        for f in done:
            result = f.result()
            if result:
                found.append(result)
    found.sort(key=lambda m: [int(o) for o in m["ip"].split(".")])
    elapsed = time.time() - t0
    with _discovery_lock:
        _discovered = found
        _discovery_done = True
        _discovery_ts = time.time()
    print(f"[*] Discovery: {len(found)} miners in {elapsed:.1f}s")

def get_discovered():
    global _known_miners
    with _discovery_lock:
        if not _discovery_done:
            t = threading.Thread(target=discover_miners, daemon=True)
            t.start()
        elif (time.time() - _discovery_ts) > SCAN_INTERVAL:
            t = threading.Thread(target=discover_miners, daemon=True)
            t.start()
        merged = list(STATIC_MINERS)
        seen = {m["ip"] for m in merged}
        for m in _discovered:
            if m["ip"] not in seen:
                merged.append(m)
                seen.add(m["ip"])
        for m in _known_miners:
            if m["ip"] not in seen:
                merged.append(m)
                seen.add(m["ip"])
        _known_miners = [{"ip": m["ip"], "name": m.get("name", m["ip"])} for m in merged if m["ip"] not in {s["ip"] for s in STATIC_MINERS}]
        return merged

# --- Data Fetching ---

def fetch_bitaxe(ip):
    try:
        req = Request(f"http://{ip}/api/system/info", headers={"User-Agent": "BitaxeDashboard/1.0"})
        resp = urlopen(req, timeout=BITAXE_API_TIMEOUT)
        data = json.loads(resp.read().decode())
        hr_mon = data.get("hashrateMonitor", {}).get("asics", [{}])[0] if data.get("hashrateMonitor") else {}
        # Normalize domains: v2.12.2+ firmware provides bare floats, convert to objects
        _raw_domains = hr_mon.get("domains", [])
        if _raw_domains and isinstance(_raw_domains[0], (int, float)):
            _asic_freq = data.get("frequency", 0)
            _total_err = hr_mon.get("errorCount", 0)
            _total_hr = sum(_raw_domains)
            _domains = []
            _cumul_err = 0
            for _i, _dhr in enumerate(_raw_domains):
                _d_err = round(_total_err * (_dhr / _total_hr)) if _total_hr > 0 else 0
                _cumul_err += _d_err
                if _i == len(_raw_domains) - 1 and _total_err > 0:
                    _d_err = _total_err - (_cumul_err - _d_err)
                _domains.append({"hr": _dhr, "frequency": _asic_freq, "errorCount": _d_err})
        else:
            _domains = _raw_domains
        def gh(h): return float(h) * 1e9
        hr = gh(data.get("hashRate", 0))
        # hashRate_1m may be missing on old firmware (v2.6.5) — fall back to instant hashRate
        hr1m = gh(data.get("hashRate_1m") or data.get("hashRate") or 0)
        # Network difficulty: prefer ASIC-reported, else cached from pool API, else stratumDiff floor
        asic_net_diff = data.get("networkDifficulty")
        if asic_net_diff:
            net_diff = float(asic_net_diff)
        else:
            with _NETWORK_DIFF_LOCK:
                net_diff = _NETWORK_DIFF_CACHE.get("btc", 0) or 1
        return {
            "hostname": data.get("hostname", "?"),
            "hashrate": hr,
            "hashrate_1m": hr1m,
            "hashrate_10m": hr1m if (v := gh(data.get("hashRate_10m", 0))) == 0 else v,
            "hashrate_1h": hr1m if (v := gh(data.get("hashRate_1h", 0))) == 0 else v,
            "expected_hashrate": hr if (v := gh(data.get("expectedHashrate", 0))) == 0 else v,
            "temp": data.get("temp", -1),
            "temp2": data.get("temp2", -1),
            "vr_temp": data.get("vrTemp", -1),
            "power": data.get("power", 0),
            "voltage": data.get("voltage", 0),
            "current": data.get("current", 0),
            "frequency": data.get("frequency", 0),
            "core_voltage": data.get("coreVoltageActual", data.get("coreVoltage", 0)),
            "core_voltage_set": data.get("coreVoltage", 0),
            "error_pct": data.get("errorPercentage", 0),
            "uptime": data.get("uptimeSeconds", 0),
            "wifi_rssi": data.get("wifiRSSI", 0),
            "wifi_status": data.get("wifiStatus", "?"),
            "ssid": data.get("ssid", ""),
            "shares_accepted": data.get("sharesAccepted", 0),
            "shares_rejected": data.get("sharesRejected", 0),
            "reject_reasons": data.get("sharesRejectedReasons", []),
            "best_diff": _parse_diff(data.get("bestDiff", 0)),
            "best_session_diff": _parse_diff(data.get("bestSessionDiff", 0)),
            "network_diff": net_diff,
            "asics": _domains,
            "asic_error_count": hr_mon.get("errorCount", 0),
            "asic_model": data.get("ASICModel", "?"),
            "asic_count": data.get("smallCoreCount", 0),
            "version": data.get("version", "?"),
            "axeos_version": data.get("axeOSVersion", data.get("version", "?")),
            "board_version": data.get("boardVersion", "?"),
            "fan_speed": data.get("fanspeed", 0),
            "fan_rpm": data.get("fanrpm", 0),
            "fan2_rpm": data.get("fan2rpm", 0),
            "autofanspeed": data.get("autofanspeed", 0),
            "min_fan_speed": data.get("minFanSpeed", 25),
            "manual_fan_speed": data.get("manualFanSpeed", 100),
            "temptarget": data.get("temptarget", 60),
            "pool_diff": data.get("poolDifficulty", 0),
            "stratum_user": data.get("stratumUser", "?"),
            "stratum_url": data.get("stratumURL", ""),
            "stratum_port": data.get("stratumPort", 0),
            "fallback_stratum_url": data.get("fallbackStratumURL", ""),
            "fallback_stratum_port": data.get("fallbackStratumPort", 0),
            "fallback_stratum_user": data.get("fallbackStratumUser", ""),
            "block_height": data.get("blockHeight", 0),
            "overheat_mode": data.get("overheat_mode", 0),
            "overclock_enabled": data.get("overclockEnabled", 0),
            "mac": data.get("macAddr", ""),
            "invertscreen": data.get("invertscreen", 0),
            "flipscreen": data.get("flipscreen", 0),
            "display_timeout": data.get("displayTimeout", -1),
            "display": data.get("display", ""),
            "rotation": data.get("rotation", 0),
            "autofanspeed": data.get("autofanspeed", 0),
            "invertfanpolarity": data.get("invertfanpolarity", 0),
            "error": None,
        }
    except Exception as e:
        return {"hostname": ip, "error": str(e), "temp": -1}

def compute_pool_stats(miners):
    online = [m for m in miners if not m.get("error")]
    total_hr = sum(m.get("hashrate_1m", 0) for m in online)
    best = max((m.get("best_session_diff", 0) or m.get("best_diff", 0)) for m in online) if online else 0
    return {
        "hashrate1m": total_hr,
        "workers": len(miners),
        "workers_online": len(online),
        "bestshare": best,
        "lastshare": time.time(),
        "worker_list": [{
            "name": m.get("hostname", "?"),
            "hashrate1m": m.get("hashrate_1m", 0),
            "shares": m.get("shares_accepted", 0),
            "lastshare": time.time(),
        } for m in online],
    }

def bitaxe_action(ip, action, body=None):
    try:
        if action == "settings":
            if body is None:
                body = request.get_json(force=True, silent=True) or {}
            payload = json.dumps(body).encode()
            req = Request(f"http://{ip}/api/system", data=payload,
                          headers={"Content-Type": "application/json", "User-Agent": "BitaxeDashboard/1.0"},
                          method="PATCH")
            resp = urlopen(req, timeout=BITAXE_API_TIMEOUT)
            text = resp.read().decode()
            try:
                return {"ok": True, "result": json.loads(text)}
            except json.JSONDecodeError:
                return {"ok": True, "result": text}
        elif action in ("restart", "pause", "resume", "identify"):
            req = Request(f"http://{ip}/api/system/{action}", method="POST",
                          headers={"User-Agent": "BitaxeDashboard/1.0"})
            resp = urlopen(req, timeout=BITAXE_API_TIMEOUT)
            text = resp.read().decode()
            try:
                return {"ok": True, "result": json.loads(text)}
            except json.JSONDecodeError:
                return {"ok": True, "result": text}
        return {"ok": False, "error": "unknown action"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

DISMISSED_BENCH_DB = os.path.join(os.path.dirname(HISTORY_DB), "needs_bench_dismissed.json")
_needs_bench_dismissed = set()
_needs_bench_lock = threading.Lock()

def _load_dismissed():
    global _needs_bench_dismissed
    try:
        with open(DISMISSED_BENCH_DB) as f:
            data = json.load(f)
            if isinstance(data, list):
                _needs_bench_dismissed = set(data)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

def _save_dismissed():
    try:
        os.makedirs(os.path.dirname(DISMISSED_BENCH_DB), exist_ok=True)
        with open(DISMISSED_BENCH_DB, "w") as f:
            json.dump(list(_needs_bench_dismissed), f)
    except Exception as e:
        print(f"[!] Failed to save dismissed: {e}", flush=True)

def fetch_all_data():
    _ensure_throttle_profiles()
    # Refresh network difficulty cache every 60s
    with _NETWORK_DIFF_LOCK:
        if time.time() - _NETWORK_DIFF_CACHE.get("ts", 0) > 60:
            threading.Thread(target=_fetch_network_difficulties, daemon=True).start()
    miners = []
    for m in get_discovered():
        data = fetch_bitaxe(m["ip"])
        ip = m["ip"]
        if not data.get("error") and data.get("temp", -1) >= 0:
            _apply_throttle(ip, data["temp"], data.get("frequency", 0), data.get("core_voltage", 0), data.get("overheat_mode", 0), data.get("hashrate", 0))
        with _throttle_lock:
            s = _throttle_state.get(ip)
            data["throttle_flash"] = 1 if s and (time.time() - s["last_action"]) < 3 else 0
            with _throttle_disabled_lock:
                data["throttle_disabled"] = 1 if ip in _throttle_disabled else 0
        with _benchmark_running_lock:
            data["benchmarking"] = 1 if ip in _benchmark_running else 0
        # New ASIC detection: no profile AND no history AND not dismissed
        has_profile = os.path.isfile(os.path.join(THROTTLE_PROFILES_DIR, f"{ip}.json"))
        has_history = False
        try:
            conn = sqlite3.connect(HISTORY_DB)
            row = conn.execute("SELECT 1 FROM history WHERE ip=? LIMIT 1", (ip,)).fetchone()
            has_history = row is not None
            conn.close()
        except Exception:
            pass
        with _needs_bench_lock:
            dismissed = ip in _needs_bench_dismissed
        data["needs_benchmark"] = 1 if (not has_profile and not has_history and not dismissed) else 0
        miners.append({**m, **data})
    pool = compute_pool_stats(miners)
    result = {"miners": miners, "pool": pool, "fetched_at": time.time()}

    # --- Push triggers: block found & overheat ---
    with _PUSH_LOCK:
        for m in miners:
            ip = m.get("ip", "")
            hostname = m.get("hostname", ip)
            # Block found detection
            if m.get("best_session_diff", 0) >= (m.get("network_diff", 1) or 1):
                if ip not in _PUSHED_BLOCKS:
                    _PUSHED_BLOCKS.add(ip)
                    _sse_broadcast("block_found", {"miner": hostname, "ip": ip, "diff": m["best_session_diff"]})
                    _send_fcm_push("BLOCK FOUND!", f"{hostname} found a block!", {"ip": ip, "type": "block"})
            else:
                _PUSHED_BLOCKS.discard(ip)
            # Overheat detection
            temp = m.get("temp", 0)
            if temp >= THROTTLE_HIGH:
                last_push = _PUSHED_OVERHEAT.get(ip, 0)
                if time.time() - last_push > 300:  # suppress for 5 min
                    _PUSHED_OVERHEAT[ip] = time.time()
                    _sse_broadcast("overheat", {"miner": hostname, "ip": ip, "temp": temp})
                    _send_fcm_push("OVERHEAT", f"{hostname} at {temp:.0f}°C", {"ip": ip, "type": "overheat", "temp": str(temp)})

    return result

_cache = {"data": None, "ts": 0, "lock": threading.Lock()}

def get_data():
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < 15:
        return _cache["data"]
    with _cache["lock"]:
        if _cache["data"] and (now - _cache["ts"]) < 15:
            return _cache["data"]
        _cache["data"] = fetch_all_data()
        _cache["ts"] = time.time()
    return _cache["data"]

# --- Routes ---

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/data")
def api_data():
    return jsonify(get_data())

@app.route("/api/history")
def api_history():
    ip = request.args.get("ip")
    hours = int(request.args.get("hours", "24"))
    since = int(time.time()) - hours * 3600
    conn = sqlite3.connect(HISTORY_DB)
    conn.row_factory = sqlite3.Row
    if ip:
        rows = conn.execute(
            "SELECT * FROM history WHERE ip=? AND ts>=? ORDER BY ts",
            (ip, since)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM history WHERE ts>=? ORDER BY ts", (since,)
        ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/bitaxe/<ip>/<action>", methods=["POST"])
def api_bitaxe_action(ip, action):
    if action == "throttle-enable":
        with _throttle_disabled_lock:
            _throttle_disabled.discard(ip)
        return jsonify({"ok": True})
    if action == "throttle-disable":
        with _throttle_disabled_lock:
            _throttle_disabled.add(ip)
        print(f"[*] Throttle disabled for {ip}", flush=True)
        return jsonify({"ok": True})
    result = bitaxe_action(ip, action)
    return jsonify(result)

@app.route("/api/benchmark/<ip>", methods=["POST"])
def api_benchmark_start(ip):
    with _benchmark_running_lock:
        if ip in _benchmark_running:
            return jsonify({"ok": False, "error": "Benchmark already running for this ASIC"})
    body = request.get_json(force=True, silent=True) or {}
    # Extract known param keys, pass as kwargs to worker
    param_keys = ("duration", "sample_interval", "stabilize_time",
                  "start_frequency", "start_voltage", "max_frequency", "max_voltage",
                  "freq_step", "volt_step")
    kwargs = {k: int(body[k]) for k in param_keys if k in body}
    thread = threading.Thread(target=_benchmark_worker, args=(ip,), kwargs=kwargs, daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Benchmark started"})

@app.route("/api/benchmark/<ip>/status")
def api_benchmark_status(ip):
    with _benchmark_progress_lock:
        status = _benchmark_progress.get(ip, {"running": False, "message": "No benchmark data"})
        return jsonify(status)

@app.route("/api/plan")
def api_plan():
    return jsonify({"plan": _PLAN, "computed_ts": _PLAN_TS, "forecast_hourly": _PLAN_HOURLY_FORECAST, "model_summary": {ip: {k: {"delta": round(v["delta"], 1), "count": v["count"]} for k, v in entries.items()} for ip, entries in THERMAL_MODEL.items()}})

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(_settings)

@app.route("/api/settings", methods=["POST"])
def api_settings_set():
    data = request.get_json(force=True, silent=True) or {}
    # Resolve city to lat/lon if a city is provided
    city = data.get("weather_city", "").strip()
    if city and (not data.get("weather_lat") or not data.get("weather_lon")):
        try:
            url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1&language=en&format=json"
            r = urlopen(Request(url, headers={"User-Agent": "BitaxeDashboard/1.0"}), timeout=10)
            results = json.loads(r.read()).get("results", [])
            if results:
                data["weather_lat"] = str(results[0]["latitude"])
                data["weather_lon"] = str(results[0]["longitude"])
        except Exception:
            pass
    save_settings(data)
    global _settings
    _settings = load_settings()
    return jsonify({"ok": True})

@app.route("/api/benchmark/<ip>/dismiss", methods=["POST"])
def api_benchmark_dismiss(ip):
    with _needs_bench_lock:
        _needs_bench_dismissed.add(ip)
        _save_dismissed()
    return jsonify({"ok": True})

@app.route("/api/geocode", methods=["POST"])
def api_geocode():
    data = request.get_json(force=True, silent=True) or {}
    city = data.get("city", "").strip()
    if not city:
        return jsonify({"ok": False, "error": "No city provided"})
    try:
        url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=3&language=en&format=json"
        r = urlopen(Request(url, headers={"User-Agent": "BitaxeDashboard/1.0"}), timeout=10)
        results = json.loads(r.read()).get("results", [])
        if not results:
            return jsonify({"ok": False, "error": "City not found"})
        matches = [{"name": res.get("name",""), "country": res.get("country",""), "lat": res["latitude"], "lon": res["longitude"]} for res in results]
        return jsonify({"ok": True, "matches": matches})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/restart", methods=["POST"])
def api_restart():
    thread = threading.Thread(target=lambda: (time.sleep(1), os._exit(0)), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Restarting..."})

# --- Mobile App: Auth Middleware ---

@app.before_request
def auth_check():
    exempt = {"/", "/api/pair/generate", "/api/pair/exchange", "/api/events"}
    if request.path in exempt:
        return None
    if request.path.startswith("/static"):
        return None
    if not _check_auth():
        return jsonify({"error": "Unauthorized", "code": 401}), 401

# --- Mobile App: Pair Endpoints ---

@app.route("/api/pair/generate")
def api_pair_generate():
    import qrcode
    ts_ip = _tailscale_ip()
    if not ts_ip:
        return jsonify({"ok": False, "error": "Tailscale not running. Install and connect Tailscale first."})
    port = 5050
    token = _generate_pair_token()
    qr_data = f"bitaxe://pair?host={ts_ip}&port={port}&token={token}"
    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    qr_b64 = base64.b64encode(buf.read()).decode()
    return jsonify({
        "ok": True,
        "host": ts_ip,
        "port": port,
        "token": token,
        "qr_data_url": f"data:image/png;base64,{qr_b64}",
        "qr_payload": qr_data,
    })

@app.route("/api/pair/exchange", methods=["POST"])
def api_pair_exchange():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token", "")
    if not _validate_pair_token(token):
        return jsonify({"ok": False, "error": "Invalid or expired token"})
    session_key = _generate_session_key()
    device_name = data.get("device_name", "Unknown")
    with _PAIRED_DEVICES_LOCK:
        _PAIRED_DEVICES[session_key] = {
            "name": device_name,
            "paired_at": time.time(),
            "last_seen": time.time(),
        }
    _save_paired_devices()
    return jsonify({"ok": True, "session_key": session_key})

@app.route("/api/pair/devices")
def api_pair_devices():
    with _PAIRED_DEVICES_LOCK:
        devices = []
        for key, info in _PAIRED_DEVICES.items():
            devices.append({
                "key": key[:8] + "...",
                "name": info.get("name", "Unknown"),
                "paired_at": info.get("paired_at", 0),
                "last_seen": info.get("last_seen", 0),
            })
    return jsonify({"ok": True, "devices": devices})

@app.route("/api/pair/revoke", methods=["POST"])
def api_pair_revoke():
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key", "")
    with _PAIRED_DEVICES_LOCK:
        if key in _PAIRED_DEVICES:
            del _PAIRED_DEVICES[key]
    _save_paired_devices()
    return jsonify({"ok": True})

# --- Mobile App: SSE Events ---

@app.route("/api/events")
def api_events():
    if not _check_auth():
        ts_token = request.args.get("token", "")
        if ts_token and _check_auth_token(ts_token):
            pass
        else:
            return jsonify({"error": "Unauthorized"}), 401

    def event_stream():
        q = queue.Queue(maxsize=100)
        with _SSE_LOCK:
            _SSE_CLIENTS.append(q)
        try:
            while True:
                try:
                    payload = q.get(timeout=30)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _SSE_LOCK:
                if q in _SSE_CLIENTS:
                    _SSE_CLIENTS.remove(q)

    return Response(stream_with_context(event_stream()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})

def _check_auth_token(token):
    with _PAIRED_DEVICES_LOCK:
        return token in _PAIRED_DEVICES

# --- Mobile App: FCM ---

@app.route("/api/fcm/register", methods=["POST"])
def api_fcm_register():
    data = request.get_json(force=True, silent=True) or {}
    fcm_token = data.get("fcm_token", "")
    device_name = data.get("device_name", "Unknown")
    if not fcm_token:
        return jsonify({"ok": False, "error": "No FCM token"})
    with _FCM_TOKENS_LOCK:
        _FCM_TOKENS[fcm_token] = {"name": device_name, "registered_at": time.time()}
    _save_fcm_tokens()
    return jsonify({"ok": True})

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bitaxe Dashboard</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='6' fill='%231a2e3f'/><text x='16' y='22' text-anchor='middle' font-size='20' font-family='monospace' font-weight='bold' fill='%233498db'>B</text></svg>">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;700;900&display=swap');
  :root {
    --bg: #08080f; --surface: #0f0f1f; --surface2: #181830; --surface3: #222248;
    --text: #eef0ff; --text2: #8888cc; --text3: #5555aa;
    --accent: #00f0ff; --accent2: #33f5ff;
    --green: #00ff88; --yellow: #ffdd00; --red: #ff0055; --blue: #3366ff;
    --radius: 10px; --shadow: 0 2px 10px rgba(0,0,0,.6);
    --glow-cyan: 0 0 12px rgba(0,240,255,.4);
    --glow-magenta: 0 0 12px rgba(255,0,85,.4);
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Orbitron', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: var(--bg); color: var(--text); min-height: 100vh; }

  /* Header */
  .header { background: linear-gradient(135deg, var(--surface) 0%, #0a0a1a 100%);
            border-bottom: 1px solid rgba(255,255,255,.05); padding: 14px 28px;
            display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
  .header-left { display: flex; align-items: center; gap: 18px; }
  .header-left h1 { font-size: 1.2rem; font-weight: 700; display: flex; align-items: center; gap: 10px;
                     letter-spacing: -.3px; }
  .header-left h1 .btc { color: var(--accent); text-shadow: var(--glow-cyan); }
  .header-status { font-size: .72rem; color: var(--text3); }

  /* Global nav */
  .global-nav { display: flex; gap: 4px; background: var(--surface); padding: 0 28px;
                border-bottom: 1px solid rgba(255,255,255,.04); }
  .global-nav .nav-item { padding: 10px 20px; font-size: .82rem; font-weight: 500; color: var(--text3);
                          cursor: pointer; border-bottom: 2px solid transparent; transition: all .15s;
                          user-select: none; }
  .global-nav .nav-item:hover { color: var(--text); }
  .global-nav .nav-item.active { color: var(--accent); border-bottom-color: var(--accent); text-shadow: var(--glow-cyan); }
  .global-nav .nav-item.kiosk { color: var(--green); }
  .global-nav .nav-item.kiosk.active { color: var(--green); border-bottom-color: var(--green); }

  /* Main panel */
  .panel { display: none; padding: 20px 28px; }
  .panel.active { display: block; }

  /* Pool bar */
  .pool-bar { display: flex; flex-wrap: wrap; gap: 8px 40px; background: var(--surface);
              border-radius: var(--radius); padding: 14px 22px; margin-bottom: 22px;
              box-shadow: var(--shadow); border: 1px solid rgba(255,255,255,.03); }
  .pool-stat .pl { font-size: .68rem; color: var(--text3); text-transform: uppercase; letter-spacing: .4px; }
  .pool-stat .pv { font-size: 1.1rem; font-weight: 700; margin-top: 1px; }

  /* Hero grid */
  .hero-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; }

  /* Kiosk hero */
.kiosk-wrap { display: flex; align-items: center; justify-content: center; min-height: calc(100vh - 140px); }
.kiosk-hero { background: var(--surface); border-radius: 20px; padding: 40px 48px; border: 1px solid rgba(0,240,255,.08);
              box-shadow: 0 8px 40px rgba(0,0,0,.5); border: 1px solid rgba(255,255,255,.06);
              text-align: center; max-width: 600px; width: 100%; position: relative; overflow: hidden; }
.kiosk-hero .hero-bg-hash { font-size: 10rem; }
.kiosk-hero .hero-name { font-size: 1.4rem; font-weight: 700; }
.kiosk-hero .hero-hr { font-size: 3rem; }
.kiosk-hero .hero-stats { justify-content: center; gap: 32px; margin-top: 20px; flex-wrap: wrap; }
.kiosk-hero .hs-value { font-size: 1.1rem; }
.kiosk-hero .kiosk-progress { display: flex; justify-content: center; gap: 6px; margin-top: 24px; }
.kiosk-hero .kiosk-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--surface3);
                         transition: all .3s; cursor: pointer; }
.kiosk-hero .kiosk-dot.active { background: var(--accent); transform: scale(1.3); }
.kiosk-hero .hero-model { font-size: .78rem; }
@media (max-width: 640px) {
  .kiosk-hero { padding: 24px 20px; border-radius: 14px; }
  .kiosk-hero .hero-hr { font-size: 2rem; }
  .kiosk-hero .hero-name { font-size: 1.1rem; }
  .kiosk-hero .hero-stats { gap: 16px; }
}

/* Hero card */
  .hero { background: var(--surface); border-radius: var(--radius); padding: 20px; border: 1px solid rgba(0,240,255,.08);
          box-shadow: var(--shadow); cursor: pointer; transition: all .2s;
          border: 1px solid rgba(255,255,255,.04); position: relative; overflow: hidden; }
  .hero:hover { transform: translateY(-3px); box-shadow: 0 8px 28px rgba(0,0,0,.5);
    border-color: rgba(255,255,255,.05); }
  .hero:active { transform: translateY(-1px); }
  .hero.offline { opacity: .5; }
  .hero.milestone { background: linear-gradient(135deg, #001a33, #0066ff); box-shadow: 0 0 20px rgba(0,102,255,.3); }
  .hero.gold { background: linear-gradient(135deg, #332800, #ffdd00); box-shadow: 0 0 20px rgba(255,221,0,.3); }
  .hero.block { background: linear-gradient(135deg, #330011, #ff0055); box-shadow: 0 0 20px rgba(255,0,85,.3); }
  .hero.throttle { animation: throttle-flash .35s ease 3; }
  @keyframes throttle-flash { 0%,100%{opacity:1} 50%{opacity:.12} }
  .hero.thr-off { outline: 2px solid var(--red); outline-offset: -2px; }
  .hero.bench { outline: 2px solid var(--yellow); outline-offset: -2px; }
  .new-asic-banner { background: rgba(255,221,0,.08); border: 1px solid rgba(255,221,0,.25); border-radius: 5px; padding: 6px 8px; margin-top: 6px; font-size: .72rem; color: var(--yellow); line-height: 1.5; }
  .new-asic-banner a { color: #f0c040; text-decoration: underline; cursor: pointer; }
  .new-asic-banner a:hover { color: #ffd700; }
  .kiosk-hero.milestone { background: linear-gradient(135deg, #001a33, #0066ff); box-shadow: 0 0 20px rgba(0,102,255,.3); }
  .kiosk-hero.gold { background: linear-gradient(135deg, #332800, #ffdd00); box-shadow: 0 0 20px rgba(255,221,0,.3); }
  .kiosk-hero.block { background: linear-gradient(135deg, #330011, #ff0055); box-shadow: 0 0 20px rgba(255,0,85,.3); }
  .kiosk-hero.throttle { animation: throttle-flash .35s ease 3; }
  .kiosk-hero.thr-off { outline: 2px solid var(--red); outline-offset: -2px; }
  .kiosk-hero.bench { outline: 2px solid var(--yellow); outline-offset: -2px; }
  #confetti-canvas { position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; pointer-events: none; z-index: 9999; }
  #block-toast { position: fixed; top: 50%; left: 50%; transform: translate(-50%,-50%);
    z-index: 9999; background: rgba(0,0,0,.95); border: 2px solid var(--red);
    border-radius: 20px; padding: 40px 60px; text-align: center; font-size: 2rem; font-weight: 700;
    color: var(--red); cursor: pointer; display: none;
    box-shadow: 0 0 60px rgba(255,0,85,.3); animation: pulse 1.5s infinite; }
  #block-toast small { display: block; font-size: 1rem; color: var(--text2); margin-top: 8px; font-weight: 400; }
  @keyframes pulse { 0%{transform:translate(-50%,-50%) scale(1)} 50%{transform:translate(-50%,-50%) scale(1.05)} 100%{transform:translate(-50%,-50%) scale(1)} }
  .hero-top { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
  .hero-status { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .hero-status.on { background: var(--green); box-shadow: 0 0 12px var(--green); }
  .hero-status.off { background: var(--red); box-shadow: 0 0 12px var(--red); }
  .hero-name { font-size: 1rem; font-weight: 600; flex: 1; }
  .hero-model { font-size: .65rem; color: var(--text3); text-transform: uppercase; letter-spacing: .5px; }

  .hero-hr { font-size: 1.8rem; font-weight: 800; line-height: 1; margin-bottom: 6px; text-shadow: var(--glow-cyan); }
  .hero-hr .unit { font-size: .85rem; font-weight: 400; color: var(--text3); }
  .hero-stats { display: flex; gap: 16px; margin-top: 10px; }
  .hero-stat .hs-label { font-size: .62rem; color: var(--text3); text-transform: uppercase; }
  .hero-stat .hs-value { font-size: .85rem; font-weight: 600; text-shadow: 0 0 6px rgba(0,240,255,.2); }

  .hero-bg-hash { position: absolute; right: -10px; bottom: -10px; font-size: 6rem; font-weight: 900;
                  color: rgba(247,147,26,.04); line-height: 1; pointer-events: none; user-select: none; }

  /* Detail view */
  .detail-header { display: flex; align-items: center; gap: 16px; margin-bottom: 22px;
                   padding-bottom: 16px; border-bottom: 1px solid rgba(255,255,255,.06); }
  .detail-back { background: none; border: none; color: var(--text3); cursor: pointer; font-size: 1.4rem;
                  padding: 4px 10px; border-radius: 6px; transition: all .15s; }
  .detail-back:hover { background: var(--surface2); color: var(--text); }
  .detail-title { font-size: 1.3rem; font-weight: 700; display: flex; align-items: center; gap: 12px; }
  .detail-title .dt-status { width: 12px; height: 12px; border-radius: 50%; }
  .detail-nav { display: flex; gap: 2px; background: var(--surface); border-radius: var(--radius);
                padding: 4px; margin-bottom: 20px; flex-wrap: wrap; }
  .detail-nav .dn-item { padding: 8px 18px; font-size: .78rem; font-weight: 500; color: var(--text3);
                          cursor: pointer; border-radius: 6px; transition: all .15s; user-select: none; }
  .detail-nav .dn-item:hover { color: var(--text); background: var(--surface2); }
  .detail-nav .dn-item.active { background: var(--accent); color: #000; }
  .detail-section { display: none; }
  .detail-section.active { display: block; }
  .detail-section .hint { font-size: .7rem; color: var(--text3); margin-left: 4px; }

  /* Metric cards in detail */
  .metric-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px;
                 margin-bottom: 18px; }
  .metric-card { background: var(--surface); border-radius: var(--radius); padding: 14px 16px; border: 1px solid rgba(0,240,255,.06);
                 border: 1px solid rgba(255,255,255,.03); }
  .metric-card .mc-label { font-size: .65rem; color: var(--text3); text-transform: uppercase; letter-spacing: .4px; }
  .metric-card .mc-value { font-size: 1.15rem; font-weight: 700; margin-top: 2px; }
  .metric-card .mc-sub { font-size: .72rem; color: var(--text3); margin-top: 1px; }
  .metric-card .mc-value.warn { color: var(--yellow); }
  .metric-card .mc-value.bad { color: var(--red); }
  .metric-card .mc-value.good { color: var(--green); }

  /* Controls */
  .btn-group { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
  .btn { padding: 7px 18px; border: none; border-radius: 6px; font-size: .8rem; font-weight: 600;
         cursor: pointer; transition: all .12s; display: inline-flex; align-items: center; gap: 6px; }
  .btn:active { transform: scale(.96); }
  .btn:disabled { opacity: .35; cursor: not-allowed; transform: none; }
  .btn-primary { background: var(--accent); color: #000; box-shadow: 0 0 10px rgba(0,240,255,.3); }
  .btn-primary:hover { background: var(--accent2); box-shadow: 0 0 16px rgba(0,240,255,.5); }
  .btn-danger { background: var(--red); color: #fff; }
  .btn-danger:hover { opacity: .85; }
  .btn-secondary { background: var(--surface3); color: var(--text); }
  .btn-secondary:hover { background: #2e4a62; }
  .btn-success { background: var(--green); color: #000; }
  .btn-success:hover { opacity: .85; }
  .btn-warning { background: var(--yellow); color: #000; }
  .btn-warning:hover { opacity: .85; }
  .btn-info { background: var(--blue); color: #fff; }
  .btn-info:hover { opacity: .85; }

  /* Form controls */
  .form-group { background: var(--surface); border-radius: var(--radius); padding: 16px;
                 margin-bottom: 12px; border: 1px solid rgba(0,240,255,.06); }
  .form-group-title { font-size: .75rem; color: var(--text2); text-transform: uppercase; letter-spacing: .5px;
                       margin-bottom: 12px; font-weight: 600; }
  .form-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
  .form-row:last-child { margin-bottom: 0; }
  .form-row label { font-size: .8rem; color: var(--text2); min-width: 90px; }
  .form-row input[type=range] { flex: 1; min-width: 100px; accent-color: var(--accent); height: 6px; }
  .form-row input[type=checkbox] { width: auto; height: auto; margin: 0; accent-color: var(--accent); }
  .form-row input[type=number], .form-row input[type=text] {
    background: var(--surface2); border: 1px solid var(--surface3); border-radius: 5px;
    padding: 5px 10px; color: var(--text); font-size: .85rem; }
  .form-row input[type=number] { width: 80px; }
  .form-row input[type=text] { flex: 1; min-width: 120px; }
  .form-row .fv { font-size: .85rem; font-weight: 600; min-width: 38px; text-align: right; }
  .form-row .funit { font-size: .78rem; color: var(--text3); }

  /* Status badge */
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: .7rem; font-weight: 600; }
  .badge-green { background: rgba(0,255,136,.12); color: var(--green); }
  .badge-red { background: rgba(255,0,85,.12); color: var(--red); }
  .badge-yellow { background: rgba(255,221,0,.12); color: var(--yellow); }
  .badge-blue { background: rgba(51,102,255,.12); color: var(--blue); }

  /* Toast */
  .toast-container { position: fixed; top: 14px; right: 14px; z-index: 9999;
                      display: flex; flex-direction: column; gap: 8px; }
  .toast { padding: 10px 20px; border-radius: var(--radius); font-size: .82rem; font-weight: 500;
           box-shadow: 0 4px 16px rgba(0,0,0,.5); animation: slideIn .2s ease; max-width: 380px;
           backdrop-filter: blur(8px); }
  .toast.success { background: rgba(0,255,136,.85); color: #000; }
  .toast.error { background: rgba(255,0,85,.85); color: #fff; }
  .toast.info { background: rgba(0,240,255,.85); color: #000; }
  @keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

  .footer { text-align: center; padding: 24px; font-size: .7rem; color: var(--text3); }

  @media (max-width: 640px) {
    .header { padding: 10px 16px; }
    .global-nav { padding: 0 16px; overflow-x: auto; }
    .panel { padding: 14px 16px; }
    .hero-hr { font-size: 1.4rem; }
    .metric-grid { grid-template-columns: 1fr 1fr; }
    .detail-nav { overflow-x: auto; flex-wrap: nowrap; }
  }
</style>
</head>
<body>
<div class="toast-container" id="toastContainer"></div>

<div class="header">
  <div class="header-left">
    <h1><span class="btc">&#923;</span> Bitaxe Dashboard</h1>
    <span class="header-status" id="fetchStatus">starting...</span>
  </div>
  <div style="font-size:.78rem;color:var(--text3)">
    <span id="refreshCount">{{ REFRESH_S }}</span>s
    <span style="margin:0 6px">&#183;</span>
    <span id="serverTime"></span>
  </div>
</div>

<div class="global-nav">
  <div class="nav-item active" onclick="showPanel('monitor')">Monitor</div>
  <div class="nav-item" onclick="showPanel('controls')">Controls</div>
  <div class="nav-item" onclick="showPanel('history')">History</div>
  <div class="nav-item" onclick="showPanel('plan')">Plan</div>
  <div class="nav-item kiosk" onclick="showPanel('kiosk'); startKioskRotate()" id="navKiosk">Kiosk</div>
  <div class="nav-item" onclick="showPanel('settings')">Settings</div>
</div>

<!-- ======================== MONITOR ======================== -->
<div class="panel active" id="panelMonitor">
  <div id="poolBar" class="pool-bar">Loading pool data...</div>
  <div id="heroGrid" class="hero-grid">Loading miners...</div>
</div>

<!-- ======================== KIOSK ======================== -->
<div class="panel" id="panelKiosk">
  <div id="kioskContent" class="kiosk-wrap">Select Kiosk to start rotating view.</div>
</div>

<!-- ======================== SETTINGS ======================== -->
<div class="panel" id="panelSettings">
  <div style="max-width:500px">
    <div class="form-group">
      <div class="form-group-title">ASIC Discovery</div>
      <div class="form-row">
        <label>Auto-discover</label>
        <input type="checkbox" id="setAutoDiscover" checked style="width:auto;margin:0" onchange="toggleAutoDiscover()">
      </div>
      <div class="form-row" id="setSubnetRow">
        <label>Scan subnet</label>
        <input type="text" id="setScanSubnet" value="192.168.0.0/24" style="width:140px">
      </div>
      <div class="form-row">
        <label>Static IPs</label>
        <input type="text" id="setStaticIps" placeholder="192.168.0.101,192.168.0.102" style="flex:1">
      </div>
      <div style="font-size:.7rem;color:var(--text3);margin-top:-6px;margin-bottom:12px">Comma or space-separated IP addresses</div>
    </div>
    <div class="form-group">
      <div class="form-group-title">Weather &amp; Throttle</div>
      <div class="form-row">
        <label>City</label>
        <input type="text" id="setWeatherCity" placeholder="e.g. London" style="flex:1" onchange="resolveCity()">
        <button class="btn btn-sm btn-info" onclick="resolveCity()" id="btnResolveCity" style="font-size:.72rem;padding:2px 8px">Resolve</button>
      </div>
      <div class="form-row"><label>Latitude</label><input type="text" id="setWeatherLat" style="width:100px"></div>
      <div class="form-row"><label>Longitude</label><input type="text" id="setWeatherLon" style="width:100px"></div>
      <div id="weatherResolvedMsg" style="font-size:.7rem;color:var(--text3);margin-top:-6px;margin-bottom:12px"></div>
      <div class="form-row"><label>Ambient offset &deg;C</label><input type="number" id="setAmbientOffset" value="-1" min="-1" max="20" style="width:60px"></div>
      <div style="font-size:.7rem;color:var(--text3);margin-top:-6px;margin-bottom:12px">&#8722;1 = auto from weather, 0&#8211;20 = manual offset</div>
      <div class="form-row"><label>Throttle high &deg;C</label><input type="number" id="setThrottleHigh" min="40" max="80" style="width:60px"></div>
      <div class="form-row"><label>Throttle low &deg;C</label><input type="number" id="setThrottleLow" min="35" max="75" style="width:60px"></div>
    </div>
    <div class="form-group">
      <div class="form-group-title">Benchmark Defaults</div>
      <div class="form-row"><label>Duration (s)</label><input type="number" id="setBenchDuration" min="10" max="3600" style="width:70px"></div>
      <div class="form-row"><label>Sample interval (s)</label><input type="number" id="setBenchInterval" min="1" max="30" style="width:60px"></div>
      <div class="form-row"><label>Stabilize (s)</label><input type="number" id="setBenchStabilize" min="0" max="300" style="width:60px"></div>
    </div>
    <div class="form-group">
      <div class="form-group-title">Mobile App</div>
      <div id="pairSection">
        <button class="btn btn-primary" onclick="generatePairQR()" id="btnPairDevice">Pair Device</button>
        <span id="pairStatus" style="font-size:.75rem;color:var(--text3);margin-left:10px"></span>
      </div>
      <div id="pairQRSection" style="display:none;margin-top:12px">
        <div style="text-align:center">
          <img id="pairQRImage" style="border-radius:8px;border:2px solid var(--accent);max-width:200px">
          <div style="font-size:.7rem;color:var(--text3);margin-top:6px">Scan with Bitaxe mobile app</div>
          <div id="pairHost" style="font-size:.75rem;color:var(--accent);margin-top:4px"></div>
        </div>
      </div>
      <div id="pairedDevicesSection" style="margin-top:12px">
        <div style="font-size:.75rem;color:var(--text3);margin-bottom:6px">Paired devices:</div>
        <div id="pairedDevicesList" style="font-size:.75rem"></div>
      </div>
    </div>
    <div style="display:flex;gap:10px;margin-top:16px">
      <button class="btn btn-success" onclick="saveSettings()" id="btnSaveSettings">Save</button>
      <button class="btn btn-danger" onclick="restartApp()" id="btnRestartApp" disabled>Restart to apply</button>
      <span id="settingsStatus" style="font-size:.8rem;color:var(--green);margin-left:8px;line-height:32px"></span>
    </div>
  </div>
</div>

<!-- ======================== HISTORY ======================== -->
<div class="panel" id="panelHistory">
  <div style="display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-bottom:14px">
    <span style="font-size:.82rem;color:var(--text3);font-weight:500">Miner:</span>
    <select id="historyMiner" onchange="loadHistory()"
      style="background:var(--surface2);border:1px solid var(--surface3);border-radius:5px;padding:5px 10px;color:var(--text);font-size:.85rem">
      <option value="">All</option>
    </select>
    <span style="font-size:.82rem;color:var(--text3);font-weight:500">Range:</span>
    <select id="historyRange" onchange="loadHistory()"
      style="background:var(--surface2);border:1px solid var(--surface3);border-radius:5px;padding:5px 10px;color:var(--text);font-size:.85rem">
      <option value="6">6h</option>
      <option value="24" selected>24h</option>
      <option value="72">3d</option>
      <option value="168">7d</option>
    </select>
    <button class="btn btn-secondary" onclick="loadHistory()">Refresh</button>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
    <div class="form-group"><div class="form-group-title">Temperature &amp; Ambient</div>
      <canvas id="chartTemp" height="200" style="width:100%"></canvas></div>
    <div class="form-group"><div class="form-group-title">Frequency &amp; Voltage</div>
      <canvas id="chartFreq" height="200" style="width:100%"></canvas></div>
  </div>
  <div class="form-group">
    <div class="form-group-title">Raw Data</div>
    <div id="historyTableWrap" style="max-height:300px;overflow:auto;font-size:.72rem"></div>
  </div>
</div>

<!-- ======================== PLAN ======================== -->
<div class="panel" id="panelPlan">
  <div style="display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-bottom:14px">
    <span style="font-size:.82rem;color:var(--text3);font-weight:500">24h Hashrate Plan</span>
    <span id="planUpdated" style="font-size:.72rem;color:var(--text3)"></span>
    <button class="btn btn-secondary btn-sm" onclick="loadPlan()">Refresh</button>
  </div>
  <div id="planTableWrap" style="max-height:600px;overflow:auto;font-size:.72rem"></div>
  <div class="form-group" style="margin-top:14px">
    <div class="form-group-title">Thermal Model (chip - ambient delta per freq)</div>
    <div id="planModelWrap" style="font-size:.7rem;max-height:200px;overflow:auto"></div>
  </div>
</div>

<!-- ======================== CONTROLS ======================== -->
  <div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:16px;
              background:var(--surface);border-radius:var(--radius);padding:12px 18px;
              box-shadow:var(--shadow);border:1px solid rgba(255,255,255,.03)">
    <span style="font-size:.78rem;color:var(--text3);font-weight:500">Batch:</span>
    <button class="btn btn-danger btn-sm" onclick="batchAction('restart')">Restart All</button>
    <button class="btn btn-info btn-sm" onclick="batchAction('identify')">Identify All</button>
    
  </div>
  <div id="controlsGrid" class="hero-grid">Loading controls...</div>
</div>

<!-- ======================== DETAIL VIEW ======================== -->
<div class="panel" id="panelDetail">
  <div class="detail-header">
    <button class="detail-back" onclick="closeDetail()">&larr;</button>
    <div class="detail-title">
      <span class="dt-status" id="dtStatus"></span>
      <span id="dtName"></span>
      <span class="badge badge-blue" id="dtModel" style="font-size:.7rem"></span>
      <span class="badge" id="dtVersion" style="font-size:.65rem;background:var(--surface3);color:var(--text3)"></span>
    </div>
  </div>
  <div class="detail-nav" id="detailNav">
    <div class="dn-item active" onclick="showDetailSection('overview')">Overview</div>
    <div class="dn-item" onclick="showDetailSection('controls')">Controls</div>
    <div class="dn-item" onclick="showDetailSection('fan')">Fan</div>
    <div class="dn-item" onclick="showDetailSection('overclock')">Overclock</div>
    <div class="dn-item" onclick="showDetailSection('pool')">Pool</div>
    <div class="dn-item" onclick="showDetailSection('system')">System</div>
  </div>

  <!-- Overview -->
  <div class="detail-section active" id="dsOverview">
    <div class="metric-grid" id="dtOverviewMetrics"></div>
    <div class="form-group">
      <div class="form-group-title">ASIC Domains</div>
      <div id="dtAsicBars" style="display:flex;gap:4px;margin-bottom:8px"></div>
      <div id="dtAsicInfo" style="font-size:.78rem;color:var(--text3)"></div>
    </div>
  </div>

  <!-- Controls -->
  <div class="detail-section" id="dsControls">
    <div class="form-group">
      <div class="form-group-title">Actions</div>
      <div class="btn-group">
        <button class="btn btn-danger" onclick="detailAction('restart')">&#8635; Restart</button>
        <button class="btn btn-info" onclick="detailAction('identify')">&#9878; Identify</button>
        <button class="btn btn-warning" id="dtBenchBtn" onclick="startBenchmark()">&#9881; Benchmark</button>
      </div>
    </div>
    <div class="form-group">
      <div class="form-group-title">Benchmark Settings</div>
      <div class="form-row"><label>Interval</label><input type="number" id="dtBenchInterval" value="1" min="1" max="30" style="width:60px"> <span class="hint">s</span></div>
      <div class="form-row"><label>Duration</label><input type="number" id="dtBenchDuration" value="180" min="10" max="3600" style="width:80px"> <span class="hint">s</span></div>
      <div class="form-row"><label>Stabilize</label><input type="number" id="dtBenchStabilize" value="30" min="0" max="300" style="width:60px"> <span class="hint">s</span></div>
      <div class="form-row"><label>Start MHz</label><input type="number" id="dtBenchStartFreq" value="500" min="400" max="1000" style="width:70px"></div>
      <div class="form-row"><label>Start mV</label><input type="number" id="dtBenchStartVolt" value="1150" min="1100" max="1400" style="width:70px"></div>
      <div class="form-row"><label>Max MHz</label><input type="number" id="dtBenchMaxFreq" value="900" min="400" max="1000" style="width:70px"></div>
      <div class="form-row"><label>Max mV</label><input type="number" id="dtBenchMaxVolt" value="1300" min="1100" max="1400" style="width:70px"></div>
    </div>
    <div class="form-group" id="dtBenchStatusGroup" style="display:none">
      <div class="form-group-title">Benchmark Progress</div>
      <div id="dtBenchStatus" style="font-size:.82rem;color:var(--text2);line-height:1.6"></div>
    </div>
  </div>

  <!-- Fan -->
  <div class="detail-section" id="dsFan">
    <div class="form-group">
      <div class="form-group-title">Fan Mode</div>
      <div class="form-row">
        <label>Mode</label>
        <button class="btn btn-info" id="dtFanModeBtn" onclick="toggleDtAutoFan()">Auto</button>
        <span style="font-size:.78rem;color:var(--text3)" id="dtFanModeLabel">automatically controlled</span>
      </div>
    </div>
    <div class="form-group">
      <div class="form-group-title">Fan Speed</div>
      <div class="form-row">
        <label>Speed</label>
        <input type="range" min="0" max="100" value="80" id="dtFanSlider"
               oninput="dtFanSlide(this.value)" onchange="dtFanCommit()">
        <span class="fv" id="dtFanVal">80%</span>
      </div>
      <div class="form-row">
        <label>RPM</label>
        <span id="dtFanRpm" style="font-size:.85rem;font-weight:600">0</span>
        <span style="font-size:.78rem;color:var(--text3)">rpm</span>
        <span style="margin-left:12px;font-size:.78rem;color:var(--text3)" id="dtFan2Rpm"></span>
      </div>
    </div>
    <div class="form-group" id="dtFanTargetGroup">
      <div class="form-group-title">Temperature Target</div>
      <div class="form-row">
        <label>Target</label>
        <input type="number" min="30" max="80" value="60" id="dtTempTarget"
               onchange="setDtSetting({temptarget:parseInt(this.value)})">
        <span class="funit">&deg;C</span>
      </div>
      <div class="form-row">
        <label>Min Speed</label>
        <input type="number" min="0" max="100" value="25" id="dtMinFan"
               onchange="setDtSetting({minFanSpeed:parseInt(this.value)})">
        <span class="funit">%</span>
      </div>
    </div>
  </div>

  <!-- Overclock -->
  <div class="detail-section" id="dsOverclock">
    <div style="background:rgba(243,156,18,.1);border:1px solid rgba(243,156,18,.2);border-radius:var(--radius);
                padding:12px 16px;margin-bottom:14px;font-size:.8rem;color:var(--yellow)">
      &#9888; Changes to frequency and voltage require a restart to take effect.
      Overclocking without adequate cooling may damage your hardware.
    </div>
    <div class="form-group">
      <div class="form-group-title">Overclock Mode</div>
      <div class="form-row">
        <label>OC Mode</label>
        <button class="btn" id="dtOcBtn" onclick="toggleDtOc()">Disabled</button>
      </div>
    </div>
    <div class="form-group">
      <div class="form-group-title">Auto-Throttle</div>
      <div class="form-row">
        <label>Throttler</label>
        <button class="btn btn-success" id="dtThrottleBtn" onclick="toggleDtThrottle()">Enabled</button>
      </div>
    </div>
    <div class="form-group">
      <div class="form-group-title">ASIC Settings</div>
      <div class="form-row">
        <label>Frequency</label>
        <input type="number" min="25" max="1000" step="25" value="400" id="dtFreq"
               onchange="setDtSetting({frequency:parseInt(this.value)})">
        <span class="funit">MHz</span>
      </div>
      <div class="form-row">
        <label>Core Voltage</label>
        <input type="number" min="1000" max="1400" step="10" value="1200" id="dtCoreV"
               onchange="setDtSetting({coreVoltage:parseInt(this.value)})">
        <span class="funit">mV</span>
      </div>
      <div class="form-row">
        <label>Actual Voltage</label>
        <span id="dtCoreVActual" style="font-size:.85rem;font-weight:600">-</span>
        <span class="funit">mV</span>
      </div>
    </div>
  </div>

  <!-- Pool -->
  <div class="detail-section" id="dsPool">
    <div class="form-group">
      <div class="form-group-title">Primary Pool</div>
      <div class="form-row">
        <label>URL</label>
        <input type="text" id="dtPoolUrl" onchange="setDtSetting({stratumURL:this.value})">
      </div>
      <div class="form-row">
        <label>Port</label>
        <input type="number" id="dtPoolPort" min="1" max="65535"
               onchange="setDtSetting({stratumPort:parseInt(this.value)})">
      </div>
      <div class="form-row">
        <label>User</label>
        <input type="text" id="dtPoolUser" onchange="setDtSetting({stratumUser:this.value})">
      </div>
      <div class="form-row">
        <label>Password</label>
        <input type="text" id="dtPoolPass" placeholder="(set via API)" readonly
               style="opacity:.5;cursor:not-allowed">
        <span style="font-size:.72rem;color:var(--text3)">read-only</span>
      </div>
    </div>
    <div class="form-group">
      <div class="form-group-title">Failover Pool</div>
      <div class="form-row">
        <label>URL</label>
        <input type="text" id="dtFbUrl" onchange="setDtSetting({fallbackStratumURL:this.value})">
      </div>
      <div class="form-row">
        <label>Port</label>
        <input type="number" id="dtFbPort" min="1" max="65535"
               onchange="setDtSetting({fallbackStratumPort:parseInt(this.value)})">
      </div>
      <div class="form-row">
        <label>User</label>
        <input type="text" id="dtFbUser" onchange="setDtSetting({fallbackStratumUser:this.value})">
      </div>
    </div>
    <div class="form-group">
      <div class="form-group-title">Pool Status</div>
      <div class="form-row">
        <label>Difficulty</label>
        <span id="dtPoolDiff" style="font-size:.85rem;font-weight:600">-</span>
      </div>
      <div class="form-row">
        <label>Block Height</label>
        <span id="dtBlockHeight" style="font-size:.85rem;font-weight:600">-</span>
      </div>
      <div class="form-row">
        <label>Net Difficulty</label>
        <span id="dtNetDiff" style="font-size:.85rem;font-weight:600">-</span>
      </div>
      <div class="form-row">
        <label>Shares</label>
        <span id="dtShares" style="font-size:.85rem;font-weight:600">-</span>
        <span id="dtRejects" style="font-size:.78rem;color:var(--text3)"></span>
      </div>
    </div>
  </div>

  <!-- System -->
  <div class="detail-section" id="dsSystem">
    <div class="form-group">
      <div class="form-group-title">Device Info</div>
      <div class="metric-grid" id="dtSysMetrics"></div>
    </div>
    <div class="form-group">
      <div class="form-group-title">Network</div>
      <div class="metric-grid" id="dtNetMetrics"></div>
    </div>
    <div class="form-group">
      <div class="form-group-title">Display</div>
      <div class="form-row">
        <label>Hostname</label>
        <input type="text" id="dtHostname" onchange="setDtSetting({hostname:this.value})">
      </div>
      <div class="form-row">
        <label>Invert Screen</label>
        <button class="btn btn-sm btn-secondary" id="dtInvertBtn" onclick="toggleDtInvert()">Off</button>
      </div>
      <div class="form-row">
        <label>Flip Screen</label>
        <button class="btn btn-sm btn-secondary" id="dtFlipBtn" onclick="toggleDtFlip()">Off</button>
      </div>
      <div class="form-row">
        <label>Rotation</label>
        <input type="number" min="0" max="270" step="90" value="0" id="dtRotation"
               onchange="setDtSetting({rotation:parseInt(this.value)})">
        <span class="funit">&deg;</span>
      </div>
      <div class="form-row">
        <label>Timeout</label>
        <input type="number" min="-1" max="3600" value="-1" id="dtDispTimeout"
               onchange="setDtSetting({displayTimeout:parseInt(this.value)})">
        <span class="funit">s (-1 = always on)</span>
      </div>
    </div>
  </div>
</div>

<div class="footer">Bitaxe Dashboard &middot; {{ REFRESH_S }}s refresh &middot; 7 ASICs</div>

<script>
let countdown = {{ REFRESH_S }};
let currentData = null;
let currentMiner = null;
let currentDetailIp = null;
let _liveTemps = {};
let _blockDismissed = {};
const SQ = "'";

function dismissBlock() {
  document.getElementById('block-toast').style.display = 'none';
  const name = document.getElementById('block-miner').textContent;
  if (name) _blockDismissed[name] = true;
}

function toast(msg, type) {
  const c = document.getElementById('toastContainer');
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.style.transition = 'opacity .3s'; t.style.opacity = '0';
    setTimeout(() => t.remove(), 300); }, 3000);
}

function showPanel(name) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  const nav = document.querySelector('.nav-item[onclick*="' + name + '"]');
  if (nav) nav.classList.add('active');
  const panel = document.getElementById('panel' + name.charAt(0).toUpperCase() + name.slice(1));
  if (panel) panel.classList.add('active');
  if (name === 'settings') loadSettings();
  if (name !== 'kiosk') stopKioskRotate();
}

// ========== FMT HELPERS ==========
function fmtHr(h) {
  if (h >= 1e12) return (h/1e12).toFixed(2) + ' TH/s';
  if (h >= 1e9) return (h/1e9).toFixed(2) + ' GH/s';
  if (h >= 1e6) return (h/1e6).toFixed(2) + ' MH/s';
  if (h >= 1e3) return (h/1e3).toFixed(2) + ' KH/s';
  return (h|0) + ' H/s';
}
function fmtHrShort(h) {
  if (h >= 1e12) return (h/1e12).toFixed(2) + 'T';
  if (h >= 1e9) return (h/1e9).toFixed(2) + 'G';
  if (h >= 1e6) return (h/1e6).toFixed(2) + 'M';
  if (h >= 1e3) return (h/1e3).toFixed(2) + 'K';
  return (h|0) + '';
}
function ago(ts) {
  if (!ts) return 'never'; const d = Date.now()/1000 - ts;
  if (d < 60) return (d|0) + 's'; if (d < 3600) return (d/60|0) + 'm';
  if (d < 86400) return (d/3600).toFixed(1) + 'h'; return (d/86400).toFixed(1) + 'd';
}
function dur(s) {
  if (s < 60) return (s|0) + 's'; if (s < 3600) return (s/60|0) + 'm';
  if (s < 86400) return (s/3600).toFixed(1) + 'h'; return (s/86400).toFixed(1) + 'd';
}
function tempCls(t) { if (t < 0) return ''; if (t >= 75) return 'bad'; if (t >= 65) return 'warn'; return 'good'; }

// ========== RENDER ==========
function renderPool(data) {
  const p = data.pool; if (p.error) return '<div style="color:var(--red)">Pool: ' + p.error + '</div>';
  return '<div class="pool-stat"><div class="pl">Hashrate (1m)</div><div class="pv">' + fmtHr(p.hashrate1m) + '</div></div>' +
    '<div class="pool-stat"><div class="pl">Workers</div><div class="pv">' + p.workers_online + '/' + p.workers +
    ' <span style="font-size:.72rem;font-weight:400;color:var(--text3)">online</span></div></div>' +
    '<div class="pool-stat"><div class="pl">Best Share</div><div class="pv">' + fmtHrShort(p.bestshare) + '</div></div>' +
    '<div class="pool-stat"><div class="pl">Shares</div><div class="pv">' + (p.worker_list||[]).reduce(function(s,w){return s+w.shares},0) + '</div></div>';
}

function renderHeroes(data) {
  return (data.miners||[]).map(m => {
    const off = m.error;
    const sd = m.best_session_diff || 0;
    const nd = m.network_diff || 1;
    const ms = sd >= nd ? ' block' : sd >= 1000000000 ? ' gold' : sd >= 100000000 ? ' milestone' : '';
    const tr = m.throttle_flash ? ' throttle' : '';
    const td = m.throttle_disabled ? ' thr-off' : '';
    const tb = m.benchmarking ? ' bench' : '';
    return '<div class="hero' + (off ? ' offline' : '') + ms + tr + td + tb + '" onclick="openDetail(' + SQ + m.ip + SQ + ')">' +
      '<div class="hero-bg-hash">#</div>' +
      '<div class="hero-top">' +
        '<span class="hero-status ' + (off ? 'off' : 'on') + '"></span>' +
        '<span class="hero-name">' + m.hostname + '</span>' +
        '<span class="hero-model">' + m.asic_model + '</span>' +
        (m.benchmarking ? '<span class="badge badge-yellow" style="margin-left:auto;font-size:.6rem">BENCHING</span>' :
          (m.throttle_disabled ? '<span class="badge badge-red" style="margin-left:auto;font-size:.6rem">THR-OFF</span>' : '')) +
        '<button class="btn-bench-hero" onclick="event.stopPropagation();startBenchmarkOn(' + SQ + m.ip + SQ + ')" ' +
          (m.benchmarking || off ? 'disabled' : '') + ' title="Benchmark">&#9881;</button>' +
      '</div>' +
      '<div class="hero-hr">' + fmtHrShort(m.hashrate) + ' <span class="unit">' +
        (m.hashrate >= 1e12 ? 'TH/s' : m.hashrate >= 1e9 ? 'GH/s' : 'MH/s') + '</span></div>' +
      '<div class="hero-stats">' +
        '<div class="hero-stat"><div class="hs-label">Temp</div><div class="hs-value" style="color:' +
          (m.temp >= 65 ? 'var(--yellow)' : m.temp >= 75 ? 'var(--red)' : 'var(--green)') + '">' +
          (m.temp >= 0 ? m.temp.toFixed(0) + '&deg;C' : 'N/A') + '</div></div>' +
        '<div class="hero-stat"><div class="hs-label">Power</div><div class="hs-value">' + m.power.toFixed(0) + 'W</div></div>' +
        '<div class="hero-stat"><div class="hs-label">Uptime</div><div class="hs-value">' + dur(m.uptime) + '</div></div>' +
        '<div class="hero-stat"><div class="hs-label">Shares</div><div class="hs-value">' +
          (m.shares_accepted || 0) + '</div></div>' +
      '</div>' +
      (m.benchmarking ? '<div style="font-size:.7rem;color:var(--yellow);margin-top:4px" id="benchStatus-' + m.ip.replace(/\./g,'_') + '"></div>' : '') +
      (m.needs_benchmark ? '<div class="new-asic-banner">&#9881; New ASIC — <a href="#" onclick="event.stopPropagation();startBenchmarkOn(' + SQ + m.ip + SQ + ')">Run Benchmark</a> &middot; <a href="#" onclick="event.stopPropagation();dismissBench(' + SQ + m.ip + SQ + ')">Dismiss</a></div>' : '') +
      (off ? '<div style="color:var(--red);font-size:.78rem;margin-top:8px">' + m.error + '</div>' : '') +
    '</div>';
  }).join('');
}

function renderControlCards(data) {
  return (data.miners||[]).map(m => {
    const off = m.error;
    const sd = m.best_session_diff || 0;
    const nd = m.network_diff || 1;
    const ms = sd >= nd ? ' block' : sd >= 1000000000 ? ' gold' : sd >= 100000000 ? ' milestone' : '';
    const tr = m.throttle_flash ? ' throttle' : '';
    const td = m.throttle_disabled ? ' thr-off' : '';
    const tb = m.benchmarking ? ' bench' : '';
    return '<div class="hero' + (off ? ' offline' : '') + ms + tr + td + tb + '" onclick="openDetail(' + SQ + m.ip + SQ + ')">' +
      '<div class="hero-top">' +
        '<span class="hero-status ' + (off ? 'off' : 'on') + '"></span>' +
        '<span class="hero-name">' + m.hostname + '</span>' +
        '<span class="hero-model">' + m.ip + '</span>' +
        (m.benchmarking ? '<span class="badge badge-yellow" style="margin-left:auto;font-size:.6rem">BENCHING</span>' :
          (m.throttle_disabled ? '<span class="badge badge-red" style="margin-left:auto;font-size:.6rem">THR-OFF</span>' : '')) +
        '<button class="btn-bench-hero" onclick="event.stopPropagation();startBenchmarkOn(' + SQ + m.ip + SQ + ')" ' +
          (m.benchmarking || off ? 'disabled' : '') + ' title="Benchmark">&#9881;</button>' +
      '</div>' +
      '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px">' +
        '<button class="btn btn-danger btn-sm" onclick="event.stopPropagation();minerAction(' + SQ + m.ip + SQ + ',' + SQ + 'restart' + SQ + ')"' + (off?' disabled':'') + '>Restart</button>' +

        '<button class="btn btn-info btn-sm" onclick="event.stopPropagation();minerAction(' + SQ + m.ip + SQ + ',' + SQ + 'identify' + SQ + ')"' + (off?' disabled':'') + '>&#9878;</button>' +
      '</div>' +
      '<div style="margin-top:8px;font-size:.78rem;color:var(--text3)">' +
        m.frequency + 'MHz / ' + m.core_voltage + 'mV &middot; Fan: ' + (m.autofanspeed ? 'Auto' : 'Manual') + ' ' + (m.fan_speed||0) + '% &middot; ' + fmtHrShort(m.hashrate) +
      '</div>' +
      (m.needs_benchmark ? '<div class="new-asic-banner">&#9881; New ASIC — <a href="#" onclick="event.stopPropagation();startBenchmarkOn(' + SQ + m.ip + SQ + ')">Run Benchmark</a> &middot; <a href="#" onclick="event.stopPropagation();dismissBench(' + SQ + m.ip + SQ + ')">Dismiss</a></div>' : '') +
      (off ? '<div style="color:var(--red);font-size:.78rem;margin-top:6px">' + m.error + '</div>' : '') +
    '</div>';
  }).join('');
}

// ========== BENCHMARK ==========
function dismissBench(ip) {
  fetch('api/benchmark/' + ip + '/dismiss', {method:'POST'});
  const cards = document.querySelectorAll('.hero');
  cards.forEach(c => { const d = c.querySelector('.new-asic-banner'); if (d) d.remove(); });
}

function loadSettings() {
  fetch('api/settings').then(r => r.json()).then(s => {
    document.getElementById('setStaticIps').value = s.static_ips || '';
    document.getElementById('setAutoDiscover').checked = s.auto_discover !== false;
    document.getElementById('setScanSubnet').value = s.scan_subnet || '192.168.0.0/24';
    document.getElementById('setWeatherCity').value = s.weather_city || '';
    document.getElementById('setWeatherLat').value = s.weather_lat || '';
    document.getElementById('setWeatherLon').value = s.weather_lon || '';
    const msg = document.getElementById('weatherResolvedMsg');
    if (s.weather_city && s.weather_lat && s.weather_lon) msg.textContent = s.weather_city + ' resolved to ' + s.weather_lat + ', ' + s.weather_lon;
    else msg.textContent = s.weather_lat && s.weather_lon ? 'Coordinates set directly' : 'Enter city and click Resolve';
    document.getElementById('setAmbientOffset').value = s.ambient_offset != null ? s.ambient_offset : -1;
    document.getElementById('setThrottleHigh').value = s.throttle_high || 59;
    document.getElementById('setThrottleLow').value = s.throttle_low || 52;
    document.getElementById('setBenchDuration').value = s.benchmark_duration || 180;
    document.getElementById('setBenchInterval').value = s.benchmark_sample_interval || 1;
    document.getElementById('setBenchStabilize').value = s.benchmark_stabilize_time || 30;
    toggleAutoDiscover();
    loadPairedDevices();
  });
}

function toggleAutoDiscover() {
  const checked = document.getElementById('setAutoDiscover').checked;
  document.getElementById('setSubnetRow').style.display = checked ? 'flex' : 'none';
}

function resolveCity() {
  const city = document.getElementById('setWeatherCity').value.trim();
  const msg = document.getElementById('weatherResolvedMsg');
  if (!city) { msg.textContent = ''; return; }
  msg.textContent = 'Resolving...';
  document.getElementById('btnResolveCity').disabled = true;
  fetch('api/geocode', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({city:city})
  }).then(r => r.json()).then(d => {
    if (d.ok && d.matches && d.matches.length) {
      const m = d.matches[0];
      document.getElementById('setWeatherLat').value = m.lat;
      document.getElementById('setWeatherLon').value = m.lon;
      msg.textContent = m.name + ', ' + m.country + ' → ' + m.lat + ', ' + m.lon;
    } else {
      msg.textContent = 'Not found: ' + (d.error || 'unknown error');
    }
  }).catch(e => {
    msg.textContent = 'Error: ' + e.message;
  }).finally(function() {
    document.getElementById('btnResolveCity').disabled = false;
  });
}

// ========== MOBILE APP PAIRING ==========
function generatePairQR() {
  document.getElementById('pairStatus').textContent = 'Generating...';
  fetch('api/pair/generate').then(r => r.json()).then(d => {
    if (d.ok) {
      document.getElementById('pairQRImage').src = d.qr_data_url;
      document.getElementById('pairHost').textContent = d.host + ':' + d.port;
      document.getElementById('pairQRSection').style.display = 'block';
      document.getElementById('pairStatus').textContent = 'QR valid for 5 minutes';
      loadPairedDevices();
    } else {
      document.getElementById('pairStatus').textContent = d.error;
    }
  }).catch(e => {
    document.getElementById('pairStatus').textContent = 'Failed: ' + e.message;
  });
}

function loadPairedDevices() {
  fetch('api/pair/devices').then(r => r.json()).then(d => {
    const list = document.getElementById('pairedDevicesList');
    if (!d.devices || d.devices.length === 0) {
      list.innerHTML = '<span style="color:var(--text3)">No paired devices</span>';
      return;
    }
    list.innerHTML = d.devices.map(dev => {
      const paired = new Date(dev.paired_at * 1000).toLocaleString();
      const seen = new Date(dev.last_seen * 1000).toLocaleString();
      return '<div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--surface2)">' +
        '<span style="flex:1">' + dev.name + ' <span style="color:var(--text3)">(' + dev.key + ')</span></span>' +
        '<span style="color:var(--text3);font-size:.65rem">Paired: ' + paired + '</span>' +
        '<button class="btn btn-sm btn-danger" onclick="revokeDevice(' + SQ + dev.key + SQ + ')" style="padding:1px 6px;font-size:.6rem">Revoke</button>' +
        '</div>';
    }).join('');
  });
}

function revokeDevice(key) {
  fetch('api/pair/revoke', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({key:key})})
    .then(r => r.json()).then(d => { if (d.ok) loadPairedDevices(); });
}

// ========== SETTINGS ==========
function saveSettings() {
  const body = {
    static_ips: document.getElementById('setStaticIps').value.trim(),
    auto_discover: document.getElementById('setAutoDiscover').checked,
    scan_subnet: document.getElementById('setScanSubnet').value.trim(),
    weather_city: document.getElementById('setWeatherCity').value.trim(),
    weather_lat: document.getElementById('setWeatherLat').value.trim(),
    weather_lon: document.getElementById('setWeatherLon').value.trim(),
    ambient_offset: parseInt(document.getElementById('setAmbientOffset').value) || -1,
    throttle_high: parseInt(document.getElementById('setThrottleHigh').value) || 59,

    throttle_low: parseInt(document.getElementById('setThrottleLow').value) || 52,
    benchmark_duration: parseInt(document.getElementById('setBenchDuration').value) || 180,
    benchmark_sample_interval: parseInt(document.getElementById('setBenchInterval').value) || 1,
    benchmark_stabilize_time: parseInt(document.getElementById('setBenchStabilize').value) || 30,
  };
  document.getElementById('btnSaveSettings').disabled = true;
  document.getElementById('settingsStatus').textContent = 'Saving...';
  fetch('api/settings', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      document.getElementById('settingsStatus').textContent = 'Saved. Restart to apply.';
      document.getElementById('btnRestartApp').disabled = false;
    } else {
      document.getElementById('settingsStatus').textContent = 'Save failed';
    }
    document.getElementById('btnSaveSettings').disabled = false;
  }).catch(() => {
    document.getElementById('settingsStatus').textContent = 'Save error';
    document.getElementById('btnSaveSettings').disabled = false;
  });
}

function restartApp() {
  document.getElementById('btnRestartApp').disabled = true;
  document.getElementById('settingsStatus').textContent = 'Restarting...';
  fetch('api/restart', {method:'POST'});
}

function startBenchmarkOn(ip) {
  const btn = event && event.target || document.querySelector('[onclick*="' + ip + '"]');
  if (btn) btn.disabled = true;
  fetch('api/benchmark/' + ip, {method:'POST'})
    .then(r => r.json()).then(d => {
      if (d.ok) {
        toast('Benchmark started for ' + ip.split('.').pop(), 'success');
      } else {
        toast('Benchmark failed: ' + (d.error||'unknown'), 'error');
        if (btn) btn.disabled = false;
      }
    }).catch(e => {
      toast('Request failed: ' + e.message, 'error');
      if (btn) btn.disabled = false;
    });
}

function startBenchmark() {
  if (!currentMiner) return;
  const ip = currentMiner.ip;
  const btn = document.getElementById('dtBenchBtn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Starting...';
  const body = {
    duration: parseInt(document.getElementById('dtBenchDuration').value) || 180,
    sample_interval: parseInt(document.getElementById('dtBenchInterval').value) || 1,
    stabilize_time: parseInt(document.getElementById('dtBenchStabilize').value) || 30,
    start_frequency: parseInt(document.getElementById('dtBenchStartFreq').value) || 500,
    start_voltage: parseInt(document.getElementById('dtBenchStartVolt').value) || 1150,
    max_frequency: parseInt(document.getElementById('dtBenchMaxFreq').value) || 900,
    max_voltage: parseInt(document.getElementById('dtBenchMaxVolt').value) || 1300,
  };
  fetch('api/benchmark/' + ip, {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
  }).then(r => r.json()).then(d => {
      if (d.ok) {
        toast('Benchmark started for ' + ip.split('.').pop(), 'success');
        document.getElementById('dtBenchStatusGroup').style.display = 'block';
        pollBenchmarkStatus(ip);
      } else {
        toast('Benchmark failed: ' + (d.error||'unknown'), 'error');
        btn.disabled = false;
        btn.textContent = '\u2699 Benchmark';
      }
    }).catch(e => {
      toast('Request failed: ' + e.message, 'error');
      btn.disabled = false;
      btn.textContent = '\u2699 Benchmark';
    });
}

function pollBenchmarkStatus(ip) {
  fetch('api/benchmark/' + ip + '/status')
    .then(r => r.json()).then(s => {
      const div = document.getElementById('dtBenchStatus');
      const group = document.getElementById('dtBenchStatusGroup');
      const btn = document.getElementById('dtBenchBtn');
      if (!div) return;
      if (s.running) {
        div.innerHTML = '<b>Step ' + (s.step||0) + ':</b> ' + (s.message||'') +
          '<br><span style="font-size:.72rem;color:var(--text3)">' + (s.freq||'?') + 'MHz @ ' + (s.volt||'?') + 'mV' +
          (s.expected_ghs ? ' (exp ' + s.expected_ghs.toFixed(1) + ' GH/s)' : '') + '</span>';
        group.style.display = 'block';
        setTimeout(function(){ pollBenchmarkStatus(ip); }, 3000);
        if (btn) { btn.disabled = true; btn.textContent = 'Benching...'; }
        if (currentMiner) { currentMiner.benchmarking = 1; }
      } else {
        div.innerHTML = s.message || 'Benchmark complete';
        if (btn) { btn.disabled = false; btn.textContent = '\u2699 Benchmark'; }
        if (currentMiner) { currentMiner.benchmarking = 0; }
        if (s.error) {
          div.innerHTML += '<br><span style="color:var(--red)">Error: ' + s.error + '</span>';
          toast('Benchmark error: ' + s.error, 'error');
        } else {
          toast('Benchmark complete for ' + ip.split('.').pop(), 'success');
        }
      }
    }).catch(e => {
      setTimeout(function(){ pollBenchmarkStatus(ip); }, 5000);
    });
}

function setDtSetting(settings) {
  const ip = currentDetailIp; if (!ip) return;
  fetch('api/bitaxe/' + ip + '/settings', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(settings)
  }).then(r => r.json()).then(d => {
    toast(Object.keys(settings)[0] + ' ' + (d.ok ? 'updated' : 'failed'), d.ok ? 'success' : 'error');
    if (d.ok) { if (currentMiner) Object.assign(currentMiner, settings); }
  }).catch(e => toast('Request failed: ' + e.message, 'error'));
}

function batchAction(action) {
  if (action === 'restart' && !confirm('Restart ALL miners?')) return;
  const miners = (currentData && currentData.miners) || [];
  toast('Sending ' + action + ' to ' + miners.length + ' miners...', 'info');
  miners.forEach(m => { if (!m.error) fetch('api/bitaxe/' + m.ip + '/' + action, {method:'POST'}); });
}

// ========== KIOSK ROTATE ==========
let kioskInterval = null;
let kioskIndex = 0;

function renderKioskHero(m, i, total) {
  const off = m.error;
  const sd = m.best_session_diff || 0;
  const nd = m.network_diff || 1;
  const ms = sd >= nd ? ' block' : sd >= 1000000000 ? ' gold' : sd >= 100000000 ? ' milestone' : '';
  const tr = m.throttle_flash ? ' throttle' : '';
  const dots = [];
  for (let j = 0; j < total; j++) dots.push('<span class="kiosk-dot' + (j === i ? ' active' : '') + '" onclick="kioskIndex=' + j + ';renderKiosk()"></span>');
  return '<div class="kiosk-hero' + ms + tr + '">' +
    '<div class="hero-bg-hash">#</div>' +
    '<div class="hero-top" style="justify-content:center;margin-bottom:18px">' +
      '<span class="hero-status ' + (off ? 'off' : 'on') + '"></span>' +
      '<span class="hero-name">' + m.hostname + '</span>' +
      '<span class="hero-model">' + m.asic_model + '</span>' +
    '</div>' +
    '<div class="hero-hr">' + fmtHrShort(m.hashrate) + ' <span class="unit">' +
      (m.hashrate >= 1e12 ? 'TH/s' : m.hashrate >= 1e9 ? 'GH/s' : 'MH/s') + '</span></div>' +
    '<div class="hero-stats">' +
      '<div class="hero-stat"><div class="hs-label">Temp</div><div class="hs-value" style="color:' +
        (m.temp >= 75 ? 'var(--red)' : m.temp >= 65 ? 'var(--yellow)' : 'var(--green)') + '">' +
        (m.temp >= 0 ? m.temp.toFixed(0) + '&deg;C' : 'N/A') + '</div></div>' +
      '<div class="hero-stat"><div class="hs-label">Power</div><div class="hs-value">' + m.power.toFixed(0) + ' W</div></div>' +
      '<div class="hero-stat"><div class="hs-label">Efficiency</div><div class="hs-value">' +
        (m.hashrate > 0 ? (m.power/(m.hashrate/1e9)).toFixed(1) : '-') + ' W/GH</div></div>' +
      '<div class="hero-stat"><div class="hs-label">Fan</div><div class="hs-value">' + (m.fan_rpm||0) + ' rpm</div></div>' +
      '<div class="hero-stat"><div class="hs-label">Best Diff</div><div class="hs-value">' + fmtHrShort(m.best_diff) + '</div></div>' +
      '<div class="hero-stat"><div class="hs-label">Uptime</div><div class="hs-value">' + dur(m.uptime) + '</div></div>' +
      '<div class="hero-stat"><div class="hs-label">Shares</div><div class="hs-value">' +
        (m.shares_accepted || 0) + '</div></div>' +
    '</div>' +
    (off ? '<div style="color:var(--red);font-size:.9rem;margin-top:12px">' + m.error + '</div>' : '') +
    '<div class="kiosk-progress">' + dots.join('') + '</div>' +
  '</div>';
}

function renderKiosk() {
  const el = document.getElementById('kioskContent');
  if (!el || !currentData) return;
  const miners = currentData.miners || [];
  if (!miners.length) { el.innerHTML = '<div style="color:var(--text3);font-size:1.1rem">No miners discovered.</div>'; return; }
  if (kioskIndex >= miners.length) kioskIndex = 0;
  const m = miners[kioskIndex];
  el.innerHTML = renderKioskHero(m, kioskIndex, miners.length);
}

function startKioskRotate() {
  stopKioskRotate();
  kioskIndex = 0;
  renderKiosk();
  kioskInterval = setInterval(function() {
    if (!currentData || !currentData.miners || !currentData.miners.length) return;
    kioskIndex = (kioskIndex + 1) % currentData.miners.length;
    renderKiosk();
  }, 8000);
}

function stopKioskRotate() {
  if (kioskInterval) { clearInterval(kioskInterval); kioskInterval = null; }
}

// ========== HISTORY ==========
function loadHistory() {
  const ip = document.getElementById('historyMiner').value;
  const hours = document.getElementById('historyRange').value;
  let url = 'api/history?hours=' + hours;
  if (ip) url += '&ip=' + encodeURIComponent(ip);
  fetch(url).then(r => r.json()).then(data => {
    renderHistoryTable(data);
    renderHistoryChart(data);
  });
}

function renderHistoryTable(rows) {
  const el = document.getElementById('historyTableWrap');
  if (!rows.length) { el.innerHTML = '<div style="color:var(--text3)">No data yet.</div>'; return; }
  let h = '<table style="width:100%;border-collapse:collapse"><thead><tr style="color:var(--text3);font-weight:600">' +
    '<th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--surface3)">Time</th>' +
    '<th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--surface3)">Miner</th>' +
    '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid var(--surface3)">Amb</th>' +
    '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid var(--surface3)">Temp</th>' +
    '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid var(--surface3)">VR</th>' +
    '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid var(--surface3)">MHz</th>' +
    '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid var(--surface3)">mV</th>' +
    '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid var(--surface3)">HR</th>' +
    '<th style="text-align:right;padding:4px 8px;border-bottom:1px solid var(--surface3)">W</th></tr></thead><tbody>';
  const recent = rows.slice(-200);
  recent.forEach(r => {
    const t = new Date(r.ts * 1000).toLocaleTimeString();
    h += '<tr><td style="padding:3px 8px;border-bottom:1px solid rgba(255,255,255,.02)">' + t + '</td>' +
      '<td style="padding:3px 8px;border-bottom:1px solid rgba(255,255,255,.02)">' + r.hostname + '</td>' +
      '<td style="padding:3px 8px;border-bottom:1px solid rgba(255,255,255,.02);text-align:right">' + (r.ambient != null ? r.ambient.toFixed(1) : '-') + '</td>' +
      '<td style="padding:3px 8px;border-bottom:1px solid rgba(255,255,255,.02);text-align:right;color:' + (r.temp >= 65 ? 'var(--yellow)' : 'var(--green)') + '">' + r.temp.toFixed(1) + '</td>' +
      '<td style="padding:3px 8px;border-bottom:1px solid rgba(255,255,255,.02);text-align:right">' + r.vr_temp.toFixed(0) + '</td>' +
      '<td style="padding:3px 8px;border-bottom:1px solid rgba(255,255,255,.02);text-align:right">' + r.frequency + '</td>' +
      '<td style="padding:3px 8px;border-bottom:1px solid rgba(255,255,255,.02);text-align:right">' + r.core_voltage + '</td>' +
      '<td style="padding:3px 8px;border-bottom:1px solid rgba(255,255,255,.02);text-align:right">' + fmtHrShort(r.hashrate) + '</td>' +
      '<td style="padding:3px 8px;border-bottom:1px solid rgba(255,255,255,.02);text-align:right">' + r.power.toFixed(1) + '</td></tr>';
  });
  h += '</tbody></table><div style="color:var(--text3);font-size:.65rem;margin-top:4px">Showing last ' + Math.min(rows.length,200) + ' of ' + rows.length + ' records</div>';
  el.innerHTML = h;
}

function renderHistoryChart(rows) {
  if (!rows.length) return;
  const ips = [...new Set(rows.map(r => r.ip))];
  const byIp = {};
  rows.forEach(r => {
    if (!byIp[r.ip]) byIp[r.ip] = [];
    byIp[r.ip].push(r);
  });

  // Temp chart
  const c1 = document.getElementById('chartTemp');
  const ctx1 = c1.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  c1.width = (c1.clientWidth || 600) * dpr;
  c1.height = (c1.clientHeight || 200) * dpr;
  ctx1.scale(dpr, dpr);
  const w = c1.width / dpr, h = c1.height / dpr;

  const colors = ['#f7931a','#3498db','#2ecc71','#e74c3c','#f39c12','#9b59b6','#1abc9c'];
  const allTemps = rows.filter(r => r.temp >= 0).map(r => r.temp);
  const allAmb = rows.filter(r => r.ambient != null).map(r => r.ambient);
  const yMin = Math.min(0, ...allAmb, ...allTemps);
  const yMax = Math.max(60, ...allTemps) + 10;

  const drawChart = (ctx, datasets, yMin, yMax) => {
    ctx.clearRect(0, 0, w, h);
    const pad = {t: 20, r: 10, b: 30, l: 40};
    const cw = w - pad.l - pad.r, ch = h - pad.t - pad.b;
    // Grid lines
    ctx.strokeStyle = 'rgba(255,255,255,.06)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + (ch / 4) * i;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    }
    // Y labels
    ctx.fillStyle = '#5a7a94'; ctx.font = '10px sans-serif'; ctx.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
      const v = yMax - ((yMax - yMin) / 4) * i;
      ctx.fillText(v.toFixed(0), pad.l - 6, pad.t + (ch / 4) * i + 4);
    }
    // Datasets
    datasets.forEach((ds, di) => {
      if (!ds.points.length) return;
      ctx.strokeStyle = colors[di % colors.length];
      ctx.lineWidth = 2;
      ctx.beginPath();
      ds.points.forEach((p, i) => {
        const x = pad.l + (i / Math.max(ds.points.length - 1, 1)) * cw;
        const y = pad.t + (1 - (p - yMin) / (yMax - yMin)) * ch;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      });
      ctx.stroke();
    });
    // Ambient fill
    const ambDs = datasets.find(d => d.label === 'Ambient');
    if (ambDs && ambDs.points.length > 1) {
      ctx.fillStyle = 'rgba(247,147,26,.08)';
      ctx.beginPath();
      ambDs.points.forEach((p, i) => {
        const x = pad.l + (i / (ambDs.points.length - 1)) * cw;
        const y = pad.t + (1 - (p - yMin) / (yMax - yMin)) * ch;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      });
      const lastX = pad.l + cw;
      ctx.lineTo(lastX, pad.t + ch);
      ctx.lineTo(pad.l, pad.t + ch);
      ctx.closePath();
      ctx.fill();
    }
    // X labels
    ctx.fillStyle = '#5a7a94'; ctx.textAlign = 'center';
    const n = Math.min(datasets[0] ? datasets[0].points.length : 0, 6);
    for (let i = 0; i < n; i++) {
      const idx = Math.floor((i / (n-1 || 1)) * ((datasets[0] ? datasets[0].points.length : 0) - 1));
      const ts = rows[idx] ? rows[idx].ts * 1000 : Date.now();
      const t = new Date(ts).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      const x = pad.l + (idx / Math.max((datasets[0] ? datasets[0].points.length : 1) - 1, 1)) * cw;
      ctx.fillText(t, x, h - 6);
    }
  };

  // Temp datasets: one per miner + ambient
  const tempDatasets = [{label: 'Ambient', points: rows.filter(r => r.ambient != null).map(r => r.ambient), color: '#f7931a'}];
  ips.forEach((ip, i) => {
    const pts = byIp[ip].filter(r => r.temp >= 0).map(r => r.temp);
    if (pts.length) tempDatasets.push({label: byIp[ip][0].hostname, points: pts, color: colors[(i+1) % colors.length]});
  });
  drawChart(ctx1, tempDatasets, yMin, yMax);

  // Legend
  ctx1.fillStyle = '#94a8b8'; ctx1.font = '10px sans-serif'; ctx1.textAlign = 'left';
  tempDatasets.forEach((ds, i) => {
    const lx = 10 + i * 100;
    ctx1.fillStyle = colors[i % colors.length];
    ctx1.fillRect(lx, 4, 8, 8);
    ctx1.fillStyle = '#94a8b8';
    ctx1.fillText(ds.label, lx + 12, 12);
  });

  // Freq chart
  const c2 = document.getElementById('chartFreq');
  const ctx2 = c2.getContext('2d');
  c2.width = (c2.clientWidth || 600) * dpr;
  c2.height = (c2.clientHeight || 200) * dpr;
  ctx2.scale(dpr, dpr);
  const freqDatasets = [];
  ips.forEach((ip, i) => {
    const pts = byIp[ip].filter(r => r.frequency > 0).map(r => r.frequency);
    if (pts.length) freqDatasets.push({label: byIp[ip][0].hostname, points: pts, color: colors[(i+1) % colors.length]});
  });
  const allFreq = rows.filter(r => r.frequency > 0).map(r => r.frequency);
  const fMin = Math.min(300, ...allFreq) - 50;
  const fMax = Math.max(500, ...allFreq) + 50;
  drawChart(ctx2, freqDatasets, fMin, fMax);
  freqDatasets.forEach((ds, i) => {
    const lx = 10 + i * 100;
    ctx2.fillStyle = colors[(i+1) % colors.length];
    ctx2.fillRect(lx, 4, 8, 8);
    ctx2.fillStyle = '#94a8b8';
    ctx2.fillText(ds.label, lx + 12, 12);
  });
}

function updateHistoryDropdown() {
  if (!currentData) return;
  const sel = document.getElementById('historyMiner');
  const cur = sel.value;
  sel.innerHTML = '<option value="">All</option>';
  (currentData.miners||[]).forEach(m => {
    const opt = document.createElement('option');
    opt.value = m.ip;
    opt.textContent = m.hostname;
    sel.appendChild(opt);
  });
  sel.value = cur;
}

// ========== DETAIL VIEW ==========
function openDetail(ip) {
  currentDetailIp = ip;
  document.getElementById('panelDetail').style.display = 'block';
  document.getElementById('panelControls').style.display = 'none';
  if (!currentData) return;
  const m = (currentData.miners||[]).find(x => x.ip === ip);
  if (m) {
    currentMiner = m;
    const off = m.error;
    document.getElementById('dtStatus').style.background = off ? 'var(--red)' : 'var(--green)';
    document.getElementById('dtName').textContent = m.hostname;
    document.getElementById('dtModel').textContent = m.asic_model;
    document.getElementById('dtModel').className = 'badge ' + (off ? 'badge-red' : 'badge-blue');
    document.getElementById('dtVersion').textContent = 'v' + (m.axeos_version || m.version || '?').replace(/^v/,'');
    renderDtOverview(m);
    renderDtFan(m);
    renderDtOc(m);
    renderDtPool(m);
    renderDtSystem(m);
    renderDtThrottle(m);
    const bb = document.getElementById('dtBenchBtn');
    if (bb) {
      bb.disabled = !!m.benchmarking || !!m.error;
      bb.textContent = m.benchmarking ? 'Benching...' : '\u2699 Benchmark';
    }
    if (m.benchmarking) {
      document.getElementById('dtBenchStatusGroup').style.display = 'block';
      pollBenchmarkStatus(m.ip);
    }
  }
}

function closeDetail() {
  currentDetailIp = null;
  currentMiner = null;
  document.getElementById('panelDetail').style.display = 'none';
  document.getElementById('panelControls').style.display = 'block';
}

function showDetailSection(name) {
  document.querySelectorAll('.detail-section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.dn-item').forEach(s => s.classList.remove('active'));
  const sec = document.getElementById('ds' + name.charAt(0).toUpperCase() + name.slice(1));
  if (sec) sec.classList.add('active');
  const nav = document.querySelector('.dn-item[onclick*="' + name + '"]');
  if (nav) nav.classList.add('active');
}

function detailAction(action) {
  const ip = currentDetailIp; if (!ip) return;
  if (action === 'restart' && !confirm('Restart this ASIC?')) return;
  fetch('api/bitaxe/' + ip + '/' + action, {method:'POST'})
    .then(r => r.json()).then(d => {
      toast(action + ' ' + (d.ok ? 'sent' : 'failed'), d.ok ? 'success' : 'error');
    }).catch(e => toast('Request failed: ' + e.message, 'error'));
}

function toggleDtAutoFan() {
  if (!currentDetailIp) return;
  const btn = document.getElementById('dtFanModeBtn');
  const isAuto = btn.textContent === 'Auto';
  const newMode = isAuto ? 0 : 1;
  setDtSetting({autofanspeed: newMode});
  btn.textContent = isAuto ? 'Manual' : 'Auto';
  document.getElementById('dtFanModeLabel').textContent = isAuto ? 'manually controlled' : 'automatically controlled';
}

function dtFanSlide(val) {
  document.getElementById('dtFanVal').textContent = val + '%';
  document.getElementById('dtFanSlider').value = val;
}

function dtFanCommit() {
  const val = parseInt(document.getElementById('dtFanSlider').value);
  dtFanSlide(val);
  setDtSetting({manualFanSpeed: val});
}

function toggleDtFlip() {
  if (!currentDetailIp || !currentMiner) return;
  const cur = currentMiner.flipscreen ? 0 : 1;
  setDtSetting({flipscreen: cur});
}

function toggleDtInvert() {
  if (!currentDetailIp || !currentMiner) return;
  const cur = currentMiner.invertscreen ? 0 : 1;
  setDtSetting({invertscreen: cur});
}

function toggleDtOc() {
  if (!currentDetailIp) return;
  const btn = document.getElementById('dtOcBtn');
  const isOn = btn.textContent === 'Enabled';
  const newVal = isOn ? 0 : 1;
  setDtSetting({overclockEnabled: newVal});
  btn.textContent = isOn ? 'Disabled' : 'Enabled';
  btn.className = 'btn ' + (isOn ? '' : 'btn-success');
}

function toggleDtThrottle() {
  const ip = currentDetailIp; if (!ip) return;
  const btn = document.getElementById('dtThrottleBtn');
  const isOn = btn.textContent === 'Enabled';
  fetch('api/bitaxe/' + ip + '/throttle-' + (isOn ? 'disable' : 'enable'), {method:'POST'})
    .then(r => r.json()).then(d => {
      if (d.ok) {
        btn.textContent = isOn ? 'Disabled' : 'Enabled';
        btn.className = 'btn ' + (isOn ? 'btn-danger' : 'btn-success');
        toast('Throttler ' + (isOn ? 'disabled' : 'enabled'), 'success');
      }
    }).catch(e => toast('Toggle failed: ' + e.message, 'error'));
}

function renderDtOverview(m) {
  const el = document.getElementById('dtOverviewMetrics');
  const metrics = [
    {label:'Hashrate', value: fmtHr(m.hashrate) + ' ' + (m.hashrate >= 1e12 ? 'TH/s' : m.hashrate >= 1e9 ? 'GH/s' : 'MH/s')},
    {label:'Chip Temp', value: m.temp >= 0 ? m.temp.toFixed(1) + '°C' : 'N/A', cls: tempCls(m.temp)},
    {label:'VR Temp', value: m.vr_temp >= 0 ? m.vr_temp.toFixed(1) + '°C' : 'N/A'},
    {label:'Power', value: m.power.toFixed(0) + ' W'},
    {label:'Core Voltage', value: m.core_voltage + ' mV (set: ' + m.core_voltage_set + ' mV)'},
    {label:'Frequency', value: m.frequency + ' MHz'},
    {label:'Efficiency', value: m.power > 0 && m.hashrate > 0 ? (m.power / (m.hashrate / 1e9 / 1000)).toFixed(1) + ' J/TH' : '-'},
    {label:'Best Diff', value: m.best_session_diff > 0 ? m.best_session_diff.toExponential(2) : '-'},
    {label:'Shares', value: m.shares_accepted + ' / ' + (m.shares_accepted + m.shares_rejected) + ' (' + m.error_pct.toFixed(1) + '% err)'},
    {label:'Uptime', value: dur(m.uptime)},
    {label:'WiFi RSSI', value: m.wifi_rssi + ' dBm'},
    {label:'Board', value: m.board_version},
  ];
  el.innerHTML = metrics.map(mm => '<div class="metric' + (mm.cls ? ' ' + mm.cls : '') + '"><div class="ml">' + mm.label + '</div><div class="mv">' + mm.value + '</div></div>').join('');
  // ASIC domains
  const barsEl = document.getElementById('dtAsicBars');
  if (m.asics && m.asics.length) {
    barsEl.innerHTML = m.asics.map(d => {
      const pct = Math.min(100, (d.hr || 0) / 100);
      const err = d.errorCount || 0;
      const freq = d.frequency || 0;
      return '<div style="flex:1;text-align:center"><div style="height:60px;background:var(--surface3);border-radius:4px;overflow:hidden;position:relative">' +
        '<div style="height:' + pct + '%;background:' + (err > 3 ? 'var(--red)' : 'var(--accent)') + ';position:absolute;bottom:0;left:0;right:0;transition:height .5s"></div></div>' +
        '<div style="font-size:.6rem;color:var(--text3);margin-top:2px">' + freq + 'MHz</div>' +
        '<div style="font-size:.6rem;color:' + (err > 3 ? 'var(--red)' : 'var(--text3)') + '">' + err + ' err</div></div>';
    }).join('');
  } else {
    barsEl.innerHTML = '<div style="font-size:.78rem;color:var(--text3)">No domain data</div>';
  }
  document.getElementById('dtAsicInfo').textContent = m.asic_count + ' chips, ASIC model: ' + m.asic_model;
}

function renderDtFan(m) {
  document.getElementById('dtFanModeBtn').textContent = m.autofanspeed ? 'Auto' : 'Manual';
  document.getElementById('dtFanModeBtn').className = 'btn ' + (m.autofanspeed ? 'btn-success' : 'btn-secondary');
  document.getElementById('dtFanModeLabel').textContent = m.autofanspeed ? 'automatically controlled' : 'manually controlled';
  const fanSpd = m.fan_speed || 0;
  document.getElementById('dtFanSlider').value = fanSpd;
  document.getElementById('dtFanVal').textContent = fanSpd + '%';
  document.getElementById('dtFanRpm').textContent = (m.fan_rpm || 0).toFixed(0);
  document.getElementById('dtFan2Rpm').textContent = m.fan2_rpm ? 'Fan2: ' + m.fan2_rpm.toFixed(0) + ' rpm' : '';
  document.getElementById('dtTempTarget').value = m.temptarget || 60;
  document.getElementById('dtMinFan').value = m.min_fan_speed || 25;
}

function renderDtOc(m) {
  const ocOn = m.overclock_enabled;
  document.getElementById('dtOcBtn').textContent = ocOn ? 'Enabled' : 'Disabled';
  document.getElementById('dtOcBtn').className = 'btn ' + (ocOn ? 'btn-success' : '');
  document.getElementById('dtFreq').value = m.frequency;
  document.getElementById('dtCoreV').value = m.core_voltage_set || m.core_voltage;
  document.getElementById('dtCoreVActual').textContent = m.core_voltage + ' mV';
  // Throttle button
  const btn = document.getElementById('dtThrottleBtn');
  btn.textContent = m.throttle_disabled ? 'Disabled' : 'Enabled';
  btn.className = 'btn ' + (m.throttle_disabled ? 'btn-danger' : 'btn-success');
}

function renderDtPool(m) {
  document.getElementById('dtPoolUrl').value = m.stratum_url || '';
  document.getElementById('dtPoolPort').value = m.stratum_port || '';
  document.getElementById('dtPoolUser').value = m.stratum_user || '';
  document.getElementById('dtFbUrl').value = m.fallback_stratum_url || '';
  document.getElementById('dtFbPort').value = m.fallback_stratum_port || '';
  document.getElementById('dtFbUser').value = m.fallback_stratum_user || '';
  document.getElementById('dtPoolDiff').textContent = m.pool_diff ? m.pool_diff.toExponential(2) : '-';
  document.getElementById('dtBlockHeight').textContent = m.block_height || '-';
  document.getElementById('dtNetDiff').textContent = m.network_diff ? m.network_diff.toExponential(2) : '-';
  document.getElementById('dtShares').textContent = (m.shares_accepted || 0).toLocaleString();
  document.getElementById('dtRejects').textContent = m.shares_rejected ? ' (' + m.shares_rejected + ' rejects)' : '';
}

function renderDtSystem(m) {
  const sysMetrics = [
    {label:'MAC', value: m.mac || '-'},
    {label:'IP', value: m.ip || '-'},
    {label:'Version', value: m.axeos_version || m.version || '?'},
    {label:'Board', value: m.board_version || '-'},
    {label:'WiFi', value: m.wifi_status || '?' + ' (' + m.wifi_rssi + ' dBm)'},
    {label:'SSID', value: m.ssid || '-'},
  ];
  document.getElementById('dtSysMetrics').innerHTML = sysMetrics.map(mm => '<div class="metric"><div class="ml">' + mm.label + '</div><div class="mv">' + mm.value + '</div></div>').join('');
  const netMetrics = [
    {label:'MAC', value: m.mac || '-'},
    {label:'WiFi', value: m.wifi_status || '?' + ' (' + m.wifi_rssi + ' dBm)'},
    {label:'SSID', value: m.ssid || '-'},
  ];
  document.getElementById('dtNetMetrics').innerHTML = netMetrics.map(mm => '<div class="metric"><div class="ml">' + mm.label + '</div><div class="mv">' + mm.value + '</div></div>').join('');
  document.getElementById('dtHostname').value = m.hostname || '';
  document.getElementById('dtInvertBtn').textContent = m.invertscreen ? 'On' : 'Off';
  document.getElementById('dtInvertBtn').className = 'btn btn-sm ' + (m.invertscreen ? 'btn-success' : 'btn-secondary');
  document.getElementById('dtFlipBtn').textContent = m.flipscreen ? 'On' : 'Off';
  document.getElementById('dtFlipBtn').className = 'btn btn-sm ' + (m.flipscreen ? 'btn-success' : 'btn-secondary');
  document.getElementById('dtRotation').value = m.rotation || 0;
  document.getElementById('dtDispTimeout').value = m.displayTimeout != null ? m.displayTimeout : -1;
}

function renderDtThrottle(m) {
  const btn = document.getElementById('dtThrottleBtn');
  if (!btn) return;
  btn.textContent = m.throttle_disabled ? 'Disabled' : 'Enabled';
  btn.className = 'btn ' + (m.throttle_disabled ? 'btn-danger' : 'btn-success');
  const label = document.querySelector('#dsOverclock .form-group:nth-child(2) .form-row span.hint');
}

// ========== REFRESH ==========
function update() {
  document.getElementById('fetchStatus').textContent = 'loading...';
  fetch('api/data').then(r => r.json()).then(data => {
    currentData = data;
    _liveTemps = {};
    (data.miners||[]).forEach(m => { if (m.temp != null) _liveTemps[m.ip] = m.temp; });
    document.getElementById('poolBar').innerHTML = renderPool(data);
    document.getElementById('heroGrid').innerHTML = renderHeroes(data);
    document.getElementById('controlsGrid').innerHTML = renderControlCards(data);
    updateHistoryDropdown();
    if (kioskInterval) renderKiosk();

    // Refresh detail view if open
    if (currentDetailIp) {
      const m = (data.miners||[]).find(x => x.ip === currentDetailIp);
      if (m) {
        currentMiner = m;
        const off = m.error;
        document.getElementById('dtStatus').style.background = off ? 'var(--red)' : 'var(--green)';
        document.getElementById('dtName').textContent = m.hostname;
        document.getElementById('dtModel').textContent = m.asic_model;
        document.getElementById('dtModel').className = 'badge ' + (off ? 'badge-red' : 'badge-blue');
        document.getElementById('dtVersion').textContent = 'v' + (m.axeos_version || m.version || '?').replace(/^v/,'');
        renderDtOverview(m);
        renderDtFan(m);
        renderDtOc(m);
        renderDtPool(m);
        renderDtSystem(m);
        renderDtThrottle(m);
        // Update benchmark button state
        const bb = document.getElementById('dtBenchBtn');
        if (bb) {
          bb.disabled = !!m.benchmarking || !!m.error;
          bb.textContent = m.benchmarking ? 'Benching...' : '\u2699 Benchmark';
        }
        if (m.benchmarking) {
          document.getElementById('dtBenchStatusGroup').style.display = 'block';
          pollBenchmarkStatus(m.ip);
        }
      }
    }

    // Block-found detection
    const toast = document.getElementById('block-toast');
    let blockMiner = null;
    (data.miners||[]).forEach(m => {
      if (m.best_session_diff >= (m.network_diff || 1)) blockMiner = m.hostname || m.ip;
    });
    if (blockMiner && !_blockDismissed[blockMiner]) {
      document.getElementById('block-miner').textContent = blockMiner;
      toast.style.display = 'block';
    } else if (!blockMiner) {
      toast.style.display = 'none';
      _blockDismissed = {};
    }

    document.getElementById('fetchStatus').textContent = 'updated ' + ago(data.fetched_at);
    document.getElementById('serverTime').textContent = new Date().toLocaleTimeString();
    loadPlan();
    countdown = {{ REFRESH_S }};
  }).catch(e => {
    document.getElementById('fetchStatus').textContent = 'Error: ' + e.message;
  });
}

function tick() {
  if (countdown <= 0) { update(); countdown = {{ REFRESH_S }}; }
  document.getElementById('refreshCount').textContent = countdown;
  countdown--;
}

function loadPlan() {
  fetch('api/plan').then(r => r.json()).then(data => {
    const plan = data.plan || [];
    document.getElementById('planUpdated').textContent = plan.length ? 'Next ' + (plan[0]?.time||'') + ' — ' + plan[0]?.forecast_ambient + '°C' : 'no plan';
    // Table header
    const ips = plan.length ? Object.keys(plan[0].asics) : [];
    let h = '<table style="width:100%;border-collapse:collapse"><thead><tr style="color:var(--text3);font-weight:600">' +
      '<th style="text-align:left;padding:3px 6px;border-bottom:1px solid var(--surface3)">Hour</th>' +
      '<th style="text-align:left;padding:3px 6px;border-bottom:1px solid var(--surface3)">Amb</th>';
    ips.forEach(ip => {
      h += '<th style="text-align:left;padding:3px 6px;border-bottom:1px solid var(--surface3)" colspan="3">' + ip + '</th>';
    });
    h += '</tr><tr style="color:var(--text3)"><th></th><th></th>';
    ips.forEach(() => { h += '<th style="padding:2px 6px;font-weight:400">MHz</th><th style="padding:2px 6px;font-weight:400">Now°C</th><th style="padding:2px 6px;font-weight:400">Pred°C</th>'; });
    h += '</tr></thead><tbody>';
    plan.forEach(entry => {
      const isCurrent = entry.ts > 0 && Math.abs(entry.ts - Date.now()/1000) < 3600;
      h += '<tr style="' + (isCurrent ? 'background:var(--accent);color:#000' : '') + '">' +
        '<td style="padding:3px 6px">' + (entry.time || '?').slice(11,16) + '</td>' +
        '<td style="padding:3px 6px">' + entry.forecast_ambient + '°</td>';
      ips.forEach(ip => {
        const a = entry.asics[ip] || {};
        const cur = _liveTemps[ip];
        const pred = a.predicted_temp;
        let dot = '';
        if (cur != null && pred != null) {
          const hotBy = cur - pred;
          if (hotBy <= 2) dot = '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#2ecc71;margin-right:3px;vertical-align:middle"></span>';
          else if (hotBy <= 5) dot = '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#f39c12;margin-right:3px;vertical-align:middle"></span>';
          else dot = '<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#e74c3c;margin-right:3px;vertical-align:middle"></span>';
        }
        h += '<td style="padding:3px 6px">' + (a.frequency || '-') + '</td>' +
          '<td style="padding:3px 6px">' + (cur != null ? cur + '°' : '-') + '</td>' +
          '<td style="padding:3px 6px;font-weight:700">' + dot + (pred || '-') + '°</td>';
      });
      h += '</tr>';
    });
    h += '</tbody></table>';
    document.getElementById('planTableWrap').innerHTML = h;

    // Thermal model
    const model = data.model_summary || {};
    let mh = '';
    Object.keys(model).sort().forEach(ip => {
      mh += '<div style="margin:4px 0"><b>' + ip + '</b>: ';
      Object.keys(model[ip]).sort().forEach(key => {
        const e = model[ip][key];
        mh += key + '→Δ' + e.delta + '°(' + e.count + 'x) ';
      });
      mh += '</div>';
    });
    document.getElementById('planModelWrap').innerHTML = mh || 'No data yet (need 24h+ history)';
  }).catch(() => {});
}
document.addEventListener('DOMContentLoaded', () => { update(); setInterval(tick, 1000); });
</script>
<canvas id="confetti-canvas"></canvas>
<div id="block-toast" onclick="dismissBlock()">&#127881; BLOCK FOUND &#127881;
  <span style="position:absolute;top:8px;right:14px;font-size:1.2rem;color:var(--text3);cursor:pointer">&times;</span>
  <small>By: <span id="block-miner"></span></small></div>
</body>
</html>
"""

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Bitaxe Dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5050, help="Port to listen on")
    parser.add_argument("--refresh", type=int, default=30, help="Refresh interval in seconds")
    args = parser.parse_args()

    HTML = HTML.replace("{{ REFRESH_S }}", str(args.refresh))
    print(f"[*] Bitaxe Dashboard starting on http://{args.host}:{args.port}")
    print(f"[*] Refresh interval: {args.refresh}s")
    print(f"[*] Scan subnet: {SCAN_SUBNET}")
    print(f"[*] Scan interval: {SCAN_INTERVAL}s")
    print(f"[*] Throttle profiles dir: {THROTTLE_PROFILES_DIR} {'EXISTS' if os.path.isdir(THROTTLE_PROFILES_DIR) else 'NOT FOUND'}")
    print(f"[*] Weather lat/lon: {WEATHER_LAT}/{WEATHER_LON}")
    _load_dismissed()
    print(f"[*] Dismissed bench set: {len(_needs_bench_dismissed)} ASICs")
    _load_paired_devices()
    print(f"[*] Paired devices: {len(_PAIRED_DEVICES)}")
    _load_fcm_tokens()
    print(f"[*] FCM tokens: {len(_FCM_TOKENS)}")
    _init_firebase()
    ts_ip = _tailscale_ip()
    print(f"[*] Tailscale IP: {ts_ip or 'not detected'}")
    _fetch_ambient_forecast()
    t = threading.Thread(target=_log_history, daemon=True)
    t.start()
    print(f"[*] History logging every {HISTORY_INTERVAL}s")
    _rebuild_thermal_model()
    _compute_plan()
    t = threading.Thread(target=_plan_executor, daemon=True)
    t.start()
    print(f"[*] Predictive planner: {len(_PLAN)} hours, {len(THERMAL_MODEL)} ASICs in model")
    print("[*] Performing initial miner discovery (may take ~30s)...")
    t = threading.Thread(target=discover_miners, daemon=True)
    t.start()
    t.join(timeout=SCAN_TIMEOUT + 5)
    with _discovery_lock:
        if _discovery_done:
            print(f"[*] Initial discovery complete: {len(_discovered)} miners found")
        else:
            print("[*] Initial discovery timed out, starting anyway")
    app.run(host=args.host, port=args.port, debug=False)
