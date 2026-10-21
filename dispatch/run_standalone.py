"""
Standalone Launcher for Dispatch Board Display Automation Scraper
Runs independently inside the root /dispatch directory.
"""

import os
import sys
import asyncio
import argparse
import http.server
import socketserver
import threading
import json
from datetime import datetime

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

_dispatch_dir = os.path.dirname(os.path.abspath(__file__))
if _dispatch_dir not in sys.path:
    sys.path.insert(0, _dispatch_dir)

from base_scraper import PST_TZ, get_pst_now
from dispatch_board_display_automation_scraper import DispatchBoardDisplayAutomationScraper

STATUS_DATA = {
    "status": "online",
    "service": "Dispatch Board  & Asset Assignment Automation",
    "mode": "DRY RUN",
    "days_configured": 31,
    "last_run": None,
    "last_run_pst": None,
    "last_status": "initialized",
    "fieldedge_status": "Connected / Active Session",
    "progress": {
        "current_day": 0,
        "total_days": 31,
        "current_date": None,
        "completed_days": 0,
        "remaining_days": 31,
        "percent_complete": 0.0,
        "status": "idle",
        "status_message": "Waiting for run to start..."
    },
    "port": 3000
}

STATE_FILE = os.path.join(_dispatch_dir, "config", "status_state.json")

def save_status_state():
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATUS_DATA, f, indent=2)
    except Exception as e:
        print(f"⚠️ Failed to save status state: {e}")

def load_status_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    STATUS_DATA.update(data)
                    print("✅ Loaded persisted status state from config/status_state.json")
        except Exception as e:
            print(f"⚠️ Failed to load status state: {e}")

load_status_state()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sterling Septic & Plumbing LLC Status</title>
    <link rel="icon" type="image/png" href="/assets/favicon.png">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            theme: {
                extend: {
                    fontFamily: {
                        sans: ['Public Sans', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
                    },
                    borderRadius: {
                        DEFAULT: '5px',
                        'sm': '5px',
                        'md': '5px',
                        'lg': '5px',
                        'xl': '5px',
                        '2xl': '5px',
                        'full': '5px'
                    },
                    colors: {
                        sterling: {
                            DEFAULT: '#76AADA',
                            dark: '#5c95c8',
                            light: '#a3c7e8',
                            bg: '#f8fafc'
                        }
                    }
                }
            }
        }
    </script>
    <style>
        .rounded-sm, .rounded-md, .rounded-lg, .rounded-xl, .rounded-2xl, .rounded-full, button, input, select {
            border-radius: 5px !important;
        }
        #fieldedgeDot {
            border-radius: 9999px !important;
        }
    </style>
</head>
<body class="text-slate-800 font-sans antialiased min-h-screen flex flex-col justify-between font-normal relative" style="background: linear-gradient(180deg, rgba(15, 23, 42, 0.35) 0%, rgba(30, 41, 59, 0.30) 100%), url('/assets/arrival-window.jpg') center / cover no-repeat fixed;">

    <!-- Top Site Header (Frosted Glass Navbar - Full Background Image Visible) -->
    <header class="bg-white/35 backdrop-blur-xl border-b border-white/30 shadow-sm sticky top-0 z-50">
        <div class="max-w-6xl mx-auto px-4 sm:px-8 py-3 flex justify-between items-center">
            <!-- Left: Company Logo -->
            <div class="flex items-center">
                <a href="javascript:void(0)" class="flex items-center">
                    <img src="/assets/logo.png" alt="Sterling Septic & Plumbing, LLC" class="h-[42px] sm:h-[48px] w-auto object-contain max-h-[48px]" onerror="this.onerror=null; this.src='/assets/favicon.png';">
                </a>
            </div>

            <!-- Right: Phone Call Button -->
            <div>
                <a href="tel:2533424061" class="bg-[#76AADA] hover:bg-[#5c95c8] text-white px-4 sm:px-5 py-2 rounded-[5px] text-xs sm:text-sm font-bold shadow-md transition-all duration-200 inline-flex items-center gap-2">
                    <svg class="w-3.5 h-3.5 sm:w-4 sm:h-4 fill-current" viewBox="0 0 24 24">
                        <path d="M6.62 10.79a15.053 15.053 0 006.59 6.59l2.2-2.2c.27-.27.67-.36 1.02-.24 1.12.37 2.33.57 3.57.57.55 0 1 .45 1 1V20c0 .55-.45 1-1 1-9.39 0-17-7.61-17-17 0-.55.45-1 1-1h3.5c.55 0 1 .45 1 1 0 1.25.2 2.45.57 3.57.11.35.03.74-.25 1.02l-2.2 2.2z"/>
                    </svg>
                    <span>(253) 342-4061</span>
                </a>
            </div>
        </div>
    </header>

    <!-- Main Content Area -->
    <main class="w-full py-6 sm:py-8 flex-grow">
        <div class="max-w-6xl mx-auto px-4 sm:px-8 space-y-6">

            <!-- Hero Section (Clean Text Directly on Background Image) -->
            <div class="py-2 sm:py-4 text-center">
                <h1 class="text-2xl sm:text-4xl font-extrabold text-white tracking-tight mb-2 drop-shadow-lg">Sterling Services Operations Status</h1>
                <div id="bannerUpdatedText" class="text-xs sm:text-sm text-[#76AADA] font-bold mb-3 drop-shadow-md">Updated 0s ago • PST Timezone</div>
                <p class="text-xs sm:text-sm leading-relaxed text-slate-100 font-medium max-w-3xl mx-auto px-2 drop-shadow-md">
                    Welcome to the Sterling Septic & Plumbing LLC Dispatch Board Display Automation Status Page. This system automatically pre-creates 30-day static visual aid work orders and technician display schedules on the FieldEdge Dispatch Board to ensure smooth dispatch operations. Monitor live automation sync progress, active target dates, and real-time FieldEdge session status below.
                </p>
            </div>

            <!-- Execution Logs Glass Card -->
            <div class="bg-white/92 backdrop-blur-xl rounded-[5px] border border-white/70 shadow-2xl overflow-hidden">
                <!-- Card Header -->
                <div class="bg-white/90 border-b border-slate-200/80 px-4 sm:px-6 py-3.5 sm:py-4 flex justify-between items-center">
                    <h2 class="text-xs sm:text-base font-bold text-slate-900">Automation Execution Logs</h2>
                    <div class="text-[11px] sm:text-xs text-slate-600 font-medium flex items-center gap-2">
                        <span id="cardUpdatedText">Updated 0s ago</span>
                        <button onclick="fetchStatus()" title="Refresh" class="hover:rotate-180 transition-transform duration-300 text-slate-600">🔄</button>
                    </div>
                </div>


                <!-- Badges Section & FieldEdge Real-Time Login Indicator -->
                <div class="px-4 sm:px-6 py-3 border-b border-slate-200/80 flex flex-wrap gap-2 sm:gap-2.5 items-center justify-between bg-white/70">
                    <div class="flex flex-wrap gap-2 sm:gap-2.5 items-center">
                        <span id="successBadge" class="px-2.5 sm:px-3 py-1 rounded-[5px] text-[11px] sm:text-xs font-semibold bg-emerald-50 text-emerald-800 border border-emerald-300 shadow-sm">0 Success</span>
                        <span id="errorBadge" class="px-2.5 sm:px-3 py-1 rounded-[5px] text-[11px] sm:text-xs font-semibold bg-rose-50 text-rose-800 border border-rose-300 shadow-sm">0 Error</span>
                        <span id="partialBadge" class="px-2.5 sm:px-3 py-1 rounded-[5px] text-[11px] sm:text-xs font-semibold bg-amber-50 text-amber-800 border border-amber-300 shadow-sm">0 Partial</span>
                        <span id="runningBadge" class="px-2.5 sm:px-3 py-1 rounded-[5px] text-[11px] sm:text-xs font-semibold bg-sky-50 text-[#5c95c8] border border-sky-300 shadow-sm">1 Running</span>
                    </div>
                    <!-- Real-Time FieldEdge Login Status -->
                    <div id="fieldedgeBadge" class="px-3 py-1 rounded-[5px] text-[11px] sm:text-xs font-semibold bg-emerald-50 text-emerald-800 border border-emerald-300 flex items-center gap-1.5 whitespace-nowrap shadow-sm">
                        <span id="fieldedgeDot" class="w-2 h-2 rounded-full bg-emerald-500"></span>
                        <span id="fieldedgeText">FieldEdge: Connected / Active Session</span>
                    </div>
                </div>

                <!-- Progress Bar Section -->
                <div class="px-4 sm:px-6 py-3.5 sm:py-4 bg-slate-50/60 border-b border-slate-200">
                    <div class="flex justify-between text-[11px] sm:text-xs font-bold text-slate-700 mb-2">
                        <span>30-Day FieldEdge Display Pre-Creation Progress</span>
                        <span id="pctLabel">0%</span>
                    </div>
                    <div class="w-full bg-slate-200/70 rounded-[5px] h-2 overflow-hidden shadow-inner">
                        <div id="progressBarFill" class="bg-gradient-to-r from-[#76AADA] to-[#5c95c8] h-full rounded-[5px] transition-all duration-500 ease-out" style="width: 0%;"></div>
                    </div>
                </div>

                <!-- Table Wrap (Mobile Responsive Horizontal Scroll - Strictly Single Line Text) -->
                <div class="overflow-x-auto w-full">
                    <table class="w-full min-w-[700px] text-left text-xs sm:text-sm text-slate-800 border-collapse">
                        <thead class="bg-white/40 backdrop-blur-md text-slate-700 font-bold uppercase text-[10px] sm:text-[11px] tracking-wider border-b border-slate-200/60">
                            <tr>
                                <th class="px-4 sm:px-6 py-3.5 whitespace-nowrap">Scraper Name</th>
                                <th class="px-4 sm:px-6 py-3.5 whitespace-nowrap">Active Target Date</th>
                                <th class="px-4 sm:px-6 py-3.5 whitespace-nowrap">Status</th>
                                <th class="px-4 sm:px-6 py-3.5 whitespace-nowrap">Batch Progress</th>
                                <th class="px-4 sm:px-6 py-3.5 whitespace-nowrap">Last Sync Time (PST)</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-200/70 bg-white/50">
                            <tr>
                                <td class="px-4 sm:px-6 py-4 font-bold text-slate-900 whitespace-nowrap">dispatch-board-display &amp; asset-assignment</td>
                                <td id="activeDateTd" class="px-4 sm:px-6 py-4 font-medium text-slate-800 whitespace-nowrap">Today</td>
                                <td id="statusPillTd" class="px-4 sm:px-6 py-4 whitespace-nowrap">
                                    <span class="px-2.5 py-1 rounded-[5px] text-[11px] sm:text-xs font-semibold bg-sky-50 text-[#5c95c8] border border-sky-300 shadow-sm">RUNNING</span>
                                </td>
                                <td id="progressTd" class="px-4 sm:px-6 py-4 font-medium text-slate-800 whitespace-nowrap">Day 0 of 30 (30 Days Left)</td>
                                <td id="lastSyncTd" class="px-4 sm:px-6 py-4 font-medium text-slate-800 whitespace-nowrap">-</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </main>

    <!-- Small Compact Footer (Frosted Glass with Visible Background) -->
    <footer class="bg-white/35 backdrop-blur-xl border-t border-white/30 py-3.5 text-center text-[11px] sm:text-xs font-semibold text-slate-900 drop-shadow-sm">
        © 2026 Sterling Septic & Plumbing LLC • All rights reserved.
    </footer>

    <script>
        let lastFetchTime = Date.now();
        
        async function fetchStatus() {
            try {
                let res = await fetch('/dispatch?json=1');
                if (!res.ok) {
                    const altUrl = window.location.pathname.replace(/\/+$/, '') + '?json=1';
                    res = await fetch(altUrl);
                }
                if (!res.ok) {
                    res = await fetch('/?json=1');
                }
                const data = await res.json();
                lastFetchTime = Date.now();
                
                const p = data.progress || {};
                const curDay = p.completed_days !== undefined ? p.completed_days : (p.current_day || 0);
                const total = p.total_days || data.days_configured || 30;
                const rem = p.remaining_days !== undefined ? p.remaining_days : Math.max(0, total - curDay);
                const pct = p.percent_complete !== undefined ? p.percent_complete : (curDay > 0 ? Math.round((curDay / total) * 100) : 0);
                const displayDay = (p.current_day !== undefined && p.current_day > 0) ? p.current_day : curDay;
                
                document.getElementById('successBadge').innerText = curDay + ' Success';
                document.getElementById('pctLabel').innerText = pct + '%';
                document.getElementById('progressBarFill').style.width = pct + '%';
                
                document.getElementById('activeDateTd').innerText = p.current_date || 'Today';
                document.getElementById('progressTd').innerText = 'Day ' + displayDay + ' of ' + total + ' (' + rem + ' Days Left)';

                
                const rawLastSync = data.last_run_pst || data.last_run || '-';
                let cleanSync = String(rawLastSync).trim();
                if (cleanSync !== '-') {
                    cleanSync = cleanSync.replace(/(\s*PST)+$/gi, '').trim() + ' PST';
                }
                document.getElementById('lastSyncTd').innerText = cleanSync;

                const statusStr = p.status || data.last_status || 'running';
                const msg = p.status_message || data.last_status || 'System Active';
                
                // Real-Time FieldEdge Login Status Badge
                const feStatus = data.fieldedge_status || (msg.includes('Concurrent') || msg.includes('logged out') ? msg : 'Connected / Active Session');
                const feBadge = document.getElementById('fieldedgeBadge');
                const feDot = document.getElementById('fieldedgeDot');
                const feText = document.getElementById('fieldedgeText');

                const feLower = feStatus.toLowerCase();
                if (feLower.includes('logged out') || feLower.includes('retry') || feLower.includes('pause') || feLower.includes('concurrent') || msg.includes('Concurrent')) {
                    feBadge.className = 'px-3 py-1 rounded-[5px] text-[11px] sm:text-xs font-semibold bg-amber-50 text-amber-700 border border-amber-200 flex items-center gap-1.5 whitespace-nowrap shadow-sm';
                    feDot.className = 'w-2 h-2 rounded-full bg-amber-500 animate-ping';
                    feText.innerText = 'FieldEdge: ' + feStatus;
                } else if (feLower.includes('logg') || (feLower.includes('connect') && !feLower.includes('active'))) {
                    feBadge.className = 'px-3 py-1 rounded-[5px] text-[11px] sm:text-xs font-semibold bg-sky-50 text-[#5c95c8] border border-sky-200 flex items-center gap-1.5 whitespace-nowrap shadow-sm';
                    feDot.className = 'w-2 h-2 rounded-full bg-[#76AADA] animate-pulse';
                    feText.innerText = 'FieldEdge: ' + feStatus;
                } else if (feLower.includes('fail') || feLower.includes('error')) {
                    feBadge.className = 'px-3 py-1 rounded-[5px] text-[11px] sm:text-xs font-semibold bg-rose-50 text-rose-700 border border-rose-200 flex items-center gap-1.5 whitespace-nowrap shadow-sm';
                    feDot.className = 'w-2 h-2 rounded-full bg-rose-500';
                    feText.innerText = 'FieldEdge: ' + feStatus;
                } else {
                    feBadge.className = 'px-3 py-1 rounded-[5px] text-[11px] sm:text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200 flex items-center gap-1.5 whitespace-nowrap shadow-sm';
                    feDot.className = 'w-2 h-2 rounded-full bg-emerald-500';
                    feText.innerText = 'FieldEdge: ' + (feStatus.includes('FieldEdge') ? feStatus : feStatus);
                }


                const statusPill = document.getElementById('statusPillTd');
                const runningBadge = document.getElementById('runningBadge');

                if (statusStr.includes('paused') || statusStr.includes('concurrent') || msg.includes('logged out') || msg.includes('Concurrent')) {
                    const waitMsg = msg.includes('Retrying') ? msg : 'PAUSED (FieldEdge Login Retry in 30m)';
                    statusPill.innerHTML = '<span class="px-2.5 py-1 rounded-[5px] text-xs font-medium bg-amber-50 text-amber-700 border border-amber-200" title="' + msg + '">' + waitMsg + '</span>';
                    runningBadge.className = 'px-3 py-1 rounded-[5px] text-xs font-medium bg-amber-50 text-amber-700 border border-amber-200';
                    runningBadge.innerText = '1 Paused (Concurrent Login)';
                } else if (statusStr === 'completed' || data.last_status === 'success') {
                    statusPill.innerHTML = '<span class="px-2.5 py-1 rounded-[5px] text-xs font-medium bg-emerald-50 text-emerald-700 border border-emerald-200">COMPLETED</span>';
                    runningBadge.className = 'px-3 py-1 rounded-[5px] text-xs font-medium bg-emerald-50 text-emerald-700 border border-emerald-200';
                    runningBadge.innerText = '0 Running (Finished)';
                } else if (statusStr.includes('error')) {
                    statusPill.innerHTML = '<span class="px-2.5 py-1 rounded-[5px] text-xs font-medium bg-rose-50 text-rose-700 border border-rose-200">ERROR</span>';
                    runningBadge.className = 'px-3 py-1 rounded-[5px] text-xs font-medium bg-rose-50 text-rose-700 border border-rose-200';
                    runningBadge.innerText = '1 Error';
                } else {
                    statusPill.innerHTML = '<span class="px-2.5 py-1 rounded-[5px] text-xs font-medium bg-sky-50 text-[#5c95c8] border border-sky-200">RUNNING</span>';
                    runningBadge.className = 'px-3 py-1 rounded-[5px] text-xs font-medium bg-sky-50 text-[#5c95c8] border border-sky-200';
                    runningBadge.innerText = '1 Running';
                }

            } catch (err) {
                console.error('Failed to fetch status:', err);
            }
        }

        function updateRelativeTime() {
            const elapsedSec = Math.floor((Date.now() - lastFetchTime) / 1000);
            const text = 'Updated ' + elapsedSec + 's ago';
            document.getElementById('bannerUpdatedText').innerText = text + ' • PST Timezone';
            document.getElementById('cardUpdatedText').innerText = text;
        }

        setInterval(fetchStatus, 2000);
        setInterval(updateRelativeTime, 1000);
        fetchStatus();
    </script>
</body>
</html>
"""

class StatusHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def do_HEAD(self):
        try:
            self.do_GET()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def do_GET(self):
        try:
            clean_path = self.path
            if clean_path.startswith("/dispatch"):
                clean_path = clean_path[len("/dispatch"):]
                if not clean_path or not clean_path.startswith("/"):
                    clean_path = "/" + clean_path

            if (clean_path.startswith("/assets/") or clean_path in ("/favicon.ico", "/logo.png", "/favicon.png")) and not clean_path.endswith("/assets/"):
                asset_name = os.path.basename(clean_path)
                asset_path = None
                if asset_name:
                    candidates = [
                        os.path.join(_dispatch_dir, "assets", asset_name),
                        os.path.join(_dispatch_dir, "dispatch", "assets", asset_name),
                        os.path.join(os.getcwd(), "assets", asset_name),
                        os.path.join(os.getcwd(), "dispatch", "assets", asset_name),
                    ]
                    for cand in candidates:
                        if os.path.isfile(cand):
                            asset_path = cand
                            break

                if asset_path:
                    self.send_response(200)
                    ext = os.path.splitext(asset_path)[1].lower()
                    if ext in (".jpg", ".jpeg"):
                        content_type = "image/jpeg"
                    elif ext == ".png":
                        content_type = "image/png"
                    else:
                        content_type = "image/x-icon"
                    self.send_header("Content-Type", content_type)
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    with open(asset_path, "rb") as f:
                        self.wfile.write(f.read())
                    return
                else:
                    self.send_response(404)
                    self.end_headers()
                    return

            if "json=1" in self.path or "json=1" in clean_path or "/api" in clean_path or "application/json" in self.headers.get("Accept", ""):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                response_data = json.dumps(STATUS_DATA, indent=2)
                self.wfile.write(response_data.encode("utf-8"))
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def log_message(self, format, *args):
        pass


class QuietThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc_type, exc_val, _ = sys.exc_info()
        if exc_type in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return  # Silently suppress socket disconnects and port scan errors
        super().handle_error(request, client_address)


def start_status_server(port: int):
    STATUS_DATA["port"] = port
    def run_server():
        try:
            handler = StatusHTTPRequestHandler
            with QuietThreadingTCPServer(("", port), handler) as httpd:
                print(f"🌐 Status & Health Server listening on port {port} (http://0.0.0.0:{port})")
                httpd.serve_forever()
        except Exception as e:
            print(f"⚠️ Could not start HTTP server on port {port}: {e}")

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

def on_progress_update(progress_dict):
    STATUS_DATA["progress"] = progress_dict
    if isinstance(progress_dict, dict) and "fieldedge_status" in progress_dict:
        STATUS_DATA["fieldedge_status"] = progress_dict["fieldedge_status"]
    STATUS_DATA["last_run_pst"] = get_pst_now().strftime("%Y-%m-%d %I:%M:%S %p PST")
    save_status_state()


async def run_automation(args):
    STATUS_DATA["mode"] = "LIVE" if args.live else "DRY RUN"
    STATUS_DATA["days_configured"] = args.days
    STATUS_DATA["progress"]["total_days"] = args.days
    STATUS_DATA["progress"]["remaining_days"] = args.days
    save_status_state()

    scraper = DispatchBoardDisplayAutomationScraper()
    current_start_date = args.start_date

    if args.loop:
        print(f"🔄 Loop Mode enabled. Running every {args.interval_hours} hours to maintain {args.days} pre-created days...")
        while True:
            now_pst = get_pst_now().strftime("%Y-%m-%d %I:%M:%S %p PST")
            print(f"\n⏰ [{now_pst}] Starting automation run for {args.days} days...")
            if current_start_date:
                print(f"   Using start date override for initial run: {current_start_date}")
            else:
                print(f"   Target date range: Today ({get_pst_now().strftime('%Y-%m-%d')}) + {args.days} rolling days ahead.")

            STATUS_DATA["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            STATUS_DATA["last_run_pst"] = now_pst
            STATUS_DATA["last_status"] = "running"
            save_status_state()
            try:
                await scraper.run(
                    days=args.days,
                    dry_run=not args.live,
                    start_date_str=current_start_date,
                    on_progress=on_progress_update,
                    skip_asset=getattr(args, "skip_asset", False)
                )

                # After initial successful run with an explicit start_date_str, reset it to None
                # so that subsequent daily loop runs start from TODAY (get_pst_now().date())
                # to maintain a rolling 30 pre-created days into the future!
                if current_start_date is not None:
                    print(f"✅ Initial start date override ('{current_start_date}') completed. Resetting start date to rolling TODAY for future 24h loops.")
                    current_start_date = None

                STATUS_DATA["last_status"] = "success"
                STATUS_DATA["fieldedge_status"] = "Connected / Active Session"
                STATUS_DATA["progress"].update({
                    "current_day": args.days,
                    "completed_days": args.days,
                    "remaining_days": 0,
                    "percent_complete": 100.0,
                    "status": "completed",
                    "status_message": f"Successfully completed all {args.days} days!"
                })
                save_status_state()
            except Exception as e:
                err_str = str(e)
                print(f"❌ Error during scheduled run: {err_str}")
                STATUS_DATA["last_status"] = f"error: {err_str}"
                save_status_state()

                # Check for session logout / concurrent login
                if "login" in err_str.lower() or "session" in err_str.lower() or "concurrent" in err_str.lower():
                    print("⚠️ FieldEdge single account concurrent login detected. Pausing for 30 minutes before auto-retry...")
                    for m in range(30, 0, -1):
                        msg = f"⚠️ Session logged out (Concurrent login). Retrying FieldEdge login in {m} min..."
                        STATUS_DATA["progress"]["status"] = "paused_concurrent_login"
                        STATUS_DATA["progress"]["status_message"] = msg
                        STATUS_DATA["fieldedge_status"] = f"Logged Out (Retrying login in {m}m)"
                        await asyncio.sleep(60)
                    STATUS_DATA["fieldedge_status"] = "Retrying FieldEdge login now..."

            sleep_seconds = int(args.interval_hours * 3600)
            print(f"\n😴 Run complete. Waiting {args.interval_hours} hours until next run. (Press Ctrl+C to stop)")
            await asyncio.sleep(sleep_seconds)
    else:
        now_pst = get_pst_now().strftime("%Y-%m-%d %I:%M:%S %p PST")
        STATUS_DATA["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        STATUS_DATA["last_run_pst"] = now_pst
        STATUS_DATA["last_status"] = "running"
        try:
            await scraper.run(
                days=args.days,
                dry_run=not args.live,
                start_date_str=args.start_date,
                on_progress=on_progress_update,
                skip_asset=getattr(args, "skip_asset", False)
            )
            STATUS_DATA["last_status"] = "success"
        except Exception as e:
            STATUS_DATA["last_status"] = f"error: {str(e)}"
            raise e

def main():
    parser = argparse.ArgumentParser(description="Run Dispatch Board Display Automation Scraper (Standalone)")
    parser.add_argument("--days", type=int, default=31, help="Number of days to process (default: 31)")
    parser.add_argument("--live", action="store_true", help="Run live on FieldEdge (skips dry-run)")
    parser.add_argument("--start-date", type=str, help="Starting date (e.g. 'August 15', '08/15/2026'). Defaults to today.")
    parser.add_argument("--loop", action="store_true", help="Run continuously on a scheduled interval (e.g. daily)")
    parser.add_argument("--interval-hours", type=float, default=24.0, help="Interval between runs in hours when --loop is set (default: 24)")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "3000")), help="HTTP status server port (default: 3000)")
    parser.add_argument("--skip-asset", action="store_true", help="Skip running the asset assignment tool for each date")
    args = parser.parse_args()

    start_status_server(args.port)

    print(f"🚀 Starting Dispatch Board Display Automation & Asset Assignment (Standalone)...")
    print(f"   Mode: {'LIVE' if args.live else 'DRY RUN'}")
    print(f"   Days: {args.days}")
    print(f"   HTTP Status Port: {args.port}")
    print(f"   Asset Tool: {'SKIPPED (--skip-asset)' if getattr(args, 'skip_asset', False) else 'ENABLED (Runs on same date)'}")
    if args.start_date:
        print(f"   Start Date: {args.start_date}")
    if args.loop:
        print(f"   Loop Schedule: Every {args.interval_hours} hours")

    asyncio.run(run_automation(args))

if __name__ == "__main__":
    main()
