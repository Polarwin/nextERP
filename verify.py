from playwright.sync_api import sync_playwright

env = {}
with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip().lower()] = v.strip().strip('"').strip("'")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(env["website"], wait_until="domcontentloaded", timeout=30000)
    page.fill('input[autocomplete="username"]', env.get("username") or env.get("user"))
    page.fill('input[type="password"]', env["password"])
    page.click('button[type="submit"]')
    page.wait_for_url("**/app**", timeout=15000)
    page.wait_for_selector(".navbar, #body, .layout-main", timeout=30000)
    page.wait_for_timeout(5000)
    cookies = {c["name"] for c in page.context.cookies()}
    print("sid cookie present:", "sid" in cookies)
    print("logged-in user (navbar):", page.title())
    page.screenshot(path="login_result.png", full_page=False)
    browser.close()
