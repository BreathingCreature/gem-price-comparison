"""Amazon.in scraper for product listings."""
import re
import random
import time
from typing import List, Dict, Any, Optional
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from gem_price.core.config import settings
from gem_price.core.logging import setup_logging

logger = setup_logging(__name__)


class AmazonScraper:
    """Amazon.in product scraper."""
    
    def __init__(self):
        self.driver = None
        self.base_url = "https://www.amazon.in"
    
    def _create_driver(self) -> webdriver.Chrome:
        """Create stealth Chrome driver."""
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(settings.selenium_page_load_timeout)
        driver.set_script_timeout(settings.selenium_script_timeout)
        
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
        return driver
    
    def _get_driver(self) -> webdriver.Chrome:
        if self.driver is None:
            self.driver = self._create_driver()
        return self.driver
    
    def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        """Search Amazon.in for a product."""
        logger.info(f"Amazon search: {query}")
        
        driver = self._get_driver()
        results = []
        
        try:
            url = f"https://www.amazon.in/s?k={query.replace(' ', '+')}"
            driver.get(f"{self.base_url}/s?k={query.replace(' ', '+')}")
            
            # Wait for results
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-component-type='s-search-result']"))
                )
            except TimeoutException:
                logger.warning("Amazon search results timeout")
                return []
            
            # Scroll to load more
            for _ in range(3):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(random.uniform(1, 2))
            
            # Parse results
            products = driver.find_elements(By.CSS_SELECTOR, "[data-component-type='s-search-result']")
            
            for product in products[:max_results]:
                try:
                    result = self._parse_product(product)
                    if result and result.get("product_name"):
                        results.append(result)
                except Exception as e:
                    logger.debug(f"Failed to parse Amazon product: {e}")
                    continue
            
        except Exception as e:
            logger.error(f"Amazon search error: {e}")
        
        return results[:5]
    
    def _parse_product(self, element) -> Optional[Dict[str, Any]]:
        """Parse a single product element."""
        try:
            # Name
            name_elem = element.find_element(By.CSS_SELECTOR, "h2 a span, .a-text-normal")
            name = name_elem.text.strip() if name_elem else ""
            
            if not name:
                return None
            
            # Price
            price = ""
            for price_sel in [
                ".a-price-whole",
                ".a-price .a-offscreen",
                ".a-price-whole",
                "[class*='price']"
            ]:
                try:
                    price_elem = element.find_element(By.CSS_SELECTOR, price_sel)
                    price = price_elem.text.strip()
                    if price:
                        break
                except NoSuchElementException:
                    continue
            
            # Link
            link = ""
            try:
                link_elem = element.find_element(By.CSS_SELECTOR, "h2 a, .a-link-normal")
                link = link_elem.get_attribute("href") or ""
            except NoSuchElementException:
                pass
            
            # Image
            image_url = ""
            try:
                img_elem = element.find_element(By.CSS_SELECTOR, "img.s-image")
                image_url = img_elem.get_attribute("src") or ""
            except NoSuchElementException:
                pass
            
            # Seller
            seller = ""
            try:
                seller_elem = element.find_element(By.CSS_SELECTOR, ".a-row .a-size-base, .a-row .a-color-base")
                seller = seller_elem.text.strip()
            except NoSuchElementException:
                pass
            
            if not name:
                return None
            
            return {
                "marketplace": "Amazon.in",
                "product_name": name[:200],
                "price": price,
                "url": link,
                "image_url": image_url,
                "seller": seller,
                "in_stock": True,
                "source": "amazon"
            }
            
        except Exception as e:
            logger.debug(f"Failed to parse Amazon product: {e}")
            return None
    
    def close(self):
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


class FlipkartScraper:
    """Flipkart product scraper."""
    
    def __init__(self):
        self.driver = None
        self.base_url = "https://www.flipkart.com"
    
    def _create_driver(self) -> webdriver.Chrome:
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(settings.selenium_page_load_timeout)
        driver.set_script_timeout(settings.selenium_script_timeout)
        
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
        return driver
    
    def _get_driver(self) -> webdriver.Chrome:
        if self.driver is None:
            self.driver = self._create_driver()
        return self.driver
    
    def search(self, query: str, max_results: int = 5) -> List[Dict[str, Any]]:
        logger.info(f"Flipkart search: {query}")
        
        driver = self._get_driver()
        results = []
        
        try:
            url = f"{self.base_url}/search?q={query.replace(' ', '+')}"
            driver.get(url)
            
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[class*='product'], [data-id], ._1AtVbE"))
                )
            except TimeoutException:
                logger.warning("Flipkart search timeout")
                return []
            
            for _ in range(3):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(random.uniform(1, 2))
            
            products = driver.find_elements(By.CSS_SELECTOR, "[data-id], ._1AtVbE, [class*='product']")
            
            for product in products[:max_results]:
                try:
                    result = self._parse_product(product)
                    if result and result.get("product_name"):
                        results.append(result)
                except Exception as e:
                    logger.debug(f"Failed to parse Flipkart product: {e}")
                    continue
            
        except Exception as e:
            logger.error(f"Flipkart search error: {e}")
        
        return results[:5]
    
    def _parse_product(self, element) -> Optional[Dict[str, Any]]:
        try:
            name = ""
            for sel in ["div._4rR01T", "div.KzDlHZ", "div.syl9yP", "a[title]"]:
                try:
                    elem = element.find_element(By.CSS_SELECTOR, sel)
                    name = elem.get_attribute("title") or elem.text
                    if name:
                        break
                except NoSuchElementException:
                    continue
            
            if not name:
                return None
            
            price = ""
            for sel in ["div._30jeq3", "div._1_WHN1", "div.Nx9bqj"]:
                try:
                    price = element.find_element(By.CSS_SELECTOR, sel).text.strip()
                    if price:
                        break
                except NoSuchElementException:
                    continue
            
            link = ""
            try:
                link_elem = element.find_element(By.CSS_SELECTOR, "a[href*='/p/']")
                link = link_elem.get_attribute("href") or ""
            except NoSuchElementException:
                pass
            
            image = ""
            try:
                img = element.find_element(By.CSS_SELECTOR, "img[src]")
                image = img.get_attribute("src") or ""
            except NoSuchElementException:
                pass
            
            seller = ""
            try:
                seller = element.find_element(By.CSS_SELECTOR, "div._2M7o9V, ._1xHGtK").text.strip()
            except NoSuchElementException:
                pass
            
            if not name:
                return None
            
            return {
                "marketplace": "Flipkart",
                "product_name": name[:200],
                "price": price,
                "url": link,
                "image_url": image,
                "seller": seller,
                "in_stock": True,
                "source": "flipkart"
            }
        except Exception as e:
            logger.debug(f"Failed to parse Flipkart product: {e}")
            return None
    
    def close(self):
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


# Quick test
if __name__ == "__main__":
    import sys
    sys.path.insert(0, r"C:\Users\Creature\Creature Folder\Synced\Project\code\src")
    
    with AmazonScraper() as amazon:
        results = amazon.search("Samsung LS49C950UAWXXL", max_results=3)
        for r in results:
            print(f"  {r['marketplace']}: {r['product_name'][:60]} - {r['price']} - {r['url'][:60]}")