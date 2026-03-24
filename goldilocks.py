import asyncio
import aiohttp
import os
import socket
import time
import datetime
from bs4 import BeautifulSoup

# ==========================================
# 1. SETUP & CONFIGURATION
# ==========================================
IGNORE_COURSES = ["195", "196", "197", "198", "199", "200", "201"]

API_TOKEN = os.environ.get("MOODLE_API_TOKEN")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
RAW_URL = os.environ.get("MOODLE_API_URL", "https://moodle.uod.ac")

# New Database Keys
JSONBIN_ID = os.environ.get("JSONBIN_ID")
JSONBIN_KEY = os.environ.get("JSONBIN_KEY")

TIMETABLE_URL = "https://tb.duhokcihan.edu.krd/departtimtable.php?departmentNo=11&ClassNo1=1&GroupNo1=1"

if RAW_URL and not RAW_URL.endswith('/webservice/rest/server.php'):
    MOODLE_URL = RAW_URL.rstrip('/') + '/webservice/rest/server.php'
else:
    MOODLE_URL = RAW_URL

TELEGRAM_URL = f"https://moodle-tele-proxy.fy20155.workers.dev/bot{BOT_TOKEN}/sendMessage"

# ==========================================
# 2. NETWORK HELPER
# ==========================================
async def fetch_data(url, is_moodle=False, post_data=None, return_json=True):
    headers = {}
    if return_json: headers["Accept"] = "application/json"
    if is_moodle: headers["User-Agent"] = "MoodleMobile"
        
    connector = aiohttp.TCPConnector(ssl=False, family=socket.AF_INET)
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        if post_data:
            async with session.post(url, data=post_data, timeout=15) as resp:
                resp.raise_for_status() 
                return await resp.json() if return_json else await resp.text()
        else:
            async with session.get(url, timeout=15) as resp:
                resp.raise_for_status()
                return await resp.json() if return_json else await resp.text()

async def send_telegram(message):
    payload = {"chat_id": CHAT_ID, "text": message}
    print(f"📲 Sending batched alert...")
    try:
        await fetch_data(TELEGRAM_URL, post_data=payload, return_json=True)
        await asyncio.sleep(1) 
    except Exception:
        pass

# ==========================================
# 3. CLOUD MEMORY MANAGEMENT (JSONBIN)
# ==========================================
async def load_memory():
    print("☁️ Fetching memory from Cloud Database...")
    default_memory = {"grades": {}, "files": {}, "timetable": {}, "deadlines": [], "server_status": "ok"}
    if not JSONBIN_ID or not JSONBIN_KEY:
        print("⚠️ No JSONBin credentials found. Using empty memory.")
        return default_memory
        
    url = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}"
    headers = {"X-Master-Key": JSONBIN_KEY}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    memory = data.get("record", {})
                    # Ensure all keys exist
                    for key in default_memory:
                        if key not in memory:
                            memory[key] = default_memory[key]
                    return memory
    except Exception as e:
        print(f"⚠️ Failed to load cloud memory: {e}")
    return default_memory

async def save_memory(memory):
    print("☁️ Saving memory to Cloud Database...")
    if not JSONBIN_ID or not JSONBIN_KEY: return
    
    url = f"https://api.jsonbin.io/v3/b/{JSONBIN_ID}"
    headers = {"X-Master-Key": JSONBIN_KEY, "Content-Type": "application/json"}
    
    try:
        async with aiohttp.ClientSession() as session:
            await session.put(url, json=memory, headers=headers, timeout=10)
    except Exception as e:
        print(f"⚠️ Failed to save cloud memory: {e}")

# ==========================================
# 4. DEADLINE RADAR
# ==========================================
async def scan_deadlines(memory, notifications):
    print("⏳ Scanning Deadlines...")
    updates_found = False
    try:
        post_data = {
            "wstoken": API_TOKEN, "wsfunction": "core_calendar_get_action_events_by_timesort", 
            "moodlewsrestformat": "json", "timesortfrom": int(time.time())
        }
        data = await fetch_data(MOODLE_URL, is_moodle=True, post_data=post_data, return_json=True)
        
        if isinstance(data, dict) and "exception" in data: return False, False
        
        if isinstance(data, dict) and "events" in data:
            for event in data["events"]:
                event_id = str(event.get("id"))
                event_name = event.get("name", "Unknown Assignment")
                course_name = event.get("course", {}).get("fullname", "Unknown Course")
                timestamp = event.get("timesort")
                
                if event_id not in memory["deadlines"]:
                    memory["deadlines"].append(event_id)
                    updates_found = True
                    dt = datetime.datetime.utcfromtimestamp(timestamp) + datetime.timedelta(hours=3)
                    date_str = dt.strftime("%A, %b %d at %I:%M %p")
                    notifications.append(f"🚨 UPCOMING DEADLINE\n📚 {course_name}\n📝 {event_name}\n⏰ Due: {date_str}")
        return updates_found, True
    except Exception as e:
        print(f"⚠️ Deadline error: {e}")
        return False, False

# ==========================================
# 5. TIMETABLE SCANNER
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

async def scan_timetable(memory, notifications):
    print("📅 Scanning Timetable...")
    try:
        html = await fetch_data(TIMETABLE_URL, return_json=False)
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
                
                if old_content is None:
                    if new_content != "Rest Hour": 
                        notifications.append(f"📅 CLASS TRACKED\n📆 Day: {day}\n⏰ Time: {time_str}\n✅ {new_content}")
                else:
                    if new_content == "Rest Hour":
                        notifications.append(f"🛏️ CLASS CANCELLED\n📆 Day: {day}\n⏰ Time: {time_str}\n❌ Old: {old_content}\n✅ New: Rest Hour")
                    elif old_content == "Rest Hour":
                        notifications.append(f"🚨 NEW CLASS ADDED\n📆 Day: {day}\n⏰ Time: {time_str}\n❌ Old: Rest Hour\n✅ New: {new_content}")
                    else:
                        notifications.append(f"⚠️ TIMETABLE CHANGED\n📆 Day: {day}\n⏰ Time: {time_str}\n❌ Old: {old_content}\n✅ New: {new_content}")
        return updates_found, True
    except Exception as e:
        print(f"⚠️ Timetable error: {e}")
        return False, False

# ==========================================
# 6. MOODLE SCANNER
# ==========================================
def format_file_name(mod_name, mod_type):
    name_lower = mod_name.lower()
    if mod_type == "url": return f"🔗 Web Link: {mod_name}"
    if mod_type == "folder": return f"📁 Folder: {mod_name}"
    if mod_type == "assign": return f"📥 Assignment: {mod_name}"
    if mod_type == "forum": return f"💬 Forum: {mod_name}"
    
    if ".pdf" in name_lower: return f"📄 PDF: {mod_name}"
    if any(ext in name_lower for ext in [".ppt", ".pptx"]): return f"📊 Presentation: {mod_name}"
    if any(ext in name_lower for ext in [".doc", ".docx"]): return f"📝 Word Doc: {mod_name}"
    if any(ext in name_lower for ext in [".xls", ".xlsx"]): return f"📈 Spreadsheet: {mod_name}"
    if any(ext in name_lower for ext in [".zip", ".rar"]): return f"🗄️ Archive: {mod_name}"
    return f"📄 File: {mod_name}"

def format_iraq_time(timestamp):
    if not timestamp: return ""
    dt = datetime.datetime.utcfromtimestamp(timestamp) + datetime.timedelta(hours=3)
    return dt.strftime("%A, %b %d, %Y at %I:%M %p")

async def scan_moodle(memory, notifications):
    print("🔍 Scanning Moodle...")
    updates_found = False

    try:
        user_data = await fetch_data(MOODLE_URL, is_moodle=True, post_data={
            "wstoken": API_TOKEN, "wsfunction": "core_webservice_get_site_info", "moodlewsrestformat": "json"
        }, return_json=True)
        if not user_data or "exception" in user_data: return False, False
        user_id = user_data.get("userid")
        if not user_id: return False, False

        courses = await fetch_data(MOODLE_URL, is_moodle=True, post_data={
            "wstoken": API_TOKEN, "wsfunction": "core_enrol_get_users_courses", 
            "moodlewsrestformat": "json", "userid": user_id
        }, return_json=True)
        if not isinstance(courses, list): return False, False

        for course in courses:
            course_id = str(course['id'])
            course_name = course['fullname']
            if course_id in IGNORE_COURSES: continue

            if course_id not in memory["grades"]: memory["grades"][course_id] = {}
            if course_id not in memory["files"]: memory["files"][course_id] = {}
            elif isinstance(memory["files"][course_id], list):
                old_list = memory["files"][course_id]
                memory["files"][course_id] = {str(mid): 0 for mid in old_list}

            # GRADES
            try:
                grade_data = await fetch_data(MOODLE_URL, is_moodle=True, post_data={
                    "wstoken": API_TOKEN, "wsfunction": "gradereport_user_get_grade_items",
                    "moodlewsrestformat": "json", "courseid": course_id, "userid": user_id
                }, return_json=True)
                if isinstance(grade_data, dict) and "usergrades" in grade_data and len(grade_data["usergrades"]) > 0:
                    for item in grade_data["usergrades"][0].get("gradeitems", []):
                        item_name = item.get("itemname")
                        grade_val = item.get("gradeformatted", "-")
                        if not item_name or grade_val == "-": continue
                        
                        old_grade = memory["grades"][course_id].get(item_name)
                        if old_grade != grade_val:
                            memory["grades"][course_id][item_name] = grade_val
                            updates_found = True
                            if old_grade is None:
                                notifications.append(f"🌟 NEW GRADE: {course_name}\n📝 {item_name}\n✅ Score: {grade_val}")
                            else:
                                notifications.append(f"⚠️ GRADE UPDATED: {course_name}\n📝 {item_name}\n❌ Old: {old_grade}\n✅ New: {grade_val}")
            except Exception: pass 

            # FILES
            try:
                content_data = await fetch_data(MOODLE_URL, is_moodle=True, post_data={
                    "wstoken": API_TOKEN, "wsfunction": "core_course_get_contents",
                    "moodlewsrestformat": "json", "courseid": course_id
                }, return_json=True)
                
                if isinstance(content_data, list):
                    for section in content_data:
                        section_name = section.get("name", "General Topic")
                        
                        for mod in section.get("modules", []):
                            mod_id = str(mod.get("id"))
                            mod_name = mod.get("name", "Unknown File")
                            mod_type = mod.get("modname", "resource")
                            
                            time_created = 0
                            time_modified = 0
                            if "contents" in mod and len(mod["contents"]) > 0:
                                time_created = mod["contents"][0].get("timecreated", 0)
                                time_modified = mod["contents"][0].get("timemodified", 0)
                            
                            old_modified = memory["files"][course_id].get(mod_id)
                            formatted_name = format_file_name(mod_name, mod_type)
                            
                            if old_modified is None:
                                memory["files"][course_id][mod_id] = time_modified
                                updates_found = True
                                date_str = f"\n📅 Created: {format_iraq_time(time_created)}" if time_created else ""
                                notifications.append(f"📢 NEW CONTENT: {course_name}\n📂 Topic: {section_name}\n{formatted_name}{date_str}")
                                
                            elif time_modified > 0 and old_modified != 0 and time_modified > old_modified:
                                memory["files"][course_id][mod_id] = time_modified
                                updates_found = True
                                date_str = f"\n⏳ Modified: {format_iraq_time(time_modified)}"
                                notifications.append(f"🔄 FILE UPDATED: {course_name}\n📂 Topic: {section_name}\n{formatted_name}{date_str}")
                                
            except Exception as e: 
                print(f"Content parse error: {e}")

        return updates_found, True
    except Exception as e:
        print(f"⚠️ Network error: {e}")
        return False, False

# ==========================================
# 7. THE CLOUD BATCH TRIGGER
# ==========================================
async def main():
    print("🚀 Booting Cloud Monitor...")
    # Using 'await' because our memory functions are now async network calls
    memory = await load_memory()
    notifications = [] 
    
    moodle_updated, moodle_ok = await scan_moodle(memory, notifications)
    timetable_updated, timetable_ok = await scan_timetable(memory, notifications)
    deadlines_updated, deadlines_ok = await scan_deadlines(memory, notifications)
    
    any_updates = moodle_updated or timetable_updated or deadlines_updated
    all_servers_ok = moodle_ok and timetable_ok and deadlines_ok
    
    if not all_servers_ok:
        if memory.get("server_status") != "failed":
            await send_telegram("🚨 SCAN FAILED\nCould not connect to the university servers. I will stay silent until the connection is restored.")
            memory["server_status"] = "failed"
            await save_memory(memory)
            print("❌ Servers are down. Sent failure alert.")
        else:
            print("❌ Servers are still down. Remaining silent to avoid spam.")
            
    elif all_servers_ok:
        if memory.get("server_status") == "failed":
            notifications.append("✅ CONNECTION RESTORED\nThe university servers are back online. Resuming normal scans.")
            memory["server_status"] = "ok"
            print("✅ Servers recovered.")
            
        if notifications:
            final_message = "🔔 **Moodle Monitor Updates**\n\n" + "\n\n〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n".join(notifications)
            
            for i in range(0, len(final_message), 4000):
                await send_telegram(final_message[i:i+4000])
                
            await save_memory(memory)
            print(f"✅ Memory saved to JSONBin. Sent {len(notifications)} updates!")
            
        else:
            await save_memory(memory)
            print("💤 No changes found. Staying perfectly silent.")

if __name__ == "__main__":
    asyncio.run(main())
