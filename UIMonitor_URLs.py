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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ---------- CLI ARGUMENTS ----------

def parse_args():
    parser = argparse.ArgumentParser(description="Synthetic Scenario Performance Monitor")
    parser.add_argument("--env", default="staging", help="Environment name")
    parser.add_argument("--urls", default="urls.txt", help="URLs file (txt or csv)")
    parser.add_argument("--duration", type=int, default=30, help="Test duration in minutes")
    parser.add_argument("--delay", type=int, default=5, help="Delay between scenarios (seconds)")
    parser.add_argument("--bucket", type=int, default=5, help="Bucket size (minutes)")
    return parser.parse_args()


# ---------- UTILITIES ----------

def load_urls(file_path):
    if file_path.endswith(".csv"):
        return pd.read_csv(file_path)["url"].dropna().tolist()
    with open(file_path, "r") as f:
        return [line.strip() for line in f if line.strip()]


def percentile(data, pct):
    if not data:
        return -1
    data = sorted(data)
    k = (len(data) - 1) * (pct / 100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return data[int(k)]
    return data[f] + (data[c] - data[f]) * (k - f)


def get_time_bucket(ts, bucket_minutes):
    return ts.replace(
        minute=(ts.minute // bucket_minutes) * bucket_minutes,
        second=0,
        microsecond=0
    )


def safe_filename(name):
    return name.replace("https://", "").replace("http://", "").replace("/", "_")


# ---------- SCENARIO OBSERVER ----------

class ScenarioObserver:
    def __init__(self):
        self.start_time = None
        self.scenario_name = None

    def start(self, name):
        self.scenario_name = name
        self.start_time = time.time()

    def end(self, page):
        if not self.start_time:
            return None

        duration_ms = int((time.time() - self.start_time) * 1000)

        fcp = page.evaluate("""
        () => performance.getEntriesByName('first-contentful-paint')[0]?.startTime || -1
        """)

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

        self.start_time = None
        return duration_ms, fcp, lcp, cls


# ---------- MAIN ----------

def main():
    args = parse_args()

    RUN_ID = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    BASE_DIR = os.path.join("runs", args.env, RUN_ID)
    SCREENSHOT_DIR = os.path.join(BASE_DIR, "screenshots")

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    RAW_RESULTS = os.path.join(BASE_DIR, "results.csv")
    ERROR_LOG = os.path.join(BASE_DIR, "errors.csv")
    SUMMARY = os.path.join(BASE_DIR, "summary_report.csv")
    BUCKETED = os.path.join(BASE_DIR, "bucketed_performance_report.csv")
    PROM = os.path.join(BASE_DIR, "prometheus_metrics.txt")

    urls = load_urls(args.urls)
    end_time = time.time() + args.duration * 60

    timings = defaultdict(list)
    bucketed_timings = defaultdict(lambda: defaultdict(list))
    bucketed_lcp = defaultdict(lambda: defaultdict(list))

    observer = ScenarioObserver()

    with open(RAW_RESULTS, "w", newline="") as raw, open(ERROR_LOG, "w", newline="") as err:
        raw_w = csv.writer(raw)
        err_w = csv.writer(err)

        raw_w.writerow([
            "timestamp_utc", "env", "run_id",
            "scenario", "status",
            "duration_ms", "fcp_ms", "lcp_ms", "cls",
            "error_type", "error_message", "screenshot_path"
        ])

        err_w.writerow([
            "timestamp_utc", "env", "run_id",
            "scenario", "error_type", "error_message", "screenshot_path"
        ])

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()

            while time.time() < end_time:
                for url in urls:
                    if time.time() >= end_time:
                        break

                    page = context.new_page()
                    now = datetime.utcnow()

                    status = "SUCCESS"
                    error_type = ""
                    error_message = ""
                    screenshot_path = ""

                    duration = fcp = lcp = -1
                    cls = 0.0

                    try:
                        observer.start(url)
                        page.goto(url, wait_until="load", timeout=60000)
                        duration, fcp, lcp, cls = observer.end(page)

                    except PlaywrightTimeoutError as e:
                        status = "FAILURE"
                        error_type = "TIMEOUT"
                        error_message = str(e)

                    except Exception as e:
                        status = "FAILURE"
                        error_type = "NAVIGATION_ERROR"
                        error_message = str(e)

                    if status != "SUCCESS":
                        screenshot_path = os.path.join(
                            SCREENSHOT_DIR,
                            f"{now.strftime('%Y%m%dT%H%M%S')}_{safe_filename(url)}_{error_type}.png"
                        )
                        try:
                            page.screenshot(path=screenshot_path, full_page=True)
                        except Exception:
                            screenshot_path = ""

                        err_w.writerow([
                            now.isoformat(), args.env, RUN_ID,
                            url, error_type, error_message, screenshot_path
                        ])

                    raw_w.writerow([
                        now.isoformat(), args.env, RUN_ID,
                        url, status,
                        duration, int(fcp), int(lcp), round(cls, 3),
                        error_type, error_message, screenshot_path
                    ])

                    if status == "SUCCESS" and duration > 0:
                        timings[url].append(duration)
                        bucket = get_time_bucket(now, args.bucket)
                        bucketed_timings[url][bucket].append(duration)
                        if lcp > 0:
                            bucketed_lcp[url][bucket].append(lcp)

                    page.close()
                    time.sleep(args.delay)

            browser.close()

    # ---------- SUMMARY ----------
    with open(SUMMARY, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "avg_ms", "p90_ms", "max_ms", "min_ms", "samples"])
        for s, t in timings.items():
            w.writerow([
                s,
                int(statistics.mean(t)),
                int(percentile(t, 90)),
                max(t), min(t), len(t)
            ])

    # ---------- BUCKETED ----------
    with open(BUCKETED, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "bucket_start_utc", "env", "run_id", "scenario",
            "p90_load_ms", "avg_load_ms",
            "p90_lcp_ms", "avg_lcp_ms", "samples"
        ])
        for s in bucketed_timings:
            for b in bucketed_timings[s]:
                lt = bucketed_timings[s][b]
                lcp_t = bucketed_lcp[s].get(b, [])
                w.writerow([
                    b.isoformat(), args.env, RUN_ID, s,
                    int(percentile(lt, 90)),
                    int(statistics.mean(lt)),
                    int(percentile(lcp_t, 90)) if lcp_t else -1,
                    int(statistics.mean(lcp_t)) if lcp_t else -1,
                    len(lt)
                ])

    # ---------- PROMETHEUS ----------
    with open(PROM, "w") as f:
        for s, t in timings.items():
            f.write(
                f'scenario_duration_p90_ms{{env="{args.env}",scenario="{s}",run_id="{RUN_ID}"}} '
                f'{int(percentile(t, 90))}\n'
            )


if __name__ == "__main__":
    main()
