# browser-ai

Dê ao seu agente de IA um **navegador** de verdade pra operar.

`browser-ai` é um **servidor MCP** (stdio, **biblioteca padrão pura — zero dependências**) que
deixa um agente de IA (Codex, Claude Code, Cursor…) controlar o Chrome: ler a
página, clicar, digitar, navegar e tirar screenshot. Ele fala **CDP (Chrome
DevTools Protocol)** direto com o navegador — então **não instala extensão
nenhuma**: você sobe o Chrome com a porta de depuração e o agente assume a aba.

## Como funciona

```
Agente (Codex)  ──MCP/stdio──►  web-ai  ──CDP──►  Chrome (porta 9222)  ──►  a aba
```

## Passo 1 — subir o Chrome com a porta de debug

Use um perfil **dedicado** (mantém a automação longe do seu perfil do dia a dia):

```bash
google-chrome --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.browser-ai-profile"
```

(Qualquer navegador Chromium serve: chromium, brave, edge. Para rodar sem janela,
acrescente `--headless=new`.)

## Passo 2 — conferir a conexão

```bash
cd browser-ai
python3 -m webai doctor     # ✓ Chrome reachable ... N tab(s) open
python3 -m webai tabs       # lista as abas abertas
```

## Passo 3 — plugar no Codex

No `~/.codex/config.toml`:

```toml
[mcp_servers.browser-ai]
command = "python3"
args = ["-m", "webai", "mcp"]
# rode o Codex a partir da pasta browser-ai, OU instale com `pip install -e .`
# e use:  command = "web-ai"   args = ["mcp"]
```

Depois de `pip install -e .` o comando `web-ai` fica no PATH e dá para apontar de
qualquer lugar. Em outros clientes MCP o formato é o mesmo:

```json
{ "mcpServers": { "browser-ai": { "command": "web-ai", "args": ["mcp"] } } }
```

## Ferramentas expostas ao agente

| Ferramenta | O que faz |
|---|---|
| `web_tabs` | Lista as abas (id, título, url). |
| `web_new_tab` | Abre uma aba (opcionalmente numa URL). |
| `web_close_tab` | Fecha uma aba pelo id. |
| `web_navigate` | Navega uma aba para uma URL. |
| `web_read` | Texto visível da página (+ título e url). |
| `web_html` | `outerHTML` da página ou de um seletor CSS. |
| `web_click` | Clica por seletor CSS **ou** pelo texto visível. |
| `web_type` | Digita num campo (seletor), com `submit` opcional. |
| `web_wait` | Espera um elemento aparecer. |
| `web_eval` | Roda JavaScript na página (a ferramenta "coringa"). |
| `web_screenshot` | PNG da aba (para IAs com visão). |
| `web_download_pdf` | Salva a página atual como **PDF** e retorna o caminho do arquivo. |
| `web_switch_tab` | Traz uma aba para frente / torna ativa, pelo id. |
| `web_back` | Volta uma entrada no histórico da aba (botão Voltar). |
| `web_forward` | Avança uma entrada no histórico da aba (botão Avançar). |

Toda ferramenta aceita `target_id` para escolher a aba; sem ele, usa a primeira.

## Segurança

- `web_eval` executa JS arbitrário na página — é o que torna o agente realmente
  útil, mas é poderoso. Desligue com `WEBAI_NO_EVAL=1` (ou `WEBAI_ALLOW_EVAL=0`)
  em máquina compartilhada/sensível.
- Use **sempre** um `--user-data-dir` dedicado. Não aponte para o perfil onde
  você fica logado em banco, e-mail etc., a menos que seja proposital.
- A porta de debug (9222) dá controle total do navegador a quem alcançá-la.
  Deixe em `127.0.0.1` e não exponha na rede.

## Variáveis de ambiente

| Variável | Padrão | Para quê |
|---|---|---|
| `WEBAI_HOST` | `127.0.0.1` | Host da porta de debug. |
| `WEBAI_PORT` | `9222` | Porta de debug do Chrome. |
| `WEBAI_NO_EVAL` | — | `1` desliga o `web_eval`. |
| `WEBAI_DOWNLOAD_DIR` | `~/Downloads` | Pasta padrão dos PDFs de `web_download_pdf`. |
| `WEBAI_DEBUG` | — | `1` inclui traceback nos erros de ferramenta. |

## Testes

```bash
python3 -m pytest -q      # 11 testes, sem precisar de Chrome aberto
```

## Licença

MIT.
