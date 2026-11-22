// Presentation only: the server snapshot remains the source of truth.
const segmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' });
export class AnswerPlayback {
  constructor(text = '') {
    this.target = text;
    this.parts = Array.from(segmenter.segment(text), part => part.segment);
    this.cursor = this.parts.length;
    this.time = null;
    this.carry = 0;
    this.deadline = null;
  }
  get text() { return this.parts.slice(0, this.cursor).join(''); }
  get pending() { return this.cursor < this.parts.length; }
  update(text, { immediate = false, finished = false } = {}, now = 0) {
    const previous = this.text;
    const wasPending = this.pending;
    if (text !== this.target) {
      this.target = text;
      this.parts = Array.from(segmenter.segment(text), part => part.segment);
      // A corrected/replaced snapshot must never reveal stale characters.
      if (!text.startsWith(previous)) this.cursor = this.parts.length;
      else {
        let length = 0; this.cursor = 0;
        while (this.cursor < this.parts.length && length + this.parts[this.cursor].length <= previous.length) length += this.parts[this.cursor++].length;
      }
      if (!wasPending) { this.time = now; this.carry = 0; }
    }
    if (finished && this.deadline === null) this.deadline = now + 1400;
    if (immediate) this.finish();
  }
  finish() { this.cursor = this.parts.length; this.carry = 0; this.time = null; }
  advance(now) {
    if (!this.pending) { this.time = now; return false; }
    if (this.deadline !== null && now >= this.deadline) { this.finish(); return true; }
    const elapsed = this.time === null ? 0 : Math.max(0, Math.min(50, now - this.time));
    this.time = now;
    const remaining = this.parts.length - this.cursor;
    const seconds = this.deadline === null ? 2 : Math.max(.016, (this.deadline - now) / 1000);
    this.carry += Math.max(85, remaining / seconds) * elapsed / 1000;
    const count = Math.min(remaining, Math.floor(this.carry));
    this.carry -= count; this.cursor += count;
    return count > 0;
  }
}
