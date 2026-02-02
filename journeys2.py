# journeys.py

def login_search_add_to_cart(page, observer, data):
    steps = []

    def run_step(name, fn):
        observer.start_step(name)
        try:
            fn()
            steps.append(observer.end_step(page))
        except Exception:
            steps.append({
                "step": name,
                "duration_ms": -1,
                "fcp": -1,
                "lcp": -1,
                "cls": -1,
                "status": "FAILURE"
            })
            raise

    # STEP 1 — Launch
    run_step("launch", lambda: (
        page.goto("https://example.com"),
        page.wait_for_load_state("networkidle")
    ))

    # STEP 2 — Login
    run_step("login", lambda: (
        page.wait_for_selector("#username"),
        page.fill("#username", data["username"]),
        page.wait_for_selector("#password"),
        page.fill("#password", data["password"]),
        page.click("#login"),
        page.wait_for_load_state("networkidle")
    ))

    # STEP 3 — Search
    run_step("search", lambda: (
        page.wait_for_selector("#search"),
        page.fill("#search", data["search_term"]),
        page.press("#search", "Enter"),
        page.wait_for_load_state("networkidle")
    ))

    # STEP 4 — Add to cart
    run_step("add_to_cart", lambda: (
        page.wait_for_selector(f"#add-{data['product_id']}"),
        page.click(f"#add-{data['product_id']}"),
        page.wait_for_timeout(1000)
    ))

    return steps


JOURNEYS = {
    "login_search_add_to_cart": login_search_add_to_cart
}
