/* Bounded bridge to local Silero inference; never falls back to an energy gate. */
(function (root) {
  'use strict';
  class CallVoiceDetector {
    constructor(options) {
      this.options = options;
      this.pending = new Map();
      this.ready = false;
      this.closed = false;
      this.epoch = 0;
      this.nextId = 0;
      try {
        this.worker = new Worker(options.url);
        this.worker.onerror = () => this.fail();
        this.worker.onmessage = ({data}) => {
          if (this.closed) return;
          if (data.type === 'ready') {
            clearTimeout(this.timer); this.ready = true; options.onReady?.();
          } else if (data.type === 'error') this.fail();
          else if (data.type === 'result' && data.epoch === this.epoch) {
            this.pending.delete(data.id);
            options.onFrames?.(data.frames, data.through);
          }
        };
        this.timer = setTimeout(() => this.fail(), 20000);
      } catch (_) { queueMicrotask(() => this.fail()); }
    }
    reset(epoch) {
      this.epoch = epoch; this.pending.clear();
      this.worker?.postMessage({type:'reset', epoch});
    }
    push(pcm, at) {
      if (!this.ready || this.closed) return;
      const oldest = this.pending.values().next().value;
      if (this.pending.size >= 10 || (oldest !== undefined && at-oldest > 1500)) { this.fail(); return; }
      const copy = pcm.slice(), id = ++this.nextId;
      this.pending.set(id, at);
      this.worker.postMessage({type:'audio', pcm:copy, at, epoch:this.epoch, id}, [copy.buffer]);
    }
    fail() {
      if (this.closed) return;
      this.close(); this.options.onError?.();
    }
    close() {
      this.closed = true; this.ready = false; clearTimeout(this.timer);
      this.pending.clear(); this.worker?.terminate(); this.worker = null;
    }
  }
  root.SakiCallVAD = {CallVoiceDetector};
  if (typeof module !== 'undefined') module.exports = root.SakiCallVAD;
})(typeof window !== 'undefined' ? window : globalThis);
