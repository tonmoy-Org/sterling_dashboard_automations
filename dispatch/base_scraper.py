"""
Base Scraper Class (Dispatch Automation Dedicated)
Provides browser automation and FieldEdge login functionality for Dispatch Board Display Automation.
"""
import os
import time
import json
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page
import pytz

# Enforce US Pacific Time (PST/PDT)
os.environ['TZ'] = 'America/Los_Angeles'
if hasattr(time, 'tzset'):
    time.tzset()

# Load environment variables robustly from local dispatch dir
_dispatch_dir = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_dispatch_dir, '.env')
load_dotenv(dotenv_path=_env_path)

# Configuration paths
RULES_FILE_PATH = os.path.join(_dispatch_dir, "config", "scraper_rules.json")
HEADLESS_MODE = os.getenv("HEADLESS", "true").strip().lower() in ("true", "1", "t", "yes")

# Pacific Standard / Daylight Timezone helper functions
PST_TZ = pytz.timezone("America/Los_Angeles")

def get_pst_now():
    """Returns current datetime in US Pacific Timezone (America/Los_Angeles)."""
    return datetime.now(PST_TZ)

def get_pst_today_str(fmt="%Y-%m-%d"):
    """Returns current date string in US Pacific Timezone (America/Los_Angeles)."""
    return get_pst_now().strftime(fmt)


class BaseScraper:
    """
    Base scraper class for Dispatch Board Display Automation.
    """
    
    @staticmethod
    def get_pst_now():
        return get_pst_now()

    @staticmethod
    def get_pst_today_str(fmt="%Y-%m-%d"):
        return get_pst_today_str(fmt)
    
    def __init__(self):
        """Initialize scraper with browser configuration."""
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        
        # Credentials from environment
        self.fieldedge_email = os.environ.get("DASH_EMAIL", "")
        self.fieldedge_password = os.environ.get("DASH_PASSWORD", "")
        
        # Load scraping rules
        self.rules = self._load_rules()
        self.scraper_name = "dispatch-board-display-automation"
    
    def _load_rules(self):
        """Load scraping rules from JSON configuration file."""
        potential_paths = [
            RULES_FILE_PATH,
            os.path.join(_dispatch_dir, "config", "scraper_rules.json"),
        ]
        
        rules_data = None
        for path in potential_paths:
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        rules_data = json.load(f)
                    print(f"✅ Rules loaded successfully from: {path}")
                    break
                except Exception as e:
                    print(f"⚠️ Failed to load rules from {path}: {e}")
        
        if not rules_data:
            print(f"❌ Rules file not found! Checked: {potential_paths}")
            return {}

        return rules_data[0] if isinstance(rules_data, list) and len(rules_data) > 0 else rules_data
    
    async def initialize(self):
        """Launch and configure Playwright browser instance."""
        try:
            self.playwright = await async_playwright().start()

            viewport_width = int(os.getenv("VIEWPORT_WIDTH", "1280"))
            viewport_height = int(os.getenv("VIEWPORT_HEIGHT", "720"))

            chrome_args = [
                f"--window-size={viewport_width},{viewport_height}",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ]
            if HEADLESS_MODE:
                chrome_args.append("--headless=new")

            proxy_config = None
            proxy_server = os.getenv("PROXY_SERVER", "").strip()
            if proxy_server:
                proxy_config = {"server": proxy_server}
                proxy_username = os.getenv("PROXY_USERNAME", "").strip()
                proxy_password = os.getenv("PROXY_PASSWORD", "").strip()
                if proxy_username:
                    proxy_config["username"] = proxy_username
                if proxy_password:
                    proxy_config["password"] = proxy_password
                print(f"🔀 Proxy configured: {proxy_server}")

            launch_kwargs = dict(
                headless=HEADLESS_MODE,
                slow_mo=50,
                args=chrome_args,
                channel="chrome",
            )
            if proxy_config:
                launch_kwargs["proxy"] = proxy_config

            try:
                self.browser = await self.playwright.chromium.launch(**launch_kwargs)
                print("✅ Browser launched: system Google Chrome")
            except Exception as chrome_err:
                print(f"⚠️ System Chrome not available ({chrome_err}). Falling back to bundled Chromium...")
                fallback_kwargs = {k: v for k, v in launch_kwargs.items() if k != "channel"}
                fallback_args = [a for a in chrome_args if a != "--headless=new"]
                if HEADLESS_MODE:
                    fallback_args.append("--headless")
                fallback_kwargs["args"] = fallback_args
                self.browser = await self.playwright.chromium.launch(**fallback_kwargs)
                print("✅ Browser launched: bundled Chromium (fallback)")

            context_kwargs = dict(
                viewport={"width": viewport_width, "height": viewport_height},
                locale="en-US",
                timezone_id="America/Los_Angeles",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/138.0.0.0 Safari/537.36"
                ),
            )
            self.context = await self.browser.new_context(**context_kwargs)

            # Block Pendo overlays
            await self.context.route("**/*pendo*", lambda route: route.abort())
            await self.context.route("**/*app.pendo.io*", lambda route: route.abort())

            self.page = await self.context.new_page()

        except Exception as e:
            print(f"❌ Failed to initialize browser: {e}")
            raise

    async def login_fieldedge(self, page: Page = None):
        """Authenticate to FieldEdge dashboard."""
        FIELDEDGE_LOGIN_URL = self.rules.get('fieldedge_login_url', 'https://login.fieldedge.com/Account/Login')
        
        def _update_fe_status(status_str):
            if hasattr(self, "progress_info") and isinstance(self.progress_info, dict):
                self.progress_info["fieldedge_status"] = status_str
            cb = getattr(self, "_on_progress_cb", None)
            if cb:
                try:
                    cb(getattr(self, "progress_info", {"fieldedge_status": status_str}))
                except Exception:
                    pass

        _update_fe_status("Logging in to FieldEdge...")
        try:
            if not self.fieldedge_email or not self.fieldedge_password:
                raise ValueError("FieldEdge credentials not found. Please ensure DASH_EMAIL and DASH_PASSWORD are set in the .env file.")

            if not page:
                page = self.page

            username_xpath = self.rules.get('username_xpath', '//input[@name="UserName" or @name="username" or @id="Username"]')
            password_xpath = self.rules.get('password_xpath', '//input[@name="Password" or @name="password" or @id="Password"]')
            login_button_xpath = self.rules.get('login_button_xpath', '//button[@type="submit" or contains(normalize-space(), "Sign In") or contains(normalize-space(), "Login")]')

            current_url = page.url
            if FIELDEDGE_LOGIN_URL not in current_url:
                print(f"Navigating to FieldEdge login page: {FIELDEDGE_LOGIN_URL}")
                await page.goto(FIELDEDGE_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

            await page.wait_for_selector(username_xpath, state="visible", timeout=30000)
            await page.fill(username_xpath, self.fieldedge_email)
            await page.fill(password_xpath, self.fieldedge_password)

            try:
                await page.press(password_xpath, "Enter")
            except Exception as e:
                print(f"Failed to press Enter to login: {e}")
                await page.evaluate('''([xpath]) => {
                    const result = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                    if (result.singleNodeValue) result.singleNodeValue.click();
                }''', [login_button_xpath])

            try:
                await page.wait_for_url(
                    lambda url: FIELDEDGE_LOGIN_URL not in url,
                    timeout=30000
                )
            except Exception:
                await page.wait_for_load_state("domcontentloaded", timeout=30000)

            print("FieldEdge login successful.")
            _update_fe_status("Connected / Active Session")

        except Exception as e:
            print(f"FieldEdge login failed: {e}")
            _update_fe_status(f"Login Failed ({e})")
            raise


    async def perform_actions_by_xpaths(self, name: str = '', action_list: list = None, value: str = None, raise_on_error: bool = False, page = None):
        """Execute actions on UI elements by XPath."""
        if action_list is None:
            action_list = []
            
        xpaths = self.rules.get(name, action_list)
        has_executed = False
        
        for item in xpaths:
            action = item.get("action", "")
            xpath = item.get("xpath", "")
            js_code = item.get("js_code", "")
            timeout = item.get("timeout", 2000)

            target_page = page or self.page

            if action == "wait":
                adjusted_timeout = max(timeout, 2000) if os.name != "nt" else timeout
                await asyncio.sleep(adjusted_timeout / 1000)
                continue

            if action == "js_code_run":
                code_to_run = js_code or xpath
                if not code_to_run:
                    continue
                try:
                    await target_page.evaluate(code_to_run)
                    await asyncio.sleep(0.5)
                    has_executed = True
                except Exception as e:
                    if raise_on_error:
                        raise Exception(f"Action '{action}' failed for script '{code_to_run}': {e}") from e
                continue
            
            if not xpath:
                continue
            
            element = target_page.locator(xpath)
            
            try:
                wait_timeout = item.get("timeout", 15000)
                try:
                    await target_page.evaluate("""() => {
                        const pendo = document.querySelectorAll('[id^="pendo-"], ._pendo-backdrop');
                        pendo.forEach(el => el.remove());
                    }""")
                except Exception:
                    pass

                try:
                    await target_page.wait_for_selector(xpath, state="attached", timeout=wait_timeout)
                except Exception:
                    pass

                element_count = await element.count()
                
                if element_count > 0:
                    target = element.first if element_count > 1 else element
                    
                    if action == "click":
                        try:
                            await target.click(timeout=10000)
                        except Exception:
                            await target.evaluate("el => el.click()", timeout=5000)
                    
                    elif action == "right_click":
                        try:
                            await target.click(button="right", timeout=10000)
                        except Exception:
                            await target.evaluate("""el => {
                                el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, button: 2 }));
                            }""", timeout=5000)
                    
                    elif action == "input":
                        if value is not None:
                            await target.fill(str(value))
                            
                    elif action == "vue_input":
                        if value is not None:
                            await target.evaluate("(node, val) => { node.value = val; node.dispatchEvent(new Event('input', { bubbles: true })); node.dispatchEvent(new Event('change', { bubbles: true })); }", str(value))
                            
                    await asyncio.sleep(0.5)
                    has_executed = True
            except Exception as e:
                if raise_on_error:
                    raise Exception(f"Action '{action}' failed for xpath '{xpath}': {e}") from e
        
        if not has_executed and raise_on_error:
            raise Exception(f"Element not found or all actions failed for: {name or action_list}")

    async def cleanup(self):
        """Clean up browser resources."""
        try:
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("Browser cleanup completed.")
        except Exception as e:
            print(f"Error during cleanup: {e}")

    def normalize_tech_name(self, raw: str) -> str:
        """Clean and standardize technician names."""
        if not raw:
            return "Unknown"
        cleaned = str(raw).replace('\xa0', ' ').strip()
        import re
        cleaned = re.sub(r'\)$', '', cleaned).strip()
        cleaned = re.sub(r'^T[A-Za-z]?\d+\s+', '', cleaned).strip()

        lower_cleaned = cleaned.lower()
        if any(variant in lower_cleaned for variant in ["skyler", "skylar", "skiler", "schyler"]):
            return "03 - Skyler"
        if "russell" in lower_cleaned:
            return "Russell"
            
        return cleaned