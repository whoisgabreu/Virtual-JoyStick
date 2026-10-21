# Testes

Testes de integração via **Chrome headless** + **CDP** (`Input.dispatchTouchEvent`),
mais o próprio servidor. Eles sobem um Chrome (`google-chrome`) e simulam toques
reais na página, conferindo a geometria dos layouts e as mensagens enviadas ao
servidor.

## Pré-requisitos

- `google-chrome` no `PATH`.
- Servidor rodando (o alvo é `MANETE_URL`, padrão `http://127.0.0.1:8081`).
- Se o servidor exigir token, exporte `MANETE_TOKEN=<token>` (é anexado às URLs).
- **Sem `DISPLAY`** apontando para a sessão do servidor: rode com `env -u DISPLAY`
  (ou garanta que o `DISPLAY` não é o `:0` que o servidor captura — senão o Chrome
  headless pode quebrar o SHM/X11 da captura).

## Como rodar

```bash
# servidor em primeiro plano (ou já rodando)
.venv/bin/python server.py

# em outro terminal
MANETE_URL=http://127.0.0.1:8081 .venv/bin/python tests/test_layouts.py
MANETE_URL=http://127.0.0.1:8081 .venv/bin/python tests/test_inputs.py
MANETE_URL=http://127.0.0.1:8081 .venv/bin/python tests/test_select.py
MANETE_URL=http://127.0.0.1:8081 .venv/bin/python tests/test_editor.py
MANETE_URL=http://127.0.0.1:8081 .venv/bin/python tests/test_sessions.py
```

## O que cada um cobre

| Arquivo | Cobertura |
|---------|-----------|
| `test_layouts.py` | Geometria dos 6 layouts (sem sobreposição / fora da tela) em retrato e paisagem |
| `test_inputs.py` | Resposta dos analógicos (deadzone/curva/ganho), pad radial ABXY e combo multi-toque |
| `test_select.py` | Seleção e persistência de layout (`?layout=` > `localStorage`) e links do painel |
| `test_editor.py` | Editor de layout: entrar/sair, mover, redimensionar (alça), persistência local e restaurar |
| `test_sessions.py` | UUID por aparelho, número de Jogador estável, `/tela` view-only e contagem de viewers sem vazamento; ações de gerenciar (renomear/identificar/kick) |

> Os testes usam portas de debug do Chrome (9222/9223/9224/9226/9301/9302); não rode
> dois ao mesmo tempo se colidirem nessas portas.
