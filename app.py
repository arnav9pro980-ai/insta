"""
Chat Browser API - FastAPI + Playwright with a persistent Chrome profile.
Logs in only when the saved session has expired.
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from playwright.async_api import async_playwright, BrowserContext, Page
from pydantic import BaseModel, Field

# ---------------- Config (set these in Render env vars) ----------------
USER_EMAIL = os.getenv("USER_EMAIL", "")
USER_PASSWORD = os.getenv("USER_PASSWORD", "")
LOGIN_URL = os.getenv("LOGIN_URL", "https://www.instagram.com/")
REDIRECT_URL = os.getenv("REDIRECT_URL", "https://www.instagram.com/direct/inbox/")
BASE_URL = os.getenv("BASE_URL", "https://www.instagram.com")
INBOX_MARKER = os.getenv("INBOX_MARKER", "/direct/")
PROFILE_DIR = os.getenv("PROFILE_DIR", "/data/chrome_profile")  # Render disk
API_KEY = os.getenv("API_KEY", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

state: dict = {"context": None, "page": None, "last_error": None, "current_thread": None}
lock = asyncio.Lock()

# ---------------- JS snippets (ported from your script) ----------------
JS_HAS_LOGIN = """() => !!(document.querySelector('input[type="password"], input[name="password"]'))"""

JS_FILL_LOGIN = """([email, pass]) => {
  const e = document.querySelector('input[type="email"], input[name="email"], input[id="email"], input[name="username"]');
  const p = document.querySelector('input[type="password"], input[name="password"], input[id="password"]');
  if (!e || !p) return false;
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  for (const [el, v] of [[e, email], [p, pass]]) {
    set.call(el, v);
    ['input','change','blur'].forEach(t => el.dispatchEvent(new Event(t, {bubbles: true})));
  }
  setTimeout(() => {
    const b = document.querySelector('button[type="submit"], input[type="submit"], button[id*="login"], button[class*="login"]');
    if (b) b.click(); else { const f = e.closest('form'); if (f) f.submit(); }
  }, 400);
  return true;
}"""

JS_THREADS = """() => {
  const threads = [];
  document.querySelectorAll('span[title]').forEach(span => {
    const name = span.getAttribute('title').trim();
    if (name && !threads.some(t => t.name === name)) {
      const a = span.closest('a') || span.closest('div[role="button"]');
      threads.push({ name, href: a && a.getAttribute('href') ? a.getAttribute('href') : '' });
    }
  });
  return threads;
}"""

JS_CLICK_THREAD = """(name) => {
  for (const span of document.querySelectorAll('span[title]')) {
    if (span.getAttribute('title').trim() !== name) continue;
    const t = span.closest('a') || span.closest('div[role="button"]') || span.closest('div[tabindex="0"]') || span;
    ['mousedown','mouseup','click'].forEach(ev => t.dispatchEvent(new MouseEvent(ev, {view: window, bubbles: true, cancelable: true})));
    return true;
  }
  return false;
}"""

JS_READ = """(limit) => {
  const out = [];
  document.querySelectorAll('div[role="article"][aria-roledescription="message"]').forEach(article => {
    const te = article.querySelector('span[dir="auto"] div[dir="auto"]') ||
               article.querySelector('div[dir="auto"] span[dir="auto"]') ||
               article.querySelector('span[dir="auto"]');
    if (!te) return;
    const text = te.innerText.trim();
    if (!text) return;
    let row = article;
    for (let i = 0; i < 6; i++) {
      const p = row.parentElement; if (!p) break;
      const s = getComputedStyle(p);
      if (s.display === 'flex' || p.getAttribute('role') === 'row' || p.className.includes('html-div')) row = p;
    }
    let isSender = false;
    const as = getComputedStyle(article), ps = getComputedStyle(article.parentElement || article);
    if (as.alignSelf === 'flex-end' || ps.alignItems === 'flex-end' || ps.justifyContent === 'flex-end') isSender = true;
    if (!isSender && article.getBoundingClientRect().left > innerWidth * 0.45) isSender = true;
    let isReply = false, replyHeader = null, quoted = null;
    row.querySelectorAll('div[role="button"]').forEach(b => {
      if (b.contains(te)) return;
      const bt = b.innerText.trim();
      if (bt && bt !== text && !bt.includes('Reply') && !bt.includes('React')) { isReply = true; quoted = bt.replace(/\\n/g, ' '); }
    });
    if (!isReply) {
      const n = Array.from(row.querySelectorAll('span, div')).find(el => /replied to/i.test(el.textContent));
      if (n) { isReply = true; replyHeader = n.textContent.trim(); }
    }
    out.push({ sender: isSender ? 'YOU' : 'THEM', text, is_reply: isReply, reply_header: replyHeader, quoted_text: quoted });
  });
  return out.slice(-limit);
}"""

JS_SEND = """async (msg) => {
  const ed = document.querySelector('div[contenteditable="true"]');
  if (!ed) return false;
  ed.focus();
  document.execCommand('insertText', false, msg);
  ed.dispatchEvent(new Event('input', {bubbles: true}));
  await new Promise(r => setTimeout(r, 300));
  const btn = document.querySelector('div[aria-label="Send"][role="button"], div[role="button"]:has(svg[aria-label="Send"])');
  if (btn) btn.click();
  else ed.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  return true;
}"""

JS_REPLY_TO = """async (index) => {
  const arts = Array.from(document.querySelectorAll('div[role="article"][aria-roledescription="message"]'));
  const a = arts[arts.length + index];
  if (!a) return false;
  a.scrollIntoView({block: 'center'});
  a.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
  a.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
  await new Promise(r => setTimeout(r, 500));
  let row = a; for (let i = 0; i < 6 && row.parentElement; i++) row = row.parentElement;
  const btn = row.querySelector('[aria-label*="Reply" i]') ||
              Array.from(row.querySelectorAll('svg[aria-label]')).find(s => /reply/i.test(s.getAttribute('aria-label')))?.closest('[role="button"]');
  if (!btn) return false;
  btn.click();
  return true;
}"""


# ---------------- Browser helpers ----------------
async def get_page() -> Page:
    page: Optional[Page] = state["page"]
    if page is None or page.is_closed():
        ctx: BrowserContext = state["context"]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        state["page"] = page
    return page


async def ensure_logged_in(page: Page) -> bool:
    """Log in only if the saved profile has no valid session."""
    if await page.evaluate(JS_HAS_LOGIN):
        if not (USER_EMAIL and USER_PASSWORD):
            raise HTTPException(500, "Login required but USER_EMAIL/USER_PASSWORD not set")
        await page.evaluate(JS_FILL_LOGIN, [USER_EMAIL, USER_PASSWORD])
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(4)
        return True
    return False


async def go_inbox(page: Page):
    if INBOX_MARKER not in page.url:
        await page.goto(REDIRECT_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)
    if await ensure_logged_in(page):
        await page.goto(REDIRECT_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)


async def open_thread(page: Page, name: str):
    await go_inbox(page)
    threads = await page.evaluate(JS_THREADS)
    match = next((t for t in threads if t["name"].lower() == name.lower()), None)
    if not match:
        raise HTTPException(404, f"Thread '{name}' not found. Available: {[t['name'] for t in threads]}")
    if match["href"] and "/direct/t/" in match["href"]:
        await page.goto(BASE_URL + match["href"], wait_until="domcontentloaded")
    else:
        await page.evaluate(JS_CLICK_THREAD, match["name"])
    await asyncio.sleep(2.5)
    state["current_thread"] = match["name"]


# ---------------- App lifecycle ----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=HEADLESS,
        viewport={"width": 1280, "height": 900},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"],
    )
    state["context"] = ctx
    try:
        page = await get_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await go_inbox(page)
    except Exception as e:
        state["last_error"] = str(e)
    yield
    await ctx.close()  # flushes cookies to disk
    await pw.stop()


app = FastAPI(title="Chat Browser API", lifespan=lifespan)


def auth(x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API key")


class SendBody(BaseModel):
    user: str = Field(..., description="Thread name as shown in /users")
    message: str


class ReplyBody(BaseModel):
    user: str
    message: str
    message_index: int = Field(-1, description="-1 = last message, -2 = second last ...")


# ---------------- Endpoints ----------------
@app.get("/health")
async def health():
    ctx = state["context"]
    return {"ok": ctx is not None, "browser_running": ctx is not None}


@app.get("/status", dependencies=[Depends(auth)])
async def status():
    page = await get_page()
    try:
        login_page = await page.evaluate(JS_HAS_LOGIN)
        title = await page.title()
    except Exception:
        login_page, title = None, None
    return {
        "url": page.url,
        "title": title,
        "on_login_page": login_page,
        "logged_in": login_page is False,
        "current_thread": state["current_thread"],
        "last_error": state["last_error"],
        "profile_dir": PROFILE_DIR,
    }


@app.get("/screenshot", dependencies=[Depends(auth)])
async def screenshot():
    from fastapi.responses import Response
    page = await get_page()
    return Response(await page.screenshot(), media_type="image/png")


@app.post("/login", dependencies=[Depends(auth)])
async def login():
    async with lock:
        page = await get_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        did = await ensure_logged_in(page)
        return {"performed_login": did, "url": page.url}


@app.get("/users", dependencies=[Depends(auth)])
async def users():
    async with lock:
        page = await get_page()
        await go_inbox(page)
        return {"users": await page.evaluate(JS_THREADS)}


@app.get("/messages/{user}", dependencies=[Depends(auth)])
async def messages(user: str, limit: int = 10):
    async with lock:
        page = await get_page()
        await open_thread(page, user)
        return {"user": state["current_thread"], "messages": await page.evaluate(JS_READ, limit)}


@app.post("/send", dependencies=[Depends(auth)])
async def send(body: SendBody):
    async with lock:
        page = await get_page()
        await open_thread(page, body.user)
        ok = await page.evaluate(JS_SEND, body.message)
        if not ok:
            raise HTTPException(500, "Message box not found")
        await asyncio.sleep(1)
        return {"sent": True, "user": state["current_thread"], "message": body.message}


@app.post("/reply", dependencies=[Depends(auth)])
async def reply(body: ReplyBody):
    async with lock:
        page = await get_page()
        await open_thread(page, body.user)
        msgs = await page.evaluate(JS_READ, 1000)
        target = msgs[body.message_index] if msgs and -len(msgs) <= body.message_index < 0 else None
        if not await page.evaluate(JS_REPLY_TO, body.message_index):
            raise HTTPException(500, "Could not open reply for that message")
        await asyncio.sleep(0.5)
        if not await page.evaluate(JS_SEND, body.message):
            raise HTTPException(500, "Message box not found")
        await asyncio.sleep(1)
        return {"sent": True, "replied_to": target, "message": body.message}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
