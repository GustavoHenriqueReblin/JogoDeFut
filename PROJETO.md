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
| `templates/player.html` | Frontend completo (HTML + CSS + JS inline) |
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
| `WARMUP_ENABLED` | Não | `true` habilita warmup automático dos canais às 07h e 12h |
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
| 04h00 | Reinício do processo via `os.execv` |
| 07h00 | Warmup de todos os canais (se `WARMUP_ENABLED=true`) |
| 12h00 | Warmup de todos os canais (se `WARMUP_ENABLED=true`) |

O warmup em dev (`ENVIRONMENT != PRODUCTION`) dispara imediatamente ao subir.

---

## Frontend (player.html)

**Bibliotecas:**
- Plyr 3.7.8 (player customizado) — controles: mute, volume, fullscreen
- HLS.js 1.x (streaming HLS no browser)
- Google Fonts: Bebas Neue + DM Mono

**Comportamento geral:**
- Ao entrar em fullscreen, trava orientação em landscape (`screen.orientation.lock('landscape')`)
- Ao sair do fullscreen, destrava
- Troca de canal aborta o polling anterior (`_currentUrl !== url`)
- Botões de canal com logo `.webp` + fallback sem logo
- Cards de jogos com poster, título, horário e canal

**Troca de canal — sequência exata:**
1. `selectChannel(name, url, meta)` muta o player imediatamente (`player.muted = true`)
2. Exibe overlay de loading (spinner + "Buscando stream…")
3. Chama `/resolve` e faz polling em `/resolve/status` — primeiro check é imediato (sem espera), o que torna a troca quase instantânea quando há cache hit
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

**Mock de jogos (desenvolvimento):**
- Se `/games` retornar lista vazia, `loadGames` usa `_buildMockGames(channels)` como fallback
- 2 mocks: os URLs reais dos 2 primeiros canais carregados (funcionam com cache); posters via imgur
- `loadGames` aguarda `_channelsReady` (Promise) antes de construir os mocks, garantindo coordenação com `init()`

**Logos disponíveis:** Band Sports, ESPN, ESPN 2, ESPN 4, Globo, Paramount+, Premiere, Premiere 2, Premiere 3, Prime Video, SBT, SporTV, SporTV 2, TNT, RecordTV, Disney+

**Scraping (scraper.py)** — TTL do cache: 12h (`_CACHE_TTL = 43200`). Ver seção [Cache persistente](#cache-persistente).

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

- **Carregado no import** do módulo — entradas expiradas são ignoradas na leitura
- **Salvo em background thread** após cada resolve bem-sucedido (não bloqueia a resposta)
- **Formato:** JSON `{ "player_url": [timestamp, result_dict] }`
- **TTL:** 12h (`_CACHE_TTL = 43200`)
- Reiniciar o app não perde o cache — streams já resolvidos ficam disponíveis imediatamente

> `cache.json` deve estar no `.gitignore` (contém URLs internas dos streams).

---

## Atalhos de teclado / controle remoto

Funciona em PC e TVs com controle que emite eventos de teclado (Android TV, Fire TV etc.).

| Tecla | Ação |
|---|---|
| `→` `↓` `CH+` (keyCode 427) | Avança na lista unificada (jogos → canais em loop) |
| `←` `↑` `CH-` (keyCode 428) | Recua na lista unificada |
| `Enter` / OK | Confirma item em foco (carousel em fullscreen tem prioridade) |
| `1`–`9` | Pula direto para o canal pelo número e inicia (sempre canais) |
| `F` | Entra/sai do fullscreen |
| `Esc` | Fecha o carousel fullscreen sem confirmar |

**Navegação pelas setas — lista unificada em loop:**
- A lista é `[...jogos, ...canais]`; o loop fecha do último canal de volta ao primeiro jogo
- Se não há jogos, navega apenas entre canais
- Em fullscreen: navegação atualiza somente o carousel — não toca nas listas da página

**Carousel fullscreen:**
- Faixa horizontal aparece na parte inferior da tela (acima dos controles) ao pressionar seta/CH em fullscreen
- Cards de jogos (poster + título + canal) seguidos de cards de canais (logo + nome)
- Item focado: borda amarela, opacidade 100%, levemente ampliado; demais ficam escurecidos
- Some automaticamente após 4s sem teclar; `Esc` fecha imediatamente
- `Enter` confirma o item focado no carousel e inicia o canal

**Foco visual (fora de fullscreen):**
- Foco (`--accent2` cinza) vs ativo (amarelo `--accent`) — aplicado a cards de jogos e botões de canais
- HUD discreto no rodapé mostra `[N/Total] Título` ao navegar; some após 2s

**Suporte a CH+/CH- de TVs:**
- Detectado por `e.key === 'ChannelUp'` / `'ChannelDown'` ou `e.keyCode === 427` / `428`
- Disponibilidade depende do browser/SO da TV — em browser nativo pode ser interceptado antes do JS

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
- **Cache 12h:** tokens e URLs de stream são caros de obter (scraping). TTL reduz carga e latência sem deixar streams mortos por tempo excessivo.
- **IP TTL de 30s:** considera dispositivo ativo enquanto está consumindo o stream (HLS.js bate o servidor a cada ~2-6s).
- **Rate limit sem dependência externa:** implementado com `deque` da stdlib, sem Flask-Limiter ou Redis.

---

## TODO

- [ ] **`_RESOLVE_STATUS` cresce indefinidamente** — nunca é limpo. Adicionar TTL ou LRU com limite de entradas
- [ ] **`/health` endpoint** — rota simples para monitoramento externo (uptime bots, load balancer). Retornar `{"ok": true}`
- [ ] **Logs estruturados** — atualmente só stdout sem nível. Considerar `logging` com níveis INFO/WARNING/ERROR para filtrar em produção
- [ ] **Métricas por canal** — contador de quantas vezes cada canal foi resolvido / falhou (útil para detectar canais problemáticos)
- [ ] **Warmup paralelo** — atualmente resolve canais em série com delays 7–17s. Pool de 2–3 workers paralelos reduziria o tempo total mantendo os locks por URL já existentes
- [ ] **Validação de cache antes de servir** — cache de 12h pode servir URL de stream morta. Fazer HEAD request na URL antes de retornar do cache
- [ ] **Jogos sem canal disponível** — backend filtra e não exibe. Avaliar mostrar como cards cinza com tooltip "Sem canal disponível" para o usuário saber que o jogo existe mas não tem transmissão configurada
