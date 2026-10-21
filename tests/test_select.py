import asyncio
import json
import os
import subprocess
import tempfile
import time
import urllib.request

import websockets

CHROME = "google-chrome"
PORT = 9224
BASE = os.environ.get("MANETE_URL", "http://127.0.0.1:8081").rstrip("/")


def _token():
    t = os.environ.get("MANETE_TOKEN")
    if t:
        return t
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "config.json")) as fh:
            return json.load(fh).get("token") or ""
    except Exception:
        return ""


TOKEN = _token()


def u(path, extra=""):
    parts = []
    if extra:
        parts.append(extra)
    if TOKEN:
        parts.append("token=" + TOKEN)
    if not parts:
        return BASE + path
    return BASE + path + ("&" if "?" in path else "?") + "&".join(parts)


def wait_ws():
    for _ in range(60):
        try:
            data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json").read())
            for t in data:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(0.2)
    raise SystemExit("chrome nao subiu")


async def main():
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={os.path.join(tempfile.gettempdir(), 'manete-chrome-select')}", "about:blank"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wsurl = wait_ws()
    async with websockets.connect(wsurl, max_size=None) as ws:
        counter = 0

        async def cmd(method, params=None):
            nonlocal counter
            counter += 1
            await ws.send(json.dumps({"id": counter, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == counter:
                    return msg

        async def ev(expr):
            r = await cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            return r.get("result", {}).get("result", {}).get("value")

        async def nav(url, wait=0.9):
            await cmd("Page.navigate", {"url": url})
            await asyncio.sleep(wait)

        await cmd("Page.enable")
        await cmd("Runtime.enable")
        await cmd("Emulation.setDeviceMetricsOverride",
                  {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})

        print("== default (sem param, sem storage) ==")
        await nav(u("/controle"))
        await ev("localStorage.removeItem('manete.layout')")
        await nav(u("/controle"))
        print("  overlay:", await ev("document.getElementById('overlay').className"))

        print("== storage = fight, reload ==")
        await ev("localStorage.setItem('manete.layout','fight')")
        await nav(u("/controle"))
        print("  overlay:", await ev("document.getElementById('overlay').className"))

        print("== URL ?layout=racing vence o storage ==")
        await nav(u("/controle?layout=racing"))
        print("  overlay:", await ev("document.getElementById('overlay').className"))

        print("== troca pelo seletor (FAB -> item 'retro') ==")
        await ev("document.getElementById('layout-fab').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}))")
        await asyncio.sleep(0.3)
        print("  sheet aberto:", await ev("document.getElementById('layout-sheet').classList.contains('open')"))
        ok = await ev("(()=>{const it=document.querySelector('.sheet-item[data-layout=\\\"retro\\\"]'); if(!it) return 'sem item'; it.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true})); return 'clicou';})()")
        await asyncio.sleep(0.3)
        print("  ", ok, "| overlay:", await ev("document.getElementById('overlay').className"),
              "| storage:", await ev("localStorage.getItem('manete.layout')"),
              "| sheet aberto:", await ev("document.getElementById('layout-sheet').classList.contains('open')"))

        print("== painel / (links com layout+token) ==")
        await nav(u("/", "layout=fps"))
        print("  link /controle:", await ev("document.querySelector('[data-mode=\\\"/controle\\\"]').getAttribute('href')"))
        print("  link /portatil:", await ev("document.querySelector('[data-mode=\\\"/portatil\\\"]').getAttribute('href')"))
        print("  link /config :", await ev("document.getElementById('config-link').getAttribute('href')"))
        print("  card selecionado:", await ev("(document.querySelector('.layout.sel')||{}).textContent"))

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


asyncio.run(main())
