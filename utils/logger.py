"""
Centralized logging configuration for PicoCode.
"""

import logging
import sys

from utils.config import CFG

logging.basicConfig(
    level=logging.DEBUG if CFG.get("debug") else logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", handlers=[logging.StreamHandler(sys.stdout)]
)

if CFG.get("debug"):
    logging.getLogger("llama_index").setLevel(logging.INFO)
    logging.getLogger("openai").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a module.

    Args:
        name: Module name (usually __name__)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)
