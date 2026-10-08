"""Real browser checks. --device requires Android; --qidian also launches the real app."""
import argparse
from pathlib import Path
import os
import shutil
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--device", action="store_true")
parser.add_argument("--qidian", action="store_true")
args = parser.parse_args()
artifacts = Path(os.getenv("QTRADE_DEBUG_ARTIFACTS", "/workspace/.qtrade-linux-debug/artifacts"))
artifacts.mkdir(parents=True, exist_ok=True)
port = int(os.getenv("QTRADE_DEBUG_PORT", "17620"))
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=shutil.which("chromium"), headless=True, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1280, "height": 1050})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"http://127.0.0.1:{port}", wait_until="networkidle")
    page.locator("body.busy").wait_for(state="detached", timeout=90000)
    assert page.title() == "QTrade · Linux Debug"
    status = page.request.get(f"http://127.0.0.1:{port}/api/debug/status", timeout=90000).json()
    if args.device or args.qidian:
        page.locator("#connect").click()
        page.locator("body.busy").wait_for(state="detached", timeout=90000)
        status = page.request.get(f"http://127.0.0.1:{port}/api/debug/status", timeout=90000).json()
        assert status["device"]["booted"], status["device"]
        if args.qidian:
            assert status["device"]["qidian_installed"], "企点 APK 尚未安装"
            page.locator("#launch").click()
            page.locator("body.busy").wait_for(state="detached", timeout=180000)
            assert page.locator("#feedback").get_attribute("class") != "error", page.locator("#feedback").inner_text()
            state = page.request.get(f"http://127.0.0.1:{port}/api/debug/status", timeout=90000).json()
            assert "com.tencent.qidian" in state["device"]["foreground"], state["device"]
        else:
            page.locator("#screenshot").click()
            page.locator("body.busy").wait_for(state="detached", timeout=90000)
        page.locator("#screen:not([hidden])").wait_for(timeout=30000)
        width = page.locator("#screen").evaluate("async (img) => { await img.decode(); return img.naturalWidth; }")
        assert width > 0
        assert page.locator("#screen").is_visible()
    else:
        assert page.locator("#badge").inner_text() != "检查中"
        if not status["device"]["booted"]:
            assert page.locator("#screenshot").is_disabled()
            assert page.locator("#launch").is_disabled()
    assert not errors, errors
    page.screenshot(path=str(artifacts / "linux-debug.png"), full_page=True)
    browser.close()
print("PASS: browser UI" + (" -> ADB -> real Android screenshot" if args.device or args.qidian else " (device E2E not requested)"))
if args.qidian:
    print("PASS: real Qidian foreground launch; login and messaging are separate checks")
