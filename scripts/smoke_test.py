#!/usr/bin/env python3
"""Smoke test against a running backend.

Hits a handful of read-only endpoints that must always respond, including
the ones this platform must never lose: Real Mode health, Prediction Lab
integrity verification, Steam Predictor report, and the Provider Capability
Report. Does not require a BetsAPI key (capability-report works keyless).
"""
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"

ENDPOINTS = [
    ("/api/health", "Real Mode health"),
    ("/api/lab/verify-integrity", "Prediction Lab integrity check"),
    ("/api/steam/report", "Steam Predictor report"),
    ("/api/provider/capability-report", "Provider Capability Report"),
]


def check(path: str, label: str) -> bool:
    url = BASE + path
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            code = resp.getcode()
            passed = 200 <= code < 300
            print(f"[{'PASS' if passed else 'FAIL':4}] {label} ({path}) -> {code}")
            return passed
    except urllib.error.HTTPError as e:
        print(f"[FAIL] {label} ({path}) -> HTTP {e.code}")
        return False
    except Exception as e:
        print(f"[FAIL] {label} ({path}) -> {e}")
        return False


def main() -> int:
    print(f"=== Smoke test against {BASE} ===")
    results = [check(path, label) for path, label in ENDPOINTS]
    print()
    passed = sum(results)
    total = len(results)
    print(f"Smoke test: {passed}/{total} endpoints passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
