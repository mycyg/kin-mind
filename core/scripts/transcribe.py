"""Transcribe voice messages locally with Qwen3-ASR (mlx).

Usage: transcribe.py FILE [FILE ...]
Accepts Ogg/Opus (Feishu voice, often saved as .bin), WAV, FLAC.
Prints one line per file: <path>\t<seconds>\t<text>
"""
import os, re, sys, tempfile, warnings
warnings.filterwarnings("ignore")
import soundfile as sf
from mlx_audio.stt.utils import load_model

MODEL = os.environ.get("KIN_ASR_MODEL", "mlx-community/Qwen3-ASR-1.7B-8bit")


def main(paths):
    if not paths:
        print(__doc__)
        return 2
    model = load_model(MODEL)
    rc = 0
    for p in paths:
        wav = None
        try:
            audio, sr = sf.read(p, dtype="float32")
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as t:
                wav = t.name
            sf.write(wav, audio, sr)
            r = model.generate(wav)
            text = re.sub(r"[\r\n\t]+", " ", getattr(r, "text", str(r))).strip()
            if not text:
                raise ValueError("empty transcription")
            print(f"{p}\t{len(audio)/sr:.1f}\t{text}", flush=True)
        except Exception as e:
            rc = 1
            print(f"{p}\tERR\t{e!r}", flush=True)
        finally:
            if wav:
                try:
                    os.unlink(wav)
                except FileNotFoundError:
                    pass
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
