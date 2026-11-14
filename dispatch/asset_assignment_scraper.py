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

from base_scraper import BaseScraper

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
                        if os.path.exists(self.template_path):
                            try:
                                with open(self.template_path, "r", encoding="utf-8") as f:
                                    local_data = json.load(f)
                            except Exception:
                                local_data = {}

                        for key, value in remote_data.items():
                            local_data[key] = value

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
        history = {}

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
                        await self.page.wait_for_timeout(2000)
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

                        const colorEl = cells[0].querySelector('.priorityColor') || cells[0].querySelector('[style*="background-color"]');
                        const priorityColor = colorEl ? colorEl.style.backgroundColor : '';
                        const colorTitle = colorEl ? (colorEl.getAttribute('title') || '') : '';

                        const priorityName = cleanText(cells[1].textContent) || colorTitle;

                        const woEl = cells[2].querySelector('.dispatch-name') || cells[2];
                        const workOrderNumber = cleanText(woEl.getAttribute('title') || woEl.textContent);
                        if (!workOrderNumber) return;

                        const customerName = cleanText(cells[3].textContent);

                        const customerAddress = cleanText(cells[4].textContent);

                        const tagsEl = cells[5].querySelector('.tag-list-container') || cells[5];
                        const tags = cleanText(tagsEl.textContent);

                        const techName = cleanText(cells[6].textContent);

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
                            customerName.includes('🏗️') ||
                            priorityName.includes('PT (') ||
                            priorityName.includes('SER TR') ||
                            priorityName.includes('EXC (') ||
                            priorityName.toLowerCase().includes('do not dispatch')
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

        all_template_trucks = self.template_data.get("truck_assign_headers", [])

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

            if tech.lower() in ["s4", "s3", ""]:
                continue

            if "👥" in cust or "laborer" in cust.lower() or "helper" in cust.lower() or "crew" in cust.lower():
                continue

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

            if c_cust in expected_truck_map:
                matched_cfg = expected_truck_map[c_cust]
            elif "|" in cust:
                after_pipe = cust.split("|")[1].strip()
                pipe_nums = re.findall(r'\d+', after_pipe)
                if pipe_nums:
                    for pn in (pipe_nums[0], str(int(pipe_nums[0]))):
                        if pn in expected_truck_map:
                            matched_cfg = expected_truck_map[pn]
                            break

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
        if c1 == c2:
            return True
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
        c_item = self.canonical_str(item_name)
        nums_item = re.findall(r'\d+', item_name) if item_name else []

        for oos in self.template_data.get("out_of_service_assets", []):
            if oos.get("active", True):
                oos_name = oos.get("asset_name", "")
                c_oos = self.canonical_str(oos_name)
                nums_oos = re.findall(r'\d+', oos_name) if oos_name else []
                if c_oos == c_item or (nums_item and nums_oos and int(nums_item[0]) == int(nums_oos[0])):
                    return True

        for h in self.template_data.get("truck_assign_headers", []):
            h_name = h.get("name", "")
            c_hname = self.canonical_str(h_name)
            nums_h = re.findall(r'\d+', h_name) if h_name else []
            matches = (c_hname == c_item) or (nums_item and nums_h and int(nums_item[0]) == int(nums_h[0]))
            if matches:
                if h.get("active") is False:
                    return True
                h_tech = h.get("tech_name")
                if h_tech:
                    for t in self.template_data.get("active_techs", []):
                        if self.is_tech_match(t.get("tech_name") or t.get("name"), h_tech):
                            if t.get("active") is False:
                                return True
                return False

        for sh in self.template_data.get("static_headers", []):
            sh_name = sh.get("name", "")
            if self.canonical_str(sh_name) == c_item:
                if sh.get("active") is False:
                    return True
                return False

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
        for _ in range(8):
            await self.page.wait_for_timeout(150)
            is_closed = await self.page.evaluate("""() => {
                const p = document.querySelector('.container__Gkig, .panel-header-container_EYC6s');
                return !p || p.offsetParent === null;
            }""")
            if is_closed:
                break
        await self.page.wait_for_timeout(200)

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
            await self.page.wait_for_timeout(200)

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
            await self.page.wait_for_timeout(250)

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
            await self.page.wait_for_timeout(250)

            clicked = await self.page.evaluate("""(args) => {
                const { targetText, targetCanon, coreTech } = args;
                const norm = (s) => (s || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g, '');

                const items = Array.from(document.querySelectorAll('.assigned-tech_omVmC ul[role="listbox"] li, .tech-dropdown_qufHC ul li, .vs__dropdown-menu li, ul[role="listbox"] li, li.vs__dropdown-option'));
                if (!items.length) return false;

                const cTarget = norm(targetCanon);
                const cCore = norm(coreTech);

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

                return false;
            }""", {"targetText": option_text, "targetCanon": target_canon, "coreTech": core_tech})

            print(f"  -> Tech dropdown selection for '{option_text}': {'Success' if clicked else 'Failed'}")
            await self.page.wait_for_timeout(300)
            return clicked
        except Exception as e:
            print(f"⚠️ Error selecting '{option_text}' in dropdown: {e}")
            return False

    async def click_and_open_sidepanel(self, work_order_number: str, tech_name: str = "") -> bool:
        clean_target_wo = re.sub(r'[^0-9]', '', str(work_order_number))
        clean_target_tech = re.sub(r'[^a-z0-9]', '', tech_name.lower()) if tech_name else ""
        try:
            is_panel_open = await self.page.evaluate("""() => {
                const p = document.querySelector('.container__Gkig, .panel-header-container_EYC6s');
                return !!p && p.offsetParent !== null;
            }""")
            if is_panel_open:
                await self.close_sidepanel()
                await self.page.wait_for_timeout(250)

            await self.page.evaluate("""() => {
                const c = document.querySelector('.p-datatable-wrapper') ||
                          document.querySelector('.p-datatable-scrollable-body') ||
                          document.querySelector('.custom-scrollbar');
                if (c) c.scrollTop = 0;
            }""")
            await self.page.wait_for_timeout(150)

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

                await self.page.evaluate("""() => {
                    const scrollContainer = document.querySelector('.p-datatable-wrapper') ||
                                             document.querySelector('.p-datatable-scrollable-body') ||
                                             document.querySelector('.custom-scrollbar');
                    if (scrollContainer) scrollContainer.scrollTop += 350;
                }""")
                await self.page.wait_for_timeout(180)

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
                await self.page.wait_for_timeout(1500)

            verified = False
            for _ in range(10):
                panel_text = await self.page.evaluate("""() => {
                    const p = document.querySelector('.container__Gkig, .panel-header-container_EYC6s');
                    return p ? (p.textContent || '') : '';
                }""")
                if clean_target_wo in re.sub(r'[^0-9]', '', panel_text):
                    verified = True
                    break
                await self.page.wait_for_timeout(150)

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
                await self.page.wait_for_timeout(200)

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
            await self.page.wait_for_timeout(250)

            current_tech = await self.page.evaluate("""() => {
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
                await self.page.wait_for_timeout(150)

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

                await self.page.wait_for_timeout(300)

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
            await self.page.wait_for_timeout(300)

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
                for _ in range(10):
                    await self.page.wait_for_timeout(150)
                    edit_card_gone = await self.page.evaluate("""() => {
                        const c = document.querySelector('.card_ngkhz.edit-mode-style_ABe0v');
                        return !c;
                    }""")
                    if edit_card_gone:
                        break
                await self.page.wait_for_timeout(300)
            else:
                print(f"  ⚠️ Save button not triggered for WO #{work_order_number}.")

            await self.close_sidepanel()
            return saved

        except Exception as e:
            print(f"❌ Error reconciling WO #{work_order_number}: {e}")
            await self.close_sidepanel()
            return False

    def find_asset_row(self, asset_name: str, rows: List[Dict[str, Any]], target_priority: Optional[str] = None, target_tech: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if not asset_name or not rows:
            return None
        import re
        c_target = self.canonical_str(asset_name)

        is_target_exc = ("🏗️" in asset_name or "u-40" in asset_name.lower() or "u-17" in asset_name.lower() or "exc" in asset_name.lower())
        is_target_pump = ("🚛" in asset_name or "pt (" in asset_name.lower() or "pump" in asset_name.lower())
        is_target_serv = ("🛻" in asset_name or "ser tr" in asset_name.lower() or "service" in asset_name.lower())

        ident_target = asset_name.split("|")[-1].strip() if "|" in asset_name else asset_name.strip()
        c_ident_target = self.canonical_str(ident_target)

        matched_rows = []
        for r in rows:
            cust = (r.get("customer_name") or "").strip()
            prio = (r.get("priority_name") or "").strip()

            if "👥" in cust or "laborer" in cust.lower() or "helper" in cust.lower():
                continue

            is_vehicle = ("🛻" in cust or "🚛" in cust or "🏗️" in cust)
            if not is_vehicle and not r.get("is_asset_assignment"):
                if not ("asset assignment" in prio.lower() or "ser tr" in prio.lower() or "pt (" in prio.lower() or "service needed" in prio.lower() or "exc (" in prio.lower() or "do not dispatch" in prio.lower()):
                    continue

            is_cust_exc = ("🏗️" in cust or "u-40" in cust.lower() or "u-17" in cust.lower() or "exc" in prio.lower() or "exc" in cust.lower())
            if is_target_exc != is_cust_exc:
                continue

            is_cust_pump = ("🚛" in cust or "pt (" in prio.lower() or "pump" in cust.lower())
            is_cust_serv = ("🛻" in cust or "ser tr" in prio.lower() or "service" in cust.lower())
            if is_target_pump and is_cust_serv:
                continue
            if is_target_serv and is_cust_pump:
                continue

            c_cust = self.canonical_str(cust)

            is_match = False
            if c_target and c_target == c_cust:
                is_match = True
            else:
                ident_cust = cust.split("|")[-1].strip() if "|" in cust else cust.strip()
                c_ident_cust = self.canonical_str(ident_cust)

                if c_ident_target and c_ident_target == c_ident_cust:
                    is_match = True
                elif not is_target_exc and not is_cust_exc:
                    cust_digits = re.findall(r'^\d+$', ident_cust)
                    targ_digits = re.findall(r'^\d+$', ident_target)
                    if cust_digits and targ_digits and int(cust_digits[0]) == int(targ_digits[0]):
                        is_match = True

            if is_match:
                matched_rows.append(r)

        if not matched_rows:
            return None

        if target_tech:
            for r in matched_rows:
                if self.canonical_str(r.get("tech_name", "")) == self.canonical_str(target_tech) or self.is_tech_match(r.get("tech_name", ""), target_tech):
                    return r

        if target_priority:
            for r in matched_rows:
                if self.is_priority_match(r.get("priority_name", ""), target_priority):
                    return r

        if target_tech:
            for r in matched_rows:
                r_tech = r.get("tech_name", "").strip().lower()
                if r_tech in ("", "s4", "s3"):
                    return r

        return matched_rows[0]

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
        if "DO NOT DISPATCH" in target_upper and "DO NOT DISPATCH" in curr_upper:
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
        current_priority: str = "",
        stage_label: str = "Service Needed Update"
    ) -> bool:
        print(f"\n🛠️ [{stage_label}] Processing WO #{work_order_number} ('{asset_name}')...")
        try:
            if not await self.click_and_open_sidepanel(work_order_number):
                return False

            try:
                await self.page.wait_for_selector('.card_ngkhz, [data-edb-sidepanel-card]', state='attached', timeout=3000)
            except Exception:
                pass
            await self.page.wait_for_timeout(300)

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
            need_time_change = bool(target_time and not self.is_time_matching(target_time, sched_to_check))
            need_assignment_update = need_tech_change or need_time_change

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
                        if (match && (match[0].includes('SERVICE') || match[0].includes('ROUTINE') || match[0].includes('PT') || match[0].includes('SER TR') || match[0].includes('SHOP') || match[0].includes('EXC') || match[0].includes('DO NOT DISPATCH') || match[0].includes('TRACKING'))) {
                            priority = match[0].trim();
                            break;
                        }
                    }
                }
                note = '';
                const allCells = document.querySelectorAll('.cell, [class*="cell"]');
                for (const cell of allCells) {
                    const title = cell.querySelector('.field-title, [class*="field-title"], .title, .label');
                    if (title && title.textContent.toLowerCase().includes('work summary')) {
                        const val = cell.querySelector('.summary-note_Dk8T7, [class*="summary-note"], div[title], textarea, div:not(.field-title)');
                        if (val) {
                            const candidate = (val.getAttribute('title') || val.value || val.textContent || '').trim();
                            if (candidate && candidate !== '--') {
                                note = candidate;
                                break;
                            }
                        }
                    }
                }
                if (!note) {
                    const allNotes = document.querySelectorAll('.summary-note_Dk8T7, [class*="summary-note"]');
                    for (const el of allNotes) {
                        const candidate = (el.getAttribute('title') || el.value || el.textContent || '').trim();
                        if (candidate && candidate !== '--') {
                            note = candidate;
                            break;
                        }
                    }
                }
                const woCard = document.querySelector('[data-edb-sidepanel-card*="WorkOrder"]') || document.querySelector('.card_ngkhz');
                const cardText = woCard ? woCard.innerText : '';
                return { priority, note, cardText };
            }""")

            prio_to_check = sidepanel_wo_info.get("priority") or current_priority
            is_prio_already_set = bool(target_priority and self.is_priority_match(prio_to_check, target_priority))
            need_priority_change = bool(target_priority and not is_prio_already_set)

            existing_note = sidepanel_wo_info.get("note") or ""
            card_text = sidepanel_wo_info.get("cardText") or ""

            is_note_already_set = bool(
                not service_note or
                (existing_note and (
                    service_note.strip().lower() in existing_note.strip().lower() or
                    existing_note.strip().lower() in service_note.strip().lower()
                )) or
                (service_note and service_note.strip().lower() in card_text.lower())
            )
            need_note_change = bool(service_note and not is_note_already_set)

            need_wo_update = need_priority_change or need_note_change

            if not need_assignment_update and not need_wo_update:
                final_note = existing_note or (service_note if service_note and service_note.strip().lower() in card_text.lower() else "")
                print(f"  ⏭️ WO #{work_order_number} ('{asset_name}') is ALREADY fully set (Tech: '{tech_to_check}', Priority: '{prio_to_check}', Note: '{final_note}'). Skipping update.")
                await self.close_sidepanel()
                return True

            if need_wo_update:
                print(f"  📝 [Step 1/2] Updating Work Order details (Need priority: {need_priority_change}, Need note: {need_note_change})...")

                print("  📜 Scrolling sidepanel down to Work Order section...")
                await self.page.evaluate("""() => {
                    const containers = Array.from(document.querySelectorAll('.custom-scrollbar, .container__Gkig, [class*="sidepanel"] .container, [class*="sidepanel"] [class*="scrollbar"], .sidepanel-content'));
                    for (const cont of containers) {
                        cont.scrollTop = cont.scrollHeight;
                    }
                    const wo = document.querySelector('.card_ngkhz:has(.workorder-summary-card_mv9VQ)') ||
                               document.querySelector('.workorder-summary-card_mv9VQ')?.closest('.card_ngkhz') ||
                               Array.from(document.querySelectorAll('.card_ngkhz')).find(c => {
                                   const pt = c.querySelector('.panel-title, .field-title');
                                   const txt = (pt ? pt.textContent : c.textContent || '').trim();
                                   return txt.includes('Work Order') && !txt.toUpperCase().includes('CUSTOMER');
                               });
                    if (wo) {
                        wo.scrollIntoView({ behavior: 'instant', block: 'center' });
                    }
                }""")
                await self.page.wait_for_timeout(200)

                wo_card_loc = self.page.locator(
                    ".card_ngkhz:has(.workorder-summary-card_mv9VQ), .card_ngkhz:has(.panel-title:has-text('Work Order'))"
                ).first

                try:
                    if await wo_card_loc.count() > 0:
                        await wo_card_loc.scroll_into_view_if_needed()
                except Exception:
                    pass
                await self.page.wait_for_timeout(150)

                print("  👉 Clicking Work Order Details arrow button...")
                details_opened = False
                for open_attempt in range(6):
                    is_details_open = await self.page.evaluate("""() => {
                        return !!(document.querySelector('.edit-icon_vG0FU') || 
                                  document.querySelector('.base-icon.edit.edit-icon_vG0FU') ||
                                  document.querySelector('[data-edb-sidepanel-card*="Details"]') ||
                                  document.querySelector('.workorder-details-card_kiboz'));
                    }""")
                    if is_details_open:
                        details_opened = True
                        print(f"  👉 Work Order details view opened successfully (Attempt {open_attempt + 1})!")
                        break

                    try:
                        chevron_loc = self.page.locator(
                            ".card_ngkhz:has(.workorder-summary-card_mv9VQ) .details-icon_lgpCm, "
                            ".card_ngkhz:has(.panel-title:has-text('Work Order')) .details-icon_lgpCm"
                        ).first
                        if await chevron_loc.count() > 0:
                            await chevron_loc.scroll_into_view_if_needed()
                            await self.page.wait_for_timeout(150)
                            await chevron_loc.click()
                        elif await wo_card_loc.count() > 0:
                            c_sub = wo_card_loc.locator(".details-icon_lgpCm, svg").first
                            if await c_sub.count() > 0:
                                await c_sub.click()
                    except Exception as c_err:
                        print(f"  ⚠️ Playwright chevron click note: {c_err}")

                    await self.page.evaluate("""() => {
                        const wo = document.querySelector('.card_ngkhz:has(.workorder-summary-card_mv9VQ)') ||
                                   document.querySelector('.workorder-summary-card_mv9VQ')?.closest('.card_ngkhz') ||
                                   Array.from(document.querySelectorAll('.card_ngkhz')).find(c => {
                                       const pt = c.querySelector('.panel-title, .field-title');
                                       const txt = (pt ? pt.textContent : c.textContent || '').trim();
                                       return txt.includes('Work Order') && !txt.toUpperCase().includes('CUSTOMER');
                                   });
                        if (!wo) return;
                        const icon = wo.querySelector('.details-icon_lgpCm') || wo.querySelector('.details-icon_lgpCm svg');
                        const header = wo.querySelector('.header_Ce6MY') || wo.querySelector('.header-content_GyDl_');
                        const target = icon || header;
                        if (target) {
                            target.scrollIntoView({ behavior: 'instant', block: 'center' });
                            target.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                            target.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                            target.click();
                        }
                    }""")

                    if open_attempt >= 1:
                        try:
                            if await wo_card_loc.count() > 0:
                                hdr_loc = wo_card_loc.locator(".header_Ce6MY, .panel-title").first
                                if await hdr_loc.count() > 0:
                                    await hdr_loc.click()
                        except Exception:
                            pass

                    for _ in range(6):
                        await self.page.wait_for_timeout(150)
                        is_details_open = await self.page.evaluate("""() => {
                            return !!(document.querySelector('.edit-icon_vG0FU') || 
                                      document.querySelector('.base-icon.edit.edit-icon_vG0FU') ||
                                      document.querySelector('[data-edb-sidepanel-card*="Details"]') ||
                                      document.querySelector('.workorder-details-card_kiboz'));
                        }""")
                        if is_details_open:
                            details_opened = True
                            break
                    if details_opened:
                        print("  👉 Details view is now open!")
                        break

                print(f"  👉 Details chevron clicked: {'Success' if details_opened else 'Warning: not detected'}")
                await self.page.wait_for_timeout(200)

                if not details_opened:
                    print("  ❌ Could not open Work Order details! Aborting Work Order update to avoid misclicking wrong section.")
                    await self.close_sidepanel()
                    return False

                print("  👉 Clicking Work Order edit icon (.edit-icon_vG0FU)...")
                edit_wo_clicked = False
                for _ in range(5):
                    is_in_edit = await self.page.evaluate("""() => {
                        return !!(document.querySelector('.workorder-details-card_kiboz input.vs__search, [data-edb-sidepanel-card*="WorkOrder"] input.vs__search, [data-edb-sidepanel-card*="WorkOrder"] textarea, .summary-note-container_f6fBE textarea, textarea.summary-note_Dk8T7'));
                    }""")
                    if is_in_edit:
                        edit_wo_clicked = True
                        break

                    try:
                        pw_edit = self.page.locator(
                            '.base-icon.edit.edit-icon_vG0FU, '
                            '.edit-icon_vG0FU, '
                            '[class*="edit-icon_vG0FU"], '
                            '.workorder-details-card_kiboz .base-icon.edit, '
                            '[data-edb-sidepanel-card*="WorkOrder"] .base-icon.edit'
                        ).first
                        if await pw_edit.count() > 0 and await pw_edit.is_visible():
                            await pw_edit.scroll_into_view_if_needed()
                            await pw_edit.click()
                            edit_wo_clicked = True
                            break
                    except Exception:
                        pass

                    edit_wo_clicked = await self.page.evaluate("""() => {
                        const btn = document.querySelector('.base-icon.edit.edit-icon_vG0FU') ||
                                    document.querySelector('.edit-icon_vG0FU') ||
                                    document.querySelector('[class*="edit-icon_vG0FU"]') ||
                                    document.querySelector('.workorder-details-card_kiboz .base-icon.edit') ||
                                    document.querySelector('[data-edb-sidepanel-card*="WorkOrder"] .base-icon.edit');
                        if (btn && btn.offsetParent !== null) {
                            btn.scrollIntoView({ behavior: 'instant', block: 'center' });
                            btn.click();
                            btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                            btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                            btn.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                            return true;
                        }
                        return false;
                    }""")
                    if edit_wo_clicked:
                        break

                    for _ in range(6):
                        await self.page.wait_for_timeout(150)
                        if await self.page.evaluate("""() => !!(document.querySelector('.workorder-details-card_kiboz input.vs__search, [data-edb-sidepanel-card*="WorkOrder"] input.vs__search, [data-edb-sidepanel-card*="WorkOrder"] textarea, .summary-note-container_f6fBE textarea, textarea.summary-note_Dk8T7'))"""):
                            edit_wo_clicked = True
                            break
                    if edit_wo_clicked:
                        break

                print(f"  👉 Work Order edit icon clicked: {'Success' if edit_wo_clicked else 'Not found'}")
                await self.page.wait_for_timeout(200)

                if not edit_wo_clicked:
                    print("  ❌ Could not enter Work Order edit mode! Aborting Work Order update.")
                    await self.close_sidepanel()
                    return False

                if need_priority_change and target_priority:
                    print(f"  🏷️ Selecting Priority: '{target_priority}'...")
                    prio_clicked = False

                    try:
                        prio_vs_loc = self.page.locator(
                            "[data-edb-sidepanel-card*='WorkOrder'] .cell:has(.field-title:has-text('Priority')) .v-select, "
                            "//div[contains(@class,'field-title') and contains(translate(., 'PRIORITY', 'priority'),'priority')]/following::div[contains(@class,'v-select')][1], "
                            ".workorder-details-card_kiboz .v-select"
                        ).first
                        if await prio_vs_loc.count() > 0 and await prio_vs_loc.is_visible():
                            await prio_vs_loc.scroll_into_view_if_needed()
                            await prio_vs_loc.click()
                    except Exception:
                        pass

                    await self.page.evaluate("""() => {
                        const allTitles = Array.from(document.querySelectorAll('.field-title, .title, .label, div'));
                        const pTitle = allTitles.find(t => (t.textContent || '').trim().toLowerCase() === 'priority');
                        let vs = null;
                        if (pTitle) {
                            const container = pTitle.closest('.cell') || pTitle.closest('.grid') || pTitle.parentElement;
                            if (container) vs = container.querySelector('.v-select');
                        }
                        if (!vs) {
                            vs = document.querySelector('.workorder-details-card_kiboz .v-select') ||
                                 document.querySelector('[data-edb-sidepanel-card*="WorkOrder"] .v-select');
                        }
                        if (vs) {
                            vs.scrollIntoView({ behavior: 'instant', block: 'center' });
                            const toggle = vs.querySelector('.vs__dropdown-toggle') || vs.querySelector('input.vs__search') || vs.querySelector('input') || vs;
                            toggle.click();
                            toggle.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                            toggle.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                        }
                    }""")
                    await self.page.wait_for_timeout(250)

                    prio_clicked = await self.page.evaluate("""(target) => {
                        const norm = (s) => (s || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g, '');
                        const cTarget = norm(target);
                        const isDnd = target.toUpperCase().includes('DO NOT DISPATCH') || target.includes('[4]');
                        const isSn = target.toUpperCase().includes('SERVICE NEEDED') || target.includes('7.12');

                        const items = Array.from(document.querySelectorAll('.vs__dropdown-menu li, li.vs__dropdown-option, ul[role="listbox"] li, .vs__dropdown-menu .vs__dropdown-option'));
                        for (const item of items) {
                            const txt = (item.textContent || '').trim();
                            const cTxt = norm(txt);

                            const exactMatch = cTxt === cTarget;
                            const subMatch = cTarget && (cTxt.includes(cTarget) || cTarget.includes(cTxt));
                            const dndMatch = isDnd && (txt.toUpperCase().includes('DO NOT DISPATCH') || txt.includes('[4]'));
                            const snMatch = isSn && (txt.toUpperCase().includes('SERVICE NEEDED') || txt.includes('7.12'));

                            if (exactMatch || subMatch || dndMatch || snMatch) {
                                item.scrollIntoView({ behavior: 'instant', block: 'nearest' });
                                item.click();
                                item.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                                return true;
                            }
                        }
                        return false;
                    }""", target_priority)

                    if not prio_clicked:
                        search_term = "DO NOT DISPATCH" if "DO NOT DISPATCH" in target_priority.upper() else ("SERVICE NEEDED" if "SERVICE NEEDED" in target_priority.upper() else target_priority)
                        try:
                            prio_inp = self.page.locator('input.vs__search:visible, input[type="search"]:visible').first
                            if await prio_inp.count() > 0 and await prio_inp.is_visible():
                                await prio_inp.fill(search_term)
                        except Exception:
                            pass

                        await self.page.evaluate("""(term) => {
                            const inps = Array.from(document.querySelectorAll('input.vs__search, input[type="search"]'));
                            for (const inp of inps) {
                                if (inp.offsetParent !== null) {
                                    inp.focus();
                                    inp.value = term;
                                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                                    break;
                                }
                            }
                        }""", search_term)
                        await self.page.wait_for_timeout(250)

                        prio_clicked = await self.page.evaluate("""(target) => {
                            const norm = (s) => (s || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g, '');
                            const cTarget = norm(target);
                            const isDnd = target.toUpperCase().includes('DO NOT DISPATCH') || target.includes('[4]');
                            const isSn = target.toUpperCase().includes('SERVICE NEEDED') || target.includes('7.12');

                            const items = Array.from(document.querySelectorAll('.vs__dropdown-menu li, li.vs__dropdown-option, ul[role="listbox"] li, .vs__dropdown-menu .vs__dropdown-option'));
                            for (const item of items) {
                                const txt = (item.textContent || '').trim();
                                const cTxt = norm(txt);

                                const exactMatch = cTxt === cTarget;
                                const subMatch = cTarget && (cTxt.includes(cTarget) || cTarget.includes(cTxt));
                                const dndMatch = isDnd && (txt.toUpperCase().includes('DO NOT DISPATCH') || txt.includes('[4]'));
                                const snMatch = isSn && (txt.toUpperCase().includes('SERVICE NEEDED') || txt.includes('7.12'));

                                if (exactMatch || subMatch || dndMatch || snMatch) {
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
                            pat = r'do\s*not\s*dispatch|\[4\]' if ('DO NOT DISPATCH' in target_priority.upper() or '[4]' in target_priority) else r'service\s*needed|7\.12'
                            opt_loc = self.page.locator('.vs__dropdown-menu li, ul[role="listbox"] li, li.vs__dropdown-option').filter(has_text=_re.compile(pat, _re.I)).first
                            if await opt_loc.count() > 0 and await opt_loc.is_visible():
                                await opt_loc.click(force=True)
                                prio_clicked = True
                        except Exception as p_err:
                            print(f"  ⚠️ Priority locator click fallback note: {p_err}")

                    if not prio_clicked:
                        try:
                            if 'prio_inp' in locals() and prio_inp and await prio_inp.count() > 0:
                                await prio_inp.fill("")
                                await self.page.wait_for_timeout(200)
                        except Exception:
                            pass
                        prio_clicked = await self.page.evaluate("""(target) => {
                            const isDnd = target.toUpperCase().includes('DO NOT DISPATCH') || target.includes('[4]');
                            const isSn = target.toUpperCase().includes('SERVICE NEEDED') || target.includes('7.12');
                            const items = Array.from(document.querySelectorAll('.vs__dropdown-menu li, li.vs__dropdown-option, ul[role="listbox"] li, .vs__dropdown-menu .vs__dropdown-option'));
                            for (const item of items) {
                                const txt = (item.textContent || '').trim();
                                const dndMatch = isDnd && (txt.toUpperCase().includes('DO NOT DISPATCH') || txt.includes('[4]'));
                                const snMatch = isSn && (txt.toUpperCase().includes('SERVICE NEEDED') || txt.includes('7.12'));
                                if (dndMatch || snMatch) {
                                    item.scrollIntoView({ behavior: 'instant', block: 'nearest' });
                                    item.click();
                                    item.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                                    return true;
                                }
                            }
                            return false;
                        }""", target_priority)

                    print(f"  -> Priority option selected: {'Success' if prio_clicked else 'Option not found'}")
                    await self.page.wait_for_timeout(200)
                else:
                    print(f"  🏷️ Priority already set to '{target_priority}'. Skipping priority selection.")

                if need_note_change and service_note:
                    print(f"  📝 Entering Work Summary: '{service_note}'...")
                    ta_filled = False
                    try:
                        ta_loc = self.page.locator(
                            '[data-edb-sidepanel-card*="WorkOrder"] textarea, '
                            '.summary-note-container_f6fBE:has-text("Work Summary") textarea, '
                            'textarea.summary-note_Dk8T7, '
                            'textarea[placeholder*="summary" i], '
                            'textarea'
                        ).first
                        if await ta_loc.count() > 0 and await ta_loc.is_visible():
                            await ta_loc.scroll_into_view_if_needed()
                            await ta_loc.fill(service_note)
                            ta_filled = True
                    except Exception:
                        pass

                    if not ta_filled:
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
                            if (!targetTa) {
                                targetTa = document.querySelector('[data-edb-sidepanel-card*="WorkOrder"] textarea') || document.querySelector('textarea');
                            }
                            if (targetTa) {
                                targetTa.focus();
                                targetTa.value = note;
                                targetTa.dispatchEvent(new Event('input', { bubbles: true }));
                                targetTa.dispatchEvent(new Event('change', { bubbles: true }));
                            }
                        }""", service_note)
                    await self.page.wait_for_timeout(200)
                else:
                    print(f"  📝 Work Summary already set. Skipping note edit.")

                print("  💾 Clicking Save on Work Order details...")
                wo_saved = False
                for _ in range(3):
                    try:
                        save_btn = self.page.locator(
                            '[data-edb-sidepanel-card*="WorkOrder"] button.confirm_I8LeR, '
                            '[data-edb-sidepanel-card*="WorkOrder"] button:has-text("Save"), '
                            '.workorder-details-card_kiboz button.confirm_I8LeR, '
                            '.workorder-details-card_kiboz button:has-text("Save"), '
                            'button.confirm_I8LeR, button:has-text("Save")'
                        ).first
                        if await save_btn.count() > 0 and await save_btn.is_visible():
                            await save_btn.scroll_into_view_if_needed()
                            await save_btn.click(force=True)
                            wo_saved = True
                            break
                    except Exception:
                        pass

                    wo_saved = await self.page.evaluate("""() => {
                        const form = document.querySelector('[data-edb-sidepanel-card*="WorkOrder"]') ||
                                     document.querySelector('.workorder-details-card_kiboz') || 
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
                    if wo_saved:
                        break
                    await self.page.wait_for_timeout(400)

                for _ in range(10):
                    await self.page.wait_for_timeout(150)
                    saved_done = await self.page.evaluate("""() => {
                        const btn = document.querySelector('[data-edb-sidepanel-card*="WorkOrder"] button.confirm_I8LeR') ||
                                    document.querySelector('.workorder-details-card_kiboz button.confirm_I8LeR');
                        return !btn || btn.offsetParent === null;
                    }""")
                    if saved_done:
                        break
                await self.page.wait_for_timeout(300)
                print(f"  🎉 Work Order save finished (Result: {'Saved' if wo_saved else 'Attempted'}).")

                print("  🔙 Clicking Back button (.back_EKQLl) to return to main sidepanel...")
                back_clicked = False
                for _ in range(4):
                    try:
                        back_btn = self.page.locator(
                            '.header_DUVwn .back_EKQLl, '
                            '.back_EKQLl, '
                            '[class*="back_EKQLl"], '
                            '.header_DUVwn div:has-text("back")'
                        ).first
                        if await back_btn.count() > 0 and await back_btn.is_visible():
                            await back_btn.scroll_into_view_if_needed()
                            await back_btn.click()
                            back_clicked = True
                            break
                    except Exception:
                        pass

                    back_clicked = await self.page.evaluate("""() => {
                        const back = document.querySelector('.header_DUVwn .back_EKQLl') ||
                                     document.querySelector('.back_EKQLl') ||
                                     document.querySelector('[class*="back_EKQLl"]') ||
                                     Array.from(document.querySelectorAll('.header_DUVwn div')).find(d => (d.textContent || '').trim().toLowerCase() === 'back');
                        if (back && back.offsetParent !== null) {
                            back.click();
                            back.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                            back.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                            back.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                            return true;
                        }
                        return false;
                    }""")
                    if back_clicked:
                        break
                    await self.page.wait_for_timeout(300)

                for _ in range(10):
                    await self.page.wait_for_timeout(150)
                    on_main = await self.page.evaluate("""() => {
                        return !!(document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                                  document.querySelector('.workorder-summary-card_mv9VQ'));
                    }""")
                    if on_main:
                        break
                await self.page.wait_for_timeout(300)
                print(f"  🔙 Back button clicked: {'Success' if back_clicked else 'Not found / already on main'}")
            else:
                print(f"  ⏭️ Work Order details already set (Priority: '{prio_to_check}'). Skipping Work Order details edit.")

            if need_assignment_update:
                print(f"  🕒 [Step 2/2] Updating Assignment card (Need tech: {need_tech_change}, Need time: {need_time_change})...")
                await self.page.evaluate("""() => {
                    const cards = Array.from(document.querySelectorAll('.card_ngkhz, [data-edb-sidepanel-card]'));
                    const card = cards.find(c => {
                        const attr = (c.getAttribute('data-edb-sidepanel-card') || '').toLowerCase();
                        if (attr.includes('assignment')) return true;
                        const pt = c.querySelector('.panel-title, .field-title');
                        return pt && (pt.textContent || '').trim().toLowerCase() === 'assignment';
                    }) || document.querySelector('.card_ngkhz.edit-mode-style_ABe0v') || document.querySelector('.card_ngkhz');
                    if (card) card.scrollIntoView({ behavior: 'instant', block: 'center' });
                }""")
                await self.page.wait_for_timeout(200)

                is_edit_mode = await self.page.evaluate("""() => {
                    const card = document.querySelector('.card_ngkhz.edit-mode-style_ABe0v') ||
                                 document.querySelector('.tech-dropdown_qufHC') ||
                                 document.querySelector('.timepicker-container[name="Scheduled Time"]');
                    return !!card;
                }""")

                if not is_edit_mode:
                    for _ in range(3):
                        assignment_edit_clicked = await self.page.evaluate("""() => {
                            const cards = Array.from(document.querySelectorAll('.card_ngkhz, [data-edb-sidepanel-card]'));
                            const card = cards.find(c => {
                                const attr = (c.getAttribute('data-edb-sidepanel-card') || '').toLowerCase();
                                if (attr.includes('assignment')) return true;
                                const pt = c.querySelector('.panel-title, .field-title');
                                return pt && (pt.textContent || '').trim().toLowerCase() === 'assignment';
                            }) || document.querySelector('.card_ngkhz');
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
                            pw_btn = self.page.locator(
                                '[data-edb-sidepanel-card*="AssignmentCard"] .header_IVnfD .base-icon.edit, '
                                '[data-edb-sidepanel-card*="Assignment"] .base-icon.edit.default, '
                                '[data-edb-sidepanel-card*="Assignment"] .header_IVnfD .base-icon.edit, '
                                '.card_ngkhz:has(.panel-title:has-text("Assignment")) .base-icon.edit'
                            ).first
                            if await pw_btn.count() > 0 and await pw_btn.is_visible():
                                await pw_btn.click(force=True)
                                break
                        except Exception:
                            pass
                        await self.page.wait_for_timeout(400)

                    for _ in range(8):
                        await self.page.wait_for_timeout(150)
                        if await self.page.evaluate("""() => !!(document.querySelector('.card_ngkhz.edit-mode-style_ABe0v') || document.querySelector('.tech-dropdown_qufHC') || document.querySelector('.timepicker-container[name="Scheduled Time"]'))"""):
                            break

                await self.page.wait_for_timeout(300)

                if target_tech and need_tech_change:
                    print(f"  👤 Updating technician to '{target_tech}'...")
                    await self.select_dropdown_option(None, target_tech)
                    await self.page.wait_for_timeout(300)
                else:
                    print(f"  👤 Technician already matches '{target_tech}'. Skipping tech edit.")

                if target_time and need_time_change:
                    print(f"  ⏰ Setting scheduled time to '{target_time}'...")
                    time_input_loc = self.page.locator(
                        '.card_ngkhz.edit-mode-style_ABe0v div[name="Scheduled Time"] input, '
                        '[data-edb-sidepanel-card*="Assignment"] div[name="Scheduled Time"] input, '
                        '.card_ngkhz.edit-mode-style_ABe0v .timepicker-container input, '
                        '[data-edb-sidepanel-card*="Assignment"] .timepicker-container input, '
                        '.cell:has(.field-title:has-text("scheduled time")) input, '
                        '.cell-pos-3x1 input, input[placeholder="Time"]'
                    ).first

                    if await time_input_loc.count() > 0 and await time_input_loc.is_visible():
                        try:
                            await time_input_loc.click(force=True)
                            await self.page.wait_for_timeout(150)
                            await time_input_loc.press("Control+A")
                            await time_input_loc.press("Backspace")
                            await time_input_loc.type(target_time, delay=50)
                            await self.page.keyboard.press("Enter")
                            await self.page.keyboard.press("Tab")
                        except Exception as t_err:
                            print(f"  ⚠️ Direct typing time note: {t_err}")

                    await self.page.evaluate("""(tVal) => {
                        const card = document.querySelector('.card_ngkhz.edit-mode-style_ABe0v') ||
                                     document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                                     document.querySelector('[data-edb-sidepanel-card*="Assignment"]') ||
                                     document.querySelector('.card_ngkhz');
                        if (!card) return false;
                        let inp = card.querySelector('div[name="Scheduled Time"] input, .timepicker-container input, .cell-pos-3x1 input, input[placeholder="Time"]');
                        if (!inp) {
                            const cells = Array.from(card.querySelectorAll('.cell'));
                            for (const c of cells) {
                                const ft = c.querySelector('.field-title');
                                if (ft && ft.textContent.toLowerCase().includes('scheduled time')) {
                                    inp = c.querySelector('input');
                                    if (inp) break;
                                }
                            }
                        }
                        if (inp) {
                            inp.focus();
                            inp.value = tVal;
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            inp.dispatchEvent(new Event('change', { bubbles: true }));
                            inp.dispatchEvent(new Event('blur', { bubbles: true }));
                            return true;
                        }
                        return false;
                    }""", target_time)
                    await self.page.wait_for_timeout(300)
                else:
                    print(f"  ⏰ Scheduled time already matches '{target_time}'. Skipping time edit.")

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
                    for _ in range(10):
                        await self.page.wait_for_timeout(150)
                        edit_done = await self.page.evaluate("""() => {
                            const editCard = document.querySelector('.card_ngkhz.edit-mode-style_ABe0v');
                            return !editCard;
                        }""")
                        if edit_done:
                            break
                    await self.page.wait_for_timeout(300)
                else:
                    print("  ⚠️ Assignment Save button not triggered.")
            else:
                print(f"  ⏭️ Assignment card already matches (Tech: '{tech_to_check}'). Skipping Assignment edit.")

            await self.close_sidepanel()
            return True

        except Exception as e:
            print(f"❌ Error updating service-needed WO #{work_order_number}: {e}")
            try:
                await self.close_sidepanel()
            except Exception:
                pass
            return False

    async def cancel_work_order_via_list(self, wo_number: str, reason: str = "") -> bool:
        if not wo_number:
            return False

        wo_str = str(wo_number).strip()
        print(f"\n🗑️ [Cancel WO #{wo_str}] Initiating cancellation via FieldEdge List (https://login.fieldedge.com/#/List/0)...")
        new_page = None
        wo_page = None

        try:
            browser_context = getattr(self, 'context', None) or (self.page.context if self.page else None)
            if not browser_context:
                print(f"  ❌ No browser context available for cancelling WO #{wo_str}.")
                return False
            new_page = await browser_context.new_page()
            list_url = "https://login.fieldedge.com/#/List/0"
            print(f"  🌐 [Cancel WO #{wo_str}] Navigating to {list_url}...")
            try:
                await new_page.goto(list_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as nav_err:
                print(f"  ⚠️ Navigation warning: {nav_err}")

            print(f"  🔍 [Cancel WO #{wo_str}] Waiting for search input...")
            search_input = new_page.locator("input#search, .custom-search input.search").first
            await search_input.wait_for(state="visible", timeout=30000)
            await new_page.wait_for_timeout(1000)

            clear_btn = new_page.locator("button.clear-search:visible")
            if await clear_btn.count() > 0:
                try:
                    await clear_btn.first.click()
                    await new_page.wait_for_timeout(500)
                except Exception:
                    pass

            await search_input.click()
            await search_input.fill("")
            await search_input.fill(wo_str)
            await search_input.press("Enter")
            print(f"  🔍 [Cancel WO #{wo_str}] Entered '{wo_str}' into search and pressed Enter. Waiting for results...")
            await new_page.wait_for_timeout(2500)

            target_row = None
            target_status = ""
            for attempt in range(12):
                rows = new_page.locator("table.fixed-table tbody.fixed-body tr.regular")
                row_count = await rows.count()
                for i in range(row_count):
                    r = rows.nth(i)
                    r_text = await r.inner_text()
                    if wo_str in r_text:
                        target_row = r
                        cells = r.locator("td")
                        if await cells.count() >= 4:
                            target_status = (await cells.nth(3).inner_text()).strip()
                        break
                if target_row:
                    break
                await new_page.wait_for_timeout(1000)

            if not target_row:
                print(f"  ⚠️ [Cancel WO #{wo_str}] No table row found matching WO #{wo_str} in search results.")
                return False

            print(f"  📋 [Cancel WO #{wo_str}] Found row in search results. Status: '{target_status}'.")
            if "complete" in target_status.lower():
                print(f"  🛑 [Cancel WO #{wo_str}] WO #{wo_str} is already '{target_status}'. Skipping cancellation to protect completed work order.")
                return False

            wo_page = new_page
            wo_span = target_row.locator(".display-name-cell").first
            if await wo_span.count() == 0:
                wo_span = target_row.locator("td:nth-child(2)").first

            print(f"  🖱️ [Cancel WO #{wo_str}] Opening work order details...")
            try:
                await wo_span.click()
                await new_page.wait_for_timeout(2000)
            except Exception as e1:
                print(f"  ℹ️ Direct click failed ({e1}). Trying double-click on row...")
                await target_row.dblclick()
                await new_page.wait_for_timeout(2000)

            save_btn_selector = self.rules.get(
                "unassigned_save_btn_selector",
                "button#dispatch-summary-save-workorder, button[data-automation-id='save-workorder-button']"
            )
            try:
                await wo_page.wait_for_selector(save_btn_selector, state="visible", timeout=20000)
            except Exception:
                print(f"  ⚠️ Save button not immediately visible for WO #{wo_str}, waiting 3s...")
                await wo_page.wait_for_timeout(3000)

            save_btn = wo_page.locator(save_btn_selector).first
            if await save_btn.count() == 0:
                print(f"  ❌ Save button not found on Work Order page for WO #{wo_str}.")
                return False

            print(f"  💾 Clicking Save button on WO #{wo_str}...")
            await save_btn.click()
            print("  ⏳ Waiting for Save / Dispatch Summary modal to appear...")
            await wo_page.wait_for_timeout(2000)

            radio_btn = self.rules.get("unassigned_canceled_radio_selector", ".field-radio-container:has-text('Canceled') .field-radio")
            notes_sel = self.rules.get("unassigned_notes_textarea_selector", "textarea[name='CancellationNotes']")
            confirm_btn_sel = self.rules.get("unassigned_confirm_btn_selector", "button#confirm-save-dispatch-summary, button:has-text('Confirm'), button:has-text('Yes'), button:has-text('Submit')")
            cancel_reason_text = reason or self.rules.get("unassigned_cancellation_reason", "Duplicate visual aid work order created by dispatch board automation.")

            print(f"  🔘 Selecting 'Canceled' radio in Save modal...")
            try:
                await wo_page.wait_for_selector(radio_btn, state="visible", timeout=7000)
                await wo_page.click(radio_btn)
            except Exception as inner_e:
                print(f"  ⚠️ Primary radio selector failed ({inner_e}), trying fallback selector...")
                alt_radio = ".field-radio-container:has-text('Canceled') .field-radio"
                try:
                    await wo_page.wait_for_selector(alt_radio, state="visible", timeout=5000)
                    await wo_page.click(alt_radio)
                except Exception:
                    await wo_page.locator("text='Canceled'").first.click()

            await wo_page.wait_for_timeout(1000)

            print(f"  📝 Entering cancellation reason: '{cancel_reason_text}'...")
            filled = False
            for target_sel in [notes_sel, "textarea[name='CancellationNotes']", "textarea[name='notes']", ".modal textarea", ".v-modal textarea", "textarea:visible"]:
                try:
                    await wo_page.wait_for_selector(target_sel, state="visible", timeout=3000)
                    await wo_page.fill(target_sel, cancel_reason_text)
                    print(f"  ✅ Cancellation reason entered successfully using '{target_sel}'.")
                    filled = True
                    break
                except Exception:
                    continue

            if not filled:
                print(f"  ⚠️ Retrying direct fill on '{notes_sel}'...")
                try:
                    await wo_page.fill(notes_sel, cancel_reason_text)
                    print("  ✅ Direct fill on cancellation notes succeeded.")
                except Exception as note_e:
                    print(f"  ❌ Could not fill cancellation notes: {note_e}")

            await wo_page.wait_for_timeout(1000)

            print(f"  ✅ Confirming cancellation for WO #{wo_str}...")
            try:
                confirm_btn = wo_page.locator("button#confirm-save-dispatch-summary:visible").first
                await confirm_btn.click(timeout=10000)
            except Exception:
                print("  -> Fallback confirm button click...")
                await wo_page.locator(f"{confirm_btn_sel} >> visible=true").first.click(timeout=10000)

            await wo_page.wait_for_timeout(4000)
            print(f"  🎉 Successfully canceled WO #{wo_str}!")
            return True

        except Exception as err:
            print(f"  ❌ Error cancelling WO #{wo_str}: {err}")
            return False

        finally:
            try:
                if wo_page and wo_page != new_page:
                    await wo_page.close()
            except Exception:
                pass
            try:
                if new_page:
                    await new_page.close()
            except Exception:
                pass
            try:
                if self.page:
                    await self.page.bring_to_front()
            except Exception:
                pass

    async def reconcile_single_date(
        self,
        date_str: str,
        click_wos: bool = True,
        status_name: Optional[str] = None,
        force_reconcile: bool = False
    ) -> Dict[str, Any]:
        try:
            dt = None
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d", "%m/%d/%y", "%m-%d-%y"):
                try:
                    dt = datetime.strptime(date_str.strip(), fmt)
                    break
                except ValueError:
                    pass

            normalized_date = self.format_date(date_str)
            db_date_key = dt.strftime("%Y-%m-%d") if dt else normalized_date

            history = self._load_history()

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
                print(f"\n🚫 [Stage 0/3] Checking {len(out_of_service_configs)} active Out of Service vehicle(s) on {normalized_date}...")
                for oos_cfg in out_of_service_configs:
                    asset_name = oos_cfg.get("asset_name", "")
                    target_time = oos_cfg.get("start_time", "1:45 PM")
                    target_priority = oos_cfg.get("priority", "[4] DO NOT DISPATCH (Tracking)")
                    service_note = oos_cfg.get("service_note") or oos_cfg.get("reason", "Vehicle is Out of Service for repairs.")
                    target_tech = oos_cfg.get("tech_name", "s4")

                    matching_row = self.find_asset_row(asset_name, all_rows, target_priority=target_priority, target_tech=target_tech)
                    if matching_row:
                        wo_num = matching_row.get("work_order_number", "")
                        curr_tech = matching_row.get("tech_name", "")
                        curr_sched = matching_row.get("scheduled_date", "")
                        curr_prio = matching_row.get("priority_name", "")

                        print(f"  👉 Found existing WO #{wo_num} for Out of Service '{asset_name}' on tech '{curr_tech}' (Sched: {curr_sched}, Priority: {curr_prio})")

                        is_tech_set = not target_tech or self.is_tech_match(curr_tech, target_tech) or (self.canonical_str(curr_tech) == self.canonical_str(target_tech))
                        is_time_set = not target_time or self.is_time_matching(target_time, curr_sched)
                        is_prio_set = not target_priority or self.is_priority_match(curr_prio, target_priority)

                        if is_tech_set and is_time_set and is_prio_set:
                            print(f"  ⏭️ WO #{wo_num} for Out of Service '{asset_name}' is ALREADY fully set (Tech: '{curr_tech}', Time: '{curr_sched}', Priority: '{curr_prio}'). Skipping sidepanel.")
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
                            current_priority=curr_prio,
                            stage_label="Out of Service Update"
                        )
                        if updated:
                            total_reconciled += 1
                    else:
                        print(f"  ⚠️ Could not find card for Out of Service vehicle '{asset_name}' on {normalized_date}.")

            if out_of_service_names and truck_headers:
                oos_on_tech = [
                    r for r in truck_headers
                    if (self.canonical_str(r.get("name", "")) in out_of_service_names
                        or self.canonical_str(r.get("asset_name", "")) in out_of_service_names)
                    and r.get("tech_name", "").lower() not in ("s4", "s3", "")
                ]
                for r in oos_on_tech:
                    wo_num = r.get("work_order_number")
                    t_name = r.get("name", "")
                    t_tech = r.get("tech_name", "")
                    if not r.get("is_completed", False) and click_wos and wo_num:
                        print(f"🚨 Out of Service vehicle '{t_name}' (WO #{wo_num}) is assigned to tech '{t_tech}'! Initiating auto-cancellation...")
                        canceled = await self.cancel_work_order_via_list(
                            wo_number=wo_num,
                            reason=f"Vehicle '{t_name}' is Out of Service; cannot be assigned to tech '{t_tech}'."
                        )
                        if canceled:
                            print(f"🗑️ Cancelled Out of Service vehicle WO #{wo_num} ({t_name}) on '{t_tech}'.")
                            total_reconciled += 1
                truck_headers = [
                    r for r in truck_headers
                    if self.canonical_str(r.get("name", "")) not in out_of_service_names
                    and self.canonical_str(r.get("asset_name", "")) not in out_of_service_names
                ]

            service_needed_configs = [
                s for s in self.template_data.get("service_needed_assets", [])
                if s.get("active", True)
            ]
            if service_needed_configs and click_wos:
                print(f"\n🔧 [Stage 1/3] Checking {len(service_needed_configs)} active service-needed assets on {normalized_date}...")
                for sn_cfg in service_needed_configs:
                    asset_name = sn_cfg.get("asset_name", "")
                    matched_truck = next((
                        t for t in self.template_data.get("truck_assign_headers", [])
                        if (self.canonical_str(t.get("name", "")) == self.canonical_str(asset_name) or
                            (re.findall(r'\d+', t.get("name", "")) and re.findall(r'\d+', asset_name) and
                             int(re.findall(r'\d+', t.get("name", ""))[0]) == int(re.findall(r'\d+', asset_name)[0])))
                    ), None)
                    is_assigned_to_tech = bool(
                        matched_truck and
                        matched_truck.get("active", True) and
                        matched_truck.get("tech_name") and
                        matched_truck.get("tech_name").strip().lower() not in ("", "s4", "s3")
                    )
                    if is_assigned_to_tech:
                        target_tech = matched_truck.get("tech_name")
                        target_time = matched_truck.get("start_time") or "06:00 AM"
                    else:
                        target_tech = sn_cfg.get("tech_name", "s4")
                        target_time = sn_cfg.get("start_time") or "06:00 AM"
                    target_priority = sn_cfg.get("priority", "[7] 7.12 SERVICE NEEDED")
                    service_note = sn_cfg.get("service_note", "")

                    if is_assigned_to_tech:
                        s4_dupes = []
                        c_a_name = self.canonical_str(asset_name)
                        nums_a = re.findall(r'\d+', asset_name)
                        for r in all_rows:
                            r_cust = (r.get("customer_name") or "").strip()
                            r_tech = (r.get("tech_name") or "").strip().lower()
                            r_wo = r.get("work_order_number")
                            if r_tech in ("s4", "s3", "") and r_wo:
                                c_r_cust = self.canonical_str(r_cust)
                                nums_r = re.findall(r'\d+', r_cust)
                                num_match = bool(nums_r and nums_a and int(nums_r[0]) == int(nums_a[0]))
                                if c_r_cust == c_a_name or num_match:
                                    s4_dupes.append(r)

                        for dupe_r in s4_dupes:
                            dupe_wo = dupe_r.get("work_order_number")
                            print(f"🗑️ Found duplicate service needed WO #{dupe_wo} for '{asset_name}' on s4. Asset is assigned to tech '{target_tech}'. Cancelling duplicate...")
                            canceled = await self.cancel_work_order_via_list(
                                wo_number=dupe_wo,
                                reason=f"Duplicate service needed asset '{asset_name}' on s4; asset is assigned to technician '{target_tech}'."
                            )
                            if canceled:
                                all_rows = [x for x in all_rows if str(x.get("work_order_number")) != str(dupe_wo)]
                                total_reconciled += 1

                    matching_row = self.find_asset_row(asset_name, all_rows, target_priority=target_priority, target_tech=target_tech)
                    if matching_row:
                        wo_num = matching_row.get("work_order_number", "")
                        curr_tech = matching_row.get("tech_name", "")
                        curr_sched = matching_row.get("scheduled_date", "")
                        curr_prio = matching_row.get("priority_name", "")

                        print(f"  👉 Found existing WO #{wo_num} for '{asset_name}' on tech '{curr_tech}' (Sched: {curr_sched}, Priority: {curr_prio})")

                        is_tech_set = not target_tech or self.is_tech_match(curr_tech, target_tech) or (self.canonical_str(curr_tech) == self.canonical_str(target_tech))
                        is_time_set = not target_time or self.is_time_matching(target_time, curr_sched)
                        is_prio_set = not target_priority or self.is_priority_match(curr_prio, target_priority)

                        if is_tech_set and is_time_set and is_prio_set:
                            print(f"  ⏭️ WO #{wo_num} for Service Needed '{asset_name}' is ALREADY fully set (Tech: '{curr_tech}', Time: '{curr_sched}', Priority: '{curr_prio}'). Skipping sidepanel.")
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

            if truck_headers:
                print(f"\n🚛 [Stage 2/2] Standard Truck Reconciliation: Found {len(truck_headers)} truck_assign_headers on {normalized_date}:")
                for r in truck_headers:
                    try:
                        print(f"  - {r['name']} -> Current Tech: {r['tech_name']} | WO #{r['work_order_number']}")
                    except Exception:
                        safe_name = str(r.get('name', '')).encode('ascii', errors='replace').decode()
                        safe_tech = str(r.get('tech_name', '')).encode('ascii', errors='replace').decode()
                        print(f"  - {safe_name} -> Current Tech: {safe_tech} | WO #{r.get('work_order_number')}")

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

                        matched_found = False
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
                            if is_match and not matched_found:
                                matched_found = True
                                print(f"  ✅ Truck '{t_name}' (WO #{wo_num}) matches JSON template for '{tech_display}'.")
                            else:
                                print(f"  ℹ️ Truck '{t_name}' (WO #{wo_num}) on '{tech_display}' does not match expected '{expected_truck_name}'. Initiating cancellation...")
                                if click_wos and wo_num:
                                    success = await self.cancel_work_order_via_list(
                                        wo_number=wo_num,
                                        reason=f"Extra/mismatched truck '{t_name}' on tech '{tech_display}' (expected '{expected_truck_name}')."
                                    )
                                    if success:
                                        print(f"  🗑️ Cancelled extra/mismatched truck WO #{wo_num} ({t_name}) on '{tech_display}'.")
                                        truck_headers = [h for h in truck_headers if str(h.get("work_order_number")) != str(wo_num)]
                                        total_reconciled += 1
                                    else:
                                        print(f"  ⚠️ Could not cancel WO #{wo_num} ({t_name}) on '{tech_display}'. Preserving.")
                                else:
                                    print(f"  ℹ️ Cancellation skipped (click_wos={click_wos}, wo_num={wo_num}).")

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
                                print(f"  ℹ️ WO #{wo_num} for '{truck_display}' is on '{t_tech}'.")

                if click_wos:
                    print(f"\n🖱️ Reconciling {len(truck_headers)} truck work orders with Dashboard config for {normalized_date}...")
                    for idx, r in enumerate(truck_headers, 1):
                        wo_num = r.get("work_order_number")
                        truck_name = r.get("name")
                        curr_tech_table = r.get("tech_name", "")
                        is_completed = r.get("is_completed", False)

                        if self.is_item_turned_off_in_dashboard(truck_name, curr_tech_table):
                            if not is_completed:
                                print(f"[{idx}/{len(truck_headers)}] 🚨 Truck '{truck_name}' (WO #{wo_num} on tech '{curr_tech_table}') is turned OFF / Out of Service in Dashboard template! Initiating auto-cancellation...")
                                if click_wos and wo_num:
                                    canceled = await self.cancel_work_order_via_list(
                                        wo_number=wo_num,
                                        reason=f"Vehicle '{truck_name}' is turned OFF / Out of Service in Dashboard template."
                                    )
                                    if canceled:
                                        print(f"[{idx}/{len(truck_headers)}] 🗑️ Cancelled turned-off truck WO #{wo_num} ({truck_name}) on '{curr_tech_table}'.")
                                        total_reconciled += 1
                                        continue
                            continue

                        expected_tech = self.get_expected_tech_for_truck(truck_name)

                        if not expected_tech:
                            print(f"[{idx}/{len(truck_headers)}] Truck '{truck_name}' is inactive or has no expected tech in Dashboard config. Skipping.")
                            continue

                        if wo_num:
                            if curr_tech_table and expected_tech and self.is_tech_match(curr_tech_table, expected_tech):
                                print(f"[{idx}/{len(truck_headers)}] ✅ Truck '{truck_name}' (WO #{wo_num}) is ALREADY assigned to '{curr_tech_table}' (matches '{expected_tech}'). Skipping sidepanel.")
                                continue

                            expected_tech_already_has_it = False
                            for other_r in truck_headers:
                                if other_r.get("work_order_number") != wo_num:
                                    other_truck = self.canonical_str(other_r.get("name", ""))
                                    other_tech = other_r.get("tech_name", "")
                                    if other_truck == self.canonical_str(truck_name) and self.is_tech_match(other_tech, expected_tech):
                                        expected_tech_already_has_it = True
                                        break

                            if expected_tech_already_has_it:
                                print(f"[{idx}/{len(truck_headers)}] ℹ️ Truck '{truck_name}' (WO #{wo_num}) is on '{curr_tech_table}', but expected tech '{expected_tech}' ALREADY has '{truck_name}'. Initiating cancellation of duplicate WO...")
                                if click_wos and wo_num:
                                    success = await self.cancel_work_order_via_list(
                                        wo_number=wo_num,
                                        reason=f"Duplicate truck '{truck_name}' on '{curr_tech_table}' (expected tech '{expected_tech}' already has it)."
                                    )
                                    if success:
                                        print(f"[{idx}/{len(truck_headers)}] 🗑️ Cancelled duplicate truck WO #{wo_num} ({truck_name}) on '{curr_tech_table}'.")
                                        total_reconciled += 1
                                        continue
                                    else:
                                        print(f"[{idx}/{len(truck_headers)}] ⚠️ Could not cancel duplicate WO #{wo_num}. Preserving.")
                                        continue
                                else:
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
        num_days = 0

        try:
            await self.initialize()
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
            _elapsed = time.time() - _start_time
            if hasattr(self, "log_finish"):
                try:
                    await self.log_finish(
                        status="error" if _error_occurred else "success",
                        error_message=_error_occurred,
                        records_processed=total_checked,
                        elapsed_seconds=_elapsed,
                        details={
                            "reassignments": total_reconciled,
                            "total_checked": total_checked,
                            "days_processed": num_days,
                        }
                    )
                except Exception as log_ex:
                    print(f"⚠️ Error logging finish in Asset Assignment: {log_ex}")
            await self.cleanup()
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
