import asyncio
import os
from typing import Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright, BrowserContext, Page

# --- Configuration ---
USER_EMAIL = os.getenv("INSTA_USER", "hiiamdudetntt")
USER_PASSWORD = os.getenv("INSTA_PASS", "ritika123")
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_data_dir")
LOGIN_URL = "https://www.instagram.com/accounts/login/"
INBOX_URL = "https://www.instagram.com/direct/inbox/"

app = FastAPI(title="Instagram Direct API Engine", version="1.0.0")

# Global Playwright State
playwright_instance = None
context: Optional[BrowserContext] = None
page: Optional[Page] = None
browser_lock = asyncio.Lock()


# --- Pydantic Request Models ---
class SendMessageRequest(BaseModel):
    thread_url: str  # e.g., "https://www.instagram.com/direct/t/123456789/" or "/direct/t/123456789/"
    message: str

class ReplyMessageRequest(BaseModel):
    thread_url: str
    reply_to_text: str  # Search text to double-click/reply to
    message: str


# --- Lifecycle Hooks ---
@app.on_event("startup")
async def startup_event():
    global playwright_instance, context, page
    os.makedirs(PROFILE_DIR, exist_ok=True)
    
    playwright_instance = await async_playwright().start()
    
    # Launch Chromium with persistent context to retain login session
    context = await playwright_instance.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    
    page = await context.new_page()
    await ensure_logged_in()


@app.on_event("shutdown")
async def shutdown_event():
    global playwright_instance, context
    if context:
        await context.close()
    if playwright_instance:
        await playwright_instance.stop()


# --- Helper Functions ---
async def ensure_logged_in():
    """Navigates to Instagram Direct, logging in if needed."""
    global page
    async with browser_lock:
        await page.goto(INBOX_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Check if redirected to login page
        if "accounts/login" in page.url or await page.query_selector('input[name="username"]'):
            print("[*] Active session not detected. Logging in...")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2)

            await page.fill('input[name="username"]', USER_EMAIL)
            await page.fill('input[name="password"]', USER_PASSWORD)
            
            submit_btn = await page.query_selector('button[type="submit"]')
            if submit_btn:
                await submit_btn.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(5)

            # Navigate to direct inbox after login
            await page.goto(INBOX_URL, wait_until="domcontentloaded")
            await asyncio.sleep(3)
        else:
            print("[*] Session restored from persistent profile successfully.")


# --- API Endpoints ---

@app.get("/health")
async def health_check():
    """Health check endpoint for Render monitoring."""
    return {"status": "ok", "service": "instagram-api"}


@app.get("/status")
async def status_check():
    """Returns engine state, active URL, and page title."""
    if not page:
        raise HTTPException(status_code=503, detail="Browser instance uninitialized")
    
    title = await page.title()
    url = page.url
    return {
        "status": "online",
        "current_url": url,
        "page_title": title,
        "logged_in": "/direct/" in url or "inbox" in url
    }


@app.get("/threads")
async def get_chat_threads():
    """Extracts available active chat threads from inbox UI."""
    async with browser_lock:
        if "/direct/inbox" not in page.url:
            await page.goto(INBOX_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2)

        js_extract_threads = """
        () => {
            const titleSpans = document.querySelectorAll('span[title]');
            const threads = [];
            titleSpans.forEach(span => {
                const val = span.getAttribute('title').trim();
                if (val && !threads.some(t => t.name === val)) {
                    const anchor = span.closest('a') || span.closest('div[role="button"]');
                    const href = anchor ? anchor.getAttribute('href') : '';
                    threads.push({ name: val, href: href });
                }
            });
            return threads;
        }
        """
        threads = await page.evaluate(js_extract_threads)
        return {"threads_count": len(threads), "threads": threads}


@app.get("/messages")
async def read_messages(thread_url: str, limit: int = 10):
    """Scrapes recent messages from a thread URL (including reply context)."""
    async with browser_lock:
        full_url = thread_url if thread_url.startswith("http") else f"https://www.instagram.com{thread_url}"
        if page.url != full_url:
            await page.goto(full_url, wait_until="domcontentloaded")
            await asyncio.sleep(3)

        js_read_msgs = f"""
        () => {{
            const messageArticles = document.querySelectorAll('div[role="article"][aria-roledescription="message"]');
            const extracted = [];

            messageArticles.forEach(article => {{
                const textElement = article.querySelector('span[dir="auto"] div[dir="auto"]') || 
                                    article.querySelector('div[dir="auto"] span[dir="auto"]') ||
                                    article.querySelector('span[dir="auto"]');
                if (!textElement) return;

                const text = textElement.innerText.trim();
                if (!text) return;

                const rect = article.getBoundingClientRect();
                const isSender = rect.left > (window.innerWidth * 0.45);

                let rowContainer = article;
                for (let i = 0; i < 5; i++) {{
                    if (rowContainer.parentElement) rowContainer = rowContainer.parentElement;
                }}

                let isReply = false;
                let quotedText = null;

                const replyBtns = rowContainer.querySelectorAll('div[role="button"]');
                replyBtns.forEach(btn => {{
                    if (btn.contains(textElement)) return;
                    const btnText = btn.innerText.trim();
                    if (btnText && btnText !== text && !btnText.includes('Reply') && !btnText.includes('React')) {{
                        isReply = true;
                        quotedText = btnText.replace(/\\n/g, ' ');
                    }}
                }});

                extracted.push({{
                    sender: isSender ? 'YOU' : 'THEM',
                    text: text,
                    is_reply: isReply,
                    quoted_text: quotedText
                }});
            }});

            return extracted.slice(-{limit});
        }}
        """
        messages = await page.evaluate(js_read_msgs)
        return {"thread_url": full_url, "messages": messages}


@app.post("/send")
async def send_message(payload: SendMessageRequest):
    """Sends a direct message to a given thread."""
    async with browser_lock:
        full_url = payload.thread_url if payload.thread_url.startswith("http") else f"https://www.instagram.com{payload.thread_url}"
        if page.url != full_url:
            await page.goto(full_url, wait_until="domcontentloaded")
            await asyncio.sleep(3)

        editor = await page.query_selector('div[contenteditable="true"]')
        if not editor:
            raise HTTPException(status_code=400, detail="Chat input box not found")

        await editor.focus()
        await page.keyboard.insert_text(payload.message)
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

        return {"status": "success", "sent_message": payload.message}


@app.post("/reply")
async def reply_to_message(payload: ReplyMessageRequest):
    """Replies directly to a target message by triggering Instagram's reply feature."""
    async with browser_lock:
        full_url = payload.thread_url if payload.thread_url.startswith("http") else f"https://www.instagram.com{payload.thread_url}"
        if page.url != full_url:
            await page.goto(full_url, wait_until="domcontentloaded")
            await asyncio.sleep(3)

        # Double click target message to trigger Instagram inline reply focus
        js_trigger_reply = f"""
        (targetText) => {{
            const articles = document.querySelectorAll('div[role="article"][aria-roledescription="message"]');
            for (let art of articles) {{
                if (art.innerText.includes(targetText)) {{
                    const dblClickEvent = new MouseEvent('dblclick', {{ bubbles: true, cancelable: true, view: window }});
                    art.dispatchEvent(dblClickEvent);
                    return true;
                }}
            }}
            return false;
        }}
        """
        triggered = await page.evaluate(js_trigger_reply, payload.reply_to_text)
        if not triggered:
            raise HTTPException(status_code=404, detail=f"Target message '{payload.reply_to_text}' not found in DOM")

        await asyncio.sleep(0.5)

        editor = await page.query_selector('div[contenteditable="true"]')
        if not editor:
            raise HTTPException(status_code=400, detail="Chat input box not found")

        await editor.focus()
        await page.keyboard.insert_text(payload.message)
        await asyncio.sleep(0.3)
        await page.keyboard.press("Enter")

        return {"status": "success", "replied_to": payload.reply_to_text, "message": payload.message}
