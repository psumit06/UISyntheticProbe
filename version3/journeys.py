from pathlib import Path
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
THINK_TIME_MS = 2000
SCREENSHOT_DIR = Path("./screenshots")
CAPTURE_SCREENSHOTS = False
payment_email = "abc@gmail.com"

def synthetic_wait(page, observer, ms):
    observer.pause_timer()
    page.wait_for_timeout(ms)
    observer.resume_timer()

def _clean_field(row, key):
    return str(row.get(key, "") or "").strip()

def _safe_wait_for_load_state(page, state="networkidle", timeout=15_000):
    try:
        page.wait_for_load_state(state, timeout=timeout)
    except PlaywrightTimeoutError:
        print(f"Warning: Timeout while waiting for load state '{state}'")
        return False
    return True

def _summarize_error(exc):
    message = str(exc).strip()
    if not message:
        return "An error occurred, but no details are available."
    
    marker = "==================================="
    if marker in message:
        message = message.split(marker, 1)[0].strip()

        first_line = message.splitlines()[0] if message else "An error occurred."
        return first_line
    
def maybe_screenshot(page, filename):
    if CAPTURE_SCREENSHOTS:
        SCREENSHOT_DIR.mkdir(parents=True,exist_ok=True)
        path = SCREENSHOT_DIR / f"{filename}.png"
        page.screenshot(path=str(path), full_page=True)
        print(f"Captured screenshot: {path}")

def launch_journey(page, observer, row, index):
    steps = []
    bill_no = _clean_field(row, "Billno")
    dob = _clean_field(row, "DOB")
    zipcode = _clean_field(row, "ZIP")

    def run_step(name, fn):
        observer.start_step(name)
        status = "SUCCESS"
        error = None
        try:
            fn()
        except Exception as exc:
            status = "FAILURE"
            error = _summarize_error(exc)
            print(f"Error in step '{name}': {error}")
        finally:
            step_result = observer.end_step(page)
            step_result["status"] = status
            if error:
                step_result["error"] = error
            steps.append(step_result)
            page.wait_for_timeout(THINK_TIME_MS)

    def step_launch():
        page.goto("https://www.medicare.gov/care-compare/")
        maybe_screenshot(page, f"journey_{index}_launch")
        if not _safe_wait_for_load_state(page):
            raise Exception("Page did not load properly after launch.")
        
    def step_search():
        page.fill("input[aria-label='Search for a hospital']", bill_no)
        maybe_screenshot(page, f"journey_{index}_search_filled")
        page.click("button:has-text('Search')")
        maybe_screenshot(page, f"journey_{index}_search_clicked")
        if not _safe_wait_for_load_state(page):
            raise Exception("Page did not load properly after search.")
        
    def step_add_amount():
        page.click("button:has-text('Add amount')")
        maybe_screenshot(page, f"journey_{index}_add_amount_clicked")
        page.fill("input[aria-label='Enter amount']", "1000")
        maybe_screenshot(page, f"journey_{index}_add_amount_filled")
        page.click("button:has-text('Save')")
        maybe_screenshot(page, f"journey_{index}_add_amount_saved")
        if not _safe_wait_for_load_state(page):
            raise Exception("Page did not load properly after adding amount.")
        
    def step_add_email():
        page.click("button:has-text('Add email')")
        maybe_screenshot(page, f"journey_{index}_add_email_clicked")
        page.fill("input[aria-label='Enter email']", payment_email)
        maybe_screenshot(page, f"journey_{index}_add_email_filled")
        page.click("button:has-text('Save')")
        maybe_screenshot(page, f"journey_{index}_add_email_saved")
        if not _safe_wait_for_load_state(page):
            raise Exception("Page did not load properly after adding email.")
    def step_submit():
        page.click("button:has-text('Submit')")
        maybe_screenshot(page, f"journey_{index}_submit_clicked")
        if not _safe_wait_for_load_state(page):
            raise Exception("Page did not load properly after submit.")
        
    run_step("Launch", step_launch)
    run_step("Search", step_search)
    run_step("Add Amount", step_add_amount)
    run_step("Add Email", step_add_email)
    run_step("Submit", step_submit)

    return steps

JOURNEYS = {
    "medicare_payment": launch_journey
}
