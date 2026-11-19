"""Very small live Render liveness probe; intentionally not a stress test.

Sends 8 GET requests to the public liveness route in four batches of two.
It never calls the database, exchange adapters, or paper-trading endpoints.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import time
from urllib.request import Request, urlopen

URL = "https://ai-crypto-trader-claude.onrender.com/api/health"
TOTAL_REQUESTS = 8
MAX_CONCURRENCY = 2
BATCH_PAUSE_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 45.0


def probe(request_number: int) -> tuple[int, float]:
    request = Request(
        URL,
        headers={"User-Agent": "ai-crypto-trader-bounded-health-probe/1.0"},
        method="GET",
    )
    started = time.perf_counter()
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read(1024)
        elapsed = time.perf_counter() - started
        if response.status != 200:
            raise RuntimeError(
                f"Request {request_number}: expected HTTP 200, got {response.status}"
            )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Request {request_number}: liveness response was not valid JSON"
        ) from exc
    if payload.get("status") != "ok":
        raise RuntimeError(
            f"Request {request_number}: unexpected liveness status"
        )
    return response.status, elapsed


def main() -> None:
    latencies = []
    completed = 0
    for batch_start in range(0, TOTAL_REQUESTS, MAX_CONCURRENCY):
        batch = range(
            batch_start + 1,
            min(batch_start + MAX_CONCURRENCY, TOTAL_REQUESTS) + 1,
        )
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as executor:
            results = list(executor.map(probe, batch))
        for request_number, (status, elapsed) in zip(batch, results):
            completed += 1
            latencies.append(elapsed)
            print(
                f"PASS request={request_number} status={status} "
                f"latency_seconds={elapsed:.3f}"
            )
        if batch_start + MAX_CONCURRENCY < TOTAL_REQUESTS:
            time.sleep(BATCH_PAUSE_SECONDS)

    ordered = sorted(latencies)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    average = sum(ordered) / len(ordered)
    print(
        "SUMMARY "
        f"completed={completed}/{TOTAL_REQUESTS} "
        f"max_concurrency={MAX_CONCURRENCY} "
        f"average_seconds={average:.3f} "
        f"p95_seconds={ordered[p95_index]:.3f} "
        f"max_seconds={max(ordered):.3f}"
    )


if __name__ == "__main__":
    main()
