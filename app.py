"""Exam server (cloud). Runs without the Mac. Photos of a practice test in -> fast parallel draft answers back + push to phone/watch,
then Opus re-solves every page and pushes ONLY corrections.
Env: ANTHROPIC_API_KEY, EXAM_TOKEN, VAPID_PRIVATE (PEM text), VAPID_PUBLIC, DATA_DIR, MOCK=1 (no API calls, for plumbing tests)."""
import os, re, json, time, asyncio, base64
from pathlib import Path
from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.routing import Route
import uvicorn

HERE = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", "/tmp/examdata")); DATA.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ.get("EXAM_TOKEN", "")
MOCK = os.environ.get("MOCK") == "1"
FAST, SMART = os.environ.get("FAST_MODEL", "claude-sonnet-5-5"), os.environ.get("SMART_MODEL", "claude-opus-5-5")
SUBS = DATA / "subs.json"
MAX_PAGES = 12

DRAFT_SYS = ("You solve a photographed page of a student's PRACTICE test/homework done at home. "
             "Output ONLY one line per question: '<number>. <final answer>' (multiple choice: letter plus a few words; math/chem: value with units; "
             "short response: one short sentence). No explanations, no markdown. Unreadable or cut-off question: '<number>. unreadable'. No questions on page: 'no questions'.")
CHECK_SYS = ("You are the careful checker for a student's practice test. Look at the page image, solve EVERY question yourself from scratch, rigorously "
             "(re-derive math, check units, sig figs, read every answer choice). Then compare with the DRAFT answers. "
             "Output ONLY lines for answers that are WRONG: '<number>. should be <correct answer> (<5-10 word reason>)'. "
             "If every draft answer is right, output exactly: ALL GOOD")


def authed(r) -> bool:
    import secrets
    return bool(TOKEN) and secrets.compare_digest(r.headers.get("x-token", r.query_params.get("k", "")), TOKEN)


def subs() -> list:
    try: return json.loads(SUBS.read_text())
    except Exception: return []


def push(title: str, body: str):
    if MOCK:
        print("[mock push]", title, body, flush=True); return 0
    from pywebpush import webpush, WebPushException
    keep, n = [], 0
    for s in subs():
        try:
            webpush(subscription_info=s, data=json.dumps({"title": title, "body": body[:230]}), vapid_private_key=os.environ["VAPID_PRIVATE"],
                    vapid_claims={"sub": "mailto:exam@example.com"}, ttl=3600)
            keep.append(s); n += 1
        except WebPushException as e:
            if getattr(e.response, "status_code", 0) in (404, 410): continue
            keep.append(s)
        except Exception as e:
            print("push error", e, flush=True); keep.append(s)
    SUBS.write_text(json.dumps(keep)); return n


_client = None
def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.AsyncAnthropic(max_retries=2)
    return _client


def img_block(d: str):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": d.split(",", 1)[1]}}


async def ask(model, system, d, text, thinking=False):
    kw = dict(model=model, system=system, max_tokens=14000 if thinking else 1500,
              messages=[{"role": "user", "content": [img_block(d), {"type": "text", "text": text}]}])
    if thinking: kw["thinking"] = {"type": "enabled", "budget_tokens": 8000}
    r = await client().messages.create(**kw)
    return "".join(b.text for b in r.content if b.type == "text").strip()


async def draft(images, note):
    async def one(d):
        if MOCK: return "1. B (mock)\n2. D (mock)"
        for _ in range(2):
            try:
                r = await asyncio.wait_for(ask(FAST, DRAFT_SYS, d, f"Student note: {note}"), 60)
                if r: return r
            except Exception as e: print("draft err", type(e).__name__, e, flush=True)
        return "couldn't read this page"
    return await asyncio.gather(*[one(d) for d in images])


async def check(images, drafts, note):
    async def one(d, dr):
        if MOCK: return "ALL GOOD"
        try: return await asyncio.wait_for(ask(SMART, CHECK_SYS, d, f"Student note: {note}\nDRAFT:\n{dr}", True), 300)
        except Exception as e: return f"CHECK FAILED ({type(e).__name__})"
    return await asyncio.gather(*[one(d, dr) for d, dr in zip(images, drafts)])


def fmt(drafts):
    return drafts[0] if len(drafts) == 1 else "\n".join(f"p{i+1}: " + d.replace("\n", " | ") for i, d in enumerate(drafts))


async def verify(images, drafts, note):
    res = await check(images, drafts, note)
    bad = [(i + 1, r) for i, r in enumerate(res) if r and not r.upper().startswith("ALL GOOD")]
    with open(DATA / "log.md", "a") as f:
        f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')} ({len(images)} pages)\n{fmt(drafts)}\nCHECK:\n" + "\n".join(res) + "\n")
    if not bad:
        await asyncio.to_thread(push, "Answers checked", "Double-checked everything: all good")
    else:
        await asyncio.to_thread(push, "Fix these answers", " ".join(f"p{i}: " + r.replace("\n", " | ") for i, r in bad))


async def exam(request):
    if not authed(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = await request.json()
    images = [d for d in (b.get("images") or [])[:MAX_PAGES] if isinstance(d, str) and d.startswith("data:image")]
    if not images: return JSONResponse({"error": "no images"}, status_code=400)
    note = (b.get("note") or "")[:300]
    t = time.time()
    drafts = await draft(images, note)
    reply = fmt(drafts)
    await asyncio.to_thread(push, "Answers", reply.replace("\n", " | "))
    asyncio.create_task(verify(images, drafts, note))
    return JSONResponse({"reply": reply, "secs": round(time.time() - t, 1)})


async def sub(request):
    if not authed(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
    s = await request.json()
    cur = [x for x in subs() if x.get("endpoint") != s.get("endpoint")] + [s]
    SUBS.write_text(json.dumps(cur)); return JSONResponse({"ok": True})


async def vapid(request): return JSONResponse({"key": os.environ.get("VAPID_PUBLIC", "")})
async def page(request): return HTMLResponse((HERE / "index.html").read_text())
async def sw(request): return Response((HERE / "sw.js").read_text(), media_type="application/javascript", headers={"service-worker-allowed": "/"})
async def icon(request): return Response((HERE / "icon.png").read_bytes(), media_type="image/png")
async def manifest(request):
    k = request.query_params.get("k", "")
    start = "/?k=" + k if k and authed(request) else "/"
    return JSONResponse({"name": "Exam", "short_name": "Exam", "start_url": start, "id": "/", "display": "standalone", "background_color": "#0b0d10", "theme_color": "#0b0d10",
                         "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]})
async def health(request): return JSONResponse({"ok": True, "mock": MOCK})

app = Starlette(routes=[Route("/", page), Route("/exam", exam, methods=["POST"]), Route("/sub", sub, methods=["POST"]), Route("/vapid", vapid),
                        Route("/sw.js", sw), Route("/icon.png", icon), Route("/manifest.json", manifest), Route("/health", health)])

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
