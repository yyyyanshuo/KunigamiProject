# Local call voice activity detection dependencies

- Silero V5 ONNX model: distributed unchanged by @ricky0123/vad-web 0.0.30; https://github.com/snakers4/silero-vad ; MIT, see SILERO-LICENSE.
- ONNX Runtime Web 1.22.0 WASM-only runtime: https://github.com/microsoft/onnxruntime/tree/v1.22.0 ; MIT, see ONNXRUNTIME-LICENSE. The JS, MJS and WASM are unmodified distribution files.
- The streaming model state/input protocol follows @ricky0123/vad-web 0.0.30 models/v5.js: https://github.com/ricky0123/vad ; ISC, see VAD-WEB-LICENSE. Our worker uses the existing call PCM stream and does not bundle MicVAD or its capture pipeline.

Pinned package URLs, registry integrity values verified before extraction, and file SHA256 values are recorded in SOURCES.json. All runtime/model assets are served from the same origin. No CDN, telemetry or external inference requests are used by this VAD. Speech-to-text still uses the existing recording transcription endpoint.
