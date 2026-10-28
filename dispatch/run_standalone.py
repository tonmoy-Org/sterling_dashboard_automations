"""
Standalone Launcher for Dispatch Board Display Automation Scraper
Powered by FastAPI & Uvicorn
Real-time Atlassian Statuspage-inspired monitoring dashboard for Sterling Septic & Plumbing LLC & FieldEdge.
"""

import os
import sys
import asyncio
import argparse
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

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
    "service": "Sterling Septic Status",
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

# In-memory status state only (no persistent status_state.json written to disk)

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sterling Septic Status</title>
    <link rel="icon" type="image/png" href="/assets/favicon.png">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: #ffffff;
            color: #333333;
        }
        button {
            font-weight: 500 !important;
        }
        .status-operational {
            background-color: #24B47E;
            box-shadow: 0 2px 8px rgba(36, 180, 126, 0.15);
        }
        .status-running {
            background: linear-gradient(135deg, #2b6cb0 0%, #3182ce 100%);
            box-shadow: 0 4px 14px rgba(49, 130, 206, 0.25);
        }
        .status-error {
            background-color: #E05252;
            box-shadow: 0 2px 8px rgba(224, 82, 82, 0.15);
        }
        .uptime-bar {
            background-color: #24B47E;
            transition: transform 0.15s ease, opacity 0.15s ease;
        }
        .uptime-bar:hover {
            opacity: 0.8;
            transform: scaleY(1.15);
        }
        .no-scrollbar::-webkit-scrollbar {
            display: none;
        }
    </style>
</head>
<body class="min-h-screen flex flex-col justify-between antialiased">

    <!-- Header -->
    <header class="w-full border-b border-gray-100 bg-white">
        <div class="max-w-[760px] mx-auto px-4 pt-[40px] pb-6 flex justify-between items-center">
            <!-- Left: Logo -->
            <div class="flex items-center">
                <a href="/" class="flex items-center">
                    <img src="/assets/logo.png" alt="Sterling Septic & Plumbing LLC" class="h-10 sm:h-12 w-auto object-contain" onerror="this.onerror=null; this.src='/assets/favicon.png';">
                </a>
            </div>

            <!-- Right: Action Button -->
            <div>
                <button onclick="openModal()" class="bg-[#3182CE] hover:bg-[#2b6cb0] text-white px-4 py-2 rounded text-xs font-medium uppercase tracking-wider transition-colors shadow-sm">
                    Subscribe to Updates
                </button>
            </div>
        </div>
    </header>

    <!-- Main Container -->
    <main class="w-full flex-grow">
        <div class="max-w-[760px] mx-auto px-4 py-8 sm:py-10">

            <!-- Status Banner -->
            <div id="statusBanner" class="status-operational text-white px-5 py-3.5 rounded-md font-medium text-sm sm:text-base flex items-center justify-between shadow-sm transition-all duration-300 relative overflow-hidden">
                <div class="flex items-center gap-3">
                    <svg id="statusIconCheck" class="w-5 h-5 flex-shrink-0 fill-current" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/>
                    </svg>
                    <div class="flex flex-wrap items-center gap-2">
                        <span id="bannerText">All Systems Operational</span>
                        <span id="bannerProgressPill" class="hidden bg-white/20 backdrop-blur-sm text-white text-[11px] px-2.5 py-0.5 rounded-full font-medium"></span>
                    </div>
                </div>
                <span id="bannerPstText" class="text-xs text-white/95 font-medium bg-black/10 px-2.5 py-1 rounded-full whitespace-nowrap">Synced PST</span>
                <div id="bannerProgressBar" class="hidden absolute bottom-0 left-0 h-[3px] bg-white/50 transition-all duration-500" style="width: 0%;"></div>
            </div>

            <!-- About This Site -->
            <section class="mt-8">
                <h2 class="text-base sm:text-lg font-semibold text-gray-900 mb-2">About This Site</h2>
                <p class="text-sm text-gray-600 leading-relaxed text-justify" style="text-align: justify; text-justify: inter-word;">
                    Welcome to Sterling's FieldEdge home for real-time updates on our dispatch automation and asset assignments. Here you'll find live system performance data; any service interruptions or sync notices will be posted here.
                </p>
            </section>

            <!-- Services Breakdown & 90-Day Uptime -->
            <section class="mt-8">
                <div class="border border-gray-200 rounded-md divide-y divide-gray-200 bg-white">
                    <!-- Service 1: Site / Dispatch Board Automation -->
                    <div class="p-4 sm:p-5">
                        <div class="flex justify-between items-center mb-3">
                            <span class="text-sm font-medium text-gray-800">Dispatch Board Display Automation</span>
                            <span id="serviceSiteStatus" class="text-xs font-medium text-[#24B47E]">Operational</span>
                        </div>
                        <div class="flex items-center gap-[2px] h-8 w-full" id="barsSite">
                            <!-- 90 bars generated via JS -->
                        </div>
                        <div class="flex justify-between items-center text-[11px] text-gray-400 mt-2 font-normal">
                            <span>90 days ago</span>
                            <span class="text-gray-500 font-medium">100 % uptime</span>
                            <span>Today</span>
                        </div>
                    </div>

                    <!-- Service 2: Mobile / Asset Assignment Reconciler -->
                    <div class="p-4 sm:p-5">
                        <div class="flex justify-between items-center mb-3">
                            <span class="text-sm font-medium text-gray-800">FieldEdge Asset Assignment Sync</span>
                            <span id="serviceAssetStatus" class="text-xs font-medium text-[#24B47E]">Operational</span>
                        </div>
                        <div class="flex items-center gap-[2px] h-8 w-full" id="barsAsset">
                            <!-- 90 bars generated via JS -->
                        </div>
                        <div class="flex justify-between items-center text-[11px] text-gray-400 mt-2 font-normal">
                            <span>90 days ago</span>
                            <span class="text-gray-500 font-medium">100 % uptime</span>
                            <span>Today</span>
                        </div>
                    </div>

                    <!-- Service 3: FieldEdge Cloud Session -->
                    <div class="p-4 sm:p-5">
                        <div class="flex justify-between items-center mb-3">
                            <span class="text-sm font-medium text-gray-800">FieldEdge Active Cloud Session</span>
                            <span id="serviceSessionStatus" class="text-xs font-medium text-[#24B47E]">Connected</span>
                        </div>
                        <div class="flex items-center gap-[2px] h-8 w-full" id="barsSession">
                            <!-- 90 bars generated via JS -->
                        </div>
                        <div class="flex justify-between items-center text-[11px] text-gray-400 mt-2 font-normal">
                            <span>90 days ago</span>
                            <span class="text-gray-500 font-medium">100 % uptime</span>
                            <span>Today</span>
                        </div>
                    </div>
                </div>
            </section>

            <!-- Live Automation Status Card (Visible when running or completed) -->
            <section class="mt-8 bg-gray-50 border border-gray-200 rounded-md p-4 sm:p-5">
                <div class="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-2 mb-3">
                    <div>
                        <h3 class="text-xs font-medium uppercase tracking-wider text-gray-500">Live Automation Run Info</h3>
                        <p id="liveStatusMsg" class="text-sm font-medium text-gray-800 mt-0.5">Ready for next scheduled cycle</p>
                    </div>
                    <div class="flex items-center gap-2">
                        <span id="liveModeBadge" class="px-2 py-0.5 rounded text-[11px] font-medium bg-blue-50 text-[#3182CE] border border-blue-200">LIVE MODE</span>
                        <span id="livePercentBadge" class="px-2 py-0.5 rounded text-[11px] font-medium bg-green-50 text-green-700 border border-green-200">100%</span>
                    </div>
                </div>
                <!-- Progress bar -->
                <div class="w-full bg-gray-200 rounded-full h-2 overflow-hidden mb-2">
                    <div id="liveProgressBar" class="bg-[#3182CE] h-2 rounded-full transition-all duration-500" style="width: 100%;"></div>
                </div>
                <div class="flex justify-between text-[11px] text-gray-500">
                    <span id="liveDaysInfo">31 Days Configured</span>
                    <span id="liveLastRunInfo">Last run: --</span>
                </div>
            </section>

            <!-- Past Incidents -->
            <section id="past-incidents" class="mt-12">
                <h2 class="text-base sm:text-lg font-semibold text-gray-900 mb-4">Past Incidents</h2>
                <div id="incidentsList" class="space-y-5">
                    <!-- Populated dynamically via JS -->
                </div>
            </section>

        </div>
    </main>

    <!-- Footer -->
    <footer class="w-full border-t border-gray-200 bg-white py-6">
        <div class="max-w-[760px] mx-auto px-4 flex flex-col sm:flex-row justify-between items-center text-xs text-gray-500 gap-4">
            <div>
                <a href="https://dev.sterlingsepticandplumbing.com/status" target="_blank" rel="noopener noreferrer" class="text-[#3182CE] hover:underline font-medium">← Incident History</a>
            </div>
            <div class="flex items-center gap-1.5 text-gray-400">
                <span>Developed by</span>
                <a href="https://github.com/tonmoy-Org" target="_blank" rel="noopener noreferrer" class="text-gray-700 hover:text-[#3182CE] font-medium hover:underline transition-colors">Tanvir Hasan Tonmoy</a>
            </div>
        </div>
    </footer>

    <!-- Interactive Modal -->
    <div id="modalSubscribe" class="fixed inset-0 bg-black/50 z-50 hidden flex items-center justify-center p-4">
        <div class="bg-white rounded-lg max-w-md w-full p-6 shadow-xl relative animate-fade-in">
            <button onclick="closeModal()" class="absolute top-4 right-4 text-gray-400 hover:text-gray-600 text-lg font-medium">&times;</button>
            <h3 class="text-base font-semibold text-gray-900 mb-2">Automation Operations & API</h3>
            <p class="text-xs text-gray-600 mb-4">
                Real-time API endpoints and manual trigger controls for the Sterling FieldEdge Dispatch Board automation.
            </p>
            <div class="space-y-3 text-xs mb-5">
                <div class="p-3 bg-gray-50 rounded border border-gray-200">
                    <div class="font-medium text-gray-700">JSON API Endpoint:</div>
                    <code class="text-[#3182CE] break-all select-all font-mono">/api</code>
                </div>
                <div class="p-3 bg-gray-50 rounded border border-gray-200">
                    <div class="font-medium text-gray-700">Swagger OpenAPI Docs:</div>
                    <a href="/docs" target="_blank" class="text-[#3182CE] hover:underline">Open /docs</a>
                </div>
            </div>
            <div class="flex justify-end gap-2">
                <button onclick="closeModal()" class="px-4 py-2 border border-gray-300 text-gray-700 rounded text-xs font-medium hover:bg-gray-50">Close</button>
                <button onclick="triggerManualRun()" id="btnManualTrigger" class="px-4 py-2 bg-[#3182CE] hover:bg-[#2b6cb0] text-white rounded text-xs font-medium">Trigger Manual Run</button>
            </div>
        </div>
    </div>

    <!-- Script for Live Status Polling & 90-Day Bars -->
    <script>
        function openModal() {
            document.getElementById('modalSubscribe').classList.remove('hidden');
        }
        function closeModal() {
            document.getElementById('modalSubscribe').classList.add('hidden');
        }

        // Generate 90 vertical day bars
        function renderUptimeBars(containerId) {
            const container = document.getElementById(containerId);
            if (!container) return;
            container.innerHTML = '';
            for (let i = 89; i >= 0; i--) {
                const bar = document.createElement('div');
                bar.className = 'flex-1 h-8 rounded-[1px] uptime-bar cursor-pointer relative';
                const date = new Date();
                date.setDate(date.getDate() - i);
                const dateStr = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
                bar.title = `${dateStr}: 100% Operational • 0 Incidents`;
                container.appendChild(bar);
            }
        }

        // Generate past 3 days incident timeline
        function renderIncidentHistory(lastStatus, lastRunPst) {
            const list = document.getElementById('incidentsList');
            if (!list) return;
            list.innerHTML = '';
            const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

            for (let i = 0; i < 3; i++) {
                const date = new Date();
                date.setDate(date.getDate() - i);
                const monthName = months[date.getMonth()];
                const day = date.getDate();
                const year = date.getFullYear();
                const dateHeader = `${monthName} ${day}, ${year}`;

                const item = document.createElement('div');
                item.className = 'pb-4 border-b border-gray-100 last:border-b-0';

                let incidentText = 'No incidents reported.';
                if (i === 0) {
                    if (lastStatus && lastStatus.startsWith('error')) {
                        incidentText = `<span class="text-red-600 font-medium">Notice: ${lastStatus}</span>`;
                    } else if (lastRunPst) {
                        incidentText = `No incidents reported today. Last sync completed at ${lastRunPst}.`;
                    } else {
                        incidentText = 'No incidents reported today.';
                    }
                }

                item.innerHTML = `
                    <h3 class="text-sm font-semibold text-gray-800">${dateHeader}</h3>
                    <p class="text-xs text-gray-600 mt-1 leading-relaxed">${incidentText}</p>
                `;
                list.appendChild(item);
            }
        }

        async function fetchStatus() {
            try {
                const res = await fetch('/api');
                if (!res.ok) return;
                const data = await res.json();
                updateUI(data);
            } catch (err) {
                console.warn('Status poll warning:', err);
            }
        }

        function updateUI(data) {
            const progress = data.progress || {};
            const isRunning = (progress.status === 'running' || data.last_status === 'running');
            const isError = (data.last_status && data.last_status.startsWith('error'));

            const banner = document.getElementById('statusBanner');
            const bannerText = document.getElementById('bannerText');
            const iconCheck = document.getElementById('statusIconCheck');
            const progressPill = document.getElementById('bannerProgressPill');
            const bannerBar = document.getElementById('bannerProgressBar');
            const bannerPstText = document.getElementById('bannerPstText');

            if (data.last_run_pst) {
                bannerPstText.textContent = `PST: ${data.last_run_pst}`;
            }

            if (isRunning) {
                banner.className = 'status-running text-white px-5 py-3.5 rounded-md font-medium text-sm sm:text-base flex items-center justify-between shadow-sm transition-all duration-300 relative overflow-hidden';
                bannerText.textContent = 'Automation in Progress';
                if (progressPill) {
                    progressPill.textContent = `Day ${progress.current_day || 1} of ${progress.total_days || 31} (${Math.round(progress.percent_complete || 0)}%)`;
                    progressPill.classList.remove('hidden');
                }
                iconCheck.classList.add('hidden');
                if (bannerBar) {
                    bannerBar.classList.remove('hidden');
                    bannerBar.style.width = `${progress.percent_complete || 0}%`;
                }
            } else if (isError) {
                banner.className = 'status-error text-white px-5 py-3.5 rounded-md font-medium text-sm sm:text-base flex items-center justify-between shadow-sm transition-all duration-300 relative overflow-hidden';
                bannerText.textContent = 'Service Notice: Recent Sync Needs Attention';
                if (progressPill) progressPill.classList.add('hidden');
                iconCheck.classList.remove('hidden');
                if (bannerBar) bannerBar.classList.add('hidden');
            } else {
                banner.className = 'status-operational text-white px-5 py-3.5 rounded-md font-medium text-sm sm:text-base flex items-center justify-between shadow-sm transition-all duration-300 relative overflow-hidden';
                bannerText.textContent = 'All Systems Operational';
                if (progressPill) progressPill.classList.add('hidden');
                iconCheck.classList.remove('hidden');
                if (bannerBar) bannerBar.classList.add('hidden');
            }

            // Update Live info card
            const msgEl = document.getElementById('liveStatusMsg');
            if (msgEl) msgEl.textContent = progress.status_message || 'Waiting for scheduled cycle...';

            const modeBadge = document.getElementById('liveModeBadge');
            if (modeBadge) modeBadge.textContent = `${data.mode || 'LIVE'} MODE`;

            const percentBadge = document.getElementById('livePercentBadge');
            const pct = Math.round(progress.percent_complete || 0);
            if (percentBadge) percentBadge.textContent = `${pct}%`;

            const bar = document.getElementById('liveProgressBar');
            if (bar) bar.style.width = `${pct}%`;

            const daysInfo = document.getElementById('liveDaysInfo');
            if (daysInfo) daysInfo.textContent = `${data.days_configured || 31} Days Configured (Target: ${progress.current_date || 'Today'})`;

            const lastRunInfo = document.getElementById('liveLastRunInfo');
            if (lastRunInfo && data.last_run_pst) lastRunInfo.textContent = `Last run: ${data.last_run_pst}`;

            // FieldEdge session indicator
            const sessionStatus = document.getElementById('serviceSessionStatus');
            if (sessionStatus && data.fieldedge_status) {
                sessionStatus.textContent = data.fieldedge_status.includes('Active') ? 'Operational' : data.fieldedge_status;
            }

            renderIncidentHistory(data.last_status, data.last_run_pst);
        }

        async function triggerManualRun() {
            const btn = document.getElementById('btnManualTrigger');
            btn.disabled = true;
            btn.textContent = 'Triggering...';
            try {
                const res = await fetch('/api/trigger', { method: 'POST' });
                const json = await res.json();
                alert(json.message || 'Run triggered successfully!');
                closeModal();
                fetchStatus();
            } catch (err) {
                alert('Could not trigger run: ' + err);
            } finally {
                btn.disabled = false;
                btn.textContent = 'Trigger Manual Run';
            }
        }

        // Initialize
        document.addEventListener('DOMContentLoaded', () => {
            renderUptimeBars('barsSite');
            renderUptimeBars('barsAsset');
            renderUptimeBars('barsSession');
            renderIncidentHistory('initialized', '');
            fetchStatus();
            setInterval(fetchStatus, 4000);
        });
    </script>
</body>
</html>
"""

def on_progress_update(progress_dict):
    STATUS_DATA["progress"] = progress_dict
    if isinstance(progress_dict, dict) and "fieldedge_status" in progress_dict:
        STATUS_DATA["fieldedge_status"] = progress_dict["fieldedge_status"]
    STATUS_DATA["last_run_pst"] = get_pst_now().strftime("%Y-%m-%d %I:%M:%S %p PST")


class DefaultArgs:
    days = 31
    live = True
    start_date = None
    loop = True
    interval_hours = 24.0
    port = int(os.getenv("PORT", "3000"))
    skip_asset = False
    force = False


async def run_automation(args):
    STATUS_DATA["mode"] = "LIVE" if args.live else "DRY RUN"
    STATUS_DATA["days_configured"] = args.days
    STATUS_DATA["progress"]["total_days"] = args.days
    STATUS_DATA["progress"]["remaining_days"] = args.days

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
            try:
                await scraper.run(
                    days=args.days,
                    dry_run=not args.live,
                    start_date_str=current_start_date,
                    on_progress=on_progress_update,
                    skip_asset=getattr(args, "skip_asset", False),
                    force=getattr(args, "force", False)
                )

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
            except Exception as e:
                err_str = str(e)
                print(f"❌ Error during scheduled run: {err_str}")
                STATUS_DATA["last_status"] = f"error: {err_str}"

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
                skip_asset=getattr(args, "skip_asset", False),
                force=getattr(args, "force", False)
            )
            STATUS_DATA["last_status"] = "success"
        except Exception as e:
            STATUS_DATA["last_status"] = f"error: {str(e)}"
            raise e


# Lifespan context manager for FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Launch background automation on startup
    args = getattr(app.state, "args", None)
    if not args:
        args = DefaultArgs()
    
    automation_task = asyncio.create_task(run_automation(args))
    app.state.automation_task = automation_task
    print(f"🚀 [FastAPI] Automation background task started.")
    yield
    # Cleanup on shutdown
    if automation_task and not automation_task.done():
        automation_task.cancel()
        try:
            await automation_task
        except asyncio.CancelledError:
            pass


# Create FastAPI App
app = FastAPI(
    title="Sterling Septic Status",
    description="Real-time status and health monitoring for Dispatch Board and Asset Assignment Automation.",
    version="2.0.0",
    lifespan=lifespan
)

# Enable CORS for external dashboards
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """Serves the Atlassian Statuspage-inspired monitoring dashboard."""
    return HTMLResponse(content=HTML_TEMPLATE, status_code=200)


@app.get("/api")
@app.get("/api/status")
async def get_status_json():
    """Returns the current real-time automation status in JSON format."""
    return JSONResponse(content=STATUS_DATA)


@app.get("/health")
async def get_health():
    """Simple healthcheck endpoint."""
    return {"status": "ok", "service": STATUS_DATA["service"], "mode": STATUS_DATA["mode"]}


@app.post("/api/trigger")
async def trigger_run(background_tasks: BackgroundTasks):
    """Trigger an immediate manual automation run."""
    args = getattr(app.state, "args", None) or DefaultArgs()
    if STATUS_DATA["last_status"] == "running":
        return JSONResponse(status_code=409, content={"message": "Automation is already currently running!"})
    
    # Run in background
    async def _manual_run():
        scraper = DispatchBoardDisplayAutomationScraper()
        STATUS_DATA["last_status"] = "running"
        await scraper.run(
            days=args.days,
            dry_run=not args.live,
            start_date_str=args.start_date,
            on_progress=on_progress_update,
            skip_asset=getattr(args, "skip_asset", False),
            force=getattr(args, "force", False)
        )
        STATUS_DATA["last_status"] = "success"

    background_tasks.add_task(_manual_run)
    return {"message": "Manual run initiated successfully in background!"}


@app.get("/assets/{filename}")
async def get_asset(filename: str):
    """Serve logo, favicon, and other assets cleanly."""
    clean_name = os.path.basename(filename)
    candidates = [
        os.path.join(_dispatch_dir, "assets", clean_name),
        os.path.join(_dispatch_dir, clean_name),
        os.path.join(os.getcwd(), "assets", clean_name),
        os.path.join(os.getcwd(), "dispatch", "assets", clean_name)
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return FileResponse(cand)
    return JSONResponse(status_code=404, content={"detail": f"Asset '{clean_name}' not found"})


def main():
    parser = argparse.ArgumentParser(description="Run Dispatch Board Display Automation Scraper (Standalone FastAPI)")
    parser.add_argument("--days", type=int, default=31, help="Number of days to process (default: 31)")
    parser.add_argument("--live", action="store_true", help="Run live on FieldEdge (skips dry-run)")
    parser.add_argument("--start-date", type=str, help="Starting date (e.g. 'August 15', '08/15/2026'). Defaults to today.")
    parser.add_argument("--loop", action="store_true", help="Run continuously on a scheduled interval (e.g. daily)")
    parser.add_argument("--interval-hours", type=float, default=24.0, help="Interval between runs in hours when --loop is set (default: 24)")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "3000")), help="HTTP status server port (default: 3000)")
    parser.add_argument("--skip-asset", action="store_true", help="Skip running the asset assignment tool for each date")
    parser.add_argument("--force", action="store_true", help="Force fresh start: bypass smart skip and verify/re-create all board items")
    args = parser.parse_args()

    app.state.args = args
    STATUS_DATA["port"] = args.port

    print(f"🚀 Starting Dispatch Board Display Automation & Asset Assignment (FastAPI & Uvicorn)...")
    print(f"   Mode: {'LIVE' if args.live else 'DRY RUN'}")
    print(f"   Days: {args.days}")
    print(f"   HTTP Status Port: {args.port} (Dashboard: http://0.0.0.0:{args.port})")
    print(f"   Swagger Docs: http://0.0.0.0:{args.port}/docs")
    print(f"   Asset Tool: {'SKIPPED (--skip-asset)' if getattr(args, 'skip_asset', False) else 'ENABLED (Runs on same date)'}")
    print(f"   Fresh Start (Force): {'ENABLED (--force)' if args.force else 'Disabled'}")
    if args.start_date:
        print(f"   Start Date: {args.start_date}")
    if args.loop:
        print(f"   Loop Schedule: Every {args.interval_hours} hours")

    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
