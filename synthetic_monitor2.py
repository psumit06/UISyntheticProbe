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

from journeys import JOURNEYS


# ================= CLI =================

def parse_args():
    p = argparse.ArgumentParser("Synthetic Monitoring Framework")
    p.add_argument("--mode", choices=["url", "journey"], required=True)
    p.add_argument("--env", default="staging")
    p.add_argument("--urls", default="urls.txt")
    p.add_argument("--journey-data", help="CSV input for journeys")
    p.add_argument("--duration", type=int, default=30)
    p.add_argument("--delay", type=int, default=5)
    return p.parse_args()


# ================= UTILS =================

def load_urls(path):
    if path.endswith(".csv"):
        return pd.read_csv(path)["url"].dropna().tolist()
    with open(path) as f:
        return [l.strip() for l in f if l.strip()]


def load_journey_data(path):
    return pd.read_csv(path).to_dict(orient="records")


def percentile(data, pct):
    if not data:
        return -1
    data = sorted(data)
    k = (len(data) - 1) * (pct / 100)
    f, c = math.floor(k), math.ceil(k)
    return data[int(k)] if f == c else data[f] + (data[c] - data[f]) * (k - f)


def safe(name):
    return name.replace("/", "_").replace(" ", "_").replace("{", "").replace("}", "")


# ================= OBSERVER =================

class StepObserver:
    def __init__(self):
        self.step_name = None
        self.start_time = None

    def start_step(self, name):
        self.step_name = name
        self.start_time = time.time()

    def end_step(self, page):
        duration = int((time.time() - self.start_time) * 1000)

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

        return {
            "step": self.step_name,
            "duration_ms": duration,
            "fcp": int(fcp),
            "lcp": int(lcp),
            "cls": round(cls, 3),
            "status": "SUCCESS"
        }


# ================= MAIN =================

def main():
    args = parse_args()

    RUN_ID = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    BASE = f"runs/{args.env}/{RUN_ID}"
    SCREENSHOT_DIR = f"{BASE}/screenshots"
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    RESULTS = f"{BASE}/results.csv"
    STEP_RESULTS = f"{BASE}/step_results.csv"
    PROM = f"{BASE}/prometheus_metrics.txt"

    journey_timings = defaultdict(list)
    step_timings = defaultdict(list)

    end_time = time.time() + args.duration * 60

    with open(RESULTS, "w", newline="") as jr, open(STEP_RESULTS, "w", newline="") as sr:
        journey_writer = csv.writer(jr)
        step_writer = csv.writer(sr)

        journey_writer.writerow([
            "timestamp", "env", "run_id",
            "journey", "status", "total_duration_ms", "input_data"
        ])

        step_writer.writerow([
            "timestamp", "env", "run_id",
            "journey", "step",
            "duration_ms", "fcp", "lcp", "cls",
            "status", "screenshot"
        ])

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            ctx.set_default_timeout(15000)

            if args.mode == "journey":
                data_rows = load_journey_data(args.journey_data)

                while time.time() < end_time:
                    for journey_name, fn in JOURNEYS.items():
                        for row in data_rows:

                            page = ctx.new_page()
                            observer = StepObserver()

                            journey_start = time.time()
                            status = "SUCCESS"

                            try:
                                steps = fn(page, observer, row)

                                for s in steps:
                                    step_timings[(journey_name, s["step"])].append(
                                        s["duration_ms"]
                                    )

                                    step_writer.writerow([
                                        datetime.utcnow().isoformat(),
                                        args.env,
                                        RUN_ID,
                                        journey_name,
                                        s["step"],
                                        s["duration_ms"],
                                        s["fcp"],
                                        s["lcp"],
                                        s["cls"],
                                        s["status"],
                                        ""
                                    ])

                            except Exception as e:
                                status = "FAILURE"

                                failed_step = observer.step_name or "unknown"

                                shot = (
                                    f"{SCREENSHOT_DIR}/"
                                    f"{journey_name}_{failed_step}_{safe(str(row))}.png"
                                )

                                page.screenshot(path=shot, full_page=True)

                                step_writer.writerow([
                                    datetime.utcnow().isoformat(),
                                    args.env,
                                    RUN_ID,
                                    journey_name,
                                    failed_step,
                                    -1,
                                    -1,
                                    -1,
                                    -1,
                                    "FAILURE",
                                    shot
                                ])

                            total_duration = int(
                                (time.time() - journey_start) * 1000
                            )

                            journey_timings[journey_name].append(total_duration)

                            journey_writer.writerow([
                                datetime.utcnow().isoformat(),
                                args.env,
                                RUN_ID,
                                journey_name,
                                status,
                                total_duration,
                                str(row)
                            ])

                            page.close()
                            time.sleep(args.delay)

            browser.close()

    # ================= PROMETHEUS =================

    with open(PROM, "w") as f:
        for j, t in journey_timings.items():
            f.write(
                f'synthetic_journey_p90_ms{{env="{args.env}",journey="{j}"}} '
                f'{int(percentile(t, 90))}\n'
            )

        for (j, s), t in step_timings.items():
            f.write(
                f'synthetic_step_p90_ms{{env="{args.env}",journey="{j}",step="{s}"}} '
                f'{int(percentile(t, 90))}\n'
            )


if __name__ == "__main__":
    main()
