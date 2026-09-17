#!/usr/bin/env python3
# ==============================================================
# goldilocks.py — Moodle Cloud Monitor v2.5.0
# Single-owner edition: watches YOUR courses, deadlines and
# timetable, plus (privately) YOUR grades — and reports to
# Telegram. State lives in Supabase. Loud when broken.
#
# Major changes vs v2.3.0:
#   - State store: JSONBin -> Supabase (single monitor_state row)
#   - Host fixed: moodle.uod.ac -> moodle.duhokcihan.edu.krd
#   - Multi-user removed (no USERS_CONFIG); grades are admin-only
#   - CHAT_ID + IGNORE_COURSES come from repo Variables
#   - QUIET_RUN=1: preview mode — alerts go to admin only (group skipped)
#   - Loud death: aborts alert the admin and exit non-zero
#   - Per-scanner rollback: one broken scanner no longer eats
#     the others' alerts (state is restored, not silently saved)
#   - No silent fallbacks for missing config, ever
#   - Download links keep the Moodle token (owner's call; group is trusted)
#   - v2.5.1: deletion patience (2 consecutive runs) + silent calendar cleanup
#   - Explicit User-Agent on all requests (Cloudflare-friendly)
# ==============================================================

import asyncio
import aiohttp
import os
import socket
import time
import datetime
import json
import html
import sys
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

__version__ = "2.5.1"

# ==========================================
# 1. SETUP & CONFIGURATION (no silent fallbacks)
# ==========================================
MOODLE_RAW    = os.environ.get("MOODLE_API_URL", "").strip()
API_TOKEN     = os.environ.get("MOODLE_API_TOKEN", "").strip()
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
TIMETABLE_URL = os.environ.get("TIMETABLE_URL", "").strip()
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY", "").strip()
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()
GH_TOKEN      = os.environ.get("GH_TOKEN", "").strip()
REPO_SLUG     = os.environ.get("GITHUB_REPOSITORY", "").strip()
RUN_MODE      = os.environ.get("RUN_MODE", "").strip() or "scan"
QUIET_RUN     = os.environ.get("QUIET_RUN", "").strip().lower() in ("1", "true", "yes", "on")

SITE_URL   = MOODLE_RAW.rstrip("/")
MOODLE_URL = f"{SITE_URL}/webservice/rest/server.php" if SITE_URL else ""

# Recipients: exactly the IDs listed in the repo Variable. Nobody else, ever.
CHAT_IDS = [c.strip() for c in os.environ.get("CHAT_ID", "").split(",") if c.strip()]

# IGNORE_COURSES: course IDs to skip. Non-numeric junk and the "0" placeholder are dropped.
IGNORE_COURSES = [c.strip() for c in os.environ.get("IGNORE_COURSES", "").split(",")
                  if c.strip().isdigit() and c.strip() != "0"]

TELEGRAM_URL = f"https://moodle-tele-proxy.fy20155.workers.dev/bot{BOT_TOKEN}/sendMessage"

UA = "MoodleMonitor/2.5"
IRAQ_TZ = ZoneInfo("Asia/Baghdad")

# Per-run reasons for strike alerts ("token rejected" vs "site down")
SERVER_NOTES = []

def config_problems():
    required = {
        "MOODLE_API_URL": MOODLE_RAW,
        "MOODLE_API_TOKEN": API_TOKEN,
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "ADMIN_CHAT_ID": ADMIN_CHAT_ID,
        "TIMETABLE_URL": TIMETABLE_URL,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
    }
    return [k for k, v in required.items() if not v]

# ==========================================
# 2. NETWORK HELPERS
# ==========================================
def safe_html(text):
    if not text: return ""
    clean_text = html.unescape(str(text))
    return clean_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def fetch_data(session, url, post_data=None, json_body=None, return_json=True, headers=None):
    h = {"User-Agent": UA}
    if headers: h.update(headers)
    if return_json: h["Accept"] = "application/json"
    if json_body is not None:
        async with session.post(url, json=json_body, headers=h, timeout=20) as resp:
            resp.raise_for_status()
            return await resp.json() if return_json else await resp.text()
    if post_data:
        async with session.post(url, data=post_data, headers=h, timeout=20) as resp:
            resp.raise_for_status()
            return await resp.json() if return_json else await resp.text()
    async with session.get(url, headers=h, timeout=20) as resp:
        resp.raise_for_status()
        return await resp.json() if return_json else await resp.text()

async def send_telegram(session, message, target_chat):
    """Send one message. Failures are logged — never silent."""
    payload = {"chat_id": target_chat, "text": message, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    try:
        await fetch_data(session, TELEGRAM_URL, post_data=payload)
        await asyncio.sleep(1)
        return True
    except Exception as e:
        print(f"⚠️ Telegram send failed (chat {target_chat}): {type(e).__name__}: {e}")
        return False

async def send_alert_dedup(session, text):
    """Admin alert with de-dupe: if the previous run also failed, stay quiet (no spam)."""
    suppress = False
    if GH_TOKEN and REPO_SLUG:
        try:
            url = f"https://api.github.com/repos/{REPO_SLUG}/actions/runs?per_page=5"
            runs = await fetch_data(session, url, headers={"Authorization": f"token {GH_TOKEN}"})
            cur = os.environ.get("GITHUB_RUN_ID", "")
            prev = [r for r in runs.get("workflow_runs", []) if str(r.get("id")) != cur]
            if prev and prev[0].get("conclusion") == "failure":
                suppress = True
        except Exception as e:
            print(f"⚠️ Alert de-dupe check failed: {e}")
    if suppress:
        print("🔇 Duplicate alert suppressed (previous run already failed).")
        return
    if BOT_TOKEN and ADMIN_CHAT_ID:
        await send_telegram(session, text, ADMIN_CHAT_ID)
    else:
        print("⚠️ Telegram not configured — alert not sent to admin.")

async def abort_run(session, reason):
    """Loud death: alert the admin, then exit non-zero."""
    print(f"🛑 ABORT: {reason}")
    await send_alert_dedup(session, f"🚨 <b>MONITOR ABORTED</b>\n{reason}\n\nNothing was scanned; state is untouched.")
    raise SystemExit(1)

# ==========================================
# 3. CLOUD MEMORY (SUPABASE)
# ==========================================
DEFAULT_MEMORY_KEYS = {"grades": {}, "files": {}, "timetable": {}, "deadlines": {},
                       "server_status": "ok"}

async def load_memory(session):
    """Returns (memory, error). memory=None means: do not run (protect the state)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None, "Supabase config missing (SUPABASE_URL / SUPABASE_KEY)"
    url = f"{SUPABASE_URL}/rest/v1/monitor_state?id=eq.1&select=data"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    err = "unknown error"
    for attempt in (1, 2):
        try:
            rows = await fetch_data(session, url, headers=headers)
            if isinstance(rows, list) and rows:
                data = rows[0].get("data") or {}
                if not isinstance(data, dict): data = {}
                for k, v in DEFAULT_MEMORY_KEYS.items():
                    data.setdefault(k, v)
                return data, None
            print("🆕 Store reachable but row empty — starting fresh memory.")
            return json.loads(json.dumps(DEFAULT_MEMORY_KEYS)), None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"⚠️ Store load failed (attempt {attempt}/2): {err}")
            await asyncio.sleep(3)
    return None, err

async def save_memory(session, memory):
    """Upsert the single state row. Returns True/False."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("⚠️ save_memory: Supabase not configured.")
        return False
    url = f"{SUPABASE_URL}/rest/v1/monitor_state"
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
               "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    payload = [{"id": 1, "data": memory,
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}]
    for attempt in (1, 2):
        try:
            await fetch_data(session, url, json_body=payload, return_json=False, headers=headers)
            return True
        except Exception as e:
            print(f"⚠️ Store save failed (attempt {attempt}/2): {type(e).__name__}: {e}")
            await asyncio.sleep(3)
    print("❌ STORE SAVE FAILED — this run's state was not persisted.")
    return False

# ==========================================
# 4. DEADLINES & CALENDAR SCANNER
# ==========================================
async def scan_deadlines(memory, notifications, session):
    print("⏳ Scanning Assignments & Deadlines...")
    updates_found = False
    fetched_event_ids = set()
    current_time = int(time.time())

    try:
        assign_data = await fetch_data(session, MOODLE_URL, post_data={
            "wstoken": API_TOKEN, "wsfunction": "mod_assign_get_assignments",
            "moodlewsrestformat": "json"
        })
        if isinstance(assign_data, dict) and "exception" in assign_data:
            SERVER_NOTES.append("deadlines call failed: " + str(assign_data.get("message", "unknown error")))
            print(f"🛑 Deadlines call failed: {assign_data.get('message')}")
            return False, False

        if isinstance(assign_data, dict) and "courses" in assign_data:
            for course_obj in assign_data["courses"]:
                course_id = str(course_obj.get("id"))
                if course_id in IGNORE_COURSES: continue
                course_name = safe_html(course_obj.get("fullname", "Unknown Course"))

                for assign in course_obj.get("assignments", []):
                    assign_id = "assign_" + str(assign.get("id"))
                    fetched_event_ids.add(assign_id)
                    event_name = safe_html(assign.get("name", "Unknown Assignment"))
                    timestamp = assign.get("duedate", 0)

                    old_data = memory["deadlines"].get(assign_id)
                    old_timestamp = old_data.get("timestamp") if isinstance(old_data, dict) else old_data

                    # RULE 1: Brand new assignment
                    if old_timestamp is None:
                        updates_found = True
                        if timestamp > current_time or timestamp == 0:
                            if timestamp > 0:
                                dt = datetime.datetime.fromtimestamp(timestamp, tz=IRAQ_TZ)
                                date_str = dt.strftime("%A, %b %d at %I:%M %p")
                            else:
                                date_str = "Open-Ended (No Due Date)"
                            notifications.append(f"🚨 <b>NEW ASSIGNMENT ADDED</b>\n📚 {course_name}\n📝 {event_name}\n⏰ Due: {date_str}")
                        memory["deadlines"][assign_id] = {"timestamp": timestamp, "name": event_name, "course": course_name}

                    # RULE 2: Deadline extended
                    elif timestamp > old_timestamp:
                        updates_found = True
                        extended_by_seconds = timestamp - old_timestamp
                        days = extended_by_seconds // 86400
                        hours = (extended_by_seconds % 86400) // 3600
                        if days > 0 and hours > 0: time_added = f"{days} day(s) and {hours} hour(s)"
                        elif days > 0: time_added = f"{days} day(s)"
                        elif hours > 0: time_added = f"{hours} hour(s)"
                        else: time_added = "less than an hour"
                        dt = datetime.datetime.fromtimestamp(timestamp, tz=IRAQ_TZ)
                        date_str = dt.strftime("%A, %b %d at %I:%M %p")
                        notifications.append(f"⏰ <b>DEADLINE EXTENDED</b>\n📚 {course_name}\n📝 {event_name}\n⏳ Extended by: {time_added}\n📅 Current Deadline: {date_str}")
                        memory["deadlines"][assign_id] = {"timestamp": timestamp, "name": event_name, "course": course_name}

                    # Silently update memory if shortened
                    elif timestamp != old_timestamp and timestamp < old_timestamp:
                        updates_found = True
                        memory["deadlines"][assign_id] = {"timestamp": timestamp, "name": event_name, "course": course_name}

        # Scan Calendar for non-assignment events (limitnum raised so busy weeks aren't cut off)
        cal_data = await fetch_data(session, MOODLE_URL, post_data={
            "wstoken": API_TOKEN, "wsfunction": "core_calendar_get_action_events_by_timesort",
            "moodlewsrestformat": "json", "timesortfrom": current_time, "limitnum": 50
        })
        if isinstance(cal_data, dict) and "exception" in cal_data:
            SERVER_NOTES.append("calendar call failed: " + str(cal_data.get("message", "unknown error")))
            print(f"🛑 Calendar call failed: {cal_data.get('message')}")
            return False, False

        if isinstance(cal_data, dict) and "events" in cal_data:
            for event in cal_data["events"]:
                if event.get("modulename") == "assign": continue
                event_id = "cal_" + str(event.get("id"))
                fetched_event_ids.add(event_id)
                event_name = safe_html(event.get("name", "Unknown Event"))
                course_name = safe_html(event.get("course", {}).get("fullname", "Unknown Course"))
                timestamp = event.get("timesort")

                old_data = memory["deadlines"].get(event_id)
                old_timestamp = old_data.get("timestamp") if isinstance(old_data, dict) else old_data

                if old_timestamp != timestamp:
                    updates_found = True
                    dt = datetime.datetime.fromtimestamp(timestamp, tz=IRAQ_TZ)
                    date_str = dt.strftime("%A, %b %d at %I:%M %p")
                    if old_timestamp is None:
                        notifications.append(f"🚨 <b>UPCOMING EVENT</b>\n📚 {course_name}\n📝 {event_name}\n⏰ Due: {date_str}")
                    else:
                        notifications.append(f"⚠️ <b>EVENT TIME CHANGED</b>\n📚 {course_name}\n📝 {event_name}\n⏰ New Date: {date_str}")
                    memory["deadlines"][event_id] = {"timestamp": timestamp, "name": event_name, "course": course_name}

        # RULE 3 & RULE 4: DELETIONS (with patience) & SILENCE
        delete_keys = []
        for event_id, event_data in list(memory["deadlines"].items()):
            if event_id in fetched_event_ids:
                continue
            if not isinstance(event_data, dict):
                delete_keys.append(event_id)  # legacy format — drop silently
                continue
            is_assign = str(event_id).startswith("assign_")

            # Patience: never trust a single missing run (flickers happen).
            miss = event_data.get("miss", 0)
            if miss < 2:
                miss += 1
                event_data["miss"] = miss
                updates_found = True
            if miss < 2:
                continue

            if is_assign:
                ts = event_data.get("timestamp", 0)
                # Rule 3: removed before the deadline was over
                if ts > current_time or ts == 0:
                    notifications.append(f"🗑️ <b>TASK DELETED</b>\n📚 {event_data.get('course','Unknown Course')}\n📝 {event_data.get('name','Unknown Task')}\n🚨 The professor has removed it before the deadline!")
                    delete_keys.append(event_id)
                # Rule 4: deadline over — silent cleanup after 7 days
                elif ts < (current_time - 604800):
                    delete_keys.append(event_id)
            else:
                # Calendar-feed items: clean up silently — the feed's composition
                # changes for non-deletion reasons; never cry wolf for these.
                delete_keys.append(event_id)

        for key in delete_keys:
            del memory["deadlines"][key]
        if delete_keys:
            updates_found = True

        # Items seen this run: clear any patience counter
        for fid in fetched_event_ids:
            d = memory["deadlines"].get(fid)
            if isinstance(d, dict) and d.get("miss"):
                d["miss"] = 0
                updates_found = True

        return updates_found, True
    except Exception as e:
        SERVER_NOTES.append("deadlines scan crashed")
        print(f"🚨 Task Scan Crash: {e}")
        return False, False

# ==========================================
# 5. TIMETABLE SCANNER
# ==========================================
def parse_timetable(page_html):
    soup = BeautifulSoup(page_html, 'html.parser')
    tables = soup.find_all('table')
    if not tables: return {}

    table = tables[0]
    rows = table.find_all('tr')
    if not rows: return {}

    headers = [th.get_text(" ", strip=True) for th in rows[0].find_all(['th', 'td'])]
    timetable_data = {}

    for i in range(1, len(rows)):
        r1_cells = rows[i].find_all(['td', 'th'])
        first_cell = r1_cells[0].get_text(strip=True).lower()
        if first_cell in ['saturday', 'sunday', 'monday', 'tuesday', 'wednsday', 'wednesday', 'thursday', 'friday']:
            day_name = r1_cells[0].get_text(" ", strip=True)
            r2_cells = rows[i+1].find_all(['td', 'th']) if i+1 < len(rows) else []
            for j in range(1, len(r1_cells)):
                time_slot = headers[j] if j < len(headers) else f"Slot_{j}"
                subj = r1_cells[j].get_text(" ", strip=True)
                instructor = r2_cells[j-1].get_text(" ", strip=True) if j-1 < len(r2_cells) else ""
                box_id = f"{day_name} ({time_slot})"
                content = f"{subj} / {instructor}".strip(' /')
                if not content: content = "Rest Hour"
                timetable_data[box_id] = content
    return timetable_data

async def scan_timetable(memory, notifications, session):
    print("📅 Scanning Timetable...")
    try:
        page_html = await fetch_data(session, TIMETABLE_URL, return_json=False)
        new_timetable = parse_timetable(page_html)
        old_timetable = memory["timetable"]
        updates_found = False

        for box_id, new_content in new_timetable.items():
            old_content = old_timetable.get(box_id)
            if old_content != new_content:
                memory["timetable"][box_id] = new_content
                updates_found = True
                try:
                    day, time_str = box_id.split(' (', 1)
                    time_str = time_str.rstrip(')')
                except ValueError:
                    day = "Unknown Day"
                    time_str = box_id

                new_c = safe_html(new_content)
                old_c = safe_html(old_content)

                if old_content is None:
                    if new_content != "Rest Hour":
                        notifications.append(f"📅 <b>CLASS TRACKED</b>\n📆 Day: {day}\n⏰ Time: {time_str}\n✅ {new_c}")
                else:
                    if new_content == "Rest Hour":
                        notifications.append(f"🛏️ <b>CLASS CANCELLED</b>\n📆 Day: {day}\n⏰ Time: {time_str}\n❌ Old: {old_c}\n✅ New: Rest Hour")
                    elif old_content == "Rest Hour":
                        notifications.append(f"🚨 <b>NEW CLASS ADDED</b>\n📆 Day: {day}\n⏰ Time: {time_str}\n❌ Old: Rest Hour\n✅ New: {new_c}")
                    else:
                        notifications.append(f"⚠️ <b>TIMETABLE CHANGED</b>\n📆 Day: {day}\n⏰ Time: {time_str}\n❌ Old: {old_c}\n✅ New: {new_c}")
        return updates_found, True
    except Exception as e:
        SERVER_NOTES.append("timetable unreachable or layout changed")
        print(f"🚨 Timetable Scan Crash: {e}")
        return False, False

# ==========================================
# 6. GLOBAL MOODLE FILES SCANNER
# ==========================================
def format_file_name(mod_name, mod_type):
    name_lower = mod_name.lower()
    if mod_type == "url": return f"🔗 Web Link: {mod_name}"
    if mod_type == "folder": return f"📁 Folder: {mod_name}"
    if mod_type == "assign": return f"📥 Assignment: {mod_name}"
    if any(ext in name_lower for ext in [".pdf"]): return f"📄 PDF: {mod_name}"
    if any(ext in name_lower for ext in [".ppt", ".pptx"]): return f"📊 Presentation: {mod_name}"
    if any(ext in name_lower for ext in [".doc", ".docx"]): return f"📝 Word Doc: {mod_name}"
    return f"📄 File: {mod_name}"

def format_iraq_time(timestamp):
    if not timestamp or timestamp == 0: return ""
    dt = datetime.datetime.fromtimestamp(timestamp, tz=IRAQ_TZ)
    return f"\n⏳ Modified: {dt.strftime('%A, %b %d, %Y at %I:%M %p')}"

async def scan_moodle(memory, notifications, session):
    print("🔍 Scanning Moodle for files...")
    updates_found = False

    try:
        user_data = await fetch_data(session, MOODLE_URL, post_data={
            "wstoken": API_TOKEN, "wsfunction": "core_webservice_get_site_info", "moodlewsrestformat": "json"
        })
        if not user_data or "exception" in user_data:
            SERVER_NOTES.append("Moodle token rejected")
            print(f"🛑 Moodle Token Rejected: {user_data}")
            return False, False

        user_id = user_data.get("userid")
        if not user_id:
            SERVER_NOTES.append("Moodle: no user id")
            return False, False

        courses = await fetch_data(session, MOODLE_URL, post_data={
            "wstoken": API_TOKEN, "wsfunction": "core_enrol_get_users_courses",
            "moodlewsrestformat": "json", "userid": user_id
        })
        if not isinstance(courses, list):
            SERVER_NOTES.append("Moodle: course list failed")
            return False, False

        for course in courses:
            course_id = str(course['id'])
            course_name = safe_html(course['fullname'])
            if course_id in IGNORE_COURSES: continue

            if course_id not in memory["files"]: memory["files"][course_id] = {}
            elif isinstance(memory["files"][course_id], list):
                memory["files"][course_id] = {str(mid): 0 for mid in memory["files"][course_id]}

            try:
                content_data = await fetch_data(session, MOODLE_URL, post_data={
                    "wstoken": API_TOKEN, "wsfunction": "core_course_get_contents",
                    "moodlewsrestformat": "json", "courseid": course_id
                })
                fetched_mod_ids = set()

                if isinstance(content_data, list):
                    for section in content_data:
                        section_name = safe_html(section.get("name", "General Topic"))
                        for mod in section.get("modules", []):
                            mod_id = str(mod.get("id"))
                            fetched_mod_ids.add(mod_id)
                            mod_name = mod.get("name", "Unknown File")
                            mod_type = mod.get("modname", "resource")

                            time_modified = 0
                            fileurl = ""
                            if "contents" in mod and len(mod["contents"]) > 0:
                                time_modified = mod["contents"][0].get("timemodified", 0)
                                fileurl = mod["contents"][0].get("fileurl", "")
                            elif "url" in mod:
                                fileurl = mod["url"]

                            # Download links keep the Moodle token — owner's informed choice.
                            html_link = ""
                            if fileurl:
                                if "webservice/pluginfile.php" in fileurl:
                                    direct_url = fileurl + (f"&token={API_TOKEN}" if "?" in fileurl else f"?token={API_TOKEN}")
                                    html_link = f"\n⬇️ <a href='{direct_url}'>Tap to Download File</a>"
                                else:
                                    html_link = f"\n🌐 <a href='{fileurl}'>Tap to Open Link</a>"

                            old_data = memory["files"][course_id].get(mod_id)
                            old_time = old_data.get("time", 0) if isinstance(old_data, dict) else (old_data if old_data is not None else None)
                            formatted_name = safe_html(format_file_name(mod_name, mod_type))

                            if old_time is None:
                                memory["files"][course_id][mod_id] = {"time": time_modified, "name": formatted_name}
                                updates_found = True
                                notifications.append(f"📢 <b>NEW CONTENT:</b> {course_name}\n📂 Topic: {section_name}\n{formatted_name}{html_link}")
                            elif time_modified != old_time:
                                memory["files"][course_id][mod_id] = {"time": time_modified, "name": formatted_name}
                                updates_found = True
                                date_str = format_iraq_time(time_modified)
                                notifications.append(f"🔄 <b>FILE UPDATED:</b> {course_name}\n📂 Topic: {section_name}\n{formatted_name}{date_str}{html_link}")

                    missing_files = []
                    for old_mod_id, old_mod_data in list(memory["files"][course_id].items()):
                        if old_mod_id not in fetched_mod_ids:
                            fname = old_mod_data.get("name", "Unknown File") if isinstance(old_mod_data, dict) else "Unknown File"
                            missing_files.append((old_mod_id, fname))

                    for missing_id, missing_name in missing_files:
                        notifications.append(f"🗑️ <b>FILE REMOVED:</b> {course_name}\n{missing_name}\n✅ The professor has deleted this file.")
                        del memory["files"][course_id][missing_id]
                        updates_found = True

            except Exception as e:
                print(f"⚠️ Internal File Sync Error for course {course_id}: {e}")

        return updates_found, True
    except Exception as e:
        SERVER_NOTES.append("Moodle scan crashed")
        print(f"🚨 Global Moodle Scan Crash: {e}")
        return False, False

# ==========================================
# 7. PRIVATE GRADES SCANNER (admin only)
# ==========================================
async def scan_grades(memory, notifications, session):
    print("🎓 Scanning grades (private, admin-only)...")
    updates_found = False

    try:
        user_data = await fetch_data(session, MOODLE_URL, post_data={
            "wstoken": API_TOKEN, "wsfunction": "core_webservice_get_site_info", "moodlewsrestformat": "json"
        })
        if not user_data or "exception" in user_data:
            SERVER_NOTES.append("grades: token rejected")
            print(f"🛑 Grades Token Rejected: {user_data}")
            return False, False

        user_id = user_data.get("userid")
        courses = await fetch_data(session, MOODLE_URL, post_data={
            "wstoken": API_TOKEN, "wsfunction": "core_enrol_get_users_courses",
            "moodlewsrestformat": "json", "userid": user_id
        })
        if not isinstance(courses, list):
            SERVER_NOTES.append("grades: course list failed")
            return False, False

        for course in courses:
            course_id = str(course['id'])
            course_name = safe_html(course['fullname'])
            if course_id in IGNORE_COURSES: continue

            if course_id not in memory["grades"]:
                memory["grades"][course_id] = {}

            try:
                grade_data = await fetch_data(session, MOODLE_URL, post_data={
                    "wstoken": API_TOKEN, "wsfunction": "gradereport_user_get_grade_items",
                    "moodlewsrestformat": "json", "courseid": course_id, "userid": user_id
                })
            except Exception as e:
                print(f"⚠️ Grades fetch failed for course {course_id}: {e}")
                continue

            if isinstance(grade_data, dict) and "usergrades" in grade_data and len(grade_data["usergrades"]) > 0:
                fetched_grade_items = set()
                for item in grade_data["usergrades"][0].get("gradeitems", []):
                    raw_name = item.get("itemname")
                    raw_grade = item.get("gradeformatted")
                    if not raw_name or not raw_grade or str(raw_grade).strip() in ["", "-", "None"]:
                        continue
                    item_name = safe_html(raw_name)
                    grade_val = safe_html(raw_grade)
                    fetched_grade_items.add(item_name)
                    old_grade = memory["grades"][course_id].get(item_name)

                    if old_grade != grade_val:
                        memory["grades"][course_id][item_name] = grade_val
                        updates_found = True
                        if old_grade is None:
                            notifications.append(f"🌟 <b>NEW GRADE:</b> {course_name}\n📝 {item_name}\n✅ Score: <b>{grade_val}</b>")
                        else:
                            notifications.append(f"⚠️ <b>GRADE UPDATED:</b> {course_name}\n📝 {item_name}\n❌ Old: {old_grade}\n✅ New: <b>{grade_val}</b>")

                for old_item_name in list(memory["grades"][course_id].keys()):
                    if old_item_name not in fetched_grade_items:
                        notifications.append(f"🗑️ <b>GRADE REMOVED:</b> {course_name}\n📝 {old_item_name}\n✅ The professor has deleted this grade from the portal.")
                        del memory["grades"][course_id][old_item_name]
                        updates_found = True

        return updates_found, True
    except Exception as e:
        SERVER_NOTES.append("grades scan crashed")
        print(f"🚨 Grades Scan Crash: {e}")
        return False, False

# ==========================================
# 8. MESSAGE HELPERS + MAIN
# ==========================================
def chunk_messages(notifs):
    messages_to_send = []
    current_msg = f"🤖 <b>Moodle Monitor v{__version__}</b>\n\n"
    separator = "\n\n〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"
    for notif in notifs:
        if len(current_msg) + len(notif) + len(separator) > 3900:
            messages_to_send.append(current_msg)
            current_msg = f"🤖 <b>Moodle Monitor v{__version__} (Cont.)</b>\n\n" + notif
        else:
            if current_msg.endswith("\n\n"): current_msg += notif
            else: current_msg += separator + notif
    messages_to_send.append(current_msg)
    return messages_to_send

async def run_list_courses(session):
    print("📚 list-courses mode")
    try:
        info = await fetch_data(session, MOODLE_URL, post_data={
            "wstoken": API_TOKEN, "wsfunction": "core_webservice_get_site_info", "moodlewsrestformat": "json"})
        if "exception" in info:
            raise RuntimeError(str(info.get("message")))
        courses = await fetch_data(session, MOODLE_URL, post_data={
            "wstoken": API_TOKEN, "wsfunction": "core_enrol_get_users_courses",
            "moodlewsrestformat": "json", "userid": info.get("userid")})
        lines = [f"📚 <b>Course list</b> — {safe_html(info.get('fullname', '?'))} (id {info.get('userid')})"]
        for c in sorted(courses, key=lambda x: x["id"]):
            end = c.get("enddate") or 0
            end_s = datetime.datetime.fromtimestamp(end, tz=IRAQ_TZ).strftime("%Y-%m-%d") if end else "no end date"
            lines.append(f"• <code>{c['id']}</code> — {safe_html(c.get('fullname',''))} (ends {end_s})")
        msg = "\n".join(lines)
        print(msg.replace("<code>", "").replace("</code>", "").replace("<b>", "").replace("</b>", ""))
        if BOT_TOKEN and ADMIN_CHAT_ID:
            await send_telegram(session, msg, ADMIN_CHAT_ID)
    except Exception as e:
        print(f"🚨 list-courses failed: {e}")

async def main():
    print(f"🤖 Moodle Cloud Monitor v{__version__} | mode={RUN_MODE} | preview={QUIET_RUN} | "
          f"{datetime.datetime.now(IRAQ_TZ).strftime('%Y-%m-%d %H:%M')} (Iraq)")
    connector = aiohttp.TCPConnector(family=socket.AF_INET)  # TLS verification is ON
    async with aiohttp.ClientSession(connector=connector) as session:
        problems = config_problems()
        if problems:
            await abort_run(session, "Missing configuration: " + ", ".join(problems))

        if RUN_MODE == "list-courses":
            await run_list_courses(session)
            return

        if not CHAT_IDS:
            print("⚠️ CHAT_ID is empty — group broadcasts will go nowhere.")

        memory, err = await load_memory(session)
        if memory is None:
            await abort_run(session, f"Cloud state unreachable ({err}). Refusing to run blind (database lock).")

        grade_notifications = []  # -> admin only

        # Snapshot each scanner's section so a failed scanner can't half-save state
        snap = {
            "files": json.loads(json.dumps(memory["files"])),
            "deadlines": json.loads(json.dumps(memory["deadlines"])),
            "timetable": json.loads(json.dumps(memory["timetable"])),
            "grades": json.loads(json.dumps(memory["grades"])),
        }

        n_moodle, n_timetable, n_deadlines = [], [], []
        results = await asyncio.gather(
            scan_moodle(memory, n_moodle, session),
            scan_timetable(memory, n_timetable, session),
            scan_deadlines(memory, n_deadlines, session),
            scan_grades(memory, grade_notifications, session),
            return_exceptions=True,
        )

        def unpack(r):
            return r if isinstance(r, tuple) and len(r) == 2 else (False, False)
        moodle_updated, moodle_ok = unpack(results[0])
        timetable_updated, timetable_ok = unpack(results[1])
        deadlines_updated, deadlines_ok = unpack(results[2])
        grades_updated, grades_ok = unpack(results[3])

        failures = []
        if not moodle_ok:
            failures.append("course files"); memory["files"] = snap["files"]
        if not timetable_ok:
            failures.append("timetable"); memory["timetable"] = snap["timetable"]
        if not deadlines_ok:
            failures.append("deadlines"); memory["deadlines"] = snap["deadlines"]
        if not grades_ok:
            failures.append("grades"); memory["grades"] = snap["grades"]
        if failures:
            print(f"⚠️ Scanners failed: {', '.join(failures)} — their state was left untouched (nothing lost).")

        any_updates = moodle_updated or timetable_updated or deadlines_updated or grades_updated
        all_servers_ok = moodle_ok and timetable_ok and deadlines_ok and grades_ok
        memory_changed = False

        if not all_servers_ok:
            current_status = memory.get("server_status", "ok")
            cause = (" — cause: " + "; ".join(dict.fromkeys(SERVER_NOTES))) if SERVER_NOTES else ""
            if current_status == "ok":
                print("⚠️ Some services missed a beat (Strike 1).")
                memory["server_status"] = "warning"
                memory_changed = True
            elif current_status == "warning":
                await send_alert_dedup(session, f"🚨 <b>SYSTEM ALERT</b>\nSome university services failed two scans in a row{cause}. I'll stay quiet about this until they recover.")
                memory["server_status"] = "failed"
                memory_changed = True
            else:
                print("❌ Services still down. Remaining silent.")
        else:
            current_status = memory.get("server_status", "ok")
            if current_status in ["failed", "warning"]:
                if current_status == "failed" and BOT_TOKEN and ADMIN_CHAT_ID:
                    await send_telegram(session, "✅ <b>CONNECTION RESTORED</b>\nThe university services are back online.", ADMIN_CHAT_ID)
                memory["server_status"] = "ok"
                memory_changed = True
                print("✅ Services recovered.")

        # Broadcasts — healthy scanners only
        group_notifs = (n_moodle if moodle_ok else []) + (n_timetable if timetable_ok else []) + (n_deadlines if deadlines_ok else [])
        if group_notifs:
            if QUIET_RUN:
                # Preview mode: the group is skipped; everything comes to YOU to check the format.
                chunks = chunk_messages(group_notifs)
                chunks[0] += "\n\n🔍 <i>Preview mode — these normally go to the group.</i>"
                for msg in chunks:
                    await send_telegram(session, msg, ADMIN_CHAT_ID)
                memory_changed = True
                print(f"🛰️ PREVIEW: sent {len(group_notifs)} update(s) to admin only (group skipped).")
            else:
                for msg in chunk_messages(group_notifs):
                    for target in CHAT_IDS:
                        await send_telegram(session, msg, target)
                memory_changed = True
                print(f"✅ Broadcasted {len(group_notifs)} update(s) to {len(CHAT_IDS)} chat(s).")
        if grade_notifications and grades_ok:
            for msg in grade_notifications:
                await send_telegram(session, msg, ADMIN_CHAT_ID)
            memory_changed = True
            print(f"🎓 Sent {len(grade_notifications)} grade notice(s) to admin.")

        if memory_changed or any_updates:
            if await save_memory(session, memory):
                print("☁️ State saved to Supabase.")
        else:
            print("🛑 No changes — skipped saving (no wasted request).")

        if HEALTHCHECK_URL:
            try:
                await fetch_data(session, HEALTHCHECK_URL, return_json=False)
                print("❤️ Healthcheck ping sent.")
            except Exception as e:
                print(f"⚠️ Healthcheck ping failed: {e}")

        print("✅ Run complete.")

if __name__ == "__main__":
    asyncio.run(main())
