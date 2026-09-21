"""Structured logging configuration."""
import logging
import sys
from typing import Optional
from pathlib import Path
from gem_price.core.config import settings


def setup_logging(name: str = "gem_price", level: Optional[str] = None) -> logging.Logger:
    """Set up and return a configured logger."""
    log_level = level or settings.log_level
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level.upper()))
    
    if logger.handlers:
        return logger
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, log_level.upper()))
    console_formatter = logging.Formatter(settings.log_format)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # File handler
    log_file = settings.data_dir / "gem_price.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    return logger


# Module-level logger
logger = setup_logging()