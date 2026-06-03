['log', 'warn', 'error'].forEach(m => {
  const orig = console[m].bind(console);
  console[m] = (...a) => {
    const ts = new Date().toLocaleTimeString('pt-BR', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    orig(`[${ts}]`, ...a);
  };
});

class PlayerCore {
  constructor() {
    this.videoEl    = document.getElementById('player');
    this.overlay    = document.getElementById('overlay');
    this.spinner    = document.getElementById('spinner');
    this.overlayMsg = document.getElementById('overlay-msg');
    this.retryBtn   = document.getElementById('retry-btn');

    this._lastChannel = null;
    this._currentSlug = null;
    this.activeSlug   = null;
    this._games       = [];
    this.hls          = null;
    this._ws           = null;
    this._wsRetryTimer = null;
    this._onFirstSync  = null;

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
        console.log('[retry] clicado — canal:', this._lastChannel.name, 'slug:', this._lastChannel.slug);
        this.activeSlug   = null;
        this._currentSlug = null;
        this.selectChannel(this._lastChannel.name, this._lastChannel.slug, this._lastChannel.meta);
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

  playStream(slug, meta = null) {
    clearTimeout(this._wsRetryTimer);
    if (this._ws) { this._ws.onclose = null; this._ws.close(); this._ws = null; }
    if (this.hls) { this.hls.destroy(); this.hls = null; }
    this._onFirstSync = null;
    const streamUrl   = '/' + slug;

    const onReady = () => {
      this.player.muted = false;
      this.hideOverlay();
      this.player.play().catch(() => {});
      if (meta) this.onInfoUpdate?.(meta);
    };

    if (Hls.isSupported()) {
      this.hls = new Hls({ enableWorker: false, ...window._HLS_CFG });
      this.hls.attachMedia(this.videoEl);
      this.hls.on(Hls.Events.MANIFEST_PARSED, onReady);
      this.hls.on(Hls.Events.FRAG_LOADED, () => { _mediaErrCount = 0; });

      // primeiro tick do relay = "go" — todos os clients carregam juntos
      this._onFirstSync = () => {
        this.hls.loadSource(streamUrl);
      };
      this._connectWs(slug);
      let _mediaErrCount = 0;
      let _stallCount = 0;
      this.hls.on(Hls.Events.ERROR, (_, data) => {
        console.warn('[hls] error', data.type, data.details, 'fatal:', data.fatal, data);
        if (!data.fatal) {
          if (data.details === Hls.ErrorDetails.BUFFER_STALLED_ERROR) {
            _stallCount++;
            console.warn(`[hls] bufferStall #${_stallCount} — canal: ${slug}`);
            return;
          }
          if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            _mediaErrCount++;
            if (_mediaErrCount <= 3) {
              console.warn(`[hls] codec error #${_mediaErrCount} — recoverMediaError`);
              this.hls.recoverMediaError();
            } else {
              console.warn('[hls] codec errors repetidos — swapAudioCodec + recover');
              _mediaErrCount = 0;
              this.hls.swapAudioCodec();
              this.hls.recoverMediaError();
            }
            return;
          }
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) return;
          return;
        }
        if (data.fatal) {
          const buffered = this.videoEl.buffered;
          const currentTime = this.videoEl.currentTime;
          let bufferAhead = 0;
          for (let i = 0; i < buffered.length; i++) {
            if (buffered.start(i) <= currentTime && currentTime <= buffered.end(i)) {
              bufferAhead = buffered.end(i) - currentTime;
              break;
            }
          }
          this.hls.destroy();
          this.hls = null;
          if (bufferAhead > 2) {
            console.warn(`[hls] FATAL mas buffer ainda tem ${bufferAhead.toFixed(1)}s — aguardando drenar`);
            const checkInterval = setInterval(() => {
              const buf = this.videoEl.buffered;
              const ct = this.videoEl.currentTime;
              let ahead = 0;
              for (let i = 0; i < buf.length; i++) {
                if (buf.start(i) <= ct && ct <= buf.end(i)) { ahead = buf.end(i) - ct; break; }
              }
              if (ahead <= 0.5 || this.videoEl.paused) {
                clearInterval(checkInterval);
                if (this._currentSlug === slug) this._reconnect(slug, meta);
              }
            }, 1000);
          } else {
            console.warn('[hls] FATAL sem buffer — reconectando');
            this._reconnect(slug, meta);
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

  _connectWs(slug, delay = 0) {
    if (this._currentSlug !== slug) return;
    clearTimeout(this._wsRetryTimer);
    this._wsRetryTimer = setTimeout(() => {
      if (this._currentSlug !== slug) return;
      if (this._ws) { this._ws.onclose = null; this._ws.close(); this._ws = null; }
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${proto}//${location.host}/ws/${slug}`);
      this._ws = ws;

      ws.onmessage = ({ data }) => {
        if (this._currentSlug !== slug) return;
        try {
          const msg = JSON.parse(data);
          if (msg.ping) return;
          if (this._onFirstSync) {
            const cb = this._onFirstSync;
            this._onFirstSync = null;
            cb(msg);
          }
        } catch {}
      };
      ws.onerror = () => {};
      ws.onclose = () => {
        if (this._currentSlug !== slug || this._ws !== ws) return;
        console.warn('[ws] desconectado — reconectando em 2s');
        this._connectWs(slug, 2000);
      };
    }, delay);
  }

  _reconnect(slug, meta) {
    if (this._currentSlug !== slug) return;
    console.warn(`[hls] reconectando '${slug}'…`);
    this.showOverlay('Reconectando…', true);
    const attempt = async () => {
      if (this._currentSlug !== slug) return;
      try {
        const r = await fetch('/' + slug);
        if (r.ok && this._currentSlug === slug) {
          console.warn(`[hls] stream '${slug}' disponível — retomando`);
          this.playStream(slug, meta);
          return;
        }
      } catch {}
      setTimeout(attempt, 3000);
    };
    setTimeout(attempt, 2000);
  }

  async selectChannel(name, slug, meta = null) {
    if (this.activeSlug === slug) return;
    this.activeSlug   = slug;
    this._currentSlug = slug;
    this._lastChannel = { name, slug, meta };
    this.onInfoClear?.();
    this.player.fullscreen.enter();

    if (!meta) {
      const match = this._games.find(g => g.embeds?.some(e => e.channel_slug === slug));
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

    this.onChannelSelect?.(slug);

    this.player.muted = true;
    this.showOverlay('Buscando stream…', true);

    let initial;
    try { initial = await fetch('/resolve/' + slug).then(r => r.json()); } catch (e) {
      console.warn('[resolve] fetch inicial falhou:', e);
    }
    if (this._currentSlug !== slug) return;
    console.log('[resolve] resposta inicial:', initial?.status, name);
    if (initial?.status === 'ready') { this.playStream(slug, channelMeta); return; }
    if (initial?.status === 'error') { this.showOverlay('Stream não disponível no momento.', false, true); return; }

    const MAX_WAIT = 30;
    for (let elapsed = 2; elapsed <= MAX_WAIT; elapsed += 2) {
      if (this._currentSlug !== slug) return;
      this.overlayMsg.textContent = `Buscando stream… ${elapsed}s`;
      await new Promise(r => setTimeout(r, 2000));
      if (this._currentSlug !== slug) return;

      let resp;
      try { resp = await fetch('/resolve/status/' + slug).then(r => r.json()); } catch {}

      if (resp?.status === 'ready') { this.playStream(slug, channelMeta); return; }
      if (resp?.status === 'error') { this.showOverlay('Stream não disponível no momento.', false, true); return; }
    }

    if (this._currentSlug === slug) this.showOverlay('Timeout: stream não disponível.', false, true);
  }
}
