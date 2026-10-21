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


PUSHLOG = []
SMSLOG = []


def mail(subject: str, body: str):
    """Email the full answer to Karthik (Mail on the watch shows the whole thing). Env: SMTP_USER, SMTP_PASS (Gmail app password), MAIL_TO (default = SMTP_USER)."""
    u, p = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    if MOCK or not (u and p): return
    try:
        import smtplib
        from email.message import EmailMessage
        m = EmailMessage(); m["From"] = u; m["To"] = os.environ.get("MAIL_TO", u); m["Subject"] = subject; m.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=25) as s:
            s.login(u, p.replace(" ", "")); s.send_message(m)
        SMSLOG.append({"t": time.strftime("%H:%M:%S"), "mail": True})
    except Exception as e:
        SMSLOG.append({"t": time.strftime("%H:%M:%S"), "mail": False, "err": f"{type(e).__name__} {str(e)[:120]}"})
    del SMSLOG[:-10]


def sms(body: str):
    """Text via email-to-SMS gateway (T-Mobile). Needs env SMTP_USER, SMTP_PASS (Gmail app password), SMS_TO (number@tmomail.net)."""
    u, p, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ.get("SMS_TO")
    if MOCK or not (u and p and to): return
    try:
        import smtplib
        from email.message import EmailMessage
        m = EmailMessage(); m["From"] = u; m["To"] = to; m.set_content(body[:300])
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(u, p.replace(" ", "")); s.send_message(m)
        SMSLOG.append({"t": time.strftime("%H:%M:%S"), "ok": True})
    except Exception as e:
        SMSLOG.append({"t": time.strftime("%H:%M:%S"), "ok": False, "err": f"{type(e).__name__} {str(e)[:120]}"})
    del SMSLOG[:-10]


def push(title: str, body: str):
    t0 = time.time()
    try:
        return _push(title, body, t0)
    finally:
        if PUSHLOG: PUSHLOG[-1]["secs"] = round(time.time() - t0, 1)


def _push(title: str, body: str, t0=0):
    if MOCK:
        print("[mock push]", title, body, flush=True); return 0
    from pywebpush import webpush, WebPushException
    keep, n, errs = [], 0, []
    for s in subs():
        try:
            webpush(subscription_info=s, data=json.dumps({"title": title, "body": body[:1800]}), vapid_private_key=os.environ["VAPID_PRIVATE"],
                    vapid_claims={"sub": "mailto:exam@example.com"}, ttl=3600)
            keep.append(s); n += 1
        except WebPushException as e:
            errs.append(f"{getattr(e.response, 'status_code', '?')} {str(e)[:160]}")
            if getattr(e.response, "status_code", 0) in (404, 410): continue
            keep.append(s)
        except Exception as e:
            errs.append(f"{type(e).__name__} {str(e)[:160]}"); print("push error", e, flush=True); keep.append(s)
    # also push through the Jarvis app's channel (already mirrors to the watch)
    try:
        js = json.loads(os.environ.get("JARVIS_SUBS", "[]")); jk = os.environ.get("JARVIS_VAPID")
        for s in js if jk else []:
            try:
                webpush(subscription_info=s, data=json.dumps({"title": title, "body": body[:1800], "url": "/"}), vapid_private_key=jk,
                        vapid_claims={"sub": "mailto:jarvis@example.com"}, ttl=3600)
                n += 1
            except Exception as e:
                errs.append("jarvis " + type(e).__name__ + " " + str(e)[:120])
    except Exception as e:
        errs.append("jarvis cfg " + str(e)[:80])
    SUBS.write_text(json.dumps(keep))
    PUSHLOG.append({"t": time.strftime("%H:%M:%S"), "title": title, "subs": len(keep) + len([1 for _ in errs if "404" in _ or "410" in _]), "sent": n, "errs": errs})
    del PUSHLOG[:-10]
    return n


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
    if thinking:
        kw["thinking"] = {"type": "adaptive"}
        kw["extra_body"] = {"output_config": {"effort": "high"}}
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
    return drafts[0] if len(drafts) == 1 else "\n\n".join(f"PAGE {i+1}\n" + d for i, d in enumerate(drafts))


async def verify(images, drafts, note):
    res = await check(images, drafts, note)
    failed = [r for r in res if r.startswith("CHECK FAILED")]
    bad = [(i + 1, r) for i, r in enumerate(res) if r and not r.upper().startswith("ALL GOOD") and not r.startswith("CHECK FAILED")]
    if failed:
        await asyncio.to_thread(push, "Check didn't run", "Double-check failed on %d page(s), verify those answers yourself" % len(failed))
    if failed and not bad:
        return
    with open(DATA / "log.md", "a") as f:
        f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')} ({len(images)} pages)\n{fmt(drafts)}\nCHECK:\n" + "\n".join(res) + "\n")
    if not bad:
        await asyncio.to_thread(push, "Answers checked", "Double-checked everything: all good")
    else:
        fixes = "\n".join(f"PAGE {i}: " + r for i, r in bad)
        await asyncio.to_thread(push, "Fix these answers", fixes)
        await asyncio.to_thread(sms, "FIX: " + fixes)


ESSAY_SYS = ("You are an expert AP teacher writing a MODEL response to a student's PRACTICE question, for them to study from. Read every page image (prompt, documents, tables, graphs). "
             "APUSH/AP World/Euro LEQ or DBQ: use EXACTLY these labeled sections and nothing else: "
             "Intro (2-3 sentences of contextualization, then a one-sentence thesis with a clear line of reasoning), "
             "Body 1 (topic sentence, 2 specific pieces of evidence, explain how they support the thesis; for a DBQ use 2-3 documents with sourcing/HIPP on at least one), "
             "Body 2 (same pattern, different category of evidence), "
             "Complexity (2-3 sentences: a counterargument, continuity vs change, or a connection to another period), "
             "Conclusion (one sentence). Total 400-500 words. "
             "SAQ: parts A, B, C, 1-3 sentences each. AP Statistics FRQ: every part with conditions, the procedure, formula and calculation, and the conclusion in context. "
             "Other science/math FRQ: every part with steps and units. AP Lang/Lit or other extended response: thesis-driven, about 400-500 words. "
             "Plain text only: no markdown symbols or bullets, short paragraphs separated by blank lines, section labels on their own line.")
ESSAYS = []


def chunks(text: str, n: int = 2200) -> list:
    out, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > n:
            cut = para.rfind(". ", 0, n) + 1 or n
            if cur: out.append(cur); cur = ""
            out.append(para[:cut].strip()); para = para[cut:].strip()
        if len(cur) + len(para) + 2 > n and cur: out.append(cur); cur = para
        else: cur = (cur + "\n\n" + para) if cur else para
    if cur: out.append(cur)
    return out


async def essay_job(images, note, jid):
    try:
        if MOCK: parts_text = "Thesis: mock essay.\n\nBody: mock."
        else:
            content = [img_block(d) for d in images] + [{"type": "text", "text": "Student note: " + note + "\nWrite the model response."}]
            r = await asyncio.wait_for(client().messages.create(model=SMART, max_tokens=7000, system=ESSAY_SYS, thinking={"type": "adaptive"},
                                       extra_body={"output_config": {"effort": "medium"}}, messages=[{"role": "user", "content": content}]), 420)
            parts_text = "".join(b.text for b in r.content if b.type == "text").strip()
    except Exception as e:
        parts_text = "ERROR: couldn't write the answer (%s). Send it again." % type(e).__name__
    ESSAYS.append({"id": jid, "t": time.strftime("%H:%M"), "text": parts_text}); del ESSAYS[:-5]
    with open(DATA / "log.md", "a") as f: f.write(f"\n## ESSAY {time.strftime('%Y-%m-%d %H:%M')}\n{parts_text}\n")
    asyncio.get_running_loop().run_in_executor(None, mail, "Model answer " + time.strftime("%H:%M"), parts_text)
    ps = chunks(parts_text)
    for i, p in enumerate(ps, 1):
        await asyncio.to_thread(push, f"Essay {i}/{len(ps)}", p)
        await asyncio.sleep(1.2)


async def latest(request):
    if not authed(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"essay": ESSAYS[-1] if ESSAYS else None})


async def exam(request):
    if not authed(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = await request.json()
    images = [d for d in (b.get("images") or [])[:MAX_PAGES] if isinstance(d, str) and d.startswith("data:image")]
    if not images: return JSONResponse({"error": "no images"}, status_code=400)
    note = (b.get("note") or "")[:300]
    s = b.get("sub")
    if isinstance(s, dict) and s.get("endpoint"):          # phone sends its push address with every request: no server-side registration needed
        SUBS.write_text(json.dumps([x for x in subs() if x.get("endpoint") != s["endpoint"]] + [s]))
    if b.get("mode") == "essay":
        jid = str(int(time.time() * 1000))
        asyncio.create_task(essay_job(images, note, jid))
        return JSONResponse({"reply": "Writing the full answer. It arrives on your watch in numbered parts in about a minute, and shows here too.", "id": jid, "secs": 0})
    t = time.time()
    drafts = await draft(images, note)
    reply = fmt(drafts)
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, push, "Answers", reply)   # background: never make the screen wait on Apple
    loop.run_in_executor(None, sms, "Answers: " + reply)
    asyncio.create_task(verify(images, drafts, note))
    return JSONResponse({"reply": reply, "secs": round(time.time() - t, 1)})


SUBTRY = []


async def sub(request):
    SUBTRY.append({"t": time.strftime("%H:%M:%S"), "has_key": bool(request.headers.get("x-token")), "ok": authed(request)}); del SUBTRY[:-10]
    if not authed(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
    s = await request.json()
    print("sub registered", s.get("endpoint", "")[:40], flush=True)
    cur = [x for x in subs() if x.get("endpoint") != s.get("endpoint")] + [s]
    SUBS.write_text(json.dumps(cur)); return JSONResponse({"ok": True})


async def debug(request):
    if not authed(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"subs": len(subs()), "endpoints": [s.get("endpoint", "")[:50] for s in subs()], "pushlog": PUSHLOG, "subtry": SUBTRY, "smslog": SMSLOG})


async def vapid(request): return JSONResponse({"key": os.environ.get("VAPID_PUBLIC", "")})
async def page(request):
    h = (HERE / "index.html").read_text()
    k = request.query_params.get("k", "")
    if k and authed(request):
        h = h.replace('href="/manifest.json"', 'href="/manifest.json?k=' + k + '"')
        h = h.replace("const K=localStorage.k||'';", "const K=localStorage.k||'';")
    return HTMLResponse(h, headers={"cache-control": "no-store"})
async def sw(request): return Response((HERE / "sw.js").read_text(), media_type="application/javascript", headers={"service-worker-allowed": "/"})
async def icon(request): return Response((HERE / "icon.png").read_bytes(), media_type="image/png")
async def manifest(request):
    k = request.query_params.get("k", "")
    start = "/?k=" + k if k and authed(request) else "/"
    return JSONResponse({"name": "Exam", "short_name": "Exam", "start_url": start, "id": "/", "display": "standalone", "background_color": "#0b0d10", "theme_color": "#0b0d10",
                         "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]})
_warm = {"t": 0}


async def warm(request):
    """Opening the app calls this: loads the SDK, opens connections to both models and imports push, so the first real send is fast."""
    if not authed(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not MOCK and time.time() - _warm["t"] > 240:
        _warm["t"] = time.time()
        async def ping(model):
            try: await client().messages.create(model=model, max_tokens=1, messages=[{"role": "user", "content": "hi"}])
            except Exception as e: print("warm err", model, type(e).__name__, flush=True)
        await asyncio.gather(ping(FAST), ping(SMART), asyncio.to_thread(lambda: __import__("pywebpush")))
    return JSONResponse({"ok": True})


async def health(request): return JSONResponse({"ok": True, "mock": MOCK})

app = Starlette(routes=[Route("/", page), Route("/exam", exam, methods=["POST"]), Route("/sub", sub, methods=["POST"]), Route("/vapid", vapid), Route("/debug", debug), Route("/latest", latest),
                        Route("/sw.js", sw), Route("/icon.png", icon), Route("/manifest.json", manifest), Route("/health", health), Route("/warm", warm)])

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
