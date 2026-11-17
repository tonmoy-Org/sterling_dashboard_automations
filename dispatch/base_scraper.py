"""
Base Scraper Class (Dispatch Automation Dedicated)
Provides browser automation, FieldEdge login, and shared utilities for Dispatch Board Automations.
"""
import os
import re
import time
import json
import asyncio
import unicodedata
from datetime import datetime
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page
import pytz

# Enforce US Pacific Time (PST/PDT)
os.environ['TZ'] = 'America/Los_Angeles'
if hasattr(time, 'tzset'):
    time.tzset()

_dispatch_dir = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_dispatch_dir, '.env')
load_dotenv(dotenv_path=_env_path)

RULES_FILE_PATH = os.path.join(_dispatch_dir, "config", "scraper_rules.json")
HEADLESS_MODE = os.getenv("HEADLESS", "true").strip().lower() in ("true", "1", "t", "yes")

PST_TZ = pytz.timezone("America/Los_Angeles")

def get_pst_now():
    """Returns current datetime in US Pacific Timezone (America/Los_Angeles)."""
    return datetime.now(PST_TZ)

def get_pst_today_str(fmt="%Y-%m-%d"):
    """Returns current date string in US Pacific Timezone (America/Los_Angeles)."""
    return get_pst_now().strftime(fmt)


class BaseScraper:
    """
    Base scraper class containing shared Playwright browser controls,
    FieldEdge authentication, API communication, and core string/equipment/tech matching.
    """
    _cached_api_token: Optional[str] = None

    @staticmethod
    def get_pst_now():
        return get_pst_now()

    @staticmethod
    def get_pst_today_str(fmt="%Y-%m-%d"):
        return get_pst_today_str(fmt)

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        
        self.fieldedge_email = os.environ.get("DASH_EMAIL", "")
        self.fieldedge_password = os.environ.get("DASH_PASSWORD", "")
        self.rules = self._load_rules()
        self.scraper_name = "dispatch-base-automation"

    def _load_rules(self) -> Dict:
        """Load scraping rules from JSON configuration file."""
        potential_paths = [
            RULES_FILE_PATH,
            os.path.join(_dispatch_dir, "config", "scraper_rules.json"),
        ]
        for path in potential_paths:
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        rules_data = json.load(f)
                    return rules_data[0] if isinstance(rules_data, list) and rules_data else rules_data
                except Exception as e:
                    print(f"⚠️ Failed to load rules from {path}: {e}")
        return {}

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
            except Exception as chrome_err:
                fallback_kwargs = {k: v for k, v in launch_kwargs.items() if k != "channel"}
                fallback_args = [a for a in chrome_args if a != "--headless=new"]
                if HEADLESS_MODE:
                    fallback_args.append("--headless=new")
                fallback_kwargs["args"] = fallback_args
                self.browser = await self.playwright.chromium.launch(**fallback_kwargs)

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

            # Block Pendo tracking overlays to prevent intercepting clicks
            await self.context.route("**/*pendo*", lambda route: route.abort())
            await self.context.route("**/*app.pendo.io*", lambda route: route.abort())

            self.page = await self.context.new_page()
        except Exception as e:
            print(f"❌ Failed to initialize browser: {e}")
            raise

    async def login_fieldedge(self, page: Optional[Page] = None):
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

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    current_url = page.url
                    if FIELDEDGE_LOGIN_URL not in current_url or attempt > 0:
                        print(f"Navigating to FieldEdge login page: {FIELDEDGE_LOGIN_URL} (Attempt {attempt + 1})")
                        await page.goto(FIELDEDGE_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

                    await page.wait_for_selector(username_xpath, state="visible", timeout=30000)
                    await page.fill(username_xpath, self.fieldedge_email)
                    await page.fill(password_xpath, self.fieldedge_password)

                    try:
                        await page.press(password_xpath, "Enter")
                    except Exception:
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

                    print("✅ FieldEdge login successful.")
                    _update_fe_status("Connected / Active Session")
                    return
                except Exception as attempt_err:
                    print(f"⚠️ FieldEdge login attempt {attempt + 1} failed: {attempt_err}")
                    if attempt < max_retries - 1:
                        await asyncio.sleep(5)
                    else:
                        _update_fe_status(f"Login Failed ({attempt_err})")
                        raise attempt_err
        except Exception as e:
            print(f"FieldEdge login failed: {e}")
            _update_fe_status(f"Login Failed ({e})")
            raise

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

    # --- SHARED API & TEMPLATE HELPERS ---

    @staticmethod
    def _extract_jwt_from_response(data) -> Optional[str]:
        token_keys = ("access", "access_token", "accessToken", "token", "jwt", "id_token", "idToken", "auth_token")
        if isinstance(data, dict):
            for k in token_keys:
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            for v in data.values():
                if isinstance(v, (dict, list)):
                    found = BaseScraper._extract_jwt_from_response(v)
                    if found:
                        return found
        elif isinstance(data, list):
            for v in data:
                found = BaseScraper._extract_jwt_from_response(v)
                if found:
                    return found
        return None

    def _get_api_auth_headers(self) -> Dict[str, str]:
        static_token = os.getenv("BACKEND_API_TOKEN", "").strip()
        if static_token:
            return {"Authorization": static_token if " " in static_token else f"Bearer {static_token}"}
        if getattr(self, "_cached_api_token", None):
            return {"Authorization": f"Bearer {self._cached_api_token}"}

        email = os.getenv("BACKEND_API_EMAIL", "").strip()
        password = os.getenv("BACKEND_API_PASSWORD", "")
        api_url = os.getenv("BACKEND_API_URL", "").rstrip("/")
        if not (email and password and api_url):
            return {}

        import urllib.request, urllib.error
        base = api_url if api_url.endswith("/api") else f"{api_url}/api"
        login_path = os.getenv("BACKEND_LOGIN_PATH", "/auth/login").strip() or "/auth/login"
        endpoint = f"{base}/{login_path.lstrip('/')}"
        body = json.dumps({"email": email, "password": password}).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "SterlingAutomations/1.0"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read().decode("utf-8", errors="replace")
                token = None
                try:
                    token = self._extract_jwt_from_response(json.loads(raw))
                except Exception:
                    pass
                if not token:
                    for cookie in response.headers.get_all("Set-Cookie") or []:
                        name, _, rest = cookie.partition("=")
                        if name.strip().lower() in ("access", "access_token", "accesstoken", "token", "jwt"):
                            token = rest.split(";", 1)[0].strip()
                            break
                if token:
                    self._cached_api_token = token
                    print(f"🔐 Authenticated with Dashboard API as {email} (JWT acquired).")
                    return {"Authorization": f"Bearer {token}"}
        except Exception as e:
            print(f"⚠️ Dashboard API login error: {e}")
        return {}

    def _load_template(self, template_path: Optional[str] = None) -> Dict:
        t_path = template_path or getattr(self, "template_path", "")
        api_url = os.getenv("BACKEND_API_URL", "").rstrip("/")
        if api_url:
            try:
                import urllib.request
                endpoint = f"{api_url}/automation/dispatch-config/" if api_url.endswith('/api') else f"{api_url}/api/automation/dispatch-config/"
                headers = {"User-Agent": "SterlingAutomations/1.0"}
                headers.update(self._get_api_auth_headers())
                req = urllib.request.Request(endpoint, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status == 200:
                        remote_data = json.loads(response.read().decode('utf-8'))
                        print(f"✅ Loaded live configuration from Dashboard API: {endpoint}")
                        local_data = {}
                        if t_path and os.path.exists(t_path):
                            try:
                                with open(t_path, "r", encoding="utf-8") as f:
                                    local_data = json.load(f)
                            except Exception:
                                local_data = {}
                        for key, value in remote_data.items():
                            local_data[key] = value
                        if t_path:
                            try:
                                with open(t_path, "w", encoding="utf-8") as f:
                                    json.dump(local_data, f, indent=2, ensure_ascii=False)
                            except Exception:
                                pass
                        return local_data
            except Exception as e:
                print(f"⚠️ Could not fetch live template from API: {e}. Falling back to local file.")

        if t_path and os.path.exists(t_path):
            with open(t_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    # --- SHARED MATCHING LOGIC ---

    @staticmethod
    def canonical_str(text: str) -> str:
        """Standardize a string by stripping non-alphanumerics and font styling (NFKD)."""
        if not text:
            return ""
        norm = re.sub(r'[^a-zA-Z0-9]', '', unicodedata.normalize('NFKD', str(text))).lower()
        return norm if norm else str(text).strip().lower()

    @staticmethod
    def is_tech_match(tech1: str, tech2: str) -> bool:
        """
        Strict exact match for technicians.
        Technicians are searched using ONLY exact names from template JSON.
        No substring, fuzzy, or partial prefix matching allowed.
        """
        if not tech1 or not tech2:
            return False
        t1 = re.sub(r'[\u00d7xX]$', '', str(tech1)).strip()
        t2 = re.sub(r'[\u00d7xX]$', '', str(tech2)).strip()
        if t1.lower() == t2.lower():
            return True
        c1 = re.sub(r'[^a-zA-Z0-9]', '', unicodedata.normalize('NFKD', t1)).lower()
        c2 = re.sub(r'[^a-zA-Z0-9]', '', unicodedata.normalize('NFKD', t2)).lower()
        return bool(c1 and c2 and c1 == c2)

    @staticmethod
    def is_same_equipment(name1: str, name2: str) -> bool:
        """
        Match equipment names strictly based on category and truck numbers.
        Prevents cross-category matching between Pump Trucks, Service Trucks, and Excavators.
        """
        if not name1 or not name2:
            return False
        s1 = unicodedata.normalize('NFKD', str(name1)).strip()
        s2 = unicodedata.normalize('NFKD', str(name2)).strip()

        is_pump1 = "🚛" in s1 or any(x in s1.lower() for x in ("pump", "pt (", "pt/"))
        is_serv1 = "🛻" in s1 or any(x in s1.lower() for x in ("ser tr", "service"))
        is_exc1 = "🏗" in s1 or any(x in s1.lower() for x in ("exc", "u-17", "u-40", "u17", "u40"))
        is_generic_truck1 = "truck" in s1.lower() and not (is_pump1 or is_serv1 or is_exc1)

        is_pump2 = "🚛" in s2 or any(x in s2.lower() for x in ("pump", "pt (", "pt/"))
        is_serv2 = "🛻" in s2 or any(x in s2.lower() for x in ("ser tr", "service"))
        is_exc2 = "🏗" in s2 or any(x in s2.lower() for x in ("exc", "u-17", "u-40", "u17", "u40"))
        is_generic_truck2 = "truck" in s2.lower() and not (is_pump2 or is_serv2 or is_exc2)

        # Cross-category rejection
        if is_pump1 and (is_serv2 or is_exc2):
            return False
        if is_serv1 and (is_pump2 or is_exc2):
            return False
        if is_exc1 and (is_pump2 or is_serv2):
            return False

        c1 = re.sub(r'[^a-zA-Z0-9]', '', s1).lower()
        c2 = re.sub(r'[^a-zA-Z0-9]', '', s2).lower()
        if c1 and c2 and c1 == c2:
            return True

        m1 = re.findall(r'\b\d{2,4}\b', s1)
        m2 = re.findall(r'\b\d{2,4}\b', s2)
        if m1 and m2:
            same_number = bool(set(m1).intersection(set(m2)))
            if same_number:
                if (is_pump1 and is_pump2) or (is_serv1 and is_serv2) or (is_exc1 and is_exc2):
                    return True
                if (is_generic_truck1 and (is_pump2 or is_serv2)) or (is_generic_truck2 and (is_pump1 or is_serv1)):
                    return True
                if not (is_pump1 or is_serv1 or is_exc1) and not (is_pump2 or is_serv2 or is_exc2):
                    return True

        return False
