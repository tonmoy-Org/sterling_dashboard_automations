import os
import sys
import re
import json
import asyncio
import time
import traceback
from datetime import datetime, timedelta
import urllib.request
import urllib.error
from typing import Optional, Tuple, Dict, Any, List

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

_dispatch_dir = os.path.dirname(os.path.abspath(__file__))
if _dispatch_dir not in sys.path:
    sys.path.insert(0, _dispatch_dir)

try:
    from base_scraper import BaseScraper, get_pst_today_str, get_pst_now
except ImportError:
    try:
        from automation.scrapers.base_scraper import BaseScraper, get_pst_today_str, get_pst_now  # type: ignore
    except ImportError:
        from dispatch.base_scraper import BaseScraper, get_pst_today_str, get_pst_now  # type: ignore



class AssetAssignmentScraper(BaseScraper):

    def __init__(self, template_path: Optional[str] = None):
        super().__init__()
        self.scraper_name = "asset-assignment-automation"
        if template_path:
            self.template_path = template_path
        else:
            _curr = os.path.dirname(os.path.abspath(__file__))
            _candidate1 = os.path.join(_curr, "config", "dispatch_board_template.json")
            _candidate2 = os.path.join(os.path.dirname(_curr), "config", "dispatch_board_template.json")
            self.template_path = _candidate1 if os.path.exists(_candidate1) else _candidate2
        self.template_data = self._load_template()

    @staticmethod
    def _extract_jwt_from_response(data) -> Optional[str]:
        """Find a JWT access token in common login response shapes."""
        token_keys = ("access", "access_token", "accessToken", "token", "jwt", "id_token", "idToken", "auth_token")
        if isinstance(data, dict):
            for k in token_keys:
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            for v in data.values():
                if isinstance(v, (dict, list)):
                    found = AssetAssignmentScraper._extract_jwt_from_response(v)
                    if found:
                        return found
        elif isinstance(data, list):
            for v in data:
                found = AssetAssignmentScraper._extract_jwt_from_response(v)
                if found:
                    return found
        return None

    def _get_api_auth_headers(self) -> Dict[str, str]:
        """
        Build Authorization header for protected Dashboard API endpoints.
        Priority:
          1. BACKEND_API_TOKEN (static JWT / token from .env)
          2. Login with BACKEND_API_EMAIL + BACKEND_API_PASSWORD at BACKEND_LOGIN_PATH (default: /auth/login)
        """
        static_token = os.getenv("BACKEND_API_TOKEN", "").strip()
        if static_token:
            return {"Authorization": static_token if " " in static_token else f"Bearer {static_token}"}

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
                    print(f"🔐 Authenticated with Dashboard API as {email} (JWT acquired).")
                    return {"Authorization": f"Bearer {token}"}
                print(f"⚠️ Dashboard API login succeeded but no JWT found in response: {raw[:200]}")
        except urllib.error.HTTPError as he:
            err_body = ""
            try:
                err_body = he.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            print(f"⚠️ Dashboard API login failed ({endpoint}): HTTP {he.code} {err_body}")
        except Exception as e:
            print(f"⚠️ Dashboard API login error ({endpoint}): {e}")
        return {}

    def _load_template(self) -> Dict:
        """Load visual work order configuration template from Dashboard API or local JSON file."""
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
                        # Load local template and overwrite with live remote data (remote is source of truth)
                        local_data = {}
                        if os.path.exists(self.template_path):
                            try:
                                with open(self.template_path, "r", encoding="utf-8") as f:
                                    local_data = json.load(f)
                            except Exception:
                                local_data = {}

                        # Overwrite with live remote settings (truck assignments, techs, headers)
                        for key, value in remote_data.items():
                            local_data[key] = value

                        # Cache live data back to local template file for offline fallback
                        try:
                            with open(self.template_path, "w", encoding="utf-8") as f:
                                json.dump(local_data, f, indent=2, ensure_ascii=False)
                        except Exception:
                            pass

                        return local_data
            except Exception as e:
                print(f"⚠️ Could not fetch live template from API ({api_url}): {e}. Falling back to local template file.")

        if os.path.exists(self.template_path):
            with open(self.template_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _load_history(self) -> Dict:
        """Load persistent history from Central Dashboard API."""
        history = {}

        # Sync from Dashboard API if configured and reachable
        api_url = os.getenv("BACKEND_API_URL", "").rstrip("/")
        if api_url:
            try:
                import urllib.request
                endpoint = f"{api_url}/automation/dispatch-history/" if api_url.endswith('/api') else f"{api_url}/api/automation/dispatch-history/"
                headers = {"User-Agent": "SterlingAutomations/1.0"}
                headers.update(self._get_api_auth_headers())
                req = urllib.request.Request(endpoint, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as response:
                    if response.status == 200:
                        remote_history = json.loads(response.read().decode('utf-8'))
                        if isinstance(remote_history, dict):
                            history.update(remote_history)
            except Exception:
                pass
        return history

    def is_reconciled(self, history: Dict, date_key: str) -> bool:
        """Check if asset reconciliation was previously completed for date_key."""
        if not history or not date_key:
            return False
        key = f"{date_key}|asset_reconciliation|completed|1"
        if key in history:
            return True
        for k, v in history.items():
            if (v.get("date") == date_key or k.startswith(f"{date_key}|")) and v.get("canon_target") == "asset_reconciliation":
                return True
        return False

    def record_reconciled(self, history: Dict, date_key: str):
        """Record completed asset reconciliation for date_key in local JSON and Central Dashboard API."""
        key = f"{date_key}|asset_reconciliation|completed|1"
        rec_data = {
            "date": date_key,
            "canon_tech": "",
            "canon_target": "asset_reconciliation",
            "occurrence": 1,
            "status": "completed",
            "name": "Asset Reconciliation",
            "created_at": datetime.now().isoformat()
        }
        history[key] = rec_data

        # Sync with Dashboard API if reachable
        api_url = os.getenv("BACKEND_API_URL", "").rstrip("/")
        if api_url:
            try:
                import urllib.request
                endpoint = f"{api_url}/automation/dispatch-history/" if api_url.endswith('/api') else f"{api_url}/api/automation/dispatch-history/"
                headers = {"Content-Type": "application/json", "User-Agent": "SterlingAutomations/1.0"}
                headers.update(self._get_api_auth_headers())
                req = urllib.request.Request(endpoint, data=json.dumps(rec_data).encode('utf-8'), headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=5) as response:
                    pass
            except Exception:
                pass

    def format_date(self, date_str: str) -> str:
        if not date_str:
            return ""
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%m/%d/%y", "%m-%d-%y"):
            try:
                return datetime.strptime(date_str.strip(), fmt).strftime("%m/%d/%Y")
            except ValueError:
                continue
        return date_str

    async def select_status(self, status_name: str = "Assigned") -> bool:
        try:
            status_locators = []
            if status_name.lower() == "assigned" and self.rules.get('status_assigned_xpath'):
                status_locators.append(self.rules.get('status_assigned_xpath'))
            status_locators.extend([
                f'.cell-pos-1x3 div.circle-btn[title="{status_name}"]',
                f'.cell-pos-1x3 .circle-btn[title="{status_name}"]',
                f'//div[contains(@class, "cell-pos-1x3")]//div[contains(@class, "circle-btn") and (@title="{status_name}" or contains(@title, "{status_name}"))]',
                f'//div[contains(@class, "cell-pos-1x3")]//div[contains(@class, "circle-btn") and .//span[normalize-space()="A"]]',
                f'div.circle-btn[title="{status_name}"]',
                f'.circle-btn[title="{status_name}"]',
                f'*[title="{status_name}"]',
                f'//div[contains(@class, "circle-btn") and @title="{status_name}"]',
                f'//div[contains(@class, "circle-btn") and contains(@title, "{status_name}")]',
                f'//div[contains(@class, "circle-btn") and .//span[normalize-space()="A"]]',
                f'button[title="{status_name}"]',
                f'button:has-text("{status_name}")',
                f'//button[normalize-space()="{status_name}"]',
                f'//button[contains(normalize-space(), "{status_name}")]',
                f'//div[contains(@class, "filter") or contains(@class, "status") or contains(@class, "button-grid")]//*[contains(@title, "{status_name}")]',
                f'//span[normalize-space()="{status_name}"]/ancestor::*[contains(@class, "circle-btn") or self::button]',
                f'//*[contains(@class, "filter") or contains(@class, "button") or contains(@class, "circle-btn")][normalize-space()="{status_name}"]',
            ])
            status_locators = [c.strip() for c in status_locators if c and isinstance(c, str) and c.strip()]

            for selector in status_locators:
                try:
                    loc = self.page.locator(selector).first
                    await loc.wait_for(state="attached", timeout=3000)

                    btn_class = (await loc.get_attribute("class")) or ""
                    if "selected" in btn_class.split():
                        print(f"Status '{status_name}' is already active/selected ({selector}).")
                        return True

                    try:
                        await loc.click()
                    except Exception:
                        await loc.evaluate("el => el.click()")

                    print(f"Selected status '{status_name}' using selector: {selector}")
                    await self.page.wait_for_timeout(1000)
                    return True

                except Exception:
                    continue

            print(f"⚠️ Status button '{status_name}' not found.")
            return False

        except Exception as e:
            print(f"❌ Error selecting status '{status_name}': {e}")
            return False

    async def set_date_filter(self, start_date: str, end_date: str) -> bool:
        try:
            start_date = self.format_date(start_date)
            end_date = self.format_date(end_date)

            date_filter_locators = [
                self.rules.get('date_filter_dropdown_xpath'),
                '//div[contains(@class, "filter-dropdown")][.//span[normalize-space(text())="Date"]]',
                '//span[normalize-space(text())="Date"]/ancestor::div[contains(@class, "filter-dropdown")]',
                'div.filter-dropdown:has(span.filter-text:has-text("Date"))',
                'div.filter-dropdown:has(.time-filter) div.filter-text',
                'div.filter-dropdown div.filter-text',
                'div.date-filter',
                'div.time-filter',
                '//div[contains(@class, "date-filter")]',
                '//div[contains(@class, "time-filter")]'
            ]
            date_filter_locators = [c.strip() for c in date_filter_locators if c and isinstance(c, str) and c.strip()]

            opened = False
            date_dropdown_el = None
            for d_loc in date_filter_locators:
                try:
                    loc = self.page.locator(d_loc).first
                    await loc.wait_for(state="attached", timeout=3000)
                    try:
                        await loc.click()
                    except Exception:
                        await loc.evaluate("el => el.click()")
                    print(f"Opened date filter dropdown using: {d_loc}")
                    opened = True
                    date_dropdown_el = loc
                    await self.page.wait_for_timeout(2000)
                    break
                except Exception:
                    continue

            if not opened:
                print("⚠️ Date filter dropdown element not found; attempting direct date input fields...")

            start_locators = [
                self.rules.get('date_start_input_xpath'),
                '.cell-pos-1x1 input[placeholder="MM/DD/YYYY"]',
                '.cell-pos-1x1 input.base-input',
                '.cell-pos-1x1 input',
                'div.calander-wrapper_lFYlm input[placeholder="MM/DD/YYYY"]',
                '#start-date-filter',
                'input[name="startDate"]',
                'input.start-date-input'
            ]
            start_locators = [c.strip() for c in start_locators if c and isinstance(c, str) and c.strip()]

            start_filled = False
            for s_loc in start_locators:
                try:
                    inp = self.page.locator(s_loc).first
                    await inp.wait_for(state="attached", timeout=2500)
                    await inp.click()
                    await self.page.wait_for_timeout(300)
                    await inp.press("Control+A")
                    await inp.press("Backspace")
                    await inp.type(start_date, delay=50)
                    await inp.evaluate(
                        "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                        start_date
                    )
                    await inp.press("Enter")
                    print(f"Filled start date '{start_date}' using: {s_loc}")
                    start_filled = True
                    await self.page.wait_for_timeout(500)
                    break
                except Exception:
                    continue

            if not start_filled:
                try:
                    all_mmdd_inputs = self.page.locator('input[placeholder="MM/DD/YYYY"]')
                    if await all_mmdd_inputs.count() >= 1:
                        inp = all_mmdd_inputs.nth(0)
                        await inp.click()
                        await inp.press("Control+A")
                        await inp.press("Backspace")
                        await inp.type(start_date, delay=50)
                        await inp.evaluate(
                            "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                            start_date
                        )
                        await inp.press("Enter")
                        print(f"Filled start date '{start_date}' using positional input[placeholder='MM/DD/YYYY']")
                except Exception as e:
                    print(f"Positional start date fallback error: {e}")

            end_locators = [
                self.rules.get('date_end_input_xpath'),
                '.cell-pos-1x3 input[placeholder="MM/DD/YYYY"]',
                '.cell-pos-1x3 input.base-input',
                '.cell-pos-1x3 input',
                '#end-date-filter',
                'input[name="endDate"]',
                'input.end-date-input'
            ]
            end_locators = [c.strip() for c in end_locators if c and isinstance(c, str) and c.strip()]

            end_filled = False
            for e_loc in end_locators:
                try:
                    inp = self.page.locator(e_loc).first
                    await inp.wait_for(state="attached", timeout=2500)
                    await inp.click()
                    await self.page.wait_for_timeout(300)
                    await inp.press("Control+A")
                    await inp.press("Backspace")
                    await inp.type(end_date, delay=50)
                    await inp.evaluate(
                        "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                        end_date
                    )
                    await inp.press("Enter")
                    print(f"Filled end date '{end_date}' using: {e_loc}")
                    end_filled = True
                    await self.page.wait_for_timeout(500)
                    break
                except Exception:
                    continue

            if not end_filled:
                try:
                    all_mmdd_inputs = self.page.locator('input[placeholder="MM/DD/YYYY"]')
                    if await all_mmdd_inputs.count() >= 2:
                        inp = all_mmdd_inputs.nth(1)
                        await inp.click()
                        await inp.press("Control+A")
                        await inp.press("Backspace")
                        await inp.type(end_date, delay=50)
                        await inp.evaluate(
                            "(el, val) => { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }",
                            end_date
                        )
                        await inp.press("Enter")
                        print(f"Filled end date '{end_date}' using positional input[placeholder='MM/DD/YYYY'] index 1")
                except Exception as e:
                    print(f"Positional end date fallback error: {e}")

            select_locators = [
                self.rules.get('date_select_button_xpath'),
                "//button[not(@disabled) and .//span[normalize-space()='Select']]",
                "//button[not(@disabled) and normalize-space()='Select']",
                "button:has-text('Select')",
                "//button[contains(@class, 'confirm') or contains(., 'Select')]"
            ]
            select_locators = [c.strip() for c in select_locators if c and isinstance(c, str) and c.strip()]

            for sel_loc in select_locators:
                try:
                    select_btn = self.page.locator(sel_loc).first
                    await select_btn.wait_for(state="attached", timeout=2500)
                    try:
                        await select_btn.click()
                    except Exception:
                        await select_btn.evaluate("el => el.click()")
                    print(f"Clicked Select button in date filter using: {sel_loc}")
                    await self.page.wait_for_timeout(2000)
                    break
                except Exception:
                    continue

            if opened and date_dropdown_el:
                try:
                    await date_dropdown_el.click()
                    await self.page.wait_for_timeout(1000)
                except Exception:
                    pass

            print(f"✅ Date filter set: {start_date} to {end_date}")
            return True

        except Exception as e:
            print(f"❌ Error setting date filter: {e}")
            return False

    async def apply_filters(self) -> bool:
        try:
            try:
                await self.page.evaluate("""() => {
                    const pendo = document.querySelectorAll('[id^="pendo-"], ._pendo-backdrop');
                    pendo.forEach(el => el.remove());
                }""")
            except Exception:
                pass

            apply_locators = [
                self.rules.get('apply_button_xpath'),
                '//button[contains(@class, "filter-button") and contains(., "Apply")]',
                '//button[contains(@class, "confirm") and contains(., "Apply")]',
                '//button[contains(@class, "plot-map-button")]',
                '//button[normalize-space()="Apply"]',
                'button.filter-button:has-text("Apply")',
                'button.confirm_hsagT:has-text("Apply")',
                'button:has-text("Apply")',
                '.plot-map-button:has-text("Apply")',
                'button.plot-map-button',
                '//button[@data-automation-id="apply-filters-button"]'
            ]
            apply_locators = [c.strip() for c in apply_locators if c and isinstance(c, str) and c.strip()]

            for app_loc in apply_locators:
                try:
                    btn = self.page.locator(app_loc).first
                    await btn.wait_for(state="attached", timeout=3000)

                    try:
                        is_disabled = await btn.get_attribute("disabled")
                        if is_disabled is not None:
                            print("Apply button is currently disabled; waiting 2s for filter changes...")
                            await self.page.wait_for_timeout(2000)
                            await btn.evaluate("el => el.removeAttribute('disabled')")
                    except Exception:
                        pass

                    try:
                        await btn.click(force=True)
                    except Exception:
                        await btn.evaluate("el => el.click()")

                    print(f"✅ Filters applied using locator: {app_loc}")
                    await self.page.wait_for_timeout(4000)
                    return True
                except Exception:
                    continue

            print("⚠️ Apply button not found.")
            return False

        except Exception as e:
            print(f"❌ Error applying filters: {e}")
            return False

    async def _scroll_table_fully(self) -> None:
        """Scroll all known FieldEdge viewport selectors slowly to force virtual-render and full load."""
        selectors = [
            "tbody.fixed-body",
            ".fixed-body-container",
            ".kgViewport",
            ".grid-viewport",
            ".ui-grid-viewport",
            ".list-table-scroll-container",
            ".scroll-container",
            ".p-datatable-wrapper",
            ".p-datatable-scrollable-body",
        ]
        for sel in selectors:
            try:
                count = await self.page.evaluate(f"() => document.querySelectorAll('{sel}').length")
                if count == 0:
                    continue
                
                for idx in range(count):
                    is_scrollable = await self.page.evaluate(
                        f"() => {{"
                        f"  const el = document.querySelectorAll('{sel}')[{idx}];"
                        f"  return el && el.scrollHeight > el.clientHeight;"
                        f"}}"
                    )
                    if not is_scrollable:
                        continue
                        
                    print(f"DEBUG: Slowly scrolling '{sel}' (index {idx}) to load all data...")
                    prev_scroll = -1
                    stuck_count = 0
                    for _ in range(1000):
                        scroll_top = await self.page.evaluate(
                            f"() => {{"
                            f"  const el = document.querySelectorAll('{sel}')[{idx}];"
                            f"  if (el) {{"
                            f"    el.scrollTop += 400;"
                            f"    el.dispatchEvent(new Event('scroll'));"
                            f"    return el.scrollTop;"
                            f"  }}"
                            f"  return -1;"
                            f"}}"
                        )
                        await self.page.wait_for_timeout(2000) # wait for data to load
                        if scroll_top == prev_scroll:
                            stuck_count += 1
                            if stuck_count >= 15:
                                break
                        else:
                            stuck_count = 0
                        prev_scroll = scroll_top
                        
                    await self.page.evaluate(
                        f"() => {{"
                        f"  const el = document.querySelectorAll('{sel}')[{idx}];"
                        f"  if (el) {{"
                        f"    el.scrollTop = 0;"
                        f"    el.dispatchEvent(new Event('scroll'));"
                        f"  }}"
                        f"}}"
                    )
                    await self.page.wait_for_timeout(2000)
                    print(f"DEBUG: Scrolled '{sel}' (index {idx}) fully.")
            except Exception as exc:
                print(f"DEBUG: Scroll error '{sel}': {exc}")

    async def scrape_asset_assignments(self) -> Dict[str, Any]:
        print("⏳ Waiting for Asset Assignment table rows to load...")
        try:
            await self.page.wait_for_selector(
                "tbody.p-datatable-tbody tr, tr.p-selectable-row, .p-datatable-table",
                state="attached",
                timeout=15000
            )
            await self.page.wait_for_timeout(2000)
        except Exception as e:
            print(f"⚠️ Timeout waiting for table rows: {e}")
            return {"rows": [], "count": 0}

        # Aggressively scroll to load all lazy-rendered rows before reading DOM
        await self._scroll_table_fully()

        try:
            scraped_data = await self.page.evaluate(r"""() => {
                const rows = [];
                const cleanText = (str) => (str || '').replace(/\s+/g, ' ').trim();

                const domRows = document.querySelectorAll(
                    'tbody.p-datatable-tbody > tr.p-selectable-row, tbody.p-datatable-tbody > tr, tr.p-selectable-row'
                );

                domRows.forEach((row) => {
                    try {
                        const cells = row.querySelectorAll('td');
                        if (!cells || cells.length < 10) return;

                        // Col 0: Priority Color
                        const colorEl = cells[0].querySelector('.priorityColor') || cells[0].querySelector('[style*="background-color"]');
                        const priorityColor = colorEl ? colorEl.style.backgroundColor : '';
                        const colorTitle = colorEl ? (colorEl.getAttribute('title') || '') : '';

                        // Col 1: Priority Name
                        const priorityName = cleanText(cells[1].textContent) || colorTitle;

                        // Col 2: WO #
                        const woEl = cells[2].querySelector('.dispatch-name') || cells[2];
                        const workOrderNumber = cleanText(woEl.getAttribute('title') || woEl.textContent);
                        if (!workOrderNumber) return;

                        // Col 3: Customer / Asset Name (e.g. 🚛 | 123, 🛻 | 03, PTO ( LATE ), etc.)
                        const customerName = cleanText(cells[3].textContent);

                        // Col 4: Customer Address
                        const customerAddress = cleanText(cells[4].textContent);

                        // Col 5: Tags
                        const tagsEl = cells[5].querySelector('.tag-list-container') || cells[5];
                        const tags = cleanText(tagsEl.textContent);

                        // Col 6: Tech Name (e.g. T91, s4, h 𝐃𝐚𝐧𝐧𝐲, l 𝐂𝐫𝐢𝐬, etc.)
                        const techName = cleanText(cells[6].textContent);

                        // Col 7: Promised Appointment
                        const promisedAppointment = cells.length > 7 ? cleanText(cells[7].textContent) : '';

                        // Col 8: Created Date
                        const createdDate = cells.length > 8 ? cleanText(cells[8].textContent) : '';

                        // Col 9: Scheduled Date (e.g. 10/14/2026 5:00 AM - 6:00 AM)
                        let scheduledDate = '';
                        if (cells.length > 9) {
                            const schedEl = cells[9].querySelector('.date-assignment') || cells[9];
                            scheduledDate = cleanText(schedEl.getAttribute('title')) || cleanText(schedEl.textContent);
                        }

                        // Col 10: Task & Duration
                        let taskName = '';
                        let taskDuration = '';
                        if (cells.length > 10) {
                            const taskEl = cells[10].querySelector('.task-name');
                            const durEl = cells[10].querySelector('.task-duration');
                            taskName = taskEl ? cleanText(taskEl.getAttribute('title') || taskEl.textContent) : '';
                            taskDuration = durEl ? cleanText(durEl.textContent) : '';
                            if (!taskName) {
                                taskName = cleanText(cells[10].textContent);
                            }
                        }

                        // Col 11: Purchase Status
                        const purchaseStatus = cells.length > 11 ? cleanText(cells[11].textContent) : '';

                        // Asset / Vehicle metadata parsing
                        let assetName = '';
                        let isAssetAssignment = false;
                        if (customerName.includes('|')) {
                            const parts = customerName.split('|');
                            if (parts.length > 1) {
                                assetName = cleanText(parts[1]);
                            }
                        }
                        if (
                            priorityName.toLowerCase().includes('asset assignment') ||
                            customerName.includes('🚛') ||
                            customerName.includes('🛻') ||
                            priorityName.includes('PT (') ||
                            priorityName.includes('SER TR')
                        ) {
                            isAssetAssignment = true;
                        }

                        const rowFullText = cleanText(row.textContent || '').toLowerCase();
                        const rowClass = (row.className || '').toLowerCase();
                        const isCompleted = rowFullText.includes('completed') || 
                                            rowClass.includes('completed') || 
                                            Boolean(row.querySelector('.completed, [title*="Completed" i], [aria-label*="Completed" i], .status-completed'));

                        rows.push({
                            priority_color: priorityColor,
                            priority_name: priorityName,
                            work_order_number: workOrderNumber,
                            customer_name: customerName,
                            customer_address: customerAddress,
                            asset_name: assetName,
                            is_asset_assignment: isAssetAssignment,
                            tags: tags,
                            tech_name: techName,
                            promised_appointment: promisedAppointment,
                            created_date: createdDate,
                            scheduled_date: scheduledDate,
                            task_name: taskName,
                            task_duration: taskDuration,
                            purchase_status: purchaseStatus,
                            is_completed: isCompleted
                        });
                    } catch (rowErr) {
                        // ignore broken row
                    }
                });

                return { rows: rows, count: rows.length };
            }""");

            rows = scraped_data.get("rows", [])
            count = len(rows)
            print(f"✅ Successfully scraped {count} total rows from the table.")

            truck_assign_headers = self.filter_truck_assign_headers(rows)
            print(f"🎯 Extracted {len(truck_assign_headers)} active truck_assign_headers.")

            result_payload = {
                "truck_assign_headers": truck_assign_headers,
                "all_rows": rows,
                "count": len(truck_assign_headers),
                "total_rows_scraped": count
            }

            if len(truck_assign_headers) > 0:
                try:
                    # Ensure legacy file in scrapers/ directory is cleaned up
                    legacy_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraped_asset_assignments.json")
                    if os.path.exists(legacy_file):
                        try:
                            os.remove(legacy_file)
                        except Exception:
                            pass

                    _curr_dir = os.path.dirname(os.path.abspath(__file__))
                    _cand_cfg1 = os.path.join(_curr_dir, "config")
                    _cand_cfg2 = os.path.join(os.path.dirname(_curr_dir), "config")
                    config_dir = _cand_cfg1 if os.path.exists(_cand_cfg1) else _cand_cfg2
                    os.makedirs(config_dir, exist_ok=True)
                    output_file = os.path.join(config_dir, "scraped_asset_assignments.json")
                    with open(output_file, "w", encoding="utf-8") as f:
                        json.dump(result_payload, f, indent=2, ensure_ascii=False)
                    print(f"💾 Saved {len(truck_assign_headers)} truck_assign_headers to: {output_file}")
                except Exception as save_err:
                    print(f"⚠️ Could not write local output file: {save_err}")

            return result_payload

        except Exception as e:
            print(f"❌ Error scraping table rows: {e}")
            return {"truck_assign_headers": [], "count": 0, "error": str(e)}

    def filter_truck_assign_headers(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        import re
        truck_headers = []

        # Load ALL truck headers (both active and inactive) from template configuration
        all_template_trucks = self.template_data.get("truck_assign_headers", [])

        # Build lookup maps for known fleet trucks (exact canonical string -> config, truck digits -> config)
        expected_truck_map = {}
        for h in all_template_trucks:
            t_name = (h.get("name") or "").strip()
            if t_name:
                c_name = self.canonical_str(t_name)
                expected_truck_map[c_name] = h
                num_m = re.findall(r'\d+', t_name)
                if num_m:
                    num_str = num_m[0]
                    expected_truck_map[num_str] = h
                    expected_truck_map[str(int(num_str))] = h

        active_techs_list = [h.get('tech_name') for h in all_template_trucks if h.get('active', True) and h.get('tech_name')]
        print(f"📋 Loaded {len(all_template_trucks)} fleet truck configs from template (Active techs: {active_techs_list}).")

        for r in rows:
            cust = r.get("customer_name", "").strip()
            prio = r.get("priority_name", "").strip()
            tech = r.get("tech_name", "").strip()
            wo_num = r.get("work_order_number", "").strip()

            # Keep s4, s3, and empty tech exclusion
            if tech.lower() in ["s4", "s3", ""]:
                continue

            # CRITICAL: Helper/Laborer/Crew assignments (👥 | ...) are personnel, NOT fleet trucks!
            # They must NEVER be treated as fleet trucks, canceled, or edited by truck reconciliation!
            if "👥" in cust or "laborer" in cust.lower() or "helper" in cust.lower() or "crew" in cust.lower():
                continue

            # CRITICAL: Must be a genuine fleet truck assignment work order!
            c_cust = self.canonical_str(cust)
            is_truck_wo = (
                ("🚛" in cust or "🛻" in cust) and
                (
                    "asset assignment" in prio.lower() or
                    "ser tr" in prio.lower() or
                    "pt (" in prio.lower() or
                    c_cust in expected_truck_map
                )
            )
            if not is_truck_wo:
                if c_cust in expected_truck_map:
                    is_truck_wo = True
                elif "|" in cust and ("🚛" in cust or "🛻" in cust):
                    after_pipe = cust.split("|")[1].strip()
                    pipe_nums = re.findall(r'\d+', after_pipe)
                    if pipe_nums and (pipe_nums[0] in expected_truck_map or str(int(pipe_nums[0])) in expected_truck_map):
                        is_truck_wo = True

            if not is_truck_wo:
                continue

            c_cust = self.canonical_str(cust)

            matched_cfg = None

            # 1. Exact canonical name match against known fleet trucks
            if c_cust in expected_truck_map:
                matched_cfg = expected_truck_map[c_cust]
            elif "|" in cust:
                # Only check digits strictly from the truck number section after the pipe '|' (e.g. '01' from '🛻 | 01')
                after_pipe = cust.split("|")[1].strip()
                pipe_nums = re.findall(r'\d+', after_pipe)
                if pipe_nums:
                    for pn in (pipe_nums[0], str(int(pipe_nums[0]))):
                        if pn in expected_truck_map:
                            matched_cfg = expected_truck_map[pn]
                            break

            # 2. General truck indicators fallback (for ad-hoc or unlisted truck work orders)
            if not matched_cfg:
                matched_cfg = {
                    "name": cust,
                    "tech_name": "",
                    "priority": prio
                }

            if matched_cfg:
                dept = matched_cfg.get("department") or ("pump" if ("🚛" in cust or "PT" in prio) else "service")
                canonical_truck_name = matched_cfg.get("name") or cust

                start_time = matched_cfg.get("start_time") or "06:00 AM"
                duration = matched_cfg.get("duration") or "1:00"
                sched_str = r.get("scheduled_date", "")
                m = re.search(r"(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)", sched_str, re.IGNORECASE)
                if m:
                    st = m.group(1).upper()
                    if len(st.split(":")[0]) == 1:
                        st = "0" + st
                    start_time = st
                m_dur = re.search(r"\((\d+)\s*H\)", r.get("task_duration", ""), re.IGNORECASE)
                if m_dur:
                    duration = f"{m_dur.group(1)}:00"

                target_tech = matched_cfg.get("tech_name") or "None (Unassigned/Inactive)"
                truck_headers.append({
                    "name": canonical_truck_name,
                    "active": matched_cfg.get("active", True),
                    "department": dept,
                    "priority": prio or matched_cfg.get("priority", ""),
                    "start_time": start_time,
                    "duration": duration,
                    "tech_name": tech,
                    "work_order_number": wo_num,
                    "scheduled_date": sched_str,
                    "asset_name": r.get("asset_name") or matched_cfg.get("name", ""),
                    "is_completed": r.get("is_completed", False)
                })
                print(f"  🎯 Matched truck WO #{wo_num}: '{canonical_truck_name}' (Current Tech: '{tech}', Target Tech: '{target_tech}', Active: {matched_cfg.get('active', True)})")

        return truck_headers

    @staticmethod
    def canonical_str(t: str) -> str:
        if not t:
            return ""
        import unicodedata, re
        norm = re.sub(r'[^a-zA-Z0-9]', '', unicodedata.normalize('NFKD', str(t))).lower()
        if not norm:
            norm = str(t).strip().lower()
        return norm

    @staticmethod
    def is_tech_match(tech1: str, tech2: str) -> bool:
        if not tech1 or not tech2:
            return False
        import unicodedata, re
        c1 = re.sub(r'[^a-zA-Z0-9]', '', unicodedata.normalize('NFKD', str(tech1))).lower()
        c2 = re.sub(r'[^a-zA-Z0-9]', '', unicodedata.normalize('NFKD', str(tech2))).lower()
        if not c1 or not c2:
            return False
        # Exact match
        if c1 == c2:
            return True
        # Extract letters/words of length >= 3 (to avoid 1-letter sort prefixes like 'f', 'k', 'd' causing false substring matches)
        words1 = [w for w in re.findall(r'[a-zA-Z0-9]+', unicodedata.normalize('NFKD', str(tech1)).lower()) if len(w) >= 3]
        words2 = [w for w in re.findall(r'[a-zA-Z0-9]+', unicodedata.normalize('NFKD', str(tech2)).lower()) if len(w) >= 3]

        for w1 in words1:
            for w2 in words2:
                if w1 == w2 or (len(w1) >= 4 and w1 in w2) or (len(w2) >= 4 and w2 in w1):
                    return True

        if len(c1) >= 4 and len(c2) >= 4:
            if c1 in c2 or c2 in c1:
                return True

        return False

    def get_expected_tech_for_truck(self, truck_name: str) -> Optional[str]:
        c_target = self.canonical_str(truck_name)
        nums_target = re.findall(r'\d+', truck_name) if truck_name else []
        for h in self.template_data.get("truck_assign_headers", []):
            if not h.get("active", True):
                continue
            h_name = h.get("name", "")
            c_hname = self.canonical_str(h_name)
            if c_hname == c_target:
                return h.get("tech_name")
            nums_h = re.findall(r'\d+', h_name) if h_name else []
            if nums_target and nums_h and int(nums_target[0]) == int(nums_h[0]):
                return h.get("tech_name")
        return None

    def get_expected_truck_for_tech(self, tech_name: str) -> Optional[Dict[str, Any]]:
        for h in self.template_data.get("truck_assign_headers", []):
            if not h.get("active", True):
                continue
            h_tech = h.get("tech_name", "")
            if self.is_tech_match(h_tech, tech_name):
                return h
        return None

    def is_item_turned_off_in_dashboard(self, item_name: str, tech_name: str = "") -> bool:
        """
        Check if an asset/truck/header/tech is marked active: False in Dashboard template data.
        Returns True if:
        1. In truck_assign_headers, matching truck has active: False
        2. In static_headers, matching header has active: False
        3. The assigned technician in active_techs has active: False
        """
        c_item = self.canonical_str(item_name)
        nums_item = re.findall(r'\d+', item_name) if item_name else []

        # 1. Check truck_assign_headers
        for h in self.template_data.get("truck_assign_headers", []):
            h_name = h.get("name", "")
            c_hname = self.canonical_str(h_name)
            nums_h = re.findall(r'\d+', h_name) if h_name else []
            matches = (c_hname == c_item) or (nums_item and nums_h and int(nums_item[0]) == int(nums_h[0]))
            if matches:
                if h.get("active") is False:
                    return True
                # Also check if its designated tech is inactive in active_techs
                h_tech = h.get("tech_name")
                if h_tech:
                    for t in self.template_data.get("active_techs", []):
                        if self.is_tech_match(t.get("tech_name") or t.get("name"), h_tech):
                            if t.get("active") is False:
                                return True
                return False

        # 2. Check static_headers
        for sh in self.template_data.get("static_headers", []):
            sh_name = sh.get("name", "")
            if self.canonical_str(sh_name) == c_item:
                if sh.get("active") is False:
                    return True
                return False

        # 3. Check if tech_name itself is inactive in active_techs
        if tech_name:
            for t in self.template_data.get("active_techs", []):
                if self.is_tech_match(t.get("tech_name") or t.get("name"), tech_name):
                    if t.get("active") is False:
                        return True

        return False

    async def close_sidepanel(self):
        try:
            close_clicked = await self.page.evaluate("""() => {
                const btn = document.querySelector('.panel-close_VNOfG .close-icon') ||
                            document.querySelector('.panel-close_VNOfG') ||
                            document.querySelector('.base-icon.close-icon') ||
                            document.querySelector('button.close');
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            }""")
            if not close_clicked:
                await self.page.keyboard.press("Escape")
        except Exception:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
        await self.page.wait_for_timeout(1500)

    async def select_dropdown_option(self, dropdown_locator_candidates: Any, option_text: str) -> bool:
        if not option_text:
            return False

        import unicodedata, re
        clean_text = unicodedata.normalize('NFKD', option_text).strip()
        target_canon = self.canonical_str(option_text)
        m_core = re.sub(r'^[a-z0-9]\s*', '', clean_text.lower())
        core_tech = m_core if len(m_core) >= 3 else clean_text.lower()

        print(f"  🔍 Selecting tech '{option_text}' (normalized: '{clean_text}') in Assignment card dropdown...")

        try:
            await self.page.evaluate("""() => {
                const dd = document.querySelector('.assigned-tech_omVmC .tech-dropdown_qufHC') ||
                           document.querySelector('.assigned-tech_omVmC .v-select') ||
                           document.querySelector('[data-edb-sidepanel-card*="Assignment"] .v-select');
                if (dd) {
                    dd.scrollIntoView({ behavior: 'instant', block: 'center' });
                }
            }""")
            await self.page.wait_for_timeout(400)

            toggle_opened = await self.page.evaluate("""() => {
                const dd = document.querySelector('.assigned-tech_omVmC .tech-dropdown_qufHC') ||
                           document.querySelector('.assigned-tech_omVmC .v-select') ||
                           document.querySelector('[data-edb-sidepanel-card*="Assignment"] .v-select');
                if (!dd) return false;
                const toggle = dd.querySelector('.vs__dropdown-toggle') || 
                               dd.querySelector('input.vs__search') || 
                               dd.querySelector('input') || 
                               dd;
                toggle.click();
                toggle.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                toggle.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                return true;
            }""")
            await self.page.wait_for_timeout(600)

            await self.page.evaluate("""(args) => {
                const { text, clean } = args;
                const input = document.querySelector('.assigned-tech_omVmC input.vs__search') || 
                              document.querySelector('.tech-dropdown_qufHC input') ||
                              document.querySelector('.assigned-tech_omVmC input') ||
                              document.querySelector('[data-edb-sidepanel-card*="Assignment"] input.vs__search');
                if (input) {
                    input.focus();
                    input.value = clean || text;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""", {"text": option_text, "clean": clean_text})
            await self.page.wait_for_timeout(600)

            clicked = await self.page.evaluate("""(args) => {
                const { targetText, targetCanon, coreTech } = args;
                const norm = (s) => (s || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g, '');

                // Dropdown menu options can be inside .assigned-tech_omVmC, in body, or in .vs__dropdown-menu
                const items = Array.from(document.querySelectorAll('.assigned-tech_omVmC ul[role="listbox"] li, .tech-dropdown_qufHC ul li, .vs__dropdown-menu li, ul[role="listbox"] li, li.vs__dropdown-option'));
                if (!items.length) return false;

                const cTarget = norm(targetCanon);
                const cCore = norm(coreTech);

                // Priority 1: Exact or canonical match
                for (const item of items) {
                    const txt = (item.textContent || item.innerText || '').trim();
                    const cTxt = norm(txt);
                    if (cTxt === cTarget || (cTarget && cTxt === cTarget)) {
                        item.scrollIntoView({ behavior: 'instant', block: 'nearest' });
                        item.click();
                        item.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                        item.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                        item.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        return true;
                    }
                }

                // Priority 2: Core technician name match (e.g. 'skyler' in 'f 𝐒𝐤𝐲𝐥𝐞𝐫')
                if (cCore && cCore.length >= 3) {
                    for (const item of items) {
                        const txt = (item.textContent || item.innerText || '').trim();
                        const cTxt = norm(txt);
                        if (cTxt.includes(cCore)) {
                            item.scrollIntoView({ behavior: 'instant', block: 'nearest' });
                            item.click();
                            item.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                            item.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                            item.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                            return true;
                        }
                    }
                }

                // Priority 3: Substring / text inclusion (strictly minimum 4 characters)
                for (const item of items) {
                    const txt = (item.textContent || item.innerText || '').toLowerCase().trim();
                    const normTxt = norm(txt);
                    if (cTarget && cTarget.length >= 4 && normTxt.length >= 4 && (normTxt.includes(cTarget) || cTarget.includes(normTxt))) {
                        item.scrollIntoView({ behavior: 'instant', block: 'nearest' });
                        item.click();
                        item.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                        item.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                        item.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        return true;
                    }
                }

                // SAFE: Never blindly select items[0]!
                return false;
            }""", {"targetText": option_text, "targetCanon": target_canon, "coreTech": core_tech})

            print(f"  -> Tech dropdown selection for '{option_text}': {'Success' if clicked else 'Failed'}")
            await self.page.wait_for_timeout(600)
            return clicked
        except Exception as e:
            print(f"⚠️ Error selecting '{option_text}' in dropdown: {e}")
            return False

    async def click_and_open_sidepanel(self, work_order_number: str, tech_name: str = "") -> bool:
        clean_target_wo = re.sub(r'[^0-9]', '', str(work_order_number))
        clean_target_tech = re.sub(r'[^a-z0-9]', '', tech_name.lower()) if tech_name else ""
        try:
            # 1. Force close any currently open sidepanel so we start clean
            is_panel_open = await self.page.evaluate("""() => {
                const p = document.querySelector('.container__Gkig, .panel-header-container_EYC6s');
                return !!p && p.offsetParent !== null;
            }""")
            if is_panel_open:
                await self.close_sidepanel()
                await self.page.wait_for_timeout(600)

            # Scroll table container to top first
            await self.page.evaluate("""() => {
                const c = document.querySelector('.p-datatable-wrapper') ||
                          document.querySelector('.p-datatable-scrollable-body') ||
                          document.querySelector('.custom-scrollbar');
                if (c) c.scrollTop = 0;
            }""")
            await self.page.wait_for_timeout(300)

            # 2. Find and click the exact table row matching work_order_number (and tech_name if given)
            clicked = False
            for scroll_step in range(12):
                click_result = await self.page.evaluate(r"""(args) => {
                    const targetWo = args.targetWo;
                    const targetTech = args.targetTech;

                    const domRows = Array.from(document.querySelectorAll(
                        'tbody.p-datatable-tbody > tr.p-selectable-row, tbody.p-datatable-tbody > tr, tr.p-selectable-row'
                    ));

                    for (const row of domRows) {
                        const cells = row.querySelectorAll('td');
                        if (!cells || cells.length < 3) continue;

                        let matchedCell = null;
                        let rowWoClean = '';
                        for (let c = 1; c <= 2 && c < cells.length; c++) {
                            const woEl = cells[c].querySelector('.dispatch-name') || cells[c];
                            const raw = (woEl.getAttribute('title') || woEl.textContent || '').replace(/[^0-9]/g, '');
                            if (targetWo && raw === targetWo) {
                                rowWoClean = raw;
                                matchedCell = woEl;
                                break;
                            }
                        }

                        if (targetWo && !rowWoClean) continue;

                        // Check tech if specified (cell 6 in FieldEdge table)
                        if (targetTech && cells.length > 6) {
                            const rawTech = (cells[6].textContent || '').toLowerCase().replace(/[^a-z0-9]/g, '');
                            if (rawTech && !rawTech.includes(targetTech) && !targetTech.includes(rawTech)) {
                                continue;
                            }
                        }

                        row.scrollIntoView({ behavior: 'instant', block: 'center' });
                        const clickTarget = matchedCell || cells[1] || cells[2];
                        clickTarget.scrollIntoView({ behavior: 'instant', block: 'center' });
                        clickTarget.click();
                        ['mousedown', 'mouseup', 'click'].forEach(evt => {
                            clickTarget.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                        });
                        return { success: true, wo: rowWoClean, tech: cells.length > 6 ? cells[6].textContent.trim() : '' };
                    }
                    return { success: false };
                }""", {"targetWo": clean_target_wo, "targetTech": clean_target_tech})

                if click_result.get("success"):
                    clicked = True
                    break

                # Scroll table container down to find row
                await self.page.evaluate("""() => {
                    const scrollContainer = document.querySelector('.p-datatable-wrapper') ||
                                             document.querySelector('.p-datatable-scrollable-body') ||
                                             document.querySelector('.custom-scrollbar');
                    if (scrollContainer) scrollContainer.scrollTop += 350;
                }""")
                await self.page.wait_for_timeout(350)

            # Fallback direct locator search if JS evaluation didn't find row
            if not clicked:
                target_xpaths = []
                if tech_name:
                    target_xpaths.extend([
                        f"//tbody//tr[contains(., '{clean_target_wo}') and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{tech_name.lower()}')]//span[contains(@class, 'dispatch-name')]",
                        f"//tbody//tr[contains(., '{clean_target_wo}') and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{tech_name.lower()}')]//td[3]",
                    ])
                target_xpaths.extend([
                    f"//span[contains(@class, 'dispatch-name') and (normalize-space()='{work_order_number}' or normalize-space()='#{work_order_number}')]",
                    f"//span[contains(@class, 'dispatch-name') and (@title='{work_order_number}' or @title='#{work_order_number}')]",
                    f"//td[contains(@class, 'cell-clickable')]//span[contains(normalize-space(), '{clean_target_wo}')]",
                ])
                for xpath in target_xpaths:
                    loc = self.page.locator(xpath)
                    if await loc.count() > 0:
                        try:
                            await loc.first.scroll_into_view_if_needed()
                            await loc.first.click()
                            clicked = True
                            break
                        except Exception:
                            try:
                                await loc.first.evaluate("el => el.click()")
                                clicked = True
                                break
                            except Exception:
                                continue

            if not clicked:
                print(f"⚠️ Could not click work order '{work_order_number}' on tech '{tech_name}'")
                return False

            tech_str = f" on tech '{tech_name}'" if tech_name else ""
            print(f"👉 Clicked WO #{work_order_number}{tech_str}, waiting for Assignment sidepanel...")
            try:
                await self.page.wait_for_selector('.container__Gkig, .custom-scrollbar', state='attached', timeout=8000)
            except Exception:
                await self.page.wait_for_timeout(2000)

            # CRITICAL VERIFICATION: Make sure opened sidepanel is actually for target work_order_number
            verified = False
            for _ in range(10):
                panel_text = await self.page.evaluate("""() => {
                    const p = document.querySelector('.container__Gkig, .panel-header-container_EYC6s');
                    return p ? (p.textContent || '') : '';
                }""")
                if clean_target_wo in re.sub(r'[^0-9]', '', panel_text):
                    verified = True
                    break
                await self.page.wait_for_timeout(300)

            if not verified:
                print(f"  ⚠️ Warning: Sidepanel text did not confirm WO #{clean_target_wo}! Verifying before action...")

            return True
        except Exception as e:
            print(f"⚠️ Error opening sidepanel for WO #{work_order_number}: {e}")
            return False

    async def cancel_work_order_via_dispatch_summary(
        self,
        work_order_number: str,
        truck_name: str = "",
        tech_name: str = "",
        reason: Optional[str] = None,
        allow_tech_cancel: bool = False,
        primary_wo_number: Optional[str] = None
    ) -> bool:
        """
        [CANCELLATION PERMANENTLY DISABLED]
        Work order cancellation has been completely removed across scrapers to prevent
        accidentally canceling real jobs and work orders on FieldEdge.
        """
        print(f"  🛡️ SAFETY GUARD: Cancellation requested for WO #{work_order_number} ('{truck_name}' on tech '{tech_name}'), but CANCELLATION IS PERMANENTLY DISABLED. Work order preserved.")
        return False

    async def click_and_reconcile_work_order(
        self,
        work_order_number: str,
        truck_name: str = "",
        expected_tech: str = ""
    ) -> bool:
        try:
            if not await self.click_and_open_sidepanel(work_order_number):
                return False

            for _ in range(16):
                has_tech = await self.page.evaluate("""() => {
                    const card = document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                                 document.querySelector('[data-edb-sidepanel-card*="Assignment"]');
                    if (card) {
                        const t = card.querySelector('.assigned-tech_omVmC') || card.querySelector('.header_IVnfD');
                        if (t && t.textContent.trim()) return true;
                    }
                    const wo = document.querySelector('[data-edb-sidepanel-card*="WorkOrderCard"]');
                    return !!wo && wo.textContent.trim().length > 0;
                }""")
                if has_tech:
                    break
                await self.page.wait_for_timeout(500)

            await self.page.evaluate("""() => {
                const card = document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                             document.querySelector('[data-edb-sidepanel-card*="Assignment"]');
                const container = document.querySelector('.custom-scrollbar.container__Gkig') || 
                                  document.querySelector('.container__Gkig');
                if (card) {
                    card.scrollIntoView({ behavior: 'instant', block: 'center' });
                    if (container && card.offsetTop > 0) {
                        container.scrollTop = Math.max(0, card.offsetTop - 50);
                    }
                } else if (container) {
                    container.scrollTop = container.scrollHeight * 0.45;
                }
            }""")
            await self.page.wait_for_timeout(800)

            current_tech = await self.page.evaluate("""() => {
                // Priority 1: AssignmentCard assigned tech
                const card = document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                             document.querySelector('[data-edb-sidepanel-card*="Assignment"]');
                if (card) {
                    const techEl = card.querySelector('.assigned-tech_omVmC .display-name_jfslj') || 
                                   card.querySelector('.assigned-tech_omVmC') ||
                                   card.querySelector('.display-name_jfslj');
                    if (techEl && techEl.textContent.trim()) {
                        return techEl.textContent.trim();
                    }
                    const cells = Array.from(card.querySelectorAll('.cell'));
                    for (const cell of cells) {
                        const title = cell.querySelector('.field-title');
                        if (title && title.textContent.toLowerCase().includes('assigned tech')) {
                            const val = cell.querySelector('.panel-content, .display-name_jfslj');
                            if (val && val.textContent.trim()) return val.textContent.trim();
                        }
                    }
                }
                // Priority 2: WorkOrderCard Primary Tech
                const woCard = document.querySelector('[data-edb-sidepanel-card*="WorkOrderCard"]') ||
                               document.querySelector('[data-edb-sidepanel-card*="WorkOrder"]');
                if (woCard) {
                    const cells = Array.from(woCard.querySelectorAll('.cell'));
                    for (const cell of cells) {
                        const title = cell.querySelector('.field-title');
                        if (title && title.textContent.toLowerCase().includes('primary tech')) {
                            const val = cell.querySelector('.break-line_Bz0vP, .panel-content');
                            if (val && val.textContent.trim()) return val.textContent.trim();
                        }
                    }
                }
                return '';
            }""")

            template_expected = self.get_expected_tech_for_truck(truck_name) if truck_name else None
            target_expected_tech = template_expected or expected_tech or ""

            print(f"  📋 Dashboard Tech: '{current_tech}' | 🎯 JSON Expected Tech: '{target_expected_tech}'")

            if current_tech and target_expected_tech and self.is_tech_match(current_tech, target_expected_tech):
                print(f"  ✅ Tech MATCH! Dashboard and JSON template have the same technician ('{current_tech}').")
                print("  Closing panel...")
                await self.close_sidepanel()
                return True

            print(f"  ⚠️ Tech MISMATCH! Reassigning WO #{work_order_number} to right tech '{target_expected_tech}'...")

            edit_active = False
            for attempt in range(1, 4):
                print(f"  ✏️ Clicking Edit icon in Assignment card (Attempt {attempt}/3)...")

                await self.page.evaluate("""() => {
                    const card = document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                                 document.querySelector('[data-edb-sidepanel-card*="Assignment"]');
                    const container = document.querySelector('.custom-scrollbar.container__Gkig') || 
                                      document.querySelector('.container__Gkig');
                    if (card) {
                        card.scrollIntoView({ behavior: 'instant', block: 'center' });
                        if (container && card.offsetTop > 0) {
                            container.scrollTop = Math.max(0, card.offsetTop - 50);
                        }
                    }
                }""")
                await self.page.wait_for_timeout(300)

                await self.page.evaluate("""() => {
                    const card = document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                                 document.querySelector('[data-edb-sidepanel-card*="Assignment"]');
                    let btn = null;
                    if (card) {
                        btn = card.querySelector('.header_IVnfD .base-icon.edit') ||
                              card.querySelector('.base-icon.edit.default') ||
                              card.querySelector('.base-icon.edit') ||
                              card.querySelector('.edit');
                    }
                    if (!btn) {
                        const headers = Array.from(document.querySelectorAll('.header_IVnfD, .header-container_IH6f_'));
                        for (const h of headers) {
                            if (h.textContent && h.textContent.toLowerCase().includes('assignment')) {
                                btn = h.querySelector('.base-icon.edit, .edit');
                                if (btn) break;
                            }
                        }
                    }
                    if (btn) {
                        btn.scrollIntoView({ behavior: 'instant', block: 'center' });
                        btn.click();
                        btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                    }
                }""")

                try:
                    pw_edit = self.page.locator('[data-edb-sidepanel-card*="Assignment"] .header_IVnfD .base-icon.edit, [data-edb-sidepanel-card*="Assignment"] .base-icon.edit, .base-icon.edit.default').first
                    if await pw_edit.count() > 0 and await pw_edit.is_visible():
                        await pw_edit.click(force=True)
                except Exception:
                    pass

                await self.page.wait_for_timeout(1000)

                edit_active = await self.page.evaluate("""() => {
                    const dd = document.querySelector('.assigned-tech_omVmC .tech-dropdown_qufHC') ||
                               document.querySelector('.tech-dropdown_qufHC') ||
                               document.querySelector('.assigned-tech_omVmC .v-select') ||
                               document.querySelector('[data-edb-sidepanel-card*="Assignment"] .v-select');
                    return !!dd;
                }""")
                if edit_active:
                    print("  ✅ Assignment card entered edit mode (tech-dropdown visible).")
                    break

            if not edit_active:
                print("  ⚠️ Could not confirm edit mode for Assignment card; attempting dropdown selection anyway.")

            await self.select_dropdown_option(None, target_expected_tech)
            await self.page.wait_for_timeout(1000)

            try:
                await self.page.wait_for_selector(
                    'button.confirm_I8LeR, button.button_a3g51:has-text("Save"), button:has-text("Save")',
                    state='visible',
                    timeout=5000
                )
            except Exception:
                pass

            saved = await self.page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button.confirm_I8LeR, button.button_a3g51, button'));
                const saveBtn = btns.find(b => {
                    const txt = (b.textContent || b.innerText || '').trim().toLowerCase();
                    return txt === 'save' || b.classList.contains('confirm_I8LeR');
                });
                if (saveBtn) {
                    saveBtn.removeAttribute('disabled');
                    saveBtn.scrollIntoView({ behavior: 'instant', block: 'center' });
                    saveBtn.click();
                    saveBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                    return true;
                }
                return false;
            }""")

            if not saved:
                try:
                    pw_save = self.page.locator('button.confirm_I8LeR, button:has-text("Save")').first
                    if await pw_save.count() > 0 and await pw_save.is_visible():
                        await pw_save.click(force=True)
                        saved = True
                except Exception:
                    pass

            if saved:
                print(f"  💾 Clicked Save button for WO #{work_order_number}.")
                await self.page.wait_for_timeout(3000)
            else:
                print(f"  ⚠️ Save button not triggered for WO #{work_order_number}.")

            await self.close_sidepanel()
            return saved

        except Exception as e:
            print(f"❌ Error reconciling WO #{work_order_number}: {e}")
            await self.close_sidepanel()
            return False

    def find_asset_row(self, asset_name: str, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Finds the matching asset work order row on the board.
        STRICT REQUIREMENT: Must be a genuine fleet vehicle work order (🚛 or 🛻).
        Must NEVER match customer jobs, crew assignments, or regular work orders.
        """
        if not asset_name or not rows:
            return None
        import re
        c_target = self.canonical_str(asset_name)
        target_nums = re.findall(r'\d+', asset_name)
        target_num = target_nums[0] if target_nums else ""

        for r in rows:
            cust = (r.get("customer_name") or "").strip()
            prio = (r.get("priority_name") or "").strip()

            # 1. Never match crew/laborer/helper cards
            if "👥" in cust or "laborer" in cust.lower() or "helper" in cust.lower():
                continue

            # 2. Must be a genuine fleet vehicle card (🚛 or 🛻) or explicit asset assignment
            is_vehicle = ("🛻" in cust or "🚛" in cust)
            if not is_vehicle and not r.get("is_asset_assignment"):
                if not ("asset assignment" in prio.lower() or "ser tr" in prio.lower() or "pt (" in prio.lower() or "service needed" in prio.lower()):
                    continue

            c_cust = self.canonical_str(cust)

            # 3. Exact canonical match (e.g. '🛻 | 71' matches '🛻 | 71')
            if c_target and c_target == c_cust:
                return r

            # 4. Strict number match on the truck number after pipe '|' for vehicle work orders
            if target_num and is_vehicle:
                if "|" in cust:
                    after_pipe = cust.split("|")[1].strip()
                    pipe_nums = re.findall(r'\d+', after_pipe)
                    if pipe_nums and int(pipe_nums[0]) == int(target_num):
                        return r
                else:
                    cust_nums = re.findall(r'\d+', cust)
                    if cust_nums and int(cust_nums[0]) == int(target_num):
                        return r

        return None

    def is_time_matching(self, target_time: str, sched_str: str) -> bool:
        if not target_time or not sched_str:
            return False
        import re
        m_target = re.search(r'(\d{1,2}):(\d{2})\s*(am|pm)?', target_time, re.IGNORECASE)
        if m_target:
            th = int(m_target.group(1))
            tm = m_target.group(2)
            tp = (m_target.group(3) or '').lower()
            if tp == 'pm' and th < 12:
                th_24 = th + 12
            elif tp == 'am' and th == 12:
                th_24 = 0
            else:
                th_24 = th

            times_in_sched = re.findall(r'(\d{1,2}):(\d{2})\s*(am|pm)?', sched_str, re.IGNORECASE)
            for sh_s, sm, sp in times_in_sched:
                sh = int(sh_s)
                sp = (sp or '').lower()
                if sp == 'pm' and sh < 12:
                    sh_24 = sh + 12
                elif sp == 'am' and sh == 12:
                    sh_24 = 0
                else:
                    sh_24 = sh

                if tp and sp:
                    if sh == th and sm == tm and sp == tp:
                        return True
                if sh_24 == th_24 and sm == tm:
                    return True

        norm_target = re.sub(r'[^0-9a-z]', '', target_time.lower())
        norm_sched = re.sub(r'[^0-9a-z]', '', sched_str.lower())
        return bool(norm_target and norm_target in norm_sched)

    @staticmethod
    def is_priority_match(current_priority: str, target_priority: str) -> bool:
        if not target_priority:
            return True
        if not current_priority:
            return False
        import unicodedata, re
        norm_curr = re.sub(r'[^a-zA-Z0-9]', '', unicodedata.normalize('NFKD', str(current_priority))).lower()
        norm_target = re.sub(r'[^a-zA-Z0-9]', '', unicodedata.normalize('NFKD', str(target_priority))).lower()
        if norm_curr == norm_target or (norm_target and norm_target in norm_curr) or (norm_curr and norm_curr in norm_target):
            return True

        curr_upper = str(current_priority).upper()
        target_upper = str(target_priority).upper()
        if "SERVICE NEEDED" in target_upper and "SERVICE NEEDED" in curr_upper:
            return True

        m_curr = re.search(r'\b\d+\.\d+\b', str(current_priority))
        m_target = re.search(r'\b\d+\.\d+\b', str(target_priority))
        if m_curr and m_target and m_curr.group(0) == m_target.group(0):
            return True

        return False

    async def update_service_needed_work_order(
        self,
        work_order_number: str,
        asset_name: str,
        target_time: str = "3:30 PM",
        target_priority: str = "[7] 7.12 SERVICE NEEDED",
        service_note: str = "",
        target_tech: str = "s4",
        current_tech: str = "",
        current_sched: str = "",
        current_priority: str = ""
    ) -> bool:
        """
        Updates an existing asset work order to be in 'Service Needed':
        1. Open sidepanel by clicking WO #.
        2. In Assignment card:
           - If tech already matches target_tech, skip tech edit.
           - If time already matches target_time, skip time edit.
           - Only edit and save Assignment card if either needs updating.
        3. In Work Order card:
           - If priority already matches target_priority, skip priority edit.
           - If note already set, skip note edit.
           - Only edit and save Work Order details if either needs updating.
        4. If everything is already set, skip entirely and close sidepanel.
        """
        print(f"\n🛠️ [Service Needed Update] Processing WO #{work_order_number} ('{asset_name}')...")
        try:
            if not await self.click_and_open_sidepanel(work_order_number):
                return False

            await self.page.wait_for_timeout(1500)

            # ── Step 1: Check live Assignment card (Time & Tech) in sidepanel ──
            sidepanel_info = await self.page.evaluate("""() => {
                const card = document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                             document.querySelector('[data-edb-sidepanel-card*="Assignment"]') ||
                             document.querySelector('.card_ngkhz');
                if (!card) return { time: '', tech: '' };
                let time = '';
                let tech = '';
                const techEl = card.querySelector('.assigned-tech_omVmC .display-name_jfslj') || 
                               card.querySelector('.assigned-tech_omVmC') ||
                               card.querySelector('.display-name_jfslj');
                if (techEl && techEl.textContent.trim()) {
                    tech = techEl.textContent.trim();
                }
                const cells = Array.from(card.querySelectorAll('.cell'));
                for (const cell of cells) {
                    const title = cell.querySelector('.field-title');
                    if (title) {
                        const t = title.textContent.toLowerCase();
                        if (t.includes('scheduled time') || t.includes('time')) {
                            const val = cell.querySelector('.panel-content, div[name="Scheduled Time"], input, div');
                            if (val) time = (val.value || val.textContent || '').trim();
                        }
                        if (!tech && t.includes('assigned tech')) {
                            const val = cell.querySelector('.panel-content, .display-name_jfslj');
                            if (val) tech = (val.value || val.textContent || '').trim();
                        }
                    }
                }
                const inp = card.querySelector('div[name="Scheduled Time"] input, .timepicker-container input');
                if (inp && inp.value) time = inp.value.trim();
                return { time, tech };
            }""")
            sched_to_check = sidepanel_info.get("time") or current_sched
            tech_to_check = sidepanel_info.get("tech") or current_tech

            need_tech_change = bool(target_tech and not self.is_tech_match(tech_to_check, target_tech) and self.canonical_str(tech_to_check) != self.canonical_str(target_tech))
            # CRITICAL: Scheduled time must NEVER be edited on FieldEdge work orders. Keep existing scheduled time!
            need_assignment_update = need_tech_change

            # ── Check Work Order details (Priority & Note) ──
            sidepanel_wo_info = await self.page.evaluate(r"""() => {
                let priority = '';
                let note = '';
                const selectedPrio = document.querySelector('.priority-select .vs__selected, #prioritySelect .vs__selected');
                if (selectedPrio && selectedPrio.textContent) {
                    priority = selectedPrio.textContent.trim();
                }
                if (!priority) {
                    const cards = document.querySelectorAll('.workorder-summary-card_mv9VQ, .workorder-details-card_kiboz, [data-edb-sidepanel-card*="WorkOrder"], .card_ngkhz');
                    for (const card of cards) {
                        const cells = card.querySelectorAll('.cell, .field-container, tr, div');
                        for (const cell of cells) {
                            const title = cell.querySelector('.field-title, .title, .label');
                            if (title && title.textContent.toLowerCase().includes('priority')) {
                                const val = cell.querySelector('.panel-content, .value, span, div:not(.field-title)');
                                if (val && val.textContent) {
                                    priority = val.textContent.trim();
                                    break;
                                }
                            }
                        }
                        if (priority) break;
                        const text = card.textContent || '';
                        const match = text.match(/\[\d+\]\s*[A-Za-z0-9\.\s\(\)\-_/]+/);
                        if (match && (match[0].includes('SERVICE') || match[0].includes('ROUTINE') || match[0].includes('PT') || match[0].includes('SER TR') || match[0].includes('SHOP'))) {
                            priority = match[0].trim();
                            break;
                        }
                    }
                }
                const noteEl = document.querySelector('.summary-note-container_f6fBE textarea') ||
                               document.querySelector('.summary-note-container_f6fBE .content') ||
                               document.querySelector('.summary-note-container_f6fBE');
                if (noteEl) {
                    note = (noteEl.value || noteEl.textContent || '').trim();
                }
                return { priority, note };
            }""")

            prio_to_check = sidepanel_wo_info.get("priority") or current_priority
            is_prio_already_set = bool(target_priority and self.is_priority_match(prio_to_check, target_priority))
            need_priority_change = bool(target_priority and not is_prio_already_set)

            existing_note = sidepanel_wo_info.get("note") or ""
            is_note_already_set = bool(not service_note or (existing_note and service_note.strip().lower() in existing_note.strip().lower()))
            need_note_change = bool(service_note and not is_note_already_set)

            need_wo_update = need_priority_change or need_note_change

            # If both Assignment card and Work Order details are ALREADY set, skip entirely!
            if not need_assignment_update and not need_wo_update:
                print(f"  ⏭️ WO #{work_order_number} ('{asset_name}') is ALREADY set (Tech: '{tech_to_check}', Priority: '{prio_to_check}'). Skipping update.")
                await self.close_sidepanel()
                return True

            # ── Step 1: Update Assignment card if tech needs change ──
            if need_assignment_update:
                print(f"  🕒 Updating Assignment card (Need tech: {need_tech_change})...")
                # Scroll Assignment card into view
                await self.page.evaluate("""() => {
                    const card = document.querySelector('.card_ngkhz.edit-mode-style_ABe0v') ||
                                 document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                                 document.querySelector('[data-edb-sidepanel-card*="Assignment"]') ||
                                 document.querySelector('.card_ngkhz');
                    if (card) card.scrollIntoView({ behavior: 'instant', block: 'center' });
                }""")
                await self.page.wait_for_timeout(400)

                # Check if Assignment card is already in edit mode
                is_edit_mode = await self.page.evaluate("""() => {
                    const card = document.querySelector('.card_ngkhz.edit-mode-style_ABe0v') ||
                                 document.querySelector('.tech-dropdown_qufHC');
                    return !!card;
                }""")

                if not is_edit_mode:
                    for _ in range(3):
                        assignment_edit_clicked = await self.page.evaluate("""() => {
                            const card = document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                                         document.querySelector('[data-edb-sidepanel-card*="Assignment"]') ||
                                         document.querySelector('.card_ngkhz');
                            if (!card) return false;
                            const btn = card.querySelector('.header_IVnfD .base-icon.edit') ||
                                        card.querySelector('.base-icon.edit.default') ||
                                        card.querySelector('.base-icon.edit') ||
                                        card.querySelector('.edit');
                            if (btn && btn.offsetParent !== null) {
                                btn.click();
                                btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                                return true;
                            }
                            return false;
                        }""")
                        if assignment_edit_clicked:
                            break
                        try:
                            pw_btn = self.page.locator('[data-edb-sidepanel-card*="Assignment"] .base-icon.edit, .card_ngkhz .base-icon.edit').first
                            if await pw_btn.count() > 0 and await pw_btn.is_visible():
                                await pw_btn.click(force=True)
                                break
                        except Exception:
                            pass
                        await self.page.wait_for_timeout(800)

                await self.page.wait_for_timeout(1000)

                # Update Tech if needed
                if target_tech and need_tech_change:
                    print(f"  👤 Updating technician to '{target_tech}'...")
                    await self.select_dropdown_option(None, target_tech)
                    await self.page.wait_for_timeout(600)
                else:
                    print(f"  👤 Technician already matches '{target_tech}'. Skipping tech edit.")

                # Click Save on Assignment card
                print("  💾 Saving Assignment card...")
                saved_assignment = await self.page.evaluate("""() => {
                    const card = document.querySelector('.card_ngkhz.edit-mode-style_ABe0v') ||
                                 document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                                 document.querySelector('.card_ngkhz');
                    if (!card) return false;
                    const btns = Array.from(card.querySelectorAll('button.confirm_I8LeR, button.button_a3g51.confirm_I8LeR, button'));
                    const saveBtn = btns.find(b => {
                        const txt = (b.textContent || '').trim().toLowerCase();
                        return txt === 'save' || b.classList.contains('confirm_I8LeR');
                    });
                    if (saveBtn) {
                        saveBtn.removeAttribute('disabled');
                        saveBtn.click();
                        saveBtn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        return true;
                    }
                    return false;
                }""")

                if not saved_assignment:
                    try:
                        pw_save = self.page.locator('.edit-mode-style_ABe0v button.confirm_I8LeR, [data-edb-sidepanel-card*="Assignment"] button.confirm_I8LeR, button.confirm_I8LeR').first
                        if await pw_save.count() > 0:
                            await pw_save.click(force=True)
                            saved_assignment = True
                    except Exception:
                        pass

                if saved_assignment:
                    print(f"  ✅ Saved Assignment card.")
                    await self.page.wait_for_timeout(2500)
                else:
                    print("  ⚠️ Assignment Save button not triggered.")
            else:
                print(f"  ⏭️ Assignment card already matches (Tech: '{tech_to_check}'). Skipping Assignment edit.")

            # ── Step 2: Update Work Order card (Priority & Description) if not already set ──
            if need_wo_update:
                print(f"  📝 Updating Work Order details (Need priority: {need_priority_change}, Need note: {need_note_change})...")

                # Scroll Work Order card into view
                await self.page.evaluate("""() => {
                    const woCard = document.querySelector('.card_ngkhz:has(.workorder-summary-card_mv9VQ)') ||
                                   document.querySelector('[data-edb-sidepanel-card*="WorkOrderCard"]') ||
                                   document.querySelector('.workorder-summary-card_mv9VQ') ||
                                   document.querySelector('.card_ngkhz');
                    if (woCard) woCard.scrollIntoView({ behavior: 'instant', block: 'center' });
                }""")
                await self.page.wait_for_timeout(500)

                # Click arrow (.details-icon_lgpCm) in Work Order summary card if present
                await self.page.evaluate("""() => {
                    const arrow = document.querySelector('.workorder-summary-card_mv9VQ ~ .details-icon_lgpCm') ||
                                  document.querySelector('.card_ngkhz:has(.workorder-summary-card_mv9VQ) .details-icon_lgpCm') ||
                                  document.querySelector('.details-icon_lgpCm');
                    if (arrow && arrow.offsetParent !== null) {
                        arrow.click();
                    }
                }""")
                await self.page.wait_for_timeout(1000)

                # Click Edit icon (.base-icon.edit.edit-icon_vG0FU)
                edit_wo_clicked = False
                for _ in range(3):
                    edit_wo_clicked = await self.page.evaluate("""() => {
                        const btn = document.querySelector('.base-icon.edit.edit-icon_vG0FU') ||
                                    document.querySelector('.edit-icon_vG0FU') ||
                                    document.querySelector('.workorder-details-card_kiboz .base-icon.edit') ||
                                    document.querySelector('[data-edb-sidepanel-card*="WorkOrder"] .base-icon.edit') ||
                                    document.querySelector('.base-icon.edit');
                        if (btn && btn.offsetParent !== null) {
                            btn.click();
                            btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                            return true;
                        }
                        return false;
                    }""")
                    if edit_wo_clicked:
                        break
                    try:
                        pw_edit = self.page.locator('.base-icon.edit.edit-icon_vG0FU, .edit-icon_vG0FU').first
                        if await pw_edit.count() > 0 and await pw_edit.is_visible():
                            await pw_edit.click(force=True)
                            edit_wo_clicked = True
                            break
                    except Exception:
                        pass
                    await self.page.wait_for_timeout(800)

                await self.page.wait_for_timeout(1000)

                # 1. Select Priority if needed
                if need_priority_change and target_priority:
                    print(f"  🏷️ Selecting Priority: '{target_priority}'...")
                    await self.page.evaluate("""() => {
                        const dd = document.querySelector('.priority-select, #prioritySelect');
                        if (dd) {
                            const toggle = dd.querySelector('.vs__dropdown-toggle') || dd.querySelector('input.vs__search') || dd;
                            toggle.click();
                            toggle.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                            toggle.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                        }
                    }""")
                    await self.page.wait_for_timeout(500)

                    await self.page.evaluate("""(target) => {
                        const inp = document.querySelector('.priority-select input.vs__search, #prioritySelect input.vs__search');
                        if (inp) {
                            inp.focus();
                            inp.value = target;
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                    }""", target_priority)
                    await self.page.wait_for_timeout(500)

                    prio_clicked = await self.page.evaluate("""(target) => {
                        const norm = (s) => (s || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g, '');
                        const cTarget = norm(target);
                        const items = Array.from(document.querySelectorAll('.priority-select ul[role="listbox"] li, #prioritySelect ul[role="listbox"] li, .vs__dropdown-menu li, li.vs__dropdown-option, ul[role="listbox"] li'));
                        for (const item of items) {
                            const txt = (item.textContent || '').trim();
                            const cTxt = norm(txt);
                            if (cTxt === cTarget || (cTarget && cTxt.includes(cTarget)) || (cTxt && cTarget.includes(cTxt)) || (txt.toUpperCase().includes('SERVICE NEEDED') && target.toUpperCase().includes('SERVICE NEEDED'))) {
                                item.scrollIntoView({ behavior: 'instant', block: 'nearest' });
                                item.click();
                                item.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                                return true;
                            }
                        }
                        return false;
                    }""", target_priority)

                    if not prio_clicked:
                        try:
                            import re as _re
                            opt_loc = self.page.locator('.vs__dropdown-menu li, ul[role="listbox"] li, li.vs__dropdown-option').filter(has_text=_re.compile(r'service\s*needed', _re.I)).first
                            if await opt_loc.count() > 0 and await opt_loc.is_visible():
                                await opt_loc.click(force=True)
                                prio_clicked = True
                        except Exception as p_err:
                            print(f"  ⚠️ Priority locator click fallback note: {p_err}")

                    print(f"  -> Priority option selected: {'Success' if prio_clicked else 'Option not found'}")
                    await self.page.wait_for_timeout(500)
                else:
                    print(f"  🏷️ Priority already set to '{target_priority}'. Skipping priority selection.")

                # 2. Enter Work Summary if needed
                if need_note_change and service_note:
                    print(f"  📝 Entering Work Summary: '{service_note}'...")
                    await self.page.evaluate("""(note) => {
                        const containers = document.querySelectorAll('.summary-note-container_f6fBE');
                        let targetTa = null;
                        for (const c of containers) {
                            const title = c.querySelector('.field-title');
                            if (title) {
                                const t = title.textContent.toLowerCase();
                                if (t.includes('work summary') || (t.includes('summary') && !t.includes('description'))) {
                                    targetTa = c.querySelector('textarea');
                                    break;
                                }
                            }
                        }
                        if (!targetTa && containers.length > 1) {
                            targetTa = containers[1].querySelector('textarea');
                        }
                        if (targetTa) {
                            targetTa.focus();
                            targetTa.value = note;
                            targetTa.dispatchEvent(new Event('input', { bubbles: true }));
                            targetTa.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }""", service_note)

                    try:
                        ta_loc = self.page.locator('.summary-note-container_f6fBE:has-text("Work Summary") textarea').first
                        if await ta_loc.count() > 0 and await ta_loc.is_visible():
                            await ta_loc.fill(service_note)
                    except Exception:
                        pass
                    await self.page.wait_for_timeout(500)
                else:
                    print(f"  📝 Work Summary already set. Skipping note edit.")

                # 3. Click Save button on Work Order form
                print("  💾 Clicking Save on Work Order details...")
                wo_saved = await self.page.evaluate("""() => {
                    const form = document.querySelector('.workorder-details-card_kiboz') || 
                                 document.querySelector('.card_ngkhz.no-bottom-padding_XvTa4') ||
                                 document.querySelector('.card_ngkhz');
                    let btn = null;
                    if (form) {
                        btn = form.querySelector('button.confirm_I8LeR') ||
                              form.querySelector('.button_a3g51.confirm_I8LeR') ||
                              Array.from(form.querySelectorAll('button')).find(b => (b.textContent || '').trim().toLowerCase() === 'save');
                    }
                    if (!btn) {
                        btn = document.querySelector('button.confirm_I8LeR, .button_a3g51.confirm_I8LeR');
                    }
                    if (btn) {
                        btn.removeAttribute('disabled');
                        btn.click();
                        btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        return true;
                    }
                    return false;
                }""")

                if not wo_saved:
                    try:
                        pw_btn = self.page.locator('button.confirm_I8LeR, button.button_a3g51.confirm_I8LeR, button:has-text("Save")').first
                        if await pw_btn.count() > 0 and await pw_btn.is_visible():
                            await pw_btn.click(force=True)
                            wo_saved = True
                    except Exception:
                        pass

                await self.page.wait_for_timeout(3000)
                print(f"  🎉 Work Order save finished (Result: {'Saved' if wo_saved else 'Attempted'}).")
            else:
                print(f"  ⏭️ Work Order details already set (Priority: '{prio_to_check}'). Skipping Work Order details edit.")

            await self.close_sidepanel()
            return True

        except Exception as e:
            print(f"❌ Error updating service-needed WO #{work_order_number}: {e}")
            try:
                await self.close_sidepanel()
            except Exception:
                pass
            return False

    async def click_work_order(self, work_order_number: str) -> bool:
        return await self.click_and_reconcile_work_order(work_order_number)

    async def reconcile_single_date(
        self,
        date_str: str,
        click_wos: bool = True,
        status_name: Optional[str] = None,
        force_reconcile: bool = False
    ) -> Dict[str, Any]:
        """
        Reconcile asset assignment work orders for a single date.
        Can run standalone or be invoked from Dispatch Board Display Automation using an existing page.
        """
        try:
            # Parse date and check persistent history first
            dt = None
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d", "%m/%d/%y", "%m-%d-%y"):
                try:
                    dt = datetime.strptime(date_str.strip(), fmt)
                    break
                except ValueError:
                    pass

            normalized_date = self.format_date(date_str)
            db_date_key = dt.strftime("%Y-%m-%d") if dt else normalized_date

            # Fetch fresh history from Central Dashboard API
            history = self._load_history()
            # User requirement: "assets skip kora jabe na, oita run korbei"
            # Always run asset reconciliation on live board to inspect trucks and reconcile wrong tech assignments.

            # Fetch fresh template configuration from Dashboard API so latest changes are always live
            self.template_data = self._load_template()

            skip_weekends = self.template_data.get("skip_vehicle_work_orders_on_weekends", False)
            if dt and skip_weekends and dt.weekday() in (5, 6):
                print(f"⏭️ Skipping weekend date {date_str} (Saturday/Sunday) for asset reconciliation.")
                return {"truck_assign_headers": [], "count": 0, "reassignments": 0, "checked": 0}

            if not self.page:
                await self.initialize()

            target_url = (
                self.rules.get('asset_assignment_url')
                or self.rules.get('web_url')
                or 'https://login.fieldedge.com/Dispatch'
            )

            # Ensure page is on Dispatch grid
            cur_url = self.page.url or ""
            if "Dispatch" not in cur_url or "#/DispatchBoard" in cur_url or "DispatchBoard" in cur_url:
                print(f"Navigating to Asset Assignment URL: {target_url}")
                await self.page.goto(target_url, wait_until='domcontentloaded')
                await self.page.wait_for_timeout(3000)

            if "Login" in (self.page.url or ""):
                await self.login_fieldedge()
                if target_url and "Login" not in self.page.url:
                    await self.page.goto(target_url, wait_until='domcontentloaded')
                    await self.page.wait_for_timeout(3000)

            target_status = status_name or self.rules.get('status_name', 'Assigned')
            await self.select_status(target_status)

            normalized_date = self.format_date(date_str)
            print(f"Setting Date filter: {normalized_date} to {normalized_date}")
            await self.set_date_filter(normalized_date, normalized_date)

            print("Applying filters...")
            await self.apply_filters()

            scraped_result = await self.scrape_asset_assignments()
            all_rows = scraped_result.get("all_rows", [])
            truck_headers = scraped_result.get("truck_assign_headers", [])

            total_reconciled = 0
            total_checked = 0

            # ── 0. Reconcile Out of Service Vehicles (Exempt from Board - NO Cancellation) ──
            out_of_service_configs = [
                s for s in self.template_data.get("out_of_service_assets", [])
                if s.get("active", True)
            ]
            out_of_service_names = {
                self.canonical_str(s.get("asset_name", ""))
                for s in out_of_service_configs
                if s.get("asset_name")
            }

            if out_of_service_configs and click_wos:
                print(f"\n🚫 [Stage 0/3] Checking {len(out_of_service_configs)} Out of Service vehicle(s) on {normalized_date}...")
                for oos_cfg in out_of_service_configs:
                    asset_name = oos_cfg.get("asset_name", "")
                    reason = oos_cfg.get("reason", "Vehicle is Out of Service for repairs.")
                    print(f"  🚫 Vehicle '{asset_name}' is OUT OF SERVICE ({reason}). Exempting from reassignment (cancellation disabled).")

            if out_of_service_names and truck_headers:
                truck_headers = [
                    r for r in truck_headers
                    if self.canonical_str(r.get("name", "")) not in out_of_service_names
                    and self.canonical_str(r.get("asset_name", "")) not in out_of_service_names
                ]

            # ── 1. Reconcile Service Needed Assets (Existing Card Update) ──
            service_needed_configs = [
                s for s in self.template_data.get("service_needed_assets", [])
                if s.get("active", True)
            ]
            service_needed_names = {
                self.canonical_str(s.get("asset_name", ""))
                for s in service_needed_configs
            }

            if service_needed_configs and click_wos:
                print(f"\n🔧 [Stage 1/3] Checking {len(service_needed_configs)} active service-needed assets on {normalized_date}...")
                for sn_cfg in service_needed_configs:
                    asset_name = sn_cfg.get("asset_name", "")
                    target_time = sn_cfg.get("start_time", "3:30 PM")
                    target_priority = sn_cfg.get("priority", "[7] 7.12 SERVICE NEEDED")
                    service_note = sn_cfg.get("service_note", "")
                    target_tech = sn_cfg.get("tech_name", "s4")

                    matching_row = self.find_asset_row(asset_name, all_rows)
                    if matching_row:
                        wo_num = matching_row.get("work_order_number", "")
                        curr_tech = matching_row.get("tech_name", "")
                        curr_sched = matching_row.get("scheduled_date", "")
                        curr_prio = matching_row.get("priority_name", "")

                        print(f"  👉 Found existing WO #{wo_num} for '{asset_name}' on tech '{curr_tech}' (Sched: {curr_sched}, Priority: {curr_prio})")

                        # Check tech & priority: if both are already set, skip sidepanel completely!
                        is_tech_set = not target_tech or self.is_tech_match(curr_tech, target_tech) or (self.canonical_str(curr_tech) == self.canonical_str(target_tech))
                        is_prio_set = not target_priority or self.is_priority_match(curr_prio, target_priority)

                        if is_tech_set and is_prio_set:
                            print(f"  ⏭️ WO #{wo_num} for '{asset_name}' is ALREADY fully set (Tech: '{curr_tech}', Priority: '{curr_prio}'). Skipping update.")
                            continue

                        total_checked += 1
                        updated = await self.update_service_needed_work_order(
                            work_order_number=wo_num,
                            asset_name=asset_name,
                            target_time=target_time,
                            target_priority=target_priority,
                            service_note=service_note,
                            target_tech=target_tech,
                            current_tech=curr_tech,
                            current_sched=curr_sched,
                            current_priority=curr_prio
                        )
                        if updated:
                            total_reconciled += 1
                    else:
                        print(f"  ⚠️ Could not find card for service-needed asset '{asset_name}' on {normalized_date}.")

            # ── 2. Standard Truck Assignment Reconciliation (Existing Asset Tool Flow) ──
            if service_needed_names and truck_headers:
                truck_headers = [
                    r for r in truck_headers
                    if self.canonical_str(r.get("name", "")) not in service_needed_names
                    and self.canonical_str(r.get("asset_name", "")) not in service_needed_names
                ]

            if truck_headers:
                print(f"\n🚛 [Stage 2/2] Standard Truck Reconciliation: Found {len(truck_headers)} truck_assign_headers on {normalized_date}:")
                for r in truck_headers:
                    try:
                        print(f"  - {r['name']} -> Current Tech: {r['tech_name']} | WO #{r['work_order_number']}")
                    except Exception:
                        safe_name = str(r.get('name', '')).encode('ascii', errors='replace').decode()
                        safe_tech = str(r.get('tech_name', '')).encode('ascii', errors='replace').decode()
                        print(f"  - {safe_name} -> Current Tech: {safe_tech} | WO #{r.get('work_order_number')}")

                # ── Multiple Truck Assignments Detection (Informational Only - NO Cancellation) ──
                # ONLY evaluate genuine fleet trucks (🚛 / 🛻). NEVER touch helpers/crew (👥).
                tech_to_trucks = {}
                for r in truck_headers:
                    t_name = r.get("name", "")
                    if "👥" in t_name or "helper" in t_name.lower() or "laborer" in t_name.lower():
                        continue
                    if not ("🚛" in t_name or "🛻" in t_name):
                        continue
                    c_tech = self.canonical_str(r.get("tech_name", ""))
                    if c_tech:
                        if c_tech not in tech_to_trucks:
                            tech_to_trucks[c_tech] = []
                        tech_to_trucks[c_tech].append(r)

                for c_tech, t_list in tech_to_trucks.items():
                    if len(t_list) > 1:
                        tech_display = t_list[0].get("tech_name", "")
                        print(f"\n⚠️ Technician '{tech_display}' has {len(t_list)} truck assignments on board: {[t.get('name') for t in t_list]}")

                        expected_truck_cfg = self.get_expected_truck_for_tech(tech_display)
                        expected_truck_name = expected_truck_cfg.get("name", "") if expected_truck_cfg else ""
                        c_exp_truck = self.canonical_str(expected_truck_name)

                        print(f"  🎯 Template expected truck for '{tech_display}' is: '{expected_truck_name}'")

                        for r_truck in t_list:
                            t_name = r_truck.get("name", "")
                            c_t_name = self.canonical_str(t_name)
                            wo_num = r_truck.get("work_order_number", "")

                            is_match = (
                                c_exp_truck and (
                                    c_t_name == c_exp_truck or
                                    c_exp_truck in c_t_name or
                                    c_t_name in c_exp_truck
                                )
                            )
                            if is_match:
                                print(f"  ✅ Truck '{t_name}' (WO #{wo_num}) matches JSON template for '{tech_display}'.")
                            else:
                                print(f"  ℹ️ Truck '{t_name}' (WO #{wo_num}) on '{tech_display}' does not match expected '{expected_truck_name}' (Cancellation disabled; preserving).")

                # ── Duplicate Truck Detection Across Technicians (Informational Only - NO Cancellation) ──
                truck_to_wos = {}
                for r in truck_headers:
                    t_name = r.get("name", "")
                    if "👥" in t_name or "helper" in t_name.lower() or "laborer" in t_name.lower():
                        continue
                    if not ("🚛" in t_name or "🛻" in t_name):
                        continue
                    c_truck = self.canonical_str(t_name)
                    if c_truck:
                        if c_truck not in truck_to_wos:
                            truck_to_wos[c_truck] = []
                        truck_to_wos[c_truck].append(r)

                for c_truck, w_list in truck_to_wos.items():
                    if len(w_list) > 1:
                        truck_display = w_list[0].get("name", "")
                        expected_tech = self.get_expected_tech_for_truck(truck_display)

                        print(f"\n⚠️ Truck '{truck_display}' appears on {len(w_list)} work orders across technicians: {[w.get('work_order_number') for w in w_list]}")
                        print(f"  🎯 Expected technician for '{truck_display}' is: '{expected_tech or 'None'}'")

                        for w in w_list:
                            wo_num = w.get("work_order_number")
                            t_tech = w.get("tech_name")
                            if expected_tech and self.is_tech_match(t_tech or "", expected_tech):
                                print(f"  ✅ WO #{wo_num} for '{truck_display}' is on expected tech '{t_tech}'.")
                            else:
                                print(f"  ℹ️ WO #{wo_num} for '{truck_display}' is on '{t_tech}' (Cancellation disabled; preserving).")

                if click_wos:
                    print(f"\n🖱️ Reconciling {len(truck_headers)} truck work orders with Dashboard config for {normalized_date}...")
                    for idx, r in enumerate(truck_headers, 1):
                        wo_num = r.get("work_order_number")
                        truck_name = r.get("name")
                        curr_tech_table = r.get("tech_name", "")
                        is_completed = r.get("is_completed", False)

                        # Check if this truck or header is turned OFF in Dashboard template
                        if self.is_item_turned_off_in_dashboard(truck_name, curr_tech_table):
                            print(f"[{idx}/{len(truck_headers)}] ⏸️ Truck '{truck_name}' (WO #{wo_num} on tech '{curr_tech_table}') is turned OFF in Dashboard template. Skipping without canceling.")
                            continue

                        expected_tech = self.get_expected_tech_for_truck(truck_name)

                        if not expected_tech:
                            print(f"[{idx}/{len(truck_headers)}] Truck '{truck_name}' is inactive or has no expected tech in Dashboard config. Skipping.")
                            continue

                        if wo_num:
                            # SMART PRE-CHECK: If the truck is ALREADY assigned to the expected technician in the table, skip sidepanel
                            if curr_tech_table and expected_tech and self.is_tech_match(curr_tech_table, expected_tech):
                                print(f"[{idx}/{len(truck_headers)}] ✅ Truck '{truck_name}' (WO #{wo_num}) is ALREADY assigned to '{curr_tech_table}' (matches '{expected_tech}'). Skipping sidepanel.")
                                continue

                            # Check if the expected technician ALREADY has this truck on another WO on the board
                            expected_tech_already_has_it = False
                            for other_r in truck_headers:
                                if other_r.get("work_order_number") != wo_num:
                                    other_truck = self.canonical_str(other_r.get("name", ""))
                                    other_tech = other_r.get("tech_name", "")
                                    if other_truck == self.canonical_str(truck_name) and self.is_tech_match(other_tech, expected_tech):
                                        expected_tech_already_has_it = True
                                        break

                            if expected_tech_already_has_it:
                                print(f"[{idx}/{len(truck_headers)}] ℹ️ Truck '{truck_name}' (WO #{wo_num}) is on '{curr_tech_table}', but expected tech '{expected_tech}' ALREADY has '{truck_name}'. Skipping without canceling.")
                                continue

                            total_checked += 1
                            print(f"\n[{idx}/{len(truck_headers)}] Checking WO #{wo_num} ({truck_name}) on {normalized_date} (Reassigning from '{curr_tech_table}' to expected '{expected_tech}')...")
                            updated = await self.click_and_reconcile_work_order(
                                work_order_number=wo_num,
                                truck_name=truck_name,
                                expected_tech=expected_tech
                            )
                            if updated:
                                total_reconciled += 1
            else:
                print(f"⚠️ No truck_assign_headers found on {normalized_date}.")

            # Record successful completion of asset reconciliation for this date in history
            self.record_reconciled(history, db_date_key)
            print(f"✅ Recorded asset reconciliation completion for {normalized_date} ({db_date_key}) in history.")

            return {
                "truck_assign_headers": truck_headers,
                "count": len(truck_headers),
                "reassignments": total_reconciled,
                "checked": total_checked
            }
        except Exception as e:
            print(f"❌ Error during asset reconciliation on {date_str}: {e}")
            return {"truck_assign_headers": [], "count": 0, "reassignments": 0, "checked": 0, "error": str(e)}

    async def run(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: Optional[int] = None,
        status_name: Optional[str] = None,
        url: Optional[str] = None,
        click_wos: bool = True
    ) -> Dict[str, Any]:
        _start_time = time.time()
        _error_occurred = None
        total_checked = 0
        total_reconciled = 0

        try:
            await self.initialize()
            # Reload live template configuration from Dashboard API (Single Source of Truth)
            self.template_data = self._load_template()

            target_url = (
                url
                or self.rules.get('asset_assignment_url')
                or self.rules.get('web_url')
                or 'https://login.fieldedge.com/Dispatch'
            )
            print(f"Navigating to Asset Assignment URL: {target_url}")
            await self.page.goto(target_url, wait_until='domcontentloaded')

            if "Login" in self.page.url:
                await self.login_fieldedge()
                if target_url and "Login" not in self.page.url:
                    print(f"Post-login navigating to target URL: {target_url}")
                    await self.page.goto(target_url, wait_until='domcontentloaded')
                    await self.page.wait_for_timeout(4000)

            ui_wait_candidates = [
                self.rules.get('status_assigned_xpath'),
                "div.circle-btn[title='Assigned']",
                ".cell-pos-1x3 .circle-btn",
                "div.circle-btn",
                "//div[contains(@class, 'circle-btn')]",
                "//div[contains(@class, 'filter-dropdown')][.//span[normalize-space(text())='Date']]",
                "button.filter-button",
                ".plot-map-button",
                "tbody.p-datatable-tbody",
                "tr.p-selectable-row",
                "tbody.fixed-body",
                ".kgRow"
            ]
            ui_wait_candidates = [c.strip() for c in ui_wait_candidates if c and isinstance(c, str) and c.strip()]

            print("Waiting for Dispatch Board / Asset Assignment UI controls...")
            ui_found = False
            for candidate in ui_wait_candidates:
                try:
                    await self.page.wait_for_selector(candidate, state='attached', timeout=8000)
                    print(f"✅ UI ready (found selector: '{candidate}')")
                    ui_found = True
                    break
                except Exception:
                    continue

            if not ui_found:
                print("⚠️ Primary UI selector not caught in initial wait, giving extra 3s buffer for SPA rendering...")
                await self.page.wait_for_timeout(3000)
            else:
                await self.page.wait_for_timeout(2000)

            target_status = status_name or self.rules.get('status_name', 'Assigned')
            print(f"Selecting Status filter: {target_status}")
            await self.select_status(target_status)

            # Determine start date and number of days to process
            pst_now = self.get_pst_now()
            start_dt = pst_now
            if start_date:
                for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d", "%m/%d/%y", "%m-%d-%y"):
                    try:
                        start_dt = datetime.strptime(start_date.strip(), fmt)
                        break
                    except ValueError:
                        pass

            if end_date:
                end_dt = None
                for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d", "%m/%d/%y", "%m-%d-%y"):
                    try:
                        end_dt = datetime.strptime(end_date.strip(), fmt)
                        break
                    except ValueError:
                        pass
                if end_dt and end_dt >= start_dt:
                    num_days = (end_dt.date() - start_dt.date()).days + 1
                else:
                    num_days = 1
            elif days is not None:
                num_days = max(1, int(days))
            else:
                num_days = int(self.rules.get('days', 30))

            print(f"📅 Running Asset Assignment monitor across {num_days} day(s), starting {start_dt.strftime('%m/%d/%Y')}...")

            total_reconciled = 0
            total_checked = 0
            all_truck_headers = []

            for d in range(num_days):
                current_dt = start_dt + timedelta(days=d)
                date_str = current_dt.strftime('%m/%d/%Y')

                print(f"\n" + "=" * 60)
                print(f"🗓️ Processing Date [{d + 1}/{num_days}]: {date_str}")
                print(f"=" * 60)

                recon_res = await self.reconcile_single_date(
                    date_str=date_str,
                    click_wos=click_wos,
                    status_name=target_status
                )
                headers = recon_res.get("truck_assign_headers", [])
                all_truck_headers.extend(headers)
                total_checked += recon_res.get("checked", 0)
                total_reconciled += recon_res.get("reassignments", 0)

            print(f"\n" + "=" * 60)
            print(f"✅ Asset Assignment run completed: Checked {total_checked} work orders across {num_days} days, reassignments performed: {total_reconciled}")
            print(f"=" * 60)

            return {
                "truck_assign_headers": all_truck_headers,
                "count": len(all_truck_headers),
                "reassignments": total_reconciled,
                "days_processed": num_days
            }

        except Exception as e:
            print(f"❌ Error during Asset Assignment run: {e}")
            _error_occurred = f"{str(e)}\n{traceback.format_exc()}"
            return {"truck_assign_headers": [], "count": 0, "error": str(e)}
        finally:
            await self.cleanup()
            # ── Execution completion summary ──
            _elapsed = time.time() - _start_time
            if _error_occurred:
                print(f"⚠️ Execution finished with ERROR ({round(_elapsed, 1)}s) for {self.scraper_name}:")
                print(_error_occurred)
            else:
                print(f"🎉 Execution completed successfully in {round(_elapsed, 1)}s for {self.scraper_name} (Processed {total_checked} records)")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Asset Assignment Scraper & Dynamic Reconciler")
    parser.add_argument("--days", type=int, default=30, help="Number of days to check and reconcile (default: 30)")
    parser.add_argument("--start-date", type=str, default=None, help="Start Date (MM/DD/YYYY)")
    parser.add_argument("--end-date", type=str, default=None, help="End Date (MM/DD/YYYY)")
    parser.add_argument("--status", type=str, default="Assigned", help="Status filter (default: Assigned)")
    parser.add_argument("--no-clicks", action="store_true", help="Skip clicking WO numbers one by one")
    parser.add_argument("--live", action="store_true", help="Run with live credentials")
    args = parser.parse_args()

    scraper = AssetAssignmentScraper()
    result = await scraper.run(
        start_date=args.start_date,
        end_date=args.end_date,
        days=args.days,
        status_name=args.status,
        click_wos=not args.no_clicks
    )
    success = result.get("count", 0) > 0 or ("error" not in result)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
