import { useEffect, useRef, useState } from 'react';
import type { AuthClient } from '../api/client';
import type { Run } from '../api/chats';
import type { AnswerTarget } from './useAnswerPlayback';

export type TurnStarts = Map<string, { chatId: string; started: number; elapsed?: number }>;

// This observation covers today's answer/card UI. The diagram renderer must add
// its own readiness signal before this can be used as the final contest timer.
export function useTurnTiming(auth: AuthClient, chatId: string | undefined, runs: Run[],
  targets: AnswerTarget[], shown: (id: string, fallback: string) => string, starts: TurnStarts) {
  const pending = useRef(new Set<string>());
  const attempts = useRef(new Map<string, number>());
  const retries = useRef(new Set<number>());
  const [revision, setRevision] = useState(0);
  useEffect(() => () => { for (const timer of retries.current) clearTimeout(timer); }, []);
  useEffect(() => {
    if (!chatId) return;
    let second = 0;
    const first = requestAnimationFrame(() => {
      second = requestAnimationFrame(() => {
        for (const run of runs) {
          const timing = starts.get(run.id);
          if (!timing || timing.chatId !== chatId || pending.current.has(run.id)) continue;
          if ((attempts.current.get(run.id) ?? 0) >= 3) continue;
          if (!['succeeded', 'failed', 'cancelled', 'waiting_input'].includes(run.status)) continue;
          const target = targets.find(item => item.id === run.id);
          if (target && shown(target.id, target.text) !== target.text) continue;
          timing.elapsed ??= Math.max(0, Math.round(performance.now() - timing.started));
          pending.current.add(run.id);
          attempts.current.set(run.id, (attempts.current.get(run.id) ?? 0) + 1);
          void auth.mutate(`/chats/${chatId}/accounting/turns/${run.id}/rendered`, 'POST', {
            elapsed_ms: timing.elapsed,
          }).then(() => {
            starts.delete(run.id); setRevision(value => value + 1);
          }).catch(() => {
            // Keep the original observation for a later retry, never replace it with
            // a shorter measurement. Missing observations stay unknown in the API.
            pending.current.delete(run.id);
            const timer = window.setTimeout(() => {
              retries.current.delete(timer); setRevision(value => value + 1);
            }, 1000);
            retries.current.add(timer);
          });
        }
      });
    });
    return () => { cancelAnimationFrame(first); cancelAnimationFrame(second); };
  }, [auth, chatId, runs, targets, shown, starts, revision]);
  return revision;
}
