// Ready-made local H.264 footage. No WebGL context or animation render loop.
export function mountVideoBackdrop(): { sync: (visible: boolean) => void; dispose: () => void } {
  const host = document.createElement('div');
  host.className = 'scene-3d scene-video-bg';
  host.hidden = true;
  host.setAttribute('aria-hidden', 'true');
  host.dataset.renderer = 'video';
  const video = document.createElement('video');
  video.className = 'scene-video';
  video.muted = true;
  video.defaultMuted = true;
  video.loop = true;
  video.playsInline = true;
  video.preload = 'none';
  video.poster = '/assets/liquid-poster.jpg';
  video.setAttribute('muted', '');
  video.setAttribute('playsinline', '');
  video.setAttribute('disablepictureinpicture', '');
  const veil = document.createElement('div');
  veil.className = 'scene-vignette';
  host.append(video, veil);
  document.body.prepend(host);
  let enabled = false;
  let loaded = false;
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  const transparency = matchMedia('(prefers-reduced-transparency: reduce), (prefers-contrast: more)');
  function update(): void {
    const visible = enabled && !transparency.matches;
    host.hidden = !visible;
    if (!visible || document.hidden || motion.matches) {
      video.pause();
      return;
    }
    if (!loaded) {
      video.src = '/assets/liquid-loop.mp4';
      loaded = true;
    }
    void video.play().catch(() => { host.dataset.playback = 'poster'; });
  }
  video.onplaying = () => { host.dataset.playback = 'playing'; };
  video.onpause = () => { host.dataset.playback = 'paused'; };
  video.onerror = () => { host.dataset.playback = 'poster'; };
  document.addEventListener('visibilitychange', update);
  motion.addEventListener('change', update);
  transparency.addEventListener('change', update);
  return {
    sync(visible) { enabled = visible; update(); },
    dispose() {
      document.removeEventListener('visibilitychange', update);
      motion.removeEventListener('change', update);
      transparency.removeEventListener('change', update);
      video.pause();
      host.remove();
    },
  };
}
