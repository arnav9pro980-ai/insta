import base64
import json
import os
import time
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, HTTPException
from playwright.sync_api import sync_playwright

# Configuration from Environment Variables (Fallback to provided defaults)
FIREBASE_DB_URL = os.getenv(
    "FIREBASE_DB_URL",
    "https://launcher-c813d-default-rtdb.europe-west1.firebasedatabase.app"
)
IG_USERNAME = os.getenv("IG_USERNAME", "hiiamdudetntt")
IG_PASSWORD = os.getenv("IG_PASSWORD", "ritika123")

INBOX_URL = "https://www.instagram.com/direct/inbox/"
LOGIN_URL = "https://www.instagram.com/accounts/login/"
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playwright_profile")

# Global Application State
APP_STATE = {
    "is_authenticated": False,
    "last_check_time": None,
    "active_url": None,
    "session_saved_to_firebase": False,
    "error": None
}

# JavaScript Injections
JS_AUTO_LOGIN = """
(args) => {
    const { userEmail, userPassword } = args;
    const emailSelector = 'input[type="email"], input[name="email"], input[id="email"], input[name="username"]';
    const passSelector = 'input[type="password"], input[name="password"], input[id="password"]';

    const emailInput = document.querySelector(emailSelector);
    const passInput = document.querySelector(passSelector);

    if (emailInput && passInput) {
        function setNativeInputValue(input, value) {
            if (!input) return;
            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, "value"
            ).set;
            nativeInputValueSetter.call(input, value);
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('blur', { bubbles: true }));
        }

        setNativeInputValue(emailInput, userEmail);
        setNativeInputValue(passInput, userPassword);

        setTimeout(() => {
            const submitButton = document.querySelector(
                'button[type="submit"], input[type="submit"], button[id*="login"], button[id*="submit"]'
            );
            if (submitButton) {
                submitButton.click();
            } else {
                const form = emailInput.closest('form');
                if (form) form.submit();
            }
        }, 400);
        return "SUBMITTED";
    }
    return "NO_FIELDS";
}
"""

# Firebase Session Storage Helpers
def save_session_to_firebase(session_data: dict):
    """Saves storage_state JSON object to Firebase Realtime Database."""
    try:
        url = f"{FIREBASE_DB_URL.rstrip('/')}/ig_session.json"
        response = requests.put(url, json=session_data, timeout=10)
        if response.status_code == 200:
            APP_STATE["session_saved_to_firebase"] = True
            print("[+] Session successfully saved to Firebase RTDB.")
        else:
            print(f"[!] Failed to save session to Firebase: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"[!] Exception saving session to Firebase: {e}")

def load_session_from_firebase() -> dict | None:
    """Retrieves existing storage_state JSON from Firebase Realtime Database."""
    try:
        url = f"{FIREBASE_DB_URL.rstrip('/')}/ig_session.json"
        response = requests.get(url, timeout=10)
        if response.status_code == 200 and response.json():
            print("[+] Loaded existing session state from Firebase.")
            return response.json()
    except Exception as e:
        print(f"[!] Exception fetching session from Firebase: {e}")
    return None

def init_automation_session():
    """Initializes Playwright, loads session from Firebase if available, and authenticates."""
    if not os.path.exists(PROFILE_DIR):
        os.makedirs(PROFILE_DIR)

    session_state = load_session_from_firebase()

    with sync_playwright() as p:
        browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
        ]

        if session_state:
            # Launch context with restored state from Firebase
            context = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=True,
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                args=browser_args
            )
            # Restore state cookies / storage
            try:
                context.add_cookies(session_state.get("cookies", []))
            except Exception as e:
                print(f"[!] Cookie restoration warning: {e}")
        else:
            context = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=True,
                viewport={"width": 1280, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                args=browser_args
            )

        page = context.pages[0] if context.pages else context.new_page()

        print("[*] Accessing Instagram Inbox...")
        page.goto(INBOX_URL, wait_until="networkidle")
        time.sleep(3)

        if "login" in page.url or page.query_selector('input[name="username"]'):
            print("[!] Performing auto-login...")
            page.goto(LOGIN_URL, wait_until="networkidle")

            for _ in range(15):
                res = page.evaluate(JS_AUTO_LOGIN, {"userEmail": IG_USERNAME, "userPassword": IG_PASSWORD})
                if res == "SUBMITTED":
                    print("[*] Credentials submitted. Waiting for session initialization...")
                    time.sleep(8)
                    break
                time.sleep(1)

            page.goto(INBOX_URL, wait_until="networkidle")
            time.sleep(3)

        APP_STATE["is_authenticated"] = "login" not in page.url
        APP_STATE["active_url"] = page.url
        APP_STATE["last_check_time"] = time.strftime("%Y-%m-%d %H:%M:%S")

        if APP_STATE["is_authenticated"]:
            # Export session state and upload to Firebase
            state = context.storage_state()
            save_session_to_firebase(state)

        context.close()

# FastAPI Lifecycle Manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run session setup on startup
    try:
        init_automation_session()
    except Exception as e:
        APP_STATE["error"] = str(e)
        print(f"[!] Startup automation error: {e}")
    yield

app = FastAPI(title="Instagram Automation Controller", lifespan=lifespan)

# API Endpoints
@app.get("/health")
def health_check():
    """Health check endpoint required for Render blue/green deployments."""
    return {"status": "ok", "service": "ig-controller"}

@app.get("/status")
def status_check():
    """Returns the current state of authentication and Firebase session storage."""
    return {
        "authenticated": APP_STATE["is_authenticated"],
        "active_url": APP_STATE["active_url"],
        "last_check": APP_STATE["last_check_time"],
        "session_in_firebase": APP_STATE["session_saved_to_firebase"],
        "error": APP_STATE["error"]
    }

@app.post("/trigger-login")
def trigger_login(background_tasks: BackgroundTasks):
    """Manually re-trigger login and session export in background."""
    background_tasks.add_task(init_automation_session)
    return {"message": "Authentication refresh initiated in background."}
