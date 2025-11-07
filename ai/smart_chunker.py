"""
Smart chunking module for code-aware text splitting.
Respects code structure (functions, classes, methods) for better semantic search.
"""
import re
from typing import List, Tuple, Optional
from pathlib import Path


class SmartChunker:
    """
    Code-aware chunker that splits text based on language structure.
    Falls back to simple chunking for non-code or unknown languages.
    """

    def __init__(self, chunk_size: int = 800, overlap: int = 100):
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk(self, text: str, language: str = "text") -> List[str]:
        """
        Chunk text based on language-specific rules.

        Args:
            text: Text content to chunk
            language: Programming language identifier

        Returns:
            List of text chunks
        """
        if language in ["python", "javascript", "typescript", "java", "go", "rust", "c", "cpp"]:
            return self._chunk_code(text, language)
        else:
            return self._chunk_simple(text)

    def _chunk_code(self, text: str, language: str) -> List[str]:
        """
        Smart chunking for code that respects structure.
        """
        # Split into logical units (functions, classes, etc.)
        units = self._split_into_units(text, language)

        if not units:
            # Fallback to simple chunking if structure detection fails
            return self._chunk_simple(text)

        chunks = []
        current_chunk = []
        current_size = 0

        for unit_text, unit_type in units:
            unit_size = len(unit_text)

            # If single unit is larger than chunk_size, split it
            if unit_size > self.chunk_size:
                # Save current chunk if it has content
                if current_chunk:
                    chunks.append("\n".join(current_chunk))
                    current_chunk = []
                    current_size = 0

                # Split large unit with simple chunking
                sub_chunks = self._chunk_simple(unit_text)
                chunks.extend(sub_chunks)
                continue

            # Check if adding this unit would exceed chunk_size
            if current_size + unit_size > self.chunk_size and current_chunk:
                # Save current chunk
                chunks.append("\n".join(current_chunk))

                # Start new chunk with overlap
                # Keep last unit for context
                if len(current_chunk) > 1:
                    last_unit = current_chunk[-1]
                    current_chunk = [last_unit, unit_text]
                    current_size = len(last_unit) + unit_size
                else:
                    current_chunk = [unit_text]
                    current_size = unit_size
            else:
                # Add to current chunk
                current_chunk.append(unit_text)
                current_size += unit_size

        # Add remaining chunk
        if current_chunk:
            chunks.append("\n".join(current_chunk))

        return chunks if chunks else [text]

    def _split_into_units(self, text: str, language: str) -> List[Tuple[str, str]]:
        """
        Split code into logical units (functions, classes, etc.).
        Returns list of (text, unit_type) tuples.
        """
        if language == "python":
            return self._split_python(text)
        elif language in ["javascript", "typescript"]:
            return self._split_javascript(text)
        elif language == "java":
            return self._split_java(text)
        elif language in ["go", "rust", "c", "cpp"]:
            return self._split_c_style(text)
        else:
            return []

    def _split_python(self, text: str) -> List[Tuple[str, str]]:
        """
        Split Python code into classes and functions.

        Uses indentation-based parsing. Works well for most Python code
        but may have edge cases with complex indentation patterns.
        Falls back to simple chunking if parsing fails.
        """
        units = []
        lines = text.split("\n")
        current_unit = []
        current_type = None
        indent_stack = []  # only populated when a class/def starts

        for i, line in enumerate(lines):
            stripped = line.lstrip()
            indent = len(line) - len(stripped)

            # Detect class or function definition
            if stripped.startswith("class ") or stripped.startswith("def "):
                # Save previous unit if exists
                if current_unit:
                    units.append(("\n".join(current_unit), current_type or "code"))
                    current_unit = []

                current_type = "class" if stripped.startswith("class ") else "function"
                current_unit = [line]
                indent_stack = [indent]
            elif current_unit:
                # Continue current unit
                current_unit.append(line)

                # Check if we're back to base indent (end of function/class)
                # Guard access to indent_stack: only compare indent if indent_stack is populated
                if stripped and not stripped.startswith("#") and indent_stack and indent <= indent_stack[0]:
                    if i < len(lines) - 1:  # Not last line
                        # Check next line to see if it's a new definition
                        next_stripped = lines[i + 1].lstrip()
                        if next_stripped.startswith("class ") or next_stripped.startswith("def "):
                            # End current unit
                            # current_unit contains the line that dedented; we want to separate the trailing dedent line
                            # The previous block is current_unit[:-1], remaining starts from current_unit[-1]
                            units.append(("\n".join(current_unit[:-1]), current_type))
                            # Start module-level accumulation with the dedent line
                            current_unit = [current_unit[-1]]
                            current_type = "module"
                            indent_stack = []
            else:
                # Module-level code
                if not current_unit:
                    current_type = "module"
                current_unit.append(line)

        # Add remaining unit
        if current_unit:
            units.append(("\n".join(current_unit), current_type or "code"))

        return units

    def _split_javascript(self, text: str) -> List[Tuple[str, str]]:
        """
        Split JavaScript/TypeScript code into functions and classes.

        Uses regex patterns to match function and class declarations.
        Works well for standard code patterns but may not handle all
        edge cases with nested structures. Falls back to brace-based
        splitting if regex matching doesn't find units.
        """
        units = []

        # Regex patterns for JS/TS
        # Match function declarations, arrow functions, class declarations
        # Note: Non-greedy matching, works for most cases but not perfect for deeply nested code
        patterns = [
            r'((?:export\s+)?(?:async\s+)?function\s+\w+\s*\([^)]*\)\s*{[\s\S]*?})',
            r'((?:export\s+)?const\s+\w+\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*{[\s\S]*?})',
            r'((?:export\s+)?class\s+\w+(?:\s+extends\s+\w+)?\s*{[\s\S]*?})',
        ]

        # Try to match and extract units
        for pattern in patterns:
            matches = re.finditer(pattern, text)
            for match in matches:
                unit_text = match.group(1)
                unit_type = "function" if "function" in unit_text or "=>" in unit_text else "class"
                units.append((unit_text, unit_type))

        # If no matches, fall back to brace-based splitting
        if not units:
            units = self._split_by_braces(text)

        return units

    def _split_java(self, text: str) -> List[Tuple[str, str]]:
        """Split Java code into classes and methods."""
        # Similar to JavaScript but with Java-specific patterns
        patterns = [
            r'((?:public|private|protected)?\s*(?:static)?\s*(?:class|interface|enum)\s+\w+[\s\S]*?{[\s\S]*?})',
            r'((?:public|private|protected)?\s*(?:static)?\s*(?:\w+\s+)?\w+\s*\([^)]*\)\s*(?:throws\s+\w+(?:,\s*\w+)*)?\s*{[\s\S]*?})',
        ]

        units = []
        for pattern in patterns:
            matches = re.finditer(pattern, text)
            for match in matches:
                unit_text = match.group(1)
                unit_type = "class" if any(kw in unit_text for kw in ["class", "interface", "enum"]) else "method"
                units.append((unit_text, unit_type))

        if not units:
            units = self._split_by_braces(text)

        return units

    def _split_c_style(self, text: str) -> List[Tuple[str, str]]:
        """Split C-style languages (Go, Rust, C, C++) into functions."""
        units = self._split_by_braces(text)
        return units if units else []

    def _split_by_braces(self, text: str) -> List[Tuple[str, str]]:
        """
        Generic brace-based splitting for C-style languages.
        Finds balanced brace blocks.

        Note: This is a simple heuristic that doesn't handle braces
        inside strings, comments, or template literals. It works well
        for most code but may produce imperfect results in edge cases.
        The chunker will still fall back to simple chunking if needed.
        """
        units = []
        lines = text.split("\n")
        current_unit = []
        brace_count = 0
        in_block = False

        for line in lines:
            current_unit.append(line)

            # Count braces (simple heuristic)
            # Note: Doesn't handle strings/comments perfectly, but works well in practice
            brace_count += line.count("{") - line.count("}")

            if "{" in line and not in_block:
                in_block = True

            if in_block and brace_count == 0:
                # Block closed
                units.append(("\n".join(current_unit), "function"))
                current_unit = []
                in_block = False

        # Add remaining lines
        if current_unit:
            units.append(("\n".join(current_unit), "code"))

        return units

    def _chunk_simple(self, text: str) -> List[str]:
        """
        Simple character-based chunking with overlap.
        Used as fallback or for non-code content.
        """
        if not text:
            return []

        if len(text) <= self.chunk_size:
            return [text]

        chunks = []
        step = max(1, self.chunk_size - self.overlap)
        start = 0

        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            chunks.append(text[start:end])
            start += step

        return chunks


# Global instance for convenience
_default_chunker = SmartChunker()


def smart_chunk(text: str, language: str = "text", chunk_size: int = 800, overlap: int = 100) -> List[str]:
    """
    Convenience function for smart chunking.

    Args:
        text: Text to chunk
        language: Programming language
        chunk_size: Maximum chunk size in characters
        overlap: Overlap between chunks in characters

    Returns:
        List of text chunks
    """
    chunker = SmartChunker(chunk_size=chunk_size, overlap=overlap)
    return chunker.chunk(text, language)
