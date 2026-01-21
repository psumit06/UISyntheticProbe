import time
import csv
import math
import statistics
import os
import uuid
import argparse
import subprocess
from datetime import datetime
from collections import defaultdict

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ================= CLI =================

def parse_args():
    p = argparse.ArgumentParser("Unified UI Synthetic Monitor")
    p.add_argument("--mode", choices=["url", "journey"], default="url")
    p.add_argument("--env", default="staging")
    p.add_argument("--input", default="urls.txt")
    p.add_argument("--duration", type=int, default=30)
    p.add_argument("--delay", type=int, default=5)
    p.add_argument("--bucket", type=int, default=5)
    p.add_argument("--lighthouse", action="store_true")
    return p.parse_args()

# ================= UTIL =================

def load_inputs(path):
    if path.endswith(".csv"):
        return pd.read_csv(path)["url"].dropna().tolist()
    with open(path) as f:
        return [x.strip() for x in f if x.strip()]

def percentile(data, pct):
    if not data:
        return -1
    data = sorted(data)
    k = (len(data) - 1) * (pct / 100)
    f = math.floor(k)
    c = math.ceil(k)
    return data[int(k)] if f == c else data[f] + (data[c] - data[f]) * (k - f)

def bucket_time(ts, size):
    return ts.replace(
        minute=(ts.minute // size) * size,
        second=0,
        microsecond=0
    )

def safe_name(v):
    return v.replace("https://", "").replace("http://", "").replace("/", "_")

# ================= LIGHTHOUSE =================

def run_lighthouse(url, ts):
    os.makedirs("lighthouse_reports", exist_ok=True)
    report = f"lighthouse_reports/{safe_name(url)}_{ts}.html"
    cmd = f'npx.cmd lighthouse "{url}" --output=html --output-path="{report}" --chrome-flags="--headless"'
    subprocess.run(cmd, shell=True)
    return report if os.path.exists(report) else ""

# ================= SCENARIO OBSERVER =================

class ScenarioObserver:
    def __init__(self):
        self.start = None
        self.name = None

    def begin(self, name):
        self.name = name
        self.start = time.time()

    def end(self, page):
        duration = int((time.time() - self.start) * 1000)

        fcp = page.evaluate(
            "() => performance.getEntriesByName('first-contentful-paint')[0]?.startTime || -1"
        )
        lcp = page.evaluate("""
        () => new Promise(r=>{
            new PerformanceObserver(l=>{
                const e=l.getEntries();r(e[e.length-1]?.startTime||-1)
            }).observe({type:'largest-contentful-paint',buffered:true})
        })
        """)
        cls = page.evaluate("""
        () => {
            let v=0;
            new PerformanceObserver(l=>{
                for(const e of l.getEntries()){
                    if(!e.hadRecentInput)v+=e.value
                }
            }).observe({type:'layout-shift',buffered:true});
            return v;
        }
        """)
        self.start = None
        return duration, int(fcp), int(lcp), round(cls,3)

# ================= BUSINESS JOURNEYS =================
# Plug your real flows here

def journey_login(page):
    page.goto("https://example.com")
    page.click("#login")

JOURNEYS = {
    "Login": journey_login,
}

# ================= MAIN =================

def main():
    args = parse_args()

    RUN_ID = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
    BASE = os.path.join("runs", args.env, RUN_ID)
    os.makedirs(BASE, exist_ok=True)

    RAW = f"{BASE}/results.csv"
    ERR = f"{BASE}/errors.csv"
    SUM = f"{BASE}/summary_report.csv"
    BUCKET = f"{BASE}/bucketed_performance_report.csv"
    SHOTS = f"{BASE}/screenshots"
    os.makedirs(SHOTS, exist_ok=True)

    inputs = load_inputs(args.input)
    end_time = time.time() + args.duration * 60

    timings = defaultdict(list)
    bucketed = defaultdict(lambda: defaultdict(list))
    bucketed_lcp = defaultdict(lambda: defaultdict(list))

    observer = ScenarioObserver()

    with open(RAW, "w", newline="") as r, open(ERR, "w", newline="") as e:
        rw, ew = csv.writer(r), csv.writer(e)

        rw.writerow([
            "timestamp_utc","env","run_id",
            "entity","status",
            "duration_ms","fcp_ms","lcp_ms","cls",
            "error_type","error_message","screenshot","lighthouse"
        ])
        ew.writerow([
            "timestamp_utc","env","run_id",
            "entity","error_type","error_message","screenshot"
        ])

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()

            while time.time() < end_time:
                for item in inputs:
                    if time.time() >= end_time:
                        break

                    page = ctx.new_page()
                    now = datetime.utcnow()
                    status, et, em, shot, lh = "SUCCESS", "", "", "", ""
                    dur=fcp=lcp=-1; cls=0

                    try:
                        observer.begin(item)
                        if args.mode == "url":
                            page.goto(item, timeout=60000)
                        else:
                            JOURNEYS[item](page)
                        dur,fcp,lcp,cls = observer.end(page)

                        if args.lighthouse and args.mode=="url":
                            lh = run_lighthouse(item, now.strftime("%H%M%S"))

                    except PlaywrightTimeoutError as ex:
                        status, et, em = "FAILURE","TIMEOUT",str(ex)
                    except Exception as ex:
                        status, et, em = "FAILURE","ERROR",str(ex)

                    if status!="SUCCESS":
                        shot=f"{SHOTS}/{safe_name(item)}_{now.strftime('%H%M%S')}.png"
                        try: page.screenshot(path=shot,full_page=True)
                        except: shot=""

                        ew.writerow([now.isoformat(),args.env,RUN_ID,item,et,em,shot])

                    rw.writerow([
                        now.isoformat(),args.env,RUN_ID,
                        item,status,
                        dur,fcp,lcp,cls,
                        et,em,shot,lh
                    ])

                    if status=="SUCCESS" and dur>0:
                        timings[item].append(dur)
                        b=bucket_time(now,args.bucket)
                        bucketed[item][b].append(dur)
                        if lcp>0: bucketed_lcp[item][b].append(lcp)

                    page.close()
                    time.sleep(args.delay)

            browser.close()

    # SUMMARY
    with open(SUM,"w",newline="") as f:
        w=csv.writer(f)
        w.writerow(["entity","avg_ms","p90_ms","max","min","samples"])
        for k,v in timings.items():
            w.writerow([k,int(statistics.mean(v)),int(percentile(v,90)),max(v),min(v),len(v)])

    # BUCKETED
    with open(BUCKET,"w",newline="") as f:
        w=csv.writer(f)
        w.writerow(["bucket_start","env","run_id","entity","p90_ms","avg_ms","p90_lcp","avg_lcp","samples"])
        for ent,bks in bucketed.items():
            for b,t in bks.items():
                l=bucketed_lcp[ent].get(b,[])
                w.writerow([
                    b.isoformat(),args.env,RUN_ID,ent,
                    int(percentile(t,90)),int(statistics.mean(t)),
                    int(percentile(l,90)) if l else -1,
                    int(statistics.mean(l)) if l else -1,
                    len(t)
                ])

if __name__=="__main__":
    main()
