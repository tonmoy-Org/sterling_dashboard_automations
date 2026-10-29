"""
Dispatch Board Display Automation Scraper
Pre-creates static visual aid work orders and completed technician display work orders
on the FieldEdge Dispatch Board up to 30 days in advance.
"""

import os
import sys
import json
import re
import asyncio
import random
from datetime import datetime, timedelta
import time
import traceback
import urllib.request
import urllib.error
import unicodedata
from typing import List, Dict, Optional, Callable

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Ensure local dispatch directory is on sys.path
_dispatch_dir = os.path.dirname(os.path.abspath(__file__))
if _dispatch_dir not in sys.path:
    sys.path.insert(0, _dispatch_dir)

try:
    from base_scraper import BaseScraper
except ImportError:
    try:
        from automation.scrapers.base_scraper import BaseScraper  # type: ignore
    except ImportError:
        from dispatch.base_scraper import BaseScraper  # type: ignore







class DispatchBoardDisplayAutomationScraper(BaseScraper):
    """
    Scraper and automation engine for FieldEdge 'Dispatch Board Display Automation'.
    """

    def __init__(self, template_path: Optional[str] = None):
        super().__init__()
        self.scraper_name = "dispatch-board-display-automation"
        if template_path:
            self.template_path = template_path
        else:
            _curr = os.path.dirname(os.path.abspath(__file__))
            _candidate1 = os.path.join(_curr, "config", "dispatch_board_template.json")
            _candidate2 = os.path.join(os.path.dirname(_curr), "config", "dispatch_board_template.json")
            self.template_path = _candidate1 if os.path.exists(_candidate1) else _candidate2
        self.template_data = self._load_template()

    def _load_history(self) -> Dict:
        """Load persistent work order creation history from Central Dashboard API."""
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
                            print(f"✅ Synced {len(remote_history)} central tracking entries from Dashboard API.")
            except Exception as e:
                print(f"ℹ️ API history sync skipped ({e}). Proceeding with in-memory tracking.")

        return history

    @staticmethod
    def _is_created_record(v) -> bool:
        """Records that were created by automation or confirmed existing on the board are trusted for skipping.
        This respects manual edits in FieldEdge and prevents redundant recreations."""
        if isinstance(v, dict):
            status = v.get("status", "")
            return status in ("created", "existing_on_board", "completed")
        return False

    def is_wo_created(self, history: Dict, date_key: str, canon_tech: str, canon_target: str, target_count_idx: int) -> bool:
        """Check if work order has already been created/logged in persistent history (API memory or local JSON)."""
        key1 = f"{date_key}|{canon_tech}|{canon_target}|{target_count_idx}"
        if key1 in history and self._is_created_record(history[key1]):
            return True
        if not canon_tech:
            key2 = f"{date_key}||{canon_target}|{target_count_idx}"
            if key2 in history and self._is_created_record(history[key2]):
                return True

        # Check if history dict has any recorded entry matching date, canon_target, and occurrence
        for k, v in history.items():
            if not self._is_created_record(v):
                continue
            if (v.get("date") == date_key or k.startswith(f"{date_key}|")) and v.get("canon_target") == canon_target and v.get("occurrence") == target_count_idx:
                rec_tech = v.get("canon_tech") or ""
                if rec_tech == canon_tech or (rec_tech and canon_tech and (rec_tech in canon_tech or canon_tech in rec_tech)):
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
        """
        Check if all items for db_date_key have already been recorded in history.
        If force=True, always returns False so all items are verified/created on FieldEdge.
        """
        if force:
            return False

        template_processed_counts = {}

        # 1. Static Headers
        for header in static_headers:
            name = header.get("name")
            start_time = header.get("start_time")
            h_tech = header.get("tech_name")
            canon_target = self.canonical_str(name)
            is_service_wo = bool(
                header.get('is_service_needed_duplicate') or
                (header.get('service_note') and ('SERVICE NEEDED' in str(header.get('priority', '')) or '7.12' in str(header.get('priority', ''))))
            )
            tracking_target = f"{canon_target}_serviceneeded" if is_service_wo else canon_target
            try:
                t_dt = datetime.strptime(start_time.strip().upper(), "%I:%M %p")
                time_key = t_dt.strftime("%I:%M %p")
                if time_key.startswith("0"): time_key = time_key[1:]
            except Exception:
                time_key = ""
            key = f"{tracking_target}|{time_key}" if time_key else tracking_target
            canon_h_tech = self.canonical_str(h_tech or '')
            processed_key = (canon_h_tech, key)
            template_processed_counts[processed_key] = template_processed_counts.get(processed_key, 0) + 1
            target_count_idx = template_processed_counts[processed_key]

            if not self.is_wo_created(history, db_date_key, canon_h_tech, tracking_target, target_count_idx):
                return False

        # 2. Truck Assign Headers (if not weekend or not skipped)
        if not (is_weekend and skip_vehicle_on_weekends):
            for header in truck_assign_headers:
                name = header.get("name")
                start_time = header.get("start_time")
                h_tech = header.get("tech_name")
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

                if not self.is_wo_created(history, db_date_key, canon_h_tech, canon_target, target_count_idx):
                    return False

        # 3. Tech Jobs
        for job in tech_jobs:
            if not job.get("active", True):
                continue
            name = job.get("name")
            start_time = job.get("start_time")
            t_tech = job.get("tech_name")
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

            if not self.is_wo_created(history, db_date_key, canon_t_tech, canon_target, target_count_idx):
                return False

        # 4. Active Tech Completed Work Orders
        for tech in active_techs:
            t_assignment_name = tech
            canon_target = self.canonical_str(tech)
            canon_t_assignment = self.canonical_str(t_assignment_name or '')
            processed_key = (canon_t_assignment, canon_target)
            template_processed_counts[processed_key] = template_processed_counts.get(processed_key, 0) + 1
            target_count_idx = template_processed_counts[processed_key]

            if not self.is_wo_created(history, db_date_key, canon_t_assignment, canon_target, target_count_idx):
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
        """Record work order creation in Dashboard API."""
        key = f"{date_key}|{canon_tech}|{canon_target}|{target_count_idx}"
        det = details or {}
        rec_data = {
            "date": date_key,
            "canon_tech": canon_tech,
            "canon_target": canon_target,
            "occurrence": target_count_idx,
            "created_at": datetime.now().isoformat(),
            "status": status,
            **det
        }
        history[key] = rec_data

        # Sync to Central Dashboard API if reachable
        api_url = os.getenv("BACKEND_API_URL", "").rstrip("/")
        if api_url:
            try:
                import urllib.request
                endpoint = f"{api_url}/automation/dispatch-history/" if api_url.endswith('/api') else f"{api_url}/api/automation/dispatch-history/"
                req_data = json.dumps(rec_data).encode('utf-8')
                headers = {"Content-Type": "application/json", "User-Agent": "SterlingAutomations/1.0"}
                headers.update(self._get_api_auth_headers())
                req = urllib.request.Request(
                    endpoint,
                    data=req_data,
                    headers=headers,
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=4) as response:
                    if response.status in (200, 201):
                        pass
            except Exception:
                pass

    @staticmethod
    def _extract_jwt_from_response(data) -> Optional[str]:
        """Find a JWT access token in common login response shapes
        (e.g. {"access": ...}, {"token": ...}, {"data": {"accessToken": ...}}, {"tokens": {"access": ...}})."""
        token_keys = ("access", "access_token", "accessToken", "token", "jwt", "id_token", "idToken", "auth_token")
        if isinstance(data, dict):
            for k in token_keys:
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            for v in data.values():
                if isinstance(v, (dict, list)):
                    found = DispatchBoardDisplayAutomationScraper._extract_jwt_from_response(v)
                    if found:
                        return found
        elif isinstance(data, list):
            for v in data:
                found = DispatchBoardDisplayAutomationScraper._extract_jwt_from_response(v)
                if found:
                    return found
        return None

    def _get_api_auth_headers(self) -> Dict[str, str]:
        """
        Build Authorization header for protected Dashboard API endpoints.
        Priority:
          1. BACKEND_API_TOKEN (static JWT / token from .env)
          2. Login with BACKEND_API_EMAIL + BACKEND_API_PASSWORD at BACKEND_LOGIN_PATH (default: /auth/login)
        Returns {} if no credentials are configured or login fails.
        """
        static_token = os.getenv("BACKEND_API_TOKEN", "").strip()
        if static_token:
            return {"Authorization": static_token if " " in static_token else f"Bearer {static_token}"}

        email = os.getenv("BACKEND_API_EMAIL", "").strip()
        password = os.getenv("BACKEND_API_PASSWORD", "")
        api_url = os.getenv("BACKEND_API_URL", "").rstrip("/")
        if not (email and password and api_url):
            print("⚠️ BACKEND_API_EMAIL / BACKEND_API_PASSWORD not set in .env - calling Dashboard API without JWT.")
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
                # Fallback: some backends return the JWT only as an HttpOnly cookie
                if not token:
                    for cookie in response.headers.get_all("Set-Cookie") or []:
                        name, _, rest = cookie.partition("=")
                        if name.strip().lower() in ("access", "access_token", "accesstoken", "token", "jwt"):
                            token = rest.split(";", 1)[0].strip()
                            break
                if token:
                    print(f"🔐 Logged in to Dashboard API as {email} (JWT acquired).")
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
        """Load visual work order configuration template from API or local JSON file."""
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

                        # Overwrite with live remote settings (truck assignments, techs, headers)
                        for key, value in remote_data.items():
                            local_data[key] = value

                        # Cache live data back to local template file for offline fallback
                        try:
                            with open(self.template_path, "w", encoding="utf-8") as f:
                                json.dump(local_data, f, indent=2, ensure_ascii=False)
                            print(f"💾 Cached live template data to {self.template_path}")
                        except Exception as cache_err:
                            print(f"⚠️ Could not cache template data to local file: {cache_err}")

                        return local_data
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
    def is_tech_match(tech1: str, tech2: str) -> bool:
        """Compare two technician names matching across single-letter prefixes, unicode bold, and full names."""
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
    def check_item_match(
        wo: Dict,
        canon_target: str,
        time_key: Optional[str] = None,
        require_customer_match: bool = False,
        is_service_wo: bool = False
    ) -> bool:
        """Check if a target canonical string matches a schedule item object."""
        if not canon_target:
            return False

        wo_is_sn = bool(wo.get("is_service_needed"))
        if is_service_wo and not wo_is_sn:
            return False
        if not is_service_wo and wo_is_sn:
            return False

        w_time = wo.get("time_key", "")
        if time_key and w_time and time_key != w_time:
            return False

        c_cust = wo.get("canon_customer", "")
        c_strings = wo.get("canon_strings", [])

        # For customer match or short targets (<= 4 chars, e.g. '71', '01', '03', '04', '90'):
        # NEVER do raw substring search in combined text, because short numbers match zip codes (98371),
        # phone numbers, lat/long, timestamps, balances, and IDs.
        if require_customer_match or len(canon_target) <= 4:
            if c_cust == canon_target:
                return True
            if canon_target in c_strings:
                return True
            if c_cust and (c_cust.startswith(canon_target) or c_cust.endswith(canon_target)):
                return True
            return False

        # Exact match in any extracted string
        if canon_target in c_strings:
            return True

        # Exact match or substring in customer name
        if c_cust and (canon_target == c_cust or canon_target in c_cust or c_cust in canon_target):
            return True

        # Match in combined string (only for longer target strings >= 5 chars)
        c_comb = wo.get("canon_combined", "")
        if c_comb and len(canon_target) >= 5 and (canon_target == c_comb or canon_target in c_comb):
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
                    
                    dt_str = (
                        item.get("ScheduleDateTime") or
                        item.get("ScheduledDateTime") or
                        item.get("ScheduledFrom") or
                        item.get("StartDateTime") or
                        item.get("StartTime")
                    )
                    time_key = ""
                    if dt_str:
                        dt_clean = re.sub(r'[+-]\d{2}:?\d{2}$', '', str(dt_str).replace("Z", "")).replace("T", " ").split(".")[0].strip()
                        for fmt in [
                            "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%d %H:%M",
                            "%I:%M %p",
                            "%H:%M:%S",
                            "%H:%M"
                        ]:
                            try:
                                local_dt = datetime.strptime(dt_clean, fmt)
                                time_key = local_dt.strftime("%I:%M %p")
                                if time_key.startswith("0"): time_key = time_key[1:]
                                break
                            except Exception:
                                pass

                    item_prio = str(item.get("Priority") or item.get("PriorityName") or item.get("PriorityDescription") or "")
                    is_sn = (
                        "service needed" in item_prio.lower() or
                        "7.12" in item_prio or
                        any("service needed" in s.lower() or "7.12" in s for s in canon_strings)
                    )
                            
                    existing_wos[canon_tech].append({
                        "canon_customer": canon_customer,
                        "canon_strings": canon_strings,
                        "canon_combined": canon_combined,
                        "time_key": time_key,
                        "is_service_needed": is_sn,
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
                    dt_to_str = appt.get("ScheduledTo")
                    time_key = ""
                    start_time_obj = None
                    end_time_obj = None
                    if dt_str:
                        try:
                            dt_str_clean = dt_str.replace("Z", "").replace("T", " ").split(".")[0].strip()
                            local_dt = datetime.strptime(dt_str_clean, "%Y-%m-%d %H:%M:%S")
                            time_key = local_dt.strftime("%I:%M %p")
                            if time_key.startswith("0"): time_key = time_key[1:]
                            start_time_obj = local_dt.time()
                        except Exception:
                            pass
                            
                    if dt_to_str:
                        try:
                            dt_to_clean = dt_to_str.replace("Z", "").replace("T", " ").split(".")[0].strip()
                            local_dt_to = datetime.strptime(dt_to_clean, "%Y-%m-%d %H:%M:%S")
                            end_time_obj = local_dt_to.time()
                        except Exception:
                            pass

                    existing_wos[canon_tech].append({
                        "canon_customer": canon_customer,
                        "canon_strings": canon_strings,
                        "canon_combined": canon_combined,
                        "time_key": time_key,
                        "start_time_obj": start_time_obj,
                        "end_time_obj": end_time_obj,
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

    async def close_create_wo_modal(self):
        """Close Create Work Order modal if open without saving to prevent unwanted changes."""
        try:
            cancel_candidates = [
                "button:has-text('Cancel')",
                "button.cancel_button",
                ".modal-header .close",
                "button.close",
                "[aria-label='Close']",
                ".modal-close"
            ]
            for cand in cancel_candidates:
                btn = self.page.locator(cand).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(force=True)
                    await self.page.wait_for_timeout(500)
                    return True
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(800)
            return True
        except Exception:
            return False

    async def select_dropdown_option(self, dropdown_selector: str, option_text: str, press_enter: bool = True) -> bool:
        """
        Robust dropdown option selection for FieldEdge Vue-select dropdowns.
        Works accurately on both Windows local and Linux headless servers with Unicode NFKD support.
        """
        if not option_text:
            return True

        import unicodedata
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
            clean_search = unicodedata.normalize('NFKD', option_text).strip()
            clean_core = re.sub(r'^[a-zA-Z0-9]{1,3}\s+', '', clean_search).strip()

            # For typing: use exact option_text directly as configured in JSON template
            # (e.g. 'l 𝐂𝐫𝐢𝐬', 'f 𝐒𝐤𝐲𝐥𝐞𝐫', '8 - Internal', etc.) so Vue-select matches precisely
            type_text = option_text.strip()
            await search_input.fill(type_text)
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

            # If press_enter and still no options, retry up to 2 more times (handles slow FieldEdge API)
            if press_enter:
                for _retry in range(2):
                    if await options.count() > 0:
                        break
                    print(f"  -> Customer search: no results yet, retrying ({_retry + 1}/2)...")
                    await search_input.press("Enter")
                    await self.page.wait_for_timeout(3000)
                    try:
                        await options.first.wait_for(state="visible", timeout=4000)
                    except Exception:
                        pass

                # If still 0 options and searching with emoji/symbols (e.g. '🚛 | 124'), fallback to clean truck digits
                if (await options.count()) == 0:
                    m_dig = re.search(r'\b\d{2,4}\b', clean_search)
                    if m_dig and m_dig.group(0) != clean_search:
                        fallback_term = m_dig.group(0)
                        print(f"  -> No options found for '{clean_search}'. Retrying customer search with clean truck number '{fallback_term}'...")
                        try:
                            await search_input.focus()
                            await search_input.fill("")
                            await search_input.fill(fallback_term)
                            await self.page.wait_for_timeout(400)
                            await search_input.press("Enter")
                            await self.page.wait_for_timeout(3000)
                            try:
                                await options.first.wait_for(state="visible", timeout=5000)
                            except Exception:
                                pass
                        except Exception:
                            pass

                # If STILL 0 options and the search term contains emoji/non-ASCII (e.g. '📝REPORTS NEEDED'),
                # strip emoji and retry with plain-ASCII text only
                if (await options.count()) == 0:
                    stripped_ascii = re.sub(r'[^\x00-\x7F]+', '', clean_search).strip()
                    if stripped_ascii and stripped_ascii != clean_search:
                        print(f"  -> No options found for '{clean_search}'. Retrying customer search with ASCII-only term: '{stripped_ascii}'...")
                        try:
                            await search_input.focus()
                            await search_input.fill("")
                            await search_input.fill(stripped_ascii)
                            await self.page.wait_for_timeout(400)
                            await search_input.press("Enter")
                            await self.page.wait_for_timeout(3000)
                            try:
                                await options.first.wait_for(state="visible", timeout=5000)
                            except Exception:
                                pass
                        except Exception:
                            pass
            else:
                # If not press_enter and 0 options found, fallback:
                if (await options.count()) == 0:
                    # If clean_core is different from option_text, try searching clean_core
                    if clean_core and clean_core != option_text:
                        try:
                            await search_input.focus()
                            await search_input.fill("")
                            await search_input.fill(clean_core)
                            await self.page.wait_for_timeout(500)
                            try:
                                await options.first.wait_for(state="visible", timeout=2000)
                            except Exception:
                                pass
                        except Exception:
                            pass
                    # If still 0 options, clear input to reveal full list of dropdown options
                    if (await options.count()) == 0:
                        try:
                            await search_input.focus()
                            await search_input.fill("")
                            await self.page.wait_for_timeout(500)
                            try:
                                await options.first.wait_for(state="visible", timeout=3000)
                            except Exception:
                                pass
                        except Exception:
                            pass

            count = await options.count()
            clicked = False
            raw_target = option_text.strip()
            target_norm = clean_search.lower()
            target_canon = self.canonical_str(option_text)
            m_core = re.sub(r'^[a-z0-9]\s*', '', target_norm)
            core_name = m_core if len(m_core) >= 3 else target_norm

            # Helper to check if an option element matches target strictly
            async def _check_and_click_option(opt_el) -> bool:
                if not await opt_el.is_visible():
                    return False
                raw_text = await opt_el.text_content()
                if not raw_text:
                    return False
                s_raw = raw_text.strip()
                norm_raw = unicodedata.normalize('NFKD', s_raw).strip().lower()
                c_raw = self.canonical_str(s_raw)

                # Pass 0: Exact raw string match
                if s_raw == raw_target:
                    await opt_el.scroll_into_view_if_needed()
                    await opt_el.click(force=True)
                    return True

                # Pass 1: Exact normalized match
                if norm_raw == target_norm or norm_raw == clean_core.lower():
                    await opt_el.scroll_into_view_if_needed()
                    await opt_el.click(force=True)
                    return True

                # Pass 2: Canonical match (ignore special chars/whitespace/unicode fonts)
                if target_canon and (target_canon == c_raw or self.canonical_str(clean_core) == c_raw):
                    await opt_el.scroll_into_view_if_needed()
                    await opt_el.click(force=True)
                    return True

                # Pass 3: Core technician name match (e.g. 'skyler' in 'f 𝐒𝐤𝐲𝐥𝐞𝐫')
                if core_name and len(core_name) >= 3:
                    if core_name in norm_raw or core_name in c_raw:
                        await opt_el.scroll_into_view_if_needed()
                        await opt_el.click(force=True)
                        return True

                # Pass 4: Technician name matching using is_tech_match
                if self.is_tech_match(s_raw, option_text) or self.is_tech_match(s_raw, clean_search) or self.is_tech_match(s_raw, clean_core):
                    await opt_el.scroll_into_view_if_needed()
                    await opt_el.click(force=True)
                    return True

                # Pass 5: Substring match (ONLY allowed if BOTH target and option have length >= 4)
                # CRITICAL: NEVER do substring matching for short tokens (like 's7', 's6', 'c', 'e')!
                if len(target_norm) >= 4 and len(norm_raw) >= 4:
                    if target_norm in norm_raw or norm_raw in target_norm:
                        await opt_el.scroll_into_view_if_needed()
                        await opt_el.click(force=True)
                        return True

                # Pass 6: Truck number / digits match (e.g. '124' in '🚛 | 124')
                m_digits = re.search(r'\b\d{2,4}\b', target_canon)
                if m_digits and len(target_canon) >= 4:
                    d_target = m_digits.group(0)
                    if d_target in c_raw:
                        await opt_el.scroll_into_view_if_needed()
                        await opt_el.click(force=True)
                        return True

                return False

            # Check visible options
            for i in range(count):
                opt = options.nth(i)
                if await _check_and_click_option(opt):
                    clicked = True
                    break

            # If not found and dropdown has scrollable menu, scroll down to reveal more items
            if not clicked and count > 0:
                try:
                    menu_container = dropdown.locator("ul.vs__dropdown-menu, ul[role='listbox']").first
                    if await menu_container.count() > 0:
                        for scroll_step in range(3):
                            await self.page.evaluate("""(menu) => { if (menu) menu.scrollTop += 300; }""", await menu_container.element_handle())
                            await self.page.wait_for_timeout(300)
                            count_now = await options.count()
                            for i in range(count_now):
                                opt = options.nth(i)
                                if await _check_and_click_option(opt):
                                    clicked = True
                                    break
                            if clicked:
                                break
                except Exception:
                    pass

            # Handle completion or safe failure (NO BLIND FALLBACK to first_opt!)
            if not clicked:
                if press_enter:
                    # For customer search, enter selects current highlighted input
                    await search_input.press("Enter")
                    await self.page.wait_for_timeout(1500)
                    # Verify that a customer was actually selected after pressing Enter.
                    # Without this check, an empty/failed search returns True, causing a
                    # 30-second Save modal hang because FieldEdge blocks save with no customer.
                    selected_check = dropdown.locator(".vs__selected, .selected-tag").first
                    if await selected_check.count() > 0 and await selected_check.is_visible():
                        return True
                    # No selection made — do NOT proceed with empty customer field
                    print(f"  -> Press-Enter fallback: No customer was selected for '{option_text}' (search returned no valid FieldEdge results). Aborting to prevent stuck Save modal.")
                    return False
                else:
                    print(f"⚠️ Could not find exact or canonical match for option '{option_text}' in dropdown ({dropdown_selector}). Aborting selection to prevent misassignment.")
                    return False

            # Post-selection verification: verify what was actually selected into the dropdown
            # For customer search (press_enter=True) on VPS, remote API response can take several seconds to populate
            verify_budget = 8.0 if press_enter else 2.5
            verify_start = time.time()
            is_verified = False
            selected_val = ""

            while (time.time() - verify_start) < verify_budget:
                selected_el = dropdown.locator(".vs__selected, .selected-tag, .vs__selected-options").first
                if await selected_el.count() > 0:
                    selected_val = (await selected_el.text_content() or "").strip()
                    clean_sel = self.canonical_str(selected_val)
                    clean_opt = self.canonical_str(option_text)
                    if (
                        selected_val == option_text or
                        clean_sel == clean_opt or
                        (len(clean_sel) >= 4 and len(clean_opt) >= 4 and (clean_sel in clean_opt or clean_opt in clean_sel)) or
                        self.is_tech_match(selected_val, option_text)
                    ):
                        is_verified = True
                        break
                await self.page.wait_for_timeout(400)

            if not is_verified:
                print(f"❌ Selection verification failed: expected '{option_text}', but dropdown selected '{selected_val}'. Aborting selection.")
                clear_btn = dropdown.locator(".vs__clear, button.close").first
                if await clear_btn.count() > 0 and await clear_btn.is_visible():
                    await clear_btn.click(force=True)
                return False

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
        task_type: Optional[str] = None,
        summary_note: Optional[str] = None
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

            # Smart wait: wait for all FieldEdge customer loading spinners to vanish and form to settle
            print(f"  -> Waiting for customer data and background spinners to finish...")
            import time
            _load_start = time.time()
            _max_load_wait = 45  # seconds total budget
            _form_ready = False

            # Initial pause to let Vue launch its customer background requests
            await self.page.wait_for_timeout(1000)

            while (time.time() - _load_start) < _max_load_wait:
                try:
                    # 1. Check for any active spinners or loaders
                    spinner_loc = self.page.locator(".spinner:visible, .vs__spinner:visible, .loading:visible, .p-progress-spinner:visible, .loading-mask:visible, .vs--loading:visible")
                    active_spinners = await spinner_loc.count()

                    # 2. Check if Task dropdown is visible and not disabled
                    task_ready = self.page.locator(
                        self.rules.get('dispatch_board_task_xpath',
                            "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Task') and not(contains(normalize-space(),'Duration'))]/following::div[contains(@class,'v-select')][1]")
                    ).first
                    task_vis = await task_ready.is_visible()
                    task_class = (await task_ready.get_attribute("class") or "") if task_vis else ""
                    task_disabled = "vs--disabled" in task_class or "disabled" in task_class

                    # 3. Check for any customer popups or confirmation dialogs
                    dialog_confirm = self.page.locator(
                        "//div[contains(@class,'v-dialog') or contains(@class,'modal') or contains(@class,'p-dialog')]//button[contains(@class,'confirm') or contains(.,'Confirm') or contains(.,'OK') or contains(.,'Yes') or contains(.,'Select')]"
                    ).first
                    if await dialog_confirm.count() > 0 and await dialog_confirm.is_visible():
                        print("  -> Dismissing customer dialog/alert with Confirm button...")
                        await dialog_confirm.click(force=True)
                        await self.page.wait_for_timeout(1000)
                        continue

                    if active_spinners == 0 and task_vis and not task_disabled:
                        _form_ready = True
                        elapsed = round(time.time() - _load_start, 1)
                        print(f"  -> Form and customer data fully ready in {elapsed}s (0 spinners active). Proceeding.")
                        break
                    elif active_spinners > 0:
                        print(f"  -> Waiting for {active_spinners} background spinners to finish...")
                except Exception:
                    pass

                await self.page.wait_for_timeout(1000)

            if not _form_ready:
                print(f"  ❌ Form not ready after {_max_load_wait}s (Customer '{customer_name}' data did not load or spinners hung). Aborting safely.")
                await self.close_create_wo_modal()
                return False

            # Buffer for Vue DOM reactivity and field bindings to completely settle
            await self.page.wait_for_timeout(1200)

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
                # Select using exact tech_name from JSON template (e.g. 'l 𝐂𝐫𝐢𝐬', 'f 𝐒𝐤𝐲𝐥𝐞𝐫')
                tech_selected = await self.select_dropdown_option(tech_xpath, tech_name, press_enter=False)
                if not tech_selected:
                    # Fallback: try clean tech name if available
                    clean_tech_name = None
                    canon_tn = self.canonical_str(tech_name)
                    for entry in self.template_data.get("active_techs", []):
                        c_tname = self.canonical_str(entry.get("tech_name", ""))
                        c_name = self.canonical_str(entry.get("name", ""))
                        if canon_tn and (canon_tn == c_tname or canon_tn == c_name):
                            clean_tech_name = entry.get("name", "")
                            break
                    if not clean_tech_name:
                        norm_tn = unicodedata.normalize('NFKD', tech_name).strip()
                        m_stripped = re.sub(r'^[a-zA-Z0-9]{1,3}\s+', '', norm_tn).strip()
                        clean_tech_name = m_stripped if len(m_stripped) >= 3 else tech_name
                    if clean_tech_name and clean_tech_name != tech_name:
                        print(f"  -> Retrying Primary Tech selection with fallback name '{clean_tech_name}'...")
                        tech_selected = await self.select_dropdown_option(tech_xpath, clean_tech_name, press_enter=False)

                if not tech_selected:
                    print(f"❌ Failed to match and select expected technician '{tech_name}'. Aborting work order to prevent wrong/duplicate assignment.")
                    await self.close_create_wo_modal()
                    return False

                # Strict Verification: Confirm Primary Tech field in modal matches expected tech_name
                tech_container = self.page.locator(tech_xpath).first
                selected_tech_text = (await tech_container.locator(".vs__selected").text_content() or "").strip()
                clean_actual_tech = self.canonical_str(selected_tech_text)
                clean_target_tech = self.canonical_str(tech_name)
                tech_verified = (
                    selected_tech_text == tech_name or
                    clean_actual_tech == clean_target_tech or
                    (len(clean_actual_tech) >= 4 and len(clean_target_tech) >= 4 and (clean_actual_tech in clean_target_tech or clean_target_tech in clean_actual_tech)) or
                    self.is_tech_match(selected_tech_text, tech_name)
                )
                if not tech_verified:
                    print(f"🛑 STRICT SAFETY STOP: Primary Tech modal value is '{selected_tech_text}', but expected '{tech_name}'! Aborting work order to prevent misassignment.")
                    await self.close_create_wo_modal()
                    return False

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

                    # Verify Start Time value in DOM and re-apply if FieldEdge auto-adjusted it
                    try:
                        actual_start_time = await time_input.input_value()
                        t_exp = datetime.strptime(start_time.strip().upper(), "%I:%M %p")
                        t_act = datetime.strptime(actual_start_time.strip().upper(), "%I:%M %p") if actual_start_time else None
                        if t_act and t_exp != t_act:
                            print(f"  ⚠️ FieldEdge auto-adjusted Start Time to '{actual_start_time}'. Re-forcing '{start_time}'...")
                            await time_input.click(force=True)
                            await self.page.keyboard.press("Control+A")
                            await self.page.keyboard.press("Backspace")
                            await time_input.type(start_time, delay=80)
                            await self.page.keyboard.press("Enter")
                            await time_input.evaluate(f"el => {{ el.value = '{start_time}'; el.dispatchEvent(new Event('input', {{ bubbles: true }})); el.dispatchEvent(new Event('change', {{ bubbles: true }})); }}")
                            await self.page.keyboard.press("Tab")
                            await self.page.wait_for_timeout(300)
                    except Exception:
                        pass

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

            # Blur active input field first to ensure Vue v-model reactivity commits data
            try:
                await self.page.evaluate("() => { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); }")
            except Exception:
                pass

            # Pre-save final verification: Primary Tech must STILL match tech_name
            if tech_name:
                final_tech = (await self.page.locator(tech_xpath).locator(".vs__selected").text_content() or "").strip()
                clean_f = self.canonical_str(final_tech)
                clean_t = self.canonical_str(tech_name)
                final_verified = (
                    final_tech == tech_name or
                    clean_f == clean_t or
                    (len(clean_f) >= 4 and len(clean_t) >= 4 and (clean_f in clean_t or clean_t in clean_f)) or
                    self.is_tech_match(final_tech, tech_name)
                )
                if not final_verified:
                    print(f"🛑 FINAL PRE-SAVE CHECK FAILED: Primary Tech '{final_tech}' != expected '{tech_name}'. Aborting save.")
                    await self.close_create_wo_modal()
                    return False

            save_btn = None
            for save_sel in [
                "//button[contains(@class,'confirm') and .//span[normalize-space()='Save']]",
                "//*[@id='scroll-container']/div/div[5]/div/button[3]",
                "div.on-bottom-modal button.confirm_hsagT",
                "button.confirm_hsagT",
                "button:has-text('Save')",
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

            print(f"  -> Waiting for form to validate and Save button to be ready...")
            await self.page.wait_for_timeout(1500)

            # Wait up to 5s for Vue to enable the Save button naturally
            for _ in range(20):
                try:
                    is_disabled = await save_btn.get_attribute("disabled")
                    btn_class = await save_btn.get_attribute("class") or ""
                    aria_disabled = await save_btn.get_attribute("aria-disabled")
                    if is_disabled is None and "disabled" not in btn_class and aria_disabled != "true":
                        break
                except Exception:
                    pass
                await self.page.wait_for_timeout(250)

            async def _trigger_save_click():
                if not save_btn:
                    return
                try:
                    await save_btn.scroll_into_view_if_needed()
                    await save_btn.click(timeout=3000, force=True)
                except Exception:
                    pass
                try:
                    await self.page.evaluate("""
                        (btn) => {
                            if (!btn) return;
                            btn.scrollIntoView({ block: 'center', inline: 'center' });
                            const span = btn.querySelector('span') || btn.querySelector('.content_YwYtM') || btn;
                            const evts = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
                            for (const ev of evts) {
                                btn.dispatchEvent(new MouseEvent(ev, { bubbles: true, cancelable: true, view: window }));
                                if (span && span !== btn) {
                                    span.dispatchEvent(new MouseEvent(ev, { bubbles: true, cancelable: true, view: window }));
                                }
                            }
                        }
                    """, await save_btn.element_handle())
                except Exception:
                    pass

            await _trigger_save_click()
            print(f"✅ Clicked Save for: '{customer_name}'")

            # -- WAIT FOR SAVE TO COMPLETE --
            customer_xpath_val = self.rules.get(
                'dispatch_board_customer_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Customer Name')]/following::div[contains(@class,'v-select')][1]"
            )
            customer_field = self.page.locator(customer_xpath_val).first

            print("  -> Waiting for Save to process (modal to close)...")
            modal_closed = False
            _save_start = time.time()
            _last_reclick = _save_start

            await self.page.wait_for_timeout(1500)

            while time.time() - _save_start < 30:
                # 1. Check for redirect to DispatchSummary (for technician work orders)
                current_url = self.page.url
                if "DispatchSummary" in current_url:
                    modal_closed = True
                    break

                # 2. Check for conflict/confirmation dialogs (e.g. Schedule Conflict / Confirm / Yes)
                for conf_sel in [
                    "//div[contains(@class,'v-dialog') or contains(@class,'modal')]//button[contains(@class,'confirm') or contains(.,'Confirm') or contains(.,'Yes') or contains(.,'Save Anyway') or contains(.,'Proceed') or contains(.,'Continue')]",
                    "button:has-text('Confirm'):visible",
                    "button:has-text('Yes'):visible",
                    "button:has-text('Save Anyway'):visible",
                    "button:has-text('Proceed'):visible",
                ]:
                    try:
                        conf_btn = self.page.locator(conf_sel).last
                        if await conf_btn.count() > 0 and await conf_btn.is_visible():
                            btn_text = (await conf_btn.text_content() or '').strip()
                            if btn_text and 'Save' not in btn_text:
                                print(f"  -> Found confirmation/conflict popup button '{btn_text}'. Clicking to confirm...")
                                await conf_btn.click(force=True)
                                await self.page.wait_for_timeout(1000)
                                break
                    except Exception:
                        pass

                # 3. Check if any error toast or banner appeared
                try:
                    err_text = await self.page.evaluate("""
                        () => {
                            const selectors = [
                                '.toast-error', '.toast-message', '.error-message',
                                '.validation-message', '.v-messages__message', '.alert-danger',
                                '[role="alert"]', '.notification-error', '.modal-error'
                            ];
                            for (const sel of selectors) {
                                const el = document.querySelector(sel);
                                if (el && el.offsetParent !== null) {
                                    const txt = (el.textContent || '').trim();
                                    if (txt) return txt;
                                }
                            }
                            return '';
                        }
                    """)
                    if err_text:
                        print(f"  ⚠️ Error/notice detected on page: '{err_text}'")
                except Exception:
                    pass

                # 4. Check if modal indicators are still visible
                save_btn_visible = False
                try:
                    if await save_btn.count() > 0 and await save_btn.is_visible():
                        save_btn_visible = True
                except Exception:
                    save_btn_visible = False

                customer_visible = False
                try:
                    if await customer_field.count() > 0 and await customer_field.is_visible():
                        customer_visible = True
                except Exception:
                    customer_visible = False

                modal_container_visible = False
                try:
                    modal_cont = self.page.locator("div.on-bottom-modal, div.create-workorder-form-grid").first
                    if await modal_cont.count() > 0 and await modal_cont.is_visible():
                        modal_container_visible = True
                except Exception:
                    modal_container_visible = False

                # Modal is closed ONLY when both save button and customer field are no longer visible
                if not save_btn_visible and not customer_visible and not modal_container_visible:
                    modal_closed = True
                    elapsed = round(time.time() - _save_start, 1)
                    print(f"  -> Save processed successfully (modal closed in {elapsed}s).")
                    break

                # 5. Every 5 seconds, if still open, re-click Save
                if (time.time() - _last_reclick) >= 5.0:
                    _last_reclick = time.time()
                    print("  -> Modal still open, re-clicking Save button...")
                    await _trigger_save_click()

                await self.page.wait_for_timeout(1000)

            if not modal_closed:
                print(f"  ❌ Modal did not close after 30s for '{customer_name}'. Work order save was not confirmed!")
                print("  -> Closing stuck modal to ensure subsequent work orders are not blocked...")
                await self.close_create_wo_modal()
                return False

            await self.page.wait_for_timeout(2000)

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
            return False

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

    async def run(self, days: int = 30, dry_run: bool = False, active_techs: Optional[List[str]] = None, start_date_str: Optional[str] = None, on_progress: Optional[Callable] = None, skip_asset: bool = False, force: bool = False):
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

        # Reload live template configuration from Dashboard API (Single Source of Truth)
        self.template_data = self._load_template()

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

        # Exempt Out of Service vehicles from appearing anywhere on the dispatch board
        oos_names = {
            self.canonical_str(s.get("asset_name", ""))
            for s in self.template_data.get("out_of_service_assets", [])
            if s.get("active", True)
        }
        if oos_names:
            static_headers = [h for h in static_headers if self.canonical_str(h.get("name", "")) not in oos_names]
            truck_assign_headers = [h for h in truck_assign_headers if self.canonical_str(h.get("name", "")) not in oos_names]
            print(f"🚫 Exempted {len(oos_names)} Out of Service vehicle(s) from dispatch board headers.")
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
                c_name = self.canonical_str(name)
                tech_priorities[c_name] = entry.get("priority")
                tech_metadata[c_name] = entry

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
                    # Only parse successful POST responses (skip OPTIONS preflight or cancelled/aborted redirects)
                    if response.status != 200 or response.request.method != "POST":
                        return
                    try:
                        text = await response.text()
                        if text:
                            board_data = json.loads(text)
                            print("📡 Intercepted Dispatch Board API data successfully.")
                    except Exception as e:
                        # Chromium discards response body from CDP memory during rapid page redirects/navigation
                        if "getResponseBody" not in str(e):
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

        created_history = self._load_history()
        print(f"📚 Loaded {len(created_history)} recorded entries from Central Dashboard API.")

        try:
            for d in range(days):
                current_date = start_dt + timedelta(days=d)
                date_str = f"{current_date.month}/{current_date.day}/{current_date.strftime('%y')}"
                db_date_key = current_date.strftime("%Y-%m-%d")
                is_weekend = current_date.weekday() >= 5

                # ── SMART SKIP CHECK: Are Stage 1 template items already completed in history? ──
                skip_stage1 = False
                if not dry_run and not force and self.is_date_fully_completed(
                    history=created_history,
                    db_date_key=db_date_key,
                    static_headers=static_headers,
                    truck_assign_headers=truck_assign_headers,
                    tech_jobs=tech_jobs,
                    active_techs=active_techs,
                    is_weekend=is_weekend,
                    skip_vehicle_on_weekends=skip_vehicle_on_weekends,
                    force=force
                ):
                    print(f"\n⚡ [Smart Skip: Stage 1] All template items for {date_str} ({db_date_key}) are already recorded in history.")
                    print(f"   Skipping Stage 1 template creation to preserve manual edits. Proceeding directly to Stage 2 (Asset Assignment)...\n")
                    skip_stage1 = True
                    if skip_asset:
                        _records_processed += 1
                        self.progress_info.update({
                            "current_day": d + 1,
                            "completed_days": d + 1,
                            "remaining_days": max(0, days - (d + 1)),
                            "percent_complete": round(((d + 1) / days) * 100, 1),
                            "status_message": f"Completed Day {d + 1} of {days} ({date_str}) [Smart Skip]"
                        })
                        if on_progress:
                            try:
                                on_progress(self.progress_info)
                            except Exception:
                                pass
                        continue

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
                if not skip_stage1:

                    if not dry_run:
                        if board_date_str != db_date_key or "DispatchBoard" not in (self.page.url or ""):
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
                        h_task = header.get("task", task)

                        # Check if the header already exists on the board for the target date
                        if not dry_run:
                            canon_target = self.canonical_str(name)
                            is_service_wo = bool(
                                header.get('is_service_needed_duplicate') or
                                (header.get('service_note') and ('SERVICE NEEDED' in str(header.get('priority', '')) or '7.12' in str(header.get('priority', ''))))
                            )
                            tracking_target = f"{canon_target}_serviceneeded" if is_service_wo else canon_target
                            try:
                                t_dt = datetime.strptime(start_time.strip().upper(), "%I:%M %p")
                                time_key = t_dt.strftime("%I:%M %p")
                                if time_key.startswith("0"): time_key = time_key[1:]
                            except Exception:
                                time_key = ""
                            key = f"{tracking_target}|{time_key}" if time_key else tracking_target
                        
                            canon_h_tech = self.canonical_str(h_tech or '')
                        
                            processed_key = (canon_h_tech, key)
                            template_processed_counts[processed_key] = template_processed_counts.get(processed_key, 0) + 1
                            target_count_idx = template_processed_counts[processed_key]
                        
                            # Check persistent creation history first
                            if not force and self.is_wo_created(created_history, db_date_key, canon_h_tech, tracking_target, target_count_idx):
                                print(f"⏭️ Header banner '{name}' (occurrence {target_count_idx} at {time_key}) was previously created and recorded in history. Skipping 2nd creation.")
                                continue

                            board_count = 0
                            for canon_tech_key, wo_list in existing_wos.items():
                                if canon_h_tech:
                                    tech_match = (
                                        canon_tech_key == canon_h_tech or
                                        canon_h_tech in canon_tech_key or
                                        canon_tech_key in canon_h_tech
                                    )
                                else:
                                    tech_match = not canon_tech_key

                                if tech_match:
                                    for wo in wo_list:
                                        if self.check_item_match(wo, canon_target, time_key, is_service_wo=is_service_wo):
                                            board_count += 1
                         
                            if board_count >= target_count_idx:
                                print(f"⏭️ Header banner '{name}' (occurrence {target_count_idx} at {time_key}) already exists on the board for tech '{h_tech}'. Recording in persistent history & skipping creation.")
                                self.record_wo_created(created_history, db_date_key, canon_h_tech, tracking_target, target_count_idx, status="existing_on_board", details={"name": name, "tech": h_tech, "time": time_key})
                                continue

                        if dry_run:
                            print(f"  [Dry-Run] Header: {name} | Date: {date_str} | Time: {start_time} | Duration: {duration} | Priority: {priority} | Tech: {h_tech} | Task: {h_task}")
                        else:
                            is_service_wo = bool(
                                header.get('is_service_needed_duplicate') or
                                (header.get('service_note') and ('SERVICE NEEDED' in str(header.get('priority', '')) or '7.12' in str(header.get('priority', ''))))
                            )
                            header_summary = header.get('service_note', '') if is_service_wo else None
                            res = await self.fill_and_save_work_order(
                                customer_name=name,
                                task=h_task,
                                lead_source=lead_source,
                                priority=priority,
                                target_date=date_str,
                                start_time=start_time,
                                duration=duration,
                                tech_name=h_tech,
                                immediate_action=immediate_action,
                                summary_note=None
                            )
                            if not res:
                                print(f"  🔁 Attempt 1 aborted/failed for '{name}'. Retrying with fresh attempt in 3s...")
                                await self.close_create_wo_modal()
                                await self.page.wait_for_timeout(3000)
                                res = await self.fill_and_save_work_order(
                                    customer_name=name,
                                    task=h_task,
                                    lead_source=lead_source,
                                    priority=priority,
                                    target_date=date_str,
                                    start_time=start_time,
                                    duration=duration,
                                    tech_name=h_tech,
                                    immediate_action=immediate_action,
                                    summary_note=None
                                )

                            if res:
                                self.record_wo_created(created_history, db_date_key, canon_h_tech, tracking_target, target_count_idx, status="created", details={"name": name, "tech": h_tech, "time": time_key})
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
                            h_task = header.get("task", task)

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

                                is_service_needed_asset = any(
                                    self.canonical_str(s.get("asset_name", "")) == canon_target
                                    for s in self.template_data.get("service_needed_assets", [])
                                    if s.get("active", True)
                                )
                            
                                # Check persistent creation history first
                                if not force and self.is_wo_created(created_history, db_date_key, canon_h_tech, canon_target, target_count_idx):
                                    print(f"⏭️ Truck assign header '{name}' (occurrence {target_count_idx} at {time_key}) was previously created and recorded in history. Skipping 2nd creation.")
                                    continue

                                # Duplicate prevention: Check if this truck already exists on ANY REAL tech on the board
                                # CRITICAL: Exclude staging/parking rows ("s4", "s3", "f2", "p2") because all fleet trucks
                                # reside on s4 as static headers! We only check if the truck is already assigned to a real technician!
                                truck_on_board = False
                                found_tech_name = None
                                found_cust_name = None
                                for c_t, w_list in existing_wos.items():
                                    if c_t in ("s4", "s3", "f2", "p2", "") or c_t.startswith("s4") or c_t.startswith("s3"):
                                        continue
                                    for wo in w_list:
                                        if not wo.get("is_appointment"):
                                            c_cust = wo.get("canon_customer", "")
                                            # Match genuine truck work order customer name on real technician row
                                            if c_cust and (c_cust == canon_target or c_cust in (f"truck{canon_target}", f"tr{canon_target}")):
                                                truck_on_board = True
                                                found_tech_name = c_t
                                                found_cust_name = c_cust
                                                break
                                    if truck_on_board:
                                        break

                                if truck_on_board:
                                    print(f"⏭️ Truck '{name}' already exists on tech '{found_tech_name}' (matched customer: '{found_cust_name}') for {date_str}. Recording in history and skipping creation to prevent duplicate assignment.")
                                    self.record_wo_created(created_history, db_date_key, canon_h_tech, canon_target, target_count_idx, status="existing_on_board", details={"name": name, "tech": h_tech, "time": time_key})
                                    continue

                                # Duplicate prevention: Check if assigned tech already has a truck on the board
                                if canon_h_tech and canon_h_tech not in ("s4", "s3", "f2", "p2", "") and not canon_h_tech.startswith("s4") and canon_h_tech in existing_wos:
                                    tech_already_has_truck = False
                                    for wo in existing_wos[canon_h_tech]:
                                        c_cust = wo.get("canon_customer", "")
                                        if any(self.canonical_str(t.get("name", "")) == c_cust for t in truck_assign_headers):
                                            tech_already_has_truck = True
                                            break
                                    if tech_already_has_truck:
                                        print(f"⚠️ Tech '{h_tech}' already has a truck assigned on {date_str}. Skipping creating '{name}' to prevent multiple trucks on one technician.")
                                        self.record_wo_created(created_history, db_date_key, canon_h_tech, canon_target, target_count_idx, status="existing_on_board", details={"name": name, "tech": h_tech, "time": time_key})
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
                                             # For non-appointment WOs: only skip if EXACT same name matches at same time
                                             if not wo.get("is_appointment"):
                                                 c_cust = wo.get("canon_customer", "")
                                                 w_time = wo.get("time_key", "")
                                                 name_match = c_cust == canon_target
                                                 time_match = is_service_needed_asset or not time_key or not w_time or time_key == w_time
                                                 if name_match and time_match:
                                                     board_count += 1
                                             else:
                                                 # For appointments (PTO/OFF/SICK): check time range overlap
                                                 match_found = False
                                                 if wo.get("start_time_obj") and wo.get("end_time_obj"):
                                                     try:
                                                         target_t = datetime.strptime(time_key, "%I:%M %p").time()
                                                         if wo["start_time_obj"] <= target_t <= wo["end_time_obj"]:
                                                             match_found = True
                                                     except Exception:
                                                         pass
                                                 if not match_found and wo.get("time_key") == time_key:
                                                     match_found = True
                                                 if match_found:
                                                     board_count += 1
                             
                                if board_count >= target_count_idx:
                                    print(f"⏭️ Truck assign header '{name}' (occurrence {target_count_idx} at {time_key}) already exists or tech is OFF/occupied. Recording in persistent history & skipping creation.")
                                    self.record_wo_created(created_history, db_date_key, canon_h_tech, canon_target, target_count_idx, status="existing_on_board", details={"name": name, "tech": h_tech, "time": time_key})
                                    continue

                            if dry_run:
                                print(f"  [Dry-Run] Truck Assign Header: {name} | Date: {date_str} | Time: {start_time} | Duration: {duration} | Priority: {priority} | Tech: {h_tech} | Task: {h_task}")
                            else:
                                res = await self.fill_and_save_work_order(
                                    customer_name=name,
                                    task=h_task,
                                    lead_source=lead_source,
                                    priority=priority,
                                    target_date=date_str,
                                    start_time=start_time,
                                    duration=duration,
                                    tech_name=h_tech,
                                    immediate_action=immediate_action,
                                    summary_note=None
                                )
                                if res:
                                    self.record_wo_created(created_history, db_date_key, canon_h_tech, canon_target, target_count_idx, status="created", details={"name": name, "tech": h_tech, "time": time_key})
                                    # Refresh board data after save
                                    f_date, f_wos = await self.fetch_board_data_for_date(db_date_key, board_req)
                                    if f_date == db_date_key:
                                        board_date_str, existing_wos = f_date, f_wos

                    # Tech Jobs (e.g. SHOP, internal meetings)
                    for job in tech_jobs:
                        if not job.get("active", True):
                            continue
                        t_tech = job.get("tech_name")
                        if t_tech and template_techs:
                            canon_job_tech = self.canonical_str(t_tech)
                            tech_is_active = False
                            for entry in template_techs:
                                if not entry.get("active", True):
                                    continue
                                c_name = self.canonical_str(entry.get("name", ""))
                                c_tname = self.canonical_str(entry.get("tech_name", ""))
                                if (canon_job_tech == c_name or canon_job_tech in c_name or c_name in canon_job_tech or
                                    canon_job_tech == c_tname or canon_job_tech in c_tname or c_tname in canon_job_tech):
                                    tech_is_active = True
                                    break
                            if not tech_is_active:
                                print(f"⏭️ Skipping tech job '{job.get('name')}' because assigned technician '{t_tech}' is inactive in template config.")
                                continue

                        name = job.get("name")
                        start_time = job.get("start_time")
                        duration = job.get("duration")
                        priority = job.get("priority")
                        j_task = job.get("task", task)

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

                            if not force and self.is_wo_created(created_history, db_date_key, canon_t_tech, canon_target, target_count_idx):
                                print(f"⏭️ Tech job '{name}' (occurrence {target_count_idx} at {time_key}) was previously created and recorded in history. Skipping 2nd creation.")
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
                                         # For non-appointment WOs: only skip if EXACT same name matches at same time
                                         if not wo.get("is_appointment"):
                                             c_cust = wo.get("canon_customer", "")
                                             w_time = wo.get("time_key", "")
                                             name_match = c_cust == canon_target
                                             time_match = not time_key or not w_time or time_key == w_time
                                             if name_match and time_match:
                                                 board_count += 1
                                         else:
                                             # For appointments (PTO/OFF/SICK): check time range overlap
                                             match_found = False
                                             if wo.get("start_time_obj") and wo.get("end_time_obj"):
                                                 try:
                                                     target_t = datetime.strptime(time_key, "%I:%M %p").time()
                                                     if wo["start_time_obj"] <= target_t <= wo["end_time_obj"]:
                                                         match_found = True
                                                 except Exception:
                                                     pass
                                             if not match_found and wo.get("time_key") == time_key:
                                                 match_found = True
                                             if match_found:
                                                 board_count += 1

                            if board_count >= target_count_idx:
                                print(f"⏭️ Tech job '{name}' (occurrence {target_count_idx} at {time_key}) already exists or tech is OFF/occupied. Recording in persistent history & skipping creation.")
                                self.record_wo_created(created_history, db_date_key, canon_t_tech, canon_target, target_count_idx, status="existing_on_board", details={"name": name, "tech": t_tech, "time": time_key})
                                continue

                        if dry_run:
                            print(f"  [Dry-Run] Tech Job: {name} | Date: {date_str} | Time: {start_time} | Duration: {duration} | Priority: {priority} | Tech: {t_tech} | Task: {j_task}")
                        else:
                            res = await self.fill_and_save_work_order(
                                customer_name=name,
                                task=j_task,
                                lead_source=lead_source,
                                priority=priority,
                                target_date=date_str,
                                start_time=start_time,
                                duration=duration,
                                tech_name=t_tech,
                                mark_completed=True,
                                immediate_action=immediate_action,
                                summary_note=None
                            )
                            if res:
                                self.record_wo_created(created_history, db_date_key, canon_t_tech, canon_target, target_count_idx, status="created", details={"name": name, "tech": t_tech, "time": time_key})
                                # Refresh board data after save
                                f_date, f_wos = await self.fetch_board_data_for_date(db_date_key, board_req)
                                if f_date == db_date_key:
                                    board_date_str, existing_wos = f_date, f_wos

                    # Technician Complete WOs
                    for tech in active_techs:
                        t_name = tech
                    
                        # Fetch specific metadata for this tech, or fallback to defaults
                        meta = tech_metadata.get(t_name) or tech_metadata.get(self.canonical_str(t_name)) or {}
                        start_time = meta.get("start_time") or tech_defaults.get("start_time")
                        duration = meta.get("duration") or tech_defaults.get("duration")
                        t_assignment_name = meta.get("tech_name", tech)
                        priority = tech_priorities.get(t_name) or tech_priorities.get(self.canonical_str(t_name)) or tech_defaults.get("priority")

                        # Check if the technician completed WO already exists on the board for the target date
                        if not dry_run:
                            canon_target = self.canonical_str(t_name)
                            canon_t_assignment = self.canonical_str(t_assignment_name or '')
                        
                            processed_key = (canon_t_assignment, canon_target)
                            template_processed_counts[processed_key] = template_processed_counts.get(processed_key, 0) + 1
                            target_count_idx = template_processed_counts[processed_key]

                            if not force and self.is_wo_created(created_history, db_date_key, canon_t_assignment, canon_target, target_count_idx):
                                print(f"  -> Completed WO for tech '{t_name}' (occurrence {target_count_idx}) was previously created and recorded in history. Skipping 2nd creation.")
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
                                print(f"  -> Completed WO for tech '{t_name}' (occurrence {target_count_idx}) skipped (tech has OFF/Appointment on board). Recording in persistent history.")
                                self.record_wo_created(created_history, db_date_key, canon_t_assignment, canon_target, target_count_idx, status="existing_on_board", details={"name": t_name, "tech": t_assignment_name})
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
                                task_type=task,
                                summary_note=None
                            )
                            if res:
                                self.record_wo_created(created_history, db_date_key, canon_t_assignment, canon_target, target_count_idx, status="created", details={"name": t_name, "tech": t_assignment_name})
                                # Refresh board data after save
                                f_date, f_wos = await self.fetch_board_data_for_date(db_date_key, board_req)
                                if f_date == db_date_key:
                                    board_date_str, existing_wos = f_date, f_wos

                if not dry_run:
                    _records_processed += 1

                    if not skip_asset:
                        # Step 2: On the SAME date, run Asset Assignment Reconciler to verify & reconcile truck assignments!
                        formatted_current_date = current_date.strftime("%m/%d/%Y")
                        print(f"\n" + "-" * 60)
                        print(f"🚛 [Step 2: Asset Assignment] Checking & Reconciling truck assignments for {formatted_current_date}...")
                        print(f"-" * 60)
                        if on_progress:
                            try:
                                self.progress_info["status_message"] = f"Step 2: Reconciling Asset Assignments for Day {d + 1} ({date_str})"
                                on_progress(self.progress_info)
                            except Exception:
                                pass
                        try:
                            if not hasattr(self, 'asset_scraper') or self.asset_scraper is None:
                                try:
                                    from asset_assignment_scraper import AssetAssignmentScraper
                                except ImportError:
                                    from dispatch.asset_assignment_scraper import AssetAssignmentScraper  # type: ignore
                                self.asset_scraper = AssetAssignmentScraper(template_path=self.template_path)

                            self.asset_scraper.playwright = self.playwright
                            self.asset_scraper.browser = self.browser
                            self.asset_scraper.context = self.context
                            self.asset_scraper.page = self.page
                            self.asset_scraper.template_data = self.template_data

                            recon_result = await self.asset_scraper.reconcile_single_date(
                                date_str=formatted_current_date,
                                click_wos=True
                            )
                            recon_cnt = recon_result.get('checked', 0)
                            recon_fixed = recon_result.get('reassignments', 0)
                            print(f"✅ [Step 2: Asset Assignment] Completed for {formatted_current_date}: Checked {recon_cnt} truck work orders, Reassigned {recon_fixed}.")
                        except Exception as asset_err:
                            print(f"⚠️ [Step 2: Asset Assignment] Note/Error on {formatted_current_date}: {asset_err}")

                    # Mark entire date as completed in persistent history so it will never be recreated/re-assigned even if deleted in FieldEdge
                    self.record_wo_created(
                        created_history,
                        db_date_key,
                        canon_tech="",
                        canon_target="date_processing_complete",
                        target_count_idx=1,
                        status="completed",
                        details={"name": "Date Processing Complete", "date": db_date_key}
                    )
                    print(f"🔒 [Date Completed] Marked date {db_date_key} as completed in persistent history.")
                elif not skip_asset:
                    formatted_current_date = current_date.strftime("%m/%d/%Y")
                    print(f"  [Dry-Run] Step 2: Would reconcile Asset Assignments for {formatted_current_date}")

                # Update progress after completing Day d + 1
                self.progress_info.update({
                    "current_day": d + 1,
                    "completed_days": d + 1,
                    "remaining_days": max(0, days - (d + 1)),
                    "percent_complete": round(((d + 1) / days) * 100, 1),
                    "status_message": f"Completed Day {d + 1} of {days} ({date_str})"
                })
                if on_progress:
                    try:
                        on_progress(self.progress_info)
                    except Exception:
                        pass

            print("\n🎉 Dispatch Board Display Automation & Asset Assignment Finished Successfully!")
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
            if not dry_run:
                await self.cleanup()

            # -- Execution completion summary --
            _elapsed = time.time() - _start_time
            if _error_occurred:
                print(f"⚠️ Execution finished with ERROR ({round(_elapsed, 1)}s):")
                print(_error_occurred)
            else:
                print(f"🎉 Execution completed successfully in {round(_elapsed, 1)}s (Processed {_records_processed} records)")

if __name__ == "__main__":
    import os
    import sys
    import argparse

    _dispatch_dir = os.path.dirname(os.path.abspath(__file__))
    if _dispatch_dir not in sys.path:
        sys.path.insert(0, _dispatch_dir)

    parser = argparse.ArgumentParser(description="Run Dispatch Board Display Automation Scraper (Standalone)")
    parser.add_argument("--days", type=int, default=2, help="Number of days to process (default: 2)")
    parser.add_argument("--live", action="store_true", help="Run live on FieldEdge (skips dry-run)")
    parser.add_argument("--start-date", type=str, help="Starting date (e.g. 'August 15', '08/15/2026'). Defaults to today.")
    parser.add_argument("--skip-asset", action="store_true", help="Skip running the asset assignment tool for each date")
    args = parser.parse_args()

    scraper = DispatchBoardDisplayAutomationScraper()
    asyncio.run(scraper.run(days=args.days, dry_run=not args.live, start_date_str=args.start_date, skip_asset=args.skip_asset))