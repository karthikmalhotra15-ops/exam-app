"""Cloud Jarvis: runs on an always-on server so phone/watch chat works with the Mac off.
Chat (memory-aware), reminders with push, queue of Mac-only tasks for when the Mac is back. No Mac control here.
Env: ANTHROPIC_API_KEY, JARVIS_TOKEN, DATA_DIR, PORT, MOCK=1 (no API calls), VAPID_PRIVATE/VAPID_PUBLIC (optional push), MONTHLY_CAP_USD (default 8)."""
import os, re, json, time, asyncio, secrets, contextlib
from pathlib import Path
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.routing import Route

HERE = Path(__file__).parent
DATA = Path(os.environ.get("CJ_DATA_DIR", "/tmp/cjdata")); DATA.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ.get("JARVIS_TOKEN") or os.environ.get("EXAM_TOKEN", "")
MOCK = os.environ.get("MOCK") == "1"
MODEL = os.environ.get("FAST_MODEL", "claude-sonnet-5-5")
CAP = float(os.environ.get("MONTHLY_CAP_USD", "8"))
PRICE_IN, PRICE_OUT = 3 / 1e6, 15 / 1e6     # rough $/token, only used for the spend cap
FAILS: dict = {}
MEMFILE, HIST, QUEUE, REM, USAGE = (DATA / n for n in ("memory.md", "history.json", "mac_queue.json", "reminders.json", "usage.json"))
NOTE = ("[CLOUD MODE: you are Jarvis running on a server, not on Karthik's laptop. You can't touch his Mac, Chrome, Classroom, files or lights. "
        "If he asks for something that needs the Mac, say you queued it for when the Mac is on and put a line starting 'QUEUE:' with the task. "
        "For a reminder put a line 'REMIND: HH:MM | text' (24h, today) or 'REMIND: YYYY-MM-DD HH:MM | text'. Never send/delete/post/spend. "
        "Reply like he texts: casual, 1-3 short lines, no markdown.] ")


WATCH_NOTE = ("[WATCH MODE: you are Jarvis answering Karthik's Apple Watch / email from a server (Mac is off). His questions are usually school questions. "
              "OUTPUT FORMAT, strictly: only math lines, no sentences, no verbs like 'add/divide/subtract', no 'check', no restating the question. Example for 'solve 2x+3=11':\n2x = 8\nx = 4\nANSWER: x = 4\nFor multiple choice give the work lines then 'ANSWER: B'. Non-math questions: one line, max 12 words. "
              "Use plain text, no markdown headers. You can't touch his Mac. Never send/delete/post/spend.] ")


def _main():
    import sys; return sys.modules.get("__main__")


async def deliver(text: str, reply: str):
    m = _main()
    try: await asyncio.to_thread(m.push, "Jarvis", reply.replace("\n", " ")[:230])
    except Exception as e: print("watch push fail", e, flush=True)
    try: await asyncio.to_thread(m.mail, ("Jarvis: " + text.replace("\n", " "))[:70], reply)
    except Exception as e: print("watch mail fail", e, flush=True)


def rj(f, d):
    try: return json.loads(f.read_text())
    except Exception: return d


def wj(f, v): f.write_text(json.dumps(v))


# ---- persistence in a PRIVATE Hugging Face dataset (the Space disk is wiped on restart) ----
HF_TOKEN, HF_REPO = os.environ.get("HF_TOKEN"), os.environ.get("HF_DATA_REPO")
STATE = DATA / "state.json"          # {"tg_chat": "...", "tg_paired": bool}
PERSIST = ("memory.md", "state.json", "reminders.json", "mac_queue.json", "usage.json", "history.json")


def hf_pull():
    if not (HF_TOKEN and HF_REPO): return
    from huggingface_hub import hf_hub_download
    for n in PERSIST:
        try: (DATA / n).write_bytes(Path(hf_hub_download(HF_REPO, n, repo_type="dataset", token=HF_TOKEN, force_download=True)).read_bytes())
        except Exception: pass


def hf_push(*names):
    if not (HF_TOKEN and HF_REPO): return
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_TOKEN)
        for n in names or PERSIST:
            if (DATA / n).exists(): api.upload_file(path_or_fileobj=str(DATA / n), path_in_repo=n, repo_id=HF_REPO, repo_type="dataset")
    except Exception as e: print("hf_push failed", e, flush=True)


TG_API = os.environ.get("TG_API", "https://api.telegram.org")
TG_TOKEN, TG_SECRET, PAIR = os.environ.get("TG_TOKEN", ""), os.environ.get("TG_SECRET", ""), os.environ.get("PAIR_CODE", "")


def tg_send(chat, text):
    import urllib.request, urllib.parse
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3500]}).encode()
    try: urllib.request.urlopen(urllib.request.Request(f"{TG_API}/bot{TG_TOKEN}/sendMessage", data=data), timeout=20).read()
    except Exception as e: print("tg_send failed", e, flush=True)


def authed(r) -> bool:
    ip = r.headers.get("x-forwarded-for") or (r.client.host if r.client else "?")
    recent = [t for t in FAILS.get(ip, []) if time.time() - t < 600]
    if len(recent) >= 10 or not TOKEN: return False
    good = secrets.compare_digest(r.headers.get("x-jarvis-token", r.query_params.get("k", "")), TOKEN)
    if not good: FAILS[ip] = recent + [time.time()]
    return good


def spend() -> float:
    return rj(USAGE, {}).get(time.strftime("%Y-%m"), 0.0)


def add_spend(i, o):
    u = rj(USAGE, {}); k = time.strftime("%Y-%m"); u[k] = u.get(k, 0.0) + i * PRICE_IN + o * PRICE_OUT; wj(USAGE, u)


_client = None
async def ask(text: str, watch: bool = False) -> str:
    if MOCK: return f"[mock] you said: {text[:80]}"
    if spend() >= CAP: return f"i hit the ${CAP:.0f} monthly cap so i'm paused till next month, or raise it."
    global _client
    if _client is None:
        import anthropic; _client = anthropic.AsyncAnthropic(max_retries=2)
    mem = MEMFILE.read_text()[:30000] if MEMFILE.exists() else ""
    hist = rj(HIST, [])[-12:]
    r = await _client.messages.create(model=MODEL, max_tokens=1500 if watch else 600, system=(WATCH_NOTE if watch else NOTE) + "\nWhat you know about Karthik:\n" + mem,
                                      messages=hist + [{"role": "user", "content": text}])
    out = "".join(b.text for b in r.content if b.type == "text").strip() or "done."
    add_spend(r.usage.input_tokens, r.usage.output_tokens)
    wj(HIST, (hist + [{"role": "user", "content": text}, {"role": "assistant", "content": out}])[-24:])
    return out


def handle_tags(reply: str, text: str) -> str:
    for m in re.finditer(r"^QUEUE:\s*(.+)$", reply, re.M):
        q = rj(QUEUE, []); q.append({"task": m.group(1).strip(), "asked": text[:200], "t": time.strftime("%Y-%m-%d %H:%M:%S"), "done": False}); wj(QUEUE, q)
    for m in re.finditer(r"^REMIND:\s*(?:(\d{4}-\d\d-\d\d)\s+)?(\d{1,2}):(\d\d)\s*\|\s*(.+)$", reply, re.M):
        d = m.group(1) or time.strftime("%Y-%m-%d"); rs = rj(REM, [])
        rs.append({"at": f"{d} {int(m.group(2)):02d}:{m.group(3)}", "text": m.group(4).strip(), "sent": False}); wj(REM, rs)
    return re.sub(r"^(QUEUE|REMIND):.*\n?", "", reply, flags=re.M).strip() or "done."


def push(title, body):
    subs = rj(DATA / "subs.json", [])
    if not subs or not os.environ.get("VAPID_PRIVATE"):
        print("push skipped:", title, body, flush=True); return
    from pywebpush import webpush
    for s in subs:
        try: webpush(subscription_info=s, data=json.dumps({"title": title, "body": body[:1800]}), vapid_private_key=os.environ["VAPID_PRIVATE"], vapid_claims={"sub": "mailto:jarvis@example.com"})
        except Exception as e: print("push fail", e, flush=True)


async def reminder_loop():
    while True:
        try:
            now = time.strftime("%Y-%m-%d %H:%M"); rs = rj(REM, []); ch = False
            for r in rs:
                if not r["sent"] and r["at"] <= now:
                    chat = rj(STATE, {}).get("tg_chat")
                    if chat and TG_TOKEN: await asyncio.to_thread(tg_send, chat, "reminder: " + r["text"])
                    else: await asyncio.to_thread(push, "Reminder", r["text"])
                    r["sent"] = True; ch = True
            if ch: wj(REM, rs); await asyncio.to_thread(hf_push, "reminders.json")
        except Exception as e: print("rem err", e, flush=True)
        await asyncio.sleep(20)


async def _answer(text, watch):
    try:
        out = handle_tags(await ask(text, watch), text); await asyncio.to_thread(hf_push, "history.json", "reminders.json", "mac_queue.json", "usage.json")
        return out
    except Exception as e:
        print("chat err", e, flush=True); return "something broke on my end, try again."


async def chat(r):
    if not authed(r): return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = await r.json(); text = (b.get("text") or "").strip()[:2000]
    if not text: return JSONResponse({"reply": ""})
    ua = r.headers.get("user-agent", "")
    watch = bool(b.get("async") or r.headers.get("x-async") or (ua and "Mozilla" not in ua))
    async def mail_only(rep):
        try: await asyncio.to_thread(_main().mail, ("Jarvis: " + text.replace("\n", " "))[:70], rep)
        except Exception as e: print("mail fail", e, flush=True)
    if not watch:
        rep = await _answer(text, False); asyncio.create_task(mail_only(rep))
        return JSONResponse({"reply": rep, "mood": "calm", "audio": ""})
    task = asyncio.create_task(_answer(text, True))
    try:
        rep = await asyncio.wait_for(asyncio.shield(task), 9); asyncio.create_task(mail_only(rep))
        return JSONResponse({"reply": rep, "mood": "calm", "audio": ""})
    except asyncio.TimeoutError:
        async def finish(): await deliver(text, await task)
        asyncio.create_task(finish())
        return JSONResponse({"reply": "on it, i'll send the answer to your notifications and mail", "mood": "calm", "audio": ""})


async def ping(r): return JSONResponse({"ok": authed(r), "cloud": True}, status_code=200 if authed(r) else 401)
async def health(r): return JSONResponse({"ok": True})
async def index(r): return HTMLResponse((HERE / "app.html").read_text())
async def sw(r): return Response((HERE / "sw.js").read_text(), media_type="application/javascript", headers={"service-worker-allowed": "/"})
async def icon(r): return Response((HERE / "icon.png").read_bytes(), media_type="image/png")
async def push_key(r): return JSONResponse({"key": os.environ.get("VAPID_PUBLIC", "")}) if authed(r) else JSONResponse({"error": "unauthorized"}, status_code=401)


async def push_sub(r):
    if not authed(r): return JSONResponse({"error": "unauthorized"}, status_code=401)
    s = rj(DATA / "subs.json", []); n = await r.json()
    if n not in s: s.append(n); wj(DATA / "subs.json", s)
    return JSONResponse({"ok": True})


async def sync_get(r):      # Mac pulls queued tasks
    if not authed(r): return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({"queue": [q for q in rj(QUEUE, []) if not q["done"]]})


async def sync_put(r):      # Mac pushes memory.md and marks queue items done
    if not authed(r): return JSONResponse({"error": "unauthorized"}, status_code=401)
    b = await r.json()
    if "memory" in b: MEMFILE.write_text(b["memory"][:200000]); await asyncio.to_thread(hf_push, "memory.md")
    if "done" in b:
        q = rj(QUEUE, [])
        for x in q:
            if x["t"] in b["done"]: x["done"] = True
        wj(QUEUE, q); await asyncio.to_thread(hf_push, "mac_queue.json")
    return JSONResponse({"ok": True})


async def tg_hook(r):
    if not TG_SECRET or not secrets.compare_digest(r.headers.get("x-telegram-bot-api-secret-token", ""), TG_SECRET) or r.path_params["sec"] != TG_SECRET:
        return JSONResponse({"ok": False}, status_code=403)
    u = await r.json(); m = u.get("message") or {}
    chat, text = str((m.get("chat") or {}).get("id", "")), (m.get("text") or "").strip()
    if not chat or not text: return JSONResponse({"ok": True})
    st = rj(STATE, {}); allowed = st.get("tg_chat")
    if not allowed:
        if PAIR and text.startswith("/start") and text.split()[-1] == PAIR:
            st["tg_chat"] = chat; wj(STATE, st); await asyncio.to_thread(hf_push, "state.json")
            await asyncio.to_thread(tg_send, chat, "paired. text me anything.")
        return JSONResponse({"ok": True})
    if chat != allowed or text.startswith("/start"): return JSONResponse({"ok": True})
    now = time.time(); TGR[:] = [t for t in TGR if now - t < 60]
    if len(TGR) >= 12:
        await asyncio.to_thread(tg_send, chat, "slow down a sec."); return JSONResponse({"ok": True})
    TGR.append(now)
    try:
        reply = handle_tags(await ask(text), text); await asyncio.to_thread(hf_push, "history.json", "reminders.json", "mac_queue.json", "usage.json")
    except Exception as e:
        print("tg err", e, flush=True); reply = "something broke on my end, try again."
    await asyncio.to_thread(tg_send, chat, reply)
    return JSONResponse({"ok": True})


TGR: list = []


async def start():
    await asyncio.to_thread(hf_pull)
    asyncio.create_task(reminder_loop())


ROUTES = [Route("/chat", chat, methods=["POST"]), Route("/tg/{sec}", tg_hook, methods=["POST"]), Route("/sync", sync_get, methods=["GET"]), Route("/sync", sync_put, methods=["PUT"])]
