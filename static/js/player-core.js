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
        this.activeUrl = null;
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
      this.hls = new Hls({ enableWorker: false });
      this.hls.loadSource(streamUrl);
      this.hls.attachMedia(this.videoEl);
      this.hls.on(Hls.Events.MANIFEST_PARSED, onReady);
      this.hls.on(Hls.Events.ERROR, (_, data) => {
        if (data.fatal) this.showOverlay('Falha ao reproduzir.', false, true);
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

    try { await fetch('/resolve?url=' + url); } catch {}

    const MAX_WAIT = 120;
    for (let elapsed = 0; elapsed <= MAX_WAIT; elapsed += 2) {
      if (this._currentUrl !== url) return;

      let resp;
      try { resp = await fetch('/resolve/status?url=' + url).then(r => r.json()); } catch {}

      if (resp?.status === 'ready') { this.playStream(url, channelMeta); return; }
      if (resp?.status === 'error') { this.showOverlay('Stream não disponível no momento.', false, true); return; }

      if (elapsed < MAX_WAIT) {
        if (elapsed > 0) this.overlayMsg.textContent = `Buscando stream… ${elapsed}s`;
        await new Promise(r => setTimeout(r, 2000));
      }
    }

    if (this._currentUrl === url) this.showOverlay('Timeout: stream não disponível.', false, true);
  }
}
