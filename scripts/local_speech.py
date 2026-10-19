"""Offline Qwen3 speech worker. stdin contains public text only; EOF exits."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    model_path, ffmpeg = sys.argv[1:3]
    model = None
    reference = None
    for line in sys.stdin:
        started = time.monotonic()
        stage = "load"
        temporary = []
        try:
            request = json.loads(line)
            # MLX/transformers diagnostics must not become protocol responses.
            with contextlib.redirect_stdout(sys.stderr):
                import numpy as np
                import mlx.core as mx
                import soundfile as sf
                from mlx_audio.tts.utils import load_model

                load_started = time.monotonic()
                cold = model is None
                if cold:
                    model = load_model(model_path)
                load_ms = round((time.monotonic() - load_started) * 1000)
                stage = "synthesis"
                generation_started = time.monotonic()
                if request.get("referenceAudio") and request.get("referenceText"):
                    # The selected synthetic recording anchors identity. Base ICL
                    # has no instruct control: expression follows text/punctuation.
                    if reference is None:
                        data = subprocess.check_output([
                            ffmpeg, "-nostdin", "-v", "error", "-i", request["referenceAudio"],
                            "-f", "f32le", "-ac", "1", "-ar", str(model.sample_rate), "pipe:1"],
                            stderr=subprocess.DEVNULL)
                        reference = mx.array(np.frombuffer(data, dtype=np.float32).copy())
                    results = list(model.generate(
                        text=request["text"], ref_audio=reference, ref_text=request["referenceText"],
                        lang_code="Chinese", temperature=0.3, top_p=0.9, verbose=False,
                    ))
                else:
                    results = list(model.generate_custom_voice(
                        text=request["text"], speaker=request["voice"], language="Chinese",
                        instruct=request.get("style") or "自然口语，像和熟悉的人聊天。",
                        verbose=False,
                    ))
                audio = np.concatenate([np.asarray(result.audio).reshape(-1) for result in results])
                rate = results[0].sample_rate
                if not audio.size:
                    raise ValueError("empty audio")
                generation_ms = round((time.monotonic() - generation_started) * 1000)
                file = Path(request["file"])
                wav, encoded = Path(str(file) + ".wav"), Path(str(file) + ".part.ogg")
                temporary = [wav, encoded]
                stage = "encoding"
                sf.write(wav, audio, rate, subtype="PCM_16")
                subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                                "-i", str(wav), "-c:a", "libopus", "-b:a", "32k", str(encoded)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                os.replace(encoded, file)
            response = {"ok": True, "cold": cold, "loadMs": load_ms, "generationMs": generation_ms,
                        "durationMs": round(audio.size / rate * 1000),
                        "elapsedMs": round((time.monotonic() - started) * 1000)}
        except Exception:
            response = {"ok": False, "stage": stage}
        finally:
            for file in temporary:
                file.unlink(missing_ok=True)
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
