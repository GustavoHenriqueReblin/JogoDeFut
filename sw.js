// Service Worker básico para PWA
self.addEventListener('install', (event) => {
  console.log('Service Worker instalado');
});

self.addEventListener('fetch', (event) => {
  // Cache básico - pode expandir se necessário
  event.respondWith(fetch(event.request));
});