"""Transcription provider using LiteLLM - supports multiple backends."""

import os
from pathlib import Path

from loguru import logger

from speckbot.config.schema import TranscriptionConfig


class LiteLLMTranscriptionProvider:
    """
    Unified transcription provider using LiteLLM.

    Supports 9 providers via model-string routing:
    - openai (whisper-1)
    - azure
    - deepgram (deepgram/nova-2)
    - groq (groq/whisper-large-v3)
    - fireworks_ai
    - mistral (mistral/voxtral-mini)
    - ovhcloud
    - vertex_ai
    - gemini

    Auto-routes to correct provider based on model string prefix.
    """

    def __init__(self, config: TranscriptionConfig | None = None):
        """
        Initialize transcription provider.

        Args:
            config: TranscriptionConfig. If not provided, uses default values.
        """
        self.config = config or TranscriptionConfig()
        self._api_key = self.config.api_key or os.environ.get("TRANSCRIPTION_API_KEY")
        self._api_base = self.config.api_base
        self._model = self.config.model or "whisper-1"
        self._extra_headers = self.config.extra_headers

    @property
    def api_key(self) -> str | None:
        """Get API key from config or environment."""
        return self._api_key

    @property
    def api_base(self) -> str | None:
        """Get API base URL from config."""
        return self._api_base

    @property
    def model(self) -> str:
        """Get default model from config."""
        return self._model

    async def transcribe(
        self,
        file_path: str | Path,
        model: str | None = None,
        language: str | None = None,
        prompt: str | None = None,
        response_format: str = "text",
        temperature: float | None = None,
    ) -> str:
        """
        Transcribe an audio file using LiteLLM.

        Auto-routes to correct provider based on model string:
        - "whisper-1" → OpenAI
        - "groq/whisper-large-v3" → Groq
        - "deepgram/nova-2" → Deepgram
        - etc.

        Args:
            file_path: Path to the audio file (string or Path object).
            model: Override default model (e.g., "deepgram/nova-2").
            language: Language code (e.g., "en", "es").
            prompt: Optional prompt to guide transcription.
            response_format: Output format - "text", "json", "srt", "verbose_json", "vtt".
            temperature: Sampling temperature (0.0 to 1.0).

        Returns:
            Transcribed text.
        """
        # Check API key
        if not self.api_key:
            logger.warning("Transcription API key not configured")
            return ""

        # Validate file exists
        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""

        # Determine model to use
        model = model or self.model

        try:
            # Import LiteLLM here to avoid import errors if not installed
            from litellm import transcription

            # Open file and call LiteLLM
            with open(path, "rb") as audio_file:
                response = transcription(
                    model=model,
                    file=audio_file,
                    language=language,
                    prompt=prompt,
                    response_format=response_format,
                    temperature=temperature,
                    api_key=self.api_key,
                    api_base=self.api_base,
                    extra_headers=self._extra_headers,
                )

                # Return the transcribed text
                # LiteLLM returns TranscriptionResponse with .text attribute
                return getattr(response, "text", str(response))

        except ImportError:
            logger.error("LiteLLM not installed. Install with: pip install litellm")
            return ""
        except Exception as e:
            logger.error("Transcription error: {}", e)
            return ""

    async def transcribe_with_fallback(
        self,
        file_path: str | Path,
        fallback_models: list[str] | None = None,
        **kwargs,
    ) -> str:
        """
        Transcribe with fallback models if primary fails.

        Args:
            file_path: Path to the audio file.
            fallback_models: List of fallback models to try (e.g., ["whisper-1", "groq/whisper-large-v3"]).
            **kwargs: Additional arguments passed to transcribe().

        Returns:
            Transcribed text or empty string on failure.
        """
        # Try primary model first
        result = await self.transcribe(file_path, **kwargs)
        if result:
            return result

        # Try fallback models
        fallback_models = fallback_models or []
        for fallback_model in fallback_models:
            logger.info("Trying fallback model: {}", fallback_model)
            result = await self.transcribe(file_path, model=fallback_model, **kwargs)
            if result:
                return result

        logger.error("All transcription models failed")
        return ""


# Convenience function for quick usage
async def transcribe(
    file_path: str | Path,
    model: str = "whisper-1",
    api_key: str | None = None,
    **kwargs,
) -> str:
    """
    Quick transcription function.

    Args:
        file_path: Path to audio file.
        model: Model to use (default: whisper-1).
        api_key: API key (optional, uses TRANSCRIPTION_API_KEY env var if not provided).
        **kwargs: Additional parameters (language, prompt, etc.).

    Returns:
        Transcribed text.
    """
    config = TranscriptionConfig(
        api_key=api_key or "",
        model=model,
    )
    provider = LiteLLMTranscriptionProvider(config)
    return await provider.transcribe(file_path, **kwargs)
