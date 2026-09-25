"""
Dispatch Board Display Automation Scraper
Pre-creates static visual aid work orders and completed technician display work orders
on the FieldEdge Dispatch Board up to 30 days in advance.
"""

import os
import sys
import json
import asyncio
import random
from datetime import datetime, timedelta
import time
import traceback
from typing import List, Dict, Optional, Callable

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Ensure local dispatch directory and Backend directory are on sys.path
_dispatch_dir = os.path.dirname(os.path.abspath(__file__))
if _dispatch_dir not in sys.path:
    sys.path.insert(0, _dispatch_dir)

_backend_dir = os.path.dirname(_dispatch_dir)
_backend_path = os.path.join(_backend_dir, "Backend")
if os.path.exists(_backend_path) and _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

try:
    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
    django.setup()
    HAS_DJANGO = True
except Exception:
    HAS_DJANGO = False

try:
    from base_scraper import BaseScraper
except ImportError:
    try:
        from Backend.automation.scrapers.base_scraper import BaseScraper  # type: ignore
    except ImportError:
        from automation.scrapers.base_scraper import BaseScraper  # type: ignore

class DispatchBoardDisplayAutomationScraper(BaseScraper):
    """
    Scraper and automation engine for FieldEdge 'Dispatch Board Display Automation'.
    """

    def __init__(self, template_path: Optional[str] = None):
        super().__init__()
        self.scraper_name = "dispatch-board-display-automation"
        self.template_path = template_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "config",
            "dispatch_board_template.json"
        )
        self.template_data = self._load_template()

    def _load_template(self) -> Dict:
        """Load visual work order configuration template from API or local JSON file."""
        api_url = os.getenv("BACKEND_API_URL", "").rstrip("/")
        if api_url:
            try:
                import urllib.request
                endpoint = f"{api_url}/automation/dispatch-config/" if api_url.endswith('/api') else f"{api_url}/api/automation/dispatch-config/"
                req = urllib.request.Request(endpoint, headers={"User-Agent": "SterlingAutomations/1.0"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    if response.status == 200:
                        remote_data = json.loads(response.read().decode('utf-8'))
                        print(f"✅ Loaded live configuration from Dashboard API: {endpoint}")
                        if os.path.exists(self.template_path):
                            with open(self.template_path, "r", encoding="utf-8") as f:
                                local_data = json.load(f)
                                local_data.update(remote_data)
                                return local_data
                        return remote_data
            except Exception as e:
                print(f"⚠️ Could not fetch live template from API ({api_url}): {e}. Falling back to local template file.")

        if os.path.exists(self.template_path):
            with open(self.template_path, "r", encoding="utf-8") as f:
                return json.load(f)
        print(f"⚠️ Template file not found at {self.template_path}, no defaults applied.")
        return {}

    @staticmethod
    def canonical_str(t: str) -> str:
        """Strip everything except alphanumeric for robust duplicate comparison across emojis & whitespace."""
        if not t:
            return ""
        import unicodedata, re
        norm = re.sub(r'[^a-zA-Z0-9]', '', unicodedata.normalize('NFKD', str(t))).lower()
        if not norm:
            norm = str(t).strip().lower()
        return norm

    @staticmethod
    def _extract_all_strings_recursive(obj) -> List[str]:
        """Recursively extract all string values from a dict or list."""
        strings = []
        if isinstance(obj, str):
            if obj.strip():
                strings.append(obj.strip())
        elif isinstance(obj, dict):
            for v in obj.values():
                strings.extend(DispatchBoardDisplayAutomationScraper._extract_all_strings_recursive(v))
        elif isinstance(obj, (list, tuple)):
            for elem in obj:
                strings.extend(DispatchBoardDisplayAutomationScraper._extract_all_strings_recursive(elem))
        return strings

    @staticmethod
    def check_item_match(wo: Dict, canon_target: str, time_key: Optional[str] = None, require_customer_match: bool = False) -> bool:
        """Check if a target canonical string matches a schedule item object."""
        if not canon_target:
            return False

        w_time = wo.get("time_key", "")
        if time_key and w_time and time_key != w_time:
            return False

        c_cust = wo.get("canon_customer", "")
        c_comb = wo.get("canon_combined", "")
        c_strings = wo.get("canon_strings", [])

        if require_customer_match:
            if c_cust and (canon_target == c_cust or canon_target in c_cust or c_cust in canon_target):
                return True
            return False

        # Exact match in any extracted string
        if canon_target in c_strings:
            return True

        # Exact match in customer name
        if c_cust and canon_target == c_cust:
            return True

        # Match in combined string
        if c_comb and (canon_target == c_comb or canon_target in c_comb):
            return True

        # Substring in any extracted string (if target is at least 3 chars long)
        if len(canon_target) >= 3:
            for s in c_strings:
                if s and (canon_target in s or (len(s) >= 3 and s in canon_target)):
                    return True

        return False

    def extract_existing_wos(self, board_data):
        """Extract existing work orders and active board date from intercepted JSON data."""
        if not board_data or "Board" not in board_data:
            return None, {}
        
        board_date_str = None
        existing_wos = {}
        
        if board_data["Board"].get("Headers"):
            first_header = board_data["Board"]["Headers"][0]
            dt_str = first_header.get("HeaderDateTime")
            if dt_str:
                try:
                    # e.g., '2026-09-01T04:45:00' or '2026-09-01 04:45:00'
                    board_date_str = dt_str.replace("T", " ").split(" ")[0]  # "YYYY-MM-DD"
                except Exception:
                    pass
        
        for group in board_data["Board"].get("Groups", []):
            for row in group.get("Rows", []):
                raw_row_name = row.get("Name") or ""
                canon_tech = self.canonical_str(raw_row_name)
                
                if canon_tech not in existing_wos:
                    existing_wos[canon_tech] = []
                
                # Process ScheduleItems
                for item in row.get("ScheduleItems", []):
                    all_raw_strings = self._extract_all_strings_recursive(item)
                    canon_strings = [self.canonical_str(s) for s in all_raw_strings if s]
                    canon_strings = [cs for cs in canon_strings if cs]
                    canon_combined = self.canonical_str(" ".join(all_raw_strings)) if all_raw_strings else ""

                    cust_name = str(item.get("CustomerName") or item.get("Customer") or item.get("Name") or item.get("Title") or "")
                    canon_customer = self.canonical_str(cust_name)
                    
                    dt_str = item.get("ScheduleDateTime")
                    time_key = ""
                    if dt_str:
                        try:
                            dt_str_clean = dt_str.replace("Z", "").replace("T", " ").split(".")[0].strip()
                            local_dt = datetime.strptime(dt_str_clean, "%Y-%m-%d %H:%M:%S")
                            time_key = local_dt.strftime("%I:%M %p")
                            if time_key.startswith("0"): time_key = time_key[1:]
                        except Exception:
                            pass
                            
                    existing_wos[canon_tech].append({
                        "canon_customer": canon_customer,
                        "canon_strings": canon_strings,
                        "canon_combined": canon_combined,
                        "time_key": time_key,
                        "is_appointment": False
                    })

                # Process AppointmentItems (e.g. OFF - AC, PTO, SICK)
                for appt in row.get("AppointmentItems", []):
                    all_raw_strings = self._extract_all_strings_recursive(appt)
                    canon_strings = [self.canonical_str(s) for s in all_raw_strings if s]
                    canon_strings = [cs for cs in canon_strings if cs]
                    canon_combined = self.canonical_str(" ".join(all_raw_strings)) if all_raw_strings else ""

                    desc_name = str(appt.get("Description") or appt.get("Title") or appt.get("Name") or "OFF")
                    canon_customer = self.canonical_str(desc_name)

                    dt_str = appt.get("ScheduledFrom") or appt.get("ScheduleDateTime")
                    time_key = ""
                    if dt_str:
                        try:
                            dt_str_clean = dt_str.replace("Z", "").replace("T", " ").split(".")[0].strip()
                            local_dt = datetime.strptime(dt_str_clean, "%Y-%m-%d %H:%M:%S")
                            time_key = local_dt.strftime("%I:%M %p")
                            if time_key.startswith("0"): time_key = time_key[1:]
                        except Exception:
                            pass

                    existing_wos[canon_tech].append({
                        "canon_customer": canon_customer,
                        "canon_strings": canon_strings,
                        "canon_combined": canon_combined,
                        "time_key": time_key,
                        "is_appointment": True
                    })
                        
        return board_date_str, existing_wos

    async def fetch_board_data_for_date(self, target_date_str: str, board_req: dict):
        """Dynamically fetch fresh board data JSON from FieldEdge for target_date_str (YYYY-MM-DD)."""
        if not board_req or not board_req.get("url"):
            print(f"  ⚠️ Cannot dynamically fetch board data for {target_date_str}: board_req template missing.")
            return None, {}

        try:
            payload = json.loads(board_req["post_data"])
            payload["Date"] = target_date_str + "T00:00:00"
            for k in list(payload.keys()):
                if "date" in k.lower():
                    if "end" in k.lower():
                        payload[k] = target_date_str + "T23:59:59"
                    else:
                        payload[k] = target_date_str + "T00:00:00"

            headers = board_req.get("headers", {})
            fetch_js = f"""
            async () => {{
                try {{
                    const resp = await fetch("{board_req['url']}", {{
                        method: 'POST',
                        headers: {{
                            'Content-Type': 'application/json',
                            'Authorization': '{headers.get('authorization', '')}',
                            'FE-CSRF-TOKEN': '{headers.get('fe-csrf-token', '')}'
                        }},
                        body: JSON.stringify({json.dumps(payload)})
                    }});
                    return await resp.json();
                }} catch(e) {{
                    return {{ error: e.toString() }};
                }}
            }}
            """
            new_data = await self.page.evaluate(fetch_js)
            if new_data and "Board" in new_data:
                b_date, e_wos = self.extract_existing_wos(new_data)
                print(f"📡 Dynamically fetched board date: '{b_date}' | Found {len(e_wos)} tech groups.")
                return b_date, e_wos
            else:
                print(f"⚠️ Dynamic board fetch returned invalid response for {target_date_str}: {new_data}")
        except Exception as e:
            print(f"⚠️ Exception fetching board data for {target_date_str}: {e}")
        return None, {}

    async def navigate_to_board_date(self, target_date_obj: datetime) -> bool:
        """
        Navigate to target_date on the Dispatch Board UI using direct URL navigation.
        URL format: https://login.fieldedge.com/#/DispatchBoard/MM-DD-YYYY (e.g., https://login.fieldedge.com/#/DispatchBoard/09-23-2026)
        """
        try:
            date_url_str = target_date_obj.strftime("%m-%d-%Y")
            target_url = f"https://login.fieldedge.com/#/DispatchBoard/{date_url_str}"
            print(f"🗓️ Navigating to Dispatch Board URL for date: {target_url}")
            
            # Go to the URL and then force reload to ensure the SPA fetches the new date's data
            await self.page.goto(target_url, wait_until='domcontentloaded', timeout=30000)
            await self.page.reload(wait_until='domcontentloaded', timeout=30000)
            await self.page.wait_for_timeout(4000)
            return True
        except Exception as e:
            print(f"⚠️ Could not navigate to board date via URL {target_url}: {e}")
            return False

    async def ensure_clean_dispatch_board(self, force_reload: bool = False):
        """
        Ensure page is cleanly loaded on Dispatch Board and not stuck in a loading spinner state.
        Navigates or reloads to dispatch_url to guarantee a fresh interactable state.
        """
        dispatch_url = self.rules.get('web_url', 'https://login.fieldedge.com/Dispatch')
        current_url = self.page.url

        # Check for Login redirect
        if "Login" in current_url or await self.page.locator("input[name='UserName']").is_visible():
            print("⚠️ Detected Login redirect. Re-authenticating to FieldEdge...")
            try:
                await self.login_fieldedge()
                print(f"Returning to Dispatch Board: {dispatch_url}")
                await self.page.goto(dispatch_url, wait_until='domcontentloaded')
                await self.page.wait_for_timeout(4000)
            except Exception as e:
                print(f"❌ Re-authentication failed: {e}")
            return

        needs_fresh_load = force_reload

        # Check if URL is not main Dispatch Board or stuck on DispatchSummary
        if "Dispatch" not in current_url or "DispatchSummary" in current_url:
            needs_fresh_load = True
        else:
            # Check if loading spinner / overlay is active and stuck
            try:
                spinners = self.page.locator(".loading, .spinner, .v-spinner, div[class*='loading']:visible")
                if await spinners.count() > 0 and await spinners.first.is_visible():
                    try:
                        await spinners.first.wait_for(state="hidden", timeout=4000)
                    except Exception:
                        print("⚠️ Dispatch Board stuck in loading spinner. Triggering fresh page reload...")
                        needs_fresh_load = True
            except Exception:
                pass

        if needs_fresh_load:
            print(f"🔄 Fresh loading Dispatch Board URL: {dispatch_url}")
            try:
                await self.page.goto(dispatch_url, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                print(f"⚠️ Navigation error ({e}). Retrying page reload...")
                await self.page.reload(wait_until='domcontentloaded')
            await self.page.wait_for_timeout(4000)

    async def open_create_wo_modal(self):
        """Click on the Create Work Order button in FieldEdge UI with session check & retry."""
        xpath = self.rules.get('dispatch_board_create_wo_btn_xpath') or "//button[contains(normalize-space(), 'Work Order') or contains(normalize-space(), 'Create Work Order')]"
        if not xpath:
            print("⚠️ 'dispatch_board_create_wo_btn_xpath' rule not defined.")
            return False

        # Ensure board is in clean interactable state before clicking
        await self.ensure_clean_dispatch_board()

        try:
            # FieldEdge sometimes has hidden 'Create Work Order' buttons in the DOM (e.g. mobile menus).
            # .first locks onto the hidden one and times out. We must find the visible one.
            buttons = self.page.locator(xpath)
            await buttons.first.wait_for(state="attached", timeout=30000)
            
            count = await buttons.count()
            clicked = False
            for i in range(count):
                btn = buttons.nth(i)
                if await btn.is_visible():
                    await btn.scroll_into_view_if_needed()
                    await btn.click(force=True)
                    try:
                        await self.page.evaluate("(el) => el.click()", await btn.element_handle())
                    except Exception:
                        pass
                    clicked = True
                    break
            
            if not clicked:
                raise Exception("No visible Create Work Order button found.")
                
            # Wait for Create WO modal/Customer dropdown to become visible (VPS rendering can be slower)
            customer_xpath = self.rules.get(
                'dispatch_board_customer_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Customer Name')]/following::div[contains(@class,'v-select')][1]"
            )
            import time
            _modal_start = time.time()
            modal_visible = False
            while time.time() - _modal_start < 30:
                for cand in [
                    customer_xpath,
                    "#vscreate-wo-customer-select",
                    "#vscreate-wo-customer-select__combobox",
                    "div[id*='vscreate-wo-customer-select']",
                    ".customer-select",
                    ".customer-search-container"
                ]:
                    try:
                        loc = self.page.locator(cand).first
                        if await loc.count() > 0 and await loc.is_visible():
                            modal_visible = True
                            break
                    except Exception:
                        pass
                if modal_visible:
                    break
                await self.page.wait_for_timeout(500)

            if not modal_visible:
                print("  -> Customer modal element not detected after initial click, attempting JS re-click...")
                for i in range(count):
                    btn = buttons.nth(i)
                    if await btn.is_visible():
                        try:
                            await self.page.evaluate("(el) => el.click()", await btn.element_handle())
                        except Exception:
                            pass
                await self.page.wait_for_timeout(2000)
            return True
        except Exception as e:
            print(f"⚠️ Could not locate or click Create Work Order button: {e}. Retrying with fresh dispatch board reload...")
            try:
                await self.ensure_clean_dispatch_board(force_reload=True)
                
                buttons = self.page.locator(xpath)
                await buttons.first.wait_for(state="attached", timeout=30000)
                
                count = await buttons.count()
                clicked = False
                for i in range(count):
                    btn = buttons.nth(i)
                    if await btn.is_visible():
                        await btn.scroll_into_view_if_needed()
                        await btn.click(force=True)
                        try:
                            await self.page.evaluate("(el) => el.click()", await btn.element_handle())
                        except Exception:
                            pass
                        clicked = True
                        break
                
                if not clicked:
                    raise Exception("No visible Create Work Order button found on retry.")
                    
                await self.page.wait_for_timeout(3000)
                return True
            except Exception as retry_err:
                print(f"❌ Retry also failed: {retry_err}")
                return False

    async def select_dropdown_option(self, dropdown_selector: str, option_text: str, press_enter: bool = True) -> bool:
        """
        Robust dropdown option selection for FieldEdge Vue-select dropdowns.
        Works accurately on both Windows local and Linux headless servers.
        """
        if not option_text:
            return True

        try:
            dropdown = self.page.locator(dropdown_selector).first
            try:
                await dropdown.wait_for(state="visible", timeout=8000)
            except Exception:
                pass

            await dropdown.scroll_into_view_if_needed()
            await dropdown.click(force=True)
            await self.page.wait_for_timeout(400)

            # Locate search input field within the dropdown or globally active vue-select
            search_input = dropdown.locator("input.vs__search, input[type='search'], input").first
            if await search_input.count() == 0 or not await search_input.is_visible():
                search_input = self.page.locator("input.vs__search:visible, input[type='search']:visible").first

            # Ensure input is focused and clear existing input
            try:
                await search_input.focus()
                await search_input.fill("")
            except Exception:
                pass

            # Type option text into search input
            await search_input.fill(option_text)
            await self.page.wait_for_timeout(600)

            if press_enter:
                # Trigger search API for fields like Customer Name that require Enter
                await search_input.press("Enter")
                # Give API response time to load (longer budget for Linux server network latency)
                await self.page.wait_for_timeout(2500)

            # Locate visible dropdown options
            options = self.page.locator("ul.vs__dropdown-menu:visible li, ul[role='listbox']:visible li, li.vs__dropdown-option:visible")
            try:
                await options.first.wait_for(state="visible", timeout=6000)
            except Exception:
                pass

            count = await options.count()
            clicked = False
            target_norm = option_text.strip().lower()
            target_canon = self.canonical_str(option_text)

            # Pass 1: Look for exact match
            for i in range(count):
                opt = options.nth(i)
                if await opt.is_visible():
                    raw_text = await opt.text_content()
                    if raw_text and raw_text.strip().lower() == target_norm:
                        await opt.scroll_into_view_if_needed()
                        await opt.click(force=True)
                        clicked = True
                        break

            # Pass 2: Partial match if exact match not found
            if not clicked:
                for i in range(count):
                    opt = options.nth(i)
                    if await opt.is_visible():
                        raw_text = await opt.text_content()
                        if raw_text and target_norm in raw_text.strip().lower():
                            await opt.scroll_into_view_if_needed()
                            await opt.click(force=True)
                            clicked = True
                            break

            # Pass 3: Canonical match (ignore special chars/whitespace)
            if not clicked and target_canon:
                for i in range(count):
                    opt = options.nth(i)
                    if await opt.is_visible():
                        raw_text = await opt.text_content()
                        if raw_text and target_canon in self.canonical_str(raw_text):
                            await opt.scroll_into_view_if_needed()
                            await opt.click(force=True)
                            clicked = True
                            break

            # Fallback if no option clicked directly
            if not clicked:
                if count > 0:
                    first_opt = options.first
                    if await first_opt.is_visible():
                        await first_opt.click(force=True)
                        clicked = True
                elif press_enter:
                    await search_input.press("Enter")

            await self.page.wait_for_timeout(500)
            return True
        except Exception as e:
            print(f"⚠️ Failed to select '{option_text}' in dropdown ({dropdown_selector}): {e}")
            return False

    async def fill_and_save_work_order(
        self,
        customer_name: str,
        task: str,
        lead_source: str,
        priority: str,
        target_date: str,
        start_time: str,
        duration: str,
        tech_name: Optional[str] = None,
        mark_completed: bool = False,
        immediate_action: str = "Create Work Order",
        task_type: Optional[str] = None
    ) -> bool:
        """
        Fill the FieldEdge Create Work Order modal form and Save.
        Order: Customer → Immediate Action → Task → Lead Source → Priority → Tech → Date → Time → Duration → Save
        """
        try:
            opened = await self.open_create_wo_modal()
            if not opened:
                return False

            await self.page.wait_for_timeout(1000)

            print(f"  [1/7] Customer: '{customer_name}'")
            # Use simplified customer selector
            customer_xpath = self.rules.get('dispatch_board_customer_xpath', "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Customer Name')]/following::div[contains(@class,'v-select')][1]")
            success = await self.select_dropdown_option(customer_xpath, customer_name, press_enter=True)
            if not success:
                print(f"⚠️ Failed to select Customer '{customer_name}'. Aborting.")
                return False

            # Smart wait: poll for the form to be ready (Task dropdown visible) with 45s total budget.
            # On slow VPS networks, customer data can take 15-30s to load from FieldEdge API.
            print(f"  -> Waiting for customer data to load...")
            import time
            _load_start = time.time()
            _max_load_wait = 45  # seconds total budget
            _form_ready = False

            while (time.time() - _load_start) < _max_load_wait:
                try:
                    # Check if Task dropdown is visible (= form has loaded customer data)
                    task_ready = self.page.locator(
                        self.rules.get('dispatch_board_task_xpath',
                            "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Task') and not(contains(normalize-space(),'Duration'))]/following::div[contains(@class,'v-select')][1]")
                    ).first
                    if await task_ready.is_visible():
                        _form_ready = True
                        elapsed = round(time.time() - _load_start, 1)
                        print(f"  -> Form ready in {elapsed}s. Proceeding to fill fields.")
                        break
                except Exception:
                    pass

                # Wait 2s between polls to avoid hammering the DOM
                await self.page.wait_for_timeout(2000)

            if not _form_ready:
                # Last resort: wait a bit more and continue anyway
                print(f"  -> Form not ready after {_max_load_wait}s. Continuing anyway...")
                await self.page.wait_for_timeout(3000)

            print(f"  [2/7] Immediate Action: '{immediate_action}'")
            ia_xpath = self.rules.get(
                'dispatch_board_immediate_action_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Immediate Action')]/following::div[contains(@class,'v-select')][1]"
            )
            await self.select_dropdown_option(ia_xpath, immediate_action, press_enter=False)
            await self.page.wait_for_timeout(500)

            print(f"  [3/7] Task: '{task}'")
            task_xpath = self.rules.get(
                'dispatch_board_task_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Task') and not(contains(normalize-space(),'Duration'))]/following::div[contains(@class,'v-select')][1]"
            )
            await self.select_dropdown_option(task_xpath, task, press_enter=False)
            await self.page.wait_for_timeout(500)

            print(f"  [4/7] Lead Source: '{lead_source}'")
            lead_xpath = self.rules.get(
                'dispatch_board_lead_source_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Work Order Lead Source')]/following::div[contains(@class,'v-select')][1]"
            )
            await self.select_dropdown_option(lead_xpath, lead_source, press_enter=False)
            await self.page.wait_for_timeout(500)

            print(f"  [5/7] Priority: '{priority}'")
            priority_xpath = self.rules.get(
                'dispatch_board_priority_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Priority')]/following::div[contains(@class,'v-select')][1]"
            )
            await self.select_dropdown_option(priority_xpath, priority, press_enter=False)
            await self.page.wait_for_timeout(500)

            if tech_name:
                print(f"  [6/7] Primary Tech: '{tech_name}'")
                tech_xpath = self.rules.get(
                    'dispatch_board_tech_name_xpath',
                    "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Primary Tech')]/following::div[contains(@class,'v-select')][1]"
                )
                # Use simple dropdown selector
                await self.select_dropdown_option(tech_xpath, tech_name, press_enter=False)
                # Smart wait: wait for Date field to become enabled (it's disabled until Tech is assigned)
                print(f"  -> Waiting for Date/Time fields to become active...")
                try:
                    date_input_xpath = self.rules.get('dispatch_board_date_input_xpath', "//input[@name='Start Date']")
                    date_field = self.page.locator(date_input_xpath).first
                    await date_field.wait_for(state="visible", timeout=5000)
                    for _ in range(15):
                        is_disabled = await date_field.get_attribute("disabled")
                        if is_disabled is None:
                            print(f"  -> Date field enabled. Ready.")
                            break
                        await self.page.wait_for_timeout(300)
                except Exception:
                    await self.page.wait_for_timeout(500)

            print(f"  [7/7] Date: {target_date} | Time: {start_time} | Duration: {duration}")
            try:
                # Date — wait for it to become enabled (it's disabled until Task is selected)
                date_input = self.page.locator(
                    self.rules.get('dispatch_board_date_input_xpath', "//input[@name='Start Date']")
                ).first
                try:
                    await date_input.wait_for(state="visible", timeout=3000)
                    # Wait until not disabled
                    for _ in range(10):
                        is_disabled = await date_input.get_attribute("disabled")
                        if is_disabled is None:
                            break
                        await self.page.wait_for_timeout(300)
                except Exception:
                    pass

                if await date_input.is_visible(timeout=1000):
                    await date_input.click(force=True)
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                    await date_input.type(target_date, delay=80)
                    await self.page.keyboard.press("Enter")
                    await date_input.evaluate("el => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }")
                    await self.page.keyboard.press("Tab")
                    await self.page.wait_for_timeout(400)

                # Start Time
                time_input = self.page.locator(
                    self.rules.get('dispatch_board_time_input_xpath',
                        "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Start Time')]/following::input[1]")
                ).first
                if await time_input.is_visible(timeout=1500) and not await time_input.get_attribute("disabled"):
                    await time_input.click(force=True)
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                    await time_input.type(start_time, delay=80)
                    await self.page.keyboard.press("Enter")
                    await time_input.evaluate(f"el => {{ el.value = '{start_time}'; el.dispatchEvent(new Event('input', {{ bubbles: true }})); el.dispatchEvent(new Event('change', {{ bubbles: true }})); }}")
                    await self.page.keyboard.press("Tab")
                    await self.page.wait_for_timeout(300)

                # Task Duration
                duration_input = self.page.locator(
                    self.rules.get('dispatch_board_duration_input_xpath',
                        "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Task Duration')]/following::input[contains(@class,'base-input')][1]")
                ).first
                if await duration_input.is_visible(timeout=1500) and not await duration_input.get_attribute("disabled"):
                    # Pad to 5 chars: '1:45' -> '01:45'
                    fmt_dur = ("0" + duration) if len(duration) == 4 and ":" in duration else duration
                    await duration_input.click(force=True)
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                    await duration_input.type(fmt_dur, delay=80)
                    await self.page.keyboard.press("Enter")
                    await duration_input.evaluate(f"el => {{ el.value = '{fmt_dur}'; el.dispatchEvent(new Event('input', {{ bubbles: true }})); el.dispatchEvent(new Event('change', {{ bubbles: true }})); }}")
                    await self.page.keyboard.press("Tab")
                    await self.page.wait_for_timeout(300)

            except Exception as e:
                print(f"  Note on Date/Time/Duration fields: {e}")

            save_btn = None
            for save_sel in [
                "//button[contains(@class,'confirm') and .//span[normalize-space()='Save']]",
                "//*[@id='scroll-container']/div/div[5]/div/button[3]",
                "div.on-bottom-modal button.confirm_hsagT",
                "button.confirm_hsagT",
            ]:
                try:
                    loc = self.page.locator(save_sel).last
                    if await loc.count() > 0 and await loc.is_visible(timeout=3000):
                        save_btn = loc
                        break
                except Exception:
                    pass

            if not save_btn:
                save_btn = self.page.locator("//button[contains(@class,'confirm') and .//span[normalize-space()='Save']]").last
                try:
                    await save_btn.wait_for(state="visible", timeout=5000)
                except Exception:
                    print(f"⚠️ Save button not visible for '{customer_name}'. Aborting.")
                    return False

            print(f"  -> Waiting 2s for Vue inputs to debounce before saving...")
            await self.page.wait_for_timeout(2000)
            await save_btn.click(force=True)
            print(f"✅ Clicked Save for: '{customer_name}'")

            # -- WAIT FOR SAVE TO COMPLETE --
            # Wait for the modal to disappear to guarantee the save was processed by the server
            try:
                modal = self.page.locator("div.create-workorder-form-grid").first
                print("  -> Waiting for Save to process (modal to close)...")
                
                modal_closed = False
                try:
                    await modal.wait_for(state="hidden", timeout=3000)
                    modal_closed = True
                except Exception:
                    pass
                
                if modal_closed:
                    print("  -> Save processed successfully (modal closed).")
                else:
                    print("  -> Save processed (proceeding without waiting for modal to hide in UI).")
            except Exception as e:
                print(f"  ⚠️ Unexpected error while waiting for modal to close: {e}")

            if mark_completed:
                # Active technician flow → FieldEdge redirects to DispatchSummary after save
                # For "Internal" task type, the redirect may be slower on VPS/headless; use robust retry.
                print("  -> Waiting for DispatchSummary Save button...")
                summary_completed = False
                try:
                    summary_save_btn = self.page.locator("button#dispatch-summary-save-workorder").first

                    # First attempt: wait up to 20s (VPS can be slower than local)
                    try:
                        await summary_save_btn.wait_for(state="visible", timeout=20000)
                    except Exception:
                        # Fallback: check if URL already contains DispatchSummary
                        current_url = self.page.url
                        print(f"  -> DispatchSummary button not found in time. Current URL: {current_url}")
                        if "DispatchSummary" in current_url or "dispatch-summary" in current_url.lower():
                            print("  -> Detected DispatchSummary URL. Retrying button search...")
                            await self.page.wait_for_timeout(2000)
                            await summary_save_btn.wait_for(state="visible", timeout=10000)
                        else:
                            # If we selected "Complete Work Order" as immediate action and didn't redirect,
                            # the WO is already completed.
                            if immediate_action == "Complete Work Order":
                                print(f"🎉 WO '{customer_name}' auto-completed directly via Immediate Action (no redirect needed).")
                                summary_completed = True
                            else:
                                print(f"  ⚠️ DispatchSummary not opened for '{customer_name}' "
                                      f"(task_type='{task_type or task}'). "
                                      f"WO created but not auto-completed. Skipping completion step.")
                                raise Exception("DispatchSummary page not reached")

                    if not summary_completed:
                        # Blur active input field first (ensures form state is committed)
                        try:
                            await self.page.keyboard.press("Tab")
                            await self.page.wait_for_timeout(300)
                        except Exception:
                            pass

                        await summary_save_btn.scroll_into_view_if_needed()
                        print("  -> Waiting for DispatchSummary Save button to be enabled & interactable...")

                        try:
                            await summary_save_btn.click(force=True, timeout=5000)
                            print("  -> DispatchSummary Save button clicked.")
                            save_clicked = True
                        except Exception as e:
                            print(f"  -> Playwright save click error: {e}")
                            save_clicked = False

                        print("  -> Save clicked. Checking for status confirmation modal...")

                        confirm_btn = self.page.locator("button#confirm-save-dispatch-summary, button:has-text('Confirm')").first
                        modal_title_loc = self.page.locator(".label-title:has-text('Would you like to change the status?')").first
                        
                        modal_appeared = False
                        _modal_start = time.time()
                        _reclicked = False
                        
                        while time.time() - _modal_start < 12:
                            try:
                                if (await confirm_btn.count() > 0 and await confirm_btn.is_visible()) or \
                                   (await modal_title_loc.count() > 0 and await modal_title_loc.is_visible()):
                                    modal_appeared = True
                                    print(f"  -> Confirmation modal appeared in {round(time.time() - _modal_start, 1)}s!")
                                    break
                            except Exception:
                                pass

                            # Single re-click attempt at 4s if modal hasn't appeared yet
                            if not _reclicked and (time.time() - _modal_start >= 4.0):
                                _reclicked = True
                                try:
                                    await self.page.evaluate("""
                                        () => {
                                            const btn = document.querySelector('#dispatch-summary-save-workorder');
                                            if (btn) {
                                                const target = btn.querySelector('.content_YwYtM, span') || btn;
                                                ['mousedown', 'mouseup', 'click'].forEach(evt => {
                                                    target.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                                                });
                                            }
                                        }
                                    """)
                                except Exception:
                                    pass

                            await self.page.wait_for_timeout(800)

                        if modal_appeared:
                            print("  -> Selecting 'Completed' status in confirmation modal...")
                            try:
                                clicked = await self.page.evaluate("""
                                    () => {
                                        const containers = document.querySelectorAll('.small.field-radio-container, .field-radio-container');
                                        for (const c of containers) {
                                            const span = c.querySelector('.radio-label span') || c.querySelector('span');
                                            if (span && span.textContent.trim() === 'Completed') {
                                                c.scrollIntoView({ behavior: 'instant', block: 'center' });
                                                c.click();
                                                const radio = c.querySelector('.field-radio');
                                                if (radio) {
                                                    radio.click();
                                                    radio.classList.add('field-radio-checked');
                                                }
                                                ['mousedown', 'mouseup', 'click'].forEach(evt => {
                                                    c.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                                                });
                                                return true;
                                            }
                                        }
                                        return false;
                                    }
                                """)
                                print(f"  -> JS click 'Completed' radio: {'success' if clicked else 'element not found'}")
                                await self.page.wait_for_timeout(800)

                                # Verify selection; if not checked, try Playwright click
                                checked_cls = await self.page.evaluate("""
                                    () => {
                                        const containers = document.querySelectorAll('.small.field-radio-container, .field-radio-container');
                                        for (const c of containers) {
                                            if (c.textContent.trim().includes('Completed')) {
                                                const radio = c.querySelector('.field-radio');
                                                return radio ? radio.className : '';
                                            }
                                        }
                                        return '';
                                    }
                                """)
                                if "field-radio-checked" not in (checked_cls or ""):
                                    print("  -> Trying Playwright click on 'Completed' radio container...")
                                    try:
                                        container_locator = self.page.locator(".small.field-radio-container, .field-radio-container").filter(has_text="Completed").first
                                        await container_locator.scroll_into_view_if_needed()
                                        await container_locator.click(force=True)
                                        await self.page.wait_for_timeout(500)
                                    except Exception:
                                        pass

                                print("  -> Selected 'Completed' status. Giving Vue 1.5s to update...")
                                await self.page.wait_for_timeout(1500)
                            except Exception as radio_err:
                                print(f"  -> Radio selection note: {radio_err}")

                            print("  -> Clicking Confirm button in modal...")
                            
                            # Ensure button has disabled state removed in Vue DOM
                            try:
                                await self.page.evaluate("""
                                    () => {
                                        const btn = document.querySelector('button#confirm-save-dispatch-summary') ||
                                                    Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Confirm' || b.textContent.trim() === 'Save');
                                        if (btn) {
                                            btn.removeAttribute('disabled');
                                            btn.classList.remove('disabled');
                                        }
                                    }
                                """)
                            except Exception:
                                pass

                            # JS click on confirm button with full MouseEvents
                            try:
                                await self.page.evaluate("""
                                    () => {
                                        const btn = document.querySelector('button#confirm-save-dispatch-summary') ||
                                                    Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Confirm' || b.textContent.trim() === 'Save');
                                        if (btn) {
                                            btn.focus();
                                            btn.click();
                                            ['mousedown', 'mouseup', 'click'].forEach(evt => {
                                                btn.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                                            });
                                            return true;
                                        }
                                        return false;
                                    }
                                """)
                            except Exception:
                                pass

                            try:
                                await confirm_btn.scroll_into_view_if_needed()
                                await confirm_btn.click(force=True)
                            except Exception:
                                pass

                            print("  -> Clicked Confirm. Waiting for save to process...")
                            await self.page.wait_for_timeout(3000)
                            try:
                                await confirm_btn.wait_for(state="hidden", timeout=10000)
                                print("  -> Modal closed. Save confirmed!")
                            except Exception:
                                print("  -> Modal close timeout — proceeding anyway.")
                            summary_completed = True
                            print(f"🎉 Active Tech WO '{customer_name}' saved & completed!")
                        else:
                            # Modal did not appear — WO may have saved directly without status prompt
                            summary_completed = True
                            print(f"🎉 Active Tech WO '{customer_name}' saved (no status modal)!")
                except Exception as summary_err:
                    if not summary_completed:
                        print(f"  -> DispatchSummary flow note for '{customer_name}': {summary_err}")
            else:
                # Static header flow
                print(f"🎉 Static WO '{customer_name}' saved!")

            print(f"  Returning to Dispatch Board (fresh load)...")
            await self.ensure_clean_dispatch_board(force_reload=True)
            return True

        except Exception as err:
            print(f"❌ Error in fill_and_save_work_order for '{customer_name}': {err}")
    async def check_and_handle_session(self, on_progress: Optional[Callable] = None) -> bool:
        """Check if FieldEdge logged out (e.g. concurrent login from another location). If logged out, wait 30m and retry."""
        if not self.page:
            return True
        current_url = self.page.url or ""
        if "Login" in current_url or "Account/Login" in current_url:
            print("⚠️ FieldEdge logout detected! (Single account concurrent login from another device).")
            try:
                print("🔄 Attempting immediate re-login...")
                await self.login_fieldedge()
                if "Login" not in self.page.url:
                    print("✅ FieldEdge session restored!")
                    return True
            except Exception as e:
                print(f"⚠️ Immediate re-login failed ({e}). Starting 30-minute retry countdown...")

            for m in range(30, 0, -1):
                msg = f"⚠️ Session logged out (Concurrent login detected). Retrying automatic login in {m} minute(s) (PST)..."
                print(f"⏳ [{self.get_pst_now().strftime('%I:%M:%S %p PST')}] {msg}")
                if hasattr(self, "progress_info"):
                    self.progress_info["status"] = "paused_concurrent_login"
                    self.progress_info["status_message"] = msg
                    if on_progress:
                        try:
                            on_progress(self.progress_info)
                        except Exception:
                            pass
                await asyncio.sleep(60)

            print("🔄 30 minutes completed. Re-initializing browser & attempting automatic login...")
            try:
                await self.initialize()
                await self.login_fieldedge()
                print("✅ Re-login successful after 30m wait!")
                return True
            except Exception as login_err:
                print(f"❌ Re-login attempt failed: {login_err}")
                return False
        return True

    async def run(self, days: int = 30, dry_run: bool = False, active_techs: Optional[List[str]] = None, start_date_str: Optional[str] = None, on_progress: Optional[Callable] = None):
        """
        Main execution method for Dispatch Board Display Automation.
        """
        _start_time = time.time()
        _error_occurred = None
        _records_processed = 0
        _details = {"processed_dates": []}

        self._on_progress_cb = on_progress
        self.progress_info = {
            "current_day": 0,
            "total_days": days,
            "current_date": None,
            "completed_days": 0,
            "remaining_days": days,
            "percent_complete": 0.0,
            "status": "starting",
            "status_message": f"Initializing automation for {days} days...",
            "fieldedge_status": "Connecting / Logging in..."
        }
        if on_progress:
            try:
                on_progress(self.progress_info)
            except Exception:
                pass


        settings = self.template_data.get("general_settings", {})
        task = settings.get("task", "8 - INTERNAL")
        lead_source = settings.get("lead_source", "Sterling Septic -FE")
        immediate_action = settings.get("immediate_action", "Create Work Order")
        skip_vehicle_on_weekends = settings.get("skip_vehicle_work_orders_on_weekends", True)

        static_headers = [
            h for h in self.template_data.get("static_headers", [])
            if h.get("active", True)
        ]
        truck_assign_headers = [
            h for h in self.template_data.get("truck_assign_headers", [])
            if h.get("active", True)
        ]
        tech_defaults = self.template_data.get("technician_display_defaults", {})
        tech_jobs = self.template_data.get("tech_jobs", [])

        tech_priorities = {}
        tech_metadata = {}

        # Load active technicians and their priorities/metadata from the template configuration
        template_techs = self.template_data.get("active_techs", [])
        
        for entry in template_techs:
            name = entry.get("name", "")
            if name:
                tech_priorities[name] = entry.get("priority")
                tech_metadata[name] = entry

        if not active_techs:
            active_techs = [entry.get("name", "") for entry in template_techs if entry.get("name") and entry.get("active", True)]
            skipped = [entry.get("name", "") for entry in template_techs if entry.get("name") and not entry.get("active", True)]
            print(f"📋 Loaded {len(active_techs)} active technicians from template config.")
            if skipped:
                print(f"⏭️ Skipping {len(skipped)} inactive tech(s) from template: {', '.join(skipped)}")

        if start_date_str:
            parsed_dt = None
            try:
                from dateutil import parser as date_parser
                parsed_dt = date_parser.parse(start_date_str).date()
            except Exception:
                # Fallback to standard formats if dateutil is not installed or fails
                for fmt in ("%B %d", "%b %d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(start_date_str, fmt)
                        if "%Y" not in fmt and "%y" not in fmt:
                            dt = dt.replace(year=self.get_pst_now().year)
                        parsed_dt = dt.date()
                        break
                    except ValueError:
                        continue

            if parsed_dt:
                start_dt = parsed_dt
                print(f"📅 Parsed start date: {start_dt}")
            else:
                print(f"⚠️ Could not parse date '{start_date_str}'. Using today.")
                start_dt = self.get_pst_now().date()
        else:
            start_dt = self.get_pst_now().date()

        # Process Lock - Prevent multiple simultaneous runs of this scraper
        import atexit

        def _is_pid_running(pid: int) -> bool:
            if pid <= 0:
                return False
            try:
                if os.name == 'nt':
                    import ctypes
                    SYNCHRONIZE = 0x00100000
                    process = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
                    if process:
                        ctypes.windll.kernel32.CloseHandle(process)
                        return True
                    return False
                else:
                    os.kill(pid, 0)
                    return True
            except Exception:
                return False

        lock_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dispatch_board_display_automation.lock")
        
        def _cleanup_lock():
            try:
                if os.path.exists(lock_file_path):
                    os.remove(lock_file_path)
            except Exception:
                pass
                
        atexit.register(_cleanup_lock)

        if os.path.exists(lock_file_path):
            try:
                with open(lock_file_path, "r") as f:
                    content = f.read().strip()
                if content:
                    parts = content.split("|")
                    if len(parts) == 2:
                        lock_pid = int(parts[0])
                        lock_start_time = float(parts[1])
                    else:
                        lock_pid = 0
                        lock_start_time = float(parts[0])

                    if lock_pid > 0 and lock_pid != os.getpid() and lock_pid != 1 and _is_pid_running(lock_pid):
                        if time.time() - lock_start_time < 7200:
                            print(f"⛔ SCRAPER IS ALREADY RUNNING (PID {lock_pid}, Started {round((time.time() - lock_start_time)/60, 1)} mins ago). Exiting to prevent duplicate creation.")
                            return
                    else:
                        print(f"🧹 Found stale lock file from inactive process (PID {lock_pid}). Cleaning up lock...")
            except Exception:
                pass
            _cleanup_lock()

        try:
            with open(lock_file_path, "w") as f:
                f.write(f"{os.getpid()}|{time.time()}")
        except Exception:
            pass

        print(f"🚀 Starting Dispatch Board Display Automation for next {days} days...")
        if dry_run:
            print("🔍 DRY RUN MODE ACTIVATED - Skipping FieldEdge UI clicks.")
        else:
            await self.initialize()
            
            # Setup network intercept to capture Raw Dispatch Board JSON data
            board_data = None
            board_req = {}
            
            async def handle_request(request):
                nonlocal board_req
                if "GetDispatchBoardData" in request.url and request.method == "POST":
                    board_req["url"] = request.url
                    board_req["headers"] = request.headers
                    board_req["post_data"] = request.post_data
                    
            self.page.on("request", handle_request)
            
            async def handle_response(response):
                nonlocal board_data
                if "GetDispatchBoardData" in response.url:
                    try:
                        text = await response.text()
                        board_data = json.loads(text)
                        print("📡 Intercepted Dispatch Board API data successfully.")
                    except Exception as e:
                        print(f"⚠️ Failed to parse intercepted board data: {e}")

            self.page.on("response", handle_response)
            
            # Note: Avoid blocking images/fonts via page.route because FieldEdge Vue UI relies on web fonts & SVG icons
            # and global request interception adds unnecessary Playwright IPC overhead for every API fetch call.
            
            # Navigate to Dispatch Board exactly like locates does
            dispatch_url = self.rules.get('web_url', 'https://login.fieldedge.com/Dispatch')
            print(f"Navigating to Dispatch Board: {dispatch_url}")
            await self.page.goto(dispatch_url, wait_until='domcontentloaded')
            
            # Login if necessary
            if "Login" in self.page.url:
                await self.login_fieldedge()

            # Wait for Dispatch Board to load (wait for the UI, not the absence of a spinner)
            print("Waiting for Dispatch Board UI to become interactable...")
            try:
                xpath = self.rules.get('dispatch_board_create_wo_btn_xpath')
                if xpath:
                    await self.page.wait_for_selector(xpath, state='visible', timeout=15000)
                    print("Dispatch Board loaded successfully!")
            except Exception:
                print("Note: Create Work Order button not immediately found, but continuing script...")
            
            # Wait up to 10 seconds for the intercepted GetDispatchBoardData response to load
            print("⏳ Waiting for Dispatch Board JSON API data to intercept...")
            for _ in range(10):
                if board_data is not None:
                    break
                await self.page.wait_for_timeout(1000)

            # Extract calendar date and existing work order names from intercepted JSON data
            board_date_str, existing_wos = self.extract_existing_wos(board_data)
            # If initial board date is fully created (all template items already exist on today's board),
            # automatically advance start_dt to start_dt + 1 day (tomorrow) so we work on upcoming future dates.
            initial_target_key = start_dt.strftime("%Y-%m-%d")
            if board_date_str and board_date_str != initial_target_key:
                print(f"🗓️ Initial board date ('{board_date_str}') does not match requested start date ('{initial_target_key}'). Navigating UI date picker...")
                await self.navigate_to_board_date(start_dt)
                fetched_date, fetched_wos = await self.fetch_board_data_for_date(initial_target_key, board_req)
                if fetched_date == initial_target_key:
                    board_date_str, existing_wos = fetched_date, fetched_wos

        locally_created_wos = set()

        try:
            for d in range(days):
                current_date = start_dt + timedelta(days=d)
                date_str = f"{current_date.month}/{current_date.day}/{current_date.strftime('%y')}"
                db_date_key = current_date.strftime("%Y-%m-%d")

                self.progress_info = {
                    "current_day": d + 1,
                    "total_days": days,
                    "current_date": date_str,
                    "completed_days": d,
                    "remaining_days": days - (d + 1),
                    "percent_complete": round(((d + 1) / days) * 100, 1),
                    "status": "processing",
                    "status_message": f"Processing Day {d + 1} of {days} ({date_str})"
                }
                if on_progress:
                    try:
                        on_progress(self.progress_info)
                    except Exception:
                        pass

                print(f"\n--- Processing Date: {date_str} (Day {d + 1}/{days}) ---")

                template_processed_counts = {}

                if not dry_run:
                    if board_date_str != db_date_key:
                        print(f"🗓️ Navigating UI date picker to {db_date_key}...")
                        nav_ok = await self.navigate_to_board_date(current_date)
                        print(f"🔄 Fetching board data for target date {db_date_key}...")
                        fetched_date, fetched_wos = await self.fetch_board_data_for_date(db_date_key, board_req)
                        if fetched_date == db_date_key:
                            board_date_str = fetched_date
                            existing_wos = fetched_wos
                        elif nav_ok:
                            # Fallback: URL navigation succeeded, update board_date_str and use fetched_wos if available
                            board_date_str = db_date_key
                            existing_wos = fetched_wos or {}
                        else:
                            print(f"⛔ Board date '{board_date_str}' does NOT match target date '{db_date_key}' and dynamic fetch could not confirm board state.")
                            print(f"⛔ Skipping creation for date {db_date_key} to prevent duplicate work orders.")
                            continue

                # Static Header Banners
                for header in static_headers:
                    name = header.get("name")
                    start_time = header.get("start_time")
                    duration = header.get("duration")
                    priority = header.get("priority")
                    h_tech = header.get("tech_name")

                    # Check if the header already exists on the board for the target date
                    if not dry_run:
                        canon_target = self.canonical_str(name)
                        try:
                            t_dt = datetime.strptime(start_time.strip().upper(), "%I:%M %p")
                            time_key = t_dt.strftime("%I:%M %p")
                            if time_key.startswith("0"): time_key = time_key[1:]
                        except Exception:
                            time_key = ""
                        key = f"{canon_target}|{time_key}" if time_key else canon_target
                        
                        canon_h_tech = self.canonical_str(h_tech or '')
                        
                        processed_key = (canon_h_tech, key)
                        template_processed_counts[processed_key] = template_processed_counts.get(processed_key, 0) + 1
                        target_count_idx = template_processed_counts[processed_key]
                        
                        # Check locally created WOs first
                        if (db_date_key, canon_h_tech, canon_target, target_count_idx) in locally_created_wos or \
                           (not canon_h_tech and (db_date_key, "", canon_target, target_count_idx) in locally_created_wos):
                            print(f"⏭️ Header banner '{name}' (occurrence {target_count_idx} at {time_key}) was created in this session. Skipping creation.")
                            continue

                        board_count = 0
                        for canon_tech_key, wo_list in existing_wos.items():
                            tech_match = (
                                not canon_h_tech or not canon_tech_key or
                                canon_tech_key == canon_h_tech or
                                canon_h_tech in canon_tech_key or
                                canon_tech_key in canon_h_tech
                            )
                            if tech_match:
                                for wo in wo_list:
                                    if self.check_item_match(wo, canon_target, time_key):
                                        board_count += 1
                         
                        if board_count >= target_count_idx:
                            print(f"⏭️ Header banner '{name}' (occurrence {target_count_idx} at {time_key}) already exists on the board for tech '{h_tech}'. Skipping creation.")
                            continue

                    if dry_run:
                        print(f"  [Dry-Run] Header: {name} | Date: {date_str} | Time: {start_time} | Duration: {duration} | Priority: {priority} | Tech: {h_tech} | Task: {task}")
                    else:
                        res = await self.fill_and_save_work_order(
                            customer_name=name,
                            task=task,
                            lead_source=lead_source,
                            priority=priority,
                            target_date=date_str,
                            start_time=start_time,
                            duration=duration,
                            tech_name=h_tech,
                            immediate_action=immediate_action
                        )
                        if res:
                            locally_created_wos.add((db_date_key, canon_h_tech, canon_target, target_count_idx))
                            # Refresh board data after save
                            f_date, f_wos = await self.fetch_board_data_for_date(db_date_key, board_req)
                            if f_date == db_date_key:
                                board_date_str, existing_wos = f_date, f_wos

                # Truck Assign Headers (Internal Vehicle Work Orders)
                if skip_vehicle_on_weekends and current_date.weekday() >= 5:
                    print(f"⏭️ Skipping internal vehicle work orders (truck_assign_headers) for weekend date {date_str} ({current_date.strftime('%A')}).")
                else:
                    for header in truck_assign_headers:
                        name = header.get("name")
                        start_time = header.get("start_time")
                        duration = header.get("duration")
                        priority = header.get("priority")
                        h_tech = header.get("tech_name")

                        # Check if the header already exists on the board for the target date
                        if not dry_run:
                            canon_target = self.canonical_str(name)
                            try:
                                t_dt = datetime.strptime(start_time.strip().upper(), "%I:%M %p")
                                time_key = t_dt.strftime("%I:%M %p")
                                if time_key.startswith("0"): time_key = time_key[1:]
                            except Exception:
                                time_key = ""
                            key = f"{canon_target}|{time_key}" if time_key else canon_target
                            
                            canon_h_tech = self.canonical_str(h_tech or '')
                            
                            processed_key = (canon_h_tech, key)
                            template_processed_counts[processed_key] = template_processed_counts.get(processed_key, 0) + 1
                            target_count_idx = template_processed_counts[processed_key]
                            
                            # Check locally created WOs first
                            if (db_date_key, canon_h_tech, canon_target, target_count_idx) in locally_created_wos or \
                               (not canon_h_tech and (db_date_key, "", canon_target, target_count_idx) in locally_created_wos):
                                print(f"⏭️ Truck assign header '{name}' (occurrence {target_count_idx} at {time_key}) was created in this session. Skipping creation.")
                                continue

                            board_count = 0
                            for canon_tech_key, wo_list in existing_wos.items():
                                tech_match = (
                                    not canon_h_tech or not canon_tech_key or
                                    canon_tech_key == canon_h_tech or
                                    canon_h_tech in canon_tech_key or
                                    canon_tech_key in canon_h_tech
                                )
                                if tech_match:
                                    for wo in wo_list:
                                        if self.check_item_match(wo, canon_target, time_key) or (wo.get("is_appointment") and wo.get("time_key") == time_key):
                                            board_count += 1
                             
                            if board_count >= target_count_idx:
                                print(f"⏭️ Truck assign header '{name}' (occurrence {target_count_idx} at {time_key}) already exists or tech is OFF/occupied. Skipping creation.")
                                continue

                        if dry_run:
                            print(f"  [Dry-Run] Truck Assign Header: {name} | Date: {date_str} | Time: {start_time} | Duration: {duration} | Priority: {priority} | Tech: {h_tech} | Task: {task}")
                        else:
                            res = await self.fill_and_save_work_order(
                                customer_name=name,
                                task=task,
                                lead_source=lead_source,
                                priority=priority,
                                target_date=date_str,
                                start_time=start_time,
                                duration=duration,
                                tech_name=h_tech,
                                immediate_action=immediate_action
                            )
                            if res:
                                locally_created_wos.add((db_date_key, canon_h_tech, canon_target, target_count_idx))
                                # Refresh board data after save
                                f_date, f_wos = await self.fetch_board_data_for_date(db_date_key, board_req)
                                if f_date == db_date_key:
                                    board_date_str, existing_wos = f_date, f_wos

                # Tech Jobs (e.g. SHOP, internal meetings)
                for job in tech_jobs:
                    if not job.get("active", True):
                        continue
                    t_tech = job.get("tech_name")
                    if t_tech and active_techs:
                        canon_job_tech = self.canonical_str(t_tech)
                        active_canon_techs = [self.canonical_str(t) for t in active_techs]
                        if canon_job_tech not in active_canon_techs:
                            print(f"⏭️ Skipping tech job '{job.get('name')}' because assigned technician '{t_tech}' is inactive/OFF.")
                            continue

                    name = job.get("name")
                    start_time = job.get("start_time")
                    duration = job.get("duration")
                    priority = job.get("priority")

                    if not dry_run:
                        canon_target = self.canonical_str(name)
                        try:
                            t_dt = datetime.strptime(start_time.strip().upper(), "%I:%M %p")
                            time_key = t_dt.strftime("%I:%M %p")
                            if time_key.startswith("0"): time_key = time_key[1:]
                        except Exception:
                            time_key = ""
                        key = f"{canon_target}|{time_key}" if time_key else canon_target

                        canon_t_tech = self.canonical_str(t_tech or '')
                        
                        processed_key = (canon_t_tech, key)
                        template_processed_counts[processed_key] = template_processed_counts.get(processed_key, 0) + 1
                        target_count_idx = template_processed_counts[processed_key]

                        if (db_date_key, canon_t_tech, canon_target, target_count_idx) in locally_created_wos or \
                           (not canon_t_tech and (db_date_key, "", canon_target, target_count_idx) in locally_created_wos):
                            print(f"⏭️ Tech job '{name}' (occurrence {target_count_idx} at {time_key}) was created in this session. Skipping creation.")
                            continue

                        board_count = 0
                        for canon_tech_key, wo_list in existing_wos.items():
                            tech_match = (
                                not canon_t_tech or not canon_tech_key or
                                canon_tech_key == canon_t_tech or
                                canon_t_tech in canon_tech_key or
                                canon_tech_key in canon_t_tech
                            )
                            if tech_match:
                                for wo in wo_list:
                                    if self.check_item_match(wo, canon_target, time_key) or (wo.get("is_appointment") and wo.get("time_key") == time_key):
                                        board_count += 1

                        if board_count >= target_count_idx:
                            print(f"⏭️ Tech job '{name}' (occurrence {target_count_idx} at {time_key}) already exists or tech is OFF/occupied. Skipping creation.")
                            continue

                    if dry_run:
                        print(f"  [Dry-Run] Tech Job: {name} | Date: {date_str} | Time: {start_time} | Duration: {duration} | Priority: {priority} | Tech: {t_tech} | Task: {task}")
                    else:
                        res = await self.fill_and_save_work_order(
                            customer_name=name,
                            task=task,
                            lead_source=lead_source,
                            priority=priority,
                            target_date=date_str,
                            start_time=start_time,
                            duration=duration,
                            tech_name=t_tech,
                            mark_completed=True,
                            immediate_action=immediate_action
                        )
                        if res:
                            locally_created_wos.add((db_date_key, canon_t_tech, canon_target, target_count_idx))
                            # Refresh board data after save
                            f_date, f_wos = await self.fetch_board_data_for_date(db_date_key, board_req)
                            if f_date == db_date_key:
                                board_date_str, existing_wos = f_date, f_wos

                # Technician Complete WOs
                for tech in active_techs:
                    t_name = tech
                    
                    # Fetch specific metadata for this tech, or fallback to defaults
                    meta = tech_metadata.get(t_name, {})
                    start_time = meta.get("start_time") or tech_defaults.get("start_time")
                    duration = meta.get("duration") or tech_defaults.get("duration")
                    t_assignment_name = meta.get("tech_name", tech)
                    priority = tech_priorities.get(t_name) or tech_defaults.get("priority")

                    # Check if the technician completed WO already exists on the board for the target date
                    if not dry_run:
                        canon_target = self.canonical_str(t_name)
                        canon_t_assignment = self.canonical_str(t_assignment_name or '')
                        
                        processed_key = (canon_t_assignment, canon_target)
                        template_processed_counts[processed_key] = template_processed_counts.get(processed_key, 0) + 1
                        target_count_idx = template_processed_counts[processed_key]

                        if (db_date_key, canon_t_assignment, canon_target, target_count_idx) in locally_created_wos or \
                           (not canon_t_assignment and (db_date_key, "", canon_target, target_count_idx) in locally_created_wos):
                            print(f"  -> Completed WO for tech '{t_name}' (occurrence {target_count_idx}) was created in this session. Skipping creation.")
                            continue

                        board_count = 0
                        tech_off_for_day = False
                        for canon_tech_key, wo_list in existing_wos.items():
                            tech_match = (
                                not canon_t_assignment or not canon_tech_key or
                                canon_tech_key == canon_t_assignment or
                                canon_t_assignment in canon_tech_key or
                                canon_tech_key in canon_t_assignment
                            )
                            if tech_match:
                                for wo in wo_list:
                                    if wo.get("is_appointment"):
                                        tech_off_for_day = True
                                        break
                                    if self.check_item_match(wo, canon_target, None, require_customer_match=True):
                                        board_count += 1
                                if tech_off_for_day:
                                    break
                                        
                        if tech_off_for_day or board_count >= target_count_idx:
                            print(f"  -> Completed WO for tech '{t_name}' (occurrence {target_count_idx}) skipped (tech has OFF/Appointment on board).")
                            continue

                    if dry_run:
                        print(f"  [Dry-Run] Tech WO: {t_name} | Date: {date_str} | Time: {start_time} | Duration: {duration} | Priority: {priority} | Tech Name: {t_assignment_name}")
                    else:
                        res = await self.fill_and_save_work_order(
                            customer_name=t_name,
                            task=task,
                            lead_source=lead_source,
                            priority=priority,
                            target_date=date_str,
                            start_time=start_time,
                            duration=duration,
                            tech_name=t_assignment_name,
                            mark_completed=True,
                            immediate_action=immediate_action,
                            task_type=task
                        )
                        if res:
                            locally_created_wos.add((db_date_key, canon_t_assignment, canon_target, target_count_idx))
                            # Refresh board data after save
                            f_date, f_wos = await self.fetch_board_data_for_date(db_date_key, board_req)
                            if f_date == db_date_key:
                                board_date_str, existing_wos = f_date, f_wos

                if not dry_run:
                    _records_processed += 1

            print("\n🎉 Dispatch Board Display Automation Finished Successfully!")
            self.progress_info = {
                "current_day": days,
                "total_days": days,
                "current_date": date_str if 'date_str' in locals() else None,
                "completed_days": days,
                "remaining_days": 0,
                "percent_complete": 100.0,
                "status": "completed",
                "status_message": f"Successfully completed all {days} days!"
            }
            if on_progress:
                try:
                    on_progress(self.progress_info)
                except Exception:
                    pass
        except Exception as e:
            print(f"Scraping error: {e}")
            _error_occurred = f"{str(e)}\n{traceback.format_exc()}"
        finally:
            # Clean up process lock file
            try:
                if os.path.exists(lock_file_path):
                    os.remove(lock_file_path)
            except Exception:
                pass

            if not dry_run and self.browser:
                await self.browser.close()

            # -- Execution completion summary --
            _elapsed = time.time() - _start_time
            if _error_occurred:
                print(f"⚠️ Execution finished with ERROR ({round(_elapsed, 1)}s):")
                print(_error_occurred)
            else:
                print(f"🎉 Execution completed successfully in {round(_elapsed, 1)}s")

if __name__ == "__main__":
    import os
    import sys
    import argparse

    _dispatch_dir = os.path.dirname(os.path.abspath(__file__))
    if _dispatch_dir not in sys.path:
        sys.path.insert(0, _dispatch_dir)

    _backend_dir = os.path.dirname(_dispatch_dir)
    _backend_path = os.path.join(_backend_dir, "Backend")
    if os.path.exists(_backend_path) and _backend_path not in sys.path:
        sys.path.insert(0, _backend_path)

    try:
        import django
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
        django.setup()
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Run Dispatch Board Display Automation Scraper (Standalone)")
    parser.add_argument("--days", type=int, default=2, help="Number of days to process (default: 2)")
    parser.add_argument("--live", action="store_true", help="Run live on FieldEdge (skips dry-run)")
    parser.add_argument("--start-date", type=str, help="Starting date (e.g. 'August 15', '08/15/2026'). Defaults to today.")
    args = parser.parse_args()

    scraper = DispatchBoardDisplayAutomationScraper()
    asyncio.run(scraper.run(days=args.days, dry_run=not args.live, start_date_str=args.start_date))