"""Optional Google answer adapter; disabled without separate company permission."""

import os
import re
import httpx


class GeminiAnswers:
    name = "gemini"

    def __init__(self, settings):
        if (os.getenv("GSME_GEMINI_CONTENT_APPROVED") != "yes" or not os.getenv("GEMINI_API_KEY")
                or os.getenv("GSME_EXTERNAL_CONTENT_APPROVED") != "yes"):
            raise ValueError("Gemini requires GEMINI_API_KEY and separate GSME_GEMINI_CONTENT_APPROVED=yes")
        self.settings, self.model = settings, settings.gemini_model
        if not re.fullmatch(r"gemini-[a-zA-Z0-9._-]+", self.model):
            raise ValueError("Set GSME_GEMINI_MODEL to an available Gemini model ID")

    def generate(self, question, evidence):
        from .answers import AnswerProviderUnavailable, InvalidAnswer, messages_for, command_schema
        messages = messages_for(question, evidence)
        try:
            with httpx.Client(timeout=self.settings.timeout, follow_redirects=False) as client:
                response = client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                    headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, json={
                        "systemInstruction": {"parts": [{"text": messages[0]["content"]}]},
                        "contents": [{"role": "user", "parts": [{"text": messages[1]["content"]}]}],
                        "generationConfig": {"temperature": 0, "maxOutputTokens": self.settings.max_output_tokens,
                            "candidateCount": 1, "responseMimeType": "application/json",
                            "responseJsonSchema": command_schema(evidence)}})
                if response.status_code in {401, 403, 404, 429, 500, 502, 503, 504}:
                    raise AnswerProviderUnavailable("Gemini answer provider unavailable")
                if not response.is_success:
                    raise InvalidAnswer("Gemini answer request failed validation")
        except httpx.TransportError:
            raise AnswerProviderUnavailable("Gemini answer provider unavailable") from None
        try:
            raw = response.json()
            candidate = raw["candidates"][0]
            if candidate.get("finishReason") != "STOP":
                raise InvalidAnswer("Gemini answer was incomplete or blocked")
            return "".join(part.get("text", "") for part in candidate["content"]["parts"] if not part.get("thought"))
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise InvalidAnswer("Gemini answer response was malformed") from None
