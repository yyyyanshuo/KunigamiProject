/* Separate 100 ms preview PCM and higher-rate recording; preserve render boundaries. */
class CallPCMResampler {
  constructor(inputRate, emit, outputRate = 16000) {
    this.ratio = inputRate / outputRate;
    this.emit = emit;
    this.weight = 0;
    this.sum = 0;
    this.frameSize = Math.round(outputRate / 10);
    this.frame = new Int16Array(this.frameSize);
    this.offset = 0;
  }
  push(samples) {
    for (const sample of samples) {
      let remaining = 1;
      while (remaining > 1e-8) {
        const take = Math.min(remaining, this.ratio - this.weight);
        this.sum += sample * take;
        this.weight += take;
        remaining -= take;
        if (this.weight >= this.ratio - 1e-8) {
          const value = Math.max(-1, Math.min(1, this.sum / this.ratio));
          this.frame[this.offset++] = Math.round(value * (value < 0 ? 32768 : 32767));
          this.sum = this.weight = 0;
          if (this.offset === this.frame.length) {
            this.emit(this.frame);
            this.frame = new Int16Array(this.frameSize);
            this.offset = 0;
          }
        }
      }
    }
  }
}

if (typeof registerProcessor === 'function') {
  class CallAudioProcessor extends AudioWorkletProcessor {
    constructor() {
      super();
      this.recordingSampleRate = Math.min(sampleRate, 48000);
      this.recordings = [];
      this.recorder = new CallPCMResampler(sampleRate, pcm => {
        this.recordings.push(pcm);
      }, this.recordingSampleRate);
      this.resampler = new CallPCMResampler(sampleRate, pcm => {
        const recording = this.recordings.shift();
        this.port.postMessage({pcm, recording, recordingSampleRate: this.recordingSampleRate}, [pcm.buffer, recording.buffer]);
      });
    }
    process(inputs) {
      if (inputs[0]?.[0]) {
        this.recorder.push(inputs[0][0]);
        this.resampler.push(inputs[0][0]);
      }
      return true;
    }
  }
  registerProcessor('call-audio', CallAudioProcessor);
}
if (typeof module !== 'undefined') module.exports = {CallPCMResampler};
