"""Video -> answers by email. POST /video (raw video body, x-token). Pulls ~1 frame/sec with ffmpeg, drops near-duplicate and blurry frames,
Opus reads every question across all frames, ONE email with explanation + answer per question. Returns right away; email in ~1-3 min."""
import os, io, time, base64, asyncio, subprocess, tempfile, glob
from starlette.responses import JSONResponse

MAX_BYTES = 400 * 1024 * 1024
MAX_FRAMES = 60
VIDEO_SYS = ("You get frames pulled from a short video a student filmed of a PRACTICE test/worksheet at home (glasses camera, may be tilted, blurry, or repeat the same page). "
             "Read every frame, find EVERY distinct question (same question seen in several frames = one question; use the sharpest view). "
             "For each question, in order, write:\n<number>. <question topic in a few words>\nExplanation: how to solve it, key steps/equations/concepts, as short lines.\nAnswer: <final answer> (MC: letter + choice text; math: value with units).\n\n"
             "Blank line between questions. No markdown. Unreadable question: say which frame/number and 'unreadable'. No questions at all: 'no questions found'.")


def ffmpeg_exe():
    try:
        import imageio_ffmpeg; return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception: return "ffmpeg"


def frames(path):
    """1 fps (2 fps if short), keep frames that differ from the last kept one, prefer sharp ones."""
    from PIL import Image, ImageFilter, ImageStat, ImageChops, ImageOps
    d = tempfile.mkdtemp()
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-i", path, "-vf", "fps=2,scale='min(1400,iw)':-2", "-q:v", "4", f"{d}/f%04d.jpg"], check=True, timeout=240)
    files = sorted(glob.glob(f"{d}/*.jpg"))
    def sharp(im): return ImageStat.Stat(im.convert("L").resize((480, 480)).filter(ImageFilter.FIND_EDGES)).var[0]
    def thumb(im): return ImageOps.autocontrast(im.convert("L").resize((160, 120)), cutoff=1)
    groups, last = [], None
    for f in files:
        im = Image.open(f); t = thumb(im); s = sharp(im)
        if last is not None and ImageStat.Stat(ImageChops.difference(t, last)).mean[0] < 4:
            if s > groups[-1][0]: groups[-1] = (s, f)       # same scene: keep the sharper frame
        else:
            groups.append((s, f))
        last = t
    if groups:
        cut = max(g[0] for g in groups) * 0.08                 # drop motion-blur junk frames
        groups = [g for g in groups if g[0] >= cut]
    if len(groups) > MAX_FRAMES:
        step = len(groups) / MAX_FRAMES; groups = [groups[int(i * step)] for i in range(MAX_FRAMES)]
    out = []
    for _, f in groups:
        out.append(base64.b64encode(open(f, "rb").read()).decode())
    return out, len(files)


async def job(path, app):
    t = time.time()
    try:
        imgs, total = await asyncio.to_thread(frames, path)
        if not imgs: raise RuntimeError("no frames")
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}} for b in imgs]
        content.append({"type": "text", "text": f"{len(imgs)} frames in video order. Answer every question."})
        if app.MOCK: body = f"MOCK: {len(imgs)} frames kept of {total}"
        else:
            r = await asyncio.wait_for(app.client().messages.create(model=app.SMART, max_tokens=16000, system=VIDEO_SYS + app.COURSES,
                     thinking={"type": "adaptive"}, messages=[{"role": "user", "content": content}]), 420)
            body = "".join(b.text for b in r.content if b.type == "text").strip()
        body += f"\n\n({len(imgs)} frames read, {round(time.time() - t)}s)"
    except Exception as e:
        body = f"Video failed: {type(e).__name__} {str(e)[:200]}"
    finally:
        try: os.remove(path)
        except Exception: pass
    await asyncio.to_thread(app.mail, "Video answers " + time.strftime("%H:%M"), body)
    with open(app.DATA / "log.md", "a") as f: f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M')} (video)\n{body}\n")
    app.SMSLOG.append({"t": time.strftime("%H:%M:%S"), "video": body[:80]})


def route(app):
    async def video(request):
        if not app.authed(request): return JSONResponse({"error": "unauthorized"}, status_code=401)
        fd, path = tempfile.mkstemp(suffix=".mov"); n = 0
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                n += len(chunk)
                if n > MAX_BYTES: return JSONResponse({"error": "video too big"}, status_code=413)
                f.write(chunk)
        if n < 1000: return JSONResponse({"error": "no video"}, status_code=400)
        asyncio.create_task(job(path, app))
        return JSONResponse({"reply": "Got the video. Answers will be in your email in 1-3 min."})
    return video
