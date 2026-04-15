"""Voice transcription providers (Groq, OpenAI Whisper, and local faster-whisper)."""

import asyncio
import os
from pathlib import Path

import httpx
from loguru import logger


class OpenAITranscriptionProvider:
    """Voice transcription provider using OpenAI's Whisper API."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.api_url = "https://api.openai.com/v1/audio/transcriptions"

    async def transcribe(self, file_path: str | Path) -> str:
        if not self.api_key:
            logger.warning("OpenAI API key not configured for transcription")
            return ""
        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    files = {"file": (path.name, f), "model": (None, "whisper-1")}
                    headers = {"Authorization": f"Bearer {self.api_key}"}
                    response = await client.post(
                        self.api_url,
                        headers=headers,
                        files=files,
                        timeout=60.0,
                    )
                    response.raise_for_status()
                    return response.json().get("text", "")
        except Exception as e:
            logger.error("OpenAI transcription error: {}", e)
            return ""


class GroqTranscriptionProvider:
    """
    Voice transcription provider using Groq's Whisper API.

    Groq offers extremely fast transcription with a generous free tier.
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.api_url = "https://api.groq.com/openai/v1/audio/transcriptions"

    async def transcribe(self, file_path: str | Path) -> str:
        """
        Transcribe an audio file using Groq.

        Args:
            file_path: Path to the audio file.

        Returns:
            Transcribed text.
        """
        if not self.api_key:
            logger.warning("Groq API key not configured for transcription")
            return ""

        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""

        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    files = {
                        "file": (path.name, f),
                        "model": (None, "whisper-large-v3"),
                    }
                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                    }

                    response = await client.post(
                        self.api_url, headers=headers, files=files, timeout=60.0
                    )

                    response.raise_for_status()
                    data = response.json()
                    return data.get("text", "")

        except Exception as e:
            logger.error("Groq transcription error: {}", e)
            return ""


class FasterWhisperTranscriptionProvider:
    """Local voice transcription using faster-whisper via an isolated venv subprocess."""

    _TIMEOUT_S = 120.0

    def __init__(
        self,
        venv_python: str = "~/.nanobot/whisper-env/bin/python",
        script_path: str = "",
        model: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
    ):
        self.venv_python = str(Path(venv_python).expanduser())
        if script_path:
            self.script_path = str(Path(script_path).expanduser())
        else:
            self.script_path = str(Path(__file__).parent / "whisper" / "transcribe.py")
        self.model = model
        self.device = device
        self.compute_type = compute_type

    async def transcribe(self, file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""

        env = {
            **os.environ,
            "NANOBOT_WHISPER_MODEL": self.model,
            "NANOBOT_WHISPER_DEVICE": self.device,
            "NANOBOT_WHISPER_COMPUTE_TYPE": self.compute_type,
        }

        try:
            proc = await asyncio.create_subprocess_exec(
                self.venv_python,
                self.script_path,
                str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._TIMEOUT_S)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.error("faster-whisper transcription timed out for: {}", file_path)
                return ""

            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip()
                logger.error(
                    "faster-whisper transcription failed (exit {}): {}", proc.returncode, err_msg
                )
                return ""

            return stdout.decode().strip()

        except Exception as e:
            logger.error("faster-whisper transcription error: {}", e)
            return ""
