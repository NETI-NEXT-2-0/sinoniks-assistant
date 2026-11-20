import { createLiquidGlassDisplacementPixels, supportsLiquidGlassSvgFilter } from './vendor/liquid-glass-core.js';

// The vendored MIT inverse-lens core is unchanged. Cache by shape, not keystroke.
const lensCache = new Map<string, { uri: string; scale: number }>();
const svgNS = 'http://www.w3.org/2000/svg';

export function mountGlass(theme: string): () => void {
  let observer: ResizeObserver | null = null;
  let frame = 0;
  const accessibility = matchMedia('(prefers-reduced-transparency: reduce), (prefers-contrast: more)');
  function clear(): void {
    observer?.disconnect();
    document.querySelector('#partner-lenses')?.remove();
    document.querySelectorAll<HTMLElement>('[data-lens]').forEach((element) => {
      delete element.dataset.lens;
      element.style.removeProperty('--lens-filter');
    });
  }
  function sync(): void {
    clear();
    if (!['glass', 'mixed'].includes(theme) || accessibility.matches || !supportsLiquidGlassSvgFilter()) return;
    const svg = document.createElementNS(svgNS, 'svg');
    svg.id = 'partner-lenses';
    svg.setAttribute('aria-hidden', 'true');
    svg.style.cssText = 'position:fixed;width:0;height:0;pointer-events:none';
    const defs = document.createElementNS(svgNS, 'defs');
    svg.append(defs);
    document.body.append(svg);
    const surfaces = document.querySelectorAll<HTMLElement>('.composer,.appearance-panel,.auth-card');
    observer = new ResizeObserver((entries) => {
      if (entries.some(({ target }) => {
        const bounds = target.getBoundingClientRect();
        return (target as HTMLElement).dataset.lensSize !== `${Math.round(bounds.width)}:${Math.round(bounds.height)}`;
      })) queue();
    });
    surfaces.forEach((element, index) => {
      const bounds = element.getBoundingClientRect();
      const width = Math.round(bounds.width), height = Math.round(bounds.height);
      if (!width || !height) return;
      const radius = parseFloat(getComputedStyle(element).borderRadius) || 16;
      const key = `${width}:${height}:${radius}`;
      let map = lensCache.get(key);
      if (!map) {
        const pixels = createLiquidGlassDisplacementPixels({ width, height, radius, magnify: 1.08, spread: .58, bezelRatio: .62, profile: 'soft' });
        const canvas = document.createElement('canvas');
        canvas.width = pixels.width; canvas.height = pixels.height;
        const context = canvas.getContext('2d');
        if (!context) return;
        const image = context.createImageData(canvas.width, canvas.height);
        image.data.set(pixels.data);
        context.putImageData(image, 0, 0);
        map = { uri: canvas.toDataURL(), scale: pixels.scale };
        lensCache.set(key, map);
        if (lensCache.size > 8) {
          const firstKey = lensCache.keys().next().value;
          if (firstKey) lensCache.delete(firstKey);
        }
      }
      const id = `partner-lens-${index}`;
      const filter = document.createElementNS(svgNS, 'filter');
      filter.id = id;
      filter.setAttribute('color-interpolation-filters', 'sRGB');
      for (const [name, value] of Object.entries({ x: '0', y: '0', width: '100%', height: '100%' })) filter.setAttribute(name, value);
      const image = document.createElementNS(svgNS, 'feImage');
      for (const [name, value] of Object.entries({ href: map.uri, x: '0', y: '0', width: '100%', height: '100%', preserveAspectRatio: 'none', result: 'map' })) image.setAttribute(name, value);
      const displacement = document.createElementNS(svgNS, 'feDisplacementMap');
      for (const [name, value] of Object.entries({ in: 'SourceGraphic', in2: 'map', xChannelSelector: 'R', yChannelSelector: 'G', scale: String(map.scale * 1.1) })) displacement.setAttribute(name, value);
      filter.append(image, displacement); defs.append(filter);
      element.dataset.lens = 'true';
      element.dataset.lensSize = `${width}:${height}`;
      element.style.setProperty('--lens-filter', `url("#${id}")`);
      observer?.observe(element);
    });
  }
  function queue(): void { cancelAnimationFrame(frame); frame = requestAnimationFrame(sync); }
  function pointer(event: PointerEvent): void {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches || !['mixed', 'glass'].includes(theme)) return;
    const surface = event.target instanceof Element ? event.target.closest<HTMLElement>('.composer,.appearance-panel,.auth-card') : null;
    if (!surface) return;
    const bounds = surface.getBoundingClientRect();
    surface.style.setProperty('--glare-x', `${Math.round((event.clientX - bounds.left) / bounds.width * 100)}%`);
    surface.style.setProperty('--glare-y', `${Math.round((event.clientY - bounds.top) / bounds.height * 100)}%`);
  }
  window.addEventListener('resize', queue);
  accessibility.addEventListener('change', queue);
  document.addEventListener('pointermove', pointer, { passive: true });
  queue();
  return () => {
    cancelAnimationFrame(frame); clear();
    window.removeEventListener('resize', queue);
    accessibility.removeEventListener('change', queue);
    document.removeEventListener('pointermove', pointer);
  };
}
