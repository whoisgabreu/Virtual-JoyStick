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


def shape(t):
    dz, c, g = 0.05, 0.7, 1.3
    t = max(0.0, min(1.0, t))
    if t <= dz:
        return 0.0
    t = (t - dz) / (1 - dz)
    return min(1.0, (t ** c) * g)


async def main():
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={os.path.join(tempfile.gettempdir(), 'manete-chrome-inputs')}", "about:blank"],
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

        async def rect(sel):
            v = await ev(f"(()=>{{const e=document.querySelector('{sel}');if(!e)return null;const b=e.getBoundingClientRect();return JSON.stringify({{x:b.x,y:b.y,w:b.width,h:b.height,cx:b.x+b.width/2,cy:b.y+b.height/2}});}})()")
            return json.loads(v) if v else None

        async def send(dtype, points):
            await cmd("Input.dispatchTouchEvent",
                      {"type": dtype, "touchPoints": [{"x": p[0], "y": p[1], "id": p[2]} for p in points]})

        async def dump():
            return json.loads(await ev("JSON.stringify(window.__log||[])"))

        async def reset():
            await ev("window.__log=[]; window.wsSend=(o)=>window.__log.push(o);")

        async def nav(lay):
            await cmd("Page.navigate", {"url": BASE + lay})
            await asyncio.sleep(1.0)

        _uid = [1000]
        def nid():
            _uid[0] += 1
            return _uid[0]

        async def stick_at(sel, fx, fy=0.0):
            r = await rect(sel); rad = r["w"] / 2
            await reset()
            t = nid()
            await send("touchStart", [(r["cx"] + fx * rad, r["cy"] + fy * rad, t)])
            await asyncio.sleep(0.08)
            got = next((m for m in await dump() if m.get("type") == "stick"), None)
            await send("touchEnd", []); await asyncio.sleep(0.05)
            return r, got

        await cmd("Page.enable")
        await cmd("Runtime.enable")
        await cmd("Emulation.setTouchEmulationEnabled", {"enabled": True})
        await cmd("Emulation.setDeviceMetricsOverride",
                  {"width": 844, "height": 390, "deviceScaleFactor": 1, "mobile": True})

        # ============ 1. Resposta do analogico (touch pad do precision) ============
        await nav("precision")
        print("Resposta do analogico (touch pad, precisao):")
        for f in (0.03, 0.30, 0.82):
            r, got = await stick_at('.stick[data-stick="left"]', f)
            exp = shape(f)
            gx = got["x"] if got else None
            ok = gx is not None and abs(gx - exp) < 0.03
            print(f"  f={f:.2f} -> x={gx if gx is None else round(gx,3)} (esp {exp:.3f}) {'OK' if ok else 'FALHOU'}")

        # ============ 2. Pad radial ABXY (zonas unicas) ============
        print("\nPad radial ABXY (zonas):")
        pad = await rect(".radialabxy")
        rp = pad["w"] / 2 * 0.6
        for name, (dx, dy) in {"Y": (0, -1), "B": (1, 0), "A": (0, 1), "X": (-1, 0)}.items():
            await reset()
            t = nid()
            await send("touchStart", [(pad["cx"] + dx * rp, pad["cy"] + dy * rp, t)]); await asyncio.sleep(0.08)
            press = next((m for m in await dump() if m.get("type") == "button" and m.get("action") == "press"), None)
            ok = press and press.get("button") == name
            print(f"  zona {name}: press={press.get('button') if press else None} {'OK' if ok else 'FALHOU'}")
            await send("touchEnd", []); await asyncio.sleep(0.05)

        # ============ 3. Regressao: classic stick ============
        await nav("classic")
        r, got = await stick_at('.stick[data-stick="left"]', 0.30)
        print(f"\nclassic stick f=0.30 -> x={round(got['x'],3) if got else None} (esp {shape(0.30):.3f})")

        # ============ 4. Regressao: volante ============
        await nav("racing")
        w = await rect(".wheel")
        await reset()
        t = nid()
        await send("touchStart", [(w["cx"] + 0.5 * (w["w"] / 2), w["cy"], t)]); await asyncio.sleep(0.08)
        got = next((m for m in await dump() if m.get("type") == "stick"), None)
        print(f"volante f=0.50 -> x={round(got['x'],3) if got else None} (esp {shape(0.50):.3f})")
        await send("touchEnd", []); await asyncio.sleep(0.05)

        # ============ 5. Combo multi-toque (por ultimo) ============
        await nav("precision")
        pad = await rect(".radialabxy")
        rp = pad["w"] / 2 * 0.6
        print("\nCombo Y+B (multi-toque):")
        pY = (pad["cx"], pad["cy"] - rp, nid())
        pB = (pad["cx"] + rp, pad["cy"], nid())
        await reset()
        await send("touchStart", [pY]); await asyncio.sleep(0.05)
        await send("touchStart", [pY, pB]); await asyncio.sleep(0.08)
        presses = sorted(m["button"] for m in await dump() if m.get("type") == "button" and m.get("action") == "press")
        print("  presses:", presses, "OK" if presses == ["B", "Y"] else "FALHOU")
        await send("touchEnd", [pB]); await asyncio.sleep(0.08)
        rel = sorted(m["button"] for m in await dump() if m.get("type") == "button" and m.get("action") == "release")
        print("  apos soltar B (mantendo Y): releases:", rel, "OK" if rel == ["B"] else "FALHOU")

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


asyncio.run(main())
