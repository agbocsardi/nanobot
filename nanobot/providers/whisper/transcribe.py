#!/usr/bin/env python3
"""Transcribe audio files using faster-whisper (small, CPU, int8).

Standalone script run by the isolated whisper-env Python.
Prints clean transcript to stdout; stderr for diagnostics only.

Environment variables:
    NANOBOT_WHISPER_MODEL        Model size (default: small)
    NANOBOT_WHISPER_DEVICE       Device (default: cpu)
    NANOBOT_WHISPER_COMPUTE_TYPE Compute type (default: int8)

Usage: transcribe.py <audio_file> [language]
Exit codes: 0 success, 1 bad args / file not found, 2 transcription error
"""

import os
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: transcribe.py <audio_file> [language]", file=sys.stderr)
        sys.exit(1)

    audio_path = Path(sys.argv[1])
    if not audio_path.exists():
        print(f"Error: file not found: {audio_path}", file=sys.stderr)
        sys.exit(1)

    language = sys.argv[2] if len(sys.argv) > 2 else None

    model_name = os.environ.get("NANOBOT_WHISPER_MODEL", "small")
    device = os.environ.get("NANOBOT_WHISPER_DEVICE", "cpu")
    compute_type = os.environ.get("NANOBOT_WHISPER_COMPUTE_TYPE", "int8")

    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        segments, _info = model.transcribe(str(audio_path), language=language)

        parts = [seg.text.strip() for seg in segments]
        clean_text = " ".join(parts).strip()

        print(clean_text, flush=True)
    except Exception as e:
        print(f"Transcription error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
