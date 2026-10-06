import base64
import json
import os
import time
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page


INBOX_URL = "https://www.instagram.com/direct/inbox/"
LOGIN_URL = "https://www.instagram.com/accounts/login/"

IG_USERNAME = os.getenv("INSTA_USER", "")
IG_PASSWORD = os.getenv("INSTA_PASSWORD", "")
IG_SESSION_BASE64 = os.getenv("IG_SESSION_BASE64", "")

PORT = int(os.getenv("PORT", "10000"))


JS_EXTRACT_THREADS = """
() => {
    const threads = [];

    const threadLinks = Array.from(
        document.querySelectorAll('a[href*="/direct/t/"]')
    );

    threadLinks.forEach(link => {
        const titleSpan =
            link.querySelector('span[title]') ||
            link.querySelector('span[dir="auto"]');

        const name = titleSpan
            ? (titleSpan.getAttribute('title') || titleSpan.innerText).trim()
            : '';

        const href = link.getAttribute('href');

        if (
            name &&
            href &&
            !threads.some(t => t.name === name || t.href === href)
        ) {
            threads.push({
                name: name,
                href: href
            });
        }
    });

    if (threads.length === 0) {
        const titleSpans = document.querySelectorAll('span[title]');

        titleSpans.forEach(span => {
            const val = span.getAttribute('title');

            if (!val) return;

            const name = val.trim();

            if (name && !threads.some(t => t.name === name)) {
                const anchor = span.closest('a');
                const href = anchor
                    ? anchor.getAttribute('href')
                    : '';

                threads.push({
                    name: name,
                    href: href
                });
            }
        });
    }

    return threads;
}
"""


JS_READ_MSGS = """
(limit) => {
    const messageArticles = document.querySelectorAll(
        'div[role="article"][aria-roledescription="message"]'
    );

    const extractedMessages = [];

    messageArticles.forEach(article => {
        const textElement =
            article.querySelector('span[dir="auto"] div[dir="auto"]') ||
            article.querySelector('div[dir="auto"] span[dir="auto"]') ||
            article.querySelector('span[dir="auto"]');

        if (!textElement) return;

        const text = textElement.innerText.trim();

        if (!text) return;

        let rowContainer = article;

        for (let i = 0; i < 6; i++) {
            if (rowContainer.parentElement) {
                const parent = rowContainer.parentElement;
                const style = window.getComputedStyle(parent);

                if (
                    style.display === 'flex' ||
                    parent.getAttribute('role') === 'row' ||
                    String(parent.className).includes('html-div')
                ) {
                    rowContainer = parent;
                }
            }
        }

        let isSender = false;

        const articleStyle = window.getComputedStyle(article);
        const parentStyle = window.getComputedStyle(
            article.parentElement || article
        );

        if (
            articleStyle.alignSelf === 'flex-end' ||
            parentStyle.alignItems === 'flex-end' ||
            parentStyle.justifyContent === 'flex-end'
        ) {
            isSender = true;
        }

        if (!isSender) {
            const rect = article.getBoundingClientRect();

            if (rect.left > window.innerWidth * 0.45) {
                isSender = true;
            }
        }

        let isReply = false;
        let quotedText = null;

        const replyBtns =
            rowContainer.querySelectorAll('div[role="button"]');

        replyBtns.forEach(btn => {
            if (btn.contains(textElement)) return;

            const btnText = btn.innerText.trim();

            if (
                btnText &&
                btnText !== text &&
                !btnText.includes('Reply') &&
                !btnText.includes('React')
            ) {
                isReply = true;
                quotedText = btnText.replace(/\\n/g, ' ');
            }
        });

        extractedMessages.push({
            sender: isSender ? 'YOU' : 'THEM',
            text: text,
            is_reply: isReply,
            quoted_text: quotedText
        });
    });

    return extractedMessages.slice(-limit);
}
"""


JS_SEND_MSG = """
(textToInsert) => {
    const editor =
        document.querySelector('div[contenteditable="true"]') ||
        document.querySelector('p.xat24cr');

    if (!editor) {
        return false;
    }

    editor.focus();

    document.execCommand(
        'insertText',
        false,
        textToInsert
    );

    editor.dispatchEvent(
        new Event('input', {
            bubbles: true
        })
    );

    setTimeout(() => {
        const sendBtn =
            document.querySelector(
                'div[aria-label="Send"][role="button"]'
            ) ||
            document.querySelector(
                'div[role="button"]:has(svg[aria-label="Send"])'
            );

        if (sendBtn) {
            sendBtn.click();
        } else {
            const enterEvent = new KeyboardEvent(
                'keydown',
                {
                    key: 'Enter',
                    code: 'Enter',
                    keyCode: 13,
                    which: 13,
                    bubbles: true
                }
            );

            editor.dispatchEvent(enterEvent);
        }
    }, 300);

    return true;
}
"""


class SendRequest(BaseModel):
    thread: str
    message: str


class InstagramController:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.lock = threading.RLock()
        self.started = False

    def start(self):
        with self.lock:
            if self.started:
                return

            print("[*] Starting Playwright...")

            self.playwright = sync_playwright().start()

            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]

            self.browser = self.playwright.chromium.launch(
                headless=True,
                args=launch_args
            )

            storage_state = None

            if IG_SESSION_BASE64:
                print("[*] Loading IG_SESSION_BASE64...")

                try:
                    decoded = base64.b64decode(
                        IG_SESSION_BASE64
                    ).decode("utf-8")

                    storage_state = json.loads(decoded)

                    print("[+] Instagram session loaded")

                except Exception as e:
                    print(
                        f"[!] Failed to decode session: {e}"
                    )

            context_args = {
                "viewport": {
                    "width": 1280,
                    "height": 900
                },
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/120.0.0.0 "
                    "Safari/537.36"
                )
            }

            if storage_state:
                context_args["storage_state"] = storage_state

            self.context = self.browser.new_context(
                **context_args
            )

            self.page = self.context.new_page()

            self.page.goto(
                INBOX_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            time.sleep(5)

            if "login" in self.page.url:
                print("[!] Instagram session is not authenticated")

                if IG_USERNAME and IG_PASSWORD:
                    self.login()

                else:
                    raise RuntimeError(
                        "Instagram session expired and "
                        "INSTA_USER/INSTA_PASSWORD are not configured"
                    )

            print(
                f"[+] Instagram ready: {self.page.url}"
            )

            self.started = True

    def login(self):
        print("[*] Logging into Instagram...")

        self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        time.sleep(3)

        username_input = self.page.locator(
            'input[name="username"], '
            'input[type="email"]'
        ).first

        password_input = self.page.locator(
            'input[name="password"], '
            'input[type="password"]'
        ).first

        username_input.fill(IG_USERNAME)
        password_input.fill(IG_PASSWORD)

        password_input.press("Enter")

        print("[*] Waiting for Instagram login...")

        time.sleep(10)

        self.page.goto(
            INBOX_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        time.sleep(5)

        if "login" in self.page.url:
            raise RuntimeError(
                "Instagram login failed or additional verification is required"
            )

        print("[+] Login successful")

    def threads(self):
        with self.lock:
            self.page.goto(
                INBOX_URL,
                wait_until="domcontentloaded",
                timeout=60000
            )

            time.sleep(3)

            return self.page.evaluate(
                JS_EXTRACT_THREADS
            )

    def open_thread(self, thread):
        with self.lock:
            threads = self.page.evaluate(
                JS_EXTRACT_THREADS
            )

            target = None

            for item in threads:
                if (
                    item["name"].lower() == thread.lower()
                    or item["href"] == thread
                ):
                    target = item
                    break

            if not target:
                raise ValueError(
                    f"Thread not found: {thread}"
                )

            href = target["href"]

            if not href:
                raise ValueError(
                    "Thread does not have a usable URL"
                )

            if href.startswith("/"):
                url = (
                    "https://www.instagram.com"
                    + href
                )
            else:
                url = href

            self.page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60000
            )

            time.sleep(3)

            return target

    def messages(self, thread, limit=10):
        with self.lock:
            self.open_thread(thread)

            messages = self.page.evaluate(
                JS_READ_MSGS,
                limit
            )

            return messages

    def send(self, thread, message):
        with self.lock:
            target = self.open_thread(thread)

            result = self.page.evaluate(
                JS_SEND_MSG,
                message
            )

            if not result:
                raise RuntimeError(
                    "Instagram message editor was not found"
                )

            time.sleep(1)

            return {
                "thread": target["name"],
                "message": message,
                "sent": True
            }

    def health(self):
        return {
            "started": self.started,
            "page_url": (
                self.page.url
                if self.page
                else None
            )
        }

    def close(self):
        with self.lock:
            try:
                if self.context:
                    self.context.close()
            except Exception:
                pass

            try:
                if self.browser:
                    self.browser.close()
            except Exception:
                pass

            try:
                if self.playwright:
                    self.playwright.stop()
            except Exception:
                pass


controller = InstagramController()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[*] Starting Instagram API")

    try:
        controller.start()
    except Exception as e:
        print(
            f"[!] Instagram startup failed: {e}"
        )

    yield

    print("[*] Shutting down")

    controller.close()


app = FastAPI(
    title="Instagram DM Playwright API",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
def root():
    return {
        "service": "Instagram DM API",
        "status": "online",
        "endpoints": [
            "/health",
            "/threads",
            "/messages/{thread}",
            "/send"
        ]
    }


@app.get("/health")
def health():
    return controller.health()


@app.get("/threads")
def get_threads():
    try:
        threads = controller.threads()

        return {
            "success": True,
            "count": len(threads),
            "threads": threads
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.get("/messages/{thread}")
def get_messages(
    thread: str,
    limit: int = 10
):
    if limit < 1:
        limit = 1

    if limit > 100:
        limit = 100

    try:
        messages = controller.messages(
            thread,
            limit
        )

        return {
            "success": True,
            "thread": thread,
            "count": len(messages),
            "messages": messages
        }

    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail=str(e)
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.post("/send")
def send_message(data: SendRequest):
    if not data.message.strip():
        raise HTTPException(
            status_code=400,
            detail="Message cannot be empty"
        )

    try:
        result = controller.send(
            data.thread,
            data.message
        )

        return {
            "success": True,
            **result
        }

    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail=str(e)
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT
    )
