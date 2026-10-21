import asyncio
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

import websockets

CHROME = "google-chrome"
SERVER = os.environ.get("MANETE_URL", "http://127.0.0.1:8081").rstrip("/")


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


def q(path):
    if not TOKEN:
        return SERVER + path
    return SERVER + path + ("&" if "?" in path else "?") + "token=" + TOKEN


def status():
    return json.loads(urllib.request.urlopen(SERVER + "/status", timeout=5).read())


def post(path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(q(path), data=body)
    return urllib.request.urlopen(req, timeout=5).read()


class Browser:
    def __init__(self, port, profile):
        self.port = port
        self.profile = profile
        self.ws = None

    async def start(self):
        env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
             f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}",
             "about:blank"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target = self._wait_target()
        self.ws = await websockets.connect(target, max_size=None)
        self.counter = 0
        await self.cmd("Page.enable")
        await self.cmd("Runtime.enable")
        await self.cmd("Emulation.setDeviceMetricsOverride",
                       {"width": 844, "height": 390, "deviceScaleFactor": 1, "mobile": True})

    def _wait_target(self):
        for _ in range(80):
            try:
                data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json").read())
                for t in data:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
            except Exception:
                time.sleep(0.2)
        raise SystemExit("chrome nao subiu")

    async def cmd(self, method, params=None):
        self.counter += 1
        await self.ws.send(json.dumps({"id": self.counter, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.counter:
                return msg

    async def ev(self, expr):
        r = await self.cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("result", {}).get("value")

    async def nav(self, url, wait=1.4):
        await self.cmd("Page.navigate", {"url": url})
        await asyncio.sleep(wait)

    async def close(self):
        if self.ws:
            await self.ws.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=8)
        except Exception:
            self.proc.kill()


def hora(msg):
    print(msg, flush=True)


async def main():
    a = Browser(9301, os.path.join(tempfile.gettempdir(), "manete-chrome-a"))
    b = Browser(9302, os.path.join(tempfile.gettempdir(), "manete-chrome-b"))
    await a.start()
    await b.start()

    # ---- 1) Um controle (portatil) -> 1 viewer, 1 controlador ----
    await a.nav(q("/portatil?device=devA"))
    s = status()
    hora(f"1) apos /portatil A: viewers={s['viewers']} active={s['controllers']['active']} reserved={s['controllers']['reserved']}")
    playerA = await a.ev("document.getElementById('player').textContent")
    hora(f"   playerA='{playerA}'")

    # ---- 2) Uma tela (tela) em outro aparelho -> +1 viewer, sem consumir slot ----
    await b.nav(q("/tela?device=devB"))
    s = status()
    hora(f"2) apos /tela B: viewers={s['viewers']} active={s['controllers']['active']} reserved={s['controllers']['reserved']}")
    playerB = await b.ev("document.getElementById('player').textContent")
    hora(f"   playerB='{playerB}'")

    # ---- 3) Reload x3 do controle -> viewers nao vaza ----
    for i in range(3):
        await a.nav(q("/portatil?device=devA"), wait=1.2)
    await asyncio.sleep(1.0)
    s = status()
    hora(f"3) apos 3 reloads do controle A: viewers={s['viewers']} active={s['controllers']['active']} reserved={s['controllers']['reserved']}")
    playerA2 = await a.ev("document.getElementById('player').textContent")
    hora(f"   playerA='{playerA2}' (deve continuar Jogador 1)")

    # ---- 4) Painel /controles lista 2 aparelhos ----
    page = urllib.request.urlopen(q("/controles"), timeout=5).read().decode()
    hora(f"4) /controles: devA={'devA' in page or 'Jogador 1' in page} devB={'Tela' in page}")

    # ---- 5) Rename + Identify + Kick via HTTP ----
    post("/controles/rename", {"id": "devB", "name": "Sala"})
    post("/controles/identify", {"id": "devA"})
    await asyncio.sleep(0.5)
    ident = await a.ev("(function(){var e=document.getElementById('identify');return e?e.textContent:'none';})()")
    hora(f"5) identify no A -> '{ident}'")
    page = urllib.request.urlopen(q("/controles"), timeout=5).read().decode()
    hora(f"   /controles apos rename: 'Sala' presente={'Sala' in page}")
    post("/controles/kick", {"id": "devA"})
    await asyncio.sleep(0.8)
    s = status()
    hora(f"   apos kick A: viewers={s['viewers']} active={s['controllers']['active']}")

    # ---- 6) Fechar B -> viewers volta para 0 ----
    await b.close()
    await asyncio.sleep(1.5)
    s = status()
    hora(f"6) apos fechar B: viewers={s['viewers']} active={s['controllers']['active']} reserved={s['controllers']['reserved']}")

    await a.close()


asyncio.run(main())
