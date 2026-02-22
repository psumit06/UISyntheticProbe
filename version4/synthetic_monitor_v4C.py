import time
import csv
import math
import statistics
import os
import uuid
import argparse
from datetime import datetime, timezone
from collections import defaultdict
from prometheus_client import (
    CollectorRegistry,
    Gauge,
    Counter,
    Histogram,
    push_to_gateway,
    delete_from_gateway,
)
#from numpy import percentile
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from journeys import JOURNEYS
HTTP_CREDS = ("synthetic_user", "synthetic_pass")
HeadlessMode = True

PUSHGATEWAY_URL = os.getenv("PUSHGATEWAY_URL", "http://localhost:9091")
PROM_JOB_URL = os.getenv("PROM_JOB_URL", "synthetic_url_monitor")
PROM_JOB_JOURNEY = os.getenv("PROM_JOB_JOURNEY", "synthetic_journey_monitor")

# ================= CLI =================

def parse_args():
    p = argparse.ArgumentParser("Synthetic Monitoring Framework")
    p.add_argument("--mode", choices=["url", "journey"], required=True)
    p.add_argument("--env", default="stage")
    p.add_argument("--urls", default="urls.txt")
    p.add_argument("--journey-data", help="CSV input for journeys")
    p.add_argument("--duration", type=int, default=30)
    p.add_argument("--delay", type=int, default=5)
    p.add_argument("--bucket", type=int, default=5, help="Bucket size in minutes for URL mode metrics")
    p.add_argument("--time-unit", choices=["ms", "s"], default="s", help="Output timings in milliseconds or seconds")
    return p.parse_args()

#  Production UX latency buckets (ms)
LATENCY_BUCKETS_MS = (
    100, 250, 500, 750,
    1000, 1500, 2000,
    3000, 4000, 5000,
    7000, 10000, 15000, 30000
)
# ================= UTILS =================

def load_urls(path):
    if path.endswith(".csv"):
        return pd.read_csv(path)["url"].dropna().tolist()
    with open(path) as f:
        return [l.strip() for l in f if l.strip()]


def load_journey_data(path):
    df = pd.read_csv(path, dtype=str).fillna("")
    return df.to_dict(orient="records")


def percentile(data, pct):
    if not data:
        return -1
    data = sorted(data)
    k = (len(data) - 1) * (pct / 100)
    f, c = math.floor(k), math.ceil(k)
    return data[int(k)] if f == c else data[f] + (data[c] - data[f]) * (k - f)


def safe(name):
    invalid = "<>:\"/\\|?*"
    table = str.maketrans({ch: "_" for ch in invalid})
    return name.translate(table).replace(" ", "_").replace("{", "").replace("}", "")


class TimeFormatter:
    """Utility to format time durations in ms or seconds based on user preference."""

    def __init__(self, unit):
        self.unit = unit
        self.label = "ms" if unit == "ms" else "s"

    def convert(self, value):
        if value is None or value < 0:
            return -1
        if self.unit == "ms":
            return int(round(value))
        return round(value / 1000.0, 3)


def capture_screenshot(page, screenshot_dir, journey, step, label=None):
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
    parts = [safe(journey), safe(step)]
    if label:
        parts.append(safe(label))
    parts.append(timestamp)
    filename = "_".join(parts) + ".png"
    path = os.path.join(screenshot_dir, filename)
    page.screenshot(path=path, full_page=True)
    return path


def clean_error_message(message):
    if not message:
        return ""

    text = str(message).strip()
    marker = "===================================="
    if marker in text:
        text = text.split(marker, 1)[0].strip()

    if "\n" in text:
        text = text.splitlines()[0]

    return text

# ============== URL HELPERS ================

def get_time_bucket(ts, bucket_minutes):
    return ts.replace(
        minute=(ts.minute // bucket_minutes) * bucket_minutes,
        second=0,
        microsecond=0
    )


def safe_url_name(url):
    trimmed = url.replace("https://", "").replace("http://", "")
    return safe(trimmed)


# ================== OBSERVERS =================
class ScenarioObserver:
    def __init__(self):
        self.scenario_name = None
        self.start_time = None
        self.measurement_active = False

    def start(self, name):
        self.scenario_name = name
        self.start_time = time.time()
        self.measurement_active = True

    def clear_performance_buffer(self, page):
        try:
            page.evaluate("""
                performance.clearResourceTimings();
                performance.clearMarks();
                performance.clearMeasures();
                window.__synthetic_nav_start = performance.now();
            """)
        except Exception:
            pass

    def end(self, page):
        if self.start_time is None:
            return -1, -1, -1, -1

        duration_ms = int((time.time() - self.start_time) * 1000)

        def safe_eval(expr):
            try:
                return page.evaluate(expr)
            except Exception:
                return -1

        fcp = safe_eval("performance.getEntriesByName('first-contentful-paint')[0]?.startTime || -1")

        lcp = safe_eval("""
        () => new Promise(resolve => {
            let value = -1;
            const observer = new PerformanceObserver(list => {
                const e = list.getEntries();
                value = e[e.length - 1]?.startTime || -1;
            });
            observer.observe({ type: 'largest-contentful-paint', buffered: true });
            setTimeout(() => resolve(value), 1500);
        })""")

        cls = safe_eval("""
        () => new Promise(resolve => {
            let clsValue = 0;
            const observer = new PerformanceObserver(list => {
                for (const entry of list.getEntries()) {
                    if (!entry.hadRecentInput) {
                        clsValue += entry.value;
                    }
                }
            });
            observer.observe({ type: 'layout-shift', buffered: true });
            setTimeout(() => resolve(clsValue), 1500);
        })""")

        cls_value = cls if isinstance(cls, (int, float)) and cls >= 0 else -1
        self.start_time = None
        self.measurement_active = False
        return (
            duration_ms,
            int(fcp) if isinstance(fcp, (int, float)) else -1,
            int(lcp) if isinstance(lcp, (int, float)) else -1,
            round(cls_value, 3) if cls_value >= 0 else -1
        )

class StepObserver:
    def __init__(self):
        self.step_name = None
        self.start_time = None
        self._pause_stack = 0
        self._pause_start = None
        self._paused_total = 0
        self.measurement_active = False

    def clear_performance_buffer(self, page):
        try:
            page.evaluate("""
                performance.clearResourceTimings();
                performance.clearMarks();
                performance.clearMeasures();
                window.__synthetic_nav_start = performance.now();
            """)
        except Exception:
            pass

    def start_step(self, name):
        self.step_name = name
        self.start_time = time.time()

        # reset pause state
        self._pause_stack = 0
        self._pause_start = None
        self._paused_total = 0
        self.measurement_active = True

    def pause_timer(self):                
            if self.start_time is None:
                return

            if self._pause_stack == 0:
                self._pause_start = time.time()

            self._pause_stack += 1

    def resume_timer(self):
            """
            Resume timing
            """
            if self.start_time is None:
                return

            if self._pause_stack == 0:
                return

            self._pause_stack -= 1

            if self._pause_stack == 0 and self._pause_start is not None:
                self._paused_total += time.time() - self._pause_start
                self._pause_start = None


    def end_step(self, page):
        if self.start_time is None:
            return {
                "step": self.step_name or "unknown",
                "duration_ms": -1,
                "fcp": -1,
                "lcp": -1,
                "cls": -1,
                "status": "FAILURE"
            }
        end_time = time.time()

        # if still paused → close pause window
        if self._pause_stack > 0 and self._pause_start is not None:
            self._paused_total += end_time - self._pause_start
            self._pause_start = None
            self._pause_stack = 0

        # Updated duration
        duration = int((end_time - self.start_time - self._paused_total) * 1000)
        duration = max(duration, -1)
        def safe_eval(expr):
            try:
                return page.evaluate(expr)
            except Exception:
                return -1

        fcp = safe_eval("performance.getEntriesByName('first-contentful-paint')[0]?.startTime || -1")

        lcp = safe_eval("""
        () => new Promise(resolve => {
            let value = -1;
            const observer = new PerformanceObserver(list => {
                const e = list.getEntries();
                value = e[e.length - 1]?.startTime || -1;
            });
            observer.observe({ type: 'largest-contentful-paint', buffered: true });
            setTimeout(() => resolve(value), 1500);
        })""")

        cls = safe_eval("""
        () => new Promise(resolve => {
            let clsValue = 0;
            const observer = new PerformanceObserver(list => {
                for (const entry of list.getEntries()) {
                    if (!entry.hadRecentInput) {
                        clsValue += entry.value;
                    }
                }
            });
            observer.observe({ type: 'layout-shift', buffered: true });
            setTimeout(() => resolve(clsValue), 1500);
        })""")
        cls_value = float(cls) if isinstance(cls, (int, float)) else -1
        self.start_time = None

        result = {
            "step": self.step_name,
            "duration_ms": duration if duration >= 0 else -1,
            "fcp": int(fcp) if isinstance(fcp, (int, float)) else -1,
            "lcp": int(lcp) if isinstance(lcp, (int, float)) else -1,
            "cls": round(cls_value, 3) if cls_value >= 0 else -1,
            "status": "SUCCESS"
        }
        # reset
        self.start_time = None
        self.step_name = None
        self.measurement_active = False

        return result
    
# ================= PROMETHEUS METRIC BUILDERS =================

def build_url_metrics(registry):
    return {
        "duration": Histogram(
            "synthetic_url_duration_ms",
            "URL duration distribution",
            ["env", "scenario", "run_id"],
            buckets=LATENCY_BUCKETS_MS,
            registry=registry
        ),
        "fcp": Histogram(
            "synthetic_url_fcp_ms",
            "URL FCP distribution",
            ["env", "scenario", "run_id"],
            buckets=LATENCY_BUCKETS_MS,
            registry=registry
        ),
        "lcp": Histogram(
            "synthetic_url_lcp_ms",
            "URL LCP distribution",
            ["env", "scenario", "run_id"],
            buckets=LATENCY_BUCKETS_MS,
            registry=registry
        ),
        "cls": Gauge(
            "synthetic_url_cls",
            "URL CLS",
            ["env", "scenario", "run_id"],
            registry=registry
        ),
        "success": Counter("synthetic_url_success_total","URL success",["env","scenario","run_id"],registry=registry),
        "failure": Counter("synthetic_url_failure_total","URL failure",["env","scenario","run_id"],registry=registry),
    }

def build_journey_metrics(registry):
    return {
        "journey_duration": Histogram(
            "synthetic_journey_duration_ms",
            "Journey duration distribution",
            ["env","journey","run_id"],
            buckets=LATENCY_BUCKETS_MS,
            registry=registry
        ),
        "step_duration": Histogram(
            "synthetic_step_duration_ms",
            "Step duration distribution",
            ["env","journey","step","run_id"],
            buckets=LATENCY_BUCKETS_MS,
            registry=registry
        ),
        "step_fcp": Histogram(
            "synthetic_step_fcp_ms",
            "Step FCP distribution",
            ["env","journey","step","run_id"],
            buckets=LATENCY_BUCKETS_MS,
            registry=registry
        ),
        "step_lcp": Histogram(
            "synthetic_step_lcp_ms",
            "Step LCP distribution",
            ["env","journey","step","run_id"],
            buckets=LATENCY_BUCKETS_MS,
            registry=registry
        ),
        "step_cls": Gauge(
            "synthetic_step_cls",
            "Step CLS",
            ["env","journey","step","run_id"],
            registry=registry
        ),
        "success": Counter("synthetic_journey_success_total","Journey success",["env","journey","run_id"],registry=registry),
        "failure": Counter("synthetic_journey_failure_total","Journey failure",["env","journey","run_id"],registry=registry),
    }

def cleanup_pushgateway(job_name, run_id, env):
    try:
        delete_from_gateway(
            PUSHGATEWAY_URL,
            job=job_name,
            grouping_key={"run_id": run_id, "env": env}
)
        print(f"[INFO] Cleared stale metrics for job: {job_name}")
    except Exception as e:
        print(f"[WARN] Could not delete old metrics: {e}")

# ================= MODES =================

def run_url_mode(args, base_dir, run_id, time_formatter):
    cleanup_pushgateway(PROM_JOB_URL, run_id, args.env)
    screenshot_dir = os.path.join(base_dir, "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)

    registry = CollectorRegistry()
    metrics = build_url_metrics(registry)

    raw_results = os.path.join(base_dir, "results.csv")
    error_log = os.path.join(base_dir, "errors.csv")
    summary_path = os.path.join(base_dir, "summary_report.csv")
    bucketed_path = os.path.join(base_dir, "bucketed_performance_report.csv")
    #prom_path = os.path.join(base_dir, "prometheus_metrics.txt")

    duration_col = f"duration_{time_formatter.label}"
    fcp_col = f"fcp_{time_formatter.label}"
    lcp_col = f"lcp_{time_formatter.label}"

    urls = load_urls(args.urls)
    if not urls:
        raise ValueError("No URLs found in the provided input.")

    end_time = time.time() + args.duration * 60
    bucket_minutes = max(1, args.bucket)

    timings = defaultdict(list)
    bucketed_timings = defaultdict(lambda: defaultdict(list))
    bucketed_lcp = defaultdict(lambda: defaultdict(list))

    observer = ScenarioObserver()

    with open(raw_results, "w", newline="") as raw_file, open(error_log, "w", newline="") as err_file:
        raw_writer = csv.writer(raw_file)
        err_writer = csv.writer(err_file)

        raw_writer.writerow([
            "timestamp_utc", "env", "run_id",
            "scenario", "status",
            duration_col, fcp_col, lcp_col, "cls",
            "error_type", "error_message", "screenshot_path"
        ])

        err_writer.writerow([
            "timestamp_utc", "env", "run_id",
            "scenario", "error_type", "error_message", "screenshot_path"
        ])

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=HeadlessMode)
            context = browser.new_context()

            while time.time() < end_time:
                for url in urls:
                    if time.time() >= end_time:
                        break

                    page = context.new_page()
                    def _auto_clear(frame):
                            if frame == page.main_frame and not observer.measurement_active:
                                page.wait_for_timeout(0)  # yield loop
                                observer.clear_performance_buffer(page)
                    page.on("framenavigated", _auto_clear)
                    now = datetime.utcnow()

                    status = "SUCCESS"
                    error_type = ""
                    error_message = ""
                    screenshot_path = ""

                    duration = fcp = lcp = -1
                    cls = -1

                    try:
                        observer.start(url)
                        observer.clear_performance_buffer(page)
                        page.goto(url, wait_until="load", timeout=60000)
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(900)
                        duration, fcp, lcp, cls = observer.end(page)
                    except PlaywrightTimeoutError as exc:
                        status = "FAILURE"
                        error_type = "TIMEOUT"
                        error_message = str(exc)
                    except Exception as exc:
                        status = "FAILURE"
                        error_type = "NAVIGATION_ERROR"
                        error_message = str(exc)

                    if status != "SUCCESS":
                        screenshot_path = os.path.join(
                            screenshot_dir,
                            f"{now.strftime('%Y%m%dT%H%M%S')}_{safe_url_name(url)}_{error_type}.png"
                        )
                        try:
                            page.screenshot(path=screenshot_path, full_page=True)
                        except Exception:
                            screenshot_path = ""

                        err_writer.writerow([
                            now.isoformat(), args.env, run_id,
                            url, error_type, error_message, screenshot_path
                        ])

                    converted_duration = time_formatter.convert(duration)
                    converted_fcp = time_formatter.convert(fcp)
                    converted_lcp = time_formatter.convert(lcp)

                    if status == "SUCCESS":
                        metrics["success"].labels(args.env, url, run_id).inc()
                    else:
                        metrics["failure"].labels(args.env, url, run_id).inc()


                    raw_writer.writerow([
                        now.isoformat(), args.env, run_id,
                        url, status,
                        converted_duration, converted_fcp, converted_lcp, round(cls, 3) if isinstance(cls, (int, float)) and cls >= 0 else "",
                        error_type, error_message, screenshot_path
                    ])

                    if status == "SUCCESS" and duration > 0:
                        timings[url].append(duration)
                        bucket_start = get_time_bucket(now, bucket_minutes)
                        bucketed_timings[url][bucket_start].append(duration)
                        if lcp > 0:
                            bucketed_lcp[url][bucket_start].append(lcp)

                    if duration > 0:
                        metrics["duration"].labels(args.env, url, run_id).observe(duration)
                    if fcp > 0:
                        metrics["fcp"].labels(args.env, url, run_id).observe(fcp)
                    if lcp > 0:
                        metrics["lcp"].labels(args.env, url, run_id).observe(lcp)
                    if isinstance(cls, (int, float)) and cls >= 0:
                        metrics["cls"].labels(args.env, url, run_id).set(cls)
                    

                    
                    page.off("framenavigated", _auto_clear)
                    page.close()
                    time.sleep(args.delay)

                try:
                        push_to_gateway(
                            PUSHGATEWAY_URL,
                            job=PROM_JOB_URL,
                            registry=registry,
                            grouping_key={"run_id": run_id, "env": args.env}
                        )
                except Exception:
                        pass

            browser.close()

    with open(summary_path, "w", newline="") as summary_file:
        writer = csv.writer(summary_file)
        writer.writerow([
            "scenario",
            f"avg_{time_formatter.label}",
            f"p90_{time_formatter.label}",
            f"max_{time_formatter.label}",
            f"min_{time_formatter.label}",
            "samples"
        ])
        for scenario, values in timings.items():
            writer.writerow([
                scenario,
                time_formatter.convert(statistics.mean(values)),
                time_formatter.convert(percentile(values, 90)),
                time_formatter.convert(max(values)),
                time_formatter.convert(min(values)),
                len(values)
            ])

    with open(bucketed_path, "w", newline="") as bucket_file:
        writer = csv.writer(bucket_file)
        writer.writerow([
            "bucket_start_utc", "env", "run_id", "scenario",
            f"p90_load_{time_formatter.label}", f"avg_load_{time_formatter.label}",
            f"p90_lcp_{time_formatter.label}", f"avg_lcp_{time_formatter.label}", "samples"
        ])
        for scenario, bucket_map in bucketed_timings.items():
            for bucket_start, durations in bucket_map.items():
                lcp_values = bucketed_lcp[scenario].get(bucket_start, [])
                writer.writerow([
                    bucket_start.isoformat(), args.env, run_id, scenario,
                    time_formatter.convert(percentile(durations, 90)),
                    time_formatter.convert(statistics.mean(durations)),
                    time_formatter.convert(percentile(lcp_values, 90)) if lcp_values else -1,
                    time_formatter.convert(statistics.mean(lcp_values)) if lcp_values else -1,
                    len(durations)
                ])

def run_journey_mode(args, base_dir, run_id, time_formatter):
    cleanup_pushgateway(PROM_JOB_JOURNEY, run_id,args.env)
    if not args.journey_data:
        raise ValueError("Journey mode requires --journey-data CSV input.")
    registry = CollectorRegistry()
    metrics = build_journey_metrics(registry)

    screenshot_dir = os.path.join(base_dir, "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)

    results_path = os.path.join(base_dir, "results.csv")
    step_results_path = os.path.join(base_dir, "step_results.csv")
    step_summary_path = os.path.join(base_dir, "step_summary_report.csv")
    step_bucketed_path = os.path.join(base_dir, "step_bucketed_performance_report.csv")
    prom_path = os.path.join(base_dir, "prometheus_metrics.txt")

    total_duration_col = f"total_duration_{time_formatter.label}"
    step_duration_col = f"duration_{time_formatter.label}"
    step_fcp_col = f"fcp_{time_formatter.label}"
    step_lcp_col = f"lcp_{time_formatter.label}"

    journey_timings = defaultdict(list)
    step_timings = defaultdict(list)
    bucket_minutes = max(1, args.bucket)
    bucketed_step_timings = defaultdict(lambda: defaultdict(list))
    bucketed_step_lcp = defaultdict(lambda: defaultdict(list))

    end_time = time.time() + args.duration * 60

    with open(results_path, "w", newline="") as jr, open(step_results_path, "w", newline="") as sr:
        journey_writer = csv.writer(jr)
        step_writer = csv.writer(sr)

        journey_writer.writerow([
            "timestamp", "env", "run_id",
            "journey", "status", total_duration_col, "input_data"
        ])

        step_writer.writerow([
            "timestamp", "env", "run_id",
            "journey", "step",
            step_duration_col, step_fcp_col, step_lcp_col, "cls",
            "status", "screenshot", "error"
        ])

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=HeadlessMode)
            ctx = browser.new_context(http_credentials={"username": HTTP_CREDS[0], "password": HTTP_CREDS[1]})

            index = 0
            data_rows = load_journey_data(args.journey_data)
            stop_requested = False

            while time.time() < end_time and not stop_requested:
                for journey_name, fn in JOURNEYS.items():
                    for row in data_rows:

                        page = ctx.new_page()
                        observer = StepObserver()

                        def _auto_clear(frame):
                            if frame == page.main_frame and not observer.measurement_active:
                                page.wait_for_timeout(0)  # yield loop
                                observer.clear_performance_buffer(page)
                        page.on("framenavigated", _auto_clear)

                        steps = []

                        journey_start = time.time()
                        try:
                            steps = fn(page, observer, row, index)
                            index += 1

                            status = "SUCCESS"
                            if any(s.get("status") != "SUCCESS" for s in steps):
                                status = "FAILURE"

                            for step_data in steps:
                                if step_data["duration_ms"] > 0:
                                    step_timings[(journey_name, step_data["step"])].append(
                                    step_data["duration_ms"]
                                )

                                error_text = clean_error_message(step_data.get("error", ""))
                                screenshot_path = ""
                                if step_data.get("status") != "SUCCESS":
                                    screenshot_path = capture_screenshot(
                                        page,
                                        screenshot_dir,
                                        journey_name,
                                        step_data.get("step", "unknown"),
                                        label=f"row{index}"
                                    )

                                step_timestamp = datetime.utcnow()
                                converted_step_duration = time_formatter.convert(step_data.get("duration_ms"))
                                converted_step_fcp = time_formatter.convert(step_data.get("fcp"))
                                converted_step_lcp = time_formatter.convert(step_data.get("lcp"))
                                step_writer.writerow([
                                    step_timestamp.isoformat(),
                                    args.env,
                                    run_id,
                                    journey_name,
                                    step_data["step"],
                                    converted_step_duration,
                                    converted_step_fcp,
                                    converted_step_lcp,
                                    step_data["cls"],
                                    step_data["status"],
                                    screenshot_path,
                                    error_text
                                ])

                                duration_valid = step_data["duration_ms"] is not None and step_data["duration_ms"] > 0
                                if duration_valid:
                                    bucket_start = get_time_bucket(step_timestamp, bucket_minutes)
                                    bucketed_step_timings[(journey_name, step_data["step"])][bucket_start].append(
                                        step_data["duration_ms"]
                                    )

                                    lcp_value = step_data.get("lcp", -1)
                                    if isinstance(lcp_value, (int, float)) and lcp_value >= 0:
                                        bucketed_step_lcp[(journey_name, step_data["step"])][bucket_start].append(
                                            lcp_value
                                        )

                        except Exception as exc:
                            status = "FAILURE"
                            failed_step = observer.step_name or "unknown"

                            shot = capture_screenshot(
                                page,
                                screenshot_dir,
                                journey_name,
                                failed_step,
                                label=f"row{index}"
                            )
                            
                            error_text = clean_error_message(str(exc))

                            step_timestamp = datetime.utcnow()
                            step_writer.writerow([
                                step_timestamp.isoformat(),
                                args.env,
                                run_id,
                                journey_name,
                                failed_step,
                                -1,
                                -1,
                                -1,
                                -1,
                                "FAILURE",
                                shot,
                                error_text
                            ])

                        total_duration = int((time.time() - journey_start) * 1000)
                        if total_duration > 0:
                            journey_timings[journey_name].append(total_duration)

                        converted_total = time_formatter.convert(total_duration)

                        if total_duration > 0:
                            metrics["journey_duration"].labels(
                                        args.env, journey_name, run_id
                                    ).observe(total_duration)

                        for step in steps:
                            if step["duration_ms"] > 0:
                                metrics["step_duration"].labels(
                                    args.env,
                                    journey_name,
                                    step["step"],
                                    run_id).observe(step["duration_ms"])
                            if step["fcp"] > 0:
                                metrics["step_fcp"].labels(args.env, journey_name, step["step"], run_id).observe(step["fcp"])
                            if step["lcp"] > 0:
                                metrics["step_lcp"].labels(args.env, journey_name, step["step"], run_id).observe(step["lcp"])
                            if isinstance(step["cls"], (int, float)) and step["cls"] >= 0:
                                metrics["step_cls"].labels(args.env, journey_name, step["step"], run_id).set(step["cls"])
                        
                        if status == "SUCCESS":
                            metrics["success"].labels(args.env, journey_name, run_id).inc()
                        else:
                            metrics["failure"].labels(args.env, journey_name, run_id).inc()

                        try:
                            push_to_gateway(
                                PUSHGATEWAY_URL,
                                job=PROM_JOB_JOURNEY,
                                registry=registry,
                                grouping_key={"run_id": run_id, "env": args.env}
                            )
                        except Exception:
                            pass

                        journey_writer.writerow([
                            datetime.utcnow().isoformat(),
                            args.env,
                            run_id,
                            journey_name,
                            status,
                            converted_total,
                            str(row)
                        ])

                        page.off("framenavigated", _auto_clear)
                        page.close()
                        time.sleep(args.delay)

                        if time.time() >= end_time:
                            stop_requested = True
                            break

                    if stop_requested:
                        break

                if stop_requested:
                    break

            browser.close()

    with open(step_summary_path, "w", newline="") as summary_file:
        summary_writer = csv.writer(summary_file)
        summary_writer.writerow([
            "journey", "step",
            f"avg_{time_formatter.label}",
            f"p90_{time_formatter.label}",
            f"max_{time_formatter.label}",
            f"min_{time_formatter.label}",
            "samples"
        ])
        for (journey_name, step_name), values in step_timings.items():
            valid_values = [v for v in values if v > 0]
            if not valid_values:
                continue

            summary_writer.writerow([
                journey_name,
                step_name,
                time_formatter.convert(statistics.mean(valid_values)),
                time_formatter.convert(percentile(valid_values, 90)),
                time_formatter.convert(max(valid_values)),
                time_formatter.convert(min(valid_values)),
                len(valid_values)
            ])

    with open(step_bucketed_path, "w", newline="") as bucket_file:
        bucket_writer = csv.writer(bucket_file)
        bucket_writer.writerow([
            "bucket_start_utc", "env", "run_id", "journey", "step",
            f"p90_duration_{time_formatter.label}", f"avg_duration_{time_formatter.label}",
            f"p90_lcp_{time_formatter.label}", f"avg_lcp_{time_formatter.label}", "samples"
        ])

        for (journey_name, step_name), bucket_map in bucketed_step_timings.items():
            for bucket_start, durations in bucket_map.items():
                if not durations:
                    continue

                lcp_values = bucketed_step_lcp[(journey_name, step_name)].get(bucket_start, [])

                bucket_writer.writerow([
                    bucket_start.isoformat(),
                    args.env,
                    run_id,
                    journey_name,
                    step_name,
                    time_formatter.convert(percentile(durations, 90)),
                    time_formatter.convert(statistics.mean(durations)),
                    time_formatter.convert(percentile(lcp_values, 90)) if lcp_values else -1,
                    time_formatter.convert(statistics.mean(lcp_values)) if lcp_values else -1,
                    len(durations)
                ])
# ================= MAIN =================

def main():
    args = parse_args()

    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    base_dir = os.path.join("runs", args.env, run_id)
    os.makedirs(base_dir, exist_ok=True)
    time_formatter = TimeFormatter(args.time_unit)

    if args.mode == "url":
        run_url_mode(args, base_dir, run_id, time_formatter)
    else:
        run_journey_mode(args, base_dir, run_id, time_formatter)

if __name__ == "__main__":
    main()