import base64
import json
import os
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from playwright.async_api import async_playwright


INBOX_URL = "https://www.instagram.com/direct/inbox/"
LOGIN_URL = "https://www.instagram.com/accounts/login/"

IG_USERNAME = "hiiamdudetntt"
IG_PASSWORD = "ritika123"
IG_SESSION_BASE64 = os.getenv("IG_SESSION_BASE64", "")

PORT = int(os.getenv("PORT", "10000"))




JS_EXTRACT_THREADS = """
() => {
    const threads = [];
    const seen = new Set();

    function addThread(name, href) {
        if (!name || !href) return;

        name = name.trim();

        if (!name) return;

        if (!href.includes("/direct/t/")) {
            return;
        }

        if (seen.has(href)) {
            return;
        }

        seen.add(href);

        threads.push({
            name: name,
            href: href
        });
    }

    const links = Array.from(
        document.querySelectorAll('a[href*="/direct/t/"]')
    );

    for (const link of links) {
        const href = link.getAttribute("href");

        if (!href || !href.includes("/direct/t/")) {
            continue;
        }

        let name = "";

        const titleElements = Array.from(
            link.querySelectorAll("[title]")
        );

        for (const el of titleElements) {
            const title = (
                el.getAttribute("title") || ""
            ).trim();

            if (
                title &&
                title !== "Messages" &&
                title !== "Instagram"
            ) {
                name = title;
                break;
            }
        }

        if (!name) {
            const spans = Array.from(
                link.querySelectorAll("span")
            );

            for (const span of spans) {
                const text = (
                    span.innerText ||
                    span.textContent ||
                    ""
                ).trim();

                if (
                    text &&
                    text.length <= 100 &&
                    text !== "Messages" &&
                    text !== "Instagram"
                ) {
                    name = text;
                    break;
                }
            }
        }

        if (!name) {
            const text = (
                link.innerText ||
                link.textContent ||
                ""
            ).trim();

            if (text) {
                const lines = text
                    .split("\\n")
                    .map(x => x.trim())
                    .filter(Boolean);

                for (const line of lines) {
                    if (
                        line !== "Messages" &&
                        line !== "Instagram"
                    ) {
                        name = line;
                        break;
                    }
                }
            }
        }

        if (name) {
            addThread(name, href);
        }
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
            article.querySelector(
                'span[dir="auto"] div[dir="auto"]'
            ) ||
            article.querySelector(
                'div[dir="auto"] span[dir="auto"]'
            ) ||
            article.querySelector(
                'span[dir="auto"]'
            );

        if (!textElement) {
            return;
        }

        const text = textElement.innerText.trim();

        if (!text) {
            return;
        }

        let rowContainer = article;

        for (let i = 0; i < 6; i++) {
            if (rowContainer.parentElement) {
                const parent =
                    rowContainer.parentElement;

                const style =
                    window.getComputedStyle(parent);

                if (
                    style.display === "flex" ||
                    parent.getAttribute("role") === "row" ||
                    String(parent.className).includes("html-div")
                ) {
                    rowContainer = parent;
                }
            }
        }

        let isSender = false;

        const articleStyle =
            window.getComputedStyle(article);

        const parentStyle =
            window.getComputedStyle(
                article.parentElement || article
            );

        if (
            articleStyle.alignSelf === "flex-end" ||
            parentStyle.alignItems === "flex-end" ||
            parentStyle.justifyContent === "flex-end"
        ) {
            isSender = true;
        }

        if (!isSender) {
            const rect =
                article.getBoundingClientRect();

            if (
                rect.left >
                window.innerWidth * 0.45
            ) {
                isSender = true;
            }
        }

        let isReply = false;
        let quotedText = null;

        const replyBtns =
            rowContainer.querySelectorAll(
                'div[role="button"]'
            );

        replyBtns.forEach(btn => {
            if (btn.contains(textElement)) {
                return;
            }

            const btnText =
                btn.innerText.trim();

            if (
                btnText &&
                btnText !== text &&
                !btnText.includes("Reply") &&
                !btnText.includes("React")
            ) {
                isReply = true;

                quotedText =
                    btnText.replace(
                        /\\n/g,
                        " "
                    );
            }
        });

        extractedMessages.push({
            sender: isSender
                ? "YOU"
                : "THEM",

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
        document.querySelector(
            'div[contenteditable="true"]'
        ) ||
        document.querySelector(
            'p.xat24cr'
        );

    if (!editor) {
        return false;
    }

    editor.focus();

    document.execCommand(
        "insertText",
        false,
        textToInsert
    );

    editor.dispatchEvent(
        new Event("input", {
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
            const enterEvent =
                new KeyboardEvent(
                    "keydown",
                    {
                        key: "Enter",
                        code: "Enter",
                        keyCode: 13,
                        which: 13,
                        bubbles: true
                    }
                );

            editor.dispatchEvent(
                enterEvent
            );
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

        self.lock = asyncio.Lock()

        self.started = False
        self.start_error = None

    async def start(self):

        async with self.lock:

            if self.started:
                return

            print("[*] Starting Playwright...")

            try:

                self.playwright = (
                    await async_playwright().start()
                )

                self.browser = (
                    await self.playwright.chromium.launch(
                        headless=True,
                        args=[
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-blink-features=AutomationControlled"
                        ]
                    )
                )

                storage_state = None

                if IG_SESSION_BASE64:

                    print(
                        "[*] Loading IG_SESSION_BASE64..."
                    )

                    try:

                        decoded = (
                            base64.b64decode(
                                IG_SESSION_BASE64
                            )
                            .decode("utf-8")
                        )

                        storage_state = json.loads(
                            decoded
                        )

                        print(
                            "[+] Instagram session loaded"
                        )

                    except Exception as e:

                        print(
                            "[!] Session decode error:",
                            e
                        )

                context_args = {
                    "viewport": {
                        "width": 1280,
                        "height": 900
                    },

                    "user_agent": (
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/120.0.0.0 "
                        "Safari/537.36"
                    )
                }

                if storage_state:

                    context_args[
                        "storage_state"
                    ] = storage_state

                self.context = (
                    await self.browser.new_context(
                        **context_args
                    )
                )

                self.page = (
                    await self.context.new_page()
                )

                print(
                    "[*] Opening Instagram inbox..."
                )

                await self.page.goto(
                    INBOX_URL,
                    wait_until="domcontentloaded",
                    timeout=60000
                )

                await asyncio.sleep(5)

                print(
                    "[*] Current URL:",
                    self.page.url
                )

                if "login" in self.page.url:

                    print(
                        "[!] Session not authenticated"
                    )

                    if (
                        IG_USERNAME
                        and IG_PASSWORD
                    ):

                        await self.login()

                    else:

                        raise RuntimeError(
                            "Instagram session expired. "
                            "Set INSTA_USER and INSTA_PASSWORD "
                            "or update IG_SESSION_BASE64."
                        )

                self.started = True
                self.start_error = None

                print(
                    "[+] Instagram controller ready"
                )

            except Exception as e:

                self.start_error = str(e)

                print(
                    "[!] Instagram startup failed:",
                    e
                )

    async def login(self):

        print(
            "[*] Opening Instagram login..."
        )

        await self.page.goto(
            LOGIN_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        await asyncio.sleep(3)

        username_input = self.page.locator(
            'input[name="username"], '
            'input[type="email"]'
        ).first

        password_input = self.page.locator(
            'input[name="password"], '
            'input[type="password"]'
        ).first

        await username_input.fill(
            IG_USERNAME
        )

        await password_input.fill(
            IG_PASSWORD
        )

        await password_input.press(
            "Enter"
        )

        print(
            "[*] Waiting for authentication..."
        )

        await asyncio.sleep(10)

        await self.page.goto(
            INBOX_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        await asyncio.sleep(5)

        if "login" in self.page.url:

            raise RuntimeError(
                "Instagram login failed or "
                "requires verification."
            )

        print(
            "[+] Instagram login successful"
        )

    async def extract_threads(self):

        if not self.started:

            raise RuntimeError(
                "Instagram controller is not ready"
            )

        await self.page.goto(
            INBOX_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        await asyncio.sleep(3)

        threads = await self.page.evaluate(
            JS_EXTRACT_THREADS
        )

        return threads

    async def open_thread(self, thread):

        if not self.started:

            raise RuntimeError(
                "Instagram controller is not ready"
            )

        threads = await self.page.evaluate(
            JS_EXTRACT_THREADS
        )

        target = None

        for item in threads:

            if (
                item["name"].lower()
                == thread.lower()
                or item["href"]
                == thread
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

            target_url = (
                "https://www.instagram.com"
                + href
            )

        else:

            target_url = href

        await self.page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        await asyncio.sleep(3)

        return target

    async def read_messages(
        self,
        thread,
        limit
    ):

        async with self.lock:

            await self.open_thread(
                thread
            )

            messages = await self.page.evaluate(
                JS_READ_MSGS,
                limit
            )

            return messages

    async def send_message(
        self,
        thread,
        message
    ):

        async with self.lock:

            target = await self.open_thread(
                thread
            )

            result = await self.page.evaluate(
                JS_SEND_MSG,
                message
            )

            if not result:

                raise RuntimeError(
                    "Instagram message editor "
                    "was not found."
                )

            await asyncio.sleep(1)

            return {
                "thread": target["name"],
                "message": message,
                "sent": True
            }

    async def debug_data(self):

        if not self.page:

            return {
                "started": False,
                "error": self.start_error
            }

        html = await self.page.content()

        return {
            "started": self.started,
            "error": self.start_error,
            "url": self.page.url,
            "title": await self.page.title(),
            "html_length": len(html),
            "html_start": html[:5000]
        }

    async def screenshot(self):

        if not self.page:

            raise RuntimeError(
                "Browser page does not exist"
            )

        return await self.page.screenshot(
            full_page=False
        )

    def health(self):

        return {
            "started": self.started,
            "error": self.start_error,
            "page_url": (
                self.page.url
                if self.page
                else None
            )
        }

    async def close(self):

        try:

            if self.context:
                await self.context.close()

        except Exception:
            pass

        try:

            if self.browser:
                await self.browser.close()

        except Exception:
            pass

        try:

            if self.playwright:
                await self.playwright.stop()

        except Exception:
            pass


controller = InstagramController()


@asynccontextmanager
async def lifespan(app):

    print(
        "[*] Starting Instagram API"
    )

    await controller.start()

    yield

    print(
        "[*] Shutting down Instagram API"
    )

    await controller.close()


app = FastAPI(
    title="Instagram DM API",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/")
async def root():

    return {
        "service": "Instagram DM API",
        "status": "online",
        "instagram": controller.health(),

        "endpoints": {
            "health": "GET /health",
            "threads": "GET /threads",
            "messages": "GET /messages/{thread}?limit=20",
            "send": "POST /send",
            "debug": "GET /debug",
            "screenshot": "GET /screenshot"
        }
    }


@app.head("/")
async def head_root():

    return


@app.get("/health")
async def health():

    return {
        "api": "online",
        "instagram": controller.health()
    }


@app.get("/threads")
async def threads():

    try:

        result = await controller.extract_threads()

        return {
            "success": True,
            "count": len(result),
            "threads": result
        }

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


@app.get("/messages/{thread}")
async def messages(
    thread: str,
    limit: int = 10
):

    if limit < 1:
        limit = 1

    if limit > 100:
        limit = 100

    try:

        result = await controller.read_messages(
            thread,
            limit
        )

        return {
            "success": True,
            "thread": thread,
            "count": len(result),
            "messages": result
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
async def send(
    data: SendRequest
):

    if not data.thread.strip():

        raise HTTPException(
            status_code=400,
            detail="Thread cannot be empty"
        )

    if not data.message.strip():

        raise HTTPException(
            status_code=400,
            detail="Message cannot be empty"
        )

    try:

        result = await controller.send_message(
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


@app.get("/debug")
async def debug():

    return await controller.debug_data()


@app.get("/screenshot")
async def screenshot():

    try:

        image = await controller.screenshot()

        return Response(
            content=image,
            media_type="image/png"
        )

    except Exception as e:

        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        reload=False
    )
