import base64
import json
import os
import threading
import time
from fastapi import FastAPI
import requests
import uvicorn
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------
# CONFIGURATION & FIREBASE
# ---------------------------------------------------------
FIREBASE_DB_URL = "https://launcher-c813d-default-rtdb.europe-west1.firebasedatabase.app"
INBOX_URL = "https://www.instagram.com/direct/inbox/"
LOGIN_URL = "https://www.instagram.com/accounts/login/"

# Read credentials and session base64 from environment variables for safety
USERNAME = os.environ.get("IG_USERNAME", "hiiamdudetntt")
PASSWORD = os.environ.get("IG_PASSWORD", "ritika123")
SESSION_BASE64 = os.environ.get("IG_SESSION_BASE64", "")

# App State Tracker for /status endpoint
bot_status = {
    "authenticated": False,
    "last_sync": None,
    "active_thread": None,
    "error": None
}

# ---------------------------------------------------------
# JAVASCRIPT INJECTION SCRIPTS
# ---------------------------------------------------------
JS_AUTO_LOGIN = """
(args) => {
    const { userEmail, userPassword } = args;
    const emailInput = document.querySelector('input[type="email"], input[name="email"], input[name="username"]');
    const passInput = document.querySelector('input[type="password"], input[name="password"]');

    if (emailInput && passInput) {
        function setNativeInputValue(input, value) {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
            setter.call(input, value);
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('blur', { bubbles: true }));
        }

        setNativeInputValue(emailInput, userEmail);
        setNativeInputValue(passInput, userPassword);

        setTimeout(() => {
            const submitBtn = document.querySelector('button[type="submit"], input[type="submit"]');
            if (submitBtn) submitBtn.click();
            else {
                const form = emailInput.closest('form');
                if (form) form.submit();
            }
        }, 400);
        return "SUBMITTED";
    }
    return "NO_FIELDS";
}
"""

JS_EXTRACT_THREADS = """
() => {
    const threads = [];
    const threadLinks = Array.from(document.querySelectorAll('a[href*="/direct/t/"]'));
    
    threadLinks.forEach(link => {
        const titleSpan = link.querySelector('span[title]') || link.querySelector('span[dir="auto"]');
        const name = titleSpan ? (titleSpan.getAttribute('title') || titleSpan.innerText).trim() : '';
        const href = link.getAttribute('href');
        const threadId = href.split('/direct/t/')[1]?.replace('/', '') || '';
        
        if (name && href && !threads.some(t => t.id === threadId)) {
            threads.push({ id: threadId, name: name, href: href });
        }
    });
    return threads;
}
"""

JS_READ_MSGS = """
(limit) => {
    const messageArticles = document.querySelectorAll('div[role="article"][aria-roledescription="message"]');
    const extractedMessages = [];

    messageArticles.forEach(article => {
        const textElement = article.querySelector('span[dir="auto"] div[dir="auto"]') || 
                            article.querySelector('div[dir="auto"] span[dir="auto"]') ||
                            article.querySelector('span[dir="auto"]');
                            
        if (!textElement) return;
        const text = textElement.innerText.trim();
        if (!text) return;

        let isSender = false;
        const articleStyle = window.getComputedStyle(article);
        const parentStyle = window.getComputedStyle(article.parentElement || article);
        
        if (articleStyle.alignSelf === 'flex-end' || parentStyle.alignItems === 'flex-end' || parentStyle.justifyContent === 'flex-end') {
            isSender = true;
        }

        extractedMessages.push({
            sender: isSender ? 'YOU' : 'THEM',
            text: text,
            timestamp: Date.now()
        });
    });

    return extractedMessages.slice(-limit);
}
"""

# ---------------------------------------------------------
# FIREBASE HELPERS
# ---------------------------------------------------------
def save_threads_to_firebase(threads):
    """Syncs retrieved thread list and IDs to Firebase Realtime Database."""
    try:
        url = f"{FIREBASE_DB_URL}/instagram/threads.json"
        data = {t['id']: {"name": t['name'], "href": t['href'], "last_updated": time.time()} for t in threads if t['id']}
        requests.patch(url, json=data, timeout=10)
    except Exception as e:
        print(f"[!] Firebase Thread Save Error: {e}")

def save_messages_to_firebase(thread_id, messages):
    """Saves extracted chat messages under the specific thread ID in Firebase."""
    try:
        url = f"{FIREBASE_DB_URL}/instagram/messages/{thread_id}.json"
        requests.put(url, json=messages, timeout=10)
    except Exception as e:
        print(f"[!] Firebase Message Save Error: {e}")

def save_session_to_firebase(context):
    """Backs up browser storage state to Firebase RTDB for persistent restarts."""
    try:
        state = context.storage_state()
        encoded = base64.b64encode(json.dumps(state).encode("utf-8")).decode("utf-8")
        url = f"{FIREBASE_DB_URL}/instagram/session.json"
        requests.put(url, json={"base64_session": encoded, "updated_at": time.time()}, timeout=10)
        print("[+] Session state saved to Firebase!")
    except Exception as e:
        print(f"[!] Failed to back up session to Firebase: {e}")

# ---------------------------------------------------------
# PLAYWRIGHT AUTOMATION ENGINE
# ---------------------------------------------------------
def run_playwright_bot():
    global bot_status
    print("[*] Starting Playwright Automation Core...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        )

        # Restore session state from environment variable or memory
        context_options = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        if SESSION_BASE64:
            try:
                decoded = json.loads(base64.b64decode(SESSION_BASE64).decode("utf-8"))
                context = browser.new_context(storage_state=decoded, **context_options)
                print("[+] Loaded session from IG_SESSION_BASE64 env var.")
            except Exception as e:
                print(f"[!] Invalid SESSION_BASE64, creating new context: {e}")
                context = browser.new_context(**context_options)
        else:
            context = browser.new_context(**context_options)

        page = context.new_page()

        # Login flow
        print("[*] Navigating to Instagram Inbox...")
        page.goto(INBOX_URL, wait_until="networkidle")
        time.sleep(3)

        if "login" in page.url or page.query_selector('input[name="username"]'):
            print("[!] Performing automated login...")
            page.goto(LOGIN_URL, wait_until="networkidle")
            for _ in range(15):
                res = page.evaluate(JS_AUTO_LOGIN, {"userEmail": USERNAME, "userPassword": PASSWORD})
                if res == "SUBMITTED":
                    print("[*] Credentials submitted. Awaiting auth...")
                    time.sleep(8)
                    break
                time.sleep(1)

            page.goto(INBOX_URL, wait_until="networkidle")
            time.sleep(3)

        bot_status["authenticated"] = True
        save_session_to_firebase(context)

        # Continuous Sync Daemon Loop
        while True:
            try:
                # 1. Fetch thread sidebar
                threads = page.evaluate(JS_EXTRACT_THREADS)
                if threads:
                    save_threads_to_firebase(threads)
                    bot_status["last_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")

                    # 2. Iterate threads to pull messages into Firebase
                    for t in threads[:3]: # Poll top 3 active threads
                        if t.get("href") and t.get("id"):
                            target_url = f"https://www.instagram.com{t['href']}" if t['href'].startswith('/') else t['href']
                            bot_status["active_thread"] = t["name"]
                            
                            page.goto(target_url, wait_until="domcontentloaded")
                            time.sleep(3)
                            
                            msgs = page.evaluate(JS_READ_MSGS, 15)
                            if msgs:
                                save_messages_to_firebase(t["id"], msgs)

                # Return to main inbox before sleep cycle
                page.goto(INBOX_URL, wait_until="domcontentloaded")
                time.sleep(20) # Poll interval
            except Exception as loop_err:
                print(f"[!] Engine loop error: {loop_err}")
                bot_status["error"] = str(loop_err)
                time.sleep(10)

# ---------------------------------------------------------
# FASTAPI SERVER & ENDPOINTS
# ---------------------------------------------------------
app = FastAPI(title="Instagram Service Controller")

@app.get("/health")
def health_check():
    """Render health check endpoint."""
    return {"status": "ok", "timestamp": time.time()}

@app.get("/status")
def get_status():
    """Bot monitoring & authentication status endpoint."""
    return {
        "bot_engine": "running",
        "authenticated": bot_status["authenticated"],
        "last_sync": bot_status["last_sync"],
        "active_thread": bot_status["active_thread"],
        "last_error": bot_status["error"]
    }

# Start Playwright daemon thread upon FastAPI startup
@app.on_event("startup")
def start_background_bot():
    bot_thread = threading.Thread(target=run_playwright_bot, daemon=True)
    bot_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)
