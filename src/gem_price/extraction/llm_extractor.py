"""LLM-based product identity extraction using NVIDIA NIM."""
import os
import json
import time
import re
from typing import Dict, Any, Optional
from datetime import datetime

from openai import OpenAI
from gem_price.core.config import settings
from gem_price.core.logging import setup_logging
from gem_price.core.models import ProductIdentity, ProductCategory

logger = setup_logging(__name__)


# LLM extraction prompt
EXTRACTION_PROMPT = """You are an expert at extracting product information from e-commerce pages.

Given the product page content from GeM (Government e-Marketplace), extract the canonical product identity.

Return ONLY a valid JSON object with these exact fields:
{
    "brand": "string - manufacturer brand name (e.g., Samsung, Dell, HP)",
    "model_number": "string - exact model number/SKU (e.g., LS49C950UAWXXL, 280-G9-SFF)",
    "product_type": "string - general type (e.g., Monitor, Laptop, Desktop, Keyboard)",
    "category": "string - one of: monitor, laptop, desktop, mobile, tablet, keyboard, mouse, headphone, printer, storage, graphics_card, processor, motherboard, ram, power_supply, unknown",
    "specifications": {
        "screen_size": "string (e.g., 49 inch, 27 inch)",
        "resolution": "string (e.g., 5120x1440, 1920x1080)",
        "refresh_rate": "string (e.g., 240Hz, 144Hz)",
        "panel_type": "string (e.g., QLED, IPS, VA, OLED)",
        "aspect_ratio": "string (e.g., 32:9, 16:9)",
        "curvature": "string (e.g., 1000R, 1800R)",
        "response_time": "string (e.g., 1ms, 4ms)",
        "cpu": "string (e.g., Intel Core i3-12100, AMD Ryzen 5 5600)",
        "ram": "string (e.g., 16GB DDR4)",
        "storage": "string (e.g., 512GB NVMe SSD)",
        "gpu": "string (e.g., RTX 3060, Intel UHD)",
        "panel_type": "string (e.g., IPS, VA, OLED)",
        "connectivity": "string (e.g., HDMI 2.1, DP 1.4, USB-C)",
        "color": "string",
        "weight": "string",
        "warranty": "string"
    },
    "confidence": "float between 0 and 1",
    "extraction_method": "llm"
}

Rules:
1. Model number MUST be exact as shown on the page (e.g., LS49C950UAWXXL, not just 49C950)
2. Only include specifications you are confident about - omit uncertain fields
3. Normalize units (e.g., "49 inch" not "49\"" or "124cm")
4. If model number is not clearly identifiable, return empty string
5. Category must be one of the listed values
6. Confidence: 1.0 = absolutely certain, 0.5 = uncertain
7. Return ONLY the JSON object, no extra text or markdown

Product Page Content:
{content}
"""

# Fallback for when LLM is unavailable
FALLBACK_EXTRACTION_PROMPT = """Extract product info from this GeM page content.

Return JSON:
{
    "brand": "string",
    "model_number": "string", 
    "product_type": "string",
    "category": "string",
    "specifications": {},
    "confidence": 0.5,
    "extraction_method": "fallback"
}"""


class LLMExtractor:
    """LLM-based product identity extractor using NVIDIA NIM."""
    
    def __init__(self):
        self.client = None
        self._init_client()
    
    def _init_client(self):
        """Initialize OpenAI client for NVIDIA NIM."""
        api_key = os.getenv("NVIDIA_API_KEY") or settings.nvidia_api_key
        if not api_key:
            logger.warning("NVIDIA_API_KEY not set, LLM extraction will be unavailable")
            self.client = None
            return
        
        try:
            self.client = OpenAI(
                base_url=settings.nvidia_base_url,
                api_key=api_key,
                timeout=settings.llm_timeout
            )
            logger.info(f"LLM client initialized with model: {settings.nvidia_model}")
        except Exception as e:
            logger.error(f"Failed to initialize LLM client: {e}")
            self.client = None
    
    def extract(self, html: str, url: str) -> Optional[dict]:
        """Extract product identity using LLM."""
        if not self.client:
            logger.warning("LLM client not available, skipping LLM extraction")
            return None
        
        try:
            # Prepare content for LLM
            content = self._prepare_content(html)
            if not content or len(content) < 100:
                logger.warning("Insufficient content for LLM extraction")
                return None
            
            # Call LLM
            result = self._call_llm(content)
            if result:
                result["extraction_method"] = "llm"
                return result
            
        except Exception as e:
            logger.error(f"LLM extraction failed: {e}")
        
        return None
    
    def _prepare_content(self, html: str) -> str:
        """Extract relevant text from HTML for LLM."""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        
        # Extract key elements
        parts = []
        
        # Title
        title_elem = soup.select_one(".variant-title, .product-title, h1, .variant-title")
        if title_elem:
            parts.append(f"Title: {title_elem.get_text(strip=True)}")
        
        # Price
        for selector in [".variant-final-price", ".price", ".product-price"]:
            elem = soup.select_one(selector)
            if elem:
                parts.append(f"Price: {elem.get_text(strip=True)}")
                break
        
        # Specifications
        for selector in [".spec-table", ".specifications", ".variant-specs", "table"]:
            elems = soup.select(selector)
            for elem in elems[:3]:  # Limit to first 3 tables
                text = elem.get_text(" ", strip=True)
                if len(text) > 50:
                    parts.append(f"Specs: {text[:2000]}")
                    break
        
        # Description
        desc_elem = soup.select_one(".description, .product-desc, .variant-desc")
        if desc_elem:
            parts.append(f"Description: {desc_elem.get_text(strip=True)[:1000]}")
        
        # Full text (truncated)
        full_text = soup.get_text(" ", strip=True)
        if len(full_text) > 5000:
            full_text = full_text[:5000] + "... [truncated]"
        parts.append(f"Full text: {full_text}")
        
        return "\n\n".join(parts)
    
    def _call_llm(self, content: str) -> Optional[dict]:
        """Call the LLM with the extraction prompt."""
        try:
            prompt = EXTRACTION_PROMPT.format(content=content)
            
            response = self.client.chat.completions.create(
                model=settings.nvidia_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=settings.nvidia_temperature,
                max_tokens=settings.nvidia_max_tokens,
            )
            
            raw = response.choices[0].message.content
            if not raw:
                logger.warning("LLM returned empty response")
                return None
            
            raw = raw.strip()
            logger.debug(f"LLM raw response: {raw[:200]}...")
            
            # Parse JSON
            return self._parse_json_response(raw)
            
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return None
    
    def _parse_json_response(self, raw: str) -> Optional[dict]:
        """Parse JSON from LLM response, handling markdown fences and errors."""
        # Remove markdown fences
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Try to find JSON object in the text
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(cleaned[start:end+1])
                except json.JSONDecodeError:
                    pass
            
            logger.warning(f"Failed to parse LLM JSON response: {raw[:200]}")
            return None


def extract_with_llm(html: str, url: str) -> Optional[dict]:
    """Main entry point for LLM extraction."""
    extractor = LLMExtractor()
    return extractor.extract(html, url)


# Quick test
if __name__ == "__main__":
    import sys
    sys.path.insert(0, r"C:\Users\Creature\Creature Folder\Synced\Project\code\src")
    
    import requests
    url = "https://mkp.gem.gov.in/computer-monitor-v2/samsung-ultrawide-monitor/p-5116877-19154912158-cat.html"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    resp = requests.get(url, headers=headers, timeout=15)
    result = extract_with_llm(resp.text, url)
    print(json.dumps(result, indent=2, ensure_ascii=False))