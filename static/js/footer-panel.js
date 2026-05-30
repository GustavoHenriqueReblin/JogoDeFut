class FooterPanel {
  constructor({ container, player, onSelect }) {
    this.container  = container;
    this.player     = player;
    this.onSelect   = onSelect;
    this._open      = false;
    this._games     = [];
    this._channels  = [];
    this._activeUrl = null;
    this._hideTimer = null;

    this._mount();
    this._bindEvents();
    this.open();
  }

  /* ── DOM ──────────────────────────────────────────────────── */
  _mount() {
    this.el = document.createElement('div');
    this.el.className = 'footer-panel';
    this.el.innerHTML = `
      <div class="footer-body">
        <div class="footer-scroll-wrap">
          <button class="footer-scroll-btn" data-dir="left" aria-label="Rolar esquerda">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="10 4 6 8 10 12"/>
            </svg>
          </button>
          <div class="footer-scroll" id="footer-scroll">
            ${Array(4).fill(`
            <div class="footer-game-card footer-skel-card">
              <div class="footer-skel footer-skel-poster"></div>
              <div class="footer-game-info">
                <div class="footer-skel footer-skel-title"></div>
                <div class="footer-skel footer-skel-desc" style="margin-top:4px"></div>
                <div class="footer-skel footer-skel-meta"></div>
              </div>
            </div>`).join('')}
            <div class="footer-separator"></div>
            ${Array(8).fill(`
            <div class="footer-ch-card footer-skel-card">
              <div class="footer-skel footer-skel-logo"></div>
              <div class="footer-skel footer-skel-chname"></div>
            </div>`).join('')}
          </div>
          <button class="footer-scroll-btn" data-dir="right" aria-label="Rolar direita">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="6 4 10 8 6 12"/>
            </svg>
          </button>
        </div>
      </div>

      <button class="footer-toggle" aria-label="Canais e jogos">
        <span class="footer-toggle__label">Canais e Jogos</span>
        <svg class="footer-toggle__chevron" width="12" height="12" viewBox="0 0 14 14"
             fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <polyline points="3 9 7 5 11 9"/>
        </svg>
      </button>

      <div class="footer-controls">
        <div class="footer-ctrl-left">
          <button class="footer-ctrl-btn" id="fp-mute" aria-label="Mudo">
            ${FooterPanel._svgVol(false)}
          </button>
          <input class="footer-volume" id="fp-vol" type="range"
                 min="0" max="1" step="0.02" value="0.5">
        </div>
        <div class="footer-ctrl-center">
          <span class="footer-info-title" id="fp-title"></span>
          <div class="footer-info-sub">
            <span class="footer-info-live hidden" id="fp-live">
              <span class="live-dot"></span>AO VIVO
            </span>
            <span class="footer-info-meta" id="fp-meta"></span>
          </div>
        </div>
        <div class="footer-ctrl-right">
          <button class="footer-ctrl-btn" id="fp-fs" aria-label="Tela cheia">
            ${FooterPanel._svgExpand()}
          </button>
        </div>
      </div>`;

    this.container.appendChild(this.el);

    this.toggleBtn = this.el.querySelector('.footer-toggle');
    this.scrollEl  = this.el.querySelector('#footer-scroll');
    this.muteBtn   = this.el.querySelector('#fp-mute');
    this.volSlider = this.el.querySelector('#fp-vol');
    this.fsBtn     = this.el.querySelector('#fp-fs');
    this.infoTitle = this.el.querySelector('#fp-title');
    this.infoMeta  = this.el.querySelector('#fp-meta');
    this.infoLive  = this.el.querySelector('#fp-live');

    this.player.on('ready', () => this._initPlayerControls());
  }

  /* ── Player controls ─────────────────────────────────────── */
  _initPlayerControls() {
    this._updateVol();

    this.player.on('volumechange',    () => this._updateVol());
    this.player.on('enterfullscreen', () => { this.fsBtn.innerHTML = FooterPanel._svgCompress(); });
    this.player.on('exitfullscreen',  () => { this.fsBtn.innerHTML = FooterPanel._svgExpand(); });

    this.muteBtn.addEventListener('click', () => {
      this.player.muted = !this.player.muted;
    });

    this.volSlider.addEventListener('input', () => {
      const v = parseFloat(this.volSlider.value);
      this.player.volume = v;
      if (this.player.muted && v > 0) this.player.muted = false;
      this.volSlider.style.setProperty('--vol', `${v * 100}%`);
    });

    this.fsBtn.addEventListener('click', () => this.player.fullscreen.toggle());
  }

  _updateVol() {
    const muted = this.player.muted || this.player.volume === 0;
    const v     = this.player.volume;
    this.muteBtn.innerHTML = FooterPanel._svgVol(muted);
    this.volSlider.value   = muted ? 0 : v;
    this.volSlider.style.setProperty('--vol', `${(muted ? 0 : v) * 100}%`);
  }

  static _svgVol(muted) {
    return muted
      ? `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
           <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
           <line x1="23" y1="9" x2="17" y2="15"/>
           <line x1="17" y1="9" x2="23" y2="15"/>
         </svg>`
      : `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
           <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
           <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
         </svg>`;
  }

  static _svgExpand() {
    return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
       <polyline points="15 3 21 3 21 9"/>
       <polyline points="9 21 3 21 3 15"/>
       <line x1="21" y1="3" x2="14" y2="10"/>
       <line x1="3" y1="21" x2="10" y2="14"/>
     </svg>`;
  }

  static _svgCompress() {
    return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
       <polyline points="4 14 10 14 10 20"/>
       <polyline points="20 10 14 10 14 4"/>
       <line x1="10" y1="14" x2="3" y2="21"/>
       <line x1="21" y1="3" x2="14" y2="10"/>
     </svg>`;
  }

  /* ── Events ──────────────────────────────────────────────── */
  _bindEvents() {
    // Mouse movement shows controls
    this.container.addEventListener('mousemove', () => this._show());

    // Click on video (outside footer): toggle visibility — PC only
    // (mobile is handled in touchend to bypass Plyr pointer-events interception)
    let _touchHandled = false;
    this.container.addEventListener('click', e => {
      if (_touchHandled) { _touchHandled = false; return; }
      if (!this.el.contains(e.target)) {
        this.el.classList.contains('visible') ? this._hideAll() : this._show();
      }
    });

    // Toggle button opens/closes the cards panel only
    this.toggleBtn.addEventListener('click', () => this.toggle());

    this.el.querySelector('[data-dir="left"]').addEventListener('click', () => {
      this.scrollEl.scrollBy({ left: -220, behavior: 'smooth' });
    });
    this.el.querySelector('[data-dir="right"]').addEventListener('click', () => {
      this.scrollEl.scrollBy({ left:  220, behavior: 'smooth' });
    });

    // Mobile: swipe + tap handling
    let _tx0 = null, _ty0 = null;
    this.container.addEventListener('touchstart', e => {
      _tx0 = e.touches[0].clientX;
      _ty0 = e.touches[0].clientY;
    }, { passive: true });
    this.container.addEventListener('touchend', e => {
      if (_ty0 === null) return;
      const dx = _tx0 - e.changedTouches[0].clientX;
      const dy = _ty0 - e.changedTouches[0].clientY;
      _touchHandled = true;

      if (!this.el.classList.contains('visible') && Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 50) {
        // swipe horizontal com footer oculto: troca canal
        this._navigateChannel(dx > 0 ? 1 : -1);
      } else if (dy > 50 && !this._open) {
        this.open();
      } else if (dy < -50 && this._open) {
        this.close();
      } else if (Math.abs(dy) < 10 && Math.abs(dx) < 10 && !this.el.contains(e.target)) {
        // tap outside footer: toggle visibility
        this.el.classList.contains('visible') ? this._hideAll() : this._show();
      } else {
        _touchHandled = false;
      }
      _tx0 = null;
      _ty0 = null;
    }, { passive: true });
  }

  /* ── Visibility ──────────────────────────────────────────── */
  _show(ms = 2000) {
    clearTimeout(this._hideTimer);
    this.el.classList.add('visible');
    if (!this._open) {
      this._hideTimer = setTimeout(() => {
        this._hideTimer = null;
        if (!this._open) this.el.classList.remove('visible');
      }, ms);
    }
  }

  _hideAll() {
    clearTimeout(this._hideTimer);
    this._hideTimer = null;
    this._open = false;
    this.el.classList.remove('open', 'visible');
  }

  /* ── Info bar ─────────────────────────────────────────────── */
  setInfo(meta) {
    if (meta.type === 'game') {
      this.infoTitle.textContent = meta.title;
      this.infoMeta.textContent  = [meta.desc, meta.time].filter(Boolean).join(' • ');
    } else {
      this.infoTitle.textContent = meta.name || '';
      this.infoMeta.textContent  = '';
    }
    this.infoLive.classList.remove('hidden');
    this._show(2000);
  }

  clearInfo() {
    this.infoTitle.textContent = '';
    this.infoMeta.textContent  = '';
    this.infoLive.classList.add('hidden');
  }

  /* ── Cards ───────────────────────────────────────────────── */
  setData(games, channels) {
    this._games    = games;
    this._channels = channels;
    this._renderCards();
  }

  setActiveUrl(url) {
    this._activeUrl = url;
    this.el.querySelectorAll('.footer-card').forEach(c => {
      c.classList.toggle('active', c.dataset.url === url);
    });
  }

  _renderCards() {
    this.scrollEl.innerHTML = '';
    this._games.forEach(g => this._appendGameCard(g));

    const usedUrls = new Set(this._games.map(g => g.embeds[0].channel_url));
    const freeChannels = this._channels.filter(ch => !usedUrls.has(ch.url));

    if (this._games.length && freeChannels.length) {
      const sep = document.createElement('div');
      sep.className = 'footer-separator';
      this.scrollEl.appendChild(sep);
    }
    freeChannels.forEach(ch => this._appendChannelCard(ch));
    if (!this._games.length && !freeChannels.length) {
      this.scrollEl.innerHTML = '<span class="footer-empty">Nenhum conteúdo disponível</span>';
    }
  }

  _appendGameCard(game) {
    const embed   = game.embeds[0];
    const timeStr = game.start_time.slice(11, 16);
    const meta    = {
      type: 'game', channelName: embed.channel_name,
      title: game.title, desc: game.description || '', time: timeStr,
    };
    const card = document.createElement('div');
    card.className   = 'footer-card footer-game-card';
    card.dataset.url = embed.channel_url;
    card.innerHTML   = `
      <img class="footer-game-poster" src="${game.poster}" alt="" loading="lazy"
           onerror="this.style.display='none'">
      <div class="footer-game-info">
        <div class="footer-game-title">${game.title}</div>
        <div class="footer-game-desc">${game.description || ''}</div>
        <div class="footer-game-meta">
          <span class="live-dot"></span>
          <span>${timeStr}</span>
          <img class="footer-game-logo" src="/static/logos/${embed.channel_name}.webp" alt=""
               onerror="this.style.display='none'">
        </div>
      </div>`;
    card.addEventListener('click', () => {
      this.onSelect(embed.channel_name, embed.channel_url, meta);
      this.close();
    });
    this.scrollEl.appendChild(card);
  }

  _appendChannelCard(ch) {
    const card = document.createElement('div');
    card.className   = 'footer-card footer-ch-card';
    card.dataset.url = ch.url;
    card.innerHTML   = `
      <img class="footer-ch-logo" src="/static/logos/${ch.name}.webp" alt="${ch.name}"
           onerror="this.style.display='none'">
      <span class="footer-ch-name">${ch.name}</span>`;
    card.addEventListener('click', () => {
      this.onSelect(ch.name, ch.url);
      this.close();
    });
    this.scrollEl.appendChild(card);
  }

  _navigateChannel(dir) {
    const usedUrls = new Set(this._games.map(g => g.embeds[0].channel_url));
    const items = [
      ...this._games.map(g => ({
        name: g.embeds[0].channel_name,
        url:  g.embeds[0].channel_url,
        meta: { type: 'game', channelName: g.embeds[0].channel_name,
                title: g.title, desc: g.description || '', time: g.start_time.slice(11, 16) },
      })),
      ...this._channels
        .filter(ch => !usedUrls.has(ch.url))
        .map(ch => ({ name: ch.name, url: ch.url, meta: null })),
    ];
    if (!items.length) return;
    const idx = items.findIndex(it => it.url === this._activeUrl);
    const next = items[(idx + dir + items.length) % items.length];
    this.onSelect(next.name, next.url, next.meta);
  }

  /* ── State ───────────────────────────────────────────────── */
  open() {
    clearTimeout(this._hideTimer);
    this._open = true;
    this.el.classList.add('open', 'visible');
    this.scrollEl.scrollTo({ left: 0 });
  }

  close() {
    this._open = false;
    this.el.classList.remove('open');
    this._show(2000);
  }

  toggle() {
    this._open ? this.close() : this.open();
  }
}
