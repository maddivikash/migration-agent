"""Record the narrated demo: edge-tts voice-over per scene, Playwright drives the UI for at least each
clip's length, ffmpeg muxes audio + video into app/static/demo.mp4 (and keeps demo.webm).
Usage: .venv/bin/python scripts/record_demo.py      (server on :8000; state cleared via the API)
"""
import asyncio, glob, json, os, shutil, subprocess, sys, time
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright
sys.path.insert(0, str(Path(__file__).resolve().parent))
from narration import SCENES

ROOT = Path(__file__).resolve().parent.parent
BASE = os.getenv("DEMO_URL", "http://localhost:8000")
OUT = ROOT / "app" / "static"
WORK = ROOT / ".demo_work"; WORK.mkdir(exist_ok=True)
VOICE = os.getenv("DEMO_VOICE", "en-US-AndrewMultilingualNeural")
FF = shutil.which("ffmpeg")
if not FF:
    import imageio_ffmpeg; FF = imageio_ffmpeg.get_ffmpeg_exe()

# ---------------------------------------------------------------- 1. voice-over
async def tts_all():
    import edge_tts
    for sid, text in SCENES:
        f = WORK / f"{sid}.mp3"
        if not f.exists():
            await edge_tts.Communicate(text, VOICE, rate="+4%").save(str(f))
asyncio.run(tts_all())

def dur(f):
    out = subprocess.run([FF, "-i", str(f)], capture_output=True, text=True).stderr
    h, m, s = out.split("Duration: ")[1].split(",")[0].split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)
DUR = {sid: dur(WORK / f"{sid}.mp3") for sid, _ in SCENES}
print("narration:", {k: round(v, 1) for k, v in DUR.items()}, "total", round(sum(DUR.values()), 1), "s")

# ---------------------------------------------------------------- 2. record video in time with the voice
httpx.delete(f"{BASE}/api/memory", timeout=5)
shutil.rmtree(OUT / "_video", ignore_errors=True)
timeline = []   # (scene_id, start_seconds) relative to recording start
T0 = None

def now(): return time.monotonic() - T0
def pause(page, s): page.wait_for_timeout(int(s * 1000))
def scene(page, sid):
    """Mark the start of a scene; returns the wall-clock deadline by which the scene should end."""
    timeline.append((sid, now())); return now() + DUR[sid] + 0.6
def hold(page, until):
    left = until - now()
    if left > 0: pause(page, left)
def wait_done(page):
    page.wait_for_function("() => document.querySelector('#statusDot').classList.contains('done')", timeout=90_000)
def tab(page, name): page.click(f".tabs button[data-tab={name}]"); pause(page, 0.8)
def card(page, title_part): return page.locator(".card", has=page.locator("h4", has_text=title_part)).first
def settle(page, c):
    c.wait_for(state="detached", timeout=30_000); page.evaluate("window.scrollTo(0,0)"); pause(page, 0.6)
def decide_option(page, title_part, label_part, note=None, sid=None):
    end = scene(page, sid) if sid else None
    c = card(page, title_part); c.scroll_into_view_if_needed(); pause(page, 1.6)
    if note: c.locator("[data-note]").type(note, delay=18); pause(page, 0.5)
    c.locator(".options button", has_text=label_part).first.click(); settle(page, c)
    if end: hold(page, end)
def decide_custom(page, title_part, values, note=None, sid=None):
    end = scene(page, sid) if sid else None
    c = card(page, title_part); c.scroll_into_view_if_needed(); pause(page, 1.6)
    for f, v in values.items(): c.locator(f".custom input[data-f={f}]").type(v, delay=45); pause(page, 0.4)
    if note: c.locator("[data-note]").type(note, delay=18); pause(page, 0.5)
    c.locator("[data-custom]").click(); settle(page, c)
    if end: hold(page, end)

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=str(OUT / "_video"),
                              record_video_size={"width": 1440, "height": 900})
    page = ctx.new_page(); page.goto(BASE); page.wait_for_timeout(800)
    T0 = time.monotonic()
    end = scene(page, "intro"); pause(page, 1.0); page.hover(".hero button"); hold(page, end)

    end = scene(page, "run"); page.click("#btnRun"); wait_done(page)
    # let the staggered feed play out and scroll to the bottom before the scene ends
    hold(page, end - 3.0); page.evaluate("document.querySelector('#feed').scrollTop = 1e9"); hold(page, end)

    end = scene(page, "queue"); tab(page, "queue"); pause(page, 2.0); page.mouse.wheel(0, 500); pause(page, 1.2); page.mouse.wheel(0, -500); hold(page, end)
    decide_option(page, "Personal Email", "Don't migrate", note="personal emails are not migrated per client policy", sid="q1")
    decide_option(page, "hire_date differs", "legacy_hris", note="HRIS is the system of record", sid="q2")
    decide_custom(page, "31/02/2020", {"hire_date": "2020-02-29"}, note="confirmed with client HR", sid="q3")
    decide_custom(page, "Madonna", {"first_name": "Madonna", "last_name": "Ciccone"}, sid="q4")
    decide_option(page, "'BD' mean", "Sales", note="Business Development sits under Sales", sid="q5")
    decide_option(page, "same person as", "Same person", sid="q6")
    decide_option(page, "terminated employee must have", "Actually still active", sid="q7")

    end = scene(page, "mappings"); tab(page, "mappings"); pause(page, 2.0); page.mouse.wheel(0, 600); pause(page, 1.5); page.mouse.wheel(0, 600); hold(page, end)

    end = scene(page, "push"); tab(page, "live"); pause(page, 0.5); page.click("#btnPush"); pause(page, 3.5)
    tab(page, "records"); page.select_option("#recStatus", "failed"); pause(page, 2.5); page.click("#btnRetry"); pause(page, 3.0)
    page.select_option("#recStatus", ""); hold(page, end)

    end = scene(page, "rollback"); page.select_option("#recStatus", "pushed"); pause(page, 1.0)
    page.once("dialog", lambda d: d.accept()); page.locator("tr.clickable [data-rollback]").first.click(); pause(page, 2.0)
    page.select_option("#recStatus", "rolled_back"); pause(page, 1.5); page.select_option("#recStatus", ""); hold(page, end)

    end = scene(page, "audit"); page.locator("tr.clickable td:nth-child(2)").first.click(); pause(page, 3.0); page.mouse.wheel(0, 700); pause(page, 2.5)
    page.click(".drawer .close"); pause(page, 0.5); tab(page, "audit"); pause(page, 2.0); page.mouse.wheel(0, 500); hold(page, end)

    end = scene(page, "outro"); page.click("#btnRun"); wait_done(page); pause(page, 1.0); tab(page, "queue"); hold(page, end); pause(page, 1.0)
    total = now()
    ctx.close(); browser.close()

webm = max(glob.glob(str(OUT / "_video" / "*.webm")), key=os.path.getmtime)
raw = WORK / "raw.webm"; shutil.move(webm, raw); shutil.rmtree(OUT / "_video", ignore_errors=True)

# ---------------------------------------------------------------- 3. mix narration onto the timeline + mux
# Playwright's video may start slightly after T0; align on the recording's actual duration.
vid = dur(raw); offset = max(0.0, vid - total)
inputs, filters = [], []
for i, (sid, start) in enumerate(timeline):
    inputs += ["-i", str(WORK / f"{sid}.mp3")]
    filters.append(f"[{i+1}:a]adelay={int((start + offset) * 1000)}|{int((start + offset) * 1000)}[a{i}]")
mix = "".join(f"[a{i}]" for i in range(len(timeline))) + f"amix=inputs={len(timeline)}:normalize=0,volume=1.0[aout]"
fc = ";".join(filters + [mix])
subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(raw), *inputs, "-filter_complex", fc,
                "-map", "0:v", "-map", "[aout]", "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(OUT / "demo.mp4")], check=True)
subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(OUT / "demo.mp4"), "-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0",
                "-c:a", "libopus", "-b:a", "64k", str(OUT / "demo.webm")], check=True)
json.dump({"timeline": timeline, "video_s": round(dur(OUT / "demo.mp4"), 1)}, open(WORK / "timeline.json", "w"), indent=1)
print("wrote", OUT / "demo.mp4", "and demo.webm;", round(dur(OUT / "demo.mp4"), 1), "s")
