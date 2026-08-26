#!/usr/bin/env python3
"""Sanitize Codex account limits into a small QML-oriented record field."""

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone


CACHE_VERSION = 1
CUSTOM_LABEL = re.compile(r"^Account \d+$")
CACHE_ACCOUNT_KEYS = {"index", "label", "planType", "limits", "additionalLimitNames", "lastSuccessAt", "stale", "error", "warning"}
CACHE_LIMIT_KEYS = {"windowMinutes", "usedPercent", "resetAtMs"}
MAX_ADDITIONAL_LIMITS = 8
MAX_RESET_AT_MS = 32503680000000
EMAIL = re.compile(r"\b[^\s@/]+@[^\s@/]+\.[^\s@/]+\b")
PATH = re.compile(r"(?:^|[^\w/~])(?:~|/)\S*")
IDENTIFIER = re.compile(r"\b(?:account|workspace|organization|org|storage)[_-]?(?:id|identifier)?\s*[:=]\s*\S+|\b(?:acct|ws|org|store)_[A-Za-z0-9_-]+\b", re.IGNORECASE)
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
TOKEN = re.compile(r"\b(?:bearer|refresh[_ -]?token|access[_ -]?token|jwt)\b|\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", re.IGNORECASE)
HIGH_ENTROPY = re.compile(r"\b(?=[A-Za-z0-9_-]{24,}\b)(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+\b")


def now_iso(value):
    if value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("--now must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def text(value, maximum=160):
    if not isinstance(value, str):
        return ""
    return "".join(character for character in value if character >= " " and character != "\x7f").strip()[:maximum]


def sensitive(value):
    return bool(EMAIL.search(value) or PATH.search(value) or IDENTIFIER.search(value)
                or UUID.search(value) or TOKEN.search(value) or HIGH_ENTROPY.search(value))


def display_text(value, maximum=160):
    value = text(value, None)
    return "" if sensitive(value) else value[:maximum]


def error_text(value, maximum=240):
    value = text(value, None)
    return "limits unavailable" if value and sensitive(value) else value[:maximum]


def meter(value):
    if not isinstance(value, dict) or value.get("windowMinutes") not in (300, 10080):
        return None
    used = value.get("usedPercent")
    if not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used):
        return None
    reset = value.get("resetAtMs")
    if not isinstance(reset, (int, float)) or isinstance(reset, bool) or not math.isfinite(reset) or not 0 < reset <= MAX_RESET_AT_MS:
        reset = None
    else:
        reset = int(reset)
    return {"windowMinutes": value["windowMinutes"], "usedPercent": max(0, min(100, used)), "resetAtMs": reset}


def account(value, position, timestamp, labels):
    value = value if isinstance(value, dict) else {}
    raw_limits = value.get("limits")
    raw_limits = raw_limits if isinstance(raw_limits, list) else []
    limits = [normalized for item in raw_limits if (normalized := meter(item)) is not None]
    limits.sort(key=lambda item: item["windowMinutes"])
    limits = [item for position, item in enumerate(limits) if not position or item["windowMinutes"] != limits[position - 1]["windowMinutes"]]
    raw_label = display_text(value.get("label"), 120)
    label = raw_label or f"Account {position + 1}"
    additional = []
    for item in raw_limits:
        if isinstance(item, dict) and item.get("windowMinutes") not in (300, 10080):
            name = display_text(item.get("name"), 80) or "Additional limit"
            if name and name not in additional:
                additional.append(name)
            if len(additional) == MAX_ADDITIONAL_LIMITS:
                break
    eligible = bool(raw_label) and not CUSTOM_LABEL.fullmatch(label) and labels.count(label) == 1
    warning = ""
    if not eligible:
        warning = "duplicate label is not cache eligible" if labels.count(label) > 1 else "label is not cache eligible"
    error = error_text(value.get("error"), 240)
    return {
        "index": value.get("index") if isinstance(value.get("index"), int) and value["index"] >= 0 else position,
        "label": label,
        "planType": display_text(value.get("planType"), 80) or ("Plan unavailable" if text(value.get("planType"), 80) else ""),
        "limits": [] if error else limits,
        "additionalLimitNames": additional,
        "lastSuccessAt": timestamp if not error else "",
        "stale": False,
        "error": error,
        "warning": warning,
    }


def cache_path(value):
    if value:
        return value
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    return os.path.join(runtime, "codex-accounts.json") if runtime else None


def load_cache(path):
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(value, dict) or set(value) != {"version", "accounts"} or value.get("version") != CACHE_VERSION:
        return []
    accounts = value.get("accounts")
    if not isinstance(accounts, list):
        return []
    candidates = [item for item in accounts if cache_account(item)]
    labels = [item["label"] for item in candidates]
    return [item for item in candidates if labels.count(item["label"]) == 1]


def cache_account(value):
    if not isinstance(value, dict) or set(value) != CACHE_ACCOUNT_KEYS:
        return False
    if not isinstance(value["index"], int) or isinstance(value["index"], bool) or not 0 <= value["index"] <= 100000:
        return False
    if not isinstance(value["label"], str) or not value["label"] or display_text(value["label"], 120) != value["label"] or CUSTOM_LABEL.fullmatch(value["label"]):
        return False
    if not isinstance(value["planType"], str) or display_text(value["planType"], 80) != value["planType"] or not isinstance(value["additionalLimitNames"], list):
        return False
    if len(value["additionalLimitNames"]) > MAX_ADDITIONAL_LIMITS or any(not isinstance(name, str) or not name or display_text(name, 80) != name for name in value["additionalLimitNames"]):
        return False
    if len(set(value["additionalLimitNames"])) != len(value["additionalLimitNames"]):
        return False
    if not isinstance(value["lastSuccessAt"], str) or parse_iso(value["lastSuccessAt"]) is None or now_iso(value["lastSuccessAt"]) != value["lastSuccessAt"]:
        return False
    if value["stale"] is not False or value["error"] != "" or value["warning"] != "":
        return False
    limits = value["limits"]
    if not isinstance(limits, list) or len(limits) > 2:
        return False
    windows = set()
    for limit in limits:
        if not isinstance(limit, dict) or set(limit) != CACHE_LIMIT_KEYS or limit.get("windowMinutes") not in (300, 10080):
            return False
        used = limit.get("usedPercent")
        reset = limit.get("resetAtMs")
        if not isinstance(used, (int, float)) or isinstance(used, bool) or not math.isfinite(used) or not 0 <= used <= 100:
            return False
        if reset is not None and (not isinstance(reset, int) or isinstance(reset, bool) or not 0 < reset <= MAX_RESET_AT_MS):
            return False
        windows.add(limit["windowMinutes"])
    return len(windows) == len(limits) and [limit["windowMinutes"] for limit in limits] == sorted(windows)


def parse_iso(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (AttributeError, ValueError):
        return None


def expired(item, now):
    success = parse_iso(item.get("lastSuccessAt"))
    if success is None or now >= success + timedelta(hours=24):
        return True
    resets = [limit["resetAtMs"] for limit in item.get("limits", []) if isinstance(limit, dict) and limit.get("resetAtMs")]
    return bool(resets) and now.timestamp() * 1000 >= min(resets)


def stale(item, reason, now):
    result = {key: item[key] for key in ("index", "label", "planType", "limits", "additionalLimitNames", "lastSuccessAt", "warning")}
    result["limits"] = [] if expired(result, now) else result["limits"]
    result["stale"] = True
    result["error"] = error_text(reason, 240) or "limits unavailable"
    return result


def atomic_json(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    descriptor, temporary = tempfile.mkstemp(prefix=".codex-accounts-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", required=True)
    parser.add_argument("--cache")
    parser.add_argument("--limits-command", nargs="+", default=["oc-codex-multi-auth", "limits", "--json"])
    parser.add_argument("--now")
    args = parser.parse_args()
    timestamp = now_iso(args.now)
    with open(args.record, encoding="utf-8") as handle:
        record = json.load(handle)
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    result = subprocess.run(args.limits_command, text=True, capture_output=True, check=False)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {}
    source = payload.get("accounts") if isinstance(payload, dict) and not payload.get("error") else None
    now = parse_iso(timestamp)
    path = cache_path(args.cache)
    old_cache = load_cache(path)
    if isinstance(source, list):
        labels = [display_text(item.get("label"), 120) if isinstance(item, dict) else "" for item in source]
        live = [account(item, position, timestamp, labels) for position, item in enumerate(source)]
        cached_by_label = {item.get("label"): item for item in old_cache if isinstance(item, dict) and isinstance(item.get("label"), str)}
        output = []
        next_cache = []
        for item in live:
            cached = cached_by_label.get(item["label"])
            if item["error"] and cached and not item["warning"]:
                output.append(stale(cached, item["error"], now))
                next_cache.append(cached)
                continue
            output.append(item)
            if not item["error"] and not item["warning"]:
                next_cache.append(item)
        if path:
            atomic_json(path, {"version": CACHE_VERSION, "accounts": next_cache})
        record["accounts"] = output
    else:
        reason = error_text(payload.get("error"), 240) if isinstance(payload, dict) else ""
        if not reason:
            reason = "limits command returned no valid account data"
        record["accounts"] = [stale(item, reason, now) for item in old_cache if isinstance(item, dict)]
    atomic_json(args.record, record)


if __name__ == "__main__":
    main()
