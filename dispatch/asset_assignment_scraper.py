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
            status_locators = [
                self.rules.get('status_assigned_xpath'),
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
            ]
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
                            purchase_status: purchaseStatus
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

        # Load active truck headers from template configuration
        template_trucks = [
            h for h in self.template_data.get("truck_assign_headers", [])
            if h.get("active", True)
        ]

        # Extract configured tech names and truck names for matching
        expected_tech_map = {}   # canonical_tech -> truck config
        expected_truck_map = {}  # canonical_truck_name / number -> truck config

        for h in template_trucks:
            t_name = (h.get("name") or "").strip()
            tech_name = (h.get("tech_name") or "").strip()
            if t_name:
                c_name = self.canonical_str(t_name)
                expected_truck_map[c_name] = h
                num_m = re.search(r'\d+', t_name)
                if num_m:
                    expected_truck_map[num_m.group(0)] = h
            if tech_name:
                c_tech = self.canonical_str(tech_name)
                expected_tech_map[c_tech] = h

        print(f"📋 Loaded {len(template_trucks)} active truck configs from template (Techs: {[h.get('tech_name') for h in template_trucks]}).")

        for r in rows:
            cust = r.get("customer_name", "").strip()
            prio = r.get("priority_name", "").strip()
            tech = r.get("tech_name", "").strip()
            wo_num = r.get("work_order_number", "").strip()

            # Keep s4, s3, and empty tech exclusion
            if tech.lower() in ["s4", "s3", ""]:
                continue

            c_cust = self.canonical_str(cust)
            c_tech = self.canonical_str(tech)

            matched_cfg = None

            # 1. Match by customer name against configured truck names/numbers
            for c_tname, cfg in expected_truck_map.items():
                if c_tname and (c_tname == c_cust or c_tname in c_cust or (len(c_tname) >= 2 and c_cust in c_tname)):
                    matched_cfg = cfg
                    break

            # 2. Match by tech name if tech matches one of the techs in truck_assign_headers
            if not matched_cfg and c_tech in expected_tech_map:
                cfg = expected_tech_map[c_tech]
                is_wo_truck = (
                    "🚛" in cust or "🛻" in cust or "|" in cust or
                    "Asset Assignment" in prio or "PT" in prio or "SER TR" in prio
                )
                if is_wo_truck:
                    matched_cfg = cfg

            # 3. Fallback: General truck indicators
            if not matched_cfg and ("🚛" in cust or "🛻" in cust or "|" in cust) and (
                "Asset Assignment" in prio or "PT" in prio or "SER TR" in prio
            ):
                matched_cfg = {
                    "name": cust,
                    "tech_name": tech,
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

                truck_headers.append({
                    "name": canonical_truck_name,
                    "active": True,
                    "department": dept,
                    "priority": prio or matched_cfg.get("priority", ""),
                    "start_time": start_time,
                    "duration": duration,
                    "tech_name": tech,
                    "work_order_number": wo_num,
                    "scheduled_date": sched_str,
                    "asset_name": r.get("asset_name") or matched_cfg.get("name", "")
                })
                print(f"  🎯 Matched truck WO #{wo_num}: '{canonical_truck_name}' (Current Tech: '{tech}', Target Tech: '{matched_cfg.get('tech_name')}')")

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

    def get_expected_tech_for_truck(self, truck_name: str) -> Optional[str]:
        c_target = self.canonical_str(truck_name)
        for h in self.template_data.get("truck_assign_headers", []):
            if not h.get("active", True):
                continue
            h_name = h.get("name", "")
            c_hname = self.canonical_str(h_name)
            if c_hname == c_target or (c_target and c_target in c_hname) or (c_hname and c_hname in c_target):
                return h.get("tech_name")
        return None

    def get_expected_truck_for_tech(self, tech_name: str) -> Optional[Dict[str, Any]]:
        c_tech = self.canonical_str(tech_name)
        for h in self.template_data.get("truck_assign_headers", []):
            if not h.get("active", True):
                continue
            h_tech = h.get("tech_name", "")
            c_htech = self.canonical_str(h_tech)
            if c_htech == c_tech or (c_tech and c_tech in c_htech) or (c_htech and c_htech in c_tech):
                return h
        return None

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

        print(f"  🔍 Selecting tech '{option_text}' in Assignment card dropdown...")

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

            await self.page.evaluate("""(text) => {
                const input = document.querySelector('.assigned-tech_omVmC input.vs__search') || 
                              document.querySelector('.tech-dropdown_qufHC input') ||
                              document.querySelector('.assigned-tech_omVmC input') ||
                              document.querySelector('[data-edb-sidepanel-card*="Assignment"] input.vs__search');
                if (input) {
                    input.focus();
                    input.value = text;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""", option_text)
            await self.page.wait_for_timeout(600)

            target_canon = self.canonical_str(option_text)
            clicked = await self.page.evaluate("""(args) => {
                const { targetText, targetCanon } = args;
                const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');

                // Dropdown menu options can be inside .assigned-tech_omVmC, in body, or in .vs__dropdown-menu
                const items = Array.from(document.querySelectorAll('.assigned-tech_omVmC ul[role="listbox"] li, .tech-dropdown_qufHC ul li, .vs__dropdown-menu li, ul[role="listbox"] li, li.vs__dropdown-option'));
                if (!items.length) return false;

                // Priority 1: Exact or canonical match
                for (const item of items) {
                    const txt = (item.textContent || item.innerText || '').trim();
                    const cTxt = norm(txt);
                    if (cTxt === targetCanon || (targetCanon && cTxt.includes(targetCanon)) || (cTxt && targetCanon.includes(cTxt))) {
                        item.scrollIntoView({ behavior: 'instant', block: 'nearest' });
                        item.click();
                        item.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                        item.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                        item.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        return true;
                    }
                }

                // Priority 2: Text inclusion
                const lowerTarget = targetText.toLowerCase().trim();
                for (const item of items) {
                    const txt = (item.textContent || item.innerText || '').toLowerCase().trim();
                    if (txt.includes(lowerTarget) || lowerTarget.includes(txt)) {
                        item.scrollIntoView({ behavior: 'instant', block: 'nearest' });
                        item.click();
                        item.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                        item.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                        item.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        return true;
                    }
                }

                // Fallback: Click first option if list has items
                if (items.length > 0) {
                    items[0].scrollIntoView({ behavior: 'instant', block: 'nearest' });
                    items[0].click();
                    return true;
                }

                return false;
            }""", {"targetText": option_text, "targetCanon": target_canon})

            print(f"  -> Tech dropdown selection for '{option_text}': {'Success' if clicked else 'Failed'}")
            await self.page.wait_for_timeout(600)
            return clicked
        except Exception as e:
            print(f"⚠️ Error selecting '{option_text}' in dropdown: {e}")
            return False

    async def click_and_open_sidepanel(self, work_order_number: str) -> bool:
        try:
            is_panel_open = await self.page.evaluate("""() => {
                const p = document.querySelector('.container__Gkig, .panel-header-container_EYC6s');
                return !!p && p.offsetParent !== null;
            }""")
            if is_panel_open:
                await self.close_sidepanel()
                await self.page.wait_for_timeout(800)

            target_xpaths = [
                f"//span[contains(@class, 'dispatch-name') and normalize-space()='{work_order_number}']",
                f"//span[contains(@class, 'dispatch-name') and @title='{work_order_number}']",
                f"//td[contains(@class, 'cell-clickable')]//span[normalize-space()='{work_order_number}']",
                f"//span[normalize-space()='{work_order_number}']",
                f"//td[contains(., '{work_order_number}')]"
            ]

            clicked = False
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
                print(f"⚠️ Could not click work order '{work_order_number}'")
                return False

            print(f"👉 Clicked WO #{work_order_number}, waiting for Assignment sidepanel...")
            try:
                await self.page.wait_for_selector('.container__Gkig, .custom-scrollbar', state='attached', timeout=8000)
            except Exception:
                await self.page.wait_for_timeout(2000)

            return True
        except Exception as e:
            print(f"⚠️ Error opening sidepanel for WO #{work_order_number}: {e}")
            return False

    async def cancel_work_order_via_dispatch_summary(
        self,
        work_order_number: str,
        truck_name: str = "",
        tech_name: str = "",
        reason: Optional[str] = None
    ) -> bool:
        """
        Open WO in sidepanel, click 'Go to Work Order' to open DispatchSummary in new tab,
        click Save, select 'Canceled' radio in modal, enter reason, and confirm cancellation.
        Used for duplicate truck assignments or when a vehicle is marked Out of Service.
        """
        log_label = f"Out of Service: '{truck_name}'" if reason and "out of service" in reason.lower() else f"Cancel Truck: '{truck_name}'"
        print(f"\n🗑️ [{log_label}] Processing WO #{work_order_number} on tech '{tech_name}'...")
        try:
            if not await self.click_and_open_sidepanel(work_order_number):
                return False

            await self.page.wait_for_timeout(1500)

            # Locate 'Go to Work Order' link in sidepanel footer
            goto_locators = [
                '.footer_CRgJf a:has-text("Go to Work Order")',
                'a:has-text("Go to Work Order")',
                '.arrow-link-container_U04bJ a.panel-link_qAFWh',
                '.arrow-link-container_U04bJ a',
                'a[href*="DispatchSummary"]',
                '.footer_CRgJf a'
            ]

            goto_btn = None
            for g_loc in goto_locators:
                loc = self.page.locator(g_loc).first
                if await loc.count() > 0 and await loc.is_visible():
                    goto_btn = loc
                    break

            if not goto_btn:
                print(f"  ⚠️ 'Go to Work Order' link not found in sidepanel for WO #{work_order_number}")
                await self.close_sidepanel()
                return False

            print(f"  ↗️ Clicking 'Go to Work Order' for WO #{work_order_number} to open in new tab...")
            new_page = None
            try:
                async with self.page.context.expect_page(timeout=10000) as new_page_info:
                    await goto_btn.click()
                new_page = await new_page_info.value
            except Exception as nav_e:
                href = await goto_btn.get_attribute("href")
                if href:
                    base_url = self.page.url.split("#")[0].rstrip("/")
                    full_url = f"{base_url}/{href.lstrip('/')}" if not href.startswith("http") else href
                    new_page = await self.page.context.new_page()
                    await new_page.goto(full_url, wait_until="domcontentloaded")
                else:
                    raise nav_e

            await new_page.wait_for_load_state("domcontentloaded")
            await new_page.wait_for_timeout(3000)

            # In the new tab, wait for Save button and click
            save_btn_sel = self.rules.get(
                "unassigned_save_btn_selector",
                "button#dispatch-summary-save-workorder, button[data-automation-id='save-workorder-button'], button:has-text('Save')"
            )
            print("  💾 Clicking Save button on DispatchSummary page...")
            await new_page.wait_for_selector(save_btn_sel, state="visible", timeout=15000)
            await new_page.locator(save_btn_sel).first.click()

            # Wait for Save modal to appear
            await new_page.wait_for_timeout(2000)

            # Select 'Canceled' radio button
            radio_btn_sel = self.rules.get(
                "unassigned_canceled_radio_selector",
                ".field-radio-container:has-text('Canceled') .field-radio"
            )
            print("  🔘 Selecting 'Canceled' radio in Save modal...")
            try:
                await new_page.wait_for_selector(radio_btn_sel, state="visible", timeout=6000)
                await new_page.locator(radio_btn_sel).first.click()
            except Exception:
                alt_radio = ".field-radio-container:has-text('Canceled') .field-radio, input[value='Canceled']"
                await new_page.locator(alt_radio).first.click()

            # Enter cancellation reason into textarea
            notes_sel = self.rules.get(
                "unassigned_notes_textarea_selector",
                "textarea[name='CancellationNotes']"
            )
            cancel_notes = reason or self.rules.get(
                "unassigned_cancellation_reason",
                "Duplicate truck assignment on technician (not assigned in template)."
            )
            print(f"  ✏️ Entering cancellation reason: '{cancel_notes}'...")
            try:
                await new_page.fill(notes_sel, cancel_notes)
            except Exception as note_e:
                print(f"  Note on textarea fill: {note_e}")

            # Confirm cancellation
            confirm_btn_sel = self.rules.get(
                "unassigned_confirm_btn_selector",
                "button#confirm-save-dispatch-summary, button:has-text('Confirm'), button:has-text('Yes')"
            )
            print("  ✅ Confirming Save and Cancellation...")
            try:
                await new_page.locator("button#confirm-save-dispatch-summary:visible").first.click(timeout=10000)
            except Exception:
                await new_page.locator(f"{confirm_btn_sel} >> visible=true").first.click(timeout=10000)

            await new_page.wait_for_timeout(4000)
            await new_page.close()
            print(f"🎉 Successfully canceled duplicate truck WO #{work_order_number} on '{tech_name}' and closed tab.")

            await self.close_sidepanel()
            return True

        except Exception as e:
            print(f"❌ Error canceling duplicate truck WO #{work_order_number}: {e}")
            try:
                await self.close_sidepanel()
            except Exception:
                pass
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

            c_curr = self.canonical_str(current_tech)
            c_exp = self.canonical_str(target_expected_tech)

            if c_curr and c_exp and (c_curr == c_exp or c_exp in c_curr or c_curr in c_exp):
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
        if not asset_name or not rows:
            return None
        import re
        c_target = self.canonical_str(asset_name)
        m = re.search(r'\d+', asset_name)
        target_num = m.group(0) if m else ""

        for r in rows:
            cust = r.get("customer_name", "")
            c_cust = self.canonical_str(cust)
            r_asset = self.canonical_str(r.get("asset_name", ""))

            # 1. Exact or substring match on canonical name
            if c_target and (c_target == c_cust or c_target == r_asset or c_target in c_cust or c_cust in c_target):
                return r

            # 2. Number match with vehicle icon/pipe match
            if target_num:
                m_cust = re.search(r'\d+', cust)
                if m_cust and m_cust.group(0) == target_num:
                    if ("🛻" in asset_name and "🛻" in cust) or ("🚛" in asset_name and "🚛" in cust) or ("|" in cust):
                        return r
        return None

    def is_time_matching(self, target_time: str, sched_str: str) -> bool:
        if not target_time or not sched_str:
            return False
        m_target = re.search(r'(\d{1,2}):(\d{2})\s*(am|pm)', target_time, re.IGNORECASE)
        if m_target:
            th = int(m_target.group(1))
            tm = m_target.group(2)
            tp = m_target.group(3).lower()

            times_in_sched = re.findall(r'(\d{1,2}):(\d{2})\s*(am|pm)', sched_str, re.IGNORECASE)
            for sh, sm, sp in times_in_sched:
                if int(sh) == th and sm == tm and sp.lower() == tp:
                    return True

        norm_target = re.sub(r'[^0-9a-z]', '', target_time.lower())
        norm_sched = re.sub(r'[^0-9a-z]', '', sched_str.lower())
        return bool(norm_target and norm_target in norm_sched)

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
           - If tech is not target_tech (e.g. s4), edit tech.
           - If time is not target_time (e.g. 3:30 PM), edit time.
           - Save Assignment card if changed.
        3. In Work Order card:
           - Click arrow (.details-icon_lgpCm) to view Work Order details.
           - Click edit icon (.base-icon.edit.edit-icon_vG0FU).
           - Set Priority to target_priority ([7] 7.12 SERVICE NEEDED).
           - Set Work Order Description to service_note.
           - Click Save (button.button_a3g51.confirm_I8LeR).
        4. Close sidepanel.
        """
        print(f"\n🛠️ [Service Needed Update] Processing WO #{work_order_number} ('{asset_name}')...")
        try:
            if not await self.click_and_open_sidepanel(work_order_number):
                return False

            await self.page.wait_for_timeout(1500)

            # ── Step 1: Update Assignment card (Time & Tech) ──
            need_tech_change = bool(target_tech and self.canonical_str(current_tech) != self.canonical_str(target_tech))
            need_time_change = not self.is_time_matching(target_time, current_sched)

            assignment_edited = False
            if not need_tech_change and not need_time_change:
                print(f"  ⏭️ Assignment card already set (Time: '{target_time}', Tech: '{target_tech}'). Skipping Assignment edit.")
            else:
                print(f"  🕒 Updating Assignment card (Need tech: {need_tech_change}, Need time: {need_time_change})...")
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
                                 document.querySelector('.timepicker-container[name="Scheduled Time"]');
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

                # Update Scheduled Time only if needed
                if need_time_change:
                    print(f"  ⏰ Setting scheduled time to '{target_time}'...")
                    time_input_loc = self.page.locator(
                        'div[name="Scheduled Time"] input, .timepicker-container input, .cell:has(.field-title:has-text("scheduled time")) input, .cell-pos-3x1 input'
                    ).first

                    if await time_input_loc.count() > 0 and await time_input_loc.is_visible():
                        try:
                            await time_input_loc.click(force=True)
                            await self.page.wait_for_timeout(200)
                            await time_input_loc.press("Control+A")
                            await time_input_loc.press("Backspace")
                            await time_input_loc.type(target_time, delay=50)
                            await self.page.keyboard.press("Enter")
                            await self.page.keyboard.press("Tab")
                        except Exception as t_err:
                            print(f"  ⚠️ Direct typing time note: {t_err}")

                    # Also ensure via JavaScript evaluation that value and events are dispatched
                    await self.page.evaluate("""(tVal) => {
                        const card = document.querySelector('.card_ngkhz.edit-mode-style_ABe0v') ||
                                     document.querySelector('[data-edb-sidepanel-card*="AssignmentCard"]') ||
                                     document.querySelector('.card_ngkhz');
                        if (!card) return false;
                        const inp = card.querySelector('div[name="Scheduled Time"] input') ||
                                    card.querySelector('.timepicker-container input') ||
                                    card.querySelector('.cell-pos-3x1 input');
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
                    await self.page.wait_for_timeout(600)
                else:
                    print(f"  ⏰ Scheduled time already matches '{target_time}'. Skipping time edit.")

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
                    assignment_edited = True
                else:
                    print("  ⚠️ Assignment Save button not triggered.")

            # ── Step 2: Update Work Order card (Priority & Description) ──
            print(f"  📝 Updating Work Order details: Priority='{target_priority}', Note='{service_note}'...")

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

            # 1. Select Priority
            if target_priority:
                print(f"  🏷️ Selecting Priority: '{target_priority}'...")
                await self.page.evaluate("""() => {
                    const dd = document.querySelector('.priority-select');
                    if (dd) {
                        const toggle = dd.querySelector('.vs__dropdown-toggle') || dd.querySelector('input.vs__search') || dd;
                        toggle.click();
                        toggle.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
                        toggle.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
                    }
                }""")
                await self.page.wait_for_timeout(500)

                await self.page.evaluate("""(target) => {
                    const inp = document.querySelector('.priority-select input.vs__search');
                    if (inp) {
                        inp.focus();
                        inp.value = target;
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }""", target_priority)
                await self.page.wait_for_timeout(500)

                prio_clicked = await self.page.evaluate("""(target) => {
                    const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
                    const cTarget = norm(target);
                    const items = Array.from(document.querySelectorAll('.priority-select ul[role="listbox"] li, .vs__dropdown-menu li, li.vs__dropdown-option, ul[role="listbox"] li'));
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
                    if (items.length > 0) {
                        items[0].click();
                        return true;
                    }
                    return false;
                }""", target_priority)
                print(f"  -> Priority option selected: {'Success' if prio_clicked else 'Option not found'}")
                await self.page.wait_for_timeout(500)

            # 2. Enter Work Summary
            if service_note:
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
        status_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Reconcile asset assignment work orders for a single date.
        Can run standalone or be invoked from Dispatch Board Display Automation using an existing page.
        """
        try:
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

            # Weekend skip check
            dt = None
            for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d", "%m/%d/%y", "%m-%d-%y"):
                try:
                    dt = datetime.strptime(date_str.strip(), fmt)
                    break
                except ValueError:
                    pass

            # Fetch fresh template configuration from Dashboard API so latest changes are always live
            self.template_data = self._load_template()

            skip_weekends = self.template_data.get("skip_vehicle_work_orders_on_weekends", False)
            if dt and skip_weekends and dt.weekday() in (5, 6):
                print(f"⏭️ Skipping weekend date {date_str} (Saturday/Sunday) for asset reconciliation.")
                return {"truck_assign_headers": [], "count": 0, "reassignments": 0, "checked": 0}

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

            # ── 0. Reconcile Out of Service Vehicles (Cancel WOs & Exempt from Board) ──
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
                oos_canceled_wos = set()
                for oos_cfg in out_of_service_configs:
                    asset_name = oos_cfg.get("asset_name", "")
                    reason = oos_cfg.get("reason", "Vehicle is Out of Service for repairs.")
                    c_target = self.canonical_str(asset_name)

                    # Look for any rows matching this vehicle in all_rows
                    matching_rows = []
                    for r in all_rows:
                        cust = self.canonical_str(r.get("customer_name", ""))
                        r_asset = self.canonical_str(r.get("asset_name", ""))
                        r_name = self.canonical_str(r.get("name", ""))
                        if c_target and (c_target == cust or c_target == r_asset or c_target == r_name or c_target in cust or cust in c_target):
                            matching_rows.append(r)

                    for m_row in matching_rows:
                        wo_num = m_row.get("work_order_number", "")
                        curr_tech = m_row.get("tech_name", "")
                        if wo_num and wo_num not in oos_canceled_wos:
                            print(f"  🚨 Vehicle '{asset_name}' is OUT OF SERVICE but active on board! (WO #{wo_num} on tech '{curr_tech}'). Canceling...")
                            cancel_reason = f"Vehicle '{asset_name}' is OUT OF SERVICE for repairs. {reason}".strip()
                            canceled = await self.cancel_work_order_via_dispatch_summary(
                                work_order_number=wo_num,
                                truck_name=asset_name,
                                tech_name=curr_tech,
                                reason=cancel_reason
                            )
                            if canceled:
                                oos_canceled_wos.add(wo_num)
                                total_reconciled += 1

                if oos_canceled_wos:
                    all_rows = [r for r in all_rows if r.get("work_order_number") not in oos_canceled_wos]
                    truck_headers = [r for r in truck_headers if r.get("work_order_number") not in oos_canceled_wos]

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

                        # If time is already set to target_time in the table (along with target_tech & priority), skip table update
                        is_time_set = self.is_time_matching(target_time, curr_sched)
                        is_tech_set = not target_tech or (self.canonical_str(curr_tech) == self.canonical_str(target_tech))
                        is_prio_set = not target_priority or ("SERVICE NEEDED" in (curr_prio or "").upper())

                        if is_time_set and is_tech_set and is_prio_set:
                            print(f"  ⏭️ WO #{wo_num} for '{asset_name}' is already set (Time: '{target_time}', Tech: '{curr_tech}', Priority: '{curr_prio}'). Skipping table update.")
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

                # ── Multiple Truck Assignments Detection & Cancellation ──
                # If a tech has multiple truck assignments, keep the one configured in JSON template and cancel the other(s).
                tech_to_trucks = {}
                for r in truck_headers:
                    c_tech = self.canonical_str(r.get("tech_name", ""))
                    if c_tech:
                        if c_tech not in tech_to_trucks:
                            tech_to_trucks[c_tech] = []
                        tech_to_trucks[c_tech].append(r)

                canceled_wos = set()
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

                            # If this truck is NOT assigned to this tech in JSON template, cancel it!
                            is_match = (
                                c_exp_truck and (
                                    c_t_name == c_exp_truck or
                                    c_exp_truck in c_t_name or
                                    c_t_name in c_exp_truck
                                )
                            )
                            if not is_match:
                                print(f"  🚨 Truck '{t_name}' (WO #{wo_num}) is NOT assigned to '{tech_display}' in JSON template! Initiating cancellation...")
                                if click_wos and wo_num:
                                    canceled = await self.cancel_work_order_via_dispatch_summary(
                                        work_order_number=wo_num,
                                        truck_name=t_name,
                                        tech_name=tech_display
                                    )
                                    if canceled:
                                        canceled_wos.add(wo_num)
                                        total_reconciled += 1
                            else:
                                print(f"  ✅ Truck '{t_name}' (WO #{wo_num}) matches JSON template for '{tech_display}'. Keeping.")

                # Filter out canceled work orders
                if canceled_wos:
                    truck_headers = [r for r in truck_headers if r.get("work_order_number") not in canceled_wos]

                if click_wos:
                    print(f"\n🖱️ Reconciling {len(truck_headers)} truck work orders with Dashboard config for {normalized_date}...")
                    for idx, r in enumerate(truck_headers, 1):
                        wo_num = r.get("work_order_number")
                        truck_name = r.get("name")
                        expected_tech = self.get_expected_tech_for_truck(truck_name)

                        if not expected_tech:
                            print(f"[{idx}/{len(truck_headers)}] Truck '{truck_name}' is inactive or has no expected tech in Dashboard config. Skipping.")
                            continue

                        if wo_num:
                            total_checked += 1
                            print(f"\n[{idx}/{len(truck_headers)}] Checking WO #{wo_num} ({truck_name}) on {normalized_date}...")
                            updated = await self.click_and_reconcile_work_order(
                                work_order_number=wo_num,
                                truck_name=truck_name,
                                expected_tech=expected_tech
                            )
                            if updated:
                                total_reconciled += 1
            else:
                print(f"⚠️ No truck_assign_headers found on {normalized_date}.")

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
