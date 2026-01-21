# journeys.py

def login_add_to_cart(page, observer):
    observer.begin()

    page.goto("https://example.com")
    page.wait_for_load_state("networkidle")

    # Example future steps:
    # page.fill("#username", "demo")
    # page.fill("#password", "secret")
    # page.click("#login")

    return observer.end(page)


def search_product(page, observer):
    observer.begin()

    page.goto("https://example.com/search")
    page.wait_for_load_state("networkidle")

    return observer.end(page)


# Registry of journeys
JOURNEYS = {
    "login_add_to_cart": login_add_to_cart,
    "search_product": search_product
}
