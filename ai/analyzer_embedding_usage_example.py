# ai/analyzer_embedding_usage_example.py
import logging
from ai.embedding_client import EmbeddingClient

logger = logging.getLogger("ai.analyzer")

# create client (will pick up env vars)
client = EmbeddingClient()

def process_file_and_embed(file_path: str, chunks: list[str]):
    logger.info("Start embedding file", extra={"file": file_path, "num_chunks": len(chunks)})
    results = client.embed_multiple(chunks, file_path=file_path)
    # Inspect results for None embeddings and act accordingly
    for r in results:
        if r.get("embedding") is None:
            logger.warning("Chunk embedding failed", extra={"file": file_path, "chunk_index": r["chunk_index"], "error": r.get("error")})
        else:
            # continue with storing the embedding
            pass
    return results
