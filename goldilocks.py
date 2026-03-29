import asyncio
import aiohttp
import os
import socket
import time
import datetime
import json
from bs4 import BeautifulSoup

__version__ = "2.2.0" # The Multi-Tenant JSON Update


# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
IGNORE_COURSES = ["195", "196", "197", "198", "199", "200", "201"]

API_TOKEN = os.environ.get("MOODLE_API_TOKEN")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID") 
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID") 
RAW_URL = os.environ.get("MOODLE_API_URL", "https://moodle.uod.ac")

JSONBIN_ID = os.environ.get("JSONBIN_ID")
JSONBIN_KEY = os.environ.get("JSONBIN_KEY")

TIMETABLE_URL = "https://tb.duhokcihan.edu.krd/departtimtable.php?departmentNo=11&ClassNo1=1&GroupNo1=1"

if RAW_URL and not RAW_URL.endswith('/webservice/rest/server.php'):
    MOODLE_URL = RAW_URL.rstrip('/') + '/webservice/rest/server.php'
else:
    MOODLE_URL = RAW_URL

TELEGRAM_URL = f"https://moodle-tele-proxy.fy20155.workers.dev/bot{BOT_TOKEN}/sendMessage"
TELEGRAM_UPDATES_URL = f"https://moodle-tele-proxy.fy20155.workers.dev/bot{BOT_TOKEN}/getUpdates"

def safe_html(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ==========================================
# 2. NETWORK HELPER
# ==========================================
async def fetch_data(session, url, is_moodle=False, post_data=None, return_json=True):
    headers = {}
    if return_json: headers["Accept"] = "application/json"
    if is_moodle: headers["User-Agent"] = "MoodleMobile"
        
    if post_data:
        async with session.post(url, data=post_data, headers=headers, timeout=15) as resp:
            resp.raise_for_status() 
            return await resp.json() if return_json else await resp.text()
    else:
        async with session.get(url, headers=headers, timeout=15) as resp:
            resp.raise_for_status()
            return await resp.json() if return_json else await resp.text()

async def send_telegram(session, message, target_chat):
    payload = {
        "chat_id": target_chat, 
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        await fetch_data(session, TELEGRAM_URL, post_data=payload, return_json=True)
        await asyncio.sleep(1) 
    except Exception:
        pass 

# ==========================================
# 3. AUTO-HARVESTER
# ==========================================
async def harvest_chat_ids(memory, session):
    print("📡 Harvesting new Telegram users & groups...")
    updates_found = False
    offset = memory.get("last_update_id", 0) + 1
    url = f"{TELEGRAM_UPDATES_URL}?offset={offset}"
    
    try:
        data = await fetch_data(session, url, return_json=True)
        if isinstance(data, dict) and data.get("ok"):
            for result in data.get("result", []):
                update_id = result.get("update_id")
                
                if update_id and update_id >= memory.get("last_update_id", 0):
                    memory["last_update_id"] = update_id
                    updates_found = True
                
                chat_id = None
                if "message" in result: 
                    chat_id = str(result["message"]["chat"]["id"])
                elif "my_chat_member" in result: 
                    chat_id = str(result["my_chat_member"]["chat"]["id"])
                elif "channel_post" in result: 
                    chat_id = str(result["channel_post"]["chat"]["id"])
                    
                if chat_id:
                    if "chat_ids" not in memory: memory["chat_ids"] = []
                    if chat_id not in memory["chat_ids"]:
                        memory["chat_ids"].append(chat_id)
                        print(f"🆕 New destination registered: {chat_id}")
                        updates_found = True
    except Exception as e:
        print(f"⚠️ Failed to harvest IDs: {e}")
        
    return updates_found

# ==========================================
# 4. CLOUD MEMORY MANAGEMENT (JSONBIN)
# ==========================================
async def load_memory(session):
    print("☁️ Fetching memory from Cloud Database...")
    default_memory = {
        "grades": {}, "private_grades": {}, "files": {}, "timetable": {}, 
        "deadlines": {}, "server_status": "ok", "chat_ids": [], "last_update_id": 0
    }
    if not JSONBIN_ID or not JSONBIN_KEY: return default_memory
        
    url = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}"
    headers = {"X-Master-Key": JSONBIN_KEY}
    
    try:
        async with session.get(url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                memory = data.get("record", {})
                for key in default_memory:
                    if key not in memory: memory[key] = default_memory[key]
                
                current_time = int(time.time())
                if isinstance(memory.get("deadlines"), list):
                    memory["deadlines"] = {str(eid): current_time + 8640000 for eid in memory["deadlines"]}
                
                memory["deadlines"] = {eid: ts for eid, ts in memory["deadlines"].items() if ts > current_time}
                return memory
    except Exception: pass
    return default_memory

async def save_memory(session, memory):
    if not JSONBIN_ID or not JSONBIN_KEY: return
    url = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}"
    headers = {"X-Master-Key": JSONBIN_KEY, "Content-Type": "application/json"}
    try:
        await session.put(url, json=memory, headers=headers, timeout=10)
    except Exception: pass

# ==========================================
# 5. DEADLINE RADAR
# ==========================================
async def scan_deadlines(memory, notifications, session):
    print("⏳ Scanning Deadlines...")
    updates_found = False
    try:
        post_data = {
            "wstoken": API_TOKEN, "wsfunction": "core_calendar_get_action_events_by_timesort", 
            "moodlewsrestformat": "json", "timesortfrom": int(time.time())
        }
        data = await fetch_data(session, MOODLE_URL, is_moodle=True, post_data=post_data, return_json=True)
        if isinstance(data, dict) and "exception" in data: return False, False
        
        if isinstance(data, dict) and "events" in data:
            for event in data["events"]:
                event_id = str(event.get("id"))
                event_name = safe_html(event.get("name", "Unknown Assignment"))
                course_name = safe_html(event.get("course", {}).get("fullname", "Unknown Course"))
                timestamp = event.get("timesort")
                
                if event_id not in memory["deadlines"]:
                    memory["deadlines"][event_id] = timestamp
                    updates_found = True
                    dt = datetime.datetime.utcfromtimestamp(timestamp) + datetime.timedelta(hours=3)
                    date_str = dt.strftime("%A, %b %d at %I:%M %p")
                    notifications.append(f"🚨 <b>UPCOMING DEADLINE</b>\n📚 {course_name}\n📝 {event_name}\n⏰ Due: {date_str}")
        return updates_found, True
    except Exception: return False, False

# ==========================================
# 6. TIMETABLE SCANNER
# ==========================================
def parse_timetable(html):
    soup = BeautifulSoup(html, 'html.parser')
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
        html = await fetch_data(session, TIMETABLE_URL, return_json=False)
        new_timetable = parse_timetable(html)
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
    except Exception: return False, False

# ==========================================
# 7. GLOBAL MOODLE SCANNER (Files Only)
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
    if not timestamp: return ""
    dt = datetime.datetime.utcfromtimestamp(timestamp) + datetime.timedelta(hours=3)
    return dt.strftime("%A, %b %d, %Y at %I:%M %p")

async def scan_moodle(memory, notifications, session):
    print("🔍 Scanning Moodle for Global Files...")
    updates_found = False

    try:
        user_data = await fetch_data(session, MOODLE_URL, is_moodle=True, post_data={
            "wstoken": API_TOKEN, "wsfunction": "core_webservice_get_site_info", "moodlewsrestformat": "json"
        }, return_json=True)
        if not user_data or "exception" in user_data: return False, False
        user_id = user_data.get("userid")
        if not user_id: return False, False

        courses = await fetch_data(session, MOODLE_URL, is_moodle=True, post_data={
            "wstoken": API_TOKEN, "wsfunction": "core_enrol_get_users_courses", 
            "moodlewsrestformat": "json", "userid": user_id
        }, return_json=True)
        if not isinstance(courses, list): return False, False

        for course in courses:
            course_id = str(course['id'])
            course_name = safe_html(course['fullname'])
            if course_id in IGNORE_COURSES: continue

            if course_id not in memory["files"]: memory["files"][course_id] = {}
            elif isinstance(memory["files"][course_id], list):
                memory["files"][course_id] = {str(mid): 0 for mid in memory["files"][course_id]}

            # FILES ONLY
            try:
                content_data = await fetch_data(session, MOODLE_URL, is_moodle=True, post_data={
                    "wstoken": API_TOKEN, "wsfunction": "core_course_get_contents",
                    "moodlewsrestformat": "json", "courseid": course_id
                }, return_json=True)
                
                if isinstance(content_data, list):
                    for section in content_data:
                        section_name = safe_html(section.get("name", "General Topic"))
                        for mod in section.get("modules", []):
                            mod_id = str(mod.get("id"))
                            mod_name = mod.get("name", "Unknown File")
                            mod_type = mod.get("modname", "resource")
                            
                            time_modified = 0
                            fileurl = ""
                            if "contents" in mod and len(mod["contents"]) > 0:
                                time_modified = mod["contents"][0].get("timemodified", 0)
                                fileurl = mod["contents"][0].get("fileurl", "")
                            elif "url" in mod:
                                fileurl = mod["url"]
                            
                            html_link = ""
                            if fileurl:
                                if "webservice/pluginfile.php" in fileurl:
                                    direct_url = fileurl + (f"&token={API_TOKEN}" if "?" in fileurl else f"?token={API_TOKEN}")
                                    html_link = f"\n⬇️ <a href='{direct_url}'>Tap to Download File</a>"
                                else:
                                    html_link = f"\n🌐 <a href='{fileurl}'>Tap to Open Link</a>"

                            old_modified = memory["files"][course_id].get(mod_id)
                            formatted_name = safe_html(format_file_name(mod_name, mod_type))
                            
                            if old_modified is None:
                                memory["files"][course_id][mod_id] = time_modified
                                updates_found = True
                                notifications.append(f"📢 <b>NEW CONTENT:</b> {course_name}\n📂 Topic: {section_name}\n{formatted_name}{html_link}")
                                
                            elif time_modified > 0 and old_modified != 0 and time_modified > old_modified:
                                memory["files"][course_id][mod_id] = time_modified
                                updates_found = True
                                date_str = f"\n⏳ Modified: {format_iraq_time(time_modified)}"
                                notifications.append(f"🔄 <b>FILE UPDATED:</b> {course_name}\n📂 Topic: {section_name}\n{formatted_name}{date_str}{html_link}")
                                
            except Exception: pass
        return updates_found, True
    except Exception: return False, False

# ==========================================
# 8. MULTI-TENANT PRIVATE GRADES
# ==========================================
async def scan_private_grades(memory, session, users_list):
    print("🎓 Scanning Private Grades for registered users...")
    updates_found = False
    servers_ok = True

    if "private_grades" not in memory: memory["private_grades"] = {}

    for user in users_list:
        u_name = user.get("name")
        u_token = user.get("token")
        u_chat = user.get("chat_id")

        if not u_token or not u_chat: continue
        if u_name not in memory["private_grades"]: memory["private_grades"][u_name] = {}

        try:
            user_data = await fetch_data(session, MOODLE_URL, is_moodle=True, post_data={
                "wstoken": u_token, "wsfunction": "core_webservice_get_site_info", "moodlewsrestformat": "json"
            }, return_json=True)
            if not user_data or "exception" in user_data: 
                servers_ok = False
                continue
                
            user_id = user_data.get("userid")
            if not user_id: continue

            courses = await fetch_data(session, MOODLE_URL, is_moodle=True, post_data={
                "wstoken": u_token, "wsfunction": "core_enrol_get_users_courses", 
                "moodlewsrestformat": "json", "userid": user_id
            }, return_json=True)
            if not isinstance(courses, list): 
                servers_ok = False
                continue

            for course in courses:
                course_id = str(course['id'])
                course_name = safe_html(course['fullname'])
                if course_id in IGNORE_COURSES: continue

                if course_id not in memory["private_grades"][u_name]: 
                    memory["private_grades"][u_name][course_id] = {}

                grade_data = await fetch_data(session, MOODLE_URL, is_moodle=True, post_data={
                    "wstoken": u_token, "wsfunction": "gradereport_user_get_grade_items",
                    "moodlewsrestformat": "json", "courseid": course_id, "userid": user_id
                }, return_json=True)
                
                if isinstance(grade_data, dict) and "usergrades" in grade_data and len(grade_data["usergrades"]) > 0:
                    for item in grade_data["usergrades"][0].get("gradeitems", []):
                        item_name = safe_html(item.get("itemname"))
                        grade_val = safe_html(item.get("gradeformatted", "-"))
                        if not item_name or grade_val == "-": continue
                        
                        old_grade = memory["private_grades"][u_name][course_id].get(item_name)
                        if old_grade != grade_val:
                            memory["private_grades"][u_name][course_id][item_name] = grade_val
                            updates_found = True
                            
                            # Send Direct Message immediately to this specific user
                            if old_grade is None:
                                msg = f"🌟 <b>NEW GRADE:</b> {course_name}\n📝 {item_name}\n✅ Score: <b>{grade_val}</b>"
                            else:
                                msg = f"⚠️ <b>GRADE UPDATED:</b> {course_name}\n📝 {item_name}\n❌ Old: {old_grade}\n✅ New: <b>{grade_val}</b>"
                            
                            print(f"Sending private grade to {u_name}")
                            await send_telegram(session, msg, u_chat)
        except Exception as e:
            print(f"⚠️ Failed grade check for {u_name}: {e}")
            servers_ok = False

    return updates_found, servers_ok


# ==========================================
# 9. THE CLOUD BATCH TRIGGER
# ==========================================
async def main():
    print(f"🚀 Booting Cloud Monitor v{__version__}...")
    
    connector = aiohttp.TCPConnector(ssl=False, family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector) as session:
        memory = await load_memory(session)
        notifications = [] 
        memory_changed = False
        
        # 1. HARVEST NEW CHAT IDs BEFORE SCANNING
        if await harvest_chat_ids(memory, session):
            memory_changed = True
            
        if CHAT_ID and str(CHAT_ID) not in memory.get("chat_ids", []):
            memory.setdefault("chat_ids", []).append(str(CHAT_ID))
            memory_changed = True
            
        if "chat_ids" in memory:
            original_count = len(memory["chat_ids"])
            cleaned_ids = list(set([str(cid).strip() for cid in memory["chat_ids"] if str(cid).strip() not in ["0", "", "None"]]))
            if len(cleaned_ids) != original_count:
                memory["chat_ids"] = cleaned_ids
                memory_changed = True

        current_admin = ADMIN_CHAT_ID if ADMIN_CHAT_ID else (memory.get("chat_ids", [None])[0] if memory.get("chat_ids") else None)

        # 2. BUILD THE DYNAMIC USER ROSTER
        # Start with the Admin (You)
        users_to_check = [{"name": "Admin", "token": API_TOKEN, "chat_id": current_admin}]
        
        # Dynamically inject friends from the USERS_CONFIG GitHub Secret
        users_json = os.environ.get('USERS_CONFIG')
        if users_json:
            try:
                friends_list = json.loads(users_json)
                users_to_check.extend(friends_list)
                print(f"👥 Successfully loaded {len(friends_list)} friends from USERS_CONFIG.")
            except Exception as e:
                print(f"⚠️ JSON Format Error in USERS_CONFIG. Check your GitHub Secret syntax: {e}")

        # 3. RUN CONCURRENT SCANS
        results = await asyncio.gather(
            scan_moodle(memory, notifications, session),
            scan_timetable(memory, notifications, session),
            scan_deadlines(memory, notifications, session),
            scan_private_grades(memory, session, users_to_check),
            return_exceptions=True
        )
        
        moodle_updated, moodle_ok = results[0] if isinstance(results[0], tuple) else (False, False)
        timetable_updated, timetable_ok = results[1] if isinstance(results[1], tuple) else (False, False)
        deadlines_updated, deadlines_ok = results[2] if isinstance(results[2], tuple) else (False, False)
        grades_updated, grades_ok = results[3] if isinstance(results[3], tuple) else (False, False)

        any_updates = moodle_updated or timetable_updated or deadlines_updated or grades_updated
        all_servers_ok = moodle_ok and timetable_ok and deadlines_ok and grades_ok
        
        # 4. THE "STRIKE TWO" LOGIC (Strictly to Admin)
        if not all_servers_ok:
            current_status = memory.get("server_status", "ok")
            if current_status == "ok":
                print("⚠️ Servers missed a beat (Strike 1). Ignoring until next scan.")
                memory["server_status"] = "warning"
                memory_changed = True
            elif current_status == "warning":
                if current_admin:
                    await send_telegram(session, "🚨 <b>SYSTEM ALERT</b>\nCould not connect to the university servers for two consecutive scans. I will stay silent until the connection is restored.", target_chat=current_admin)
                print("❌ Servers down (Strike 2). Sent failure alert to admin.")
                memory["server_status"] = "failed"
                memory_changed = True
            else:
                print("❌ Servers still down. Remaining silent.")
                
        elif all_servers_ok:
            current_status = memory.get("server_status", "ok")
            if current_status in ["failed", "warning"]:
                if current_status == "failed" and current_admin:
                    await send_telegram(session, "✅ <b>CONNECTION RESTORED</b>\nThe university servers are back online.", target_chat=current_admin)
                memory["server_status"] = "ok"
                memory_changed = True
                print("✅ Servers recovered.")
                
            # 5. BROADCAST NOTIFICATIONS TO EVERYONE (Files, Timetable, Deadlines)
            if notifications:
                messages_to_send = []
                current_msg = f"🤖 <b>Moodle Monitor v{__version__}</b>\n\n"
                separator = "\n\n〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"

                for notif in notifications:
                    if len(current_msg) + len(notif) + len(separator) > 3900:
                        messages_to_send.append(current_msg)
                        current_msg = f"🤖 <b>Moodle Monitor v{__version__} (Cont.)</b>\n\n" + notif
                    else:
                        if current_msg.endswith("\n\n"): current_msg += notif
                        else: current_msg += separator + notif
                
                messages_to_send.append(current_msg)

                for msg in messages_to_send:
                    for target_chat in memory.get("chat_ids", []):
                        await send_telegram(session, msg, target_chat)
                
                memory_changed = True
                print(f"✅ Broadcasted {len(notifications)} updates to {len(memory.get('chat_ids', []))} unique chats!")

        # 6. SAVE STATE
        if memory_changed or any_updates:
            await save_memory(session, memory)
            print("☁️ Memory changes detected. Saved to JSONBin.")
        else:
            print("🛑 Skipped saving to JSONBin (No API request wasted).")

if __name__ == "__main__":
    asyncio.run(main())
