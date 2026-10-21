import asyncio
import io
import json
import logging
import os
import socket
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from fractions import Fraction
from html import escape
from pathlib import Path
from urllib.parse import parse_qs

import mss
import numpy as np
import qrcode
from PIL import Image

import av
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.codecs import get_encoder as _default_get_encoder
from aiortc.codecs import h264 as _h264
import aiortc.rtcrtpsender as _rtcrtpsender
from av import VideoFrame

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

import uvicorn
import vgamepad as vg

BASE_DIR = Path(__file__).parent
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8081"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "")  # IP/host mostrado na tela /servidor (default: LAN)
OPEN_MONITOR = os.environ.get("OPEN_MONITOR", "").strip()  # "1"/"0"; vazio = auto (se houver DISPLAY)
MONITOR_BROWSER = os.environ.get("MONITOR_BROWSER")  # None = padrão por SO (Linux: google-chrome; Windows: navegador padrão)
MONITOR = int(os.environ.get("MONITOR", "1"))       # 1 = primário (eDP-1); 0 = todos
MAX_CONTROLLERS = int(os.environ.get("MAX_CONTROLLERS", "4"))  # limite Xbox 360/XInput
WS_IDLE_TIMEOUT = float(os.environ.get("WS_IDLE_TIMEOUT", "40"))  # s sem ping -> libera slot
SLOT_GRACE = float(os.environ.get("SLOT_GRACE", "60"))  # s que o slot fica reservado ao aparelho offline
RESAMPLE = Image.Resampling.NEAREST  # NEAREST (~2ms) p/ 30fps; BILINEAR melhor qualidade (~40ms)

CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_CONFIG = {
    "out_width": 960,
    "fps": 30,
    "bitrate_kbps": 3000,
    "preset": "veryfast",
    "encoder": "libx264",   # libx264 | auto | vaapi
    "token": "",            # vazio = sem autenticação
}
INT_LIMITS = {"out_width": (320, 3840), "fps": (5, 120), "bitrate_kbps": (500, 20000)}
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"]
ENCODERS = ["libx264", "auto", "vaapi"]
TOKEN_MAXLEN = 64


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text())
            for key, (low, high) in INT_LIMITS.items():
                if key in data:
                    cfg[key] = clamp(int(data[key]), low, high)
            if data.get("preset") in PRESETS:
                cfg["preset"] = data["preset"]
            if data.get("encoder") in ENCODERS:
                cfg["encoder"] = data["encoder"]
            if isinstance(data.get("token"), str):
                cfg["token"] = data["token"][:TOKEN_MAXLEN]
        except Exception as e:
            logging.warning("config.json inválido (%s); usando padrões", e)
    else:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    logging.info("Configuração salva: %s", cfg)


CONFIG = load_config()
OUT_WIDTH = CONFIG["out_width"]
CAPTURE_FPS = CONFIG["fps"]


# ---------------------------------------------------------------------------
# Captura de tela: uma fonte -> cópia independente por consumidor
# (evita compartilhar o mesmo VideoFrame entre senders/threads, que causava SEGV)
# ---------------------------------------------------------------------------
class ScreenCapture:
    def __init__(self, monitor: int, out_width: int, fps: int):
        self._monitor = monitor
        self._out_width = out_width
        self._fps = fps
        self._sct = mss.MSS()
        self._mon = self._sct.monitors[monitor]
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Future | None = None

    @property
    def time_base(self) -> Fraction:
        return Fraction(1, self._fps)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        try:
            self._sct.close()
        except Exception:
            pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _grab(self) -> np.ndarray:
        shot = self._sct.grab(self._mon)
        target = self._out_width
        if target and target >= shot.width:
            target = None  # nunca faz upscale
        if target and target != shot.width:
            img = Image.frombytes("RGB", (shot.width, shot.height), shot.bgra, "raw", "BGRX")
            h = round(shot.height * target / shot.width)
            img = img.resize((target, h), RESAMPLE)
            return np.asarray(img)
        # sem resize: BGRA -> RGB via numpy (evita PIL a cada frame)
        arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        return np.ascontiguousarray(arr[:, :, 2::-1])  # H x W x 3 (rgb24)

    async def _run(self) -> None:
        loop = asyncio.get_event_loop()
        pts = 0
        next_t = loop.time()
        while True:
            try:
                arr = await loop.run_in_executor(None, self._grab)
            except Exception as e:
                logging.warning("Falha na captura: %s", e)
                try:
                    self._sct.close()
                except Exception:
                    pass
                await asyncio.sleep(0.5)
                try:
                    self._sct = mss.MSS()
                    self._mon = self._sct.monitors[self._monitor]
                    logging.info("Captura reiniciada com sucesso")
                except Exception as e2:
                    logging.warning("Falha ao reiniciar captura: %s", e2)
                    await asyncio.sleep(1)
                continue

            for q in list(self._subscribers):
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait((arr, pts))
            pts += 1

            next_t += 1 / self._fps
            lag = next_t - loop.time()
            if lag > 0:
                await asyncio.sleep(lag)
            else:
                next_t = loop.time()


capture = ScreenCapture(MONITOR, OUT_WIDTH, CAPTURE_FPS)


class ConsumerTrack(VideoStreamTrack):
    """Track independente por PeerConnection, com sua própria cópia do frame."""

    kind = "video"

    def __init__(self, source: ScreenCapture):
        super().__init__()
        self._source = source
        self._queue = source.subscribe()
        self._time_base = source.time_base

    async def recv(self) -> VideoFrame:
        arr, pts = await self._queue.get()
        frame = VideoFrame.from_ndarray(arr.copy(), format="rgb24")
        frame.pts = pts
        frame.time_base = self._time_base
        return frame

    def stop(self) -> None:
        self._source.unsubscribe(self._queue)
        super().stop()


pcs: set[RTCPeerConnection] = set()


class _ConfiguredH264Encoder(_h264.H264Encoder):
    """H264Encoder do aiortc com preset do x264 configurável e, opcionalmente,
    aceleração por hardware (VAAPI/QSV/NVENC/AMF). Se o hardware não estiver
    disponível, cai automaticamente para o libx264 em software."""

    preset = "veryfast"
    _active = "libx264"

    @classmethod
    def configure(cls, preset: str, encoder: str) -> None:
        cls.preset = preset if preset in PRESETS else "veryfast"
        previous = cls._active
        cls._active = cls._select_encoder(encoder)
        if cls._active != previous:
            logging.info("Encoder H.264 ativo: %s (preset=%s)", cls._active, cls.preset)

    @classmethod
    def _select_encoder(cls, requested: str) -> str:
        if requested == "libx264":
            return "libx264"
        if requested == "vaapi":
            candidates = ["h264_vaapi"]
        else:  # auto
            candidates = ["h264_vaapi", "h264_qsv", "h264_nvenc", "h264_amf"]
        for name in candidates:
            if cls._probe(name):
                return name
        logging.warning(
            "Nenhum encoder de hardware utilizável (pedido=%s); usando libx264", requested
        )
        return "libx264"

    @classmethod
    def _probe(cls, name: str) -> bool:
        try:
            codec = cls._build(name, 320, 240, 1_000_000)
            frame = av.VideoFrame(320, 240, codec.pix_fmt)
            frame.pts = 0
            for _ in codec.encode(frame):
                pass
            return True
        except Exception as e:
            logging.debug("Probe do encoder %s falhou: %s", name, e)
            return False

    @classmethod
    def _build(cls, name: str, width: int, height: int, bitrate: int):
        kwargs = {}
        if name == "h264_vaapi":
            from av.codec.hwaccel import HWAccel

            device = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")
            kwargs["hwaccel"] = HWAccel("vaapi", device=device)
            pix_fmt = "vaapi"
        elif name in ("h264_qsv", "h264_nvenc", "h264_amf"):
            pix_fmt = "nv12"
        else:
            pix_fmt = "yuv420p"

        codec = av.CodecContext.create(name, "w", **kwargs)
        codec.width = width
        codec.height = height
        codec.bit_rate = bitrate
        codec.pix_fmt = pix_fmt
        codec.framerate = Fraction(_h264.MAX_FRAME_RATE, 1)
        codec.time_base = Fraction(1, _h264.MAX_FRAME_RATE)
        if name == "libx264":
            codec.options = {"level": "31", "tune": "zerolatency", "preset": cls.preset}
            codec.profile = "Baseline"
        return codec

    def _encode_frame(self, frame, force_keyframe):
        if self.codec and (
            frame.width != self.codec.width
            or frame.height != self.codec.height
            or abs(self.target_bitrate - self.codec.bit_rate) / self.codec.bit_rate > 0.1
        ):
            self.buffer_data = b""
            self.buffer_pts = None
            self.codec = None

        if force_keyframe:
            frame.pict_type = av.video.frame.PictureType.I
        else:
            frame.pict_type = av.video.frame.PictureType.NONE

        if self.codec is None:
            self.codec = self._build(
                self._active, frame.width, frame.height, self.target_bitrate
            )

        data_to_send = b""
        try:
            for package in self.codec.encode(frame):
                data_to_send += bytes(package)
        except Exception as e:
            logging.warning(
                "Falha no encoder %s (%s); migrando para libx264", self._active, e
            )
            _ConfiguredH264Encoder._active = "libx264"
            self.codec = self._build(
                "libx264", frame.width, frame.height, self.target_bitrate
            )
            for package in self.codec.encode(frame):
                data_to_send += bytes(package)

        if data_to_send:
            yield from self._split_bitstream(data_to_send)


def _configured_get_encoder(codec):
    """Substitui o seletor de encoder do aiortc para usar o H264 configurável."""
    if codec.mimeType.lower() == "video/h264":
        return _ConfiguredH264Encoder()
    return _default_get_encoder(codec)


_rtcrtpsender.get_encoder = _configured_get_encoder


def apply_config(cfg: dict) -> None:
    """Aplica a configuração atual (captura ao vivo + limites/encoder do aiortc)."""
    capture._out_width = cfg["out_width"]
    capture._fps = cfg["fps"]
    _h264.MAX_FRAME_RATE = cfg["fps"]
    _h264.MAX_BITRATE = cfg["bitrate_kbps"] * 1000
    _h264.DEFAULT_BITRATE = cfg["bitrate_kbps"] * 1000
    _ConfiguredH264Encoder.configure(cfg["preset"], cfg["encoder"])
    logging.info("Config aplicada: %s", cfg)


async def drop_peers() -> None:
    """Fecha os PeerConnections atuais para que os clientes reconectem com a nova config."""
    for pc in list(pcs):
        try:
            await pc.close()
        except Exception:
            pass
    pcs.clear()
    for sess in sessions.values():
        sess.pc = None


# ---------------------------------------------------------------------------
# Controle: pool de gamepads virtuais (1 celular = 1 controle)
# ---------------------------------------------------------------------------
BUTTON_MAP = {
    'A': vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
    'B': vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
    'X': vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
    'Y': vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
    'UP': vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
    'DOWN': vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
    'LEFT': vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
    'RIGHT': vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
    'START': vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
    'BACK': vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
    'LB': vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
    'RB': vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
    'L3': vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
    'R3': vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
}


class ControllerManager:
    def __init__(self, max_slots: int):
        self._max = max_slots
        self._gamepads: dict[int, vg.VX360Gamepad] = {}
        self._free = list(range(1, max_slots + 1))

    def acquire(self, preferred: int | None = None) -> int | None:
        if not self._free:
            return None
        if preferred is not None and preferred in self._free:
            slot = preferred
        else:
            slot = self._free[0]
        self._free.remove(slot)
        self._gamepads[slot] = vg.VX360Gamepad()
        return slot

    def get(self, slot: int) -> vg.VX360Gamepad | None:
        return self._gamepads.get(slot)

    def release(self, slot: int) -> None:
        gp = self._gamepads.pop(slot, None)
        if gp is not None:
            try:
                gp.reset()
                gp.update()
            except Exception:
                pass
        if slot not in self._free:
            self._free.append(slot)
            self._free.sort()

    @property
    def used(self) -> int:
        return len(self._gamepads)

    @property
    def total(self) -> int:
        return self._max


controllers = ControllerManager(MAX_CONTROLLERS)


# ---------------------------------------------------------------------------
# Sessões por aparelho (UUID): identidade estável + limpeza do vídeo
# ---------------------------------------------------------------------------
@dataclass
class Session:
    device: str
    name: str = ""
    ws: WebSocket | None = None
    view: bool = False
    slot: int | None = None
    prev_slot: int | None = None
    pc: RTCPeerConnection | None = None
    connected_at: float = 0.0
    last_seen: float = 0.0
    grace_task: asyncio.Task | None = None


sessions: dict[str, Session] = {}
device_names: dict[str, str] = {}  # device -> nome (persiste entre sessões)


def get_session(device: str, name: str = "") -> Session:
    sess = sessions.get(device)
    if sess is None:
        sess = Session(device=device, name=device_names.get(device, ""))
        sessions[device] = sess
    if name:
        sess.name = name
        device_names[device] = name
    return sess


async def close_peer(pc: RTCPeerConnection | None) -> None:
    if pc is None:
        return
    try:
        await pc.close()
    except Exception:
        pass
    pcs.discard(pc)


def schedule_release(sess: Session) -> None:
    """Libera o slot de um aparelho que ficou offline por SLOT_GRACE segundos."""
    if sess.slot is None:
        return
    if sess.grace_task and not sess.grace_task.done():
        sess.grace_task.cancel()

    async def _release() -> None:
        try:
            await asyncio.sleep(SLOT_GRACE)
        except asyncio.CancelledError:
            return
        if sess.ws is not None:
            return
        if sess.slot is not None:
            controllers.release(sess.slot)
            logging.info("Slot %d liberado (aparelho %s offline)", sess.slot, sess.device[:8])
            sess.prev_slot = sess.slot
            sess.slot = None
        if sess.ws is None:
            sessions.pop(sess.device, None)

    sess.grace_task = asyncio.ensure_future(_release())


# ---------------------------------------------------------------------------
# Endereço de acesso / tela do servidor
# ---------------------------------------------------------------------------
def lan_ip() -> str:
    """IP/host mostrado na tela /servidor: PUBLIC_HOST > bind != 0.0.0.0 > IP da LAN."""
    if PUBLIC_HOST:
        return PUBLIC_HOST
    if HOST not in ("0.0.0.0", "::", ""):
        return HOST
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))  # não envia pacotes; só resolve a rota
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return "127.0.0.1"


def lan_url(path: str = "/") -> str:
    return f"http://{lan_ip()}:{PORT}{path}"


def auth_qs() -> str:
    token = CONFIG.get("token")
    return f"?token={token}" if token else ""


def open_monitor_window() -> None:
    """Abre /servidor na máquina do servidor (janela Chrome --app, ou navegador padrão no Windows)."""
    if OPEN_MONITOR == "0":
        return
    if not OPEN_MONITOR and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or os.name == "nt"
    ):
        return  # headless/systemd sem sessão gráfica
    url = f"http://127.0.0.1:{PORT}/servidor{auth_qs()}"
    try:
        if os.name == "nt" and not MONITOR_BROWSER:
            os.startfile(url)  # navegador padrão
        else:
            subprocess.Popen(
                [MONITOR_BROWSER or "google-chrome", f"--app={url}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        logging.info("Tela do servidor aberta: %s", url)
    except Exception as exc:
        logging.warning("Não foi possível abrir a tela do servidor: %s", exc)


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
async def _open_monitor_soon() -> None:
    await asyncio.sleep(0.8)  # dá tempo do uvicorn começar a ouvir
    open_monitor_window()


@asynccontextmanager
async def lifespan(app: FastAPI):
    apply_config(CONFIG)
    capture.start()
    asyncio.ensure_future(_open_monitor_soon())
    logging.info("Servidor pronto em %s (encoder=%s)", lan_url("/"), _ConfiguredH264Encoder._active)
    try:
        yield
    finally:
        logging.info("Encerrando: fechando peers e captura...")
        await asyncio.gather(*[pc.close() for pc in list(pcs)], return_exceptions=True)
        pcs.clear()
        await capture.stop()


app = FastAPI(lifespan=lifespan)

UI_FILE = BASE_DIR / "index.html"
TEMPLATES_DIR = BASE_DIR / "templates"
PROTECTED_PATHS = {
    "/", "/config", "/controle", "/tela", "/portatil", "/offer",
    "/controles", "/controles/kick", "/controles/rename", "/controles/identify",
    "/servidor", "/servidor/qr.png",
}


def render_template(name: str, **values) -> str:
    """Renderização mínima: substitui marcadores {{chave}} (evita braces do CSS)."""
    html = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    for key, value in values.items():
        html = html.replace("{{" + key + "}}", str(value))
    return html


def _select_options(values, current: str) -> str:
    return "".join(
        '<option value="{0}"{1}>{0}</option>'.format(
            value, " selected" if value == current else ""
        )
        for value in values
    )


def config_html(cfg: dict, saved: bool = False) -> str:
    notice = '<p class="ok">Configuracao salva.</p>' if saved else ""
    return render_template(
        "config.html",
        notice=notice,
        out_width=cfg["out_width"],
        fps=cfg["fps"],
        bitrate_kbps=cfg["bitrate_kbps"],
        token=cfg["token"],
        encoder_options=_select_options(ENCODERS, cfg["encoder"]),
        preset_options=_select_options(PRESETS, cfg["preset"]),
    )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    token = CONFIG.get("token")
    if token and request.url.path in PROTECTED_PATHS:
        supplied = request.query_params.get("token") or request.headers.get("x-token")
        if supplied != token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/healthz")
async def healthz():
    active = sum(1 for s in sessions.values() if s.ws is not None and s.slot is not None)
    return JSONResponse(
        {
            "status": "ok",
            "encoder": _ConfiguredH264Encoder._active,
            "viewers": len(pcs),
            "controllers_active": active,
        }
    )


@app.get("/status")
async def status():
    safe_cfg = {k: v for k, v in CONFIG.items() if k != "token"}
    safe_cfg["token_enabled"] = bool(CONFIG.get("token"))
    active = sum(1 for s in sessions.values() if s.ws is not None and s.slot is not None)
    used = controllers.used
    return JSONResponse(
        {
            "config": safe_cfg,
            "controllers": {
                "used": active,
                "total": controllers.total,
                "free": controllers.total - used,
                "active": active,
                "reserved": max(0, used - active),
            },
            "viewers": len(pcs),
        }
    )


@app.get("/")
async def menu():
    return HTMLResponse(render_template("menu.html"))


@app.get("/servidor")
async def servidor():
    return HTMLResponse(
        render_template(
            "servidor.html",
            access_url=lan_url("/") + auth_qs(),
            host=lan_ip(),
            port=PORT,
            qr_src="/servidor/qr.png" + auth_qs(),
        )
    )


@app.get("/servidor/qr.png")
async def servidor_qr():
    img = qrcode.make(lan_url("/") + auth_qs(), box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/config")
async def config_get():
    return HTMLResponse(config_html(CONFIG))


@app.post("/config")
async def config_post(request: Request):
    form = parse_qs((await request.body()).decode(), keep_blank_values=True)
    for key, (low, high) in INT_LIMITS.items():
        try:
            CONFIG[key] = clamp(int(form.get(key, [CONFIG[key]])[0]), low, high)
        except (TypeError, ValueError):
            pass
    preset = form.get("preset", [CONFIG["preset"]])[0]
    if preset in PRESETS:
        CONFIG["preset"] = preset
    encoder = form.get("encoder", [CONFIG["encoder"]])[0]
    if encoder in ENCODERS:
        CONFIG["encoder"] = encoder
    CONFIG["token"] = form.get("token", [CONFIG["token"]])[0][:TOKEN_MAXLEN]
    save_config(CONFIG)
    apply_config(CONFIG)
    await drop_peers()
    return HTMLResponse(config_html(CONFIG, saved=True))


def controles_html() -> str:
    now = time.monotonic()
    order = sorted(
        sessions.values(),
        key=lambda s: (s.slot is None, s.slot if s.slot is not None else 99),
    )
    rows = []
    for sess in order:
        online = sess.ws is not None
        if sess.view:
            kind, cls = "Tela", "tela"
        elif sess.slot is not None:
            kind, cls = f"Jogador {sess.slot}", "ctrl"
        else:
            kind, cls = "Espectador", "spec"
        nome = sess.name or "(sem nome)"
        dur = int(max(0, now - sess.connected_at)) if sess.connected_at else 0
        video = "sim" if sess.pc is not None else "nao"
        estado = "online" if online else "em graca"
        dev = escape(sess.device)
        rows.append(f"""
        <div class="row">
          <div class="who">
            <div class="name">{escape(nome)}</div>
            <div class="id">{dev[:8]}</div>
          </div>
          <div class="meta">
            <span class="badge {cls}">{kind}</span>
            <span class="muted">{estado} &middot; {dur}s &middot; video: {video}</span>
          </div>
          <div class="acts">
            <form method="post" action="/controles/rename">
              <input type="hidden" name="id" value="{dev}">
              <input type="text" name="name" maxlength="32" placeholder="nome" value="{escape(sess.name)}">
              <button type="submit">Renomear</button>
            </form>
            <form method="post" action="/controles/identify">
              <input type="hidden" name="id" value="{dev}">
              <button type="submit">Identificar</button>
            </form>
            <form method="post" action="/controles/kick">
              <input type="hidden" name="id" value="{dev}">
              <button type="submit" class="danger">Desconectar</button>
            </form>
          </div>
        </div>""")
    lista = "".join(rows) if rows else '<p class="empty">Nenhum aparelho conectado.</p>'
    return render_template("controles.html", list=lista, total=controllers.total)


def _redirect_controles(request: Request) -> RedirectResponse:
    token = request.query_params.get("token")
    url = "/controles" + (f"?token={token}" if token else "")
    return RedirectResponse(url, status_code=303)


@app.get("/controles")
async def controles():
    return HTMLResponse(controles_html())


@app.post("/controles/rename")
async def controles_rename(request: Request):
    form = parse_qs((await request.body()).decode(), keep_blank_values=True)
    device = form.get("id", [""])[0]
    name = form.get("name", [""])[0].strip()[:32]
    if device:
        device_names[device] = name
        sess = sessions.get(device)
        if sess is not None:
            sess.name = name
    return _redirect_controles(request)


@app.post("/controles/identify")
async def controles_identify(request: Request):
    form = parse_qs((await request.body()).decode(), keep_blank_values=True)
    sess = sessions.get(form.get("id", [""])[0])
    if sess is not None and sess.ws is not None:
        try:
            await sess.ws.send_text(json.dumps({
                "type": "identify",
                "name": sess.name or sess.device[:8],
                "player": sess.slot,
            }))
        except Exception:
            pass
    return _redirect_controles(request)


@app.post("/controles/kick")
async def controles_kick(request: Request):
    form = parse_qs((await request.body()).decode(), keep_blank_values=True)
    device = form.get("id", [""])[0]
    sess = sessions.get(device)
    if sess is not None:
        if sess.grace_task and not sess.grace_task.done():
            sess.grace_task.cancel()
        if sess.ws is not None:
            try:
                await sess.ws.close(code=4002)
            except Exception:
                pass
            sess.ws = None
        if sess.pc is not None:
            await close_peer(sess.pc)
            sess.pc = None
        if sess.slot is not None:
            controllers.release(sess.slot)
            sess.prev_slot = sess.slot
            sess.slot = None
        sessions.pop(device, None)
    return _redirect_controles(request)


@app.get("/controle")
async def controle():
    return FileResponse(UI_FILE)


@app.get("/tela")
async def tela():
    return FileResponse(UI_FILE)


@app.get("/portatil")
async def portatil():
    return FileResponse(UI_FILE)


@app.post("/offer")
async def offer(request: Request):
    try:
        params = await request.json()
        sdp = params["sdp"]
        typ = params["type"]
    except Exception:
        return JSONResponse(
            {"error": "payload inválido; esperado {'sdp': ..., 'type': ...}"},
            status_code=400,
        )
    offer_desc = RTCSessionDescription(sdp=sdp, type=typ)

    device = request.query_params.get("device")

    # Fecha o PeerConnection anterior do mesmo aparelho (reload/reconnect limpa o antigo)
    if device:
        sess = get_session(device)
        if sess.pc is not None:
            await close_peer(sess.pc)
            sess.pc = None

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logging.info("PeerConnection state: %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            pcs.discard(pc)
            if device:
                sess = sessions.get(device)
                if sess is not None and sess.pc is pc:
                    sess.pc = None
            await close_peer(pc)

    pc.addTrack(ConsumerTrack(capture))

    if device:
        get_session(device).pc = pc

    await pc.setRemoteDescription(offer_desc)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    # ICE não-trickle: espera juntar todos os candidatos antes de responder
    for _ in range(100):
        if pc.iceGatheringState == "complete":
            break
        await asyncio.sleep(0.05)

    return JSONResponse(
        {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    )


@app.websocket("/ws")
async def ws_control(websocket: WebSocket):
    token = CONFIG.get("token")
    if token and websocket.query_params.get("token") != token:
        await websocket.close(code=1008)
        return

    device = websocket.query_params.get("device") or f"anon-{uuid.uuid4().hex[:8]}"
    name = (websocket.query_params.get("name") or "").strip()[:32]
    view = websocket.query_params.get("view") == "1"

    await websocket.accept()
    sess = get_session(device, name)

    # Encerra a WS anterior do mesmo aparelho
    if sess.ws is not None and sess.ws is not websocket:
        try:
            await sess.ws.close(code=4001)
        except Exception:
            pass

    # Reconectou a tempo: cancela a liberação pendente do slot
    if sess.grace_task and not sess.grace_task.done():
        sess.grace_task.cancel()
        sess.grace_task = None

    sess.ws = websocket
    sess.view = view
    sess.connected_at = time.monotonic()
    sess.last_seen = sess.connected_at

    slot: int | None = None
    if view:
        if sess.slot is not None:  # virou espectador de tela: não segura slot
            controllers.release(sess.slot)
            sess.prev_slot = sess.slot
            sess.slot = None
    elif sess.slot is not None:
        slot = sess.slot
    else:
        slot = controllers.acquire(preferred=sess.prev_slot)
        sess.slot = slot
        sess.prev_slot = slot

    gamepad = controllers.get(slot) if slot is not None else None

    if view:
        await websocket.send_text(json.dumps({"type": "slot", "player": None, "view": True}))
    elif slot is not None:
        await websocket.send_text(json.dumps({"type": "slot", "player": slot}))
        logging.info("Controle %d conectado (%s)", slot, device[:8])
    else:
        await websocket.send_text(
            json.dumps({"type": "slot", "player": None, "spectator": True})
        )

    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=WS_IDLE_TIMEOUT
                )
            except asyncio.TimeoutError:
                logging.info("Sessão %s inativa (sem ping); encerrando", device[:8])
                break

            sess.last_seen = time.monotonic()
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if gamepad is None:
                continue

            msg_type = data.get("type")

            if msg_type == "ping":
                continue

            if msg_type == "button":
                btn = data.get("button")
                action = data.get("action")
                if btn in BUTTON_MAP:
                    if action == "press":
                        gamepad.press_button(button=BUTTON_MAP[btn])
                    elif action == "release":
                        gamepad.release_button(button=BUTTON_MAP[btn])
                    gamepad.update()

            elif msg_type == "stick":
                x = float(data.get("x", 0.0))
                y = float(data.get("y", 0.0))  # +1 = cima
                ix = max(-32768, min(32767, round(x * 32767)))
                iy = max(-32768, min(32767, round(-y * 32767)))
                if data.get("which") == "left":
                    gamepad.left_joystick(ix, iy)
                else:
                    gamepad.right_joystick(ix, iy)
                gamepad.update()

            elif msg_type == "trigger":
                value = max(0.0, min(1.0, float(data.get("value", 0.0))))
                if data.get("which") == "left":
                    gamepad.left_trigger(round(value * 255))
                else:
                    gamepad.right_trigger(round(value * 255))
                gamepad.update()

    except WebSocketDisconnect:
        pass
    finally:
        if sess.ws is websocket:
            sess.ws = None
        if sess.pc is not None:
            await close_peer(sess.pc)
            sess.pc = None
        schedule_release(sess)
        if slot is not None:
            logging.info("Controle %d desconectado (%s)", slot, device[:8])


# Produção: python server.py  (ou: .venv/bin/uvicorn server:app --host 0.0.0.0 --port 8081)
if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )
