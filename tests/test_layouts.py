import asyncio
import json
import os
import subprocess
import tempfile
import time
import urllib.request

import websockets

CHROME = "google-chrome"
PORT = 9222
ROOT = os.environ.get("MANETE_URL", "http://127.0.0.1:8081").rstrip("/")


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
BASE = ROOT + "/controle?" + (("token=" + TOKEN + "&") if TOKEN else "") + "layout="

MEASURE = r"""
(() => {
  const sel = '.btn,.small,.abtn,.pedal,.stick,.lookpad,.wheel,.radialabxy';
  const els = [...document.querySelectorAll(sel)];
  const rects = els.map(e => {
    const r = e.getBoundingClientRect();
    return { c: (e.className||'').toString().slice(0,18), w: Math.round(r.width), h: Math.round(r.height),
             x: Math.round(r.x), y: Math.round(r.y), right: Math.round(r.right), bottom: Math.round(r.bottom) };
  });
  const vw = innerWidth, vh = innerHeight;
  const off = rects.filter(r => r.x < -1 || r.y < -1 || r.right > vw + 1 || r.bottom > vh + 1)
                   .map(r => r.c + '@' + r.x + ',' + r.y + ' ' + r.w + 'x' + r.h);
  const overlaps = [];
  for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) {
    const a = rects[i], b = rects[j];
    const ox = Math.min(a.right, b.right) - Math.max(a.x, b.x);
    const oy = Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y);
    if (ox > 2 && oy > 2) overlaps.push(a.c + ' x ' + b.c + ' (' + ox + 'x' + oy + ')');
  }
  return JSON.stringify({ count: els.length, off, overlaps });
})()
"""


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
         f"--remote-debugging-port={PORT}", f"--user-data-dir={os.path.join(tempfile.gettempdir(), 'manete-chrome-layouts')}", "about:blank"],
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

        await cmd("Page.enable")
        await cmd("Runtime.enable")

        viewports = [("retrato", 390, 844), ("paisagem", 844, 390)]
        layouts = ["classic", "precision", "fps", "racing", "fight", "retro"]

        for name, w, h in viewports:
            await cmd("Emulation.setDeviceMetricsOverride",
                      {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": True})
            print(f"\n===== {name} {w}x{h} =====")
            for lay in layouts:
                await cmd("Page.navigate", {"url": BASE + lay})
                await asyncio.sleep(0.8)
                res = await cmd("Runtime.evaluate",
                                {"expression": MEASURE, "returnByValue": True})
                val = res.get("result", {}).get("result", {}).get("value")
                try:
                    data = json.loads(val)
                except Exception:
                    print(f"  {lay:10} -> ERRO ao medir: {res}")
                    continue
                status = "OK" if not data["off"] and not data["overlaps"] else "PROBLEMA"
                print(f"  {lay:10} [{status}] controles={data['count']}")
                for o in data["off"]:
                    print(f"      fora da tela: {o}")
                for o in data["overlaps"]:
                    print(f"      sobreposicao: {o}")

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


asyncio.run(main())
