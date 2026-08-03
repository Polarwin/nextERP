"""Generic login automation: reads WEBSITE, USERNAME, PASSWORD from .env,
attempts to log in with Playwright, and saves a screenshot of the result.
Credentials are never printed."""
import re
import sys
from playwright.sync_api import sync_playwright

env = {}
with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip().lower()] = v.strip().strip('"').strip("'")

website = env.get("website")
username = env.get("username") or env.get("user") or env.get("email")
password = env.get("password")
if not all([website, username, password]):
    sys.exit("Missing website/username/password in .env")
if not re.match(r"https?://", website):
    website = "https://" + website

USER_SELECTORS = [
    'input[name="username"]', 'input[name="user"]', 'input[name="email"]',
    'input[name="login"]', 'input[id="username"]', 'input[id="email"]',
    'input[type="email"]', 'input[autocomplete="username"]',
]
PASS_SELECTORS = ['input[type="password"]', 'input[name="password"]']
SUBMIT_SELECTORS = [
    'button[type="submit"]', 'input[type="submit"]',
    'button:has-text("Log in")', 'button:has-text("Sign in")',
    'button:has-text("Login")', 'button:has-text("登录")',
]


def fill_first(page, selectors, value):
    for sel in selectors:
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.fill(value)
            return sel
    return None


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(website, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    u = fill_first(page, USER_SELECTORS, username)
    pw = fill_first(page, PASS_SELECTORS, password)
    print(f"username field: {u or 'NOT FOUND'}")
    print(f"password field: {pw or 'NOT FOUND'}")

    if u and pw:
        clicked = None
        for sel in SUBMIT_SELECTORS:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                clicked = sel
                break
        if not clicked:
            page.keyboard.press("Enter")
            clicked = "Enter key"
        print(f"submitted via: {clicked}")
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        page.wait_for_timeout(3000)

    print(f"final URL: {page.url}")
    print(f"page title: {page.title()}")
    page.screenshot(path="login_result.png", full_page=True)
    browser.close()
print("Screenshot saved to login_result.png")
