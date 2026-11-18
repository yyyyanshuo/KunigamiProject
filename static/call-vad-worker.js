/* Silero V5 streaming inference. See vendor/call-vad-20261030/THIRD_PARTY.md.
 * Inference is serialized in this worker; recording never waits for the model.
 */
'use strict';
const ASSETS = new URL('./vendor/call-vad-20261030/', self.location.href).href;
let session, state, sr, epoch = 0, carry = new Float32Array(0);
const initialized = (async () => {
  importScripts(ASSETS + 'ort.wasm.min.js');
  ort.env.wasm.wasmPaths = ASSETS;
  ort.env.wasm.numThreads = 1;
  ort.env.wasm.proxy = false;
  session = await ort.InferenceSession.create(ASSETS + 'silero_vad_v5.onnx', {executionProviders:['wasm']});
  sr = new ort.Tensor('int64', [16000n]);
  reset(0);
  self.postMessage({type:'ready'});
})();
function reset(nextEpoch) {
  epoch = nextEpoch;
  state?.dispose();
  state = new ort.Tensor('float32', new Float32Array(256), [2, 1, 128]);
  carry = new Float32Array(0);
}
let sequence = initialized;
self.onmessage = ({data}) => {
  sequence = sequence.then(async () => {
    if (data.type === 'reset') { reset(data.epoch); return; }
    if (data.type !== 'audio' || data.epoch !== epoch) return;
    const pcm = data.pcm, samples = new Float32Array(carry.length + pcm.length);
    samples.set(carry);
    for (let i=0; i<pcm.length; i++) samples[carry.length+i] = pcm[i] / 32768;
    const frames = [];
    let offset = 0;
    while (offset + 512 <= samples.length) {
      const input = new ort.Tensor('float32', samples.slice(offset, offset+512), [1, 512]);
      const previousState = state;
      const out = await session.run({input, state, sr});
      state = out.stateN;
      const probability = Number(out.output.data[0]);
      if (!Number.isFinite(probability) || !state) throw new Error('Invalid VAD output');
      // Timestamp of the captured audio, not completion of delayed inference.
      frames.push({probability, at:data.at - (samples.length-offset-512)/16, duration:32});
      input.dispose(); previousState.dispose(); out.output.dispose();
      offset += 512;
    }
    carry = samples.slice(offset);
    self.postMessage({type:'result', epoch, id:data.id, frames, through:data.at});
  }).catch(() => {
    self.postMessage({type:'error'});
    self.close();
  });
};
initialized.catch(() => { self.postMessage({type:'error'}); self.close(); });
