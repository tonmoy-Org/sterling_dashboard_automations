"""
Asset Assignment Scraper & Reconciler
Maintains accurate truck-to-technician assignments, Out of Service vehicles,
and Service Needed asset tracking on the FieldEdge Dispatch Board.
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


class AssetAssignmentScraper(BaseScraper):
    """
    Automated scraper for reconciling asset assignments in FieldEdge.
    Ensures that fleet trucks, trailers, and excavators are accurately assigned to technicians,
    reconciles Out of Service and Service Needed cards, and cleans up duplicate cards.
    """

    def __init__(self, template_path: Optional[str] = None):
        super().__init__()
        self.scraper_name = "asset-assignment-automation"
        _dispatch_dir = os.path.dirname(os.path.abspath(__file__))

        if template_path:
            self.template_path = template_path
        else:
            cand1 = os.path.join(_dispatch_dir, "config", "dispatch_board_template.json")
            cand2 = os.path.join(os.path.dirname(_dispatch_dir), "config", "dispatch_board_template.json")
            self.template_path = cand1 if os.path.exists(cand1) else cand2

        self.template_data = self._load_template()
        self.history_file = os.path.join(_dispatch_dir, "reconciled_history.json")

    # --- RECONCILIATION HISTORY PERSISTENCE ---

    def _load_history(self) -> Dict:
        """Load persistent history of reconciled assignments."""
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def is_reconciled(self, history: Dict, date_key: str, truck_name: str) -> bool:
        c_truck = self.canonical_str(truck_name)
        key = f"{date_key}|{c_truck}"
        rec = history.get(key)
        return bool(isinstance(rec, dict) and rec.get("status") in ("reconciled", "skipped_match", "no_card_found"))

    def record_reconciled(
        self,
        history: Dict,
        date_key: str,
        truck_name: str,
        tech_name: str,
        status: str = "reconciled"
    ):
        c_truck = self.canonical_str(truck_name)
        c_tech = self.canonical_str(tech_name)
        key = f"{date_key}|{c_truck}"
        rec_data = {
            "date": date_key,
            "truck": truck_name,
            "canon_truck": c_truck,
            "tech": tech_name,
            "canon_tech": c_tech,
            "reconciled_at": self.get_pst_now().isoformat(),
            "status": status,
        }
        history[key] = rec_data
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ Could not write reconciliation history: {e}")

    # --- UI FILTERING & NAVIGATION ---

    @staticmethod
    def format_date(date_str: str) -> str:
        """Normalizes date string to MM/DD/YYYY format."""
        if not date_str:
            return ""
        s = str(date_str).strip()
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%b %d", "%B %d"):
            try:
                dt = datetime.strptime(s, fmt)
                if "%Y" not in fmt and "%y" not in fmt:
                    dt = dt.replace(year=datetime.now().year)
                return dt.strftime("%m/%d/%Y")
            except ValueError:
                pass
        return s

    async def select_status(self, status_name: str) -> bool:
        """Selects status in the list filter dropdown."""
        try:
            status_container = self.page.locator(".status-dropdown-container_c1L9z, .status-select").first
            if await status_container.count() == 0:
                status_container = self.page.locator("//div[contains(@class,'v-select') and .//span[contains(.,'Status')]]").first

            await status_container.click(force=True)
            await self.page.wait_for_timeout(200)

            option = self.page.locator("ul.vs__dropdown-menu li, ul[role='listbox'] li").filter(has_text=status_name).first
            if await option.count() > 0:
                await option.click(force=True)
                return True
        except Exception as e:
            print(f"⚠️ Note on selecting status '{status_name}': {e}")
        return False

    async def set_date_filter(self, start_date: str, end_date: str) -> bool:
        """Fills the From and To date filter fields."""
        try:
            inputs = self.page.locator("input.date-picker_fG3k2, .date-range input, input[placeholder*='Date']")
            if await inputs.count() >= 2:
                for inp, val in [(inputs.nth(0), start_date), (inputs.nth(1), end_date)]:
                    await inp.click(force=True)
                    await self.page.keyboard.press("Control+A")
                    await self.page.keyboard.press("Backspace")
                    await inp.type(val, delay=25)
                    await self.page.keyboard.press("Enter")
                    await inp.evaluate(f"el => {{ el.value = '{val}'; el.dispatchEvent(new Event('input', {{ bubbles: true }})); el.dispatchEvent(new Event('change', {{ bubbles: true }})); }}")
                    await self.page.wait_for_timeout(150)
                return True
        except Exception as e:
            print(f"⚠️ Error setting date filters: {e}")
        return False

    async def apply_filters(self) -> bool:
        """Clicks the Apply Filter button and waits for results to load."""
        try:
            apply_btn = self.page.locator("button.apply-filter, button:has-text('Apply'), button:has-text('Filter')").first
            if await apply_btn.count() > 0:
                await apply_btn.click(force=True)
                await self.page.wait_for_timeout(1000)

            spinners = self.page.locator(".loading, .spinner, .p-datatable-loading")
            if await spinners.count() > 0:
                await spinners.first.wait_for(state="hidden", timeout=12000)
            return True
        except Exception as e:
            print(f"⚠️ Note applying filters: {e}")
            return False

    async def _scroll_table_fully(self):
        """Scrolls the data table to load all rows."""
        try:
            container = self.page.locator(".p-datatable-wrapper, .p-datatable-scrollable-body, .custom-scrollbar").first
            if await container.count() > 0:
                for _ in range(5):
                    await container.evaluate("el => el.scrollTop += 600")
                    await self.page.wait_for_timeout(200)
                await container.evaluate("el => el.scrollTop = 0")
        except Exception:
            pass

    async def scrape_asset_assignments(self) -> Dict[str, Any]:
        """Scrapes all rows from the FieldEdge table into structured dictionaries."""
        await self._scroll_table_fully()
        try:
            dom_rows = await self.page.evaluate(r"""() => {
                const trs = Array.from(document.querySelectorAll('tbody.p-datatable-tbody > tr.p-selectable-row, tbody.p-datatable-tbody > tr'));
                return trs.map(tr => {
                    const cells = Array.from(tr.querySelectorAll('td')).map(td => (td.textContent || '').trim());
                    const woEl = tr.querySelector('.dispatch-name') || tr.querySelector('td:nth-child(2)');
                    const woNum = woEl ? (woEl.getAttribute('title') || woEl.textContent || '').replace(/[^0-9]/g, '') : '';
                    const isCompleted = (cells[3] || '').toLowerCase().includes('complete');
                    return {
                        work_order_number: woNum,
                        customer_name: cells[0] || '',
                        status_name: cells[3] || '',
                        tech_name: cells[6] || '',
                        priority_name: cells[5] || '',
                        scheduled_date: cells[4] || '',
                        is_completed: isCompleted,
                        cells: cells
                    };
                });
            }""")
            truck_headers = [r for r in dom_rows if self.is_fleet_vehicle_name(r.get("customer_name", ""))]
            return {"all_rows": dom_rows, "truck_assign_headers": truck_headers}
        except Exception as e:
            print(f"⚠️ Error scraping asset assignments: {e}")
            return {"all_rows": [], "truck_assign_headers": []}

    # --- TEMPLATE SPECIFICATION HELPERS ---

    def filter_truck_assign_headers(self, rows: List[Dict]) -> List[Dict]:
        return [r for r in rows if self.is_fleet_vehicle_name(r.get("customer_name", ""))]

    def get_expected_tech_for_truck(self, truck_name: str) -> Optional[str]:
        for h in self.template_data.get("truck_assign_headers", []):
            if h.get("active", True) and self.is_same_equipment(truck_name, h.get("name", "")):
                tech = (h.get("tech_name") or "").strip()
                if tech and tech.lower() not in ("s3", "s4", "unassigned", "none", "clear", ""):
                    return tech
        return None

    def get_expected_header_for_truck(self, truck_name: str) -> Optional[Dict]:
        for h in self.template_data.get("truck_assign_headers", []):
            if h.get("active", True) and self.is_same_equipment(truck_name, h.get("name", "")):
                return h
        return None

    def get_expected_truck_for_tech(self, tech_name: str) -> Optional[Dict]:
        for h in self.template_data.get("truck_assign_headers", []):
            if h.get("active", True) and self.is_tech_match(h.get("tech_name", ""), tech_name):
                return h
        return None

    def is_item_turned_off_in_dashboard(self, item_name: str) -> bool:
        c_target = self.canonical_str(item_name)
        for section in ("static_headers", "truck_assign_headers", "out_of_service_assets", "service_needed_assets"):
            for item in self.template_data.get(section, []):
                name = item.get("name") or item.get("asset_name") or ""
                if self.canonical_str(name) == c_target or self.is_same_equipment(name, item_name):
                    return item.get("active") is False
        return False

    def is_static_header_name(self, name: str) -> bool:
        c_name = self.canonical_str(name)
        for h in self.template_data.get("static_headers", []):
            if self.canonical_str(h.get("name", "")) == c_name:
                return True
        return False

    @staticmethod
    def is_fleet_vehicle_name(name: str) -> bool:
        if not name:
            return False
        s = str(name).strip()
        return any(x in s for x in ("🛻", "🚛", "🏗", "PT", "SER TR", "EXC")) or bool(re.search(r'\b\d{2,4}\b', s))

    # --- SIDEPANEL UI ACTIONS ---

    async def close_sidepanel(self):
        """Closes the right-hand inspection sidepanel."""
        try:
            close_btn = self.page.locator("button.close_rLp2b, .panel-header-container_EYC6s .close, button.close, [aria-label='Close Sidepanel']").first
            if await close_btn.count() > 0 and await close_btn.is_visible():
                await close_btn.click(force=True)
            else:
                await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(200)
        except Exception:
            pass

    async def select_dropdown_option(self, dropdown_locator_candidates: Any, option_text: str) -> bool:
        """
        Selects an option in sidepanel dropdown (e.g. technician).
        Strict exact match: searches and clicks ONLY exact normalized option.
        No prefix or fuzzy matching.
        """
        if not option_text:
            return False

        target_text = str(option_text).strip()
        target_canon = self.canonical_str(target_text)
        print(f"  🔍 Selecting tech '{target_text}' in Assignment card dropdown...")

        try:
            toggle_opened = await self.page.evaluate("""() => {
                const dd = document.querySelector('.assigned-tech_omVmC .tech-dropdown_qufHC') ||
                           document.querySelector('.assigned-tech_omVmC .v-select') ||
                           document.querySelector('[data-edb-sidepanel-card*="Assignment"] .v-select');
                if (!dd) return false;
                dd.scrollIntoView({ behavior: 'instant', block: 'center' });
                const toggle = dd.querySelector('.vs__dropdown-toggle') || dd.querySelector('input') || dd;
                toggle.click();
                return true;
            }""")
            if not toggle_opened:
                return False
            await self.page.wait_for_timeout(250)

            # Type exact search term
            await self.page.evaluate("""(text) => {
                const input = document.querySelector('.assigned-tech_omVmC input.vs__search') || 
                              document.querySelector('.tech-dropdown_qufHC input') ||
                              document.querySelector('[data-edb-sidepanel-card*="Assignment"] input.vs__search');
                if (input) {
                    input.focus();
                    input.value = text;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""", target_text)
            await self.page.wait_for_timeout(350)

            # Match exact normalized option
            clicked = await self.page.evaluate("""(args) => {
                const { targetText, targetCanon } = args;
                const norm = (s) => (s || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g, '');
                const items = Array.from(document.querySelectorAll('.assigned-tech_omVmC ul[role="listbox"] li, .tech-dropdown_qufHC ul li, .vs__dropdown-menu li, li.vs__dropdown-option'));
                const cTarget = norm(targetCanon || targetText);
                const rawTarget = (targetText || '').trim().toLowerCase();

                for (const item of items) {
                    const txt = (item.textContent || item.innerText || '').trim();
                    const cleanTxt = txt.replace(/[\u00d7xX]$/, '').trim();
                    const cTxt = norm(cleanTxt);
                    if ((cTxt && cTarget && cTxt === cTarget) || cleanTxt.toLowerCase() === rawTarget) {
                        item.scrollIntoView({ behavior: 'instant', block: 'nearest' });
                        item.click();
                        ['mousedown', 'mouseup', 'click'].forEach(e => item.dispatchEvent(new MouseEvent(e, { bubbles: true, cancelable: true })));
                        return true;
                    }
                }
                return false;
            }""", {"targetText": target_text, "targetCanon": target_canon})

            print(f"  -> Tech dropdown selection for '{target_text}': {'Success' if clicked else 'Failed'}")
            await self.page.wait_for_timeout(300)
            return clicked
        except Exception as e:
            print(f"⚠️ Error selecting '{target_text}' in dropdown: {e}")
            return False

    async def click_and_open_sidepanel(self, work_order_number: str, tech_name: str = "") -> bool:
        """Finds row on table by WO number and clicks to open details sidepanel."""
        clean_wo = re.sub(r'[^0-9]', '', str(work_order_number))
        clean_tech = re.sub(r'[^a-z0-9]', '', tech_name.lower()) if tech_name else ""
        try:
            await self.close_sidepanel()
            await self.page.evaluate("() => { const c = document.querySelector('.p-datatable-wrapper'); if (c) c.scrollTop = 0; }")
            await self.page.wait_for_timeout(150)

            for _ in range(12):
                res = await self.page.evaluate(r"""(args) => {
                    const { wo, tech } = args;
                    const rows = Array.from(document.querySelectorAll('tbody.p-datatable-tbody > tr'));
                    for (const row of rows) {
                        const cells = row.querySelectorAll('td');
                        if (cells.length < 3) continue;
                        const rowWo = (cells[1]?.textContent || '').replace(/[^0-9]/g, '');
                        if (rowWo === wo) {
                            if (tech && cells.length > 6) {
                                const rowTech = (cells[6].textContent || '').toLowerCase().replace(/[^a-z0-9]/g, '');
                                if (rowTech && rowTech !== tech) continue;
                            }
                            row.scrollIntoView({ behavior: 'instant', block: 'center' });
                            const target = cells[1].querySelector('.dispatch-name') || cells[1];
                            target.click();
                            return true;
                        }
                    }
                    return false;
                }""", {"wo": clean_wo, "tech": clean_tech})

                if res:
                    await self.page.wait_for_timeout(400)
                    return True
                await self.page.evaluate("() => { const c = document.querySelector('.p-datatable-wrapper'); if (c) c.scrollTop += 350; }")
                await self.page.wait_for_timeout(150)
        except Exception as e:
            print(f"⚠️ Error opening sidepanel for WO #{clean_wo}: {e}")
        return False

    async def scroll_sidepanel_to_work_order(self):
        """Scrolls sidepanel down to Work Order card."""
        try:
            await self.page.evaluate("""() => {
                const container = document.querySelector('.container__Gkig, .custom-scrollbar');
                if (container) container.scrollTop = container.scrollHeight * 0.75;
                const wo = document.querySelector('[data-edb-sidepanel-card*="WorkOrder"]');
                if (wo) wo.scrollIntoView({ behavior: 'instant', block: 'center' });
            }""")
            await self.page.wait_for_timeout(200)
        except Exception:
            pass

    async def _edit_assignment_card(self, target_tech: Optional[str] = None, target_time: Optional[str] = None) -> bool:
        """Modular helper to edit Tech and/or Scheduled Time on the Assignment card."""
        try:
            # 1. Click edit button on Assignment card
            await self.page.evaluate("""() => {
                const card = document.querySelector('[data-edb-sidepanel-card*="Assignment"]') || document.querySelector('.card_ngkhz');
                if (card) {
                    card.scrollIntoView({ behavior: 'instant', block: 'center' });
                    const btn = card.querySelector('.base-icon.edit, .edit');
                    if (btn) btn.click();
                }
            }""")
            await self.page.wait_for_timeout(300)

            # 2. Update Tech if needed
            if target_tech:
                await self.select_dropdown_option(None, target_tech)
                await self.page.wait_for_timeout(250)

            # 3. Update Time if needed
            if target_time:
                await self.page.evaluate("""(tVal) => {
                    const card = document.querySelector('[data-edb-sidepanel-card*="Assignment"]') || document.querySelector('.card_ngkhz');
                    if (!card) return;
                    const inp = card.querySelector('div[name="Scheduled Time"] input, .timepicker-container input, .cell-pos-3x1 input, input[placeholder="Time"]');
                    if (inp) {
                        inp.focus();
                        inp.value = tVal;
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                        inp.dispatchEvent(new Event('blur', { bubbles: true }));
                    }
                }""", target_time)
                await self.page.wait_for_timeout(250)

            # 4. Click Save on Assignment card
            saved = await self.page.evaluate("""() => {
                const card = document.querySelector('.card_ngkhz.edit-mode-style_ABe0v, [data-edb-sidepanel-card*="Assignment"]') || document.querySelector('.card_ngkhz');
                if (!card) return false;
                const saveBtn = Array.from(card.querySelectorAll('button')).find(b => (b.textContent || '').trim().toLowerCase() === 'save' || b.classList.contains('confirm_I8LeR'));
                if (saveBtn) {
                    saveBtn.removeAttribute('disabled');
                    saveBtn.click();
                    return true;
                }
                return false;
            }""")
            await self.page.wait_for_timeout(350)
            return bool(saved)
        except Exception as e:
            print(f"⚠️ Error editing Assignment card: {e}")
            return False

    async def _edit_workorder_card(self, target_priority: Optional[str] = None, note: Optional[str] = None) -> bool:
        """Modular helper to edit Priority and/or Work Summary Note on the Work Order card."""
        try:
            await self.scroll_sidepanel_to_work_order()

            # 1. Open Work Order details view (click chevron)
            await self.page.evaluate("""() => {
                const wo = document.querySelector('[data-edb-sidepanel-card*="WorkOrder"]');
                if (wo) {
                    const chevron = wo.querySelector('.details-icon_lgpCm, .header_Ce6MY');
                    if (chevron) chevron.click();
                }
            }""")
            await self.page.wait_for_timeout(300)

            # 2. Click edit button on Work Order details card
            await self.page.evaluate("""() => {
                const btn = document.querySelector('.workorder-details-card_kiboz .edit-icon_vG0FU, .edit-icon_vG0FU, .base-icon.edit');
                if (btn) btn.click();
            }""")
            await self.page.wait_for_timeout(300)

            # 3. Update Priority if needed
            if target_priority:
                await self.page.evaluate("""(prio) => {
                    const sel = document.querySelector('.priority-select, #prioritySelect');
                    if (sel) {
                        const toggle = sel.querySelector('.vs__dropdown-toggle') || sel;
                        toggle.click();
                    }
                }""")
                await self.page.wait_for_timeout(200)

                await self.page.evaluate("""(prio) => {
                    const items = Array.from(document.querySelectorAll('.priority-select li, .vs__dropdown-menu li'));
                    const match = items.find(i => (i.textContent || '').trim().includes(prio));
                    if (match) match.click();
                }""", target_priority)
                await self.page.wait_for_timeout(200)

            # 4. Update Summary Note if needed
            if note:
                await self.page.evaluate("""(val) => {
                    const ta = document.querySelector('.summary-note_Dk8T7 textarea, textarea[name="WorkSummary"], textarea');
                    if (ta) {
                        ta.focus();
                        ta.value = val;
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                        ta.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""", note)
                await self.page.wait_for_timeout(200)

            # 5. Click Save on Work Order details card
            saved = await self.page.evaluate("""() => {
                const btn = document.querySelector('.workorder-details-card_kiboz button.confirm_I8LeR, button.confirm_I8LeR, button:has-text("Save")');
                if (btn) {
                    btn.removeAttribute('disabled');
                    btn.click();
                    return true;
                }
                return false;
            }""")
            await self.page.wait_for_timeout(350)
            return bool(saved)
        except Exception as e:
            print(f"⚠️ Error editing Work Order card: {e}")
            return False

    # --- RECONCILIATION ACTIONS ---

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
        """Reconciles Assignment (tech, time) and Work Order (priority, note) for an asset."""
        print(f"\n🛠️ [{stage_label}] Processing WO #{work_order_number} ('{asset_name}')...")
        try:
            if not await self.click_and_open_sidepanel(work_order_number):
                return False

            need_tech = bool(target_tech and not self.is_tech_match(current_tech, target_tech) and self.canonical_str(current_tech) != self.canonical_str(target_tech))
            need_time = bool(target_time and not self.is_time_matching(target_time, current_sched))
            need_assignment = need_tech or need_time

            need_prio = bool(target_priority and not self.is_priority_match(current_priority, target_priority))
            need_note = bool(service_note)
            need_wo = need_prio or need_note

            if not need_assignment and not need_wo:
                print(f"  ⏭️ WO #{work_order_number} ('{asset_name}') is ALREADY fully set. Skipping update.")
                await self.close_sidepanel()
                return True

            if need_assignment:
                print(f"  🕒 Updating Assignment card (tech={target_tech}, time={target_time})...")
                await self._edit_assignment_card(target_tech=target_tech if need_tech else None, target_time=target_time if need_time else None)

            if need_wo:
                print(f"  📝 Updating Work Order details (prio={target_priority})...")
                await self._edit_workorder_card(target_priority=target_priority if need_prio else None, note=service_note if need_note else None)

            await self.close_sidepanel()
            return True
        except Exception as e:
            print(f"⚠️ Error updating WO #{work_order_number}: {e}")
            await self.close_sidepanel()
            return False

    async def click_and_reconcile_work_order(
        self,
        work_order_number: str,
        truck_name: str = "",
        expected_tech: str = ""
    ) -> bool:
        """Reassigns a truck work order to its expected technician if mismatched."""
        try:
            if not await self.click_and_open_sidepanel(work_order_number):
                return False

            card_tech = await self.page.evaluate("""() => {
                const card = document.querySelector('[data-edb-sidepanel-card*="Assignment"]');
                return card ? (card.querySelector('.assigned-tech_omVmC')?.textContent || '').trim() : '';
            }""")

            if card_tech and expected_tech and self.is_tech_match(card_tech, expected_tech):
                print(f"  ✅ WO #{work_order_number} already assigned to '{expected_tech}'.")
                await self.close_sidepanel()
                return True

            print(f"  👤 Reassigning WO #{work_order_number} to '{expected_tech}'...")
            reassigned = await self._edit_assignment_card(target_tech=expected_tech)
            await self.close_sidepanel()
            return reassigned
        except Exception as e:
            print(f"⚠️ Error reconciling WO #{work_order_number}: {e}")
            await self.close_sidepanel()
            return False

    async def cancel_work_order_via_list(self, wo_number: str, reason: str = "", expected_asset: str = "") -> bool:
        """Cancels a duplicate or extra work order via the FieldEdge List/0 interface with safety checks."""
        if not wo_number:
            return False
        wo_str = str(wo_number).strip()
        print(f"\n🗑️ [Cancel WO #{wo_str}] Initiating cancellation via FieldEdge List...")
        new_page = None

        try:
            context = getattr(self, 'context', None) or (self.page.context if self.page else None)
            if not context:
                return False
            new_page = await context.new_page()
            await new_page.goto("https://login.fieldedge.com/#/List/0", wait_until="domcontentloaded", timeout=45000)

            search_input = new_page.locator("input#search, .custom-search input.search").first
            await search_input.wait_for(state="visible", timeout=30000)
            await search_input.fill(wo_str)
            await search_input.press("Enter")
            await new_page.wait_for_timeout(1500)

            target_row = None
            target_status = ""
            target_cust = ""
            for _ in range(20):
                rows = new_page.locator("table.fixed-table tbody.fixed-body tr.regular")
                for i in range(await rows.count()):
                    r = rows.nth(i)
                    txt = await r.inner_text()
                    if wo_str in txt:
                        target_row = r
                        cells = r.locator("td")
                        if await cells.count() >= 4:
                            target_cust = (await cells.nth(0).inner_text()).strip()
                            target_status = (await cells.nth(3).inner_text()).strip()
                        break
                if target_row:
                    break
                await new_page.wait_for_timeout(400)

            if not target_row:
                print(f"  ⚠️ No table row found matching WO #{wo_str}.")
                return False

            # Safety Guard: Ensure asset matches expected asset
            if expected_asset and target_cust and not self.is_same_equipment(expected_asset, target_cust):
                print(f"  🛑 [SAFETY GUARD] Customer '{target_cust}' != expected '{expected_asset}'! Aborting cancellation.")
                return False

            if "cancel" in target_status.lower():
                print(f"  ✅ WO #{wo_str} is already Canceled.")
                return True
            if "complete" in target_status.lower():
                print(f"  🛑 WO #{wo_str} is Completed. Protecting work order.")
                return False

            # Click row to open details
            wo_span = target_row.locator(".display-name-cell, td:nth-child(2)").first
            await wo_span.click()
            await new_page.wait_for_timeout(1000)

            save_btn = new_page.locator("button#dispatch-summary-save-workorder, button[data-automation-id='save-workorder-button']").first
            await save_btn.wait_for(state="visible", timeout=20000)
            await save_btn.click()
            await new_page.wait_for_timeout(1000)

            # Select 'Canceled' radio in modal
            modal = new_page.locator(".modal_gRQBi, .modal, [role='dialog']").first
            radio = modal.locator(".field-radio-container").filter(has_text="Canceled").first
            await radio.click(force=True)
            await new_page.wait_for_timeout(250)

            # Fill cancellation note
            notes_ta = modal.locator("textarea[name='CancellationNotes'], textarea").first
            if await notes_ta.count() > 0:
                await notes_ta.fill(reason or "Duplicate visual aid work order.")

            confirm_btn = modal.locator("button#confirm-save-dispatch-summary, button:has-text('Confirm'), button:has-text('Yes')").first
            await confirm_btn.click(force=True)
            await new_page.wait_for_timeout(1000)
            print(f"🎉 Successfully canceled WO #{wo_str}!")
            return True
        except Exception as e:
            print(f"❌ Error cancelling WO #{wo_str}: {e}")
            return False
        finally:
            if new_page:
                try:
                    await new_page.close()
                except Exception:
                    pass

    # --- TIME & PRIORITY MATCHING HELPERS ---

    def find_asset_row(
        self,
        asset_name: str,
        rows: List[Dict],
        target_priority: Optional[str] = None,
        target_time: Optional[str] = None,
        target_tech: Optional[str] = None,
        is_service_needed: bool = False,
        is_out_of_service: bool = False
    ) -> Optional[Dict]:
        matched_rows = [r for r in rows if not r.get("is_completed") and self.is_same_equipment(asset_name, r.get("customer_name", ""))]
        if not matched_rows:
            return None
        if len(matched_rows) == 1:
            return matched_rows[0]

        # Score candidates by matching time, priority, and tech
        scored = []
        for r in matched_rows:
            sc = 0
            if target_time and self.is_time_matching(target_time, r.get("scheduled_date", "")):
                sc += 100
            if target_priority and self.is_priority_match(r.get("priority_name", ""), target_priority):
                sc += 50
            if target_tech and (self.is_tech_match(r.get("tech_name", ""), target_tech) or self.canonical_str(r.get("tech_name", "")) == self.canonical_str(target_tech)):
                sc += 25
            scored.append((sc, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    @staticmethod
    def is_time_matching(target_time: str, sched_str: str) -> bool:
        if not target_time or not sched_str:
            return False
        m_target = re.search(r'(\d{1,2}):(\d{2})\s*(am|pm)?', target_time, re.IGNORECASE)
        if not m_target:
            return False
        th = int(m_target.group(1))
        tp = (m_target.group(3) or '').lower()
        th_24 = (th + 12 if th < 12 else th) if tp == 'pm' else (0 if th == 12 else th)

        range_parts = sched_str.split('-')
        start_str = range_parts[0] if range_parts else sched_str
        times_in_sched = re.findall(r'(\d{1,2}):(\d{2})\s*(am|pm)?', start_str, re.IGNORECASE)
        if times_in_sched:
            sh_s, sm, sp = times_in_sched[0]
            sh = int(sh_s)
            sh_24 = (sh + 12 if sh < 12 else sh) if (sp or '').lower() == 'pm' else (0 if sh == 12 else sh)
            return th_24 == sh_24 and m_target.group(2) == sm
        return False

    @staticmethod
    def is_priority_match(current_prio: str, target_prio: str) -> bool:
        if not current_prio or not target_prio:
            return False
        norm = lambda p: re.sub(r'[^a-zA-Z0-9]', '', unicodedata.normalize('NFKD', str(p))).lower()
        c_p = norm(current_prio)
        t_p = norm(target_prio)
        if c_p == t_p:
            return True
        # Extract priority code e.g. '[7]' or '7.12'
        m_c = re.findall(r'\b\d+\b', str(current_prio))
        m_t = re.findall(r'\b\d+\b', str(target_prio))
        return bool(m_c and m_t and m_c[0] == m_t[0] and (c_p in t_p or t_p in c_p))

    # --- SINGLE DATE RECONCILIATION ---

    async def reconcile_single_date(
        self,
        date_str: str,
        click_wos: bool = True,
        status_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Reconciles Out of Service, Service Needed, and driver truck assignments for a single date."""
        print(f"\n=======================================================")
        print(f"🎯 Starting Asset Assignment Reconciliation for: {date_str}")
        print(f"=======================================================")

        try:
            target_url = self.rules.get('asset_assignment_url') or self.rules.get('web_url') or 'https://login.fieldedge.com/Dispatch'
            if "Dispatch" not in (self.page.url or ""):
                await self.page.goto(target_url, wait_until='domcontentloaded')
                await self.page.wait_for_timeout(1000)

            if "Login" in (self.page.url or ""):
                await self.login_fieldedge()
                await self.page.goto(target_url, wait_until='domcontentloaded')

            await self.select_status(status_name or self.rules.get('status_name', 'Assigned'))
            normalized_date = self.format_date(date_str)
            await self.set_date_filter(normalized_date, normalized_date)
            await self.apply_filters()

            scraped = await self.scrape_asset_assignments()
            all_rows = scraped.get("all_rows", [])
            total_checked = 0
            total_reconciled = 0

            # 1. Out of Service Assets (Stage 0)
            oos_configs = [s for s in self.template_data.get("out_of_service_assets", []) if s.get("active", True)]
            oos_names = {self.canonical_str(s.get("asset_name", "")) for s in oos_configs if s.get("asset_name")}

            if oos_configs and click_wos:
                for oos_cfg in oos_configs:
                    asset = oos_cfg.get("asset_name", "")
                    r = self.find_asset_row(asset, all_rows, target_priority=oos_cfg.get("priority"), target_time=oos_cfg.get("start_time", "1:45 PM"), is_out_of_service=True)
                    if r:
                        total_checked += 1
                        if await self.update_service_needed_work_order(r.get("work_order_number", ""), asset, target_time=oos_cfg.get("start_time", "1:45 PM"), target_priority=oos_cfg.get("priority", "[4] DO NOT DISPATCH (Tracking)"), service_note=oos_cfg.get("service_note", ""), target_tech=oos_cfg.get("tech_name", "s4"), current_tech=r.get("tech_name", ""), current_sched=r.get("scheduled_date", ""), current_priority=r.get("priority_name", ""), stage_label="Out of Service Update"):
                            total_reconciled += 1

            # 2. Service Needed Assets (Stage 1)
            sn_configs = [s for s in self.template_data.get("service_needed_assets", []) if s.get("active", True) and self.canonical_str(s.get("asset_name", "")) not in oos_names]
            if sn_configs and click_wos:
                for sn_cfg in sn_configs:
                    asset = sn_cfg.get("asset_name", "")
                    r = self.find_asset_row(asset, all_rows, target_priority=sn_cfg.get("priority"), target_time=sn_cfg.get("start_time", "3:30 PM"), is_service_needed=True)
                    if r:
                        total_checked += 1
                        if await self.update_service_needed_work_order(r.get("work_order_number", ""), asset, target_time=sn_cfg.get("start_time", "3:30 PM"), target_priority=sn_cfg.get("priority", "[7] 7.12 SERVICE NEEDED"), service_note=sn_cfg.get("service_note", ""), target_tech=sn_cfg.get("tech_name", "s4"), current_tech=r.get("tech_name", ""), current_sched=r.get("scheduled_date", ""), current_priority=r.get("priority_name", ""), stage_label="Service Needed Update"):
                            total_reconciled += 1

            # 3. Active Truck Assignments (Stage 2)
            active_trucks = [h for h in self.template_data.get("truck_assign_headers", []) if h.get("active", True) and (h.get("tech_name") or "").strip().lower() not in ("s3", "s4", "unassigned", "none", "clear", "")]
            if active_trucks and click_wos:
                for truck_cfg in active_trucks:
                    truck_name = truck_cfg.get("name", "")
                    exp_tech = truck_cfg.get("tech_name", "")
                    exp_time = truck_cfg.get("start_time", "6:00 AM")

                    matching_cards = [r for r in all_rows if not r.get("is_completed") and self.is_same_equipment(truck_name, r.get("customer_name", ""))]
                    if len(matching_cards) == 1:
                        c = matching_cards[0]
                        total_checked += 1
                        if not self.is_tech_match(c.get("tech_name", ""), exp_tech):
                            if await self.click_and_reconcile_work_order(c.get("work_order_number", ""), truck_name, exp_tech):
                                total_reconciled += 1
                    elif len(matching_cards) > 1:
                        # Duplicate cards: keep driver's card, cancel extras
                        driver_card = next((c for c in matching_cards if self.is_tech_match(c.get("tech_name", ""), exp_tech)), None)
                        for c in matching_cards:
                            if c != driver_card:
                                print(f"🗑️ Found duplicate card WO #{c.get('work_order_number')} for '{truck_name}'. Cancelling...")
                                await self.cancel_work_order_via_list(c.get("work_order_number"), reason=f"Duplicate card for '{truck_name}'.", expected_asset=truck_name)

            return {"date": date_str, "checked": total_checked, "reassignments": total_reconciled}
        except Exception as e:
            print(f"⚠️ Error during reconciliation for {date_str}: {e}")
            return {"date": date_str, "checked": 0, "reassignments": 0, "error": str(e)}

    async def run(
        self,
        days: int = 30,
        dry_run: bool = False,
        start_date_str: Optional[str] = None,
        on_progress: Optional[Callable] = None
    ):
        """Runs multi-day asset assignment reconciliation."""
        start_dt = self.get_pst_now().date()
        if start_date_str:
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
                try:
                    start_dt = datetime.strptime(start_date_str, fmt).date()
                    break
                except ValueError:
                    pass

        await self.initialize()
        for d in range(days):
            current_date = start_dt + timedelta(days=d)
            fmt_date = current_date.strftime("%m/%d/%Y")
            await self.reconcile_single_date(fmt_date, click_wos=not dry_run)
            if on_progress:
                on_progress({"current_day": d + 1, "total_days": days, "status": "completed"})
        await self.cleanup()
