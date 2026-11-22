import { useLayoutEffect, useRef, useState } from 'react';
import { AnswerPlayback } from './answer-playback.mjs';

export interface AnswerTarget { id: string; text: string; finished: boolean; stopped: boolean }
export function useAnswerPlayback(scope: string, targets: AnswerTarget[], follow: () => void) {
  const store = useRef({ scope: '', entries: new Map<string, AnswerPlayback>() });
  const followRef = useRef(follow); followRef.current = follow;
  const [revision, setRevision] = useState(0);
  useLayoutEffect(() => { followRef.current(); }, [revision]);
  useLayoutEffect(() => {
    const fresh = store.current.scope !== scope;
    if (fresh) store.current = { scope, entries: new Map() };
    const { entries } = store.current;
    const motion = matchMedia('(prefers-reduced-motion: reduce)');
    const immediate = (): boolean => motion.matches || document.hidden;
    const now = performance.now();
    const retained = new Set(targets.map(item => item.id));
    for (const id of entries.keys()) if (!retained.has(id)) entries.delete(id);
    for (const target of targets) {
      let entry = entries.get(target.id);
      if (!entry) { entry = new AnswerPlayback(fresh || target.finished || target.stopped ? target.text : ''); entries.set(target.id, entry); }
      entry.update(target.text, { finished: target.finished, immediate: immediate() || target.stopped }, now);
    }
    // Apply replacements, stop and initial history before the browser paints.
    setRevision(value => value + 1);
    let frame = 0;
    const tick = (time: number): void => {
      let changed = false, pending = false;
      for (const entry of entries.values()) { changed = entry.advance(time) || changed; pending ||= entry.pending; }
      if (changed) setRevision(value => value + 1);
      if (pending) frame = requestAnimationFrame(tick);
    };
    const settle = (): void => {
      if (!immediate()) return;
      for (const entry of entries.values()) entry.finish();
      cancelAnimationFrame(frame); setRevision(value => value + 1);
    };
    if ([...entries.values()].some(entry => entry.pending)) frame = requestAnimationFrame(tick);
    motion.addEventListener('change', settle); document.addEventListener('visibilitychange', settle);
    return () => { cancelAnimationFrame(frame); motion.removeEventListener('change', settle); document.removeEventListener('visibilitychange', settle); };
  }, [scope, targets]);
  return (id: string, fallback: string): string => store.current.scope === scope
    ? store.current.entries.get(id)?.text ?? '' : fallback;
}
