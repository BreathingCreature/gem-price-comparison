"""Configuration management for Universal Price Comparison."""
import os
from pathlib import Path
from typing import List, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables with defaults."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    
    # Project paths
    project_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent.parent.parent
    )
    data_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent.parent.parent / "data"
    )
    db_path: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent.parent.parent / "db" / "gem_project.db"
    )
    cache_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent.parent.parent / "data" / "cache"
    )
    
    # Scraping settings
    scrape_timeout: int = 30
    scrape_delay_min: float = 2.0
    scrape_delay_max: float = 5.0
    max_retries: int = 3
    retry_backoff: float = 2.0
    concurrent_scrapers: int = 3
    selenium_page_load_timeout: int = 45
    selenium_script_timeout: int = 30
    
    # LLM settings
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "openai/gpt-oss-20b"
    nvidia_api_key: str = Field(default="", description="NVIDIA API key from env")
    nvidia_max_tokens: int = 700
    nvidia_temperature: float = 0.0
    nvidia_min_seconds_between_calls: float = 1.6
    llm_timeout: int = 30
    
    # Search settings
    google_search_enabled: bool = True
    serpapi_key: str = Field(default="", description="SerpAPI key for Google Shopping")
    max_search_results: int = 20
    search_timeout: int = 30
    
    # Matching settings
    match_threshold: float = 0.9
    critical_spec_tolerance: float = 0.0  # 0 = exact match required
    
    # Cache settings
    cache_identity_ttl: int = 86400  # 24 hours
    cache_search_ttl: int = 3600     # 1 hour
    cache_verification_ttl: int = 21600  # 6 hours
    cache_price_ttl: int = 1800      # 30 minutes
    
    # Marketplace settings
    enabled_marketplaces: List[str] = Field(
        default_factory=lambda: [
            "google_shopping", "amazon", "flipkart", "reliance_digital",
            "croma", "vijay_sales", "md_computers", "primeabgb",
            "samsung_official", "lg_official", "dell_official", "hp_official"
        ]
    )
    
    # API settings
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    
    # Logging
    log_level: str = "INFO"
    log_format: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


settings = Settings()