import os
import sys
import json
import re
import asyncio
import random
from datetime import datetime, timedelta, time as datetime_time
from zoneinfo import ZoneInfo
try:
    PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    import pytz
    PACIFIC_TZ = pytz.timezone("America/Los_Angeles")

DUMMY_HEADER_TECHS = ("s3", "s4", "s6", "s7", "t9", "t91", "f2", "p2")
import time
import traceback
import urllib.request
import urllib.error
import unicodedata
from typing import List, Dict, Optional, Callable, Tuple, Any

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

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

    def __init__(self, template_path: Optional[str] = None):
        super().__init__()
        self.scraper_name = "dispatch-board-display-automation"
        self.intercepted_board_by_date: Dict[str, Any] = {}
        self.board_req: Dict[str, Any] = {}
        if template_path:
            self.template_path = template_path
        else:
            _curr = os.path.dirname(os.path.abspath(__file__))
            _candidate1 = os.path.join(_curr, "config", "dispatch_board_template.json")
            _candidate2 = os.path.join(os.path.dirname(_curr), "config", "dispatch_board_template.json")
            self.template_path = _candidate1 if os.path.exists(_candidate1) else _candidate2
        self.template_data = self._load_template()

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
                            print(f"✅ Synced {len(remote_history)} central tracking entries from Dashboard API.")
            except Exception as e:
                print(f"ℹ️ API history sync skipped ({e}). Proceeding with in-memory tracking.")

        return history

    @staticmethod
    def _is_created_record(v) -> bool:
        if isinstance(v, dict):
            status = v.get("status", "")
            return status in ("created", "existing_on_board", "completed")
        return False

    def is_wo_created(
        self,
        history: Dict,
        date_key: str,
        canon_tech: str,
        canon_target: str,
        target_count_idx: int,
        time_key: Optional[str] = None
    ) -> bool:
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
            if time_key and rec_time:
                if self.normalize_time_key(rec_time) == self.normalize_time_key(time_key):
                    return True
            elif not time_key or not rec_time:
                return True

        if not canon_tech:
            base_key_notech = f"{date_key}||{canon_target}|{target_count_idx}"
            if base_key_notech in history and self._is_created_record(history[base_key_notech]):
                rec = history[base_key_notech]
                rec_time = rec.get("time") or (rec.get("details", {}).get("time") if isinstance(rec.get("details"), dict) else None)
                if time_key and rec_time:
                    if self.normalize_time_key(rec_time) == self.normalize_time_key(time_key):
                        return True
                elif not time_key or not rec_time:
                    return True

        for k, v in history.items():
            if not self._is_created_record(v) or not isinstance(v, dict):
                continue
            if (v.get("date") == date_key or k.startswith(f"{date_key}|")):
                v_target = v.get("canon_target", "")
                v_base = v.get("base_target", "")
                target_matched = (
                    v_target == canon_target or
                    v_base == canon_target or
                    (norm_time and v_target == f"{canon_target}_{norm_time}")
                )
                if not target_matched:
                    continue
                if v.get("occurrence") != target_count_idx:
                    continue

                rec_tech = v.get("canon_tech") or ""
                tech_matched = (rec_tech == canon_tech or (rec_tech and canon_tech and (rec_tech in canon_tech or canon_tech in rec_tech)))
                if not tech_matched:
                    continue

                rec_time = v.get("time") or (v.get("details", {}).get("time") if isinstance(v.get("details"), dict) else None)
                if time_key and rec_time:
                    if self.normalize_time_key(rec_time) == self.normalize_time_key(time_key):
                        return True
                    continue
                elif not time_key or not rec_time:
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
        if force:
            return False

        template_processed_counts = {}

        for header in static_headers:
            name = header.get("name")
            start_time = header.get("start_time")
            h_tech = header.get("tech_name")
            canon_target = self.canonical_str(name)
            is_service_wo = bool(
                header.get('is_service_needed_duplicate') or
                (header.get('service_note') and ('SERVICE NEEDED' in str(header.get('priority', '')) or '7.12' in str(header.get('priority', ''))))
            )
            is_oos_wo = bool(
                header.get('is_out_of_service') or
                (header.get('reason') and 'DO NOT DISPATCH' in str(header.get('priority', '')))
            )
            if is_service_wo:
                tracking_target = f"{canon_target}_serviceneeded"
            elif is_oos_wo:
                tracking_target = f"{canon_target}_outofservice"
            else:
                tracking_target = canon_target
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

            if not self.is_wo_created(history, db_date_key, canon_h_tech, tracking_target, target_count_idx, time_key=time_key):
                return False

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

                if not self.is_wo_created(history, db_date_key, canon_h_tech, canon_target, target_count_idx, time_key=time_key):
                    return False

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

            if not self.is_wo_created(history, db_date_key, canon_t_tech, canon_target, target_count_idx, time_key=time_key):
                return False

        for tech in active_techs:
            t_assignment_name = tech
            canon_target = self.canonical_str(tech)
            canon_t_assignment = self.canonical_str(t_assignment_name or '')
            processed_key = (canon_t_assignment, canon_target)
            template_processed_counts[processed_key] = template_processed_counts.get(processed_key, 0) + 1
            target_count_idx = template_processed_counts[processed_key]

            if not self.is_wo_created(history, db_date_key, canon_t_assignment, canon_target, target_count_idx, time_key="4:45 AM"):
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
            try:
                import urllib.request
                import threading
                endpoint = f"{api_url}/automation/dispatch-history/" if api_url.endswith('/api') else f"{api_url}/api/automation/dispatch-history/"
                req_data = json.dumps(rec_data).encode('utf-8')
                headers = {"Content-Type": "application/json", "User-Agent": "SterlingAutomations/1.0"}
                headers.update(self._get_api_auth_headers())

                def _send_post(ep, data_bytes, req_headers):
                    try:
                        post_req = urllib.request.Request(
                            ep,
                            data=data_bytes,
                            headers=req_headers,
                            method="POST"
                        )
                        with urllib.request.urlopen(post_req, timeout=4) as response:
                            pass
                    except Exception:
                        pass

                threading.Thread(target=_send_post, args=(endpoint, req_data, headers), daemon=True).start()
            except Exception:
                pass

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
                    found = DispatchBoardDisplayAutomationScraper._extract_jwt_from_response(v)
                    if found:
                        return found
        elif isinstance(data, list):
            for v in data:
                found = DispatchBoardDisplayAutomationScraper._extract_jwt_from_response(v)
                if found:
                    return found
        return None

    _cached_api_token = None

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
                if not token:
                    for cookie in response.headers.get_all("Set-Cookie") or []:
                        name, _, rest = cookie.partition("=")
                        if name.strip().lower() in ("access", "access_token", "accesstoken", "token", "jwt"):
                            token = rest.split(";", 1)[0].strip()
                            break
                if token:
                    self._cached_api_token = token
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
        # Code prefix matching (e.g. "p2" matching "p2asher", "f2" matching "f2kelvin", "p6" matching "p6kyle"):
        if re.match(r'^[a-zA-Z]{1,2}\d{1,2}$', c1) and c2.startswith(c1):
            return True
        if re.match(r'^[a-zA-Z]{1,2}\d{1,2}$', c2) and c1.startswith(c2):
            return True
        # Core technician name matching (strips leading code like 'p6 ', 'b ', 'f2 ', 'p2 '):
        core1 = re.sub(r'^[a-z]{1,2}\d{0,2}\s+', '', unicodedata.normalize('NFKD', str(tech1)).strip().lower())
        core2 = re.sub(r'^[a-z]{1,2}\d{0,2}\s+', '', unicodedata.normalize('NFKD', str(tech2)).strip().lower())
        c_core1 = re.sub(r'[^a-z0-9]', '', core1)
        c_core2 = re.sub(r'[^a-z0-9]', '', core2)
        if c_core1 and c_core2 and c_core1 == c_core2:
            return True
        return False

    @staticmethod
    def is_same_equipment(name1: str, name2: str) -> bool:
        if not name1 or not name2:
            return False
        import unicodedata, re
        s1 = unicodedata.normalize('NFKD', str(name1)).strip()
        s2 = unicodedata.normalize('NFKD', str(name2)).strip()

        is_pump1 = "🚛" in s1 or any(x in s1.lower() for x in ("pump", "pt (", "pt/"))
        is_serv1 = "🛻" in s1 or any(x in s1.lower() for x in ("ser tr", "service"))
        is_exc1 = "🏗" in s1 or any(x in s1.lower() for x in ("exc", "u-17", "u-40", "u17", "u40"))
        is_trai1 = any(x in s1 for x in ("▭", "🔲")) or any(x in s1.lower() for x in ("trai", "tilt", "dump", "t-1", "t-2", "t-3", "t-4", "t-05", "t-6"))
        is_supp1 = "👥" in s1 or any(x in s1.lower() for x in ("laborer", "support", "helper"))

        is_pump2 = "🚛" in s2 or any(x in s2.lower() for x in ("pump", "pt (", "pt/"))
        is_serv2 = "🛻" in s2 or any(x in s2.lower() for x in ("ser tr", "service"))
        is_exc2 = "🏗" in s2 or any(x in s2.lower() for x in ("exc", "u-17", "u-40", "u17", "u40"))
        is_trai2 = any(x in s2 for x in ("▭", "🔲")) or any(x in s2.lower() for x in ("trai", "tilt", "dump", "t-1", "t-2", "t-3", "t-4", "t-05", "t-6"))
        is_supp2 = "👥" in s2 or any(x in s2.lower() for x in ("laborer", "support", "helper"))

        cat1 = "pump" if is_pump1 else ("serv" if is_serv1 else ("exc" if is_exc1 else ("trai" if is_trai1 else ("supp" if is_supp1 else None))))
        cat2 = "pump" if is_pump2 else ("serv" if is_serv2 else ("exc" if is_exc2 else ("trai" if is_trai2 else ("supp" if is_supp2 else None))))

        if cat1 and cat2 and cat1 != cat2:
            return False

        c1 = re.sub(r'[^a-zA-Z0-9]', '', s1).lower()
        c2 = re.sub(r'[^a-zA-Z0-9]', '', s2).lower()
        if not c1 or not c2:
            return False
        if c1 == c2:
            return True
        if c1 in (f"truck{c2}", f"tr{c2}") or c2 in (f"truck{c1}", f"tr{c1}"):
            return True

        id1 = s1.split("|")[-1].strip() if "|" in s1 else s1
        id2 = s2.split("|")[-1].strip() if "|" in s2 else s2
        cid1 = re.sub(r'[^a-zA-Z0-9]', '', id1).lower()
        cid2 = re.sub(r'[^a-zA-Z0-9]', '', id2).lower()
        if cid1 and cid2 and cid1 == cid2:
            return True

        if cid1.isdigit() and cid2.isdigit() and int(cid1) == int(cid2):
            if cat1 == cat2 or (not cat1 and not cat2):
                return True

        return False

    @staticmethod
    def _extract_all_strings_recursive(obj) -> List[str]:
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
    def normalize_time_key(time_val: Optional[str]) -> str:
        if not time_val:
            return ""
        s = str(time_val).strip()
        for fmt in ["%I:%M %p", "%I:%M%p", "%I:%M:%S %p", "%H:%M:%S", "%H:%M"]:
            try:
                dt = datetime.strptime(s.upper(), fmt)
                formatted = dt.strftime("%I:%M %p")
                return formatted[1:] if formatted.startswith("0") else formatted
            except Exception:
                pass
        return s.upper().lstrip("0")

    @staticmethod
    def check_item_match(
        wo: Dict,
        canon_target: str,
        time_key: Optional[str] = None,
        require_customer_match: bool = False,
        is_service_wo: bool = False,
        is_oos_wo: bool = False,
        match_any_status: bool = False
    ) -> bool:
        if not canon_target:
            return False

        if wo.get("is_appointment"):
            return False

        if not match_any_status:
            wo_is_sn = bool(wo.get("is_service_needed"))
            wo_is_oos = bool(wo.get("is_out_of_service"))

            if is_service_wo and not wo_is_sn:
                return False
            if not is_service_wo and wo_is_sn:
                return False

            if is_oos_wo and not wo_is_oos:
                return False
            if not is_oos_wo and wo_is_oos:
                return False

        w_time = wo.get("time_key", "")
        wo_time_keys = wo.get("time_keys") or ({DispatchBoardDisplayAutomationScraper.normalize_time_key(w_time)} if w_time else set())
        if time_key:
            norm_target = DispatchBoardDisplayAutomationScraper.normalize_time_key(time_key)
            if norm_target and wo_time_keys and norm_target not in wo_time_keys:
                if not (is_service_wo or is_oos_wo):
                    return False

        c_cust = wo.get("canon_customer", "")
        c_strings = wo.get("canon_strings", [])

        if require_customer_match or len(canon_target) <= 4 or len(c_cust) <= 4:
            if c_cust == canon_target:
                return True
            if canon_target in c_strings:
                if not any(cs.startswith("u") or cs.startswith("t") for cs in [c_cust, canon_target]):
                    return True
            if c_cust and canon_target and c_cust.isdigit() and canon_target.isdigit() and int(c_cust) == int(canon_target):
                return True
            return False

        if canon_target in c_strings:
            return True

        if c_cust and len(canon_target) >= 5 and len(c_cust) >= 5 and (canon_target == c_cust or canon_target in c_cust or c_cust in canon_target):
            return True

        c_comb = wo.get("canon_combined", "")
        if c_comb and len(canon_target) >= 5 and (canon_target == c_comb or canon_target in c_comb):
            return True

        return False

    @staticmethod
    def parse_fieldedge_datetime(dt_val: Any) -> Tuple[str, str, Optional[datetime_time]]:
        """
        Parses a FieldEdge datetime value into (date_str: YYYY-MM-DD, time_key: I:M p, time_obj: time).
        FieldEdge API timestamps represent the local dispatch board time directly (even if trailing 'Z' is present).
        """
        if not dt_val:
            return "", "", None
        raw_str = str(dt_val).strip()
        if not raw_str:
            return "", "", None

        clean_naive = re.sub(r'[+-]\d{2}:?\d{2}$', '', raw_str.replace("Z", "")).replace("T", " ").split(".")[0].strip()
        for fmt in [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%I:%M %p",
            "%H:%M:%S",
            "%H:%M"
        ]:
            try:
                local_dt = datetime.strptime(clean_naive, fmt)
                date_str = local_dt.strftime("%Y-%m-%d") if "%Y" in fmt else ""
                time_key = local_dt.strftime("%I:%M %p")
                if time_key.startswith("0"):
                    time_key = time_key[1:]
                return date_str, time_key, local_dt.time()
            except Exception:
                pass

        return "", "", None

    def extract_existing_wos(self, board_data, target_date_key: Optional[str] = None):
        if not board_data or "Board" not in board_data:
            return None, {}
        
        board_date_str = target_date_key or None
        existing_wos = {}
        
        if not board_date_str and board_data["Board"].get("Headers"):
            first_header = board_data["Board"]["Headers"][0]
            dt_str = first_header.get("HeaderDateTime")
            if dt_str:
                try:
                    board_date_str = dt_str.replace("T", " ").split(" ")[0]
                except Exception:
                    pass

        effective_date = target_date_key or board_date_str
        
        for group in board_data["Board"].get("Groups", []):
            for row in group.get("Rows", []):
                raw_row_name = row.get("Name") or ""
                canon_tech = self.canonical_str(raw_row_name)
                
                if canon_tech not in existing_wos:
                    existing_wos[canon_tech] = []
                
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
                    item_date_str, time_key, time_obj = self.parse_fieldedge_datetime(dt_str)

                    naive_date_str = ""
                    naive_time_key = ""
                    if dt_str:
                        raw_dt_clean = re.sub(r'[+-]\d{2}:?\d{2}$', '', str(dt_str).replace("Z", "")).replace("T", " ").split(".")[0].strip()
                        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%I:%M %p", "%H:%M:%S", "%H:%M"]:
                            try:
                                ldt = datetime.strptime(raw_dt_clean, fmt)
                                if "%Y" in fmt:
                                    naive_date_str = ldt.strftime("%Y-%m-%d")
                                t_k = ldt.strftime("%I:%M %p")
                                if t_k.startswith("0"): t_k = t_k[1:]
                                naive_time_key = t_k
                                break
                            except Exception:
                                pass

                    if effective_date:
                        date_matches = (
                            item_date_str == effective_date or
                            naive_date_str == effective_date or
                            (not item_date_str and not naive_date_str)
                        )
                        if not date_matches:
                            continue

                    time_keys = set()
                    if time_key:
                        time_keys.add(self.normalize_time_key(time_key))
                    if naive_time_key:
                        time_keys.add(self.normalize_time_key(naive_time_key))

                    item_prio = str(item.get("Priority") or item.get("PriorityName") or item.get("PriorityDescription") or "")
                    norm_prio = re.sub(r'[^a-zA-Z0-9]', '', item_prio).lower()

                    is_sn = (
                        "serviceneeded" in norm_prio or
                        "712" in norm_prio or
                        any("serviceneeded" in s.lower() or "712" in s for s in canon_strings) or
                        bool(time_keys.intersection({"3:30 PM", "3:30", "03:30 PM", "3:30pm"})) and canon_tech in ["s3", "s4"]
                    )

                    is_oos = (
                        "donotdispatch" in norm_prio or
                        "outofservice" in norm_prio or
                        any("donotdispatch" in s.lower() or "outofservice" in s.lower() for s in canon_strings) or
                        bool(time_keys.intersection({"1:45 PM", "1:45", "01:45 PM", "1:45pm"})) and canon_tech in ["s3", "s4"]
                    )
                            
                    existing_wos[canon_tech].append({
                        "canon_customer": canon_customer,
                        "canon_strings": canon_strings,
                        "canon_combined": canon_combined,
                        "time_key": time_key or naive_time_key,
                        "time_keys": time_keys,
                        "time_obj": time_obj,
                        "is_service_needed": is_sn,
                        "is_out_of_service": is_oos,
                        "is_appointment": False
                    })

                for appt in row.get("AppointmentItems", []):
                    all_raw_strings = self._extract_all_strings_recursive(appt)
                    canon_strings = [self.canonical_str(s) for s in all_raw_strings if s]
                    canon_strings = [cs for cs in canon_strings if cs]
                    canon_combined = self.canonical_str(" ".join(all_raw_strings)) if all_raw_strings else ""

                    desc_name = str(appt.get("Description") or appt.get("Title") or appt.get("Name") or "OFF")
                    canon_customer = self.canonical_str(desc_name)

                    dt_str = (
                        appt.get("ScheduledFrom") or
                        appt.get("ScheduleDateTime") or
                        appt.get("ScheduledDateTime") or
                        appt.get("StartDateTime") or
                        appt.get("StartTime")
                    )
                    dt_to_str = appt.get("ScheduledTo") or appt.get("EndDateTime")
                    appt_from_date, time_key, start_time_obj = self.parse_fieldedge_datetime(dt_str)
                    appt_to_date, _, end_time_obj = self.parse_fieldedge_datetime(dt_to_str)

                    if effective_date:
                        if appt_to_date and appt_to_date < effective_date:
                            continue
                        if appt_from_date and appt_from_date > effective_date:
                            continue

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
        req_to_use = board_req or getattr(self, 'board_req', {})
        if not req_to_use or not req_to_use.get("url"):
            print(f"  ⚠️ Cannot dynamically fetch board data for {target_date_str}: board_req template missing.")
            return None, {}

        try:
            payload = json.loads(req_to_use.get("post_data") or "{}")
            payload["Date"] = target_date_str + "T00:00:00"
            for k in list(payload.keys()):
                if "date" in k.lower():
                    if "end" in k.lower():
                        payload[k] = target_date_str + "T23:59:59"
                    else:
                        payload[k] = target_date_str + "T00:00:00"

            headers = dict(req_to_use.get("headers", {}))
            cleaned_headers = {}
            forbidden = {'host', 'content-length', 'connection', 'origin', 'referer', 'sec-fetch-dest', 'sec-fetch-mode', 'sec-fetch-site'}
            for hk, hv in headers.items():
                if hk.lower() not in forbidden and not hk.startswith(':'):
                    cleaned_headers[hk] = hv
            cleaned_headers['Content-Type'] = 'application/json'

            fetch_js = f"""
            async () => {{
                try {{
                    const resp = await fetch("{req_to_use['url']}", {{
                        method: 'POST',
                        credentials: 'include',
                        headers: {json.dumps(cleaned_headers)},
                        body: JSON.stringify({json.dumps(payload)})
                    }});
                    if (!resp.ok) {{
                        return {{ error: 'HTTP ' + resp.status, status: resp.status }};
                    }}
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
                print(f"📡 Dynamically fetched board date: '{b_date}' | Found {len(e_wos)} tech groups, {total_wos} existing work orders.")
                return b_date, e_wos
            else:
                err_msg = new_data.get("error") if isinstance(new_data, dict) else str(new_data)
                print(f"⚠️ Dynamic board fetch returned invalid response for {target_date_str}: {err_msg}")
        except Exception as e:
            print(f"⚠️ Exception fetching board data for {target_date_str}: {e}")
        return None, {}

    async def get_or_fetch_board_data_for_date(self, target_date_obj: datetime, max_retries: int = 2) -> Tuple[Optional[str], Optional[Dict]]:
        target_date_key = target_date_obj.strftime("%Y-%m-%d")

        # 1. First check if we already have it in cache
        if target_date_key in self.intercepted_board_by_date:
            b_data = self.intercepted_board_by_date[target_date_key]
            b_date, e_wos = self.extract_existing_wos(b_data, target_date_key=target_date_key)
            total_items = sum(len(w_list) for w_list in e_wos.values())
            print(f"📡 Verified FieldEdge API board data from cache for {target_date_key}: {len(e_wos)} tech groups, {total_items} items.")
            return b_date, e_wos

        # 2. Navigate to the date on the Dispatch Board
        await self.navigate_to_board_date(target_date_obj)

        # 3. Dynamic fetch via page.evaluate
        if self.board_req and self.board_req.get("url"):
            print(f"🔄 Fetching FieldEdge API board data for target date {target_date_key}...")
            b_date, e_wos = await self.fetch_board_data_for_date(target_date_key, self.board_req)
            if b_date == target_date_key and e_wos is not None:
                total_items = sum(len(w_list) for w_list in e_wos.values())
                print(f"📡 Verified FieldEdge API board data via fetch for {target_date_key}: {len(e_wos)} tech groups, {total_items} items.")
                return b_date, e_wos

        # 4. If retry is allowed, retry once
        if max_retries > 0:
            print(f"🔁 Retrying FieldEdge API board fetch for {target_date_key}...")
            await self.page.wait_for_timeout(1000)
            b_date, e_wos = await self.fetch_board_data_for_date(target_date_key, self.board_req)
            if b_date == target_date_key and e_wos is not None:
                total_items = sum(len(w_list) for w_list in e_wos.values())
                print(f"📡 Verified FieldEdge API board data on retry for {target_date_key}: {len(e_wos)} tech groups, {total_items} items.")
                return b_date, e_wos

        # 5. If STILL not verified, do NOT guess or default to empty dict! Return None to indicate failure!
        print(f"⛔ CRITICAL SAFETY GUARD: Could NOT verify FieldEdge Dispatch Board API for date {target_date_key}!")
        return None, None

    async def navigate_to_board_date(self, target_date_obj: datetime) -> bool:
        try:
            date_url_str = target_date_obj.strftime("%m-%d-%Y")
            target_url = f"https://login.fieldedge.com/#/DispatchBoard/{date_url_str}"
            print(f"🗓️ Navigating to Dispatch Board URL for date: {target_url}")
            
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
            print(f"⚠️ Could not navigate to board date via URL {target_url}: {e}")
            return False

    async def ensure_clean_dispatch_board(self, force_reload: bool = False):
        dispatch_url = self.rules.get('web_url', 'https://login.fieldedge.com/Dispatch')
        current_url = self.page.url

        if "Login" in current_url or await self.page.locator("input[name='UserName']").is_visible():
            print("⚠️ Detected Login redirect. Re-authenticating to FieldEdge...")
            try:
                await self.login_fieldedge()
                print(f"Returning to Dispatch Board: {dispatch_url}")
                await self.page.goto(dispatch_url, wait_until='domcontentloaded')
                try:
                    btn_loc = self.page.locator("//button[contains(normalize-space(), 'Work Order') or contains(normalize-space(), 'Create Work Order')]")
                    await btn_loc.first.wait_for(state="attached", timeout=4000)
                except Exception:
                    await self.page.wait_for_timeout(1000)
                await self.page.wait_for_timeout(500)
            except Exception as e:
                print(f"❌ Re-authentication failed: {e}")
            return

        needs_fresh_load = force_reload

        if "Dispatch" not in current_url or "DispatchSummary" in current_url:
            needs_fresh_load = True
        else:
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
            try:
                btn_loc = self.page.locator("//button[contains(normalize-space(), 'Work Order') or contains(normalize-space(), 'Create Work Order')]")
                await btn_loc.first.wait_for(state="attached", timeout=4000)
            except Exception:
                await self.page.wait_for_timeout(1000)
            await self.page.wait_for_timeout(500)

    async def open_create_wo_modal(self):
        xpath = self.rules.get('dispatch_board_create_wo_btn_xpath') or "//button[contains(normalize-space(), 'Work Order') or contains(normalize-space(), 'Create Work Order')]"
        if not xpath:
            print("⚠️ 'dispatch_board_create_wo_btn_xpath' rule not defined.")
            return False

        await self.ensure_clean_dispatch_board()

        customer_modal_selectors = [
            self.rules.get(
                'dispatch_board_customer_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Customer Name')]/following::div[contains(@class,'v-select')][1]"
            ),
            "#vscreate-wo-customer-select",
            "#vscreate-wo-customer-select__combobox",
            "div[id*='vscreate-wo-customer-select']",
            ".customer-select",
            ".customer-search-container"
        ]

        # 1. Quick check: is the Create Work Order modal ALREADY open from a previous step?
        for cand in customer_modal_selectors:
            try:
                m_loc = self.page.locator(cand).first
                if await m_loc.count() > 0 and await m_loc.is_visible():
                    return True
            except Exception:
                pass

        # 2. Wait for any loading spinners or overlays to clear first
        try:
            spinners = self.page.locator(".loading, .spinner, .v-spinner, div[class*='loading']:visible")
            if await spinners.count() > 0:
                await spinners.first.wait_for(state="hidden", timeout=5000)
        except Exception:
            pass

        btn_candidates = [
            xpath,
            "//button[contains(normalize-space(), 'Create Work Order') or contains(normalize-space(), 'Work Order')]",
            "button:has-text('Create Work Order')",
            "button:has-text('Work Order')",
            "button[data-automation-id*='create-wo']",
            "button[class*='create-wo']"
        ]

        # 3. Robust polling loop: give FieldEdge up to 10 seconds to finish rendering and make button visible
        import time
        start_t = time.time()
        clicked = False

        while time.time() - start_t < 10:
            for cand in btn_candidates:
                try:
                    btns = self.page.locator(cand)
                    c = await btns.count()
                    for i in range(c):
                        btn = btns.nth(i)
                        if await btn.is_visible():
                            await btn.scroll_into_view_if_needed()
                            try:
                                await btn.click(timeout=1500)
                            except Exception:
                                await self.page.evaluate("(el) => el.click()", await btn.element_handle())
                            clicked = True
                            break
                    if clicked:
                        break
                except Exception:
                    pass
            if clicked:
                break
            await self.page.wait_for_timeout(300)

        # 4. Wait for modal to appear
        if clicked:
            _modal_start = time.time()
            modal_visible = False
            while time.time() - _modal_start < 10:
                for cand in customer_modal_selectors:
                    try:
                        loc = self.page.locator(cand).first
                        if await loc.count() > 0 and await loc.is_visible():
                            modal_visible = True
                            break
                    except Exception:
                        pass
                if modal_visible:
                    break
                await self.page.wait_for_timeout(150)

            if modal_visible:
                return True

            print("  -> Customer modal element not detected after initial click, attempting JS re-click...")
            for cand in btn_candidates:
                try:
                    btns = self.page.locator(cand)
                    c = await btns.count()
                    for i in range(c):
                        btn = btns.nth(i)
                        if await btn.is_visible():
                            try:
                                await self.page.evaluate("(el) => el.click()", await btn.element_handle())
                            except Exception:
                                pass
                except Exception:
                    pass
            await self.page.wait_for_timeout(1000)

            for cand in customer_modal_selectors:
                try:
                    loc = self.page.locator(cand).first
                    if await loc.count() > 0 and await loc.is_visible():
                        return True
                except Exception:
                    pass

        # 5. Fallback reload retry if initial attempts failed
        print("⚠️ Could not locate or open Create Work Order modal within 10s. Retrying with fresh dispatch board reload...")
        try:
            await self.ensure_clean_dispatch_board(force_reload=True)
            await self.page.wait_for_timeout(1000)

            retry_clicked = False
            retry_start = time.time()
            while time.time() - retry_start < 10:
                for cand in btn_candidates:
                    try:
                        btns = self.page.locator(cand)
                        c = await btns.count()
                        for i in range(c):
                            btn = btns.nth(i)
                            if await btn.is_visible():
                                await btn.scroll_into_view_if_needed()
                                try:
                                    await btn.click(timeout=1500)
                                except Exception:
                                    await self.page.evaluate("(el) => el.click()", await btn.element_handle())
                                retry_clicked = True
                                break
                        if retry_clicked:
                            break
                    except Exception:
                        pass
                if retry_clicked:
                    break
                await self.page.wait_for_timeout(300)

            if not retry_clicked:
                print("❌ Retry also failed: No visible Create Work Order button found.")
                return False

            await self.page.wait_for_timeout(1000)
            return True
        except Exception as retry_err:
            print(f"❌ Retry also failed: {retry_err}")
            return False

    async def close_create_wo_modal(self):
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
                    await self.page.wait_for_timeout(200)
                    return True
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(250)
            return True
        except Exception:
            return False

    @staticmethod
    def extract_name_variants(text: str) -> List[str]:
        """
        Extracts all useful search & match variants for a name (with and without emojis/pipes).
        e.g. '👥 | GENE' -> ['👥 | GENE', '| GENE', 'GENE']
             '🛻 | 04'   -> ['🛻 | 04', '| 04', '04']
             ' GENE'     -> [' GENE', 'GENE']
        """
        variants = []
        if not text:
            return variants
        raw = str(text).strip()
        variants.append(raw)

        # 1. Strip emojis and astral unicode symbols
        no_emoji = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f\u200d]', '', raw).strip()
        if no_emoji and no_emoji != raw:
            variants.append(no_emoji)

        # 2. Strip leading/trailing pipes, symbols, whitespace, em-spaces
        clean_core = re.sub(r'^[|#\-_\s\u2000-\u200f]+|[|#\-_\s\u2000-\u200f]+$', '', no_emoji).strip()
        if clean_core and clean_core not in variants:
            variants.append(clean_core)

        # 3. Extract after pipe if present
        if "|" in raw:
            right_side = raw.split("|")[-1].strip()
            right_clean = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f\u200d\u2000-\u200f]', '', right_side).strip()
            if right_clean and right_clean not in variants:
                variants.append(right_clean)

        return variants

    _BANNER_EMOJI_REGEX = re.compile(
        r'['
        r'\u203C-\u3299'         # Miscellaneous symbols, Dingbats, geometric shapes
        r'\U0001F000-\U0001F9FF' # Emoticons, transport, supplemental symbols
        r'\U0001FA00-\U0001FAFF' # Symbols and pictographs extended-A
        r']+'
    )

    @classmethod
    def has_banner_prefix(cls, t: str) -> bool:
        if not t:
            return False
        return ('|' in t) or bool(cls._BANNER_EMOJI_REGEX.search(str(t)))

    async def select_dropdown_option(
        self,
        dropdown_selector: str,
        option_text: str,
        press_enter: bool = True,
        is_customer: bool = False,
        is_tech: bool = False
    ) -> bool:
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
            await self.page.wait_for_timeout(200)

            search_input = dropdown.locator("input.vs__search, input[type='search'], input").first
            if await search_input.count() == 0 or not await search_input.is_visible():
                search_input = self.page.locator("input.vs__search:visible, input[type='search']:visible").first

            try:
                await search_input.focus()
                await search_input.fill("")
            except Exception:
                pass

            target_has_banner = self.has_banner_prefix(option_text)
            name_variants = self.extract_name_variants(option_text)
            target_canons = set(self.canonical_str(v) for v in name_variants if self.canonical_str(v))
            target_norms = set(unicodedata.normalize('NFKD', v).strip().lower() for v in name_variants if v)
            is_time_target = bool(re.search(r'\b\d{1,2}:\d{2}\b', option_text)) or any(w in ('am', 'pm') for w in re.findall(r'\b[a-zA-Z0-9]+\b', option_text.lower()))

            clean_search = unicodedata.normalize('NFKD', option_text).strip()
            clean_core = re.sub(r'^[a-zA-Z0-9]{1,3}\s+', '', clean_search).strip()

            type_text = re.sub(r'^[\s\u2000-\u200f]+|[\s\u2000-\u200f]+$', '', str(option_text)).strip('\r\n')
            await search_input.fill(type_text)
            await self.page.wait_for_timeout(250)

            if press_enter:
                await search_input.press("Enter")
                await self.page.wait_for_timeout(350)

                selected_check = dropdown.locator(".vs__selected, .selected-tag").first
                if await selected_check.count() > 0 and (await selected_check.text_content() or "").strip():
                    sel_text = (await selected_check.text_content() or "").strip()
                    c_sel = self.canonical_str(sel_text)
                    c_target = self.canonical_str(option_text)
                    t_words = set(re.findall(r'\b[a-zA-Z0-9]+\b', option_text.lower()))
                    s_words = set(re.findall(r'\b[a-zA-Z0-9]+\b', sel_text.lower()))
                    qualifier_conflict = is_time_target and ((('am' in t_words) != ('am' in s_words)) or (('pm' in t_words) != ('pm' in s_words)))
                    sel_has_banner = self.has_banner_prefix(sel_text)
                    banner_conflict = (target_has_banner != sel_has_banner)

                    is_initial_match = False
                    if not qualifier_conflict and not banner_conflict:
                        if sel_text.strip().lower() == option_text.strip().lower() or c_sel == c_target or c_sel in target_canons:
                            is_initial_match = True
                        elif self.has_banner_prefix(option_text) and self.is_same_equipment(sel_text, option_text):
                            is_initial_match = True
                        elif is_tech and self.is_tech_match(sel_text, option_text):
                            is_initial_match = True

                    if is_initial_match:
                        print(f"  -> Selected '{sel_text}' via Enter directly.")
                        return True
                    else:
                        conflict_desc = "banner/emoji conflict" if banner_conflict else ("time qualifier conflict" if qualifier_conflict else "non-matching option")
                        print(f"  -> Enter selected '{sel_text}' ({conflict_desc} for '{option_text}': target_banner={target_has_banner}, sel_banner={sel_has_banner}). Clearing to pick exact match from dropdown menu...")
                        clear_btn = dropdown.locator(".vs__clear, button.close").first
                        if await clear_btn.count() > 0 and await clear_btn.is_visible():
                            await clear_btn.click(force=True)
                        else:
                            await search_input.click(force=True)
                        await self.page.wait_for_timeout(200)
                        try:
                            await search_input.focus()
                            await search_input.fill("")
                            await search_input.fill(type_text)
                            await self.page.wait_for_timeout(250)
                        except Exception:
                            pass

                try:
                    dyn_opts = self.page.locator("ul.vs__dropdown-menu:visible li, ul[role='listbox']:visible li, li.vs__dropdown-option:visible")
                    await dyn_opts.first.wait_for(state="visible", timeout=1200)
                except Exception:
                    await self.page.wait_for_timeout(300)

            options = self.page.locator("ul.vs__dropdown-menu:visible li, ul[role='listbox']:visible li, li.vs__dropdown-option:visible")
            try:
                await options.first.wait_for(state="visible", timeout=6000)
            except Exception:
                pass

            if press_enter:
                for _retry in range(2):
                    if await options.count() > 0:
                        break
                    print(f"  -> Customer search: no results yet, retrying ({_retry + 1}/2)...")
                    await search_input.press("Enter")
                    await self.page.wait_for_timeout(1200)
                    try:
                        await options.first.wait_for(state="visible", timeout=4000)
                    except Exception:
                        pass

                # Fallback: If searching with full name (or emoji) yielded 0 options, retry with clean emoji-free variants
                if (await options.count()) == 0 and len(name_variants) > 1:
                    for fallback_var in name_variants[1:]:
                        if (await options.count()) > 0:
                            break
                        print(f"  -> No options found for '{clean_search}'. Retrying customer search without emoji using '{fallback_var}'...")
                        try:
                            await search_input.focus()
                            await search_input.fill("")
                            await search_input.fill(fallback_var)
                            await self.page.wait_for_timeout(200)
                            await search_input.press("Enter")
                            await self.page.wait_for_timeout(1200)
                            try:
                                await options.first.wait_for(state="visible", timeout=5000)
                            except Exception:
                                pass
                        except Exception:
                            pass

                if (await options.count()) == 0:
                    m_dig = re.search(r'\b\d{2,4}\b', clean_search)
                    if m_dig and m_dig.group(0) != clean_search:
                        fallback_term = m_dig.group(0)
                        print(f"  -> No options found for '{clean_search}'. Retrying customer search with clean truck number '{fallback_term}'...")
                        try:
                            await search_input.focus()
                            await search_input.fill("")
                            await search_input.fill(fallback_term)
                            await self.page.wait_for_timeout(200)
                            await search_input.press("Enter")
                            await self.page.wait_for_timeout(1200)
                            try:
                                await options.first.wait_for(state="visible", timeout=5000)
                            except Exception:
                                pass
                        except Exception:
                            pass

                if (await options.count()) == 0:
                    words = re.findall(r'[a-zA-Z]{3,}', option_text)
                    keyword_fallback = max(words, key=len) if words else ""
                    if keyword_fallback and keyword_fallback.upper() != option_text.upper():
                        print(f"  -> No options found for '{clean_search}'. Retrying customer search with keyword: '{keyword_fallback}'...")
                        try:
                            await search_input.focus()
                            await search_input.fill("")
                            await search_input.fill(keyword_fallback)
                            await self.page.wait_for_timeout(200)
                            await search_input.press("Enter")
                            await self.page.wait_for_timeout(1200)
                            try:
                                await options.first.wait_for(state="visible", timeout=5000)
                            except Exception:
                                pass
                        except Exception:
                            pass
            else:
                if (await options.count()) == 0:
                    if clean_core and clean_core != option_text:
                        try:
                            await search_input.focus()
                            await search_input.fill("")
                            await search_input.fill(clean_core)
                            await self.page.wait_for_timeout(250)
                            try:
                                await options.first.wait_for(state="visible", timeout=2000)
                            except Exception:
                                pass
                        except Exception:
                            pass
                    if (await options.count()) == 0 and len(name_variants) > 1:
                        for fallback_var in name_variants[1:]:
                            if (await options.count()) > 0:
                                break
                            try:
                                await search_input.focus()
                                await search_input.fill("")
                                await search_input.fill(fallback_var)
                                await self.page.wait_for_timeout(250)
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

            async def _exact_match_option(opt_el) -> bool:
                raw_text = (await opt_el.text_content() or "").strip()
                if not raw_text:
                    return False
                s_raw = raw_text.strip()
                first_line = [l.strip() for l in s_raw.splitlines() if l.strip()][0] if s_raw else ""
                norm_first = unicodedata.normalize('NFKD', first_line).strip().lower()
                c_first = self.canonical_str(first_line)
                c_raw = self.canonical_str(s_raw)

                if is_time_target:
                    t_words = set(re.findall(r'\b[a-zA-Z0-9]+\b', raw_target.lower()))
                    o_words = set(re.findall(r'\b[a-zA-Z0-9]+\b', s_raw.lower()))
                    if ('am' in t_words) != ('am' in o_words) or ('pm' in t_words) != ('pm' in o_words):
                        return False

                sel_has_banner = self.has_banner_prefix(s_raw) or self.has_banner_prefix(first_line)
                if target_has_banner != sel_has_banner:
                    if not self.is_same_equipment(raw_target, s_raw):
                        return False

                if self.is_same_equipment(raw_target, s_raw) or self.is_same_equipment(raw_target, first_line):
                    await opt_el.scroll_into_view_if_needed()
                    await opt_el.click(force=True)
                    return True

                if is_tech:
                    if self.is_tech_match(s_raw, option_text) or self.is_tech_match(first_line, option_text):
                        await opt_el.scroll_into_view_if_needed()
                        await opt_el.click(force=True)
                        return True

                # Strict exact text match (on first line or full option text - NO partial match):
                if (
                    s_raw.lower() == raw_target.lower() or
                    first_line.lower() == raw_target.lower() or
                    first_line in name_variants or
                    s_raw in name_variants or
                    norm_first == target_norm or
                    norm_first in target_norms or
                    c_first == target_canon or
                    c_raw == target_canon or
                    c_first in target_canons or
                    c_raw in target_canons
                ):
                    await opt_el.scroll_into_view_if_needed()
                    await opt_el.click(force=True)
                    return True

                return False

            # 1. Scan currently loaded options for exact match:
            for idx_opt in range(count):
                if await _exact_match_option(options.nth(idx_opt)):
                    clicked = True
                    break

            # 2. If not clicked, scroll down the dropdown menu container to search all items (down to bottom):
            if not clicked and count > 0:
                try:
                    menu_container = dropdown.locator("ul.vs__dropdown-menu, ul[role='listbox']").first
                    if await menu_container.count() > 0:
                        for scroll_step in range(6):
                            await self.page.evaluate("""(menu) => { if (menu) menu.scrollTop += 350; }""", await menu_container.element_handle())
                            await self.page.wait_for_timeout(200)
                            count_now = await options.count()
                            for idx_opt in range(count_now):
                                if await _exact_match_option(options.nth(idx_opt)):
                                    clicked = True
                                    break
                            if clicked:
                                break
                except Exception:
                    pass

            # 3. For customer selection, strictly ABORT if no exact match was found (NEVER partial match another customer):
            if not clicked and is_customer:
                print(f"❌ Could not find exact match for Customer '{option_text}' in search results. Aborting to prevent wrong customer assignment.")
                return False

            # 4. Fallback for non-customer dropdowns only (e.g. Task, Priority):
            if not clicked and not is_customer and not is_tech:
                async def _fuzzy_match_option(opt_el) -> bool:
                    raw_text = (await opt_el.text_content() or "").strip()
                    if not raw_text:
                        return False
                    first_line = [l.strip() for l in raw_text.splitlines() if l.strip()][0] if raw_text else ""
                    norm_first = unicodedata.normalize('NFKD', first_line).strip().lower()
                    c_first = self.canonical_str(first_line)
                    c_raw = self.canonical_str(raw_text)

                    if is_time_target:
                        t_words = set(re.findall(r'\b[a-zA-Z0-9]+\b', raw_target.lower()))
                        o_words = set(re.findall(r'\b[a-zA-Z0-9]+\b', raw_text.lower()))
                        if ('am' in t_words) != ('am' in o_words) or ('pm' in t_words) != ('pm' in o_words):
                            return False

                    if (c_first and c_first in target_canons) or (c_raw and c_raw in target_canons):
                        await opt_el.scroll_into_view_if_needed()
                        await opt_el.click(force=True)
                        return True

                    if target_canon and (target_canon == c_raw or target_canon == c_first):
                        await opt_el.scroll_into_view_if_needed()
                        await opt_el.click(force=True)
                        return True

                    if self.is_same_equipment(option_text, raw_text):
                        await opt_el.scroll_into_view_if_needed()
                        await opt_el.click(force=True)
                        return True

                    return False

                for idx_opt in range(await options.count()):
                    if await _fuzzy_match_option(options.nth(idx_opt)):
                        clicked = True
                        break

            if not clicked:
                print(f"⚠️ Could not find exact or canonical match for option '{option_text}' in dropdown ({dropdown_selector}). Aborting selection to prevent misassignment.")
                return False

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
                    t_words = set(re.findall(r'\b[a-zA-Z0-9]+\b', option_text.lower()))
                    s_words = set(re.findall(r'\b[a-zA-Z0-9]+\b', selected_val.lower()))
                    qualifier_conflict = is_time_target and ((('am' in t_words) != ('am' in s_words)) or (('pm' in t_words) != ('pm' in s_words)))
                    sel_has_banner = self.has_banner_prefix(selected_val)
                    banner_conflict = (target_has_banner != sel_has_banner)
                    if not qualifier_conflict and not banner_conflict:
                        if (
                            selected_val == option_text or
                            clean_sel == clean_opt or
                            clean_sel in target_canons or
                            selected_val.strip() in name_variants or
                            any(v.lower() == selected_val.strip().lower() for v in name_variants) or
                            selected_val.strip().lower() == option_text.strip().lower() or
                            self.is_same_equipment(selected_val, option_text) or
                            (is_tech and self.is_tech_match(selected_val, option_text))
                        ):
                            is_verified = True
                            break
                await self.page.wait_for_timeout(150)

            if not is_verified:
                print(f"❌ Selection verification failed: expected '{option_text}', but dropdown selected '{selected_val}'. Aborting selection.")
                clear_btn = dropdown.locator(".vs__clear, button.close").first
                if await clear_btn.count() > 0 and await clear_btn.is_visible():
                    await clear_btn.click(force=True)
                return False

            await self.page.wait_for_timeout(200)
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
        try:
            opened = await self.open_create_wo_modal()
            if not opened:
                return False

            await self.page.wait_for_timeout(300)

            print(f"  [1/7] Customer: '{customer_name}'")
            customer_xpath = self.rules.get('dispatch_board_customer_xpath', "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Customer Name')]/following::div[contains(@class,'v-select')][1]")
            success = await self.select_dropdown_option(customer_xpath, customer_name, press_enter=True, is_customer=True)
            if not success:
                print(f"⚠️ Failed to select Customer '{customer_name}'. Aborting.")
                return False

            print(f"  -> Waiting for customer data and background spinners to finish...")
            import time
            _load_start = time.time()
            _max_load_wait = 45
            _form_ready = False

            await self.page.wait_for_timeout(300)

            while (time.time() - _load_start) < _max_load_wait:
                try:
                    spinner_loc = self.page.locator(".spinner:visible, .vs__spinner:visible, .loading:visible, .p-progress-spinner:visible, .loading-mask:visible, .vs--loading:visible")
                    active_spinners = await spinner_loc.count()

                    task_ready = self.page.locator(
                        self.rules.get('dispatch_board_task_xpath',
                            "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Task') and not(contains(normalize-space(),'Duration'))]/following::div[contains(@class,'v-select')][1]")
                    ).first
                    task_vis = await task_ready.is_visible()
                    task_class = (await task_ready.get_attribute("class") or "") if task_vis else ""
                    task_disabled = "vs--disabled" in task_class or "disabled" in task_class

                    dialog_confirm = self.page.locator(
                        "//div[contains(@class,'v-dialog') or contains(@class,'modal') or contains(@class,'p-dialog')]//button[contains(@class,'confirm') or contains(.,'Confirm') or contains(.,'OK') or contains(.,'Yes') or contains(.,'Select')]"
                    ).first
                    if await dialog_confirm.count() > 0 and await dialog_confirm.is_visible():
                        print("  -> Dismissing customer dialog/alert with Confirm button...")
                        await dialog_confirm.click(force=True)
                        await self.page.wait_for_timeout(300)
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

                await self.page.wait_for_timeout(200)

            if not _form_ready:
                print(f"  ❌ Form not ready after {_max_load_wait}s (Customer '{customer_name}' data did not load or spinners hung). Aborting safely.")
                await self.close_create_wo_modal()
                return False

            await self.page.wait_for_timeout(350)

            print(f"  [2/7] Immediate Action: '{immediate_action}'")
            ia_xpath = self.rules.get(
                'dispatch_board_immediate_action_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Immediate Action')]/following::div[contains(@class,'v-select')][1]"
            )
            await self.select_dropdown_option(ia_xpath, immediate_action, press_enter=False)
            await self.page.wait_for_timeout(200)

            print(f"  [3/7] Task: '{task}'")
            task_xpath = self.rules.get(
                'dispatch_board_task_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Task') and not(contains(normalize-space(),'Duration'))]/following::div[contains(@class,'v-select')][1]"
            )
            await self.select_dropdown_option(task_xpath, task, press_enter=False)
            await self.page.wait_for_timeout(200)

            print(f"  [4/7] Lead Source: '{lead_source}'")
            lead_xpath = self.rules.get(
                'dispatch_board_lead_source_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Work Order Lead Source')]/following::div[contains(@class,'v-select')][1]"
            )
            await self.select_dropdown_option(lead_xpath, lead_source, press_enter=False)
            await self.page.wait_for_timeout(200)

            print(f"  [5/7] Priority: '{priority}'")
            priority_xpath = self.rules.get(
                'dispatch_board_priority_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Priority')]/following::div[contains(@class,'v-select')][1]"
            )
            await self.select_dropdown_option(priority_xpath, priority, press_enter=False)
            await self.page.wait_for_timeout(200)

            if tech_name:
                print(f"  [6/7] Primary Tech: '{tech_name}'")
                tech_xpath = self.rules.get(
                    'dispatch_board_tech_name_xpath',
                    "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Primary Tech')]/following::div[contains(@class,'v-select')][1]"
                )
                tech_selected = await self.select_dropdown_option(tech_xpath, tech_name, press_enter=True, is_tech=True)
                if not tech_selected:
                    print(f"❌ Failed to match and select expected technician '{tech_name}'. Aborting work order to prevent wrong/duplicate assignment.")
                    await self.close_create_wo_modal()
                    return False

                tech_container = self.page.locator(tech_xpath).first
                selected_tech_text = (await tech_container.locator(".vs__selected").text_content() or "").strip()
                clean_actual_tech = self.canonical_str(selected_tech_text)
                clean_target_tech = self.canonical_str(tech_name)
                tech_verified = (
                    selected_tech_text == tech_name or
                    clean_actual_tech == clean_target_tech or
                    clean_actual_tech.startswith(clean_target_tech) or
                    (len(clean_actual_tech) >= 4 and len(clean_target_tech) >= 4 and (clean_actual_tech in clean_target_tech or clean_target_tech in clean_actual_tech)) or
                    self.is_tech_match(selected_tech_text, tech_name)
                )
                if not tech_verified:
                    print(f"🛑 STRICT SAFETY STOP: Primary Tech modal value is '{selected_tech_text}', but expected '{tech_name}'! Aborting work order to prevent misassignment.")
                    await self.close_create_wo_modal()
                    return False

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
                        await self.page.wait_for_timeout(150)
                except Exception:
                    await self.page.wait_for_timeout(200)

            print(f"  [7/7] Date: {target_date} | Time: {start_time} | Duration: {duration}")
            try:
                date_input = self.page.locator(
                    self.rules.get('dispatch_board_date_input_xpath', "//input[@name='Start Date']")
                ).first
                try:
                    await date_input.wait_for(state="visible", timeout=3000)
                    for _ in range(10):
                        is_disabled = await date_input.get_attribute("disabled")
                        if is_disabled is None:
                            break
                        await self.page.wait_for_timeout(150)
                except Exception:
                    pass

                if await date_input.is_visible(timeout=1000):
                    await date_input.click(force=True)
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                    await date_input.type(target_date, delay=30)
                    await self.page.keyboard.press("Enter")
                    await date_input.evaluate("el => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }")
                    await self.page.keyboard.press("Tab")
                    await self.page.wait_for_timeout(150)

                time_input = self.page.locator(
                    self.rules.get('dispatch_board_time_input_xpath',
                        "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Start Time')]/following::input[1]")
                ).first
                if await time_input.is_visible(timeout=1500) and not await time_input.get_attribute("disabled"):
                    await time_input.click(force=True)
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                    await time_input.type(start_time, delay=30)
                    await self.page.keyboard.press("Enter")
                    await time_input.evaluate(f"el => {{ el.value = '{start_time}'; el.dispatchEvent(new Event('input', {{ bubbles: true }})); el.dispatchEvent(new Event('change', {{ bubbles: true }})); }}")
                    await self.page.keyboard.press("Tab")
                    await self.page.wait_for_timeout(150)

                    try:
                        actual_start_time = await time_input.input_value()
                        t_exp = datetime.strptime(start_time.strip().upper(), "%I:%M %p")
                        t_act = datetime.strptime(actual_start_time.strip().upper(), "%I:%M %p") if actual_start_time else None
                        if t_act and t_exp != t_act:
                            print(f"  ⚠️ FieldEdge auto-adjusted Start Time to '{actual_start_time}'. Re-forcing '{start_time}'...")
                            await time_input.click(force=True)
                            await self.page.keyboard.press("Control+A")
                            await self.page.keyboard.press("Backspace")
                            await time_input.type(start_time, delay=30)
                            await self.page.keyboard.press("Enter")
                            await time_input.evaluate(f"el => {{ el.value = '{start_time}'; el.dispatchEvent(new Event('input', {{ bubbles: true }})); el.dispatchEvent(new Event('change', {{ bubbles: true }})); }}")
                            await self.page.keyboard.press("Tab")
                            await self.page.wait_for_timeout(150)
                    except Exception:
                        pass

                duration_input = self.page.locator(
                    self.rules.get('dispatch_board_duration_input_xpath',
                        "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Task Duration')]/following::input[contains(@class,'base-input')][1]")
                ).first
                if await duration_input.is_visible(timeout=1500) and not await duration_input.get_attribute("disabled"):
                    fmt_dur = ("0" + duration) if len(duration) == 4 and ":" in duration else duration
                    await duration_input.click(force=True)
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                    await duration_input.type(fmt_dur, delay=30)
                    await self.page.keyboard.press("Enter")
                    await duration_input.evaluate(f"el => {{ el.value = '{fmt_dur}'; el.dispatchEvent(new Event('input', {{ bubbles: true }})); el.dispatchEvent(new Event('change', {{ bubbles: true }})); }}")
                    await self.page.keyboard.press("Tab")
                    await self.page.wait_for_timeout(150)

            except Exception as e:
                print(f"  Note on Date/Time/Duration fields: {e}")

            if summary_note:
                print(f"  📝 Entering Work Order Summary: '{summary_note[:40]}...'")
                try:
                    filled_note = await self.page.evaluate("""(note) => {
                        const containers = document.querySelectorAll('.summary-note-container_f6fBE, .field-container, .form-group');
                        for (const c of containers) {
                            const title = c.querySelector('.field-title, label, .title');
                            if (title && (title.textContent.toLowerCase().includes('work summary') || 
                                         (title.textContent.toLowerCase().includes('summary') && !title.textContent.toLowerCase().includes('customer')))) {
                                const ta = c.querySelector('textarea, .text-area_he5R6');
                                if (ta) {
                                    ta.focus();
                                    ta.value = note;
                                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                                    ta.dispatchEvent(new Event('change', { bubbles: true }));
                                    return true;
                                }
                            }
                        }
                        const allTextareas = document.querySelectorAll('.on-bottom-modal textarea, .v-dialog textarea, .modal textarea, .text-area_he5R6');
                        if (allTextareas.length >= 2) {
                            const ta = allTextareas[1];
                            ta.focus();
                            ta.value = note;
                            ta.dispatchEvent(new Event('input', { bubbles: true }));
                            ta.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        } else if (allTextareas.length === 1) {
                            const ta = allTextareas[0];
                            ta.focus();
                            ta.value = note;
                            ta.dispatchEvent(new Event('input', { bubbles: true }));
                            ta.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                        return false;
                    }""", summary_note)

                    if not filled_note:
                        for sel in [
                            '.summary-note-container_f6fBE:has-text("Work Summary") textarea',
                            'div:has-text("Work Summary") + div textarea',
                            'textarea[name="WorkSummary"]',
                            '.text-area_he5R6'
                        ]:
                            ta_loc = self.page.locator(sel).first
                            if await ta_loc.count() > 0 and await ta_loc.is_visible():
                                await ta_loc.fill(summary_note)
                                await ta_loc.evaluate("el => { el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true })); }")
                                break

                    await self.page.wait_for_timeout(150)
                except Exception as note_err:
                    print(f"  ⚠️ Warning setting Work Order Summary note: {note_err}")

            try:
                await self.page.evaluate("() => { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); }")
            except Exception:
                pass

            if tech_name:
                final_tech = (await self.page.locator(tech_xpath).locator(".vs__selected").text_content() or "").strip()
                clean_f = self.canonical_str(final_tech)
                clean_t = self.canonical_str(tech_name)
                final_verified = (
                    final_tech == tech_name or
                    clean_f == clean_t or
                    clean_f.startswith(clean_t) or
                    (len(clean_f) >= 4 and len(clean_t) >= 4 and (clean_f in clean_t or clean_t in clean_f)) or
                    self.is_tech_match(final_tech, tech_name)
                )
                if not final_verified:
                    print(f"🛑 FINAL PRE-SAVE CHECK FAILED: Primary Tech '{final_tech}' != expected '{tech_name}'. Aborting save.")
                    await self.close_create_wo_modal()
                    return False

            # --- FINAL PRE-SAVE GUARD: CUSTOMER NAME & BANNER/EMOJI VERIFICATION ---
            final_cust = (await self.page.locator(customer_xpath).locator(".vs__selected, .selected-tag").first.text_content() or "").strip()
            clean_cust = self.canonical_str(final_cust)
            clean_target = self.canonical_str(customer_name)
            cust_name_variants = self.extract_name_variants(customer_name)
            cust_target_canons = set(self.canonical_str(v) for v in cust_name_variants if self.canonical_str(v))

            target_has_banner = self.has_banner_prefix(customer_name)
            sel_has_banner = self.has_banner_prefix(final_cust)
            banner_conflict = (target_has_banner != sel_has_banner)

            is_time_cust = bool(re.search(r'\b\d{1,2}:\d{2}\b', customer_name)) or any(w in ('am', 'pm') for w in re.findall(r'\b[a-zA-Z0-9]+\b', customer_name.lower()))
            qualifier_conflict = False
            if is_time_cust:
                t_words = set(re.findall(r'\b[a-zA-Z0-9]+\b', customer_name.lower()))
                s_words = set(re.findall(r'\b[a-zA-Z0-9]+\b', final_cust.lower()))
                qualifier_conflict = ((('am' in t_words) != ('am' in s_words)) or (('pm' in t_words) != ('pm' in s_words)))

            name_matched = (
                final_cust == customer_name or
                clean_cust == clean_target or
                clean_cust in cust_target_canons or
                final_cust in cust_name_variants or
                any(v.lower() == final_cust.lower() for v in cust_name_variants) or
                self.is_same_equipment(final_cust, customer_name)
            )

            if banner_conflict or qualifier_conflict or not name_matched:
                reason = "banner/emoji mismatch" if banner_conflict else ("time qualifier conflict" if qualifier_conflict else "name mismatch")
                print(f"🛑 FINAL PRE-SAVE CHECK FAILED: Customer '{final_cust}' ({reason}) != expected '{customer_name}' (target_banner={target_has_banner}, sel_banner={sel_has_banner}). Aborting save to prevent invalid work order.")
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
            await self.page.wait_for_timeout(250)

            for _ in range(25):
                try:
                    is_disabled = await save_btn.get_attribute("disabled")
                    btn_class = await save_btn.get_attribute("class") or ""
                    aria_disabled = await save_btn.get_attribute("aria-disabled")
                    if is_disabled is None and "disabled" not in btn_class and aria_disabled != "true":
                        break
                except Exception:
                    pass
                await self.page.wait_for_timeout(150)

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

            customer_xpath_val = self.rules.get(
                'dispatch_board_customer_xpath',
                "//div[@class[contains(.,'field-title')] and contains(normalize-space(),'Customer Name')]/following::div[contains(@class,'v-select')][1]"
            )
            customer_field = self.page.locator(customer_xpath_val).first

            print("  -> Waiting for Save to process (modal to close)...")
            modal_closed = False
            _save_start = time.time()
            _last_reclick = _save_start

            await self.page.wait_for_timeout(350)

            while time.time() - _save_start < 30:
                current_url = self.page.url
                if "DispatchSummary" in current_url:
                    modal_closed = True
                    break

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
                                await self.page.wait_for_timeout(300)
                                break
                    except Exception:
                        pass

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

                if not save_btn_visible and not customer_visible and not modal_container_visible:
                    modal_closed = True
                    elapsed = round(time.time() - _save_start, 1)
                    print(f"  -> Save processed successfully (modal closed in {elapsed}s).")
                    break

                if (time.time() - _last_reclick) >= 5.0:
                    _last_reclick = time.time()
                    print("  -> Modal still open, re-clicking Save button...")
                    await _trigger_save_click()

                await self.page.wait_for_timeout(200)

            if not modal_closed:
                print(f"  ❌ Modal did not close after 30s for '{customer_name}'. Work order save was not confirmed!")
                print("  -> Closing stuck modal to ensure subsequent work orders are not blocked...")
                await self.close_create_wo_modal()
                return False

            await self.page.wait_for_timeout(400)

            if mark_completed:
                print("  -> Waiting for DispatchSummary Save button...")
                summary_completed = False
                try:
                    summary_save_btn = self.page.locator("button#dispatch-summary-save-workorder").first

                    try:
                        await summary_save_btn.wait_for(state="visible", timeout=20000)
                    except Exception:
                        current_url = self.page.url
                        print(f"  -> DispatchSummary button not found in time. Current URL: {current_url}")
                        if "DispatchSummary" in current_url or "dispatch-summary" in current_url.lower():
                            print("  -> Detected DispatchSummary URL. Retrying button search...")
                            await self.page.wait_for_timeout(600)
                            await summary_save_btn.wait_for(state="visible", timeout=10000)
                        else:
                            if immediate_action == "Complete Work Order":
                                print(f"🎉 WO '{customer_name}' auto-completed directly via Immediate Action (no redirect needed).")
                                summary_completed = True
                            else:
                                print(f"  ⚠️ DispatchSummary not opened for '{customer_name}' "
                                      f"(task_type='{task_type or task}'). "
                                      f"WO created but not auto-completed. Skipping completion step.")
                                raise Exception("DispatchSummary page not reached")

                    if not summary_completed:
                        try:
                            await self.page.keyboard.press("Tab")
                            await self.page.wait_for_timeout(150)
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

                            await self.page.wait_for_timeout(200)

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
                                await self.page.wait_for_timeout(250)

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
                                        await self.page.wait_for_timeout(200)
                                    except Exception:
                                        pass

                                print("  -> Selected 'Completed' status. Giving Vue 1.5s to update...")
                                await self.page.wait_for_timeout(350)
                            except Exception as radio_err:
                                print(f"  -> Radio selection note: {radio_err}")

                            print("  -> Clicking Confirm button in modal...")
                            
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
                            await self.page.wait_for_timeout(300)
                            try:
                                await confirm_btn.wait_for(state="hidden", timeout=10000)
                                print("  -> Modal closed. Save confirmed!")
                            except Exception:
                                print("  -> Modal close timeout — proceeding anyway.")
                            summary_completed = True
                            print(f"🎉 Active Tech WO '{customer_name}' saved & completed!")
                        else:
                            summary_completed = True
                            print(f"🎉 Active Tech WO '{customer_name}' saved (no status modal)!")
                except Exception as summary_err:
                    if not summary_completed:
                        print(f"  -> DispatchSummary flow note for '{customer_name}': {summary_err}")
            else:
                print(f"🎉 Static WO '{customer_name}' saved!")

            print(f"  Returning to Dispatch Board (fresh load)...")
            await self.ensure_clean_dispatch_board(force_reload=True)
            return True

        except Exception as err:
            print(f"❌ Error in fill_and_save_work_order for '{customer_name}': {err}")
            return False

    async def check_and_handle_session(self, on_progress: Optional[Callable] = None) -> bool:
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

        self.template_data = self._load_template()

        settings = self.template_data.get("general_settings", {})
        task = settings.get("task", "8 - INTERNAL")
        lead_source = settings.get("lead_source", "Sterling Septic -FE")
        immediate_action = settings.get("immediate_action", "Create Work Order")
        skip_vehicle_on_weekends = settings.get("skip_vehicle_work_orders_on_weekends", True)

        truck_assign_headers = [
            h for h in self.template_data.get("truck_assign_headers", [])
            if h.get("active", True)
            and (h.get("tech_name") or "").strip()
            and (h.get("tech_name") or "").strip().lower() not in ("unassigned", "none", "clear")
        ]

        static_headers = [
            h for h in self.template_data.get("static_headers", [])
            if h.get("active", True)
        ]

        out_of_service_headers = []
        oos_canonical_names = set()
        oos_asset_numbers = set()

        for oos in self.template_data.get("out_of_service_assets", []):
            if not oos.get("active", True):
                continue
            oos_name = (oos.get("asset_name") or "").strip()
            if not oos_name:
                continue
            c_n = self.canonical_str(oos_name)
            if c_n:
                oos_canonical_names.add(c_n)
            after_pipe = oos_name.split("|")[-1].strip() if "|" in oos_name else oos_name.strip()
            if re.match(r'^\d{2,4}$', after_pipe):
                oos_asset_numbers.add(after_pipe)

            note = oos.get("service_note") or oos.get("reason") or ""
            out_of_service_headers.append({
                "name": oos_name,
                "start_time": oos.get("start_time") or "1:45 PM",
                "duration": oos.get("duration") or "1:30",
                "priority": oos.get("priority") or "[4] DO NOT DISPATCH (Tracking)",
                "tech_name": oos.get("tech_name") or "s4",
                "service_note": note,
                "reason": note,
                "is_out_of_service": True,
                "active": True,
                "task": oos.get("task", task),
            })

        def _is_oos_asset(header_item):
            h_name = header_item.get("name", "")
            h_asset = header_item.get("asset_name", "")
            for check_str in (h_name, h_asset):
                if not check_str:
                    continue
                if self.canonical_str(check_str) in oos_canonical_names:
                    return True
                after_p = check_str.split("|")[-1].strip() if "|" in check_str else check_str.strip()
                if re.match(r'^\d{2,4}$', after_p) and after_p in oos_asset_numbers:
                    is_h_truck = ("🛻" in check_str or "🚛" in check_str)
                    if is_h_truck:
                        return True
            return False

        if oos_canonical_names:
            static_headers = [h for h in static_headers if not _is_oos_asset(h)]
            truck_assign_headers = [h for h in truck_assign_headers if not _is_oos_asset(h)]
            print(f"🚫 Exempted {len(oos_canonical_names)} Out of Service vehicle(s) from standard categories: {', '.join([s['name'] for s in out_of_service_headers])}")

        service_needed_headers = []
        sn_canonical_names = set()
        sn_asset_numbers = set()
        sn_map = {}

        for sn in self.template_data.get("service_needed_assets", []):
            if not sn.get("active", True):
                continue
            sn_name = (sn.get("asset_name") or "").strip()
            if not sn_name:
                continue
            if self.canonical_str(sn_name) in oos_canonical_names:
                continue

            c_n = self.canonical_str(sn_name)
            sn_canonical_names.add(c_n)
            sn_map[c_n] = sn
            after_pipe = sn_name.split("|")[-1].strip() if "|" in sn_name else sn_name.strip()
            if re.match(r'^\d{2,4}$', after_pipe):
                sn_asset_numbers.add(after_pipe)
                sn_map[after_pipe] = sn

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

        def _is_sn_asset(header_item):
            h_name = header_item.get("name", "")
            h_asset = header_item.get("asset_name", "")
            for check_str in (h_name, h_asset):
                if not check_str:
                    continue
                if self.canonical_str(check_str) in sn_canonical_names:
                    return True
                after_p = check_str.split("|")[-1].strip() if "|" in check_str else check_str.strip()
                if re.match(r'^\d{2,4}$', after_p) and after_p in sn_asset_numbers:
                    is_h_truck = ("🛻" in check_str or "🚛" in check_str)
                    if is_h_truck:
                        return True
            return False

        if sn_canonical_names:
            static_headers = [h for h in static_headers if not _is_sn_asset(h)]
            print(f"🔧 Exempted {len(sn_canonical_names)} Service Needed vehicle(s) from morning static categories: {', '.join([s['name'] for s in service_needed_headers])}")

        # Match truck_assign_headers with service_needed_assets
        # If a truck is assigned to a technician AND has Service Needed:
        # Keep it on the technician at their scheduled time, attach note,
        # AND ALSO create the Service Needed card under '🔧 SERVICE NEEDED' at 3:30 PM!
        for h in truck_assign_headers:
            h_name = h.get("name", "")
            c_h = self.canonical_str(h_name)
            after_p = h_name.split("|")[-1].strip() if "|" in h_name else h_name.strip()
            sn_obj = sn_map.get(c_h) or (sn_map.get(after_p) if re.match(r'^\d{2,4}$', after_p) else None)

            h_tech = (h.get("tech_name") or "").strip()
            is_assigned = h.get("active", True) and h_tech and h_tech.lower() not in ("unassigned", "none", "clear")

            if sn_obj and is_assigned:
                h["service_note"] = sn_obj.get("service_note") or ""
                h["summary_note"] = sn_obj.get("service_note") or ""
                h["is_service_needed"] = True
                print(f"🔧 Truck '{h_name}' on tech '{h_tech}' marked with Service Needed note at {h.get('start_time')}.")

        # Service Needed assets are placed under '🔧 SERVICE NEEDED' at 3:30 PM on s4
        if service_needed_headers:
            static_headers = static_headers + service_needed_headers
            print(f"🔧 Added {len(service_needed_headers)} Service Needed header(s) under 3:30 PM.")
        tech_defaults = self.template_data.get("technician_display_defaults", {})
        tech_jobs = self.template_data.get("tech_jobs", [])

        tech_priorities = {}
        tech_metadata = {}

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
            
            self.intercepted_board_by_date.clear()
            self.board_req.clear()
            
            async def handle_request(request):
                if "GetDispatchBoardData" in request.url and request.method == "POST":
                    self.board_req["url"] = request.url
                    self.board_req["headers"] = request.headers
                    self.board_req["post_data"] = request.post_data
                    
            self.page.on("request", handle_request)
            
            # Response interception removed per user request. Board data is fetched on-demand via fetch_board_data_for_date.
            
            
            dispatch_url = self.rules.get('web_url', 'https://login.fieldedge.com/Dispatch')
            print(f"Navigating to Dispatch Board: {dispatch_url}")
            await self.page.goto(dispatch_url, wait_until='domcontentloaded')
            
            if "Login" in self.page.url:
                await self.login_fieldedge()

            print("Waiting for Dispatch Board UI to become interactable...")
            try:
                xpath = self.rules.get('dispatch_board_create_wo_btn_xpath')
                if xpath:
                    await self.page.wait_for_selector(xpath, state='visible', timeout=15000)
                    print("Dispatch Board loaded successfully!")
            except Exception:
                print("Note: Create Work Order button not immediately found, but continuing script...")
            
            print("⏳ Waiting for Dispatch Board JSON API data to intercept...")
            for _ in range(50):
                if self.board_req.get("url"):
                    break
                await self.page.wait_for_timeout(200)

            board_date_str = None
            existing_wos = {}

        created_history = self._load_history()
        print(f"📚 Loaded {len(created_history)} recorded entries from Central Dashboard API.")

        try:
            for d in range(days):
                current_date = start_dt + timedelta(days=d)
                date_str = f"{current_date.month}/{current_date.day}/{current_date.strftime('%y')}"
                db_date_key = current_date.strftime("%Y-%m-%d")
                is_weekend = current_date.weekday() >= 5

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
                        b_date, e_wos = await self.get_or_fetch_board_data_for_date(current_date)
                        if e_wos is None:
                            print(f"⛔ [SAFETY GUARD] FieldEdge Dispatch Board API data could NOT be verified for {db_date_key} ({date_str})!")
                            print(f"⛔ Skipping creation for date {db_date_key} to PREVENT DUPLICATE WORK ORDERS!")
                            continue
                        board_date_str = b_date or db_date_key
                        existing_wos = e_wos

                    for header in static_headers:
                        name = header.get("name")
                        start_time = header.get("start_time")
                        duration = header.get("duration")
                        priority = header.get("priority")
                        h_tech = header.get("tech_name")
                        h_task = header.get("task", task)

                        if not dry_run:
                            canon_target = self.canonical_str(name)
                            is_service_wo = bool(
                                header.get('is_service_needed_duplicate') or
                                (header.get('service_note') and ('SERVICE NEEDED' in str(header.get('priority', '')) or '7.12' in str(header.get('priority', ''))))
                            )
                            is_oos_wo = bool(
                                header.get('is_out_of_service') or
                                (header.get('reason') and 'DO NOT DISPATCH' in str(header.get('priority', '')))
                            )
                            if is_service_wo:
                                tracking_target = f"{canon_target}_serviceneeded"
                            elif is_oos_wo:
                                tracking_target = f"{canon_target}_outofservice"
                            else:
                                tracking_target = canon_target
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
                        
                            if not force and self.is_wo_created(created_history, db_date_key, canon_h_tech, tracking_target, target_count_idx, time_key=time_key):
                                print(f"⏭️ Header banner '{name}' (occurrence {target_count_idx} at {time_key}) was previously created and recorded in history. Skipping 2nd creation.")
                                continue

                            if is_service_wo:
                                sn_existing_found = False
                                for c_tk, w_list in existing_wos.items():
                                    for w in w_list:
                                        if not w.get("is_appointment") and self.check_item_match(w, canon_target, require_customer_match=True, match_any_status=True):
                                            w_time = self.normalize_time_key(w.get("time_key", ""))
                                            norm_target_time = self.normalize_time_key(time_key)
                                            is_sn_slot = bool(norm_target_time and w_time == norm_target_time)
                                            if w.get("is_service_needed") or is_sn_slot or c_tk in ("s4", "s3"):
                                                sn_existing_found = True
                                                break
                                    if sn_existing_found:
                                        break
                                
                                if sn_existing_found:
                                    print(f"⏭️ Service Needed card for asset '{name}' ALREADY exists at {time_key} on the board for {date_str}. Skipping 2nd creation.")
                                    self.record_wo_created(created_history, db_date_key, canon_h_tech, tracking_target, 1, status="existing_on_board", details={"name": name, "tech": h_tech, "time": time_key})
                                    continue

                            if is_oos_wo:
                                oos_existing_tech = None
                                for c_tk, w_list in existing_wos.items():
                                    for w in w_list:
                                        if self.check_item_match(w, canon_target, require_customer_match=True, match_any_status=True):
                                            oos_existing_tech = c_tk
                                            break
                                    if oos_existing_tech:
                                        break
                                
                                if oos_existing_tech:
                                    print(f"⏭️ Out of Service asset '{name}' ALREADY exists on the board (tech '{oos_existing_tech}') for {date_str}. Skipping new creation so Asset Scraper can re-assign/update it.")
                                    self.record_wo_created(created_history, db_date_key, canon_h_tech, tracking_target, 1, status="existing_on_board", details={"name": name, "tech": h_tech, "time": time_key})
                                    continue

                            if not is_service_wo and not is_oos_wo:
                                asset_already_oos = False
                                for c_tk, w_list in existing_wos.items():
                                    for w in w_list:
                                        if not w.get("is_appointment") and self.check_item_match(w, canon_target, require_customer_match=True, match_any_status=True):
                                            if w.get("is_out_of_service") and canon_target in oos_canonical_names:
                                                asset_already_oos = True
                                                break
                                    if asset_already_oos:
                                        break
                                if asset_already_oos:
                                    print(f"⏭️ Asset '{name}' is OUT OF SERVICE for {date_str}. Skipping standard static header creation.")
                                    continue

                            board_count = 0
                            for canon_tech_key, wo_list in existing_wos.items():
                                if canon_h_tech:
                                    tech_match = (
                                        canon_tech_key == canon_h_tech or
                                        canon_h_tech in canon_tech_key or
                                        canon_tech_key in canon_h_tech or
                                        self.is_tech_match(canon_h_tech, canon_tech_key)
                                    )
                                else:
                                    tech_match = not canon_tech_key

                                if tech_match:
                                    for wo in wo_list:
                                        if not wo.get("is_appointment"):
                                            is_dummy_tech = (
                                                canon_h_tech in DUMMY_HEADER_TECHS or
                                                any(canon_h_tech.startswith(dt) for dt in DUMMY_HEADER_TECHS)
                                            )
                                            if self.check_item_match(
                                                wo,
                                                canon_target,
                                                time_key,
                                                is_service_wo=is_service_wo,
                                                is_oos_wo=is_oos_wo,
                                                match_any_status=(is_service_wo or is_oos_wo or is_dummy_tech)
                                            ):
                                                board_count += 1
                                            elif is_dummy_tech and not is_service_wo and not is_oos_wo:
                                                # If this item name is unique on this technician column (only 1 occurrence in template),
                                                # count it even if scheduled time was slightly adjusted on the board, but NEVER match
                                                # cards sitting in Out of Service (1:45 PM) or Service Needed (3:30 PM) slots!
                                                w_time_norm = self.normalize_time_key(wo.get("time_key", ""))
                                                if w_time_norm not in ("1:45 PM", "3:30 PM", "1:45", "3:30"):
                                                    if sum(1 for h in static_headers if self.canonical_str(h.get("tech_name", "")) == canon_h_tech and self.canonical_str(h.get("name", "")) == canon_target) == 1:
                                                        if self.check_item_match(
                                                            wo,
                                                            canon_target,
                                                            time_key=None,
                                                            require_customer_match=True,
                                                            match_any_status=True
                                                        ):
                                                            board_count += 1
                                        else:
                                            appt_matched = False
                                            if wo.get("start_time_obj") and wo.get("end_time_obj") and time_key:
                                                try:
                                                    target_t = datetime.strptime(self.normalize_time_key(time_key), "%I:%M %p").time()
                                                    if wo["start_time_obj"] <= target_t <= wo["end_time_obj"]:
                                                        appt_matched = True
                                                except Exception:
                                                    pass
                                            if not appt_matched and wo.get("time_key") and time_key:
                                                if self.normalize_time_key(wo["time_key"]) == self.normalize_time_key(time_key):
                                                    appt_matched = True
                                            if appt_matched:
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
                            h_note = header.get("service_note") or header.get("reason")
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
                                summary_note=h_note
                            )
                            if not res:
                                print(f"  🔁 Attempt 1 aborted/failed for '{name}'. Retrying with fresh attempt in 3s...")
                                await self.close_create_wo_modal()
                                await self.page.wait_for_timeout(1000)
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
                                    summary_note=h_note
                                )

                            if res:
                                self.record_wo_created(created_history, db_date_key, canon_h_tech, tracking_target, target_count_idx, status="created", details={"name": name, "tech": h_tech, "time": time_key})
                                opt_wo = {
                                    "canon_customer": canon_target,
                                    "canon_strings": [canon_target],
                                    "canon_combined": canon_target,
                                    "time_key": time_key,
                                    "is_service_needed": is_service_wo,
                                    "is_out_of_service": is_oos_wo,
                                    "is_appointment": False
                                }
                                if canon_h_tech not in existing_wos:
                                    existing_wos[canon_h_tech] = []
                                existing_wos[canon_h_tech].append(opt_wo)

                                f_date, f_wos = await self.fetch_board_data_for_date(db_date_key, self.board_req)
                                if f_date == db_date_key and f_wos:
                                    for t_key, w_list in existing_wos.items():
                                        if t_key not in f_wos:
                                            f_wos[t_key] = w_list
                                        else:
                                            old_matching = [w for w in w_list if w.get("canon_customer") == canon_target]
                                            new_matching = [w for w in f_wos[t_key] if w.get("canon_customer") == canon_target]
                                            if len(old_matching) > len(new_matching):
                                                diff = len(old_matching) - len(new_matching)
                                                f_wos[t_key].extend(old_matching[:diff])
                                    board_date_str, existing_wos = f_date, f_wos

                    if skip_vehicle_on_weekends and current_date.weekday() >= 5:
                        print(f"⏭️ Skipping internal vehicle work orders (truck_assign_headers) for weekend date {date_str} ({current_date.strftime('%A')}).")
                    else:
                        for header in truck_assign_headers:
                            name = header.get("name")
                            start_time = header.get("start_time")
                            duration = header.get("duration")
                            priority = header.get("priority")
                            h_tech = (header.get("tech_name") or "").strip()
                            h_task = header.get("task", task)

                            # Guard: skip if unassigned or no valid technician
                            if not h_tech or h_tech.lower() in ("unassigned", "none", "clear") or not header.get("active", True):
                                print(f"⏭️ Skipping unassigned truck '{name}' (no technician assigned) from creation.")
                                continue

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
                            
                                if not force and self.is_wo_created(created_history, db_date_key, canon_h_tech, canon_target, target_count_idx, time_key=time_key):
                                    print(f"⏭️ Truck assign header '{name}' (occurrence {target_count_idx} at {time_key}) was previously created and recorded in history. Skipping 2nd creation.")
                                    continue

                                is_oos_asset = any(
                                    self.canonical_str(s.get("asset_name", "")) == canon_target
                                    for s in self.template_data.get("out_of_service_assets", [])
                                    if s.get("active", True)
                                )
                                if is_oos_asset:
                                    print(f"⏭️ Truck '{name}' is Out of Service for {date_str}. Skipping creating truck assign header on tech '{h_tech}'.")
                                    continue

                                truck_on_target_tech = False
                                found_tech_name = None
                                found_cust_name = None
                                found_time = None
                                if canon_h_tech in existing_wos:
                                    norm_target_time = self.normalize_time_key(time_key)
                                    for wo in existing_wos[canon_h_tech]:
                                        if not wo.get("is_appointment"):
                                            c_cust = wo.get("canon_customer", "")
                                            w_time = wo.get("time_key", "")
                                            wo_time_keys = wo.get("time_keys") or ({self.normalize_time_key(w_time)} if w_time else set())
                                            norm_wo_time = self.normalize_time_key(w_time)
                                            time_matched = not norm_target_time or not norm_wo_time or norm_target_time == norm_wo_time or (norm_target_time in wo_time_keys)
                                            if c_cust and (c_cust == canon_target or c_cust in (f"truck{canon_target}", f"tr{canon_target}")) and time_matched:
                                                truck_on_target_tech = True
                                                found_tech_name = canon_h_tech
                                                found_cust_name = c_cust
                                                found_time = w_time
                                                break

                                if truck_on_target_tech:
                                    print(f"⏭️ Truck '{name}' already exists on tech '{found_tech_name}' at {found_time} PST (matched: '{found_cust_name}') for {date_str}. Recording in history and skipping creation.")
                                    self.record_wo_created(created_history, db_date_key, canon_h_tech, canon_target, target_count_idx, status="existing_on_board", details={"name": name, "tech": h_tech, "time": time_key})
                                    continue

                                is_dummy_h_tech = (
                                    not canon_h_tech or
                                    canon_h_tech in DUMMY_HEADER_TECHS or
                                    any(canon_h_tech.startswith(dt) for dt in DUMMY_HEADER_TECHS)
                                )
                                if not is_dummy_h_tech and canon_h_tech in existing_wos:
                                    tech_already_has_truck = False
                                    for wo in existing_wos[canon_h_tech]:
                                        if wo.get("is_appointment"):
                                            continue
                                        c_cust = wo.get("canon_customer", "")
                                        if any(self.canonical_str(t.get("name", "")) == c_cust for t in truck_assign_headers):
                                            tech_already_has_truck = True
                                            break
                                    if tech_already_has_truck:
                                        print(f"⚠️ Tech '{h_tech}' already has a truck assigned on {date_str}. Skipping creating '{name}' to prevent multiple trucks on one technician.")
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
                                             if not wo.get("is_appointment"):
                                                 c_cust = wo.get("canon_customer", "")
                                                 w_time = wo.get("time_key", "")
                                                 wo_time_keys = wo.get("time_keys") or ({self.normalize_time_key(w_time)} if w_time else set())
                                                 name_match = c_cust == canon_target or canon_target in wo.get("canon_strings", [])
                                                 norm_target = self.normalize_time_key(time_key)
                                                 norm_wo = self.normalize_time_key(w_time)
                                                 time_match = not norm_target or not norm_wo or norm_target == norm_wo or (norm_target in wo_time_keys)
                                                 if name_match and time_match:
                                                     board_count += 1
                                             else:
                                                 appt_matched = False
                                                 if wo.get("start_time_obj") and wo.get("end_time_obj") and time_key:
                                                     try:
                                                         target_t = datetime.strptime(self.normalize_time_key(time_key), "%I:%M %p").time()
                                                         if wo["start_time_obj"] <= target_t <= wo["end_time_obj"]:
                                                             appt_matched = True
                                                     except Exception:
                                                         pass
                                                 if not appt_matched and wo.get("time_key") and time_key:
                                                     if self.normalize_time_key(wo["time_key"]) == self.normalize_time_key(time_key):
                                                         appt_matched = True
                                                 if appt_matched:
                                                     board_count += 1
                             
                                if board_count >= target_count_idx:
                                    print(f"⏭️ Truck assign header '{name}' (occurrence {target_count_idx} at {time_key}) already exists or tech has appointment at {time_key}. Recording in persistent history & skipping creation.")
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
                                    summary_note=header.get("service_note")
                                )
                                if res:
                                    self.record_wo_created(created_history, db_date_key, canon_h_tech, canon_target, target_count_idx, status="created", details={"name": name, "tech": h_tech, "time": time_key})
                                    opt_wo = {
                                        "canon_customer": canon_target,
                                        "canon_strings": [canon_target],
                                        "canon_combined": canon_target,
                                        "time_key": time_key,
                                        "is_service_needed": False,
                                        "is_out_of_service": False,
                                        "is_appointment": False
                                    }
                                    if canon_h_tech not in existing_wos:
                                        existing_wos[canon_h_tech] = []
                                    existing_wos[canon_h_tech].append(opt_wo)

                                    f_date, f_wos = await self.fetch_board_data_for_date(db_date_key, self.board_req)
                                    if f_date == db_date_key and f_wos:
                                        for t_key, w_list in existing_wos.items():
                                            if t_key not in f_wos:
                                                f_wos[t_key] = w_list
                                            else:
                                                old_matching = [w for w in w_list if w.get("canon_customer") == canon_target]
                                                new_matching = [w for w in f_wos[t_key] if w.get("canon_customer") == canon_target]
                                                if len(old_matching) > len(new_matching):
                                                    diff = len(old_matching) - len(new_matching)
                                                    f_wos[t_key].extend(old_matching[:diff])
                                        board_date_str, existing_wos = f_date, f_wos

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

                            if not force and self.is_wo_created(created_history, db_date_key, canon_t_tech, canon_target, target_count_idx, time_key=time_key):
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
                                         if not wo.get("is_appointment"):
                                             c_cust = wo.get("canon_customer", "")
                                             w_time = wo.get("time_key", "")
                                             wo_time_keys = wo.get("time_keys") or ({self.normalize_time_key(w_time)} if w_time else set())
                                             name_match = c_cust == canon_target or canon_target in wo.get("canon_strings", [])
                                             norm_target = self.normalize_time_key(time_key)
                                             norm_wo = self.normalize_time_key(w_time)
                                             time_match = not norm_target or not norm_wo or norm_target == norm_wo or (norm_target in wo_time_keys)
                                             if name_match and time_match:
                                                 board_count += 1
                                         else:
                                             appt_matched = False
                                             if wo.get("start_time_obj") and wo.get("end_time_obj") and time_key:
                                                 try:
                                                     target_t = datetime.strptime(self.normalize_time_key(time_key), "%I:%M %p").time()
                                                     if wo["start_time_obj"] <= target_t <= wo["end_time_obj"]:
                                                         appt_matched = True
                                                 except Exception:
                                                     pass
                                             if not appt_matched and wo.get("time_key") and time_key:
                                                 if self.normalize_time_key(wo["time_key"]) == self.normalize_time_key(time_key):
                                                     appt_matched = True
                                             if appt_matched:
                                                 board_count += 1

                            if board_count >= target_count_idx:
                                print(f"⏭️ Tech job '{name}' (occurrence {target_count_idx} at {time_key}) already exists or tech has appointment at {time_key}. Recording in persistent history & skipping creation.")
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
                                summary_note=job.get("service_note") or job.get("summary_note")
                            )
                            if res:
                                self.record_wo_created(created_history, db_date_key, canon_t_tech, canon_target, target_count_idx, status="created", details={"name": name, "tech": t_tech, "time": time_key})
                                opt_wo = {
                                    "canon_customer": canon_target,
                                    "canon_strings": [canon_target],
                                    "canon_combined": canon_target,
                                    "time_key": time_key,
                                    "is_service_needed": False,
                                    "is_out_of_service": False,
                                    "is_appointment": False
                                }
                                if canon_t_tech not in existing_wos:
                                    existing_wos[canon_t_tech] = []
                                existing_wos[canon_t_tech].append(opt_wo)

                                f_date, f_wos = await self.fetch_board_data_for_date(db_date_key, self.board_req)
                                if f_date == db_date_key and f_wos:
                                    for t_key, w_list in existing_wos.items():
                                        if t_key not in f_wos:
                                            f_wos[t_key] = w_list
                                        else:
                                            old_matching = [w for w in w_list if w.get("canon_customer") == canon_target]
                                            new_matching = [w for w in f_wos[t_key] if w.get("canon_customer") == canon_target]
                                            if len(old_matching) > len(new_matching):
                                                diff = len(old_matching) - len(new_matching)
                                                f_wos[t_key].extend(old_matching[:diff])
                                    board_date_str, existing_wos = f_date, f_wos

                    for tech in active_techs:
                        t_name = tech
                    
                        meta = tech_metadata.get(t_name) or tech_metadata.get(self.canonical_str(t_name)) or {}
                        start_time = meta.get("start_time") or tech_defaults.get("start_time")
                        duration = meta.get("duration") or tech_defaults.get("duration")
                        t_assignment_name = meta.get("tech_name", tech)
                        priority = tech_priorities.get(t_name) or tech_priorities.get(self.canonical_str(t_name)) or tech_defaults.get("priority")

                        if not dry_run:
                            canon_target = self.canonical_str(t_name)
                            canon_t_assignment = self.canonical_str(t_assignment_name or '')
                        
                            processed_key = (canon_t_assignment, canon_target)
                            template_processed_counts[processed_key] = template_processed_counts.get(processed_key, 0) + 1
                            target_count_idx = template_processed_counts[processed_key]

                            time_key = ""
                            if start_time:
                                try:
                                    t_dt = datetime.strptime(start_time.strip().upper(), "%I:%M %p")
                                    time_key = t_dt.strftime("%I:%M %p")
                                    if time_key.startswith("0"): time_key = time_key[1:]
                                except Exception:
                                    time_key = ""

                            if not force and self.is_wo_created(created_history, db_date_key, canon_t_assignment, canon_target, target_count_idx, time_key=time_key):
                                print(f"  -> Completed WO for tech '{t_name}' (occurrence {target_count_idx} at {time_key}) was previously created and recorded in history. Skipping 2nd creation.")
                                continue

                            board_count = 0
                            tech_off_for_day = False
                            for canon_tech_key, wo_list in existing_wos.items():
                                tech_match = (
                                    not canon_t_assignment or not canon_tech_key or
                                    canon_tech_key == canon_t_assignment or
                                    canon_t_assignment in canon_tech_key or
                                    canon_tech_key in canon_t_assignment or
                                    self.is_tech_match(canon_t_assignment, canon_tech_key)
                                )
                                if tech_match:
                                    for wo in wo_list:
                                        if wo.get("is_appointment"):
                                            appt_matched = False
                                            if wo.get("start_time_obj") and wo.get("end_time_obj") and time_key:
                                                try:
                                                    target_t = datetime.strptime(self.normalize_time_key(time_key), "%I:%M %p").time()
                                                    if wo["start_time_obj"] <= target_t <= wo["end_time_obj"]:
                                                        appt_matched = True
                                                except Exception:
                                                    pass
                                            if not appt_matched and wo.get("time_key") and time_key:
                                                if self.normalize_time_key(wo["time_key"]) == self.normalize_time_key(time_key):
                                                    appt_matched = True
                                            if appt_matched:
                                                tech_off_for_day = True
                                                break
                                            continue
                                        if self.check_item_match(wo, canon_target, time_key=time_key, require_customer_match=True, match_any_status=True):
                                            board_count += 1
                                    if tech_off_for_day:
                                        break
                                        
                            if tech_off_for_day or board_count >= target_count_idx:
                                print(f"  -> Completed WO for tech '{t_name}' (occurrence {target_count_idx} at {time_key}) skipped (tech has matching appointment or already on board). Recording in persistent history.")
                                self.record_wo_created(created_history, db_date_key, canon_t_assignment, canon_target, target_count_idx, status="existing_on_board", details={"name": t_name, "tech": t_assignment_name, "time": time_key})
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
                                self.record_wo_created(created_history, db_date_key, canon_t_assignment, canon_target, target_count_idx, status="created", details={"name": t_name, "tech": t_assignment_name, "time": time_key})
                                opt_wo = {
                                    "canon_customer": canon_target,
                                    "canon_strings": [canon_target],
                                    "canon_combined": canon_target,
                                    "time_key": time_key,
                                    "is_service_needed": False,
                                    "is_out_of_service": False,
                                    "is_appointment": False
                                }
                                if canon_t_assignment not in existing_wos:
                                    existing_wos[canon_t_assignment] = []
                                existing_wos[canon_t_assignment].append(opt_wo)

                                f_date, f_wos = await self.fetch_board_data_for_date(db_date_key, self.board_req)
                                if f_date == db_date_key and f_wos:
                                    for t_key, w_list in existing_wos.items():
                                        if t_key not in f_wos:
                                            f_wos[t_key] = w_list
                                        else:
                                            old_matching = [w for w in w_list if w.get("canon_customer") == canon_target]
                                            new_matching = [w for w in f_wos[t_key] if w.get("canon_customer") == canon_target]
                                            if len(old_matching) > len(new_matching):
                                                diff = len(old_matching) - len(new_matching)
                                                f_wos[t_key].extend(old_matching[:diff])
                                    board_date_str, existing_wos = f_date, f_wos

                if not dry_run:
                    _records_processed += 1

                    if not skip_asset:
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
                                    try:
                                        from automation.scrapers.asset_assignment_scraper import AssetAssignmentScraper  # type: ignore
                                    except ImportError:
                                        from dispatch.asset_assignment_scraper import AssetAssignmentScraper  # type: ignore
                                self.asset_scraper = AssetAssignmentScraper(template_path=self.template_path)

                            self.asset_scraper.playwright = self.playwright
                            self.asset_scraper.browser = self.browser
                            self.asset_scraper.context = getattr(self, 'context', None) or (self.page.context if self.page else None)
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

            _elapsed = time.time() - _start_time
            if not dry_run and hasattr(self, "log_finish"):
                await self.log_finish(
                    status="error" if _error_occurred else "success",
                    error_message=_error_occurred,
                    records_processed=_records_processed,
                    elapsed_seconds=_elapsed,
                    details=_details
                )
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
