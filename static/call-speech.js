/*
 * Call input pipeline: record -> pause detection -> whole-recording STT -> delivery.
 * Architecture references (no framework source copied):
 * https://docs.pipecat.ai/server/utilities/turn-management/user-turn-strategies
 * https://docs.livekit.io/agents/logic/turns/
 * Realtime transcripts are previews, never the text sent to the character.
 * Provider segment finals do not end a turn. Keep recording during batch STT;
 * confirmed speech cancels the snapshot and extends the same utterance.
 */
(function (root) {
  'use strict';
  const DEFAULTS = Object.freeze({
    silenceMs: 3000, maxTurnMs: 90000, preRollFrames: 100,
    speechThreshold: 0.65, speechReleaseThreshold: 0.35, minSpeechMs: 160, speechGapMs: 160,
    connectTimeoutMs: 8000, batchTimeoutMs: 120000, maxQueuedFrames: 80,
  });
  const spokenText = text => String(text || '')
    .replace(/（[^（）]{1,40}）|\([^()]{1,40}\)|\[[^\[\]]{1,40}\]/g, '')
    .replace(/\s+/g, ' ').trim();
  const hasSpeech = text => /[\p{L}\p{N}]/u.test(spokenText(text));
  const textKey = text => spokenText(text).replace(/[\p{P}\p{Z}\s]/gu, '');

  class TurnDetector {
    constructor(options = {}) {
      this.options = {...DEFAULTS, ...options};
      this.preRoll = [];
      this.frames = [];
      this.committed = [];
      this.partial = '';
      this.startedAt = null;
      this.lastVoiceAt = null;
      this.lastTextAt = null;
      this.voiceMs = 0;
      this.voiceGapMs = 0;
      this.confirmedSpeech = false;
      this.vadAt = null;
      this.revision = 0;
      this.sampleRate = 16000;
      this.audioMs = 0;
    }
    get active() { return this.startedAt !== null; }
    get text() { return [...this.committed, this.partial].filter(Boolean).join(' ').trim(); }
    start(now) {
      if (this.active) return;
      this.startedAt = now;
      this.frames = this.preRoll.splice(0);
      this.audioMs = this.frames.reduce((sum, frame) => sum + frame.length * 1000 / this.sampleRate, 0);
    }
    audio(pcm, now, recording = pcm, sampleRate = 16000) {
      this.sampleRate = sampleRate;
      const duration = pcm.length / 16;
      if (this.active) {
        // Hard memory bound even when a background tab delays the watchdog timer.
        if (this.audioMs < this.options.maxTurnMs) {
          this.frames.push(recording);
          this.audioMs += duration;
        }
      } else {
        this.preRoll.push(recording);
        if (this.preRoll.length > this.options.preRollFrames) this.preRoll.shift();
      }
    }
    voice(probability, now, duration = 32) {
      this.vadAt = now;
      if (probability >= this.options.speechThreshold) {
        this.voiceMs += duration;
        this.voiceGapMs = 0;
      } else {
        // Require sustained positive evidence: isolated clicks cannot accumulate.
        this.voiceMs = 0;
        this.voiceGapMs += duration;
        if (this.voiceGapMs >= this.options.speechGapMs) this.confirmedSpeech = false;
      }
      if (this.voiceMs >= this.options.minSpeechMs) this.confirmedSpeech = true;
      if (this.confirmedSpeech && probability >= this.options.speechReleaseThreshold) {
        this.start(now);
        this.lastVoiceAt = now;
        this.revision++;
      }
    }
    get speechCandidate() { return this.voiceMs > 0 && !this.confirmedSpeech; }
    transcript(type, raw, now) {
      const text = spokenText(raw);
      if (!hasSpeech(text)) return false;
      if (!['partial_transcript', 'committed_transcript', 'committed_transcript_with_timestamps'].includes(type)) return false;
      const before = textKey(this.text);
      this.start(now); // Captions can still recover a short/soft utterance missed by VAD.
      if (type === 'partial_transcript') {
        this.partial = text;
      } else {
        this.committed.push(text);
        this.partial = '';
      }
      if (textKey(this.text) !== before) this.lastTextAt = now;
      return true;
    }
    endpoint(now) {
      if (!this.active) return null;
      if (this.audioMs >= this.options.maxTurnMs) return 'recording_limit';
      // A segment final is just a caption update. Both acoustic activity and
      // genuinely new words protect a hesitation or a soft continuation.
      const lastActivity = Math.max(this.lastVoiceAt ?? this.startedAt, this.lastTextAt ?? this.startedAt);
      return now - lastActivity >= this.options.silenceMs ? 'silence' : null;
    }
  }

  function wavBlob(frames, sampleRate = 16000) {
    const samples = frames.reduce((sum, pcm) => sum + pcm.length, 0);
    const buffer = new ArrayBuffer(44 + samples * 2), view = new DataView(buffer);
    const ascii = (offset, text) => [...text].forEach((letter, i) => view.setUint8(offset + i, letter.charCodeAt(0)));
    ascii(0, 'RIFF'); view.setUint32(4, 36 + samples * 2, true); ascii(8, 'WAVE');
    ascii(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    ascii(36, 'data'); view.setUint32(40, samples * 2, true);
    let offset = 44;
    for (const frame of frames) for (const sample of frame) { view.setInt16(offset, sample, true); offset += 2; }
    return new Blob([buffer], {type: 'audio/wav'});
  }
  function encodePCM(pcm) {
    // Explicit little endian, independent of the host's typed-array byte order.
    const buffer = new ArrayBuffer(pcm.length * 2), view = new DataView(buffer);
    pcm.forEach((sample, i) => view.setInt16(i * 2, sample, true));
    let binary = '';
    for (const byte of new Uint8Array(buffer)) binary += String.fromCharCode(byte);
    return btoa(binary);
  }
  async function withDeadline(run, ms, controller) {
    let timer, abort;
    const canceled = new Promise((_, reject) => {
      abort = () => reject(new DOMException('Aborted', 'AbortError'));
      controller.signal.addEventListener('abort', abort, {once: true});
      if (controller.signal.aborted) abort();
    });
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        reject(new Error('语音识别超时，请重试'));
        controller.abort();
      }, ms);
    });
    try { return await Promise.race([run(controller.signal), timeout, canceled]); }
    finally { clearTimeout(timer); controller.signal.removeEventListener('abort', abort); }
  }

  // Upload time is separate from recognition time. A mobile uplink must not use
  // up the provider's entire response budget before it receives the recording.
  function uploadRecording(url, blob, signal, onProgress, options = {}) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      let timer, settled = false;
      const finish = (error, value) => {
        if (settled) return;
        settled = true; clearTimeout(timer);
        signal?.removeEventListener('abort', abort);
        if (error) reject(error); else resolve(value);
      };
      const fail = (message, code) => {
        const error = new Error(message); error.code = code;
        finish(error); xhr.abort();
      };
      const abort = () => { finish(new DOMException('Aborted', 'AbortError')); xhr.abort(); };
      const deadline = (ms, message, code) => {
        clearTimeout(timer); timer = setTimeout(() => fail(message, code), ms);
      };
      xhr.open('POST', url);
      xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
      xhr.responseType = 'json';
      xhr.upload.onprogress = event => onProgress?.('uploading', event.lengthComputable ? Math.round(event.loaded * 100 / event.total) : null);
      xhr.upload.onload = () => {
        if (settled) return;
        deadline(options.responseTimeoutMs ?? 55000, '录音识别超时，录音已保留，请重试', 'call_recognition_timeout');
        onProgress?.('transcribing');
      };
      xhr.onload = () => {
        if (xhr.status < 200 || xhr.status >= 300) {
          const error = new Error(xhr.response?.error || `请求失败（${xhr.status}）`);
          error.code = xhr.response?.code;
          finish(error); return;
        }
        const data = xhr.response;
        if (!data || typeof data.transcript !== 'string' || typeof data.has_speech !== 'boolean') {
          fail('没有收到有效的识别结果，录音已保留，请重试', 'call_transcript_invalid'); return;
        }
        finish(null, {text: data.transcript, hasSpeech: data.has_speech});
      };
      xhr.onerror = () => fail('录音传输失败，录音已保留，请检查网络后重试', 'call_upload_failed');
      xhr.onabort = () => finish(new DOMException('Aborted', 'AbortError'));
      signal?.addEventListener('abort', abort, {once: true});
      if (signal?.aborted) { abort(); return; }
      deadline(options.uploadTimeoutMs ?? 60000, '录音上传超时，录音已保留，请检查网络后重试', 'call_upload_timeout');
      onProgress?.('uploading', 0);
      const form = new FormData(); form.append('file', blob, 'call.wav');
      xhr.send(form);
    });
  }

  class CallSpeechInput {
    constructor(options) {
      this.options = {...DEFAULTS, ...options};
      this.epoch = 0;
      this.closed = false;
      this.enabled = false;
      this.stream = null;
      this.context = null;
      this.socket = null;
      this.pending = null;
      this.retries = 0;
      this.connectionAttempt = 0;
      this.vad = null;
      this.vadState = 'loading';
    }
    status(value) {
      if (['connecting', 'listening', 'degraded'].includes(value) && this.vadState !== 'ready') {
        value = this.vadState === 'failed' ? 'manual' : 'vad_loading';
      }
      this.options.onStatus?.(value);
    }
    startVAD() {
      if (this.vad && !this.vad.closed) return;
      this.vadState = 'loading';
      try {
        this.vad = new root.SakiCallVAD.CallVoiceDetector({
          url: this.options.vadUrl,
          onReady: () => {
            this.vadState = 'ready';
            if (this.enabled && !this.pending) this.status('listening');
          },
          onError: () => {
            this.vadState = 'failed';
            if (this.enabled && !this.pending) this.status('manual');
          },
          onFrames: (frames, through) => {
            if (!this.enabled || !this.turn || this.recordingFull || this.pending?.explicit) return;
            for (const frame of frames) this.turn.voice(frame.probability, frame.at, frame.duration);
            this.vadThrough = through;
            if (this.pending && !this.pending.explicit && this.pending.revision !== this.turn.revision) {
              this.cancelTranscription();
              this.status('listening');
            }
          },
        });
      } catch (_) { this.vadState = 'failed'; }
    }
    async start() {
      if (this.closed || this.enabled) return;
      if (this.starting) return this.starting;
      const epoch = this.epoch;
      const starting = (async () => {
        try {
          if (!this.stream) {
            this.status('connecting');
            const stream = await navigator.mediaDevices.getUserMedia({
              audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}, video: false,
            });
            if (this.closed || epoch !== this.epoch) { stream.getTracks().forEach(track => track.stop()); return; }
            this.stream = stream;
            this.startVAD();
            const Context = root.AudioContext || root.webkitAudioContext;
            const context = new Context(); this.context = context;
            await context.resume();
            if (!context.audioWorklet) throw new Error('当前浏览器不支持通话录音，请更新浏览器');
            // Worklet runs off the UI thread: scrolling/rendering cannot stall capture.
            await context.audioWorklet.addModule(this.options.workletUrl);
            if (this.closed || epoch !== this.epoch) return;
            this.source = context.createMediaStreamSource(stream);
            this.processor = new AudioWorkletNode(context, 'call-audio');
            this.processor.port.onmessage = event => this.audio(event.data);
            this.silent = context.createGain(); this.silent.gain.value = 0;
            this.source.connect(this.processor); this.processor.connect(this.silent); this.silent.connect(context.destination);
            stream.getAudioTracks().forEach(track => { track.onended = () => this.captureFailed(); });
            this.processor.onprocessorerror = () => this.captureFailed();
          }
          if (!this.closed && epoch === this.epoch) {
            if (this.context?.state === 'suspended') await this.context.resume();
            if (!this.closed && epoch === this.epoch) {
              this.startVAD();
              this.resume();
            }
          }
        } catch (error) {
          if (!this.closed && epoch === this.epoch) {
            this.releaseCapture();
            this.options.onError?.(error, false);
          }
        }
      })();
      this.starting = starting;
      try { await starting; }
      finally { if (this.starting === starting) this.starting = null; }
    }
    captureFailed() {
      if (this.closed) return;
      this.pause(); this.releaseCapture();
      this.options.onError?.(new Error('无法使用麦克风'), false);
    }
    resume() {
      if (this.closed || !this.stream || this.enabled) return;
      this.epoch++;
      this.enabled = true;
      this.pending = null;
      this.turn = new TurnDetector(this.options);
      this.vadThrough = null;
      this.vad?.reset(this.epoch);
      this.queue = [];
      this.previewUnavailable = false;
      this.recordingFull = false;
      this.attemptedRevision = null;
      this.turnId = `turn_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 11)}`;
      this.retries = 0;
      this.lastAudioAt = performance.now();
      this.options.onText?.('');
      this.status('connecting');
      this.interval = setInterval(() => {
        if (!this.enabled) return;
        const now = performance.now(), reason = this.turn.endpoint(now);
        if (reason === 'recording_limit' && !this.recordingFull) {
          this.recordingFull = true;
          this.disconnect();
          this.status('limit');
        } else if (reason && this.vadState === 'ready' && this.vadThrough !== null && now-this.vadThrough < 1000 &&
            !this.recordingFull && this.attemptedRevision !== this.turn.revision) {
          void this.finalize(reason);
        }
        else if (now - this.lastAudioAt > 5000) this.captureFailed();
      }, 100);
      void this.connect(this.epoch);
    }
    audio(frame) {
      if (!this.enabled || this.closed) return;
      const now = performance.now(); this.lastAudioAt = now;
      // Explicit send fixes the utterance. Preserve that exact snapshot on a
      // failed request; waiting, clicks and retries cannot grow its upload.
      if (this.recordingFull || this.pending?.explicit) return;
      const pcm = frame.pcm || frame;
      this.turn.audio(pcm, now, frame.recording || pcm, frame.recordingSampleRate || 16000);
      this.vad?.push(pcm, now);
      if (this.ready) this.send(pcm);
      else if (!this.previewUnavailable) {
        this.queue.push(pcm);
        if (this.queue.length > this.options.maxQueuedFrames) this.connectionFailed(this.epoch);
      }
    }
    send(pcm) {
      if (!this.socket || this.socket.readyState !== 1) return;
      // A slow uplink must not accumulate unlimited stale audio in the browser.
      if (this.socket.bufferedAmount > 256000) { this.connectionFailed(this.epoch); return; }
      try {
        this.socket.send(JSON.stringify({message_type: 'input_audio_chunk', audio_base_64: encodePCM(pcm), sample_rate: 16000}));
      } catch (_) { this.connectionFailed(this.epoch); }
    }
    async connect(epoch) {
      const attempt = ++this.connectionAttempt;
      const controller = new AbortController(); this.connectAbort = controller;
      try {
        const token = await withDeadline(signal => this.options.fetchToken(signal), this.options.connectTimeoutMs, controller);
        if (!this.enabled || epoch !== this.epoch || attempt !== this.connectionAttempt) return;
        const url = new URL('wss://api.elevenlabs.io/v1/speech-to-text/realtime');
        Object.entries({token: token.token, model_id: token.model_id || 'scribe_v2_realtime',
          audio_format: 'pcm_16000', commit_strategy: 'vad', vad_silence_threshold_secs: '1.5',
          min_speech_duration_ms: '100', min_silence_duration_ms: '100', include_timestamps: 'false',
        }).forEach(([key, value]) => url.searchParams.set(key, value));
        const socket = new WebSocket(url); this.socket = socket;
        const current = () => this.enabled && this.epoch === epoch && this.socket === socket;
        this.connectTimer = setTimeout(() => { if (current()) this.connectionFailed(epoch); }, this.options.connectTimeoutMs);
        socket.onmessage = event => {
          if (!current()) return;
          let message; try { message = JSON.parse(event.data); } catch (_) { return; }
          if (message.message_type === 'session_started') {
            clearTimeout(this.connectTimer); this.ready = true;
            for (const frame of this.queue.splice(0)) { if (!this.ready) break; this.send(frame); }
            if (this.ready && !this.pending) this.status('listening');
          } else if (message.error || /error|quota_exceeded|rate_limited|queue_overflow|resource_exhausted|session_time_limit_exceeded|insufficient_audio_activity|unaccepted_terms/.test(message.message_type || '')) {
            this.connectionFailed(epoch);
          } else if (this.turn.transcript(message.message_type, message.text, performance.now())) {
            this.options.onText?.(this.turn.text);
          }
        };
        socket.onerror = socket.onclose = () => { if (current()) this.connectionFailed(epoch); };
      } catch (_) {
        if (this.enabled && epoch === this.epoch && attempt === this.connectionAttempt) this.connectionFailed(epoch);
      }
    }
    disconnect() {
      this.connectionAttempt++;
      clearTimeout(this.connectTimer); clearTimeout(this.retryTimer);
      this.connectAbort?.abort(); this.connectAbort = null;
      this.ready = false;
      const socket = this.socket; this.socket = null;
      if (socket) {
        socket.onmessage = socket.onerror = socket.onclose = null;
        try { socket.close(); } catch (_) {}
      }
    }
    connectionFailed(epoch) {
      if (!this.enabled || epoch !== this.epoch) return;
      this.disconnect();
      this.queue = [];
      if (!this.pending) this.status('degraded');
      // Preview failure must not stop the independent recording or its ASR.
      if (this.turn.active || this.retries >= 2) { this.previewUnavailable = true; return; }
      this.retryTimer = setTimeout(() => {
        if (this.enabled && epoch === this.epoch) void this.connect(epoch);
      }, 500 * ++this.retries);
    }
    cancelTranscription() {
      const pending = this.pending;
      this.pending = null;
      pending?.controller.abort();
    }
    pause() {
      this.enabled = false; this.epoch++;
      clearInterval(this.interval);
      this.disconnect();
      this.cancelTranscription();
      this.queue = [];
      this.turn = null;
      this.vad?.reset(this.epoch);
    }
    async finalize(reason) {
      if (!this.enabled || this.closed) return;
      if (this.pending?.running) {
        if (reason === 'manual') { this.pending.explicit = true; this.disconnect(); }
        return;
      }
      const turn = this.turn;
      if (!turn.active) {
        if (reason !== 'manual' || !turn.preRoll.length) return;
        turn.start(performance.now()); // Manual send also works when preview missed everything.
      }
      const now = performance.now(), epoch = this.epoch;
      const previous = this.pending;
      const sameRecording = previous && (previous.explicit ||
        (this.vadState === 'ready' && previous.revision === turn.revision && previous.textKey === textKey(turn.text)));
      this.cancelTranscription();
      const pending = {
        blob: sameRecording ? previous.blob : wavBlob(turn.frames, turn.sampleRate), revision: turn.revision,
        textKey: textKey(turn.text),
        reason, explicit: reason === 'manual', clientTurnId: this.turnId, controller: new AbortController(), running: true,
        speechEndedAt: turn.lastVoiceAt ?? now, detectedAt: now,
      };
      this.pending = pending;
      if (pending.explicit) this.disconnect();
      this.attemptedRevision = turn.revision;
      this.status('transcribing');
      const current = () => !this.closed && this.enabled && epoch === this.epoch &&
        this.pending === pending && (pending.explicit || turn.revision === pending.revision);
      try {
        // Every turn uses the entire recording. Realtime text is never a fallback.
        const result = await withDeadline(signal => this.options.transcribe(pending.blob, signal, (status, percent) => {
          if (current()) this.options.onProgress?.(status, percent);
        }),
          this.options.batchTimeoutMs, pending.controller);
        if (!current()) return;
        const text = result.hasSpeech ? spokenText(result.text) : '';
        if (!hasSpeech(text)) {
          pending.running = false;
          pending.explicit = false; // Empty audio can be extended by fresh speech.
          this.status('empty'); // Retain the recording for retry or further speech.
          return;
        }
        // Let captured frames finish classification before accepting an automatic
        // result. A brief high-probability click can expire without canceling ASR.
        if (!pending.explicit) {
          await new Promise((resolve, reject) => {
            let timer;
            const done = () => { clearTimeout(timer); pending.controller.signal.removeEventListener('abort', abort); resolve(); };
            const abort = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); };
            const waitUntil = performance.now() + 400;
            const check = () => {
              if (!current()) return done();
              if (performance.now() >= waitUntil || (!turn.speechCandidate && !this.vad?.pending.size)) return done();
              timer = setTimeout(check, 32);
            };
            pending.controller.signal.addEventListener('abort', abort, {once:true});
            check();
          });
          if (!current()) return;
        }
        const metrics = {source: 'batch_recording', endpointMs: pending.detectedAt - pending.speechEndedAt,
          recognitionWaitMs: performance.now() - pending.detectedAt};
        this.pause();
        this.options.onTurn({text, clientTurnId: pending.clientTurnId, metrics});
      } catch (error) {
        if (current()) this.options.onError?.(error, true);
      } finally { pending.running = false; }
    }
    commitNow() { return this.finalize('manual'); }
    retry() { return this.enabled ? this.finalize('manual') : this.start(); }
    releaseCapture() {
      if (this.processor) { this.processor.port.onmessage = null; this.processor.disconnect(); }
      this.source?.disconnect(); this.silent?.disconnect();
      this.stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
      if (this.context) void this.context.close().catch(() => {});
      this.vad?.close(); this.vad = null; this.vadState = 'loading';
      this.stream = this.context = this.processor = this.source = this.silent = null;
    }
    close() { this.closed = true; this.pause(); this.releaseCapture(); }
  }
  const exports = {CallSpeechInput, TurnDetector, spokenText, hasSpeech, wavBlob, uploadRecording, DEFAULTS};
  if (typeof module !== 'undefined') module.exports = exports;
  root.SakiCallSpeech = exports;
})(typeof window !== 'undefined' ? window : globalThis);
