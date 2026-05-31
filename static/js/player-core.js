class PlayerCore {
  constructor() {
    this.videoEl    = document.getElementById('player');
    this.overlay    = document.getElementById('overlay');
    this.spinner    = document.getElementById('spinner');
    this.overlayMsg = document.getElementById('overlay-msg');
    this.retryBtn   = document.getElementById('retry-btn');

    this._lastChannel = null;
    this._currentUrl  = null;
    this.activeUrl    = null;
    this._games       = [];
    this.hls          = null;

    this._resolveChannels = null;
    this.channelsReady = new Promise(r => { this._resolveChannels = r; });

    this.onChannelSelect = null;
    this.onInfoUpdate    = null;
    this.onInfoClear     = null;

    this._initPlayer();
    this._initKeys();
    this._bindRetry();
  }

  _initPlayer() {
    this.player = new Plyr(this.videoEl, {
      controls: [],
      clickToPlay: false,
      volume: 0.5,
      resetOnEnd: false,
      storage: { enabled: false },
      fullscreen: { container: '#video-wrap' },
    });

    this.player.on('enterfullscreen', () => {
      screen.orientation?.lock('landscape').catch(() => {});
    });
    this.player.on('exitfullscreen', () => {
      screen.orientation?.unlock();
    });
  }

  _initKeys() {
    document.addEventListener('keydown', e => {
      if (e.key === 'f' || e.key === 'F') {
        e.preventDefault();
        this.player.fullscreen.toggle();
      }
    });
  }

  _bindRetry() {
    this.retryBtn.addEventListener('click', () => {
      if (this._lastChannel) {
        console.log('[retry] clicado — canal:', this._lastChannel.name, 'url:', this._lastChannel.url);
        this.activeUrl = null;
        this._currentUrl = null;
        this.selectChannel(this._lastChannel.name, this._lastChannel.url, this._lastChannel.meta);
      }
    });
  }

  showOverlay(msg, loading = false, canRetry = false) {
    this.overlay.classList.remove('hidden');
    this.overlayMsg.textContent = msg;
    this.spinner.style.display  = loading  ? 'block' : 'none';
    this.retryBtn.style.display = canRetry ? 'block' : 'none';
  }

  hideOverlay() {
    this.overlay.classList.add('hidden');
    this.retryBtn.style.display = 'none';
  }

  playStream(encUrl, meta = null) {
    if (this.hls) { this.hls.destroy(); this.hls = null; }
    const streamUrl = '/stream?url=' + encUrl;

    const onReady = () => {
      this.player.muted = false;
      this.hideOverlay();
      this.player.play().catch(() => {});
      if (meta) this.onInfoUpdate?.(meta);
    };

    if (Hls.isSupported()) {
      this.hls = new Hls({ enableWorker: false, ...window._HLS_CFG });
      this.hls.loadSource(streamUrl);
      this.hls.attachMedia(this.videoEl);
      this.hls.on(Hls.Events.MANIFEST_PARSED, onReady);
      this.hls.on(Hls.Events.ERROR, (_, data) => {
        console.warn('[hls] error', data.type, data.details, 'fatal:', data.fatal, data);
        if (data.fatal) {
          // só mostra erro ao usuário quando o buffer estiver vazio
          // se ainda tem conteúdo, o player continua reproduzindo — erro silencioso
          const buffered = this.videoEl.buffered;
          const currentTime = this.videoEl.currentTime;
          let bufferAhead = 0;
          for (let i = 0; i < buffered.length; i++) {
            if (buffered.start(i) <= currentTime && currentTime <= buffered.end(i)) {
              bufferAhead = buffered.end(i) - currentTime;
              break;
            }
          }
          if (bufferAhead > 2) {
            console.warn(`[hls] FATAL mas buffer ainda tem ${bufferAhead.toFixed(1)}s — aguardando drenar antes de exibir erro`);
            this.hls.destroy();
            this.hls = null;
            const checkInterval = setInterval(() => {
              const buf = this.videoEl.buffered;
              const ct = this.videoEl.currentTime;
              let ahead = 0;
              for (let i = 0; i < buf.length; i++) {
                if (buf.start(i) <= ct && ct <= buf.end(i)) { ahead = buf.end(i) - ct; break; }
              }
              if (ahead <= 0.5 || this.videoEl.paused) {
                clearInterval(checkInterval);
                if (this._currentUrl === url) this.showOverlay('Falha ao reproduzir.', false, true);
              }
            }, 1000);
          } else {
            console.error('[hls] FATAL sem buffer — mostrando erro imediatamente');
            this.hls.destroy();
            this.hls = null;
            this.showOverlay('Falha ao reproduzir.', false, true);
          }
        }
      });
    } else if (this.videoEl.canPlayType('application/vnd.apple.mpegurl')) {
      this.player.source = { type: 'video', sources: [{ src: streamUrl, type: 'application/x-mpegURL' }] };
      this.videoEl.addEventListener('loadedmetadata', onReady, { once: true });
    } else {
      this.showOverlay('HLS não suportado neste browser');
    }
  }

  async selectChannel(name, url, meta = null) {
    if (this.activeUrl === url) return;
    this.activeUrl    = url;
    this._currentUrl  = url;
    this._lastChannel = { name, url, meta };
    this.onInfoClear?.();
    this.player.fullscreen.enter();

    if (!meta) {
      const match = this._games.find(g => g.embeds?.some(e => e.channel_url === url));
      if (match) {
        const embed = match.embeds[0];
        meta = {
          type: 'game', channelName: embed.channel_name,
          title: match.title, desc: match.description || '',
          time: match.start_time.slice(11, 16),
        };
      }
    }
    const channelMeta = meta || { type: 'channel', name };

    this.onChannelSelect?.(url);

    this.player.muted = true;
    this.showOverlay('Buscando stream…', true);

    let initial;
    try { initial = await fetch('/resolve?url=' + url).then(r => r.json()); } catch (e) {
      console.warn('[resolve] fetch inicial falhou:', e);
    }
    if (this._currentUrl !== url) return;
    console.log('[resolve] resposta inicial:', initial?.status, name);
    if (initial?.status === 'ready') { this.playStream(url, channelMeta); return; }
    if (initial?.status === 'error') { this.showOverlay('Stream não disponível no momento.', false, true); return; }

    const MAX_WAIT = 30;
    for (let elapsed = 2; elapsed <= MAX_WAIT; elapsed += 2) {
      if (this._currentUrl !== url) return;
      this.overlayMsg.textContent = `Buscando stream… ${elapsed}s`;
      await new Promise(r => setTimeout(r, 2000));
      if (this._currentUrl !== url) return;

      let resp;
      try { resp = await fetch('/resolve/status?url=' + url).then(r => r.json()); } catch {}

      if (resp?.status === 'ready') { this.playStream(url, channelMeta); return; }
      if (resp?.status === 'error') { this.showOverlay('Stream não disponível no momento.', false, true); return; }
    }

    if (this._currentUrl === url) this.showOverlay('Timeout: stream não disponível.', false, true);
  }
}
