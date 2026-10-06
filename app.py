import os
import logging
from playwright.async_api import async_playwright

USER_EMAIL = "hiiamdudetntt"
USER_PASSWORD = "ritika123"

async def ensure_logged_in():
    async with async_playwright() as p:
        # Pass stealth arguments to lower bot detection flags on Render
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        logging.info("[*] Navigating to login page...")
        await page.goto("https://www.instagram.com/accounts/login/", wait_until="networkidle")

        # 1. Accept Cookie Banners if present
        try:
            cookie_button = page.get_by_role("button", name="Allow all cookies")
            if await cookie_button.is_visible(timeout=5000):
                await cookie_button.click()
        except Exception:
            pass

        # 2. Wait explicitly for the username input
        try:
            logging.info("[*] Waiting for username input field...")
            username_input = await page.wait_for_selector('input[name="username"]', timeout=15000)
            await username_input.fill(USER_EMAIL)
            
            password_input = await page.wait_for_selector('input[name="password"]', timeout=5000)
            await password_input.fill(USER_PASSWORD)

            await page.click('button[type="submit"]')
            logging.info("[*] Login form submitted successfully.")

        except Exception as e:
            # Capture what the browser actually sees for debugging in Render logs
            page_title = await page.title()
            page_content = await page.content()
            logging.error(f"[!] Login failed. Current page title: '{page_title}'")
            logging.error(f"[!] Page HTML snippet: {page_content[:500]}...")
            await browser.close()
            raise e

        await browser.close()
