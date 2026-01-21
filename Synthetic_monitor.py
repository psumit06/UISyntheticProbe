import time
import csv
import math
import statistics
import os
import uuid
import argparse
from datetime import datetime
from collections import defaultdict

import pandas as pd
from playwright.sync_api import sync_playwright

from journeys import JOURNEYS   # 🔥 IMPORT HERE


# ================= CLI =================

def parse_args():
    p = argparse.ArgumentParser("Synthetic Monitoring Framework")
    p.add_argument("--mode", choices=["url", "journey"], required=True)
    p.add_argument("--env", default="staging")
    p.add_argument("--urls", default="urls.txt")
    p.add_argument("--duration", type=int, default=30)
    p.add_argument("--delay", type=int, default=5)
    p.add_argument("--bucket", type=int, default=5)
    return p.parse_args()


# ================= UTILS =================

def load_urls(path):
    if path.endswith(".csv"):
        return pd.read_csv(path)["url"].dropna().tolist()
    with open(path) as f:
        return [l.strip() for l in f if l.strip()]


def percentile(data, pct):
    if not data:
        return -1
    data = sorted(data)
    k = (len(data) - 1) * (pct / 100)
    f, c = math.floor(k), math.ceil(k)
    return data[int(k)] if f == c else data[f] + (data[c] - data[f]) * (k - f)


# ================= OBSERVER =================

class Observer:
    def __init__(self):
        self.start = None

    def begin(self):
        self.start = time.time()

    def end(self, page):
        duration = int((time.time() - self.start) * 1000)

        fcp = page.evaluate(
            "performance.getEntriesByName('first-contentful-paint')[0]?.startTime || -1"
        )

        lcp = page.evaluate("""
        () => new Promise(resolve => {
            new PerformanceObserver(list => {
                const e = list.getEntries()
                resolve(e[e.length - 1]?.startTime || -1)
            }).observe({ type: 'largest-contentful-paint', buffered: true })
        })
        """)

        cls = page.evaluate("""
        () => {
            let v = 0
            new PerformanceObserver(list => {
                for (const e of list.getEntries()) {
                    if (!e.hadRecentInput) v += e.value
                }
            }).observe({ type: 'layout-shift', buffered: true })
            return v
        }
        """)

        return duration, int(fcp), int(lcp), round(cls, 3)


# ================= MAIN =================

def main():
    args = parse_args()

    RUN_ID = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    BASE = f"runs/{args.env}/{RUN_ID}"
    os.makedirs(f"{BASE}/screenshots", exist_ok=True)

    RAW = f"{BASE}/results.csv"
    PROM = f"{BASE}/prometheus_metrics.txt"

    timings = defaultdict(list)
    end_time = time.time() + args.duration * 60

    with open(RAW, "w", newline="") as raw:
        writer = csv.writer(raw)
        writer.writerow([
            "timestamp", "env", "run_id",
            "entity", "entity_type",
            "status", "duration_ms", "fcp", "lcp", "cls"
        ])

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()

            while time.time() < end_time:

                # ================= URL MODE =================
                if args.mode == "url":
                    urls = load_urls(args.urls)

                    for url in urls:
                        page = ctx.new_page()
                        obs = Observer()

                        try:
                            obs.begin()
                            page.goto(url, timeout=60000)
                            d, f, l, c = obs.end(page)
                            status = "SUCCESS"
                        except Exception:
                            d, f, l, c = -1, -1, -1, 0.0
                            status = "FAILURE"

                        writer.writerow([
                            datetime.utcnow().isoformat(),
                            args.env, RUN_ID,
                            url, "URL",
                            status, d, f, l, c
                        ])

                        if d > 0:
                            timings[url].append(d)

                        page.close()
                        time.sleep(args.delay)

                # ================= JOURNEY MODE =================
                else:
                    for name, fn in JOURNEYS.items():
                        page = ctx.new_page()
                        obs = Observer()

                        try:
                            d, f, l, c = fn(page, obs)
                            status = "SUCCESS"
                        except Exception:
                            d, f, l, c = -1, -1, -1, 0.0
                            status = "FAILURE"

                        writer.writerow([
                            datetime.utcnow().isoformat(),
                            args.env, RUN_ID,
                            name, "JOURNEY",
                            status, d, f, l, c
                        ])

                        if d > 0:
                            timings[name].append(d)

                        page.close()
                        time.sleep(args.delay)

            browser.close()

    # ================= PROMETHEUS =================
    with open(PROM, "w") as f:
        for entity, values in timings.items():
            f.write(
                f'synthetic_p90_ms{{env="{args.env}",entity="{entity}",mode="{args.mode}"}} '
                f'{int(percentile(values, 90))}\n'
            )


if __name__ == "__main__":
    main()
