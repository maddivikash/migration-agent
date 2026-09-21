"""Record the demo video with Playwright: run -> resolve escalations in the UI -> push -> retry -> rollback -> audit.
Usage: .venv/bin/python scripts/record_demo.py   (server must be running on :8000; .state is cleared first)
Output: app/static/demo.webm (plays in any browser / VLC)
"""
import glob, os, shutil, subprocess, time
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE = os.getenv("DEMO_URL", "http://localhost:8000")
OUT = ROOT / "app" / "static"
httpx.delete(f"{BASE}/api/memory", timeout=5)   # start with an empty mapping memory
shutil.rmtree(ROOT / "docs" / "_video", ignore_errors=True)


def pause(page, s=1.2):
    page.wait_for_timeout(int(s * 1000))


def wait_done(page):
    page.wait_for_function("() => document.querySelector('#statusDot').classList.contains('done')", timeout=90_000)
    pause(page, 1.0)


def tab(page, name):
    page.click(f".tabs button[data-tab={name}]"); pause(page, 1.0)


def card(page, title_part):
    return page.locator(".card", has=page.locator("h4", has_text=title_part)).first


def settle(page, c, title_part):
    """Wait until the card is gone (decision applied + UI refreshed), then report."""
    wait_done(page)
    c.wait_for(state="detached", timeout=20_000)
    page.evaluate("window.scrollTo(0,0)"); pause(page, 0.8)
    print("resolved:", title_part, "| open now:", page.locator("#queueBadge").inner_text())


def decide_option(page, title_part, label_part, note=None):
    c = card(page, title_part)
    c.scroll_into_view_if_needed(); pause(page, 1.2)
    if note:
        c.locator("[data-note]").fill(note); pause(page, 0.6)
    c.locator(".options button", has_text=label_part).first.click()
    settle(page, c, title_part)


def decide_custom(page, title_part, values, note=None):
    c = card(page, title_part)
    c.scroll_into_view_if_needed(); pause(page, 1.2)
    for f, v in values.items():
        c.locator(f".custom input[data-f={f}]").fill(v); pause(page, 0.5)
    if note:
        c.locator("[data-note]").fill(note); pause(page, 0.6)
    c.locator("[data-custom]").click()
    settle(page, c, title_part)


with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=str(OUT / "_video"),
                              record_video_size={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto(BASE); pause(page, 1.5)

    # 1. run the agent, watch the live feed
    page.click("#btnRun")
    page.wait_for_function("() => document.querySelector('#statusDot').classList.contains('running')", timeout=10_000)
    wait_done(page); pause(page, 1.5)

    # 2. the escalation queue
    tab(page, "queue"); pause(page, 2.5)
    decide_option(page, "Personal Email", "Don't migrate", note="personal emails are not migrated per client policy")
    decide_option(page, "hire_date differs", "legacy_hris", note="HRIS is the system of record")
    decide_custom(page, "31/02/2020", {"hire_date": "2020-02-29"}, note="confirmed with client HR")
    decide_custom(page, "Madonna", {"first_name": "Madonna", "last_name": "Ciccone"})
    decide_option(page, "'BD' mean", "Sales", note="Business Development sits under Sales")
    decide_option(page, "same person as", "Same person")
    decide_option(page, "terminated employee must have", "Actually still active")
    pause(page, 2.0)

    # 3. mappings
    tab(page, "mappings"); pause(page, 2.5)
    page.mouse.wheel(0, 700); pause(page, 2.0); page.evaluate("window.scrollTo(0,0)")

    # 4. push, retry
    tab(page, "live")
    page.click("#btnPush"); pause(page, 3.0)
    tab(page, "records"); pause(page, 1.0)
    page.select_option("#recStatus", "failed"); pause(page, 2.0)
    page.click("#btnRetry"); pause(page, 3.0)
    page.select_option("#recStatus", ""); pause(page, 1.5)

    # 5. rollback one record, re-queue it
    page.select_option("#recStatus", "pushed"); pause(page, 1.0)
    page.once("dialog", lambda d: d.accept())
    page.locator("tr.clickable [data-rollback]").first.click(); pause(page, 2.5)
    page.select_option("#recStatus", "rolled_back"); pause(page, 1.5)
    page.select_option("#recStatus", ""); pause(page, 1.0)

    # 6. audit: record drawer, then audit tab
    page.locator("tr.clickable td:nth-child(2)").first.click(); pause(page, 3.0)
    page.mouse.wheel(0, 600); pause(page, 2.0)
    page.click(".drawer .close"); pause(page, 0.8)
    tab(page, "audit"); pause(page, 3.0)
    page.mouse.wheel(0, 500); pause(page, 2.0)

    # 7. memory: second run asks fewer questions
    page.click("#btnRun"); wait_done(page)
    tab(page, "queue"); pause(page, 3.0)
    ctx.close(); browser.close()

webm = max(glob.glob(str(OUT / "_video" / "*.webm")), key=os.path.getmtime)
shutil.move(webm, OUT / "demo.webm"); shutil.rmtree(OUT / "_video", ignore_errors=True)
print("wrote", OUT / "demo.webm")
