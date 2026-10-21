import asyncio
import json
import os
import subprocess
import tempfile
import time
import urllib.request

import websockets

CHROME = "google-chrome"
PORT = 9226
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


def url(path):
    if not TOKEN:
        return BASE + path
    return BASE + path + ("&" if "?" in path else "?") + "token=" + TOKEN


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
         f"--remote-debugging-port={PORT}",
         f"--user-data-dir={os.path.join(tempfile.gettempdir(), 'manete-chrome-editor')}",
         "about:blank"],
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
            res = r.get("result", {})
            if res.get("exceptionDetails"):
                return "ERRO: " + json.dumps(res["exceptionDetails"])[:200]
            return res.get("result", {}).get("value")

        async def nav(url, wait=0.9):
            await cmd("Page.navigate", {"url": url})
            await asyncio.sleep(wait)

        def pe(event, x, y, pid=1):
            return (f"(function(){{var l=document.getElementById('edit-layer');"
                    f"l.dispatchEvent(new PointerEvent('{event}',"
                    f"{{clientX:{x},clientY:{y},pointerId:{pid},bubbles:true,pointerType:'touch'}}));return true;}})()")

        await cmd("Page.enable")
        await cmd("Runtime.enable")
        await cmd("Emulation.setDeviceMetricsOverride",
                  {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})

        await nav(url("/controle"))
        await ev("localStorage.removeItem('manete.edit.classic')")
        await nav(url("/controle?layout=classic"))

        print("== entra no modo edicao ==")
        await ev("document.getElementById('edit-fab').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:1}))")
        await asyncio.sleep(0.3)
        print("  body.editing:", await ev("document.body.classList.contains('editing')"))
        print("  edit-layer visivel:", await ev("getComputedStyle(document.getElementById('edit-layer')).display"))

        print("== move o stick-left ==")
        center = await ev("(()=>{const r=document.querySelector('#overlay [data-unit=\"stick-left\"]').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2];})()")
        cx, cy = center
        await ev(pe("pointerdown", cx, cy))
        await ev(pe("pointermove", cx + 50, cy + 30))
        await ev(pe("pointerup", cx + 50, cy + 30))
        await asyncio.sleep(0.2)
        print("  storage:", await ev("localStorage.getItem('manete.edit.classic')"))
        print("  transform:", await ev("document.querySelector('#overlay [data-unit=\"stick-left\"]').style.transform"))
        print("  edit-box:", await ev("getComputedStyle(document.getElementById('edit-box')).display"))

        print("== redimensiona o dpad (alca) ==")
        dcenter = await ev("(()=>{const r=document.querySelector('#overlay [data-unit=\"dpad\"]').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2];})()")
        dx, dy = dcenter
        await ev(pe("pointerdown", dx, dy, pid=2))
        await ev(pe("pointerup", dx, dy, pid=2))
        await asyncio.sleep(0.2)
        h = await ev("(()=>{const r=document.querySelector('#edit-box .handle').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2];})()")
        hx, hy = h
        await ev(pe("pointerdown", hx, hy, pid=3))
        await ev(pe("pointermove", hx + 60, hy + 60, pid=3))
        await ev(pe("pointerup", hx + 60, hy + 60, pid=3))
        await asyncio.sleep(0.2)
        print("  storage:", await ev("localStorage.getItem('manete.edit.classic')"))

        print("== restaurar limpa os ajustes ==")
        await ev("document.getElementById('edit-reset').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:4}))")
        await asyncio.sleep(0.3)
        print("  storage:", await ev("localStorage.getItem('manete.edit.classic')"))
        print("  transform:", repr(await ev("document.querySelector('#overlay [data-unit=\"stick-left\"]').style.transform")))

        print("== concluir sai da edicao e religa os controles ==")
        await ev("document.getElementById('edit-done').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:5}))")
        await asyncio.sleep(0.3)
        print("  body.editing:", await ev("document.body.classList.contains('editing')"))
        # apos sair, o toque no analógico deve existir (wire religado): confere listener via Input
        print("  stick existe:", await ev("!!document.querySelector('#overlay [data-unit=\"stick-left\"]')"))

        print("== editar outro layout nao afeta o classic (chaves separadas) ==")
        await nav(url("/controle?layout=fps"))
        print("  edit-fab presente:", await ev("!!document.getElementById('edit-fab')"))

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


asyncio.run(main())
