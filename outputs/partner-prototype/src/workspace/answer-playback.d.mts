export class AnswerPlayback {
  constructor(text?: string);
  readonly text: string;
  readonly pending: boolean;
  update(text: string, options?: { immediate?: boolean; finished?: boolean }, now?: number): void;
  finish(): void;
  advance(now: number): boolean;
}
