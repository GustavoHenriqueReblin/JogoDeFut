if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}


(async function init() {
  const core = new PlayerCore();

  const footer = new FooterPanel({
    container: document.getElementById('video-wrap'),
    player:    core.player,
    onSelect:  (name, url, meta) => core.selectChannel(name, url, meta),
  });

  core.onChannelSelect = slug => footer.setActiveSlug(slug);
  core.onInfoUpdate    = meta => footer.setInfo(meta);
  core.onInfoClear     = ()   => footer.clearInfo();

  let channels = [];
  try {
    channels = await fetch('/channels').then(r => r.json());
  } catch {}
  core._resolveChannels(channels);

  let games = [];
  try {
    games = await fetch('/games').then(r => r.json());
  } catch {}
  core._games = games;

  footer.setData(games, channels);
})();
