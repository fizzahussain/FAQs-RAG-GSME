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

    def generate_stream(self, question, evidence):
        """Google's SSE generate-content protocol, with a required STOP marker."""
        from .answers import AnswerProviderUnavailable, InvalidAnswer, messages_for, command_schema
        from .streaming import strict_loads
        messages = messages_for(question, evidence)
        complete, frame = False, []
        try:
            with httpx.Client(timeout=self.settings.timeout, follow_redirects=False) as client:
                with client.stream("POST", f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:streamGenerateContent?alt=sse",
                        headers={"x-goog-api-key":os.environ["GEMINI_API_KEY"]}, json={
                            "systemInstruction":{"parts":[{"text":messages[0]["content"]}]},
                            "contents":[{"role":"user","parts":[{"text":messages[1]["content"]}]}],
                            "generationConfig":{"temperature":0, "maxOutputTokens":self.settings.max_output_tokens,
                                "candidateCount":1, "responseMimeType":"application/json", "responseJsonSchema":command_schema(evidence)}}) as response:
                    if response.status_code in {401,403,404,429,500,502,503,504}:
                        raise AnswerProviderUnavailable("Gemini answer provider unavailable")
                    if not response.is_success:
                        raise InvalidAnswer("Gemini streaming request failed validation")
                    # A missing final blank line is allowed at HTTP EOF, but a
                    # truncated JSON event or missing STOP is not success.
                    from itertools import chain
                    for line in chain(response.iter_lines(), [""]):
                        if line.startswith("data:"):
                            frame.append(line[5:].lstrip(" "))
                            if sum(len(s.encode()) for s in frame) > 128000:
                                raise InvalidAnswer("Gemini event exceeds its bounded size")
                        elif not line and frame:
                            raw = strict_loads("\n".join(frame)); frame = []
                            if raw.get("error"):
                                raise AnswerProviderUnavailable("Gemini answer provider unavailable")
                            for candidate in raw.get("candidates", []):
                                if candidate.get("index", 0) != 0:
                                    raise InvalidAnswer("Unexpected Gemini candidate")
                                for part in candidate.get("content", {}).get("parts", []):
                                    if not part.get("thought") and part.get("text"):
                                        yield part["text"]
                                finish = candidate.get("finishReason")
                                if finish:
                                    if finish != "STOP":
                                        raise InvalidAnswer("Gemini answer was incomplete or blocked")
                                    complete = True
        except httpx.TransportError:
            raise AnswerProviderUnavailable("Gemini answer provider unavailable") from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise InvalidAnswer("Gemini answer stream was malformed") from None
        if not complete:
            raise InvalidAnswer("Gemini answer stream was incomplete")
