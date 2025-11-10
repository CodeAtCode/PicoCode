from typing import Optional, List, Dict, Any
import os
import time
import uuid
import json
import logging
import traceback
import threading
from openai import OpenAI
import requests

from utils.config import CFG

# Instantiate client exactly as you requested, reading the key from the standard env var.
_client = OpenAI(api_key=CFG.get("api_key"), base_url=CFG.get("api_url"),)

# Default models come from CFG (loaded from .env). Analyzer can pass model explicitly too.
DEFAULT_EMBEDDING_MODEL = CFG.get("embedding_model")
DEFAULT_CODING_MODEL = CFG.get("coding_model")

# Embedding client logger
_embedding_logger = logging.getLogger("ai.analyzer.embedding")

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


class EmbeddingError(Exception):
    """Custom exception for embedding failures"""
    pass


class EmbeddingClient:
    """
    Embedding client with detailed logging, retry logic, and configurable timeouts.
    Provides better debugging for embedding API failures.
    """
    def __init__(self,
                 api_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 timeout: float = 30.0,
                 max_retries: int = 2,
                 backoff: float = 1.5):
        self.api_url = api_url or CFG.get("api_url")
        self.api_key = api_key or CFG.get("api_key")
        self.model = model or DEFAULT_EMBEDDING_MODEL or "text-embedding-3-small"
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"Authorization": f"Bearer {self.api_key}"})
        self.session.headers.update({"Content-Type": "application/json"})

    def _generate_curl_command(self, url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> str:
        """
        Generate a curl command for debugging purposes.
        Masks the API key for security.
        """
        # Start with basic curl command
        curl_parts = ["curl", "-X", "POST", f"'{url}'"]
        
        # Add headers
        for key, value in headers.items():
            if key.lower() == "authorization" and value:
                # Mask the API key for security
                if value.startswith("Bearer "):
                    masked_value = f"Bearer <API_KEY_MASKED>"
                else:
                    masked_value = "<API_KEY_MASKED>"
                curl_parts.append(f"-H '{key}: {masked_value}'")
            else:
                curl_parts.append(f"-H '{key}: {value}'")
        
        # Add data payload
        payload_json = json.dumps(payload)
        # Escape single quotes in the JSON for shell compatibility
        payload_json_escaped = payload_json.replace("'", "'\\''")
        curl_parts.append(f"-d '{payload_json_escaped}'")
        
        return " \\\n  ".join(curl_parts)

    def _log_request_start(self, request_id: str, file_path: str, chunk_index: int, chunk_len: int):
        _embedding_logger.debug(
            "Embedding request START",
            extra={
                "request_id": request_id,
                "file": file_path,
                "chunk_index": chunk_index,
                "chunk_length": chunk_len,
                "model": self.model,
                "api_url": self.api_url,
                "timeout": self.timeout,
            },
        )

    def _log_request_end(self, request_id: str, elapsed: float, status: Optional[int], response_body_preview: str):
        _embedding_logger.debug(
            "Embedding request END",
            extra={
                "request_id": request_id,
                "elapsed_s": elapsed,
                "status": status,
                "response_preview": response_body_preview,
            },
        )

    def embed_text(self, text: str, file_path: str = "<unknown>", chunk_index: int = 0) -> List[float]:
        """
        Embed a single chunk of text. Returns the embedding vector.
        Raises EmbeddingError on failure.
        """
        request_id = str(uuid.uuid4())
        chunk_len = len(text)
        self._log_request_start(request_id, file_path, chunk_index, chunk_len)

        payload = {
            "model": self.model,
            "input": text,
        }

        attempt = 0
        err_msg = ""
        while True:
            attempt += 1
            start = time.perf_counter()
            try:
                resp = self.session.post(
                    self.api_url,
                    data=json.dumps(payload),
                    timeout=self.timeout,
                )
                elapsed = time.perf_counter() - start

                # Try to parse JSON safely
                try:
                    resp_json = resp.json()
                except Exception:
                    resp_json = None

                preview = ""
                if resp_json is not None:
                    preview = json.dumps(resp_json)[:1000]
                else:
                    preview = (resp.text or "")[:1000]

                self._log_request_end(request_id, elapsed, resp.status_code, preview)

                if resp.status_code >= 200 and resp.status_code < 300:
                    # expected format: {"data": [{"embedding": [...]}], ...}
                    if not resp_json:
                        raise EmbeddingError(f"Empty JSON response (status={resp.status_code})")
                    try:
                        # tolerant extraction
                        data = resp_json.get("data") if isinstance(resp_json, dict) else None
                        if data and isinstance(data, list) and len(data) > 0:
                            emb = data[0].get("embedding")
                            if emb and isinstance(emb, list):
                                _embedding_logger.info(
                                    "Embedding succeeded",
                                    extra={"request_id": request_id, "file": file_path, "chunk_index": chunk_index},
                                )
                                return emb
                        # Fallback: maybe top-level "embedding" key
                        if isinstance(resp_json, dict) and "embedding" in resp_json:
                            emb = resp_json["embedding"]
                            if isinstance(emb, list):
                                return emb
                        raise EmbeddingError(f"Unexpected embedding response shape: {resp_json}")
                    except KeyError as e:
                        raise EmbeddingError(f"Missing keys in embedding response: {e}")
                else:
                    # Non-2xx
                    _embedding_logger.warning(
                        "Embedding API returned non-2xx",
                        extra={
                            "request_id": request_id,
                            "status_code": resp.status_code,
                            "file": file_path,
                            "chunk_index": chunk_index,
                            "attempt": attempt,
                            "body_preview": preview,
                        },
                    )
                    # fall through to retry logic
                    err_msg = f"Status {resp.status_code}: {preview}"

            except requests.Timeout as e:
                elapsed = time.perf_counter() - start
                err_msg = f"Timeout after {elapsed:.2f}s: {e}"
                
                # Generate and print curl command for debugging
                curl_command = self._generate_curl_command(self.api_url, dict(self.session.headers), payload)
                _embedding_logger.error(
                    "Embedding API Timeout",
                    extra={
                        "request_id": request_id,
                        "error": str(e),
                        "elapsed_s": elapsed,
                        "curl_command": curl_command
                    }
                )
                # Also print to console for easy debugging
                print(f"\n{'='*80}")
                print(f"Embedding request timed out after {elapsed:.2f}s")
                print(f"Request ID: {request_id}")
                print(f"File: {file_path}, Chunk: {chunk_index}")
                print(f"\nDebug with this curl command:")
                print(curl_command)
                print(f"{'='*80}\n")
            except requests.RequestException as e:
                elapsed = time.perf_counter() - start
                err_msg = f"RequestException after {elapsed:.2f}s: {e}\n{traceback.format_exc()}"
                _embedding_logger.error("Embedding request exception", extra={"request_id": request_id, "error": err_msg})
            except Exception as e:
                elapsed = time.perf_counter() - start
                err_msg = f"Unexpected error after {elapsed:.2f}s: {e}\n{traceback.format_exc()}"
                _embedding_logger.exception("Unexpected embedding exception", extra={"request_id": request_id})

            # Retry logic
            if attempt > self.max_retries:
                _embedding_logger.error(
                    "Max retries exceeded for embedding request",
                    extra={"request_id": request_id, "file": file_path, "chunk_index": chunk_index, "attempts": attempt},
                )
                raise EmbeddingError(f"Failed to get embedding after {attempt} attempts. Last error: {err_msg}")

            # Backoff and retry
            sleep_for = self.backoff * (2 ** (attempt - 1))
            _embedding_logger.info(
                "Retrying embedding request",
                extra={
                    "request_id": request_id,
                    "file": file_path,
                    "chunk_index": chunk_index,
                    "attempt": attempt,
                    "sleep_s": sleep_for,
                },
            )
            time.sleep(sleep_for)

    def embed_multiple(self, chunks: List[str], file_path: str = "<unknown>") -> List[Dict[str, Any]]:
        """
        Embed a list of text chunks. Returns list of dicts: {"chunk_index": i, "embedding": [...]}.
        This method logs progress and errors for each chunk.
        """
        results = []
        for i, chunk in enumerate(chunks):
            try:
                emb = self.embed_text(chunk, file_path=file_path, chunk_index=i)
                results.append({"chunk_index": i, "embedding": emb})
            except EmbeddingError as e:
                _embedding_logger.error(
                    "Failed to embed chunk",
                    extra={"file": file_path, "chunk_index": i, "error": str(e)},
                )
                # append a failure marker or skip depending on desired behavior
                results.append({"chunk_index": i, "embedding": None, "error": str(e)})
        return results


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
