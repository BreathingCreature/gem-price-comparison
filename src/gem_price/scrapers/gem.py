"""GeM (Government e-Marketplace) scraper using Selenium."""
import time
import random
import re
from typing import Dict, List, Optional, Any
from datetime import datetime
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from bs4 import BeautifulSoup

from gem_price.core.config import settings
from gem_price.core.logging import setup_logging
from gem_price.core.models import ProductIdentity, ProductCategory

logger = setup_logging(__name__)


class GeMScraper:
    """Scraper for GeM (Government e-Marketplace) product pages."""
    
    def __init__(self):
        self.driver = None
        self.wait_timeout = settings.selenium_page_load_timeout
    
    def _create_driver(self) -> webdriver.Chrome:
        """Create a stealth Chrome driver for GeM."""
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument(f"user-agent={self._get_user_agent()}")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(settings.selenium_page_load_timeout)
        driver.set_script_timeout(settings.selenium_script_timeout)
        
        # Hide webdriver property
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
        
        return driver
    
    def _get_user_agent(self) -> str:
        """Return a realistic user agent string."""
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    
    def _get_driver(self) -> webdriver.Chrome:
        """Get or create the driver instance."""
        if self.driver is None:
            self.driver = self._create_driver()
        return self.driver
    
    def scrape_product(self, url: str) -> ProductIdentity:
        """Scrape a single GeM product page."""
        # Clean URL - remove fragment
        url = url.split('#')[0]
        logger.info(f"Scraping GeM product: {url}")
        
        driver = self._get_driver()
        try:
            driver.get(url)
            self._wait_for_page_load(driver)
            
            # Check for blocking
            if self._is_blocked(driver):
                logger.warning("GeM page appears to be blocked")
                return self._empty_identity(url, "blocked")
            
            # Wait for product content
            self._wait_for_product_content(driver)
            
            # Extract data
            html = driver.page_source
            identity = self._parse_product_page(html, url)
            logger.info(f"Successfully scraped: {identity.raw_name[:80]}")
            return identity
            
        except TimeoutException:
            logger.error(f"Timeout loading GeM page: {url}")
            return self._empty_identity(url, "timeout")
        except Exception as e:
            logger.error(f"Error scraping GeM product: {e}")
            return self._empty_identity(url, f"error: {e}")
    
    def _wait_for_page_load(self, driver: webdriver.Chrome):
        """Wait for initial page load."""
        try:
            WebDriverWait(driver, self.wait_timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            logger.warning("Page load timeout, continuing anyway")
    
    def _is_blocked(self, driver: webdriver.Chrome) -> bool:
        """Check if the page is blocked (CAPTCHA, redirect, etc.)."""
        try:
            current_url = driver.current_url.lower()
            if "captcha" in current_url or "access denied" in driver.page_source.lower():
                return True
            body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
            if any(sig in body_text for sig in ["captcha", "please verify", "are you a human", "robot check"]):
                return True
        except Exception:
            pass
        return False
    
    def _wait_for_product_content(self, driver: webdriver.Chrome):
        """Wait for product content to be present."""
        try:
            # Try multiple selectors for product content
            selectors = [
                ".variant-title",
                ".variant-desc",
                ".product-title",
                "h1",
                "[class*='product']",
                "[class*='variant']"
            ]
            for selector in selectors:
                try:
                    WebDriverWait(driver, 5).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                    )
                    break
                except TimeoutException:
                    continue
        except Exception:
            pass
    
    def scrape(self, url: str) -> ProductIdentity:
        """Main scrape method with retries."""
        for attempt in range(settings.max_retries):
            try:
                return self.scrape_product(url)
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed: {e}")
                if attempt < settings.max_retries - 1:
                    time.sleep(settings.retry_backoff ** attempt)
                else:
                    raise
    
    def _empty_identity(self, url: str, reason: str) -> ProductIdentity:
        """Return an empty identity on failure."""
        from gem_price.core.models import ProductIdentity, ProductCategory
        return ProductIdentity(
            brand="",
            model_number="",
            product_type="",
            category=ProductCategory.UNKNOWN,
            source_url=url,
            confidence=0.0,
            extraction_method=f"failed: {reason}"
        )
    
    def _parse_product_page(self, html: str, url: str):
        """Parse the product page HTML to extract identity."""
        from gem_price.core.models import ProductIdentity, ProductCategory
        from gem_price.extraction.identity import extract_identity
        return extract_identity(html, url)
    
    def close(self):
        """Close the browser."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# Convenience function
def scrape_gem_product(url: str) -> ProductIdentity:
    """Scrape a single GeM product URL."""
    with GeMScraper() as scraper:
        return scraper.scrape(url)