from typing import Optional
import os
import time
import threading
from openai import OpenAI

from utils.config import CFG

# Instantiate client exactly as you requested, reading the key from the standard env var.
_client = OpenAI(api_key=CFG.get("api_key"), base_url=CFG.get("api_url"),)

# Default models come from CFG (loaded from .env). Analyzer can pass model explicitly too.
DEFAULT_EMBEDDING_MODEL = CFG.get("embedding_model")
DEFAULT_CODING_MODEL = CFG.get("coding_model")

# Rate limiting configuration
_RATE_LIMIT_CALLS = 100  # max calls per minute
_RATE_LIMIT_WINDOW = 60.0  # seconds
_rate_limit_lock = threading.Lock()
_rate_limit_times = []

# Circuit breaker configuration
_CIRCUIT_BREAKER_THRESHOLD = 5  # consecutive failures to open circuit
_CIRCUIT_BREAKER_TIMEOUT = 60.0  # seconds to wait before retry when open
_circuit_state = {"failures": 0, "open_until": 0}
_circuit_lock = threading.Lock()

def _check_rate_limit():
    """Simple token bucket rate limiter"""
    with _rate_limit_lock:
        now = time.time()
        # Remove timestamps older than window
        while _rate_limit_times and _rate_limit_times[0] < now - _RATE_LIMIT_WINDOW:
            _rate_limit_times.pop(0)
        
        if len(_rate_limit_times) >= _RATE_LIMIT_CALLS:
            # Rate limit exceeded, wait
            sleep_time = _rate_limit_times[0] + _RATE_LIMIT_WINDOW - now
            if sleep_time > 0:
                time.sleep(sleep_time)
                # Retry after sleep
                return _check_rate_limit()
        
        _rate_limit_times.append(now)

def _check_circuit_breaker():
    """Check if circuit breaker is open"""
    with _circuit_lock:
        if _circuit_state["open_until"] > time.time():
            raise RuntimeError(f"Circuit breaker open: too many recent failures. Retry after {_circuit_state['open_until'] - time.time():.1f}s")

def _record_success():
    """Reset circuit breaker on successful call"""
    with _circuit_lock:
        _circuit_state["failures"] = 0
        _circuit_state["open_until"] = 0

def _record_failure():
    """Increment failure counter and potentially open circuit"""
    with _circuit_lock:
        _circuit_state["failures"] += 1
        if _circuit_state["failures"] >= _CIRCUIT_BREAKER_THRESHOLD:
            _circuit_state["open_until"] = time.time() + _CIRCUIT_BREAKER_TIMEOUT

def _retry_with_backoff(func, *args, **kwargs):
    """Retry function with exponential backoff on transient errors"""
    max_retries = 3
    base_delay = 1.0
    
    # Transient error indicators that should be retried
    transient_error_keywords = [
        'timeout', 'timed out', 'connection', 'network', 
        'temporary', 'unavailable', 'rate limit', '429', 
        '500', '502', '503', '504', 'overload'
    ]
    
    for attempt in range(max_retries):
        try:
            _check_circuit_breaker()
            _check_rate_limit()
            result = func(*args, **kwargs)
            _record_success()
            return result
        except Exception as e:
            error_str = str(e).lower()
            is_transient = any(keyword in error_str for keyword in transient_error_keywords)
            
            # Always record failure for circuit breaker
            _record_failure()
            
            # Only retry on transient errors or if it's not the last attempt
            if attempt == max_retries - 1:
                raise
            
            # If it's clearly not a transient error, don't retry
            if not is_transient and attempt > 0:
                raise
            
            delay = base_delay * (2 ** attempt)
            time.sleep(delay)


def get_embedding_for_text(text: str, model: Optional[str] = None):
    """
    Return embedding vector (list[float]) using the new OpenAI client.
    Includes rate limiting, retry logic with exponential backoff, and circuit breaker.
    model: optional model id; if not provided, uses DEFAULT_EMBEDDING_MODEL from CFG.
    """
    model_to_use = model or DEFAULT_EMBEDDING_MODEL
    if not model_to_use:
        raise RuntimeError("No embedding model configured. Set EMBEDDING_MODEL in .env or pass model argument.")

    def _get_embedding():
        resp = _client.embeddings.create(model=model_to_use, input=text)
        return resp.data[0].embedding
    
    try:
        return _retry_with_backoff(_get_embedding)
    except Exception as e:
        raise RuntimeError(f"Failed to obtain embedding from OpenAI client: {e}") from e


def call_coding_api(prompt: str, model: Optional[str] = None, max_tokens: int = 1024):
    """
    Call a generative/coding model via the new OpenAI client.
    Includes rate limiting, retry logic with exponential backoff, and circuit breaker.
    Prefers chat completions (client.chat.completions.create) and falls back to client.completions.create
    or client.responses.create only if those exist on the provider client. No legacy SDK usage.
    Returns textual response (string).
    """
    model_to_use = model or DEFAULT_CODING_MODEL
    if not model_to_use:
        raise RuntimeError("No coding model configured. Set CODING_MODEL in .env or pass model argument.")

    def _call_model():
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
    
    try:
        return _retry_with_backoff(_call_model)
    except Exception as e:
        raise RuntimeError(f"Failed to call coding model via OpenAI client: {e}") from e
