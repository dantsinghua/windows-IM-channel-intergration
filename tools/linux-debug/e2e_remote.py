"""Two real browser origins -> authenticated API/WS -> actual redroid, no device mocks.

Uses loopback HTTP only for this integration harness. Public Sites/TLS ingress is
separate and must also be checked against the eventual deployment URL.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import shutil
import socket
import tempfile
from threading import Thread
import time
import urllib.request
import json

from playwright.sync_api import sync_playwright, expect
import uvicorn
from server import create_app, Device

ROOT = Path(__file__).resolve().parent
ARTIFACTS = Path('/workspace/.qtrade-linux-debug/artifacts')
ARTIFACTS.mkdir(parents=True, exist_ok=True)

class QuietStatic(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

def until(predicate, page, timeout=45):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        page.wait_for_timeout(150)
    raise AssertionError('Timed out waiting for real device/browser state')

static=ThreadingHTTPServer(('127.0.0.1',0),partial(QuietStatic,directory=str(ROOT/'web')))
Thread(target=static.serve_forever,daemon=True).start()
site_origin=f'http://127.0.0.1:{static.server_port}'
api_socket=socket.socket()
api_socket.bind(('127.0.0.1',0))
api_origin=f'http://127.0.0.1:{api_socket.getsockname()[1]}'
report={'site_deployed':False,'transport':'two loopback HTTP origins','checks':[]}

def passed(message):
    report['checks'].append(message)
    print('PASS:',message,flush=True)

with tempfile.TemporaryDirectory(prefix='qtrade-remote-',dir='/tmp') as directory:
    key=Path(directory)/'token'
    token=secrets.token_urlsafe(32)
    key.write_text(token);key.chmod(0o600)
    device=Device()
    original_forwards=device.run('forward','--list',selected=False)
    app=create_app(device,site_origin=site_origin,api_origin=api_origin,token_file=key)
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False,
                                       ws='websockets',ws_max_size=65536))
    thread=Thread(target=lambda:server.run(sockets=[api_socket]),daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:break
        time.sleep(.05)
    if not server.started:raise RuntimeError('Test API failed to start')
    def actual_status():
        request=urllib.request.Request(api_origin+'/api/debug/status',headers={
            'Origin':site_origin,'Authorization':'Bearer '+token})
        with urllib.request.urlopen(request,timeout=45) as response:return json.load(response)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1100})
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(site_origin,wait_until='networkidle')
            page.locator('body.busy').wait_for(state='detached',timeout=90000)
            def connect(value):
                page.locator('#backend-url').fill(api_origin)
                page.locator('#backend-token').fill(value)
                page.locator('#connect-backend').click()
                page.locator('body.busy').wait_for(state='detached',timeout=90000)
            connect('invalid')
            expect(page.locator('#feedback')).to_contain_text('访问凭据无效')
            assert page.locator('#launch').is_disabled()
            passed('wrong credential is rejected and device actions stay disabled')
            connect(token)
            expect(page.locator('#badge')).to_have_text('设备就绪')
            assert page.locator('#backend-token').input_value()==''
            assert page.evaluate('JSON.stringify(localStorage)')=='{}'
            assert page.evaluate('JSON.stringify(sessionStorage)')=='{}'
            assert token not in page.url
            passed('cross-origin browser authentication and real Android status')
            page.locator('#connect').click()
            page.locator('body.busy').wait_for(state='detached',timeout=90000)
            expect(page.locator('#feedback')).to_have_text('ADB 已连接')
            passed('cross-origin POST with preflight, bearer credential and CSRF')
            page.locator('#live-start').click()
            canvas=page.locator('#video-screen')
            until(lambda:int(canvas.get_attribute('data-frames') or '0')>=5,page)
            assert canvas.is_visible()
            frame_count=int(canvas.get_attribute('data-frames'))
            started=time.monotonic();page.wait_for_timeout(2500)
            report['observed_fps']=round((int(canvas.get_attribute('data-frames'))-frame_count)/(time.monotonic()-started),1)
            report['video_size']=canvas.evaluate('(e)=>[e.width,e.height]')
            passed('authenticated WebSocket carries H264 decoded by browser WebCodecs')
            before_home=canvas.evaluate('(e)=>e.toDataURL()')
            home_started=time.monotonic()
            page.locator('[data-key="home"]').click()
            page.locator('body.busy').wait_for(state='detached',timeout=90000)
            until(lambda:canvas.evaluate('(e)=>e.toDataURL()')!=before_home,page)
            report['observed_home_feedback_ms']=round((time.monotonic()-home_started)*1000)
            until(lambda:'com.android.launcher3' in actual_status()['device']['foreground'],page)
            passed('web navigation key reaches the actual Android launcher')
            page.wait_for_timeout(1500)
            before=canvas.evaluate('(e)=>e.toDataURL()')
            page.locator('#interactive').check()
            bounds=canvas.bounding_box()
            x=bounds['x']+bounds['width']*.5
            y=bounds['y']+bounds['height']*.9
            page.mouse.move(x,y);page.mouse.down()
            for i in range(1,10):
                page.mouse.move(x,y-bounds['height']*.07*i);page.wait_for_timeout(45)
            page.mouse.up()
            until(lambda:canvas.evaluate('(e)=>e.toDataURL()')!=before,page)
            # Inspect Launcher3's actual UI state. uiautomator may refuse to dump
            # while continuous video/animations keep the accessibility tree busy.
            until(lambda:'mState:AllApps' in device.shell('dumpsys','activity','top',timeout=20),page)
            passed('pointer down/move/up opens the real Android app drawer')
            page.locator('#interactive').uncheck()
            page.locator('#launch').click()
            page.locator('body.busy').wait_for(state='detached',timeout=180000)
            assert page.locator('#feedback').get_attribute('class')!='error',page.locator('#feedback').inner_text()
            assert 'com.tencent.qidian' in actual_status()['device']['foreground']
            assert canvas.is_visible()
            passed('Qidian opens while its live video continues')
            page.screenshot(path=str(ARTIFACTS/'remote-live-video.png'),full_page=True)
            page.locator('#live-stop').click()
            assert canvas.is_hidden()
            until(lambda:device.run('forward','--list',selected=False)==original_forwards,page,timeout=30)
            passed('stopping video releases its ADB forward and Android stream')
            page.locator('#live-start').click()
            until(lambda:int(canvas.get_attribute('data-frames') or '0')>=3,page)
            passed('live stream reconnects with fresh frames')
            page.locator('#disconnect').click()
            assert canvas.is_hidden() and page.locator('#launch').is_disabled()
            expect(page.locator('#connection-status')).to_have_text('尚未选择后端')
            passed('disconnect clears control and hides stale frames')
            connect(token)
            server.should_exit=True;thread.join(timeout=30)
            assert not thread.is_alive()
            page.locator('#refresh').click()
            page.locator('body.busy').wait_for(state='detached',timeout=90000)
            expect(page.locator('#badge')).to_have_text('未连接调试服务')
            assert page.locator('#launch').is_disabled()
            passed('real backend shutdown is shown as unavailable')
            assert not errors,errors
            browser.close()
    finally:
        server.should_exit=True
        thread.join(timeout=30)
        static.shutdown();static.server_close()
        api_socket.close()
        (ARTIFACTS/'remote-e2e.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print('Measured video:',report['video_size'],report['observed_fps'],'fps; Sites deployment remains unverified')
