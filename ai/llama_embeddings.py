"""
LlamaIndex-compatible embeddings using OpenAI API.
Replaces the custom EmbeddingClient with llama-index's embedding abstraction.
"""

from llama_index.core.bridge.pydantic import PrivateAttr
from llama_index.core.embeddings import BaseEmbedding
from openai import OpenAI

from utils.config import CFG
from utils.logger import get_logger

logger = get_logger(__name__)


class OpenAICompatibleEmbedding(BaseEmbedding):
    """
    LlamaIndex-compatible embedding model using OpenAI-compatible API.
    Works with any OpenAI-compatible endpoint (OpenAI, Azure, local servers, etc.)
    """

    _client: OpenAI = PrivateAttr()
    _model: str = PrivateAttr()

    def __init__(self, api_key: str | None = None, api_base: str | None = None, model: str | None = None, **kwargs):
        """
        Initialize the embedding model.

        Args:
            api_key: OpenAI API key (defaults to config)
            api_base: API base URL (defaults to config)
            model: Model name (defaults to config)
        """
        super().__init__(**kwargs)

        self._client = OpenAI(api_key=api_key or CFG.get("api_key"), base_url=api_base or CFG.get("api_url"))
        self._model = model or CFG.get("embedding_model") or "text-embedding-3-small"

        if not getattr(self.__class__, "_init_logged", False):
            logger.info(f"Initialized OpenAICompatibleEmbedding with model: {self._model}")
            self.__class__._init_logged = True

    @classmethod
    def class_name(cls) -> str:
        return "OpenAICompatibleEmbedding"

    async def _aget_query_embedding(self, query: str) -> list[float]:
        """Get query embedding asynchronously."""
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        """Get text embedding asynchronously."""
        return self._get_text_embedding(text)

    def _get_query_embedding(self, query: str) -> list[float]:
        """Get embedding for a query."""
        return self._get_text_embedding(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        """Get embedding for a text."""
        try:
            text = text.replace("\n", " ").strip()
            if not text:
                logger.warning("Empty text provided for embedding")
                return []

            response = self._client.embeddings.create(input=[text], model=self._model)

            if response.data and len(response.data) > 0:
                embedding = response.data[0].embedding
                import re

                match = re.search(r"path:\s*([^\s]+)", text)
                file_path = match.group(1) if match else "unknown"
                logger.info(f"Generated embedding (dim {len(embedding)}) for file: {file_path}")
                return embedding
            else:
                logger.error("No embedding returned from API")
                return []

        except Exception as e:
            logger.exception(f"Failed to generate embedding: {e}")
            return []

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        """Get embeddings for multiple texts."""
        embeddings = []
        for text in texts:
            embedding = self._get_text_embedding(text)
            embeddings.append(embedding)
        return embeddings
