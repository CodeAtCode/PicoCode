# ai/embedding_client.py
import os
import time
import uuid
import json
import logging
import traceback
from typing import List, Optional, Dict, Any

import requests

logger = logging.getLogger("ai.analyzer.embedding")

# Configurable via environment
EMBEDDING_API_URL = os.getenv("PICOCODE_EMBEDDING_URL", "https://example.com/v1/embeddings")
EMBEDDING_API_KEY = os.getenv("PICOCODE_EMBEDDING_API_KEY", "")
DEFAULT_TIMEOUT = float(os.getenv("PICOCODE_EMBEDDING_TIMEOUT", "30"))  # seconds per request
MAX_RETRIES = int(os.getenv("PICOCODE_EMBEDDING_RETRIES", "2"))
BACKOFF_FACTOR = float(os.getenv("PICOCODE_EMBEDDING_BACKOFF", "1.5"))
MODEL_NAME = os.getenv("PICOCODE_EMBEDDING_MODEL", "text-embedding-3-small")

# Optionally enable requests debug logging by setting PICOCODE_HTTP_DEBUG=true
if os.getenv("PICOCODE_HTTP_DEBUG", "").lower() in ("1", "true", "yes"):
    logging.getLogger("requests").setLevel(logging.DEBUG)
    logging.getLogger("urllib3").setLevel(logging.DEBUG)


class EmbeddingError(Exception):
    pass


class EmbeddingClient:
    def __init__(self,
                 api_url: str = EMBEDDING_API_URL,
                 api_key: str = EMBEDDING_API_KEY,
                 model: str = MODEL_NAME,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = MAX_RETRIES,
                 backoff: float = BACKOFF_FACTOR):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"Authorization": f"Bearer {api_key}"})
        self.session.headers.update({"Content-Type": "application/json"})

    def _log_request_start(self, request_id: str, file_path: str, chunk_index: int, chunk_len: int):
        logger.debug(
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
        logger.debug(
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
                                logger.info(
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
                    logger.warning(
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
                logger.error("Embedding API Timeout", extra={"request_id": request_id, "error": str(e)})
            except requests.RequestException as e:
                elapsed = time.perf_counter() - start
                err_msg = f"RequestException after {elapsed:.2f}s: {e}\n{traceback.format_exc()}"
                logger.error("Embedding request exception", extra={"request_id": request_id, "error": err_msg})
            except Exception as e:
                elapsed = time.perf_counter() - start
                err_msg = f"Unexpected error after {elapsed:.2f}s: {e}\n{traceback.format_exc()}"
                logger.exception("Unexpected embedding exception", extra={"request_id": request_id})

            # Retry logic
            if attempt > self.max_retries:
                logger.error(
                    "Max retries exceeded for embedding request",
                    extra={"request_id": request_id, "file": file_path, "chunk_index": chunk_index, "attempts": attempt},
                )
                raise EmbeddingError(f"Failed to get embedding after {attempt} attempts. Last error: {err_msg}")

            # Backoff and retry
            sleep_for = self.backoff * (2 ** (attempt - 1))
            logger.info(
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
                logger.error(
                    "Failed to embed chunk",
                    extra={"file": file_path, "chunk_index": i, "error": str(e)},
                )
                # append a failure marker or skip depending on desired behavior
                results.append({"chunk_index": i, "embedding": None, "error": str(e)})
        return results
