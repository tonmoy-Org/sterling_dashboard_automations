"""
Dispatch Board Display Automation Scraper
Automates the pre-creation of daily Dispatch Board headers, truck assignments, and tech routines in FieldEdge.
Maintains a rolling 31-day horizon of visual aid work orders with intelligent history tracking.
"""
import os
import re
import time
import json
import asyncio
import unicodedata
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple, Callable

try:
    from base_scraper import BaseScraper, get_pst_now, get_pst_today_str
except ImportError:
    from dispatch.base_scraper import BaseScraper, get_pst_now, get_pst_today_str


class DispatchBoardDisplayAutomationScraper(BaseScraper):
    """
    Automated scraper for FieldEdge Dispatch Board display items.
    Creates and manages Static Headers, Truck Assignments, Tech Jobs, and Active Tech routines.
    """

    _BANNER_EMOJI_REGEX = re.compile(
        r'[\u203C-\u3299\U0001F000-\U0001F9FF\U0001FA00-\U0001FAFF]+'
    )

    def __init__(self, template_path: Optional[str] = None):
        super().__init__()
        self.scraper_name = "dispatch-board-display-automation"
        _dispatch_dir = os.path.dirname(os.path.abspath(__file__))

        if template_path:
            self.template_path = template_path
        else:
            cand1 = os.path.join(_dispatch_dir, "config", "dispatch_board_template.json")
            cand2 = os.path.join(os.path.dirname(_dispatch_dir), "config", "dispatch_board_template.json")
            self.template_path = cand1 if os.path.exists(cand1) else cand2

        self.history_file = os.path.join(_dispatch_dir, "created_wos_history.json")
        self.template_data = self._load_template()
        self.intercepted_board_by_date: Dict[str, Any] = {}
        self.board_req: Dict[str, Any] = {}
        self.progress_info: Dict[str, Any] = {}
        self._on_progress_cb: Optional[Callable] = None
        self.asset_scraper = None

    # --- HISTORY TRACKING & PERSISTENCE ---

    def _load_history(self) -> Dict:
        """Load persistent history of created visual aid work orders."""
        api_url = os.getenv("BACKEND_API_URL", "").rstrip("/")
        if api_url:
            try:
                import urllib.request
                endpoint = f"{api_url}/automation/dispatch-history/" if api_url.endswith('/api') else f"{api_url}/api/automation/dispatch-history/"
                headers = {"User-Agent": "SterlingAutomations/1.0", **self._get_api_auth_headers()}
                req = urllib.request.Request(endpoint, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status == 200:
                        remote_records = json.loads(response.read().decode('utf-8'))
                        return {r["key"]: r for r in remote_records if isinstance(r, dict) and "key" in r}
            except Exception as e:
                print(f"⚠️ Could not load history from API: {e}. Falling back to local file.")

        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    @staticmethod
    def _is_created_record(record: Any) -> bool:
        if not isinstance(record, dict):
            return False
        return record.get("status") in ("created", "completed", "existing_on_board", "skipped_appointment_conflict")

    def is_wo_created(
        self,
        history: Dict,
        date_key: str,
        canon_tech: str,
        canon_target: str,
        target_count_idx: int,
        time_key: Optional[str] = None
    ) -> bool:
        """Check if work order is already recorded in history for this date/tech/item/occurrence."""
        norm_time = self.canonical_str(time_key) if time_key else ""

        if norm_time:
            key_time = f"{date_key}|{canon_tech}|{canon_target}_{norm_time}|{target_count_idx}"
            if key_time in history and self._is_created_record(history[key_time]):
                return True
            if not canon_tech:
                key_time_notech = f"{date_key}||{canon_target}_{norm_time}|{target_count_idx}"
                if key_time_notech in history and self._is_created_record(history[key_time_notech]):
                    return True

        base_key = f"{date_key}|{canon_tech}|{canon_target}|{target_count_idx}"
        if base_key in history and self._is_created_record(history[base_key]):
            rec = history[base_key]
            rec_time = rec.get("time") or (rec.get("details", {}).get("time") if isinstance(rec.get("details"), dict) else None)
            if not time_key or not rec_time or self.normalize_time_key(rec_time) == self.normalize_time_key(time_key):
                return True

        if not canon_tech:
            base_key_notech = f"{date_key}||{canon_target}|{target_count_idx}"
            if base_key_notech in history and self._is_created_record(history[base_key_notech]):
                rec = history[base_key_notech]
                rec_time = rec.get("time") or (rec.get("details", {}).get("time") if isinstance(rec.get("details"), dict) else None)
                if not time_key or not rec_time or self.normalize_time_key(rec_time) == self.normalize_time_key(time_key):
                    return True

        # Scan for matching occurrence in history dictionary
        for k, v in history.items():
            if not isinstance(v, dict) or not self._is_created_record(v):
                continue
            if v.get("date") == date_key or k.startswith(f"{date_key}|"):
                v_target = v.get("canon_target", "")
                v_base = v.get("base_target", "")
                target_matched = (v_target == canon_target or v_base == canon_target or (norm_time and v_target == f"{canon_target}_{norm_time}"))
                if not target_matched or v.get("occurrence") != target_count_idx:
                    continue

                rec_tech = v.get("canon_tech") or ""
                if rec_tech != canon_tech and not self.is_tech_match(rec_tech, canon_tech):
                    continue

                rec_time = v.get("time") or (v.get("details", {}).get("time") if isinstance(v.get("details"), dict) else None)
                if not time_key or not rec_time or self.normalize_time_key(rec_time) == self.normalize_time_key(time_key):
                    return True
        return False

    def is_date_fully_completed(
        self,
        history: Dict,
        db_date_key: str,
        static_headers: List[Dict],
        truck_assign_headers: List[Dict],
        tech_jobs: List[Dict],
        active_techs: List[str],
        is_weekend: bool,
        skip_vehicle_on_weekends: bool,
        force: bool = False
    ) -> bool:
        """Determines if all visual aid work orders for this date are already fully created."""
        if force:
            return False

        if self.is_wo_created(history, db_date_key, "", "date_processing_complete", 1):
            return True

        template_counts: Dict[Tuple[str, str], int] = {}

        def _is_item_done(name: str, tech: Optional[str], start_time: str, is_service_wo: bool = False, is_oos_wo: bool = False) -> bool:
            c_target = self.canonical_str(name)
            tracking_target = f"{c_target}_serviceneeded" if is_service_wo else (f"{c_target}_outofservice" if is_oos_wo else c_target)
            try:
                t_dt = datetime.strptime(start_time.strip().upper(), "%I:%M %p")
                time_key = t_dt.strftime("%I:%M %p").lstrip("0")
            except Exception:
                time_key = ""
            c_tech = self.canonical_str(tech or '')
            key = f"{tracking_target}|{time_key}" if time_key else tracking_target
            processed_key = (c_tech, key)
            template_counts[processed_key] = template_counts.get(processed_key, 0) + 1
            idx = template_counts[processed_key]
            return self.is_wo_created(history, db_date_key, c_tech, tracking_target, idx, time_key=time_key)

        for h in static_headers:
            is_sn = bool(h.get('is_service_needed_duplicate') or (h.get('service_note') and ('SERVICE NEEDED' in str(h.get('priority', '')) or '7.12' in str(h.get('priority', '')))))
            is_oos = bool(h.get('is_out_of_service') or (h.get('reason') and 'DO NOT DISPATCH' in str(h.get('priority', ''))))
            if not _is_item_done(h.get("name", ""), h.get("tech_name"), h.get("start_time", ""), is_sn, is_oos):
                return False

        if not (is_weekend and skip_vehicle_on_weekends):
            for h in truck_assign_headers:
                if not _is_item_done(h.get("name", ""), h.get("tech_name"), h.get("start_time", "")):
                    return False

        for j in tech_jobs:
            if j.get("active", True) and not _is_item_done(j.get("name", ""), j.get("tech_name"), j.get("start_time", "")):
                return False

        for t in active_techs:
            if not _is_item_done(t, t, "4:45 AM"):
                return False

        return True

    def record_wo_created(
        self,
        history: Dict,
        date_key: str,
        canon_tech: str,
        canon_target: str,
        target_count_idx: int,
        status: str = "created",
        details: Optional[Dict] = None
    ):
        """Record work order creation in local dictionary and async post to Dashboard API."""
        det = details or {}
        time_key = det.get("time", "")
        norm_time = self.canonical_str(time_key) if time_key else ""
        target_with_time = f"{canon_target}_{norm_time}" if norm_time else canon_target
        key = f"{date_key}|{canon_tech}|{target_with_time}|{target_count_idx}"
        rec_data = {
            "date": date_key,
            "canon_tech": canon_tech,
            "canon_target": target_with_time,
            "base_target": canon_target,
            "time": time_key,
            "occurrence": target_count_idx,
            "created_at": self.get_pst_now().isoformat(),
            "status": status,
            **det
        }
        history[key] = rec_data
        base_key = f"{date_key}|{canon_tech}|{canon_target}|{target_count_idx}"
        if base_key not in history:
            history[base_key] = rec_data

        api_url = os.getenv("BACKEND_API_URL", "").rstrip("/")
        if api_url:
            import threading, urllib.request
            endpoint = f"{api_url}/automation/dispatch-history/" if api_url.endswith('/api') else f"{api_url}/api/automation/dispatch-history/"
            req_data = json.dumps(rec_data).encode('utf-8')
            headers = {"Content-Type": "application/json", "User-Agent": "SterlingAutomations/1.0", **self._get_api_auth_headers()}

            def _send_post():
                try:
                    req = urllib.request.Request(endpoint, data=req_data, headers=headers, method="POST")
                    with urllib.request.urlopen(req, timeout=4):
                        pass
                except Exception:
                    pass

            threading.Thread(target=_send_post, daemon=True).start()

    # --- STRING EXTRACTION & PARSING UTILITIES ---

    def _extract_all_strings_recursive(self, obj: Any) -> List[str]:
        """Extract all string values from nested ScheduleItem JSON."""
        res = []
        if isinstance(obj, str):
            res.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                res.extend(self._extract_all_strings_recursive(v))
        elif isinstance(obj, list):
            for item in obj:
                res.extend(self._extract_all_strings_recursive(item))
        return res

    @staticmethod
    def normalize_time_key(time_str: str) -> str:
        """Normalizes time strings (e.g. '04:45 AM' -> '4:45 AM')."""
        if not time_str:
            return ""
        s = re.sub(r'[^a-zA-Z0-9:]', '', str(time_str)).upper()
        m = re.search(r'(\d{1,2}):(\d{2})([AP]M)?', s)
        if m:
            h = int(m.group(1))
            minute = m.group(2)
            p = m.group(3) or ""
            return f"{h}:{minute} {p}".strip()
        return time_str.strip().lstrip("0")

    def check_item_match(
        self,
        wo: Dict,
        canon_target: str,
        time_key: Optional[str] = None,
        is_service_wo: bool = False,
        is_oos_wo: bool = False,
        require_customer_match: bool = False,
        match_any_status: bool = False
    ) -> bool:
        """Determines if a live work order on the Dispatch Board matches the target specification."""
        c_cust = wo.get("canon_customer", "")
        raw_cust = wo.get("raw_customer", "")
        w_time = wo.get("time_key", "")
        wo_time_keys = wo.get("time_keys") or ({self.normalize_time_key(w_time)} if w_time else set())

        name_match = (
            c_cust == canon_target or
            self.is_same_equipment(raw_cust, canon_target) or
            (not require_customer_match and canon_target in wo.get("canon_strings", []))
        )
        if not name_match:
            return False

        if time_key:
            norm_target = self.normalize_time_key(time_key)
            norm_wo = self.normalize_time_key(w_time)
            time_match = not norm_target or not norm_wo or norm_target == norm_wo or (norm_target in wo_time_keys)
            if not time_match:
                return False

        if not match_any_status:
            if is_service_wo and not wo.get("is_service_needed"):
                return False
            if is_oos_wo and not wo.get("is_out_of_service"):
                return False
            if not is_service_wo and not is_oos_wo and (wo.get("is_service_needed") or wo.get("is_out_of_service")):
                return False

        return True

    def parse_fieldedge_datetime(self, dt_str: Optional[str]) -> Tuple[str, str, Optional[datetime.time]]:
        """Parses FieldEdge date/time strings to (YYYY-MM-DD, 12h time_key, time_obj)."""
        if not dt_str:
            return "", "", None
        raw_dt = str(dt_str).replace("Z", "").replace("T", " ")
        raw_dt = re.sub(r'[+-]\d{2}:?\d{2}$', '', raw_dt).split(".")[0].strip()

        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%I:%M %p", "%H:%M:%S", "%H:%M"]:
            try:
                dt = datetime.strptime(raw_dt, fmt)
                date_part = dt.strftime("%Y-%m-%d") if "%Y" in fmt else ""
                t_k = dt.strftime("%I:%M %p").lstrip("0")
                return date_part, t_k, dt.time()
            except Exception:
                pass
        return "", "", None

    def extract_existing_wos(self, board_data: Dict, target_date_key: Optional[str] = None) -> Tuple[Optional[str], Dict[str, List[Dict]]]:
        """Parses FieldEdge DispatchBoard JSON payload into existing_wos grouped by technician."""
        if not board_data or "Board" not in board_data:
            return None, {}

        board_date_str = target_date_key or None
        existing_wos: Dict[str, List[Dict]] = {}

        if not board_date_str and board_data["Board"].get("Headers"):
            first_hdr = board_data["Board"]["Headers"][0]
            dt_s = first_hdr.get("HeaderDateTime")
            if dt_s:
                try:
                    board_date_str = dt_s.replace("T", " ").split(" ")[0]
                except Exception:
                    pass

        effective_date = target_date_key or board_date_str

        for group in board_data["Board"].get("Groups", []):
            for row in group.get("Rows", []):
                canon_tech = self.canonical_str(row.get("Name") or "")
                if canon_tech not in existing_wos:
                    existing_wos[canon_tech] = []

                for item in row.get("ScheduleItems", []):
                    all_raw_strings = self._extract_all_strings_recursive(item)
                    canon_strings = [self.canonical_str(s) for s in all_raw_strings if s]
                    cust_name = str(item.get("CustomerName") or item.get("Customer") or item.get("Name") or item.get("Title") or "")
                    dt_str = item.get("ScheduleDateTime") or item.get("ScheduledDateTime") or item.get("StartDateTime") or item.get("StartTime")
                    item_date_str, time_key, time_obj = self.parse_fieldedge_datetime(dt_str)

                    if effective_date and item_date_str and item_date_str != effective_date:
                        continue

                    time_keys = {self.normalize_time_key(time_key)} if time_key else set()
                    item_prio = str(item.get("Priority") or item.get("PriorityName") or "")
                    norm_prio = re.sub(r'[^a-zA-Z0-9]', '', item_prio).lower()

                    is_sn = ("serviceneeded" in norm_prio or "712" in norm_prio or any("serviceneeded" in s.lower() or "712" in s for s in canon_strings))
                    is_oos = ("donotdispatch" in norm_prio or "outofservice" in norm_prio or any("donotdispatch" in s.lower() or "outofservice" in s for s in canon_strings))

                    existing_wos[canon_tech].append({
                        "id": item.get("Id") or item.get("WorkOrderId"),
                        "raw_customer": cust_name,
                        "canon_customer": self.canonical_str(cust_name),
                        "time_key": time_key,
                        "time_keys": time_keys,
                        "time_obj": time_obj,
                        "is_service_needed": is_sn,
                        "is_out_of_service": is_oos,
                        "canon_strings": canon_strings,
                        "is_appointment": bool(item.get("AppointmentId") or item.get("IsAppointment")),
                        "start_time_obj": time_obj
                    })

        return board_date_str, existing_wos

    # --- BOARD NAVIGATION & DATA FETCHING ---

    async def fetch_board_data_for_date(self, target_date_str: str, board_req: dict) -> Tuple[Optional[str], Dict[str, List[Dict]]]:
        """Dynamically fetch Dispatch Board JSON payload for target date using captured request."""
        req_to_use = board_req or self.board_req
        if not req_to_use or not req_to_use.get("url"):
            return None, {}

        try:
            payload = json.loads(req_to_use.get("post_data") or "{}")
            payload["Date"] = f"{target_date_str}T00:00:00"
            for k in list(payload.keys()):
                if "date" in k.lower():
                    payload[k] = f"{target_date_str}T23:59:59" if "end" in k.lower() else f"{target_date_str}T00:00:00"

            headers = {k: v for k, v in req_to_use.get("headers", {}).items() if k.lower() not in {'host', 'content-length', 'connection', 'origin', 'referer', 'sec-fetch-dest', 'sec-fetch-mode', 'sec-fetch-site'} and not k.startswith(':')}
            headers['Content-Type'] = 'application/json'

            fetch_js = f"""
            async () => {{
                try {{
                    const resp = await fetch("{req_to_use['url']}", {{
                        method: 'POST',
                        credentials: 'include',
                        headers: {json.dumps(headers)},
                        body: JSON.stringify({json.dumps(payload)})
                    }});
                    if (!resp.ok) return {{ error: 'HTTP ' + resp.status }};
                    return await resp.json();
                }} catch(e) {{
                    return {{ error: e.toString() }};
                }}
            }}
            """
            new_data = await self.page.evaluate(fetch_js)
            if new_data and isinstance(new_data, dict) and "Board" in new_data:
                self.intercepted_board_by_date[target_date_str] = new_data
                b_date, e_wos = self.extract_existing_wos(new_data, target_date_key=target_date_str)
                total_wos = sum(len(w_l) for w_l in e_wos.values())
                print(f"📡 Dynamically fetched board date: '{b_date}' | Found {len(e_wos)} tech groups, {total_wos} work orders.")
                return b_date, e_wos
        except Exception as e:
            print(f"⚠️ Exception fetching board data for {target_date_str}: {e}")
        return None, {}

    async def get_or_fetch_board_data_for_date(self, target_date_obj: datetime, max_retries: int = 2) -> Tuple[Optional[str], Optional[Dict]]:
        target_date_key = target_date_obj.strftime("%Y-%m-%d")

        if target_date_key in self.intercepted_board_by_date:
            b_data = self.intercepted_board_by_date[target_date_key]
            b_date, e_wos = self.extract_existing_wos(b_data, target_date_key=target_date_key)
            return b_date, e_wos

        await self.navigate_to_board_date(target_date_obj)

        if self.board_req and self.board_req.get("url"):
            print(f"🔄 Fetching FieldEdge API board data for target date {target_date_key}...")
            b_date, e_wos = await self.fetch_board_data_for_date(target_date_key, self.board_req)
            if b_date == target_date_key and e_wos is not None:
                return b_date, e_wos

        if max_retries > 0:
            await self.page.wait_for_timeout(1000)
            b_date, e_wos = await self.fetch_board_data_for_date(target_date_key, self.board_req)
            if b_date == target_date_key and e_wos is not None:
                return b_date, e_wos

        print(f"⛔ CRITICAL SAFETY GUARD: Could NOT verify FieldEdge Dispatch Board API for date {target_date_key}!")
        return None, None

    async def navigate_to_board_date(self, target_date_obj: datetime) -> bool:
        try:
            date_url_str = target_date_obj.strftime("%m-%d-%Y")
            target_url = f"https://login.fieldedge.com/#/DispatchBoard/{date_url_str}"
            print(f"🗓️ Navigating to Dispatch Board URL: {target_url}")
            await self.page.goto(target_url, wait_until='domcontentloaded', timeout=30000)
            await self.page.reload(wait_until='domcontentloaded', timeout=30000)
            try:
                spinners = self.page.locator(".loading, .spinner, .v-spinner, div[class*='loading']:visible")
                if await spinners.count() > 0:
                    await spinners.first.wait_for(state="hidden", timeout=4000)
            except Exception:
                pass
            await self.page.wait_for_timeout(1000)
            return True
        except Exception as e:
            print(f"⚠️ Could not navigate to board date via URL: {e}")
            return False

    async def ensure_clean_dispatch_board(self, force_reload: bool = False):
        dispatch_url = self.rules.get('web_url', 'https://login.fieldedge.com/Dispatch')
        current_url = self.page.url

        if "Login" in current_url or await self.page.locator("input[name='UserName']").is_visible():
            print("⚠️ Detected Login redirect. Re-authenticating to FieldEdge...")
            await self.login_fieldedge()
            await self.page.goto(dispatch_url, wait_until='domcontentloaded')
            await self.page.wait_for_timeout(500)
            return

        if force_reload or "Dispatch" not in current_url or "DispatchSummary" in current_url:
            print(f"🔄 Reloading Dispatch Board URL: {dispatch_url}")
            await self.page.goto(dispatch_url, wait_until='domcontentloaded', timeout=30000)
            await self.page.wait_for_timeout(500)

    # --- WORK ORDER MODAL CONTROLS ---

    async def open_create_wo_modal(self) -> bool:
        """Opens Create Work Order modal, handling loading spinners and reload fallbacks."""
        customer_sel = self.rules.get(
            'dispatch_board_customer_xpath',
            "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Customer Name')]/following::div[contains(@class,'v-select')][1]"
        )
        if await self.page.locator(customer_sel).count() > 0 and await self.page.locator(customer_sel).first.is_visible():
            return True

        await self.ensure_clean_dispatch_board()

        btn_candidates = [
            self.rules.get('dispatch_board_create_wo_btn_xpath', "//button[contains(normalize-space(), 'Work Order')]"),
            "button:has-text('Create Work Order')",
            "button:has-text('Work Order')",
            "button[data-automation-id*='create-wo']"
        ]

        for attempt in range(2):
            for cand in btn_candidates:
                btn = self.page.locator(cand).first
                if await btn.count() > 0 and await btn.is_visible():
                    try:
                        await btn.click(timeout=2000)
                    except Exception:
                        await self.page.evaluate("(el) => el.click()", await btn.element_handle())
                    break

            start_t = time.time()
            while time.time() - start_t < 8:
                if await self.page.locator(customer_sel).count() > 0 and await self.page.locator(customer_sel).first.is_visible():
                    return True
                await self.page.wait_for_timeout(200)

            if attempt == 0:
                print("⚠️ Retrying opening modal with fresh Dispatch Board reload...")
                await self.ensure_clean_dispatch_board(force_reload=True)

        return False

    async def close_create_wo_modal(self) -> bool:
        """Closes open Create Work Order modal using Cancel button or Escape."""
        try:
            for cand in ["button:has-text('Cancel')", "button.close", "[aria-label='Close']"]:
                btn = self.page.locator(cand).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(force=True)
                    await self.page.wait_for_timeout(200)
                    return True
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(200)
            return True
        except Exception:
            return False

    @staticmethod
    def extract_name_variants(text: str) -> List[str]:
        """Extracts name search variants (with/without emojis, after pipes)."""
        if not text:
            return []
        raw = str(text).strip()
        variants = [raw]
        no_emoji = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f\u200d]', '', raw).strip()
        if no_emoji and no_emoji != raw:
            variants.append(no_emoji)
        clean_core = re.sub(r'^[|#\-_\s\u2000-\u200f]+|[|#\-_\s\u2000-\u200f]+$', '', no_emoji).strip()
        if clean_core and clean_core not in variants:
            variants.append(clean_core)
        if "|" in raw:
            right_side = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f\u200d\u2000-\u200f]', '', raw.split("|")[-1]).strip()
            if right_side and right_side not in variants:
                variants.append(right_side)
        return variants

    @classmethod
    def has_banner_prefix(cls, t: str) -> bool:
        return ('|' in str(t)) or bool(cls._BANNER_EMOJI_REGEX.search(str(t)))

    # --- DROPDOWN SELECTION & VERIFICATION ---

    async def select_dropdown_option(
        self,
        dropdown_selector: str,
        option_text: str,
        press_enter: bool = False,
        is_tech: bool = False,
        is_customer: bool = False
    ) -> bool:
        """
        Selects an option in a Vue-Select dropdown.
        Strictly enforces exact technician names without fuzzy/partial searches.
        Enforces category and emoji guards on equipment.
        """
        if not option_text:
            return False

        try:
            dropdown = self.page.locator(dropdown_selector).first
            await dropdown.wait_for(state="visible", timeout=10000)
            await dropdown.scroll_into_view_if_needed()

            if not is_customer:
                is_customer = "customer" in dropdown_selector.lower()
            if not is_tech:
                is_tech = "primary tech" in dropdown_selector.lower() or "tech" in dropdown_selector.lower()

            # Check existing selection
            sel_el = dropdown.locator(".vs__selected, .selected-tag").first
            dd_class = await dropdown.get_attribute("class") or ""
            if await sel_el.count() > 0 and await sel_el.is_visible():
                cur_val = (await sel_el.text_content() or "").strip()
                clean_cur = re.sub(r'[\u00d7xX]$', '', cur_val).strip()

                is_placeholder = (
                    "no-option-selected" in dd_class or
                    not clean_cur or
                    clean_cur.lower() in (
                        "select", "select primary tech", "select tech", "select technician",
                        "type to search", "none", "select..."
                    ) or
                    clean_cur.lower().startswith("select ")
                )

                if clean_cur and not is_placeholder:
                    already_matched = False
                    if is_tech:
                        already_matched = self.is_tech_match(clean_cur, option_text) or self.is_tech_match(cur_val, option_text)
                    elif self.is_same_equipment(clean_cur, option_text) or clean_cur == option_text or self.canonical_str(clean_cur) == self.canonical_str(option_text):
                        already_matched = True

                    if already_matched:
                        print(f"  -> '{option_text}' already selected in dropdown ('{cur_val}'). Proceeding.")
                        return True

                    print(f"  -> Clearing existing mismatched selection '{cur_val}' (expected '{option_text}')...")
                    clear_btn = dropdown.locator(".vs__clear, button.close, button.vs__clear").first
                    if await clear_btn.count() > 0 and await clear_btn.is_visible():
                        await clear_btn.click(force=True)
                        await self.page.wait_for_timeout(200)

            # Ensure dropdown menu is open before searching/selecting
            dd_class = await dropdown.get_attribute("class") or ""
            is_open = (
                "vs--open" in dd_class or
                await dropdown.locator(".vs__dropdown-toggle[aria-expanded='true']").count() > 0 or
                await dropdown.locator("ul.vs__dropdown-menu:visible, ul[role='listbox']:visible, li.vs__dropdown-option:visible").count() > 0 or
                await self.page.locator("li.vs__dropdown-option:visible").count() > 0
            )
            if not is_open:
                toggle = dropdown.locator(".vs__dropdown-toggle, .arrow_icon, .vs__open-indicator").first
                if await toggle.count() > 0:
                    await toggle.click(force=True)
                else:
                    await dropdown.click(force=True)
                await self.page.wait_for_timeout(250)

            search_input = dropdown.locator("input.vs__search, input").first
            if await search_input.count() == 0 or not await search_input.is_visible():
                search_input = self.page.locator("input.vs__search:visible, input[type='search']:visible").first

            if await search_input.count() > 0:
                try:
                    await search_input.focus()
                    await search_input.fill("")
                except Exception:
                    pass

            # Prepare search term
            type_text = str(option_text).strip()
            clean_search = unicodedata.normalize('NFKD', type_text).strip()
            target_canon = self.canonical_str(type_text)

            name_variants = [type_text] if is_tech else self.extract_name_variants(option_text)
            target_canons = {self.canonical_str(v) for v in name_variants if self.canonical_str(v)}
            target_has_banner = False if is_tech else self.has_banner_prefix(option_text)

            if await search_input.count() > 0:
                await search_input.fill(type_text)
                await self.page.wait_for_timeout(250)

            # Re-check open state: typing in vue-select opens menu automatically.
            # Only click toggle if it's strictly not open.
            dd_class = await dropdown.get_attribute("class") or ""
            is_open = (
                "vs--open" in dd_class or
                await dropdown.locator(".vs__dropdown-toggle[aria-expanded='true']").count() > 0 or
                await dropdown.locator("ul.vs__dropdown-menu:visible, ul[role='listbox']:visible, li.vs__dropdown-option:visible").count() > 0 or
                await self.page.locator("li.vs__dropdown-option:visible").count() > 0
            )
            if not is_open:
                toggle = dropdown.locator(".arrow_icon, .vs__open-indicator, .vs__actions").first
                if await toggle.count() > 0:
                    await toggle.click(force=True)
                else:
                    await dropdown.click(force=True)
                await self.page.wait_for_timeout(250)

            if is_customer:
                print(f"  🔍 Entered customer search '{type_text}'. Pressing Enter to initiate search...")
                await search_input.press("Enter")
                await self.page.wait_for_timeout(500)
                try:
                    spinner = dropdown.locator(".vs__spinner:visible, .spinner:visible")
                    if await spinner.count() > 0:
                        await spinner.first.wait_for(state="hidden", timeout=12000)
                except Exception:
                    pass
            else:
                await self.page.wait_for_timeout(250)

            async def _trigger_select(opt_el) -> bool:
                """
                Dispatches mousedown, mouseup, and click directly on the dropdown option DOM element.
                Vue-Select binds selection to @mousedown.prevent.stop="select(option)",
                so dispatching native DOM mousedown is required to trigger selection reliably.
                """
                try:
                    await opt_el.evaluate("""(el) => {
                        el.scrollIntoView({ behavior: 'instant', block: 'nearest' });
                        ['mousedown', 'mouseup', 'click'].forEach(evt => {
                            el.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                        });
                    }""")
                except Exception:
                    pass
                try:
                    await opt_el.click(force=True)
                except Exception:
                    pass
                await self.page.wait_for_timeout(250)
                return True

            async def _check_and_click_option(opt_candidate, is_fuzzy: bool = False) -> bool:
                try:
                    raw_text = (await opt_candidate.text_content() or "").strip()
                except Exception:
                    return False
                if not raw_text:
                    return False
                clean_opt = re.sub(r'[\u00d7xX]$', '', raw_text).strip()
                first_line = clean_opt.splitlines()[0].strip() if clean_opt else ""

                if is_tech:
                    if self.is_tech_match(clean_opt, option_text) or self.is_tech_match(first_line, option_text):
                        print(f"  -> Selecting matched technician option: '{clean_opt}'")
                        return await _trigger_select(opt_candidate)
                    return False

                # Equipment & fleet check
                if any(x in option_text for x in ("🚛", "🛻", "🏗")) or any(x in clean_opt for x in ("🚛", "🛻", "🏗")):
                    if self.is_same_equipment(option_text, clean_opt) or self.is_same_equipment(option_text, first_line):
                        print(f"  -> Selecting matched equipment option: '{clean_opt}'")
                        return await _trigger_select(opt_candidate)
                    return False

                if self.has_banner_prefix(clean_opt) != target_has_banner:
                    return False

                c_opt = self.canonical_str(clean_opt)
                c_first = self.canonical_str(first_line)
                if not is_fuzzy:
                    if (
                        clean_opt == type_text or
                        clean_opt.lower() == type_text.lower() or
                        clean_opt in name_variants or
                        c_opt in target_canons or
                        c_first in target_canons
                    ):
                        print(f"  -> Selecting matched dropdown option: '{clean_opt}'")
                        return await _trigger_select(opt_candidate)
                else:
                    if target_canon and len(target_canon) >= 4 and (target_canon in c_opt or c_opt in target_canon):
                        print(f"  -> Selecting fuzzy matched dropdown option: '{clean_opt}'")
                        return await _trigger_select(opt_candidate)
                return False

            clicked = False
            poll_start = time.time()
            max_wait = 12.0 if is_customer else 6.0

            while time.time() - poll_start < max_wait:
                # 1. First priority: Check highlighted option
                # Vue-select highlights the best match as you type (.vs__dropdown-option--highlight)
                high_opts = dropdown.locator("li.vs__dropdown-option--highlight:visible")
                if await high_opts.count() == 0:
                    high_opts = self.page.locator("li.vs__dropdown-option--highlight:visible")

                if await high_opts.count() > 0:
                    if await _check_and_click_option(high_opts.first, is_fuzzy=False):
                        clicked = True
                        break

                # 2. Check all visible options (within dropdown, then globally)
                options = dropdown.locator("ul.vs__dropdown-menu li:visible, ul[role='listbox'] li:visible, li.vs__dropdown-option:visible")
                count = await options.count()
                if count == 0:
                    options = self.page.locator(".vs__dropdown-menu:visible li:visible, ul[role='listbox']:visible li:visible, li.vs__dropdown-option:visible")
                    count = await options.count()

                for i in range(count):
                    if await _check_and_click_option(options.nth(i), is_fuzzy=False):
                        clicked = True
                        break

                if clicked:
                    break

                await self.page.wait_for_timeout(250)

            # Fallback fuzzy scan for non-tech items
            if not clicked and not is_tech:
                options = dropdown.locator("ul.vs__dropdown-menu li:visible, ul[role='listbox'] li:visible, li.vs__dropdown-option:visible")
                count = await options.count()
                if count == 0:
                    options = self.page.locator(".vs__dropdown-menu:visible li:visible, ul[role='listbox']:visible li:visible, li.vs__dropdown-option:visible")
                    count = await options.count()
                for i in range(count):
                    if await _check_and_click_option(options.nth(i), is_fuzzy=True):
                        clicked = True
                        break

            if not clicked:
                if (press_enter or is_customer) and not is_tech:
                    print(f"  -> Fallback Enter for '{option_text}'...")
                    await search_input.press("Enter")
                    await self.page.wait_for_timeout(400)
                else:
                    print(f"⚠️ Could not find exact matching option for '{option_text}' in dropdown. Aborting selection.")
                    return False

            # Verify selection
            verify_start = time.time()
            is_verified = False
            selected_val = ""

            while time.time() - verify_start < (12.0 if is_customer else 5.0):
                sel_check = dropdown.locator(".vs__selected, .selected-tag").first
                if await sel_check.count() > 0:
                    selected_val = (await sel_check.text_content() or "").strip()
                    clean_val = re.sub(r'[\u00d7xX]$', '', selected_val).strip()

                    # Ignore placeholder if still showing
                    if not clean_val or clean_val.lower() in ("select", "select primary tech", "select tech", "select technician", "type to search"):
                        await self.page.wait_for_timeout(200)
                        continue

                    if is_tech:
                        if self.is_tech_match(clean_val, option_text) or self.is_tech_match(selected_val, option_text):
                            is_verified = True
                            break
                    else:
                        banner_ok = (self.has_banner_prefix(selected_val) == target_has_banner)
                        equip_ok = self.is_same_equipment(selected_val, option_text) if any(x in option_text for x in ("🚛", "🛻", "🏗")) else True
                        if banner_ok and equip_ok and (
                            clean_val == type_text or
                            clean_val.lower() == type_text.lower() or
                            self.canonical_str(clean_val) in target_canons or
                            clean_val in name_variants
                        ):
                            is_verified = True
                            break
                await self.page.wait_for_timeout(200)

            if not is_verified:
                print(f"❌ Selection verification failed: expected '{option_text}', but dropdown selected '{selected_val}'. Aborting selection.")
                clear_btn = dropdown.locator(".vs__clear, button.close").first
                if await clear_btn.count() > 0 and await clear_btn.is_visible():
                    await clear_btn.click(force=True)
                return False

            print(f"  ✅ Dropdown selected '{option_text}' successfully.")
            return True
        except Exception as e:
            print(f"⚠️ Failed to select '{option_text}' in dropdown: {e}")
            return False

    # --- WORK ORDER FILL & SAVE ---

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
        task_type: Optional[str] = None,
        summary_note: Optional[str] = None
    ) -> bool:
        """Fills all fields in Create Work Order modal, validates data, saves, and confirms completion if requested."""
        try:
            if not await self.open_create_wo_modal():
                return False

            await self.page.wait_for_timeout(400)

            # 1. Customer
            print(f"  [1/7] Customer: '{customer_name}'")
            cust_xpath = self.rules.get('dispatch_board_customer_xpath', "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Customer Name')]/following::div[contains(@class,'v-select')][1]")
            if not await self.select_dropdown_option(cust_xpath, customer_name, press_enter=True, is_customer=True):
                print(f"⚠️ Failed to select Customer '{customer_name}'. Aborting.")
                await self.close_create_wo_modal()
                return False

            # Wait for customer data spinners
            load_start = time.time()
            form_ready = False
            task_xpath = self.rules.get('dispatch_board_task_xpath', "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Task') and not(contains(normalize-space(),'Duration'))]/following::div[contains(@class,'v-select')][1]")

            while time.time() - load_start < 45:
                spinners = self.page.locator(".spinner:visible, .vs__spinner:visible, .loading:visible")
                task_el = self.page.locator(task_xpath).first
                task_vis = await task_el.count() > 0 and await task_el.is_visible()
                task_cls = (await task_el.get_attribute("class") or "") if task_vis else ""

                dialog = self.page.locator("//div[contains(@class,'v-dialog') or contains(@class,'modal')]//button[contains(.,'Confirm') or contains(.,'OK') or contains(.,'Yes')]").first
                if await dialog.count() > 0 and await dialog.is_visible():
                    await dialog.click(force=True)

                if (await spinners.count()) == 0 and task_vis and "disabled" not in task_cls:
                    form_ready = True
                    break
                await self.page.wait_for_timeout(250)

            if not form_ready:
                print(f"  ❌ Form not ready after 45s for Customer '{customer_name}'. Aborting.")
                await self.close_create_wo_modal()
                return False

            # 2. Immediate Action
            ia_xpath = self.rules.get('dispatch_board_immediate_action_xpath', "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Immediate Action')]/following::div[contains(@class,'v-select')][1]")
            await self.select_dropdown_option(ia_xpath, immediate_action, press_enter=False)

            # 3. Task
            if not await self.select_dropdown_option(task_xpath, task, press_enter=False):
                print(f"❌ Failed to select Task '{task}'. Aborting work order.")
                await self.close_create_wo_modal()
                return False

            # 4. Lead Source
            lead_xpath = self.rules.get('dispatch_board_lead_source_xpath', "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Work Order Lead Source')]/following::div[contains(@class,'v-select')][1]")
            if not await self.select_dropdown_option(lead_xpath, lead_source, press_enter=False):
                print(f"❌ Failed to select Lead Source '{lead_source}'. Aborting work order.")
                await self.close_create_wo_modal()
                return False

            # 5. Priority
            prio_xpath = self.rules.get('dispatch_board_priority_xpath', "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Priority')]/following::div[contains(@class,'v-select')][1]")
            if not await self.select_dropdown_option(prio_xpath, priority, press_enter=False):
                print(f"❌ Failed to select Priority '{priority}'. Aborting work order.")
                await self.close_create_wo_modal()
                return False

            # 6. Primary Tech
            tech_xpath = self.rules.get('dispatch_board_tech_name_xpath', "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Primary Tech')]/following::div[contains(@class,'v-select')][1]")
            if tech_name:
                print(f"  [6/7] Primary Tech: '{tech_name}'")
                if not await self.select_dropdown_option(tech_xpath, tech_name, press_enter=False, is_tech=True):
                    print(f"❌ Failed to select technician '{tech_name}'. Aborting work order.")
                    await self.close_create_wo_modal()
                    return False

            # 7. Date, Time, Duration
            print(f"  [7/7] Date: {target_date} | Time: {start_time} | Duration: {duration}")

            async def _fill_field(locator, val: str):
                if await locator.count() > 0 and await locator.is_visible():
                    await locator.click(force=True)
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                    await locator.type(val, delay=25)
                    await self.page.keyboard.press("Enter")
                    await locator.evaluate(f"el => {{ el.value = '{val}'; el.dispatchEvent(new Event('input', {{ bubbles: true }})); el.dispatchEvent(new Event('change', {{ bubbles: true }})); }}")
                    await self.page.keyboard.press("Tab")

            date_input = self.page.locator(self.rules.get('dispatch_board_date_input_xpath', "//input[@name='Start Date']")).first
            await _fill_field(date_input, target_date)

            time_input = self.page.locator(self.rules.get('dispatch_board_time_input_xpath', "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Start Time')]/following::input[1]")).first
            await _fill_field(time_input, start_time)

            duration_input = self.page.locator(self.rules.get('dispatch_board_duration_input_xpath', "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Task Duration')]/following::input[contains(@class,'base-input')][1]")).first
            fmt_dur = ("0" + duration) if len(duration) == 4 and ":" in duration else duration
            await _fill_field(duration_input, fmt_dur)

            # Work Summary Note
            if summary_note:
                ta = self.page.locator(".summary-note-container_f6fBE textarea, textarea[name='WorkSummary'], .text-area_he5R6").first
                if await ta.count() > 0 and await ta.is_visible():
                    await ta.fill(summary_note)
                    await ta.evaluate("el => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }")

            # Final Safety Guards: Verify Tech & Customer before clicking Save
            if tech_name:
                final_tech = (await self.page.locator(tech_xpath).locator(".vs__selected").text_content() or "").strip()
                if not (final_tech == tech_name or self.is_tech_match(final_tech, tech_name)):
                    print(f"🛑 FINAL PRE-SAVE CHECK FAILED: Tech '{final_tech}' != expected '{tech_name}'. Aborting save.")
                    await self.close_create_wo_modal()
                    return False

            final_cust = (await self.page.locator(cust_xpath).locator(".vs__selected, .selected-tag").first.text_content() or "").strip()
            if not (final_cust == customer_name or self.is_same_equipment(final_cust, customer_name) or self.canonical_str(final_cust) == self.canonical_str(customer_name)):
                print(f"🛑 FINAL PRE-SAVE CHECK FAILED: Customer '{final_cust}' != expected '{customer_name}'. Aborting save.")
                await self.close_create_wo_modal()
                return False

            # Save Button
            save_btn = self.page.locator("//button[contains(@class,'confirm') and .//span[normalize-space()='Save']], button:has-text('Save')").last
            await save_btn.wait_for(state="visible", timeout=8000)
            await save_btn.click(force=True)
            print(f"✅ Clicked Save for: '{customer_name}'")

            # Wait for modal to close
            save_start = time.time()
            modal_closed = False
            while time.time() - save_start < 25:
                if "DispatchSummary" in self.page.url:
                    modal_closed = True
                    break
                if (await self.page.locator(cust_xpath).count() == 0) or not await self.page.locator(cust_xpath).first.is_visible():
                    modal_closed = True
                    break
                # Handle any conflict popup
                popup_btn = self.page.locator("//div[contains(@class,'v-dialog') or contains(@class,'modal')]//button[contains(.,'Confirm') or contains(.,'Save Anyway') or contains(.,'Proceed')]").first
                if await popup_btn.count() > 0 and await popup_btn.is_visible():
                    await popup_btn.click(force=True)
                await self.page.wait_for_timeout(300)

            if not modal_closed:
                print(f"❌ Modal did not close after 25s for '{customer_name}'. Aborting.")
                await self.close_create_wo_modal()
                return False

            # Mark Completed via DispatchSummary
            if mark_completed:
                print("  -> Finalizing 'Completed' status on DispatchSummary...")
                summary_save = self.page.locator("button#dispatch-summary-save-workorder").first
                try:
                    await summary_save.wait_for(state="visible", timeout=15000)
                    await summary_save.click(force=True)

                    confirm_btn = self.page.locator("button#confirm-save-dispatch-summary, button:has-text('Confirm')").first
                    modal_open = False
                    for _ in range(25):
                        if await confirm_btn.count() > 0 and await confirm_btn.is_visible():
                            modal_open = True
                            break
                        await self.page.wait_for_timeout(200)

                    if modal_open:
                        radio = self.page.locator(".field-radio-container").filter(has_text="Completed").first
                        if await radio.count() > 0:
                            await radio.click(force=True)
                            await self.page.wait_for_timeout(300)
                        await confirm_btn.click(force=True)
                        await confirm_btn.wait_for(state="hidden", timeout=8000)
                        print(f"🎉 Active Tech WO '{customer_name}' saved & completed!")
                except Exception as sum_err:
                    print(f"  Note on DispatchSummary completion: {sum_err}")

            return True
        except Exception as e:
            print(f"⚠️ Error saving work order for '{customer_name}': {e}")
            await self.close_create_wo_modal()
            return False

    # --- MAIN AUTOMATION RUNNER ---

    async def run(
        self,
        days: int = 30,
        dry_run: bool = False,
        active_techs: Optional[List[str]] = None,
        start_date_str: Optional[str] = None,
        on_progress: Optional[Callable] = None,
        skip_asset: bool = False,
        force: bool = False
    ):
        """Runs the rolling multi-day automation loop for Dispatch Board and Asset Assignment reconciliation."""
        self._on_progress_cb = on_progress
        self.progress_info = {
            "current_day": 0, "total_days": days, "current_date": None,
            "completed_days": 0, "remaining_days": days, "percent_complete": 0.0,
            "status": "starting", "status_message": f"Initializing automation for {days} days...",
            "fieldedge_status": "Connecting / Logging in..."
        }
        if on_progress:
            on_progress(self.progress_info)

        self.template_data = self._load_template()
        settings = self.template_data.get("general_settings", {})
        task = settings.get("task", "8 - INTERNAL")
        lead_source = settings.get("lead_source", "Sterling Septic -FE")
        immediate_action = settings.get("immediate_action", "Create Work Order")
        skip_vehicle_on_weekends = settings.get("skip_vehicle_work_orders_on_weekends", True)

        truck_assign_headers = [
            h for h in self.template_data.get("truck_assign_headers", [])
            if h.get("active", True) and (h.get("tech_name") or "").strip() and (h.get("tech_name") or "").strip().lower() not in ("unassigned", "none", "clear")
        ]
        static_headers = [h for h in self.template_data.get("static_headers", []) if h.get("active", True)]

        # Process Out of Service assets
        out_of_service_headers = []
        oos_canonical_names = set()
        for oos in self.template_data.get("out_of_service_assets", []):
            if not oos.get("active", True):
                continue
            oos_name = (oos.get("asset_name") or "").strip()
            if oos_name:
                oos_canonical_names.add(self.canonical_str(oos_name))
                out_of_service_headers.append({
                    "name": oos_name,
                    "start_time": oos.get("start_time") or "1:45 PM",
                    "duration": oos.get("duration") or "1:30",
                    "priority": oos.get("priority") or "[4] DO NOT DISPATCH (Tracking)",
                    "tech_name": oos.get("tech_name") or "s4",
                    "service_note": oos.get("service_note") or oos.get("reason", ""),
                    "is_out_of_service": True,
                    "active": True,
                    "task": oos.get("task", task),
                })

        if oos_canonical_names:
            static_headers = [h for h in static_headers if self.canonical_str(h.get("name", "")) not in oos_canonical_names]
            truck_assign_headers = [h for h in truck_assign_headers if self.canonical_str(h.get("name", "")) not in oos_canonical_names]

        # Process Service Needed assets
        service_needed_headers = []
        for sn in self.template_data.get("service_needed_assets", []):
            if not sn.get("active", True):
                continue
            sn_name = (sn.get("asset_name") or "").strip()
            if sn_name and self.canonical_str(sn_name) not in oos_canonical_names:
                service_needed_headers.append({
                    "name": sn_name,
                    "start_time": sn.get("start_time") or "3:30 PM",
                    "duration": sn.get("duration") or "1:00",
                    "priority": sn.get("priority") or "[7] 7.12 SERVICE NEEDED",
                    "tech_name": "s4",
                    "service_note": sn.get("service_note") or "",
                    "is_service_needed_duplicate": True,
                    "active": True,
                    "task": sn.get("task", task),
                })

        if service_needed_headers:
            static_headers.extend(service_needed_headers)

        template_techs = self.template_data.get("active_techs", [])
        if not active_techs:
            active_techs = [e.get("name", "") for e in template_techs if e.get("name") and e.get("active", True)]

        tech_jobs = self.template_data.get("tech_jobs", [])

        # Start Date resolution
        start_dt = self.get_pst_now().date()
        if start_date_str:
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%b %d", "%B %d"):
                try:
                    dt = datetime.strptime(start_date_str, fmt)
                    if "%Y" not in fmt:
                        dt = dt.replace(year=start_dt.year)
                    start_dt = dt.date()
                    break
                except ValueError:
                    pass

        print(f"🚀 Starting Dispatch Board Automation for {days} days from {start_dt}...")

        if not dry_run:
            await self.initialize()
            self.intercepted_board_by_date.clear()
            self.board_req.clear()

            async def handle_request(req):
                if "GetDispatchBoardData" in req.url and req.method == "POST":
                    self.board_req["url"] = req.url
                    self.board_req["headers"] = req.headers
                    self.board_req["post_data"] = req.post_data

            self.page.on("request", handle_request)
            dispatch_url = self.rules.get('web_url', 'https://login.fieldedge.com/Dispatch')
            await self.page.goto(dispatch_url, wait_until='domcontentloaded')
            if "Login" in self.page.url:
                await self.login_fieldedge()

            for _ in range(50):
                if self.board_req.get("url"):
                    break
                await self.page.wait_for_timeout(200)

        created_history = self._load_history()
        print(f"📚 Loaded {len(created_history)} recorded entries from Central Dashboard API.")

        for d in range(days):
            current_date = start_dt + timedelta(days=d)
            date_str = f"{current_date.month}/{current_date.day}/{current_date.strftime('%y')}"
            db_date_key = current_date.strftime("%Y-%m-%d")
            is_weekend = current_date.weekday() >= 5

            skip_stage1 = False
            if not dry_run and not force and self.is_date_fully_completed(
                created_history, db_date_key, static_headers, truck_assign_headers,
                tech_jobs, active_techs, is_weekend, skip_vehicle_on_weekends, force
            ):
                print(f"\n⚡ [Smart Skip: Stage 1] All items for {date_str} ({db_date_key}) are already recorded in history.")
                skip_stage1 = True

            self.progress_info.update({
                "current_day": d + 1, "current_date": date_str, "completed_days": d,
                "remaining_days": days - (d + 1), "percent_complete": round(((d + 1) / days) * 100, 1),
                "status": "processing", "status_message": f"Processing Day {d + 1} of {days} ({date_str})"
            })
            if on_progress:
                on_progress(self.progress_info)

            print(f"\n--- Processing Date: {date_str} (Day {d + 1}/{days}) ---")

            if not skip_stage1:
                existing_wos = {}
                if not dry_run:
                    b_date, e_wos = await self.get_or_fetch_board_data_for_date(current_date)
                    if e_wos is None:
                        print(f"⛔ Could NOT verify Dispatch Board data for {date_str}. Skipping to prevent duplicates.")
                        continue
                    existing_wos = e_wos

                template_processed_counts: Dict[Tuple[str, str], int] = {}

                async def _sync_item(
                    item_name: str,
                    item_tech: Optional[str],
                    item_time: str,
                    item_priority: str,
                    item_duration: str = "1:00",
                    item_task: str = task,
                    item_lead: str = lead_source,
                    item_action: str = immediate_action,
                    item_summary: Optional[str] = None,
                    is_sn: bool = False,
                    is_oos: bool = False,
                    is_completed: bool = False,
                    check_single_truck: bool = False
                ):
                    canon_target = self.canonical_str(item_name)
                    tracking_target = f"{canon_target}_serviceneeded" if is_sn else (f"{canon_target}_outofservice" if is_oos else canon_target)
                    try:
                        t_dt = datetime.strptime(item_time.strip().upper(), "%I:%M %p")
                        time_key = t_dt.strftime("%I:%M %p").lstrip("0")
                    except Exception:
                        time_key = ""

                    canon_h_tech = self.canonical_str(item_tech or '')
                    key = f"{tracking_target}|{time_key}" if time_key else tracking_target
                    processed_key = (canon_h_tech, key)
                    template_processed_counts[processed_key] = template_processed_counts.get(processed_key, 0) + 1
                    target_idx = template_processed_counts[processed_key]

                    if not force and self.is_wo_created(created_history, db_date_key, canon_h_tech, tracking_target, target_idx, time_key=time_key):
                        print(f"⏭️ Item '{item_name}' (occurrence {target_idx} at {time_key}) was previously created. Skipping.")
                        return

                    if dry_run:
                        print(f"  [Dry-Run] Would create '{item_name}' on tech '{item_tech}' at {item_time}.")
                        return

                    # Check technician single truck constraint
                    if check_single_truck and canon_h_tech:
                        trucks_in_template = sum(1 for t in truck_assign_headers if self.is_tech_match(t.get("tech_name", ""), item_tech) or self.canonical_str(t.get("tech_name", "")) == canon_h_tech)
                        if trucks_in_template <= 1 and canon_h_tech in existing_wos:
                            has_truck = any(any(self.canonical_str(t.get("name", "")) == wo.get("canon_customer") for t in truck_assign_headers) for wo in existing_wos[canon_h_tech] if not wo.get("is_appointment"))
                            if has_truck:
                                print(f"⚠️ Tech '{item_tech}' already has a truck assigned on {date_str}. Skipping '{item_name}'.")
                                return

                    # Scan board for existing matches
                    board_count = 0
                    for canon_tech_key, wo_list in existing_wos.items():
                        tech_match = (canon_tech_key == canon_h_tech or self.is_tech_match(canon_h_tech, canon_tech_key)) if canon_h_tech else (not canon_tech_key)
                        if tech_match:
                            for wo in wo_list:
                                if not wo.get("is_appointment"):
                                    if self.check_item_match(wo, canon_target, time_key, is_sn, is_oos, match_any_status=(is_sn or is_oos)):
                                        board_count += 1

                    if board_count >= target_idx:
                        print(f"⏭️ Item '{item_name}' (occurrence {target_idx}) already exists on board. Recording in history.")
                        self.record_wo_created(created_history, db_date_key, canon_h_tech, tracking_target, target_idx, status="existing_on_board", details={"name": item_name, "tech": item_tech, "time": time_key})
                        return

                    created = await self.fill_and_save_work_order(
                        customer_name=item_name,
                        task=item_task,
                        lead_source=item_lead,
                        priority=item_priority,
                        target_date=date_str,
                        start_time=item_time,
                        duration=item_duration,
                        tech_name=item_tech,
                        mark_completed=is_completed,
                        immediate_action=item_action,
                        summary_note=item_summary
                    )
                    if created:
                        self.record_wo_created(created_history, db_date_key, canon_h_tech, tracking_target, target_idx, status="created", details={"name": item_name, "tech": item_tech, "time": time_key})

                # Loop 1: Static Headers
                for h in static_headers:
                    is_sn = bool(h.get('is_service_needed_duplicate') or (h.get('service_note') and ('SERVICE NEEDED' in str(h.get('priority', '')) or '7.12' in str(h.get('priority', '')))))
                    is_oos = bool(h.get('is_out_of_service') or (h.get('reason') and 'DO NOT DISPATCH' in str(h.get('priority', ''))))
                    await _sync_item(
                        item_name=h.get("name", ""),
                        item_tech=h.get("tech_name"),
                        item_time=h.get("start_time", "6:00 AM"),
                        item_priority=h.get("priority", "[5] SHOP (Tracking)"),
                        item_duration=h.get("duration", "1:00"),
                        item_task=h.get("task", task),
                        item_summary=h.get("service_note") or h.get("reason"),
                        is_sn=is_sn, is_oos=is_oos
                    )

                # Loop 2: Truck Assign Headers
                if not (is_weekend and skip_vehicle_on_weekends):
                    for h in truck_assign_headers:
                        await _sync_item(
                            item_name=h.get("name", ""),
                            item_tech=h.get("tech_name"),
                            item_time=h.get("start_time", "6:00 AM"),
                            item_priority=h.get("priority", "[5] SER TR (Asset Assignment)"),
                            item_duration=h.get("duration", "1:00"),
                            item_task=h.get("task", task),
                            item_summary=h.get("service_note"),
                            check_single_truck=True
                        )

                # Loop 3: Tech Jobs
                for j in tech_jobs:
                    if j.get("active", True):
                        await _sync_item(
                            item_name=j.get("name", ""),
                            item_tech=j.get("tech_name"),
                            item_time=j.get("start_time", "6:00 AM"),
                            item_priority=j.get("priority", "[7] ROUTINE (S) (STATUS)"),
                            item_duration=j.get("duration", "1:00"),
                            item_task=j.get("task", task)
                        )

                # Loop 4: Active Tech Routines (Completed at 4:45 AM)
                for t in active_techs:
                    t_meta = next((entry for entry in template_techs if entry.get("name") == t), {})
                    prio = t_meta.get("priority", "[7] ROUTINE (S) (STATUS)")
                    dept = t_meta.get("department", "service")
                    task_val = "8 - PUMP" if dept == "pump" else "8 - SERVICE"
                    await _sync_item(
                        item_name=t,
                        item_tech=t,
                        item_time="04:45 AM",
                        item_priority=prio,
                        item_duration="1:15",
                        item_task=task_val,
                        is_completed=True
                    )

            # Stage 2: Reconcile Asset Assignments
            if not skip_asset and not dry_run:
                try:
                    if not self.asset_scraper:
                        try:
                            from asset_assignment_scraper import AssetAssignmentScraper
                        except ImportError:
                            from dispatch.asset_assignment_scraper import AssetAssignmentScraper  # type: ignore
                        self.asset_scraper = AssetAssignmentScraper(template_path=self.template_path)

                    self.asset_scraper.playwright = self.playwright
                    self.asset_scraper.browser = self.browser
                    self.asset_scraper.context = getattr(self, 'context', None) or (self.page.context if self.page else None)
                    self.asset_scraper.page = self.page
                    self.asset_scraper.template_data = self.template_data

                    fmt_date = current_date.strftime("%m/%d/%Y")
                    recon = await self.asset_scraper.reconcile_single_date(date_str=fmt_date, click_wos=True)
                    print(f"✅ [Step 2: Asset Assignment] Completed for {fmt_date}: Checked {recon.get('checked', 0)}, Reassigned {recon.get('reassignments', 0)}.")
                except Exception as asset_err:
                    print(f"⚠️ [Step 2: Asset Assignment] Note on {date_str}: {asset_err}")

            self.record_wo_created(created_history, db_date_key, "", "date_processing_complete", 1, status="completed", details={"name": "Date Processing Complete", "date": db_date_key})
            print(f"🔒 Marked date {db_date_key} as completed in history.")

            self.progress_info.update({
                "current_day": d + 1, "completed_days": d + 1,
                "remaining_days": max(0, days - (d + 1)),
                "percent_complete": round(((d + 1) / days) * 100, 1),
                "status_message": f"Completed Day {d + 1} of {days} ({date_str})"
            })
            if on_progress:
                on_progress(self.progress_info)

        if not dry_run:
            await self.cleanup()
        print("🎉 Dispatch Board Automation completed successfully!")
