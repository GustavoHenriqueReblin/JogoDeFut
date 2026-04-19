#!/usr/bin/env python3
import os
import sys
import subprocess

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as env_file:
            for line in env_file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

def install(pkg):
    subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    from flask import Flask, render_template_string, request, send_file, send_from_directory
    from flask_cors import CORS
except ImportError:
    print("Instalando dependências...")
    install("flask")
    install("flask-cors")
    from flask import Flask, render_template_string, request, send_file, send_from_directory
    from flask_cors import CORS

app = Flask(__name__)
CORS(app)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('PORT', 5000))
DEFAULT_URL = os.environ.get('START_URL', '')
current_url = DEFAULT_URL

HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <meta name="theme-color" content="#e8ff47"/>
  <meta name="description" content="Player de vídeo HLS simples"/>
  <link rel="manifest" href="/manifest.json"/>
  <link rel="icon" type="image/png" sizes="32x32" href="/static/icons/logo-32.png"/>
  <link rel="icon" type="image/png" sizes="192x192" href="/static/icons/logo-192.png"/>
  <link rel="shortcut icon" href="/favicon.ico"/>
  <link rel="apple-touch-icon" href="/static/icons/logo-192.png"/>
  <title>HLS Player</title>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&display=swap');
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    :root{
      --bg:#0a0a0f;--surface:#111118;--border:#1e1e2e;
      --accent:#e8ff47;--accent2:#47ffe8;--text:#e8e8f0;
      --muted:#555570;--error:#ff4757;--warn:#ffaa00;
    }
    body{
      background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;
      min-height:100vh;display:flex;flex-direction:column;
      align-items:center;padding:40px 20px;gap:24px;
    }
    body.compact .url-badge,
    body.compact .url-input,
    body.compact .stats,
    body.compact .quality-bar,
    body.compact .log{display:none}
    body::before{
      content:'';position:fixed;inset:0;
      background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);
      background-size:48px 48px;opacity:.25;pointer-events:none;z-index:0;
    }
    header{position:relative;z-index:1;text-align:center}
    header h1{font-size:1.8rem;font-weight:800;letter-spacing:-.03em;color:var(--accent)}
    .url-badge{
      font-family:'DM Mono',monospace;font-size:.72rem;color:var(--muted);
      margin-top:6px;max-width:640px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    }
    .url-input{
      position:relative;z-index:1;width:100%;max-width:900px;
      background:var(--surface);border:1px solid var(--border);
      border-radius:8px;padding:12px 16px;display:flex;gap:12px;
    }
    .url-input input{
      flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);
      font-family:'DM Mono',monospace;font-size:.78rem;
      border-radius:6px;padding:8px 12px;outline:none;
    }
    .url-input button{
      background:var(--accent);border:none;color:#000;
      font-family:'DM Mono',monospace;font-size:.78rem;font-weight:500;
      border-radius:6px;padding:8px 16px;cursor:pointer;transition:all .15s;
    }
    .url-input button:hover{background:#d4ff3f}
    .video-wrap{
      position:relative;z-index:1;width:100%;max-width:900px;
      aspect-ratio:16/9;background:#000;border-radius:12px;
      overflow:hidden;border:1px solid var(--border);
    }
    video{width:100%;height:100%;display:block}
    #buf-overlay{
      position:absolute;inset:0;display:none;
      align-items:center;justify-content:center;
      background:rgba(0,0,0,.55);z-index:10;flex-direction:column;gap:10px;
    }
    #buf-overlay.show{display:flex}
    .spinner{width:36px;height:36px;border:3px solid #333;border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite}
    @keyframes spin{to{transform:rotate(360deg)}}
    #buf-msg{font-family:'DM Mono',monospace;font-size:.75rem;color:var(--muted)}
    .sound-btn{
      position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:12;
      background:var(--accent);border:none;color:#000;
      font-family:'DM Mono',monospace;font-size:.9rem;font-weight:500;
      border-radius:8px;padding:14px 22px;cursor:pointer;transition:all .15s;
      box-shadow:0 8px 26px rgba(0,0,0,.45);
    }
    .sound-btn:hover{background:#d4ff3f;transform:translate(-50%,-50%) scale(1.03)}
    .sound-btn.hidden{display:none}
    .stats{position:relative;z-index:1;display:flex;gap:10px;width:100%;max-width:900px;flex-wrap:wrap}
    .stat{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 16px;flex:1;min-width:110px}
    .stat-label{font-family:'DM Mono',monospace;font-size:.62rem;color:var(--muted);letter-spacing:.1em;text-transform:uppercase}
    .stat-value{font-size:.95rem;font-weight:700;color:var(--accent2);margin-top:2px}
    .quality-bar{
      position:relative;z-index:1;width:100%;max-width:900px;
      background:var(--surface);border:1px solid var(--border);
      border-radius:8px;padding:12px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
    }
    .quality-bar label{font-family:'DM Mono',monospace;font-size:.72rem;color:var(--muted)}
    .quality-bar select{
      background:var(--bg);border:1px solid var(--border);color:var(--text);
      font-family:'DM Mono',monospace;font-size:.78rem;
      border-radius:6px;padding:5px 10px;outline:none;cursor:pointer;
    }
    .q-btn{
      background:transparent;border:1px solid var(--border);color:var(--muted);
      font-family:'DM Mono',monospace;font-size:.72rem;
      border-radius:6px;padding:5px 12px;cursor:pointer;transition:all .15s;
    }
    .q-btn:hover{border-color:var(--accent);color:var(--accent)}
    .q-btn.active{border-color:var(--accent);color:var(--accent);background:rgba(232,255,71,.07)}
    .install-btn{display:none}
    body.show-log .install-btn.is-ready{display:inline-block}
    .log{
      position:relative;z-index:1;width:100%;max-width:900px;
      background:var(--surface);border:1px solid var(--border);
      border-radius:10px;padding:12px 16px;max-height:150px;overflow-y:auto;
    }
    .ll{font-family:'DM Mono',monospace;font-size:.72rem;padding:2px 0;border-bottom:1px solid #15151f;color:var(--muted);animation:fi .2s ease}
    .ll.ok{color:var(--accent)} .ll.err{color:var(--error)} .ll.inf{color:var(--accent2)} .ll.warn{color:var(--warn)}
    @keyframes fi{from{opacity:0;transform:translateY(3px)}to{opacity:1}}
    ::-webkit-scrollbar{width:4px} ::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
  </style>
</head>
<body class="{{ 'show-log' if show_log else 'compact' }}">
<header>
  <h1>▶ HLS PLAYER</h1>
  <div class="url-badge" id="url-display"></div>
</header>

<div class="url-input">
  <input type="text" id="url-input" placeholder="Cole a URL .m3u8 aqui">
  <button onclick="connect()">Conectar</button>
</div>

<div class="video-wrap">
  <video id="v" controls autoplay muted playsinline></video>
  <button class="sound-btn" id="sound-btn" type="button" onclick="startWatching()">ASSISTIR EM TELA CHEIA</button>
  <div id="buf-overlay">
    <div class="spinner"></div>
    <div id="buf-msg">rebufferizando...</div>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="stat-label">Status</div><div class="stat-value" id="ss">aguardando URL...</div></div>
  <div class="stat"><div class="stat-label">Qualidade</div><div class="stat-value" id="sq">—</div></div>
  <div class="stat"><div class="stat-label">Bitrate</div><div class="stat-value" id="sb">—</div></div>
  <div class="stat"><div class="stat-label">Buffer</div><div class="stat-value" id="sc">—</div></div>
  <div class="stat"><div class="stat-label">Tentativas</div><div class="stat-value" id="sr">0</div></div>
</div>

<div class="quality-bar">
  <label>QUALIDADE</label>
  <button class="q-btn active" onclick="setAuto()">AUTO</button>
  <select id="q-select" onchange="setLevel(this.value)" style="display:none"></select>
  <button class="q-btn install-btn" id="install-btn" type="button">INSTALAR</button>
</div>

<div class="log" id="log"></div>

<script>
let URL_M3U8 = {{ url|tojson }};
const video = document.getElementById("v");
video.muted = true;
video.defaultMuted = true;
const overlay = document.getElementById("buf-overlay");
const bufMsg = document.getElementById("buf-msg");
const inputElement = document.getElementById('url-input');
let retries = 0;
let hls;
let fullscreenDone = false;
let deferredInstallPrompt = null;
let hasUserStarted = false;
let shouldResumeAfterLoad = false;

if(URL_M3U8){
  inputElement.value = URL_M3U8;
  document.getElementById('url-display').textContent = URL_M3U8;
  document.getElementById('url-display').title = URL_M3U8;
}

// Polling para verificar mudanças na URL sem recarregar a página.
setInterval(() => {
  fetch('/current_url')
    .then(r => r.text())
    .then(url => {
      if(url !== URL_M3U8 && url.trim()){
        log("Trocando canal","inf");
        switchStream(url, { resumeCurrent: true, source: "admin" });
      }
    })
    .catch(err => console.log('Erro no polling:', err));
}, 5000);

// Registrar Service Worker para PWA
if('serviceWorker' in navigator){
  navigator.serviceWorker.register('/sw.js')
    .then(reg => console.log('SW registrado'))
    .catch(err => console.log('Erro SW:', err));
}

window.addEventListener('beforeinstallprompt', (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  const installButton = document.getElementById('install-btn');
  installButton.classList.add('is-ready');
  installButton.onclick = async () => {
    installButton.classList.remove('is-ready');
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
  };
});

window.addEventListener('appinstalled', () => {
  deferredInstallPrompt = null;
  document.getElementById('install-btn').classList.remove('is-ready');
});

function log(msg, t=""){
  const d=document.getElementById("log"), l=document.createElement("div");
  l.className="ll "+t;
  l.textContent="["+new Date().toLocaleTimeString()+"] "+msg;
  d.appendChild(l); d.scrollTop=d.scrollHeight;
}
function st(id,v){ document.getElementById(id).textContent=v }
function showBuf(msg){ overlay.classList.add("show"); bufMsg.textContent=msg||"rebufferizando..." }
function hideBuf(){ overlay.classList.remove("show") }
function showSoundButton(){
  document.getElementById("sound-btn").classList.remove("hidden");
}
function hideSoundButton(){
  document.getElementById("sound-btn").classList.add("hidden");
}
function updateUrlDisplay(url){
  inputElement.value = url;
  document.getElementById('url-display').textContent = url;
  document.getElementById('url-display').title = url;
}
function resetStreamStats(){
  retries = 0;
  st("sr", retries);
  st("sq","—");
  st("sb","—");
  st("sc","—");
}
function resumePlaybackAfterLoad(){
  if(!shouldResumeAfterLoad) return;
  shouldResumeAfterLoad = false;
  video.play()
    .then(() => {
      if(video.muted || video.volume === 0) showSoundButton();
      else hideSoundButton();
      log("Canal trocado sem recarregar","ok");
    })
    .catch(() => {
      showSoundButton();
      log("Canal trocado - toque em ASSISTIR para continuar","warn");
    });
}
function startMutedAutoplay(){
  video.muted = true;
  video.defaultMuted = true;
  video.play()
    .then(() => {
      st("ss","▶ reproduzindo");
      showSoundButton();
      log("Autoplay mutado ativo","ok");
    })
    .catch(() => {
      showSoundButton();
      log("Autoplay bloqueado - toque em ASSISTIR","warn");
    });
}
function switchStream(newUrl, options = {}){
  newUrl = (newUrl || "").trim();
  if(!newUrl || newUrl === URL_M3U8) return;

  const wasPlaying = hasUserStarted && !video.paused;
  const wasMuted = video.muted;
  URL_M3U8 = newUrl;
  shouldResumeAfterLoad = Boolean(options.resumeCurrent && wasPlaying);

  updateUrlDisplay(newUrl);
  resetStreamStats();
  st("ss", options.source === "admin" ? "trocando canal..." : "carregando...");
  showBuf(options.source === "admin" ? "trocando canal..." : "carregando stream...");
  video.muted = wasMuted;
  video.defaultMuted = wasMuted;

  if(Hls.isSupported()){
    if(hls){
      hls.stopLoad();
      hls.loadSource(URL_M3U8);
      hls.startLoad();
    } else {
      initHls();
    }
  } else if(video.canPlayType("application/vnd.apple.mpegurl")){
    if(hls){ hls.destroy(); hls = null; }
    video.src = URL_M3U8;
    st("ss","pronto");
    hideBuf();
    if(shouldResumeAfterLoad) resumePlaybackAfterLoad();
    else if(!hasUserStarted) startMutedAutoplay();
    else showSoundButton();
  } else {
    hideBuf();
    log("HLS não suportado neste browser.","err");
    st("ss","✗ sem suporte");
  }
}
async function startWatching(){
  hasUserStarted = true;
  video.muted = false;
  video.defaultMuted = false;
  video.volume = 1;
  try {
    await video.play();
    hideSoundButton();
    log("Som ativado pelo usuário","inf");
    tryFullscreen();
  } catch (err) {
    log("Browser bloqueou reprodução com áudio — toque novamente","warn");
  }
}

video.addEventListener("volumechange", () => {
  if(video.muted || video.volume === 0){
    showSoundButton();
  } else {
    hideSoundButton();
  }
});

// Tenta fullscreen a partir do toque no botão ASSISTIR.
function tryFullscreen(){
  if(fullscreenDone) return;
  const el = video;
  if(el.webkitEnterFullscreen){
    el.webkitEnterFullscreen();
    fullscreenDone = true;
    return;
  }
  const req = el.requestFullscreen
    || el.webkitRequestFullscreen
    || el.mozRequestFullScreen
    || el.msRequestFullscreen;
  if(req){
    const result = req.call(el);
    fullscreenDone = true;
    if(result && result.catch){
      result.catch(()=>{
        fullscreenDone = false;
        log("Fullscreen bloqueado pelo browser - toque em ASSISTIR novamente","warn");
      });
    }
  }
}

function initHls(){
  if(!URL_M3U8) return;
  if(hls){ hls.destroy() }

  hls = new Hls({
    maxBufferLength: 60,
    maxMaxBufferLength: 120,
    maxBufferSize: 60 * 1000000,
    maxBufferHole: 1.0,
    fragLoadingTimeOut: 30000,
    fragLoadingMaxRetry: 6,
    fragLoadingRetryDelay: 1000,
    fragLoadingMaxRetryTimeout: 8000,
    manifestLoadingTimeOut: 20000,
    manifestLoadingMaxRetry: 4,
    manifestLoadingRetryDelay: 1000,
    levelLoadingTimeOut: 20000,
    levelLoadingMaxRetry: 4,
    levelLoadingRetryDelay: 1000,
    nudgeMaxRetry: 10,
    nudgeOffset: 0.2,
    abrEwmaDefaultEstimate: 1000000,
    abrBandWidthFactor: 0.8,
    startLevel: -1,
  });

  hls.loadSource(URL_M3U8);
  hls.attachMedia(video);

  hls.on(Hls.Events.MANIFEST_PARSED, (e, d) => {
    log("Manifest OK — "+d.levels.length+" qualidade(s)","ok");
    st("ss","pronto");
    hideBuf();
    const sel = document.getElementById("q-select");
    sel.innerHTML = "";
    sel.style.display = "none";
    d.levels.forEach((l,i)=>{
      const opt=document.createElement("option");
      opt.value=i;
      opt.textContent=l.height+"p — "+(l.bitrate/1000).toFixed(0)+"kbps";
      sel.appendChild(opt);
    });
    if(d.levels.length>1) sel.style.display="inline-block";

    if(shouldResumeAfterLoad){
      resumePlaybackAfterLoad();
    } else if(!hasUserStarted){
      startMutedAutoplay();
    } else {
      showSoundButton();
      log("Stream pronto - toque em ASSISTIR","inf");
    }
  });

  hls.on(Hls.Events.LEVEL_SWITCHED, (e, d) => {
    const l=hls.levels[d.level];
    st("sq", l.height+"p");
    st("sb", (l.bitrate/1000).toFixed(0)+" kbps");
    document.getElementById("q-select").value = d.level;
  });

  // Mantem o status e o CTA de som sincronizados com o estado real do video.
  video.addEventListener("playing", ()=>{
    hideBuf();
    st("ss","▶ reproduzindo");
    if(video.muted) showSoundButton();
  }, { once: false });

  video.addEventListener("waiting", ()=>{ showBuf("carregando fragmento..."); st("ss","⏳ buffering") });
  video.addEventListener("stalled", ()=>{ showBuf("stream travado, aguardando...") });

  hls.on(Hls.Events.ERROR, (e, d) => {
    if(d.details === "bufferStalledError"){
      log("Buffer travado — tentando recuperar","warn");
      showBuf("buffer travado, recuperando...");
      if(!video.paused){
        const bump = video.currentTime + 0.5;
        if(bump < video.duration || isNaN(video.duration)) video.currentTime = bump;
      }
      return;
    }
    if(d.details === "fragLoadTimeOut"){
      retries++;
      st("sr", retries);
      log("Timeout no fragmento — re-tentando ("+retries+")","warn");
      showBuf("timeout, re-tentando...");
      return;
    }
    if(d.fatal){
      log("Erro fatal: "+d.details,"err");
      if(d.type === Hls.ErrorTypes.NETWORK_ERROR){
        log("Erro de rede — reconectando em 3s...","warn");
        showBuf("reconectando...");
        setTimeout(()=>{ hls.startLoad() }, 3000);
      } else if(d.type === Hls.ErrorTypes.MEDIA_ERROR){
        log("Erro de mídia — tentando recuperar...","warn");
        hls.recoverMediaError();
      } else {
        st("ss","✗ erro fatal");
      }
    } else {
      log("Aviso: "+d.details,"warn");
    }
  });

  setInterval(()=>{
    if(video.buffered.length>0){
      const buf=(video.buffered.end(video.buffered.length-1)-video.currentTime).toFixed(1);
      st("sc", buf+"s");
    }
  }, 1000);
}

function setAuto(){
  if(!hls) return;
  hls.currentLevel = -1;
  document.querySelector(".q-btn").classList.add("active");
  log("Qualidade: AUTO","inf");
}
function setLevel(i){
  if(!hls) return;
  hls.currentLevel = parseInt(i);
  document.querySelector(".q-btn").classList.remove("active");
  const l=hls.levels[i];
  log("Qualidade manual: "+l.height+"p","inf");
}

function connect(){
  const input = document.getElementById('url-input');
  const newUrl = input.value.trim();
  if(!newUrl){
    log("URL vazia","warn");
    return;
  }
  switchStream(newUrl, { resumeCurrent: true, source: "manual" });
}

if(URL_M3U8 && Hls.isSupported()){
  initHls();
} else if(URL_M3U8 && video.canPlayType("application/vnd.apple.mpegurl")){
  video.src=URL_M3U8;
  showSoundButton();
  st("ss","pronto");
  log("Player nativo pronto - toque em ASSISTIR","ok");
  startMutedAutoplay();
} else if(URL_M3U8){
  log("HLS não suportado neste browser.","err"); st("ss","✗ sem suporte");
}
</script>
</body>
</html>"""

@app.route("/")
def index():
    url = request.args.get('url', current_url)
    show_log = 'showLog' in request.args
    return render_template_string(HTML, url=url, show_log=show_log)

@app.route("/current_url")
def get_current_url():
    return current_url

@app.route("/manifest.json")
def manifest():
    return send_from_directory(BASE_DIR, 'manifest.json', mimetype='application/manifest+json')

@app.route("/sw.js")
def sw():
    return send_from_directory(BASE_DIR, 'sw.js', mimetype='application/javascript')

@app.route("/favicon.ico")
def favicon():
    return send_file(os.path.join(BASE_DIR, "static", "icons", "logo-48.png"), mimetype="image/png")

@app.route("/admin", methods=["GET", "POST"])
def admin():
    global current_url
    message = ""
    if request.method == "POST":
        new_url = request.form.get("url", "").strip()
        current_url = new_url
        message = f"URL atualizada com sucesso para: {new_url}"
    return f'''
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
      <meta charset="UTF-8"/>
      <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
      <title>Admin - HLS Player</title>
      <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&display=swap');
        *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
        :root{{
          --bg:#0a0a0f;--surface:#111118;--border:#1e1e2e;
          --accent:#e8ff47;--accent2:#47ffe8;--text:#e8e8f0;
          --muted:#555570;--error:#ff4757;--warn:#ffaa00;
        }}
        body{{
          background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;
          min-height:100vh;display:flex;flex-direction:column;
          align-items:center;padding:40px 20px;gap:24px;
        }}
        body::before{{
          content:'';position:fixed;inset:0;
          background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);
          background-size:48px 48px;opacity:.25;pointer-events:none;z-index:0;
        }}
        header{{position:relative;z-index:1;text-align:center}}
        header h1{{font-size:1.8rem;font-weight:800;letter-spacing:-.03em;color:var(--accent)}}
        .admin-form{{
          position:relative;z-index:1;width:100%;max-width:600px;
          background:var(--surface);border:1px solid var(--border);
          border-radius:8px;padding:20px;display:flex;flex-direction:column;gap:16px;
        }}
        .admin-form label{{font-family:'DM Mono',monospace;font-size:.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}}
        .admin-form input{{
          background:var(--bg);border:1px solid var(--border);color:var(--text);
          font-family:'DM Mono',monospace;font-size:.78rem;
          border-radius:6px;padding:12px;outline:none;width:100%;
        }}
        .admin-form button{{
          background:var(--accent);border:none;color:#000;
          font-family:'DM Mono',monospace;font-size:.78rem;font-weight:500;
          border-radius:6px;padding:12px;cursor:pointer;transition:all .15s;
        }}
        .admin-form button:hover{{background:#d4ff3f}}
        .message{{font-family:'DM Mono',monospace;font-size:.72rem;color:var(--accent2);text-align:center;padding:10px 0}}
        .current{{font-family:'DM Mono',monospace;font-size:.62rem;color:var(--muted);margin-top:8px}}
      </style>
    </head>
    <body>
    <header>
      <h1>ADMIN - HLS PLAYER</h1>
    </header>
    <form class="admin-form" method="post">
      <label>URL DO STREAM ATUAL</label>
      <input type="text" name="url" value="{current_url}" placeholder="Cole a URL .m3u8 aqui">
      <div class="current">Atual: {current_url or 'Nenhuma'}</div>
      <button type="submit">ATUALIZAR URL</button>
      {f'<div class="message">{message}</div>' if message else ''}
    </form>
    </body>
    </html>
    '''

if __name__ == "__main__":
    print(f"App is running on port {PORT}. Access via the Railway URL assigned to your project.")
    if DEFAULT_URL:
        print(f"Default stream URL from START_URL={DEFAULT_URL}")
    print(f"Current URL: {current_url}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
