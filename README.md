# Controle Remoto (WebRTC + Gamepad virtual)

Controla o PC (gamepad virtual Xbox 360) e espelha a tela pelo celular, usando **WebRTC (H.264)** para o vídeo e **WebSocket** para os controles. Vários celulares podem se conectar; cada um pega um controle independente (até 4, limite do XInput).

## Requisitos

- Python 3.12 (testado).
- PC do servidor com sessão gráfica (**Linux X11** ou **Windows**) para capturar a tela.
- Celular na mesma rede do PC (até 4 controles simultâneos).

## Linux — ações necessárias

### O que instalar

```bash
sudo apt update
sudo apt install -y python3.12-venv \
  libevdev2 libx11-6 libxfixes3 libxrandr2 \
  libxcb1 libxcb-randr0 libxcb-render0 libxcb-shm0 libxcb-xfixes0
# opcional: janela /servidor em modo app
sudo apt install -y google-chrome-stable   # ou chromium
```

### Acesso ao `/dev/uinput` (gamepad virtual)

```bash
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf

sudo tee /etc/udev/rules.d/99-uinput.rules >/dev/null <<'EOF'
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
EOF

sudo udevadm control --reload-rules && sudo udevadm trigger
```

Relogue na sessão e teste:

```bash
.venv/bin/python -c "import vgamepad as vg; print(vg.VX360Gamepad())"
```

### Instalar e rodar

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# precisa de uma sessão X11 (não Wayland puro)
DISPLAY=:0 .venv/bin/python server.py
# ou explicitamente:
HOST=0.0.0.0 PORT=8081 DISPLAY=:0 .venv/bin/python server.py
```

Se houver firewall: `sudo ufw allow 8081/tcp`.

Como serviço de usuário (systemd), com o display correto:

```bash
systemd-run --user --unit=manete --collect \
  --working-directory="$PWD" \
  --setenv=DISPLAY=:0 \
  "$PWD/.venv/bin/python" server.py
```

Logs: `journalctl --user -u manete -f` · Parar: `systemctl --user stop manete`.

## Windows — o que instalar

### O que instalar

1. **Python 3.12** (python.org) — marque **"Add python.exe to PATH"**.
2. Atualize o pip:
   ```bat
   py -m pip install -U pip setuptools wheel
   ```
3. Crie o ambiente e instale as dependências:
   ```bat
   py -3.12 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
   O `vgamepad` baixa e executa o instalador do driver **ViGEmBus** (aceite o UAC).
   Se a instalação automática falhar, instale manualmente em
   <https://github.com/ViGEm/ViGEmBus/releases> e rode o `pip install` de novo.

> No Windows **não** são necessários `libX11`, `libevdev` nem `/dev/uinput`.

### Rodar

```bat
.venv\Scripts\python server.py

REM porta/host, se quiser mudar:
set HOST=0.0.0.0
set PORT=8081
.venv\Scripts\python server.py
```

- No Firewall do Windows, permita o **Python** em **redes privadas** (ou libere a porta **8081**).
- Acesse no celular: `http://<IP-do-PC>:8081/`.

### Observações (Windows)

- A captura usa **GDI** (via `mss`), sem `DISPLAY`/X11.
- O **libx264** vem nos wheels do PyAV; `encoder: auto` ainda tenta NVENC/QSV/AMF.
- A tela `/servidor` abre no **navegador padrão**. Para uma janela dedicada, defina
  `set MONITOR_BROWSER=C:\Program Files\Google\Chrome\Application\chrome.exe`.
  Para não abrir: `set OPEN_MONITOR=0`.
- Os testes em `tests/` são de **Linux** (Chrome headless + X11).

## Rotas

| Rota | Descrição |
|------|-----------|
| `/` | Painel (escolha de modo + layout, status ao vivo) |
| `/controle` | Apenas o controle (não abre vídeo; consome 1 slot de gamepad) |
| `/tela` | Apenas o espelhamento (não consome slot de controle) |
| `/portatil` | Controle + tela no mesmo aparelho |
| `/config` | Configuração (largura, fps, bitrate, encoder, preset, token) |
| `/controles` | Gerenciar controles (listar/renomear/identificar/desconectar aparelhos) |
| `/servidor` | Tela do servidor (endereço + QR + contadores ao vivo); aberta automaticamente no start |
| `/servidor/qr.png` | QR code do endereço de acesso (PNG, gerado offline) |
| `/status` | JSON com resumo da config e slots de controle (sem o token) |
| `/healthz` | JSON de saúde (`status`, `encoder`, `viewers`, `controllers_active`), sem token |
| `/favicon.ico` | Resposta vazia (204) |
| `/ws` | WebSocket de controle (heartbeat) |
| `/offer` | Handshake WebRTC (não-trickle) |

As páginas ficam em `templates/` (`menu.html`, `config.html`, `controles.html`, `servidor.html`), com marcadores `{{chave}}` substituídos no servidor.

Acesse do celular: `http://<IP-do-PC>:8081/` (o IP e o QR aparecem em `/servidor`).

## Tela do servidor

A rota **`/servidor`** é um painel para o monitor do PC: mostra o **endereço de
acesso**, um **QR code** (para abrir no celular pela câmera) e contadores atualizados
a cada 2s via `/status` — **Telas conectadas** (`viewers`), **Controles**
(`active`/`total`, com reservados) e a config de vídeo. Ao iniciar o servidor ela é
aberta automaticamente: no **Linux** numa janela dedicada (Chrome `--app`) e no
**Windows** no navegador padrão. Controlada por `OPEN_MONITOR` e `MONITOR_BROWSER`.
O IP exibido vem de `PUBLIC_HOST` ou é detectado automaticamente.

## Layouts de controle

O `index.html` tem um motor de layouts: cada um monta seu overlay e compartilha os mesmos widgets (analógico, touch pad, pad radial ABXY, d-pad, botões, gatilhos, volante). A seleção é por aparelho, com precedência **`?layout=` na URL > `localStorage` (`manete.layout`) > `classic`**, e pode ser trocada em jogo pelo botão **Layout** (canto superior).

| id | Layout | Uso |
|----|--------|-----|
| `classic` | Clássico | 2 analógicos + d-pad + A/B/X/Y |
| `precision` | Precisão (Touch) | touch pad esquerdo (preciso) + pad radial ABXY + d-pad + mini analógico direito |
| `fps` | FPS | mover + área de mira (right stick) + ações grandes (atirar/mira/pular/recarregar) |
| `racing` | Corrida | volante touch + acelerar/frear + marchas + freio de mão (+ tilt) |
| `fight` | Luta/Arcade | d-pad grande + 6 botões |
| `retro` | Plataforma/Retrô | d-pad grande + 2 botões |

No layout `precision`, o pad radial ABXY é um **círculo com 4 setores de 25%** (90° cada, divisórias nas diagonais de 45°) na disposição normal — **Y topo, B direita, A baixo, X esquerda** — e cada zona dispara **o botão correspondente** (A→A, B→B, X→X, Y→Y). Suporta **multi-toque**, então dá para segurar duas zonas ao mesmo tempo (ex.: Y+B). No `fps`, arrastar na área de mira controla o analógico **direito** (não é mouse). Em `racing`, o botão **Tilt** usa o giroscópio **se o navegador permitir** (requer HTTPS/contexto seguro; no LAN via HTTP normalmente não funciona, então o volante touch é o padrão).

### Resposta dos analógicos

Todos os controles analógicos (analógicos, touch pad, área de mira e volante) usam uma resposta com **deadzone + curva + ganho** (constantes `STICK` em `index.html`):

```js
const STICK = { deadzone: 0.05, curve: 0.7, gain: 1.3 };
```

Ordem: `deadzone` (ignora toques muito perto do centro) → `curve` (expoente; `<1` responde mais cedo) → `gain` (satura a deflexão máxima antes da borda). Com os defaults, ~30% do raio já vira ~0.5 (supera a deadzone típica dos jogos) e a deflexão total chega a ~82% do raio. Ajuste esses três valores para calibrar sensibilidade. Gatilhos/pedais (LT/RT) seguem lineares.

## Editor de layout

Dá para **mover e redimensionar** os elementos do layout direto no celular. Toque no botão **Editar** (ou em "Editar este layout" na folha de layouts) para entrar no modo de edição:

- **Toque** seleciona a unidade (contorno azul + rótulo com o fator de escala).
- **Arraste** move; a **alça** no canto redimensiona (escala proporcional à distância do centro).
- **Restaurar** limpa os ajustes do layout atual; **Concluir** sai da edição.

Enquanto edita, os toques **não** são enviados ao gamepad. Os ajustes valem em retrato e paisagem (offsets em `vmin`) e são salvos **por aparelho**, em `localStorage['manete.edit.<layout>']` como `{ unidade: { x, y, s } }` — cada celular tem o seu. Unidades são os **blocos** (`dpad`, `abxy`, `abxy6`, `radial`, `stick-left/right`, `look`, `wheel`) e cada **botão avulso** (topo `LT/LB/RT/RB/BACK/START`, pedais, botões do FPS/retro, etc.); os botões internos de um bloco (`dpad`/`abxy`) se movem junto com ele.

## Sessões, identidade e gerenciamento

Cada celular gera um **UUID** persistido em `localStorage` (`manete.device`) e o envia como `?device=<uuid>` no `/ws` e no `/offer`. Isso dá dois benefícios:

- **Número de Jogador estável**: o slot é amarrado ao UUID; reconectar/recarregar reusa o **mesmo** número (não vira Jogador 2). O slot fica **reservado por `SLOT_GRACE` s** após o aparelho sair e é liberado depois (ou na hora, pela tela de gerenciar).
- **Contagem de telas correta**: o `pc` de vídeo é fechado quando a WebSocket do aparelho cai. O `/tela` também abre uma WS **view-only** (`?view=1`, não consome slot de gamepad) só para registrar/limpar. Sem isso, o aiortc (que não tem estado `disconnected`) deixava PeerConnections "presos" para sempre e o contador de telas crescia sem parar.

A tela **`/controles`** lista os aparelhos (nome, Jogador/Tela, tempo, vídeo) e permite **Renomear**, **Identificar** (faz o aparelho exibir um aviso e vibrar) e **Desconectar** (fecha WS+pc e libera o slot na hora; o aparelho desconectado não reconecta sozinho). Duas abas no mesmo celular compartilham o UUID (a segunda assume, encerrando a primeira).

## Configuração

Config persistida em `config.json` (**runtime**, fora do versionamento; criado automaticamente a partir de `config.example.json`) e editável por `/config`:

| Campo | Valores | Efeito |
|-------|---------|--------|
| `out_width` | 320–3840 | Resolução/nitidez. Menor = mais fluido. Nunca faz upscale além da tela real. |
| `fps` | 5–120 | Teto de fps (captura + encoder). |
| `bitrate_kbps` | 500–20000 | Qualidade em movimento / banda. |
| `preset` | ultrafast…medium | Preset do x264 (velocidade x qualidade). |
| `encoder` | `libx264` / `auto` / `vaapi` | `libx264` = software; `auto`/`vaapi` tentam hardware e caem para libx264 se indisponível. |
| `token` | texto (máx. 64) | Se preenchido, exige `?token=...` em todas as rotas/WS. Vazio = sem senha. |

Ao salvar, a captura é ajustada ao vivo e os clientes de vídeo são reconectados para aplicar os limites do encoder. O encoder escolhido é registrado no log ("Encoder H.264 ativo: ...").

Variáveis de ambiente:

| Variável | Padrão | Efeito |
|----------|--------|--------|
| `HOST` | `0.0.0.0` | Interface de bind. |
| `PORT` | `8081` | Porta HTTP. |
| `LOG_LEVEL` | `INFO` | Nível de log. |
| `PUBLIC_HOST` | (auto) | IP/host exibido em `/servidor` e no QR (senão detecta o IP da LAN). |
| `OPEN_MONITOR` | (auto) | `1`/`0`: abrir a tela `/servidor` no start (auto = ligado com `DISPLAY`/Windows). |
| `MONITOR_BROWSER` | (auto) | Binário para abrir a tela no Linux (`--app`; padrão `google-chrome`). No Windows, se vazio, usa o navegador padrão. |
| `MONITOR` | `1` | `1` = monitor primário; `0` = todos. |
| `MAX_CONTROLLERS` | `4` | Limite de gamepads (1–4). |
| `WS_IDLE_TIMEOUT` | `40` | Segundos sem ping até liberar o slot. |
| `SLOT_GRACE` | `60` | Segundos que o slot fica reservado ao aparelho offline. |
| `VAAPI_DEVICE` | `/dev/dri/renderD128` | Dispositivo de render p/ VAAPI. |

> **Nota sobre hardware**: o encoder de hardware depende do build do PyAV/FFmpeg. Neste ambiente o `h264_vaapi` não existe e QSV/NVENC/AMF não inicializam, então `auto`/`vaapi` caem para `libx264`. Em um build com VAAPI funcional, `vaapi`/`auto` passam a usá-lo automaticamente.

## Arquitetura

- **Captura** (`ScreenCapture`): um loop captura a tela e publica frames em filas — uma cópia independente por espectador (evita compartilhar o mesmo `VideoFrame` entre threads, que causava SEGV). Auto-recupera o `mss` se a captura X11 falhar.
- **Vídeo**: cada PeerConnection recebe um `ConsumerTrack` próprio; o aiortc encoda **H.264 por espectador** via `_ConfiguredH264Encoder` (preset configurável; tentativa de hardware com fallback para software).
- **Controle** (`ControllerManager` + `sessions`): pool de até 4 gamepads virtuais; o slot é amarrado ao UUID do aparelho (número de Jogador estável) e reservado por `SLOT_GRACE` s após a saída. O `/ws` usa heartbeat (fecha conexões inativas após `WS_IDLE_TIMEOUT`) e é o batimento que também encerra o `pc` de vídeo do aparelho.
- **UI** (`index.html`): motor de **layouts de controle** (registry `LAYOUTS` + widgets reutilizáveis), modo conforme a rota (`/controle`, `/tela`, `/portatil`), reconexão automática de vídeo/WS, overlay de erro de vídeo e ping WS a cada 15s.

## Limitações / caveats

- Encode é **software x264** e **cada espectador re-encoda** — subir fps/resolução pesa mais com vários celulares.
- O aiortc limita fps a 30 por padrão; o servidor ajusta esse teto conforme `fps` do config.
- **Não** rode Chrome headless apontando para `DISPLAY=:0` enquanto o servidor captura: pode quebrar a conexão X11/SHM. (A captura se auto-recupera recriando o `mss`.)

## Testes

Testes de integração via Chrome headless + CDP (geometria dos layouts, resposta dos
analógicos, seleção de layout, editor e sessões/UUID). Veja `tests/README.md`. Alvo padrão:
`http://127.0.0.1:8081` (ajuste com `MANETE_URL`; se houver token, defina `MANETE_TOKEN`).

```bash
.venv/bin/python server.py &
MANETE_URL=http://127.0.0.1:8081 .venv/bin/python tests/test_layouts.py
MANETE_URL=http://127.0.0.1:8081 .venv/bin/python tests/test_inputs.py
MANETE_URL=http://127.0.0.1:8081 .venv/bin/python tests/test_select.py
MANETE_URL=http://127.0.0.1:8081 .venv/bin/python tests/test_editor.py
MANETE_URL=http://127.0.0.1:8081 .venv/bin/python tests/test_sessions.py
```
# Virtual-JoyStick
