"""Controlled synthetic I/O benchmark; no provider calls or application DB."""

import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import redis

from pipeline_network import ordered_search
from query_cache import RedisCache, cached_compute


def stats(samples):
    samples = sorted(samples)
    return {"p50_ms": round(statistics.median(samples), 3), "p95_ms": round(samples[-1], 3), "samples": len(samples)}


def run_benchmark(connection):
    # Caller supplies an explicitly isolated Redis connection, never app settings.
    calls = []
    lock = threading.Lock()

    def service():
        with lock:
            calls.append(1)
        time.sleep(0.03)
        return {"fixture": 1}

    def store():
        return RedisCache(redis.Redis(connection_pool=connection.connection_pool))

    def query(key, enabled):
        started = time.perf_counter()
        value = cached_compute("io-benchmark", key, service, enabled=enabled, validate=lambda v: v == {"fixture": 1})
        assert value == {"fixture": 1}
        return (time.perf_counter() - started) * 1000

    results = {}
    with (
        patch("query_cache.backend", store),
        patch.dict(
            "os.environ",
            {
                "RAG_CACHE_HMAC_KEY": "synthetic-benchmark-key-over-32-bytes-long",
                "RAG_CACHE_WAIT_MS": "1000",
            },
        ),
    ):
        for mode in ("uncached", "cold", "warm", "coalesced"):
            calls.clear()
            if mode == "warm":
                query(["warm"], True)
                calls.clear()
            if mode == "coalesced":
                with ThreadPoolExecutor(max_workers=8) as executor:
                    durations = list(executor.map(lambda _: query(["concurrent"], True), range(8)))
            else:
                durations = [query([mode, i] if mode != "warm" else ["warm"], mode != "uncached") for i in range(8)]
            results[mode] = {**stats(durations), "simulated_upstream_calls": len(calls)}
        for concurrency in (1, 2):
            samples = []
            with patch.dict("os.environ", {"RAG_RETRIEVAL_CONCURRENCY": str(concurrency)}):
                for _ in range(3):
                    started = time.perf_counter()
                    ordered_search(list(range(4)), lambda _: time.sleep(0.03), safe=True)
                    samples.append((time.perf_counter() - started) * 1000)
            results[f"search_concurrency_{concurrency}"] = stats(samples)
    return {
        "scope": "Controlled 30ms fake service with isolated real Redis; no SQL version checks or model generation",
        "limitations": "Timings demonstrate implementation overhead/reuse only; not production latency or cost estimates.",
        "results": results,
        "provider_calls": 0,
    }


def main():
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-unix-socket", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    connection = redis.Redis(unix_socket_path=args.redis_unix_socket, decode_responses=True)
    try:
        report = run_benchmark(connection)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
