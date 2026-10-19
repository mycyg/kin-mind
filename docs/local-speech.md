# Local speech replies

A public reply can carry `presentation: {mode: "text" | "voice" | "both", style: ""}` alongside its existing `private_note`, `action`, and `bubbles`. Omitting it keeps text behavior. Kin chooses the form in the same response; the current owner's explicit choice takes precedence over older preferences. Everyday chat favors short, natural exchanges; work and requested detail remain complete. No fixed character or bubble count is imposed.

Private notes are saved and removed at the existing boundary. Only public bubble text and the separate short style instruction reach the local synthesizer. The same presentation field is carried by proactive drafts.

The host's private `mind-config.json` configures `tts.python`, `tts.model` (an existing local Qwen3-TTS CustomVoice directory), `tts.ffmpeg`, and `tts.voice` (default `serena`). No credentials or model downloads are required. A lazy child worker loads once, processes requests sequentially, and exits when its host closes stdin. Completed Opus files use existing transport IDs for resume. `speed` is not advertised: the installed model implementation does not implement a numeric speed factor.

Each ordinary reply remains one logical bubble. Voice adds one speech fragment; both retains text fragments followed by speech. Existing receipts decide delivery. A synthesis failure is `tts-failed`, with accepted text retained as partial delivery; it becomes an existing reply outcome/tail fact rather than a successful reply. Proactive contacts retain accepted items and expose the failure on their settlement. No automatic text replacement or new retry loop is added.

Feishu uploads with `file_type=opus` and sends `msg_type=audio`. WeChat ordinary reply media uses its existing file uploader and `FILE` message, never a claimed native voice bar. The current deployment routes replies and proactive contacts to Feishu; its receive-only WeChat policy and retired proactive WeChat entry remain in force. Explicit WeChat artifact requests retain their existing path.

Tests use synthetic text, a local worker or channel doubles. Live phone messages require an explicit recipient/content test scope. Local performance samples prove synthesis only, not channel acceptance or perceived voice quality.
