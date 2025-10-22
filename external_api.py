from typing import Optional
import os
from openai import OpenAI

from config import CFG

# Instantiate client exactly as you requested, reading the key from the standard env var.
_client = OpenAI(api_key=CFG.get("api_key"), base_url=CFG.get("api_url"),)

# Default models come from CFG (loaded from .env). Analyzer can pass model explicitly too.
DEFAULT_EMBEDDING_MODEL = CFG.get("embedding_model")
DEFAULT_CODING_MODEL = CFG.get("coding_model")


def get_embedding_for_text(text: str, model: Optional[str] = None):
    """
    Return embedding vector (list[float]) using the new OpenAI client.
    model: optional model id; if not provided, uses DEFAULT_EMBEDDING_MODEL from CFG.
    """
    model_to_use = model or DEFAULT_EMBEDDING_MODEL
    if not model_to_use:
        raise RuntimeError("No embedding model configured. Set EMBEDDING_MODEL in .env or pass model argument.")

    try:
        resp = _client.embeddings.create(model=model_to_use, input=text)
        return resp.data[0].embedding
    except Exception as e:
        raise RuntimeError(f"Failed to obtain embedding from OpenAI client: {e}") from e


def call_coding_api(prompt: str, model: Optional[str] = None, max_tokens: int = 1024):
    """
    Call a generative/coding model via the new OpenAI client.
    Prefers chat completions (client.chat.completions.create) and falls back to client.completions.create
    or client.responses.create only if those exist on the provider client. No legacy SDK usage.
    Returns textual response (string).
    """
    model_to_use = model or DEFAULT_CODING_MODEL
    if not model_to_use:
        raise RuntimeError("No coding model configured. Set CODING_MODEL in .env or pass model argument.")

    try:
        # Preferred: chat completions on the new client
        if hasattr(_client, "chat") and hasattr(_client.chat, "completions") and hasattr(_client.chat.completions, "create"):
            resp = _client.chat.completions.create(
                model=model_to_use,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens
            )
            if resp and getattr(resp, "choices", None):
                choice = resp.choices[0]
                # object-like: choice.message.content
                if hasattr(choice, "message") and getattr(choice.message, "content", None):
                    return choice.message.content
                # dict-like fallback
                if isinstance(choice, dict):
                    if "message" in choice and isinstance(choice["message"], dict) and "content" in choice["message"]:
                        return choice["message"]["content"]
                    if "text" in choice and choice["text"]:
                        return choice["text"]

        # Next: completions.create
        if hasattr(_client, "completions") and hasattr(_client.completions, "create"):
            resp = _client.completions.create(model=model_to_use, prompt=prompt, max_tokens=max_tokens)
            if resp and getattr(resp, "choices", None):
                choice = resp.choices[0]
                if hasattr(choice, "text") and getattr(choice, "text", None):
                    return choice.text
                if isinstance(choice, dict) and "text" in choice:
                    return choice["text"]

        # Last attempt: responses API (provider-specific)
        if hasattr(_client, "responses") and hasattr(_client.responses, "create"):
            resp = _client.responses.create(model=model_to_use, input=prompt, max_tokens=max_tokens)
            output = getattr(resp, "output", None)
            if isinstance(output, list) and len(output) > 0:
                parts = []
                for item in output:
                    if isinstance(item, dict):
                        content = item.get("content", [])
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and "text" in block:
                                    parts.append(block["text"])
                if parts:
                    return "\n".join(parts)

        raise RuntimeError("OpenAI client did not return a usable completion for the provided model.")
    except Exception as e:
        raise RuntimeError(f"Failed to call coding model via OpenAI client: {e}") from e
