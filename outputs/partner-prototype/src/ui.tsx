import { product } from './product';

const icons = {
  plus: 'M12 5v14M5 12h14', search: 'm21 21-4.4-4.4M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  panel: 'M9 3v18M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1',
  arrow: 'M12 19V5m-6 6 6-6 6 6', doc: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zm0 0v6h6M8 13h8m-8 4h5',
  message: 'M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8z',
  attach: 'm21 11-8.5 8.5a6 6 0 0 1-8.5-8.5L13 2a4 4 0 0 1 6 6l-9 9a2 2 0 0 1-3-3l8-8',
  close: 'm6 6 12 12M6 18 18 6', check: 'm5 12 4 4L19 6',
  sun: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1',
  logout: 'M9 5H4v14h5M12 12h9m-4-4 4 4-4 4',
  export: 'M7 10V3h10v7m-10 0-3 4v7h16v-7l-3-4M7 17h10',
  edit: 'm16 3 5 5-12 12-6 1 1-6L16 3zM13 6l5 5',
  refresh: 'M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 2M4 17l2 2a7 7 0 0 0 12-2',
  trash: 'M4 7h16M9 7V3h6v4M6 7l1 14h10l1-14M10 11v6m4-6v6',
  down: 'm6 9 6 6 6-6',
  stop: 'M6 6h12v12H6z',
  copy: 'M9 9h12v12H9zM5 15H3V3h12v2',
} as const;

export function Icon({ name }: { name: keyof typeof icons }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={icons[name]} /></svg>;
}

export function Brand() {
  return <div className="brand" aria-label={product.name}>
    {product.logoLight ? <>
      <img width="227" height="32" className="partner-logo logo-light" src={product.logoLight} alt={product.partnerName} />
      <img width="227" height="32" className="partner-logo logo-dark" src={product.logoDark || product.logoLight} alt={product.partnerName} />
    </> : <span className="partner-wordmark">{product.partnerName || product.name}</span>}
    {product.partnerName && <span className="brand-product">{product.name}</span>}
  </div>;
}
