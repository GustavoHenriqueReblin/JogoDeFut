# JogoDeFut — Documentação do Projeto

## Visão Geral

App web PWA para assistir futebol ao vivo. Funciona como um intermediário: busca canais de TV (Band Sports, ESPN, Globo, Premiere etc.) via scraping, resolve os streams HLS e os reproduz no browser com Plyr + HLS.js.

**Stack:** Python (Flask) no backend + HTML/JS vanilla no frontend.

---

## Arquitetura

```
browser
  └── player.html (Plyr + HLS.js)
        ├── GET /channels           → lista de canais configurados (name + slug)
        ├── GET /games              → jogos ao vivo (API externa)
        ├── GET /resolve/<slug>     → dispara resolve assíncrono + inicia relay
        ├── GET /resolve/status/<slug> → polling do status do resolve
        ├── GET /<slug>             → m3u8 pré-cacheado pelo relay (resposta instantânea)
        ├── WS  /ws/<slug>          → sincronismo: relay faz broadcast do segmento canônico
        └── GET /proxy/ts?url=      → proxy dos segmentos .ts (URL do segmento encriptada)

app.py (Flask)
  ├── StreamRelay (um por canal ativo)
  │     └── loop background: busca M3U8 do CDN a cada 2s, cacheia pronto
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
| `check_cache.py` | Script utilitário: testa URLs do `stream_log.txt` e insere as válidas no `cache.json` |
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
| `GAMES_API_URL` | Sim | URL do worker Cloudflare (`games-proxy`) que retorna os jogos ao vivo em JSON. O worker em si roda fora deste repo e busca os dados de uma API de jogos (domínio já mudou pra `api.reidoscanais.st`) — se `/games` parar de retornar dados, checar se o worker ainda aponta pro domínio certo, não é algo que se resolve aqui no `.env`. |
| `TOKEN_API_URL` | **Sim** | URL do endpoint que troca o token do Turnstile pela URL do stream (`POST /get_token`). Sem default no código de propósito — esse domínio muda periodicamente (a API/CDN roda em domínios descartáveis que trocam quando um é banido) e não deve ficar hardcoded no source. Se o scraper começar a levar 403 de bloqueio de zona (página de erro do Cloudflare, não da API), é sinal de que o domínio mudou; atualizar aqui sem precisar mexer em código. |
| `P2P_CDN_HOST` | **Sim** | Host usado pra identificar a linha do PNG de verificação P2P dentro do M3U8 (`check_stream`, ver seção de validação). Sem default no código de propósito, mesmo motivo do `TOKEN_API_URL`. Se esse host mudar, a validação de P2P para de detectar a linha e passa a considerar todo stream "sem P2P" como ok — ajustar aqui sem mexer em código. |
| `PROXY_SECRET` | **Sim** | Chave hex 64 chars para encriptar URLs. **Deve estar fixada no `.env`** — se ausente, uma chave aleatória é gerada a cada restart, invalidando todas as URLs encriptadas em sessões abertas (erro 400). Gerar com `python -c "import secrets; print(secrets.token_hex(32))"` |
| `PORT` | Não | Porta do servidor (padrão: 5000) |
| `ENVIRONMENT` | Não | `PRODUCTION` desliga logs de debug. Qualquer outro valor (padrão `DEVELOPMENT`) habilita logs. |
| `WARMUP_ENABLED` | Não | `true` habilita warmup automático dos canais às 05h, 12h e 18h |
| `RESTART_ENABLED` | Não | `true` habilita o restart automático de 04h (`os.execv`-like via subprocess, ver seção Scheduler). **Padrão: `false` (desativado)** — app roda 24/7 sem restart programado. |
| `WARMUP_WORKERS` | Não | Número de canais resolvidos em paralelo no warmup (padrão: 2) |
| `MIN_POOL_SIZE` | Não | Tamanho mínimo do pool de URLs por canal antes de invocar Chromium (padrão: 5) |
| `HEADLESS_DEBUG` | Não | `true` abre o browser visível durante scraping (útil para debug local) |
| `STATUS_TOKEN` | Não | Token de acesso às rotas `/status` e `/status/stream`. Se vazio, rotas ficam abertas. Passar via `?token=X` ou header `Authorization: Bearer X` |
| `HLS_BUFFER_LENGTH` | Não | Segundos de buffer que o HLS.js tenta manter à frente (padrão: 20). Controla quanto buffer acumula; valor alto reduz risco de stall em quedas de CDN. |
| `HLS_MAX_BUFFER_LENGTH` | Não | Teto absoluto do buffer HLS.js (padrão: 30). Limita acúmulo máximo de buffer em RAM. |

---

## Fluxo de Reprodução

1. Usuário clica em um canal (ou jogo)
2. Player é **mutado imediatamente** — o stream anterior continua em buffer mas sem áudio enquanto o novo carrega
3. Frontend chama `GET /resolve/<slug>`:
   - Pool ok → retorna `ready` imediatamente **e já inicia o `StreamRelay`** para pré-buscar o M3U8
   - Pool vazio → retorna `loading`, scraping roda em background; quando pronto **inicia o relay**
4. Frontend faz polling em `GET /resolve/status/<slug>` a cada 2s (timeout: 30s)
5. Quando status = `ready`, chama `playStream()` que aponta HLS.js para `GET /stream/<slug>`
6. `/stream/<slug>` consulta o `StreamRelay` do canal — se já tiver M3U8 cacheado devolve instantaneamente; se relay ainda estiver na 1ª busca, aguarda até 5s
7. O M3U8 devolvido já tem segmentos reescritos para `/proxy/ts?url=<enc_seg>` — feito pelo relay
8. HLS.js re-busca `/stream` a cada ~2-6s para novos segmentos; relay já tem o próximo M3U8 pronto
9. HLS.js busca segmentos via `/proxy/ts` que faz proxy transparente com os headers corretos
10. Quando `MANIFEST_PARSED` dispara: **desmuta**, esconde overlay, entra em fullscreen, exibe info bar

---

## Scraping (scraper.py)

O scraping é necessário porque os players ficam atrás de Cloudflare Turnstile.

**Fluxo:**
1. `resolve_stream(player_url)` — verifica pool. Pool completo → retorna imediatamente. Pool parcial → retorna o que tem e dispara `_accumulate_bg` numa thread solta, fire-and-forget (não bloqueia o caller). Pool vazio → bloqueia até resolver (`_accumulate_bg` direto). Só invoca Chromium se `pool_size < MIN_POOL_SIZE`
2. `_accumulate_bg(player_url)` — bloqueia até terminar; chama `_do_resolve`, valida (`is_stream_alive`) e insere/atualiza a entry no pool. Retorna `bool` (sucesso). **Importante:** quem precisa de concorrência controlada (ex: warmup) deve chamar `_accumulate_bg` diretamente, nunca `resolve_stream` — pool parcial faz `resolve_stream` retornar na hora e a thread solta que ele dispara não é contada em nenhum limite de workers (ver bug do warmup abaixo).
3. `_do_resolve()` — extrai `host` e `channel` da URL, consulta `CLOUDFLAIRE_PLAYERS` para saber a `fonte`
4. Chama `_scrape_token()` até 8 vezes com delays progressivos `[3,5,8,10,12,15,20]`s após 404 **ou falha total de token/direct_url** (ver bug corrigido abaixo)
5. `_scrape_token()` abre Chromium via Playwright (patchright), navega até a URL e:
   - **Prioridade (até ~15s) — interceptação da chamada `get_token` que o próprio site faz:** o player da página, ao carregar, resolve o Turnstile e chama a API de token sozinho, dentro do browser (com fingerprint de TLS real, cookies corretos etc.). O scraper intercepta essa response e extrai a `url` direto dali, sem precisar montar seu próprio request — **esse é o caminho confiável**, porque evita replay externo do token.
   - **Fallback (mais ~15s), só se o site não resolver sozinho:** simula interação humana (mouse/scroll) e tenta capturar um token via DOM (`[name="cf-turnstile-response"]`) ou response do próprio Turnstile, para então fazer o `POST` manual via `requests`. Esse caminho é frágil: o Cloudflare consegue diferenciar o fingerprint TLS/HTTP de um cliente Python do de um Chrome real, então mesmo com token tecnicamente válido a API pode rejeitar (404) um POST feito fora do contexto do browser. Exige tamanho mínimo de token (`_MIN_TOKEN_LEN`) consistente com challenges de alta confiança — tokens curtos são sistematicamente rejeitados pela API mesmo passando na validação de formato.
6. Retorna `{"url": "<hls_url>", "referer": player_url}` — entry adicionada ao pool do canal

> **Bug corrigido — sem retry em falha total de token:** o loop de `_do_resolve` retryava com backoff só em 404 da API; se `_scrape_token` falhasse completamente (nem `direct_url` nem `token`, ex: Chromium sobrecarregado sob concorrência alta), o código desistia na primeira tentativa (`return None` direto), ignorando as 8 tentativas com backoff que existem pra exatamente esse cenário. Corrigido: agora aplica o mesmo backoff e `continue` do caso 404.

**Detecção de automação:** mesmo com Playwright via patchright (anti-detecção), o Cloudflare pode identificar a sessão como automatizada e entregar tokens de confiança reduzida. Simular interação humana (mouse/scroll) pós-carregamento não altera esse resultado quando a decisão já é tomada no fingerprint do ambiente antes da interação — nesses casos, a captura direta da `url` (item 4 acima) é o único caminho que funciona de forma confiável.

**Extração do hash do canal:** o hash é sempre um MD5 (32 chars hex) no path da stream URL, mas o separador e a posição podem variar conforme o formato vigente do CDN (ex: `fonte_{hash}/arquivo` ou `fonte/{hash}/arquivo`). Importante: o **host do CDN também é um MD5 sequencial** (ver seção de infraestrutura abaixo), então a extração pega o **último** match de 32-hex na URL — o hash do canal sempre vem depois do host no path.

**Proteção contra bloqueio de IP no warmup:** após 3 falhas consecutivas, aguarda 5 minutos antes de continuar.

---

## Monitoramento de Dispositivos Ativos

`_ACTIVE_IPS: dict[str, tuple[float, float]]` — armazena `ip → (first_seen, last_seen)`

- IP é registrado a cada hit em `/stream`
- TTL de 30s — IP some da contagem se não bater stream dentro desse tempo
- Log impresso quando novo IP conecta: `[dd/mm HH:MM:SS] Novo IP (x.x.x.x) conectado. Total ativos: (N).`
**GET /status** — JSON (chamada de API) ou página HTML (browser):
- JSON: `{"devices": 2, "clients": [{"ip": "177.x.x.x", "connected_for": "1h 23m"}, ...]}`
- HTML: página monospace dark com contador de dispositivos, dot pulsante e tabela IP/tempo. Atualiza a cada 5s via `setInterval` + fetch no próprio `/status`.

`connected_for` usa formato legível: `Xs`, `Xm Ys`, `Xh Ym`

---

## Rotas

| Método | Rota | Descrição |
|---|---|---|
| GET | `/` | Frontend (player.html) |
| GET | `/channels` | Lista de canais com URLs encriptadas |
| GET | `/games` | Jogos ao vivo (proxia GAMES_API_URL, faz match com canais disponíveis) |
| GET | `/resolve/<slug>` | Inicia resolve assíncrono do stream |
| GET | `/resolve/status/<slug>` | Retorna `loading \| ready \| error \| unknown` |
| GET | `/<slug>` | m3u8 com segmentos proxiados |
| WS  | `/ws/<slug>` | Sinal "go": relay → `{seq, dur, wall_ts, server_ts}` · cliente recebe e carrega o stream |
| GET | `/proxy/ts?url=` | Proxy de segmento .ts |
| GET | `/status` | Dispositivos ativos — JSON (Accept padrão) ou página HTML com polling 5s (Accept: text/html) |
| GET | `/cache-status` | Página HTML com status do cache por canal (verde = válido + idade, vermelho = sem cache) |
| GET | `/manifest.json` | PWA manifest |
| GET | `/sw.js` | Service Worker |
| GET | `/favicon.ico` | Ícone |

---

## Segurança

**URLs de segmentos** são encriptadas com AES-256-GCM (biblioteca `cryptography`) antes de aparecer no M3U8. A chave é `PROXY_SECRET` (hex 64 chars = 256 bits). Sem essa chave fixada no `.env`, cada restart gera uma nova chave e sessões abertas recebem 400 ao tentar usar segmentos antigos.

URLs de canais **não** são encriptadas — o frontend usa slugs plain text (ex: `/stream/premiere`, `/resolve/espn`). Os slugs são derivados do último segmento da URL de player configurada no `.env`.

---

## Scheduler (APScheduler)

Roda em background thread com timezone `America/Sao_Paulo`:

| Horário | Job |
|---|---|
| 04h00 | Reinício via subprocess detached (`_midnight_restart`) — sobe um novo processo e derruba o atual (`os._exit`). **Desativado por padrão** (`RESTART_ENABLED=false`) — só é agendado se `RESTART_ENABLED=true` no `.env`. Com o restart desligado, o app roda 24/7 sem interrupção programada. |
| 05h00 | Warmup de todos os canais (se `WARMUP_ENABLED=true`) — pool vazio da madrugada |
| 12h00 | Warmup de todos os canais (se `WARMUP_ENABLED=true`) — cobre jogos europeus (13h–17h) |
| 18h00 | Warmup de todos os canais (se `WARMUP_ENABLED=true`) — cobre jogos sul-americanos (19h–23h) |

O warmup em dev (`ENVIRONMENT != PRODUCTION`) dispara imediatamente ao subir. O app é iniciado diretamente com `python app.py` — sem systemd.

> **Bug corrigido — `WARMUP_WORKERS` não era respeitado de verdade:** o warmup (`_resolve_one` em `app.py`) chamava `resolve_stream()` pra cada canal. Só que `resolve_stream`, quando o pool está **parcial** (o caso comum — quase todo canal fica em `N/MIN_POOL_SIZE` a maior parte do tempo), retorna **na hora** com o que já tem e dispara a acumulação de verdade numa thread solta, fire-and-forget, fora de qualquer controle. Resultado: o `ThreadPoolExecutor(max_workers=WARMUP_WORKERS)` do warmup via cada canal "concluir" quase instantaneamente (porque já tinha pool) e passava pro próximo, disparando **um Chromium por canal simultaneamente** — muito mais que o limite configurado — enquanto essas threads soltas rodavam por conta própria em paralelo. Isso sobrecarregava o scraping (visível em produção: vários canais falhando a captura de token ao mesmo tempo, tudo durante a janela em que os 15 canais foram todos disparados em ~25s). Corrigido: warmup agora chama `_accumulate_bg()` diretamente (bloqueante, retorna `bool`), então o pool de workers realmente limita quantos Chromium rodam ao mesmo tempo.

**`check_stream(url)`** — função central de validação em `scraper.py` (tristate):
- `'alive'` — M3U8 válido, live, P2P ok
- `'dead'` — 404, `#EXT-X-ENDLIST` presente, P2P 404 ou status 4xx (erro permanente)
- `'transient'` — 5xx, timeout ou falha de rede (erro temporário)

Chamada indiretamente por dois wrappers com filosofia distinta:
- **`is_stream_alive(url)`** — retorna `check_stream == 'alive'`. Usada na **inserção** (`_accumulate_bg`): conservadora ao adicionar.
- **`is_stream_definitely_dead(url, attempts=3, delay=5s)`** — usada na **evicção** (warmup): retorna `True` só se alguma tentativa for `'dead'`; se todas forem `'transient'`, retorna `False` (CDN instável não causa remoção).

Critérios de `'dead'`:
1. GET → status 404 ou qualquer 4xx
2. Body não começa com `#EXTM3U`
3. `#EXT-X-ENDLIST` presente (stream encerrado)
4. HEAD no PNG de P2P retorna 404 (sem P2P → player bloqueia)

**Princípio:** difícil entrar, mais difícil sair. Falha transiente de CDN não deve jamais custar uma URL válida do pool.

**Lógica de skip no warmup:** após validar e remover confirmadamente mortas, se `pool_size >= MIN_POOL_SIZE` → pula. Se parcial (ou vazio) → resolve via Chromium para acumular mais uma URL.

**Prevenção de loop:** validação na inserção garante que URL ruim (ex: Paramount+ sem P2P) nunca persiste no cache.

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

**Tratamento de erro HLS.js — reconexão automática:**
Quando o HLS.js reporta um erro `fatal` (desistiu de tentar), o cliente entra em modo de reconexão automática — sem exibir erro, sem botão de retry:
- **Buffer > 2s:** instância HLS é destruída (para requests). Um `setInterval` de 1s monitora o buffer restante; quando restar ≤ 0.5s chama `_reconnect`.
- **Buffer ≤ 2s ou vazio:** chama `_reconnect` imediatamente.
- **`_reconnect(slug, meta)`:** exibe overlay "Reconectando…" (loading) e faz polling `GET /<slug>` a cada 3s. Quando o relay responder 200, chama `playStream` automaticamente — o usuário não precisa interagir.
- Erros de network não-fatais: HLS.js gerencia retry internamente (não chama `startLoad()` manualmente para evitar flood de requests ao backend).
- Guard `_currentSlug === slug` cancela reconexão se o usuário trocou de canal.

**Logos disponíveis:** Band Sports, ESPN, ESPN 2, ESPN 4, Globo, Paramount+, Premiere, Premiere 2, Premiere 3, Prime Video, SBT, SporTV, SporTV 2, TNT, RecordTV, Disney+

**Scraping (scraper.py)** — pool sem TTL, validade decidida por teste real. Ver seção [Cache persistente](#cache-persistente).

---

## Infraestrutura (Cloudflare Tunnel)

O app é exposto via **Cloudflare Tunnel** (cloudflared) — sem IP público exposto, sem porta aberta.

**Configurações ativas no dashboard:**
- HTTP/2, HTTP/3 (QUIC), TLS 1.3, 0-RTT — todos habilitados
- Sempre usar HTTPS — habilitado
- WebSockets — habilitado
- **Cache Rule "TS Files":** `/proxy/ts*` → qualificado para cache, Edge TTL 30s — segmentos `.ts` são servidos do edge Cloudflare sem bater no servidor quando já cacheados

**Header `X-Accel-Buffering: no`** na resposta do `/proxy/ts` — impede o Cloudflare de acumular o segmento inteiro antes de repassar ao cliente (crítico para live streaming).

**Cache busting de assets estáticos:** `player.html` recebe `?v=<git_hash>` em todos os imports de CSS/JS (`_GIT_HASH` calculado no import do `app.py`). A cada novo deploy o hash muda, forçando o navegador a buscar a versão nova.

**`/cache-status`** (protegida por `STATUS_TOKEN`) — página HTML que lista todos os canais configurados com badge colorido: verde com horário de expiração em pt-BR (`✓ expira hoje 14:32`, `✓ expira amanhã 02:15`, `✓ expira qua 09:00`) ou vermelho "sem cache". Útil para confirmar que o warmup rodou corretamente.

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

- **Estrutura em memória:** `dict[str, list[tuple[float, dict]]]` — pool de entradas por canal, acumula, não substitui
- **Cada entry:** `(timestamp, {"url": "...", "referer": "..."})` — uma URL por entry
- **Carregado no import** do módulo (migra formato antigo automaticamente)
- **Salvo em background thread** após cada resolve (não bloqueia a resposta), com `indent=2` para legibilidade
- **Sem TTL — `_valid_pool()` retorna o pool inteiro, sem filtrar por idade.** Uma entrada não é considerada inválida só porque "passou tempo"; a validade é decidida por teste real: `is_stream_alive` na inserção (`_accumulate_bg`), `is_stream_definitely_dead` no warmup, e o próprio `StreamRelay` evictando em 404 confirmado durante o uso. `_latest_valid()` retorna a entrada mais recente do pool (todas são candidatas, não só as "não-expiradas").
  > **Histórico:** havia um TTL de 48h (`_CACHE_TTL`) que filtrava o pool por idade. Isso causava um bug real em produção: quando uma entrada "expirava" pelo TTL mas a URL continuava funcionando de verdade, um novo resolve trazia o **mesmo hash** de volta — só que o dedup por hash comparava contra `_CACHE` inteiro (sem filtro de TTL), via a entrada antiga como "já presente" e não a readicionava. Resultado: pool ficava vazio pra sempre pro cliente (`_valid_pool` filtrando por TTL) mesmo a URL sendo válida, e o dedup impedia a correção. TTL removido — dedup por hash agora **atualiza o timestamp** da entrada existente em vez de ignorar.
- **`_evict_url(url)`** remove só a URL morta; **`_evict_cache()`** limpa o pool todo
- **Formato JSON:** `{ "player_url": [[timestamp, entry], ...] }`
- Reiniciar o app não perde o cache — pool completo disponível imediatamente

**Histórico de streams (`stream_log.txt`):** a cada novo resolve bem-sucedido, uma linha é appendada na raiz do projeto:
```
[YYYY-MM-DD HH:MM:SS] <player_url> | <stream_url> | referer: <player_url>
```

> `cache.json` e `stream_log.txt` devem estar no `.gitignore` (contêm URLs internas dos streams).

---

## check_cache.py — Recuperação de URLs do histórico

Script utilitário para reaproveitar URLs registradas no `stream_log.txt` que ainda não estão no `cache.json`.

**Uso:**
```bash
python check_cache.py              # testa e salva
python check_cache.py --dry-run    # apenas testa, não salva
python check_cache.py --workers 12 # mais threads (padrão: 8)
```

**Comportamento:**
1. Lê `stream_log.txt`, dedup por hash de canal
2. Carrega `cache.json`, coleta todos os hashes presentes
3. Testa apenas hashes **não** presentes no cache (evita re-testar o que já está válido)
4. Adiciona entradas `'alive'` ao cache com `time.time()` como timestamp (TTL começa do momento do teste, não do registro no log)

**É estritamente insert-only** — nunca remove nem modifica entradas existentes no `cache.json`. Remoção de URLs mortas é responsabilidade do warmup e do `StreamRelay`.

Saída por URL: `✓ alive`, `✗ dead` ou `~ transient` com hash e nome do canal.

---

## Atalhos de teclado

| Tecla | Ação |
|---|---|
| `F` | Entra/sai do fullscreen |

---

## Segurança implementada

- **AES-256-GCM nos segmentos** — cliente nunca vê URLs reais dos segmentos `.ts`. URLs de canais usam slugs plain text.
- **Rate limiting global** via `before_request`: 120 req/min por IP (sliding window com `deque`). Retorna 429. Rotas isentas: `/proxy/ts`, `/sw.js`, `/favicon.ico`, `/manifest.json`, qualquer path em `/static/` — assets estáticos e segmentos HLS não contam no limite para não interromper a reprodução
- **Auth em `/status`** via env `STATUS_TOKEN` — passar como `?token=X` ou `Authorization: Bearer X`. Se não definido, rota fica aberta

## Decisões de Design Notáveis

- **Lock por URL no scraper:** impede múltiplos browsers simultâneos para o mesmo canal. Timeout de 90s.
- **Resolve assíncrono:** o frontend não bloqueia — dispara o resolve e faz polling, permitindo troca de canal enquanto resolve.
- **Reescrita dos segmentos m3u8:** necessária para que o browser busque os `.ts` via proxy (evita CORS e headers de autenticação do servidor original). Feita pelo relay antes de cachear.
- **StreamRelay:** elimina latência de CDN do caminho crítico. HLS.js re-busca o manifesto a cada ~2-6s; o relay já tem o próximo pronto. Falha de CDN não trava o front — relay troca de fonte silenciosamente. Múltiplos clientes no mesmo canal compartilham um único relay (eficiência de CDN).
- **Pool por canal:** múltiplas URLs por canal acumuladas ao longo do tempo. `_evict_url` remove só a morta. Histórico completo em `stream_log.txt`.
- **IP TTL de 30s:** considera dispositivo ativo enquanto está consumindo o stream (HLS.js bate o servidor a cada ~2-6s).
- **Rate limit sem dependência externa:** implementado com `deque` da stdlib, sem Flask-Limiter ou Redis.

---

## TODO

- [ ] **Validação de cache antes de servir** — warmup já valida via HEAD antes de cada resolve; `/stream` ainda serve diretamente do cache sem validar
- [ ] **Detecção de stream "congelado" (dead air)** — `check_stream` valida estrutura do M3U8 (`#EXTM3U`, sem `#EXT-X-ENDLIST`, P2P ok) mas não verifica se o `MEDIA-SEQUENCE` está avançando. Observado em produção: canal com encoder de origem travado passa em todas as validações como `'alive'`, mas serve sempre a mesma janela de segmentos — reproduzido tanto pelo nosso relay quanto acessando o site da fonte diretamente pelo navegador (F5 + novo token + mesmo resultado travado), então não é cache de CDN, é a transmissão de origem parada. Fix proposto: no `check_stream`, buscar o M3U8 duas vezes com intervalo (~10s, a duração de um segmento) e comparar `MEDIA-SEQUENCE`; se não avançar, tratar como suspeito/morto. Trade-off: +10s de latência por validação (usada no warmup e na inserção no pool). Ainda não implementado — decisão pendente de custo/benefício.
- [ ] **Jogos sem canal disponível** — backend filtra e não exibe. Avaliar mostrar como cards cinza com tooltip "Sem canal disponível" para o usuário saber que o jogo existe mas não tem transmissão configurada
- [ ] **`_RESOLVE_STATUS` cresce indefinidamente** — nunca é limpo. Adicionar TTL ou LRU com limite de entradas
- [ ] **`/health` endpoint** — rota simples para monitoramento externo (uptime bots, load balancer). Retornar `{"ok": true}`
- [ ] **Logs estruturados** — atualmente só stdout sem nível. Considerar `logging` com níveis INFO/WARNING/ERROR para filtrar em produção
- [ ] **Métricas por canal** — contador de quantas vezes cada canal foi resolvido / falhou (útil para detectar canais problemáticos)

---

## Backend multi-CDN — Pool de URLs

> **Implementado em 30/05/2026.** Pool de URLs por canal acumulando múltiplos CDN hosts. Chromium ainda é necessário para obter a hash do canal, mas roda cada vez menos conforme o pool cresce.

### Infraestrutura descoberta

```
api.<domínio>/get_token
  └→ retorna { token: "<hash_canal>", url: "https://<cdn-host>/<player>/<hash_canal>/<arquivo-disfarçado>" }

m3u8 disfarçado de arquivo estático contém segmentos:
  https://cdn.<domínio-segmentos>/<hash>.png   ← domínio fixo, entrega P2P

  Se esse PNG retorna 404 → P2P indisponível → player bloqueia o stream mesmo com #EXTM3U válido.
  Canais sem P2P (ex: Globo RJ) podem ter CDN direto e não referenciarem esse PNG — ausência da linha é ok.
```

> **Nota:** a extensão/nome do arquivo disfarçado e o path da URL já mudaram mais de uma vez (`style.css` → `file.txt`, `<player>_<hash>/` com underscore → `<player>/<hash>/` com barra). Nenhum desses detalhes é hardcoded no parsing — `_channel_hash` (scraper.py) extrai o hash pelo formato MD5 (32 chars hex), não pelo nome do arquivo ou separador, então mudanças futuras nesse padrão não devem quebrar a extração.

#### CDN host — totalmente previsível

O host CDN segue o padrão `MD5(str(n).zfill(4)) + ".<domínio>"` onde `n` é sequencial (0000–9999).

**Descoberta crítica:** todos os hosts 0000–9999 estão ativos simultaneamente — são aliases para o mesmo backend. A "rotação" é só qual deles a API retorna em cada chamada. Se o backend cair, todos caem juntos.

**Consequência:** qualquer `MD5(0–9999)` funciona como host. Não há necessidade de rastrear qual está "ativo". O identificador real de um stream é o `hash_canal` (`nossoplayer_{hash}`), não a URL completa — por isso o dedup do pool é feito por hash, não por URL.

#### Hash do canal — ainda requer Chromium (uma vez)

O hash de canal **não** segue o padrão MD5 sequencial — é opaco, retornado pela API. Pode ou não mudar ao longo do tempo (TTL confirmado >24h, observar `stream_log.txt` para determinar limite real).

Os hashes conhecidos ficam em `cache.json` e `stream_log.txt`.

### Descobertas adicionais (30/05/2026)

- **Wildcard DNS confirmado:** qualquer subdomínio serve o stream. O subdomínio é irrelevante — só a hash do canal importa.
- **Múltiplos tokens válidos simultaneamente:** a API emite um token novo a cada chamada sem invalidar os anteriores — funciona como um pool crescente de tickets. Base da estratégia de multi-URL.
- **TTL real do token: >24h confirmado** — `_CACHE_TTL` ajustado para 172800s (48h). Observar `stream_log.txt` por mais dias para determinar limite real.

### Arquitetura implementada (30/05/2026)

**Problema resolvido:** evict prematuro — HLS timeout fazia o código descartar a URL e rodar Chromium, mas a URL estava viva. CDN hosts antigos e novos servem o stream simultaneamente por horas.

**Solução — pool de URLs por canal (`scraper.py`):**
- `_CACHE[player_url]` = `[(ts, stream_entry), ...]` — acumula, não substitui
- `resolve_stream`: pool completo → serve; parcial → serve + acumula em background; vazio → bloqueia. Só invoca Chromium se `pool_size < MIN_POOL_SIZE` (configurável via `.env`, padrão 5)
- **Dedup por `hash_canal`** — o CDN host é irrelevante (todos os hosts 0000–9999 são aliases do mesmo backend). `6252cf.../nossoplayer_abc` e `2b77fb.../nossoplayer_abc` são o mesmo hash; só hashes genuinamente novos acumulam no pool
- `_evict_url` remove apenas a URL morta do pool
- `_evict_cache` limpa o pool todo (só quando todas as URLs falham)
- Warmup valida todas as URLs do pool via `is_stream_alive()`; remove mortas; só pula o canal se ainda `>= MIN_POOL_SIZE` vivas

**StreamRelay — arquitetura de relay por canal (`app.py`):**
- Um `StreamRelay` por canal ativo, criado no `/resolve` assim que o pool fica disponível
- Loop de background (`_run`): busca M3U8 do CDN a cada 2s, reescreve segmentos, cacheia
- `/<slug>` consulta `relay.get_m3u8(timeout=5)` — retorno quase instantâneo na maioria dos casos (relay já buscou antes)
- Failover de fonte: 3 falhas consecutivas na fonte atual → troca para outra URL do pool
- 404 permanente → remove URL do pool via `_evict_url`, força troca de fonte
- Erros transientes (timeout, 5xx) → incrementa contador, troca de fonte após threshold, não descarta do pool
- Relay sem acesso por 5 min → parado automaticamente pelo `_relay_cleanup_loop`
- Múltiplos clientes assistindo o mesmo canal → compartilham o mesmo relay (uma busca CDN serve todos)

**Segment cache (`_SEG_CACHE`):**
- O relay pre-busca cada segmento novo em background assim que aparece no M3U8 (`_prefetch`)
- `/proxy/ts` serve do cache em RAM; só vai ao CDN se o segmento ainda não estiver cacheado
- Resultado: todos os clientes recebem os mesmos bytes do mesmo cache — sincronismo de conteúdo
- TTL: 20s (além disso o segmento já passou do live edge e não será mais requisitado)
- Evicção automática a cada `_seg_put` — entradas expiradas são removidas

**WebSocket (`/ws/<slug>`) — sinal de "go" para carregamento coordenado:**
- Relay faz broadcast a cada ciclo de 2s (+ mid-tick em 1s para reduzir delay de entrada)
- **Propósito:** cliente só chama `hls.loadSource` após receber o primeiro tick do relay — garante que o backend já tem M3U8 válido antes de o player tentar carregar, evitando tentativas com URLs ruins/não resolvidas
- **Canal unidirecional (backend → cliente):** relay envia `{seq, dur, wall_ts, server_ts}` periodicamente; cliente não envia estado
- `hls.loadSource` chamado no primeiro WS tick (`_onFirstSync`) — todos os clientes que abrirem o mesmo canal num mesmo período carregam juntos (consequência natural do "go" compartilhado)
- WS mantido aberto para detectar desconexão (thread `_reader` fica em `ws.receive()` — quando retorna `None`, coloca `_SENTINEL` na fila e encerra o handler)
- `_WS_CHANNELS`: dict slug → list de `{q: Queue}` — uma fila por cliente conectado
- Sincronismo de posição entre clientes **não implementado** — tentativas de correção via `playbackRate` causavam instabilidade (diff instável por discretização do `playingDate`, oscilação pós-stall, burst duplo). Clientes podem divergir gradualmente pela natureza do HLS live; o `_SEG_CACHE` garante que todos recebem os mesmos bytes, mas a posição exata depende do momento de entrada de cada cliente

### TTL confirmado (histórico — TTL removido depois)

- **>24h confirmado:** token de 29/05 20:38 ainda ativo em 30/05 20:41 (~24h03min)
- **`_CACHE_TTL` na época:** 172800s (48h) — subido em 30/05/2026 após confirmação >24h
- **TTL removido depois** (ver seção [Cache persistente](#cache-persistente)): filtrar por idade causava um bug real — entrada "expirava" mas a URL ainda funcionava, e o dedup por hash (que ignorava o TTL) impedia a re-inserção, deixando o pool vazio pra sempre. Validade hoje é só por teste real (vivo/morto), não por tempo.
