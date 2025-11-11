"""
LlamaIndex-based chunking for code and text.
Replaces smart_chunker.py with llama-index's built-in splitters.
"""
from typing import List
from llama_index.core.node_parser import CodeSplitter, SentenceSplitter
from llama_index.core.schema import Document

try:
    import tree_sitter_language_pack as tslp
except ImportError:
    tslp = None

from utils.logger import get_logger

logger = get_logger(__name__)


def chunk_with_llama_index(
    content: str,
    language: str = "text",
    chunk_size: int = 800,
    chunk_overlap: int = 100
) -> List[str]:
    """
    Chunk text or code using llama-index's splitters.
    
    Args:
        content: Text or code content to chunk
        language: Programming language (python, javascript, etc.) or "text"
        chunk_size: Target size for each chunk in characters
        chunk_overlap: Overlap between chunks in characters
    
    Returns:
        List of text chunks
    """
    # Map language names to tree-sitter-language-pack identifiers
    language_map = {
        "python": "python",
        "javascript": "javascript",
        "js": "javascript",
        "typescript": "typescript",
        "ts": "typescript",
        "java": "java",
        "go": "go",
        "rust": "rust",
        "c": "c",
        "cpp": "cpp",
        "c++": "cpp",
    }
    
    try:
        # Check if it's a supported code language
        llama_lang = language_map.get(language.lower())
        
        if llama_lang:
            # Create parser using tree_sitter_language_pack if available
            parser = None
            if tslp is not None:
                try:
                    parser = tslp.get_parser(llama_lang)
                    logger.debug(f"Created parser for language: {llama_lang}")
                except Exception as e:
                    logger.warning(f"Could not create parser for {llama_lang}: {e}")
            
            # Use CodeSplitter for code
            splitter = CodeSplitter(
                language=llama_lang,
                chunk_lines=40,  # Target lines per chunk (approximation)
                chunk_lines_overlap=5,  # Overlap in lines
                max_chars=chunk_size,
                parser=parser  # Pass parser explicitly to avoid tree_sitter_languages dependency
            )
            logger.debug(f"Using CodeSplitter for language: {llama_lang}")
        else:
            # Use SentenceSplitter for text or unknown languages
            splitter = SentenceSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                paragraph_separator="\n\n",
                secondary_chunking_regex="[^,.;。？！]+[,.;。？！]?"
            )
            logger.debug(f"Using SentenceSplitter for language: {language}")
        
        # Create a document and split it
        doc = Document(text=content)
        nodes = splitter.get_nodes_from_documents([doc])
        
        # Extract text from nodes
        chunks = [node.text for node in nodes if node.text]
        
        logger.debug(f"Split content into {len(chunks)} chunks")
        return chunks if chunks else [content]
        
    except Exception as e:
        logger.exception(f"Error chunking with llama-index: {e}")
        # Fallback to simple chunking
        return simple_chunk(content, chunk_size, chunk_overlap)


def simple_chunk(text: str, chunk_size: int = 800, chunk_overlap: int = 100) -> List[str]:
    """
    Simple character-based chunking fallback.
    
    Args:
        text: Text to chunk
        chunk_size: Size of each chunk
        chunk_overlap: Overlap between chunks
    
    Returns:
        List of text chunks
    """
    if not text:
        return []
    
    chunks = []
    step = max(1, chunk_size - chunk_overlap)
    
    for i in range(0, len(text), step):
        end = min(i + chunk_size, len(text))
        chunk = text[i:end]
        if chunk.strip():
            chunks.append(chunk)
        
        if end >= len(text):
            break
    
    return chunks if chunks else [text]
