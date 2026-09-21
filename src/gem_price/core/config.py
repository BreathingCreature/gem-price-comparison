"""Configuration management for GeM Price Comparison."""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Settings:
    """Application settings loaded from environment variables with defaults."""
    
    # Project paths - db and data are in the code/ directory (project root)
    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent.parent.parent)
    data_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent.parent.parent / "data")
    db_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent.parent.parent / "db" / "gem_project.db")
    raw_data_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent.parent.parent / "data" / "raw")
    debug_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent.parent.parent / "data" / "raw" / "gem_debug")
    
    # Scraping settings
    gem_max_results: int = 20
    flipkart_max_results: int = 30
    gem_polite_delay_min: float = 2.0
    gem_polite_delay_max: float = 3.0
    flipkart_page_delay_min: float = 1.0
    flipkart_page_delay_max: float = 2.0
    flipkart_scroll_count: int = 8
    selenium_page_load_timeout: int = 45
    selenium_script_timeout: int = 30
    
    # Matching settings
    embedding_model: str = "all-MiniLM-L6-v2"
    cosine_threshold: float = 0.55
    composite_threshold: float = 0.6
    top_k_matches: int = 10
    
    # LLM settings
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "openai/gpt-oss-20b"
    nvidia_min_seconds_between_calls: float = 1.6
    nvidia_max_tokens: int = 700
    nvidia_temperature: float = 0.0
    
    # API settings
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_request_timeout: int = 30
    
    # Database settings
    db_foreign_keys: bool = True
    
    # Logging
    log_level: str = "INFO"
    log_format: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    
    @classmethod
    def from_env(cls) -> "Settings":
        """Create settings from environment variables."""
        return cls(
            gem_max_results=int(os.getenv("GEM_MAX_RESULTS", "20")),
            flipkart_max_results=int(os.getenv("FLIPKART_MAX_RESULTS", "30")),
            embedding_model=os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
            cosine_threshold=float(os.getenv("COSINE_THRESHOLD", "0.55")),
            composite_threshold=float(os.getenv("COMPOSITE_THRESHOLD", "0.6")),
            top_k_matches=int(os.getenv("TOP_K_MATCHES", "10")),
            nvidia_model=os.getenv("NVIDIA_MODEL", "openai/gpt-oss-20b"),
            nvidia_min_seconds_between_calls=float(os.getenv("NVIDIA_MIN_SECONDS_BETWEEN_CALLS", "1.6")),
            nvidia_max_tokens=int(os.getenv("NVIDIA_MAX_TOKENS", "700")),
            api_host=os.getenv("API_HOST", "0.0.0.0"),
            api_port=int(os.getenv("API_PORT", "8000")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


# Global settings instance
settings = Settings.from_env()