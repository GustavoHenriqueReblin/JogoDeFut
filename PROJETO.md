# JogoDeFut — Documentação do Projeto

## Visão Geral

App web PWA para assistir futebol ao vivo. Funciona como um intermediário: busca canais de TV (Band Sports, ESPN, Globo, Premiere etc.) via scraping, resolve os streams HLS e os reproduz no browser com Plyr + HLS.js.

**Stack:** Python (Flask) no backend + HTML/JS vanilla no frontend.

---

## Arquitetura

```
browser
  └── player.html (Plyr + HLS.js)
        ├── GET /channels        → lista de canais configurados
        ├── GET /games           → jogos ao vivo (API externa)
        ├── GET /resolve?url=    → dispara resolve assíncrono
        ├── GET /resolve/status  → polling do status do resolve
        ├── GET /stream?url=     → m3u8 proxiado (segmentos reescritos)
        └── GET /proxy/ts?url=   → proxy dos segmentos .ts

app.py (Flask)
  └── scraper.py (Playwright/patchright)
        └── API externa  → retorna a URL do stream HLS
```

---

## Arquivos

| Arquivo | Papel |
|---|---|
| `app.py` | Servidor Flask — rotas, proxy, cache de IPs ativos, scheduler |
| `scraper.py` | Scraping do token Cloudflare Turnstile + chamada à API externa |
| `templates/player.html` | Shell HTML — importa CSS/JS externos, sem lógica inline |
| `static/css/player.css` | Estilos base: body, video-wrap, overlay, info-bar, Plyr overrides |
| `static/css/footer-panel.css` | Estilos do footer panel e seus cards de jogos/canais |
| `static/js/player-core.js` | Classe `PlayerCore` — Plyr, HLS.js, selectChannel, overlay |
| `static/js/footer-panel.js` | Classe `FooterPanel` — painel deslizante de jogos e canais |
| `static/js/app.js` | Init — instancia PlayerCore + FooterPanel, carrega dados da API |
| `stream_log.txt` | Histórico de streams resolvidos (appendado a cada resolve, no `.gitignore`) |
| `manifest.json` | PWA manifest |
| `sw.js` | Service Worker — cache offline dos assets estáticos |
| `requirements.txt` | Dependências Python |
| `static/logos/` | Logos dos canais em `.webp` |
| `static/icons/` | Ícones do PWA (192, 512, maskable) |

---

## Variáveis de Ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `CHANNELS` | Sim | Lista `Nome:URL,Nome:URL` dos canais. URL é a página do player do canal no site cloudflaire. |
| `CLOUDFLAIRE_PLAYERS` | Sim | Mapeamento `host:fonte` — relaciona o domínio do player à fonte usada na API. Ex: `player.exemplo.com:globo` |
| `GAMES_API_URL` | Sim | URL da API externa que retorna os jogos ao vivo em JSON |
| `PROXY_SECRET` | Não | Chave hex para encriptar URLs (gerada automaticamente se ausente) |
| `PORT` | Não | Porta do servidor (padrão: 5000) |
| `ENVIRONMENT` | Não | `PRODUCTION` desliga logs de debug. Qualquer outro valor (padrão `DEVELOPMENT`) habilita logs. |
| `WARMUP_ENABLED` | Não | `true` habilita warmup automático dos canais às 07h e 13h |
| `HEADLESS_DEBUG` | Não | `true` abre o browser visível durante scraping (útil para debug local) |
| `STATUS_TOKEN` | Não | Token de acesso às rotas `/status` e `/status/stream`. Se vazio, rotas ficam abertas. Passar via `?token=X` ou header `Authorization: Bearer X` |

---

## Fluxo de Reprodução

1. Usuário clica em um canal (ou jogo)
2. Player é **mutado imediatamente** — o stream anterior continua em buffer mas sem áudio enquanto o novo carrega
3. Frontend chama `GET /resolve?url=<enc>` — dispara resolve em background thread
4. Frontend faz polling em `GET /resolve/status?url=<enc>` a cada 2s (timeout: 120s)
5. Quando status = `ready`, chama `playStream()` que aponta HLS.js para `GET /stream?url=<enc>`
6. `/stream` chama `resolve_stream()` (que usa o cache), busca o m3u8, reescreve os segmentos para `/proxy/ts?url=<enc_seg>` e devolve o m3u8 modificado
7. HLS.js busca segmentos via `/proxy/ts` que faz proxy transparente
8. Quando `MANIFEST_PARSED` dispara: **desmuta**, esconde overlay, entra em fullscreen, exibe info bar

---

## Scraping (scraper.py)

O scraping é necessário porque os players ficam atrás de Cloudflare Turnstile.

**Fluxo:**
1. `resolve_stream(player_url)` — verifica cache (TTL 12h), adquire lock por URL para evitar scraping paralelo do mesmo canal
2. `_do_resolve()` — extrai `host` e `channel` da URL, consulta `CLOUDFLAIRE_PLAYERS` para saber a `fonte`
3. Chama `_scrape_token()` até 8 vezes com delays progressivos `[3,5,8,10,12,15,20]`s após 404
4. `_scrape_token()` abre Chromium via Playwright (patchright), navega até a URL e captura o token Turnstile por 3 métodos:
   - Interceptação de request (URL com `token=`)
   - Interceptação de response body (JSON com `"token"`)
   - Polling de DOM (`[name="cf-turnstile-response"]`)
5. Com o token, faz `POST` na API externa (`/get_token`) com `{fonte, channel, token}`
6. Retorna `{"streams": [{"url": "<hls_url>", "referer": player_url}]}`

**Proteção contra bloqueio de IP no warmup:** após 3 falhas consecutivas, aguarda 5 minutos antes de continuar.

---

## Monitoramento de Dispositivos Ativos

`_ACTIVE_IPS: dict[str, tuple[float, float]]` — armazena `ip → (first_seen, last_seen)`

- IP é registrado a cada hit em `/stream`
- TTL de 30s — IP some da contagem se não bater stream dentro desse tempo
- Log impresso quando novo IP conecta: `[dd/mm HH:MM:SS] Novo IP (x.x.x.x) conectado. Total ativos: (N).`
- SSE em `/status/stream` notifica em tempo real o número de dispositivos ativos

**GET /status** retorna:
```json
{
  "devices": 2,
  "clients": [
    {"ip": "177.x.x.x", "connected_for": "1h 23m"},
    {"ip": "189.x.x.x", "connected_for": "4m 12s"}
  ]
}
```

`connected_for` usa formato legível: `Xs`, `Xm Ys`, `Xh Ym`

---

## Rotas

| Método | Rota | Descrição |
|---|---|---|
| GET | `/` | Frontend (player.html) |
| GET | `/channels` | Lista de canais com URLs encriptadas |
| GET | `/games` | Jogos ao vivo (proxia GAMES_API_URL, faz match com canais disponíveis) |
| GET | `/resolve?url=` | Inicia resolve assíncrono do stream |
| GET | `/resolve/status?url=` | Retorna `loading \| ready \| error \| unknown` |
| GET | `/stream?url=` | m3u8 com segmentos proxiados |
| GET | `/proxy/ts?url=` | Proxy de segmento .ts |
| GET | `/status` | JSON com dispositivos ativos + IP + tempo conectado |
| GET | `/status/stream` | SSE — emite count de ativos a cada mudança |
| GET | `/manifest.json` | PWA manifest |
| GET | `/sw.js` | Service Worker |
| GET | `/favicon.ico` | Ícone |

---

## Segurança

URLs de canais e segmentos são **encriptadas com AES-256-GCM** (biblioteca `cryptography`) antes de ir ao cliente. A chave é `PROXY_SECRET` (hex 32 chars = 128 bits). Sem essa chave, o cliente não consegue derivar as URLs originais.

---

## Scheduler (APScheduler)

Roda em background thread com timezone `America/Sao_Paulo`:

| Horário | Job |
|---|---|
| 04h00 | Reinício via `os._exit(0)` — systemd reinicia o processo; porta é liberada antes da nova instância subir |
| 07h00 | Warmup de todos os canais (se `WARMUP_ENABLED=true`) — cobre jogos europeus (08h–17h) |
| 13h00 | Warmup de todos os canais (se `WARMUP_ENABLED=true`) — cobre jogos sul-americanos (16h–23h) |

O warmup em dev (`ENVIRONMENT != PRODUCTION`) dispara imediatamente ao subir.

**Lógica de skip no warmup:** para cada canal, faz HEAD request (timeout 3s) na URL em cache. Se 2xx → pula. Se 4xx/erro ou sem cache → evicta e re-resolve. Garante que após restart com `cache.json` do dia anterior, URLs mortas são detectadas e substituídas.

---

## Frontend

**Arquitetura de componentes (sem bundler):**
- `player-core.js` → classe `PlayerCore` — tudo relativo a Plyr, HLS.js e polling de stream
- `footer-panel.js` → classe `FooterPanel` — UI do painel deslizante
- `app.js` → init global — instancia as classes, carrega dados da API, conecta callbacks

**Bibliotecas:**
- Plyr 3.7.8 (player customizado) — controles: mute, volume, fullscreen
- HLS.js 1.x (streaming HLS no browser)
- Google Fonts: Bebas Neue + DM Mono

**Comportamento geral:**
- Ao entrar em fullscreen, trava orientação em landscape (`screen.orientation.lock('landscape')`)
- Ao sair do fullscreen, destrava
- Troca de canal aborta o polling anterior (`_currentUrl !== url`)
- Não há mais seção de canais/jogos abaixo do player — tudo está dentro do footer panel

**Footer Panel:**
- UI customizada dentro do `.video-wrap` (position absolute, z-index 20) — Plyr roda com `controls:[]`
- Três seções com `position:absolute` próprio: `.footer-body` (cards), `.footer-toggle` (strip), `.footer-controls` (barra de controles)
- **Controles**: mute + volume slider (esquerda, `width:110px`) | info do jogo/canal (centro, `flex:1`) | fullscreen (direita, `width:110px`) — larguras iguais garantem centro matematicamente centralizado
- **Toggle "CANAIS E JOGOS"**: strip com degradê lateral (transparent→escuro→transparent). Quando aberto, fundo sólido aparece via `::before opacity` (assimétrico: 0.5s abrir, 2s fechar). Sem fundo quando painel fechado
- **Abertura/fechamento do painel**: clique no toggle | swipe up/down (touch)
- **Troca de canal por swipe**: swipe left/right no vídeo (touch) — navega pela lista ordenada (jogos → canais livres) em loop. Detectado quando `|dx| > |dy|` e `|dx| > 50px`. Bloqueado quando o painel está aberto (`_open`) para não conflitar com rolagem do carrossel
- **Visibilidade dos controles**: aparece no carregamento (aberto), mousemove mostra por 2s, clique no vídeo faz toggle show/hide. No mobile, tap detectado no `touchend` com flag `_touchHandled` (o `click` é interceptado pelo Plyr quando `pointer-events:none`)
- **Conteúdo**: skeleton loading (4 game cards + 8 channel cards com shimmer) até dados carregarem; depois jogos, separador, canais livres (canais já presentes em algum jogo são ocultados)
- Abre automaticamente na carga da página (`open()` no constructor)

**Troca de canal — sequência exata:**
1. `selectChannel(name, url, meta)` muta o player imediatamente (`player.muted = true`)
2. Exibe overlay de loading (spinner + "Buscando stream…")
3. Chama `/resolve` e **usa a resposta diretamente** — se `status=ready`, chama `playStream` sem round trip extra. Só entra no polling de 2s quando `status=loading`
4. Quando pronto: `playStream(url, meta)` é chamado
5. No evento `MANIFEST_PARSED` do HLS.js: desmuta (`player.muted = false`), esconde overlay, entra em fullscreen, exibe info bar, força controles visíveis por 9s

**Fullscreen — implementação:**
- Plyr usa `fullscreen.container: '#video-wrap'` — o elemento `.video-wrap` inteiro vai para fullscreen, incluindo overlay e info bar
- CSS garante que `.plyr` ocupe 100% da altura do container em fullscreen:
  ```css
  #video-wrap:fullscreen .plyr { height: 100% }
  #video-wrap:-webkit-full-screen .plyr { height: 100% }
  ```
- Sem isso, o `.plyr` manteria a altura mínima inicial e os controles ficariam no topo da tela

**Controles visíveis após troca de canal:**
- Ao carregar novo stream, a classe `controls-on` é adicionada ao container do Plyr via JS
- CSS força `opacity:1 !important` nos controles enquanto a classe está ativa, ignorando lógica de mouse do Plyr
- Classe é removida após 9s — Plyr retoma o auto-hide normal

**Info bar estilo TV:**
- Injetada via JS dentro do `.plyr__controls` após o evento `ready` do Plyr
- Posicionada com `position:absolute; left:50%; transform:translateX(-50%)` — centralizada na tela independente dos botões ao redor
- Aparece e some automaticamente junto com os controles do Plyr — sem lógica extra de show/hide
- Conteúdo varia conforme o que foi selecionado:
  - **Canal sem jogo:** apenas logo do canal, centralizada — sem texto
  - **Canal com jogo / jogo:** logo do canal + título do jogo + descrição (liga) + `HH:MM • AO VIVO`
- Quando o usuário clica num botão de canal, `selectChannel` faz lookup em `_games` para verificar se há jogo naquele canal e exibe infos completas se encontrar
- Gradiente de fundo dos controles mais escuro (`rgba(0,0,0,.95)`) para garantir legibilidade

**Mock de jogos:** removido. `/games` retornando vazio ou falhando resulta em lista vazia — nenhum fallback.

**Logos disponíveis:** Band Sports, ESPN, ESPN 2, ESPN 4, Globo, Paramount+, Premiere, Premiere 2, Premiere 3, Prime Video, SBT, SporTV, SporTV 2, TNT, RecordTV, Disney+

**Scraping (scraper.py)** — TTL do cache: 12h (`_CACHE_TTL = 43200`). Ver seção [Cache persistente](#cache-persistente).

---

## Infraestrutura (Cloudflare Tunnel)

O app é exposto via **Cloudflare Tunnel** (cloudflared) — sem IP público exposto, sem porta aberta.

**Configurações ativas no dashboard:**
- HTTP/2, HTTP/3 (QUIC), TLS 1.3, 0-RTT — todos habilitados
- Sempre usar HTTPS — habilitado
- WebSockets — habilitado (necessário para SSE em `/status/stream`)
- **Cache Rule "TS Files":** `/proxy/ts*` → qualificado para cache, Edge TTL 30s — segmentos `.ts` são servidos do edge Cloudflare sem bater no servidor quando já cacheados

**Header `X-Accel-Buffering: no`** na resposta do `/proxy/ts` — impede o Cloudflare de acumular o segmento inteiro antes de repassar ao cliente (crítico para live streaming).

**Cache busting de assets estáticos:** `player.html` recebe `?v=<git_hash>` em todos os imports de CSS/JS (`_GIT_HASH` calculado no import do `app.py`). A cada novo deploy o hash muda, forçando o navegador a buscar a versão nova.

---

## PWA

- `manifest.json` — `display: standalone`, orientação any, theme `#e8ff47`
- Service Worker (`sw.js`) — cache network-first dos assets, fallback offline

---

## Dependências Python

```
flask, flask-cors, gunicorn, python-dotenv
requests, cryptography
patchright          # fork do playwright com anti-detecção
apscheduler, pytz
```

---

## Logs

- Formato: `[dd/mm HH:MM:SS] mensagem`
- Logs de debug (`_log`) só aparecem fora de `PRODUCTION`
- Erros críticos (lock timeout, token não encontrado) sempre aparecem
- Werkzeug e APScheduler têm log reduzido a ERROR/WARNING

---

## Cache persistente

O `_CACHE` do `scraper.py` é salvo em `cache.json` na raiz do projeto.

- **Estrutura em memória:** `dict[str, tuple[float, dict]]` — uma entrada por canal (mais recente)
- **Sobrescreve** a entrada anterior a cada novo resolve bem-sucedido
- **Carregado no import** do módulo
- **Salvo em background thread** após cada resolve (não bloqueia a resposta)
- **Cache hit:** `_latest_valid()` retorna a entrada se dentro do TTL (12h), `None` caso contrário
- **`_save_cache`** só persiste entradas ainda válidas (dentro do TTL) — `cache.json` nunca acumula entradas expiradas
- **Formato JSON:** `{ "player_url": [timestamp, result_dict] }`
- **TTL:** 12h (`_CACHE_TTL = 43200`)
- Reiniciar o app não perde o cache — streams já resolvidos ficam disponíveis imediatamente

**Histórico de streams (`stream_log.txt`):** a cada novo resolve bem-sucedido, uma linha é appendada na raiz do projeto:
```
[2026-05-29 14:32:10] https://.../tv/espn | https://cdn.cloudflaire.lat/.../style.css | referer: https://.../tv/espn
```

> `cache.json` e `stream_log.txt` devem estar no `.gitignore` (contêm URLs internas dos streams).

---

## Atalhos de teclado

| Tecla | Ação |
|---|---|
| `F` | Entra/sai do fullscreen |

---

## Segurança implementada

- **AES-256-GCM nas URLs** — cliente nunca vê URLs reais de canais ou segmentos
- **Rate limiting global** via `before_request`: 120 req/min por IP (sliding window com `deque`). Retorna 429. Rotas isentas: `/proxy/ts`, `/sw.js`, `/favicon.ico`, `/manifest.json`, qualquer path em `/static/` — assets estáticos e segmentos HLS não contam no limite para não interromper a reprodução
- **Auth em `/status`** via env `STATUS_TOKEN` — passar como `?token=X` ou `Authorization: Bearer X`. Se não definido, rota fica aberta

## Monitoramento SSE — expiração automática

Thread `_expiry_watcher` roda a cada 10s e compara o count de IPs ativos. Se mudou (por expiração natural de TTL), faz push no SSE — garante que o painel atualize mesmo sem novos connects.

---

## Decisões de Design Notáveis

- **Lock por URL no scraper:** impede múltiplos browsers simultâneos para o mesmo canal. Timeout de 90s.
- **Resolve assíncrono:** o frontend não bloqueia — dispara o resolve e faz polling, permitindo troca de canal enquanto resolve.
- **Reescrita dos segmentos m3u8:** necessária para que o browser busque os `.ts` via proxy (evita CORS e headers de autenticação do servidor original).
- **Cache por canal (entrada única):** uma entrada por canal, sobrescrita a cada novo resolve. `cache.json` só persiste entradas válidas. Histórico completo em `stream_log.txt`.
- **IP TTL de 30s:** considera dispositivo ativo enquanto está consumindo o stream (HLS.js bate o servidor a cada ~2-6s).
- **Rate limit sem dependência externa:** implementado com `deque` da stdlib, sem Flask-Limiter ou Redis.

---

## TODO

- [ ] **Validação de cache antes de servir** — warmup já valida via HEAD antes de cada resolve; `/stream` ainda serve diretamente do cache sem validar
- [ ] **Jogos sem canal disponível** — backend filtra e não exibe. Avaliar mostrar como cards cinza com tooltip "Sem canal disponível" para o usuário saber que o jogo existe mas não tem transmissão configurada
- [ ] **`_RESOLVE_STATUS` cresce indefinidamente** — nunca é limpo. Adicionar TTL ou LRU com limite de entradas
- [ ] **`/health` endpoint** — rota simples para monitoramento externo (uptime bots, load balancer). Retornar `{"ok": true}`
- [ ] **Logs estruturados** — atualmente só stdout sem nível. Considerar `logging` com níveis INFO/WARNING/ERROR para filtrar em produção
- [ ] **Métricas por canal** — contador de quantas vezes cada canal foi resolvido / falhou (útil para detectar canais problemáticos)
