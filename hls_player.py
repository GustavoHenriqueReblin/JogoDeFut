#!/usr/bin/env python3
import os
import sys
import subprocess

def install(pkg):
    subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    from flask import Flask, render_template_string
    from flask_cors import CORS
except ImportError:
    print("Instalando dependências...")
    install("flask")
    install("flask-cors")
    from flask import Flask, render_template_string
    from flask_cors import CORS

app = Flask(__name__)
CORS(app)
PORT = int(os.environ.get('PORT', 5000))

HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
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
<body>
<header>
  <h1>▶ HLS PLAYER</h1>
  <div class="url-badge" id="url-display"></div>
</header>

<div class="url-input">
  <input type="text" id="url-input" placeholder="Cole a URL .m3u8 aqui">
  <button onclick="connect()">Conectar</button>
</div>

<div class="video-wrap">
  <video id="v" controls autoplay playsinline></video>
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
</div>

<div class="log" id="log"></div>

<script>
let URL_M3U8 = {{ url|tojson }};
const video = document.getElementById("v");
const overlay = document.getElementById("buf-overlay");
const bufMsg = document.getElementById("buf-msg");
let retries = 0;
let hls;
let fullscreenDone = false;

function log(msg, t=""){
  const d=document.getElementById("log"), l=document.createElement("div");
  l.className="ll "+t;
  l.textContent="["+new Date().toLocaleTimeString()+"] "+msg;
  d.appendChild(l); d.scrollTop=d.scrollHeight;
}
function st(id,v){ document.getElementById(id).textContent=v }
function showBuf(msg){ overlay.classList.add("show"); bufMsg.textContent=msg||"rebufferizando..." }
function hideBuf(){ overlay.classList.remove("show") }

// Tenta fullscreen — browsers exigem que seja disparado dentro de um evento
// ou logo após uma interação do usuário. Aqui tentamos assim que o vídeo começa
// a tocar, que conta como interação iniciada pelo usuário via autoplay.
function tryFullscreen(){
  if(fullscreenDone) return;
  fullscreenDone = true;
  const el = video;
  const req = el.requestFullscreen
    || el.webkitRequestFullscreen
    || el.mozRequestFullScreen
    || el.msRequestFullscreen;
  if(req){
    req.call(el).catch(err=>{
      // Navegador bloqueou fullscreen automático (política do browser)
      // Aparece um botão pra o usuário clicar manualmente
      log("Fullscreen bloqueado pelo browser — clique no botão ⛶ no player","warn");
    });
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
    st("ss","▶ reproduzindo");
    hideBuf();
    const sel = document.getElementById("q-select");
    sel.innerHTML = "";
    d.levels.forEach((l,i)=>{
      const opt=document.createElement("option");
      opt.value=i;
      opt.textContent=l.height+"p — "+(l.bitrate/1000).toFixed(0)+"kbps";
      sel.appendChild(opt);
    });
    if(d.levels.length>1) sel.style.display="inline-block";

    // Inicia o vídeo — fullscreen é pedido assim que começar a tocar
    video.play().catch(()=>{
      // Autoplay com som bloqueado? Tenta novamente
      video.play();
    });
  });

  hls.on(Hls.Events.LEVEL_SWITCHED, (e, d) => {
    const l=hls.levels[d.level];
    st("sq", l.height+"p");
    st("sb", (l.bitrate/1000).toFixed(0)+" kbps");
    document.getElementById("q-select").value = d.level;
  });

  // Assim que o vídeo começa a tocar de verdade → fullscreen
  video.addEventListener("playing", ()=>{
    hideBuf();
    st("ss","▶ reproduzindo");
    tryFullscreen();
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
  URL_M3U8 = newUrl;
  document.getElementById('url-display').textContent = newUrl;
  document.getElementById('url-display').title = newUrl;
  st("ss","carregando...");
  retries = 0;
  st("sr", retries);
  st("sq","—");
  st("sb","—");
  st("sc","—");
  video.muted = false;
  initHls();
}

if(URL_M3U8 && Hls.isSupported()){
  initHls();
} else if(URL_M3U8 && video.canPlayType("application/vnd.apple.mpegurl")){
  video.src=URL_M3U8;
  video.play().catch(()=>{ video.play(); });
  log("Player nativo","ok"); st("ss","▶ nativo");
} else if(URL_M3U8){
  log("HLS não suportado neste browser.","err"); st("ss","✗ sem suporte");
}
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML, url="")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
