// Only file drags are intercepted; text selection and ordinary links keep native behavior.
export function registerFileDrop(target, { onActive, onFiles, isAvailable }) {
  let depth = 0;
  const isFile = event => !!event.dataTransfer?.types.includes('Files');
  const reset = () => { depth = 0; onActive(false); };
  const enter = event => { if (!isFile(event)) return; event.preventDefault(); depth++; onActive(true); };
  const over = event => { if (!isFile(event)) return; event.preventDefault(); event.dataTransfer.dropEffect = isAvailable() ? 'copy' : 'none'; };
  const leave = event => { if (!isFile(event)) return; depth = Math.max(0, depth - 1); if (!depth) reset(); };
  const drop = event => { if (!isFile(event)) return; event.preventDefault(); reset(); const files = Array.from(event.dataTransfer.files); if (files.length && isAvailable()) onFiles(files); };
  const escape = event => { if (event.key === 'Escape') reset(); };
  const handlers = { dragenter: enter, dragover: over, dragleave: leave, drop, blur: reset, keydown: escape };
  for (const [name, handler] of Object.entries(handlers)) target.addEventListener(name, handler);
  return () => { for (const [name, handler] of Object.entries(handlers)) target.removeEventListener(name, handler); depth = 0; };
}
