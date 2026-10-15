"""ElevenLabs over its REST API: clone a voice, find it again, remove it, and speak with it.

No SDK (P7): the job and the API call four endpoints, and an SDK would add its import to
every cold start.
"""

import httpx2

BASE_URL = "https://api.elevenlabs.io"
PREVIEW_FORMAT = "mp3_44100_128"


class ElevenLabsError(Exception):
    """ElevenLabs refused the request, or could not be reached."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type  # ElevenLabs' `detail.type`, when it sends one

    @property
    def rejected(self) -> bool:
        """The request itself was refused, such as a sample it cannot clone: sending it
        again would fail the same way. A refused key is not the request's fault, though
        ElevenLabs answers some with 400: like a timeout or a limit, ElevenLabs is then
        unavailable to us."""
        return (
            self.status_code is not None
            and 400 <= self.status_code < 500
            and self.status_code not in (401, 403, 408, 429)
            and self.error_type != "authentication_error"
        )


def voice_name(voice_id: object) -> str:
    """The name a version's voice gets at ElevenLabs: a retry finds it by this name."""
    return f"wobot-{voice_id}"


class ElevenLabs:
    def __init__(self, client: httpx2.AsyncClient, api_key: str) -> None:
        self._client = client
        self._headers = {"xi-api-key": api_key}

    async def find_voice(self, name: str) -> str | None:
        """The ID of the account's own voice with exactly this name, if one exists. The
        search also matches parts of names and descriptions, so the name is compared."""
        response = await self._send(
            "GET",
            "/v2/voices",
            params={"search": name, "voice_type": "personal", "page_size": 100},
        )
        for voice in response.json()["voices"]:
            if voice.get("name") == name:
                return voice["voice_id"]
        return None

    async def add_voice(self, name: str, sample: bytes, filename: str) -> str:
        """Clone a voice from one sample (Instant Voice Cloning); returns its ID."""
        response = await self._send(
            "POST",
            "/v1/voices/add",
            data={"name": name, "remove_background_noise": "false"},
            files={"files": (filename, sample)},
        )
        return response.json()["voice_id"]

    async def delete_voice(self, provider_voice_id: str) -> None:
        """Remove a voice; one already gone counts as removed."""
        try:
            await self._send("DELETE", f"/v1/voices/{provider_voice_id}")
        except ElevenLabsError as exc:
            if exc.status_code != 404:
                raise

    async def speak(
        self, provider_voice_id: str, text: str, model: str, output_format: str = PREVIEW_FORMAT
    ) -> bytes:
        """The text spoken in the voice, as MP3."""
        response = await self._send(
            "POST",
            f"/v1/text-to-speech/{provider_voice_id}",
            params={"output_format": output_format},
            json={"text": text, "model_id": model},
        )
        return response.content

    async def _send(self, method: str, path: str, **kwargs) -> httpx2.Response:
        try:
            response = await self._client.request(
                method, BASE_URL + path, headers=self._headers, **kwargs
            )
        except httpx2.HTTPError as exc:
            raise ElevenLabsError(f"{method} {path}: {type(exc).__name__}") from exc
        if response.is_error:
            # The message may repeat what was sent, so only the error's type and code are
            # kept, which say what went wrong without it.
            error_type, code = _error_of(response)
            raise ElevenLabsError(
                f"{method} {path}: HTTP {response.status_code} {error_type} {code}",
                response.status_code,
                error_type,
            )
        return response


def _error_of(response: httpx2.Response) -> tuple[str | None, str | None]:
    """`detail.type` and `detail.code` of an error body, when it has them."""
    try:
        detail = response.json().get("detail")
    except ValueError:
        return None, None
    if not isinstance(detail, dict):
        return None, None
    return detail.get("type"), detail.get("code")
