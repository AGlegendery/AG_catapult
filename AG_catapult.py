#!/usr/bin/env python3
# ag_catapult_final.py
"""
AG_catapult — Final menu-style live (polling) chat
User preferences applied:
- Original menu UI (List Contacts, Add Contact, Inbox, Send Message, Exit)
- Inbox shows last message per contact (preview + timestamp)
- Chat: polling mode appends only new messages (no history erase)
- Auto-reload (poll every 1s) works while in chat (background thread)
- Manual reload (r) works and does not clear history (it only appends unseen messages)
- Clear chat deletes messages between two users on the server and local preview is updated
- Delete account fully removes user and their messages from the DB
- User ID always shown under banner at program start
- Multiline messages: single ENTER = newline, double ENTER = send
- Colors: typing/default = white (terminal default), sent = YELLOW, incoming = BLUE
- No 'seen' handling to avoid server pressure
- Auto-installer prints status messages
"""

import os
import sys
import time
import json
import random
import subprocess
import threading
from pathlib import Path
from datetime import datetime

# ---------------- Auto-installer ----------------
REQUIRED_PACKAGES = ["psycopg2-binary", "colorama", "pyfiglet"]

def auto_install_packages():
    for package in REQUIRED_PACKAGES:
        mod = package.split("-")[0]
        try:
            __import__(mod)
        except ImportError:
            print(f"[+] Installing {package} ...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", package])

auto_install_packages()

# ---------------- Imports ----------------
import psycopg2
import pyfiglet
from colorama import init as color_init, Fore, Style

color_init(autoreset=True)

# ---------------- Config & Paths ----------------
# IMPORTANT: if your DB password contains '@' encode it as %40
DATABASE_URL = "postgres://base-user:4B2V8OCa4SW%40I7_4TIM_TISC@754b23bb47b942dab1a491ed3ec251e5.db.arvandbaas.ir:5432/default?sslmode=disable"

DATA_DIR = Path("ag_catapult_data")
DATA_DIR.mkdir(exist_ok=True)
CONTACTS_FILE = DATA_DIR / "contacts.json"
USER_FILE = DATA_DIR / "user.json"

POLL_INTERVAL = 1.0  # seconds for live polling

print_lock = threading.Lock()

# ---------------- Helpers ----------------
def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

def slow_print(text, delay=0.01, color=Fore.YELLOW):
    s = str(text)
    with print_lock:
        for ch in s:
            sys.stdout.write(color + ch)
            sys.stdout.flush()
            time.sleep(delay)
        print(Style.RESET_ALL)

def fast_print(text):
    with print_lock:
        print(text)

def generate_user_id():
    return ''.join(str(random.randint(0,9)) for _ in range(8))

# ---------------- DB ----------------
def connect_db():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        fast_print(Fore.RED + f"[!] Cannot connect to server: {e}")
        return None

def ensure_tables():
    conn = connect_db()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id VARCHAR(8) PRIMARY KEY,
                username VARCHAR(100) UNIQUE
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                from_user_id VARCHAR(8) REFERENCES users(user_id),
                to_user_id VARCHAR(8) REFERENCES users(user_id),
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        fast_print(Fore.RED + f"[!] Table creation failed: {e}")
        return False

# ---------------- User Management ----------------
def get_user_by_username(username):
    conn = connect_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id, username FROM users WHERE username=%s;", (username,))
        r = cur.fetchone()
        cur.close()
        conn.close()
        if r:
            return {"user_id": r[0], "username": r[1]}
        return None
    except Exception:
        return None

def register_user_on_server(user):
    conn = connect_db()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE username=%s;", (user["username"],))
        existing = cur.fetchone()
        if existing:
            cur.close()
            conn.close()
            return True
        cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s);", (user["user_id"], user["username"]))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        fast_print(Fore.RED + f"[!] DB Error during register: {e}")
        return False

def delete_user_account(user):
    conn = connect_db()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM messages WHERE from_user_id=%s OR to_user_id=%s;", (user["user_id"], user["user_id"]))
        cur.execute("DELETE FROM users WHERE user_id=%s;", (user["user_id"],))
        conn.commit()
        cur.close()
        conn.close()
        if USER_FILE.exists():
            USER_FILE.unlink()
        return True
    except Exception as e:
        fast_print(Fore.RED + f"[!] Failed to delete account: {e}")
        return False

# ---------------- Contacts ----------------
def load_contacts():
    if CONTACTS_FILE.exists():
        try:
            return json.loads(CONTACTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_contacts(contacts):
    CONTACTS_FILE.write_text(json.dumps(contacts, ensure_ascii=False), encoding="utf-8")

def lookup_contact_on_server(user_id):
    conn = connect_db()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT username FROM users WHERE user_id=%s;", (user_id,))
        r = cur.fetchone()
        cur.close()
        conn.close()
        return r[0] if r else None
    except Exception:
        return None

# ---------------- Messaging core ----------------
def insert_message_returning_id(from_id, to_id, message_text):
    conn = connect_db()
    if not conn:
        return None, "DB connection failed"
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (from_user_id, to_user_id, message) VALUES (%s, %s, %s) RETURNING id, created_at;",
            (from_id, to_id, message_text)
        )
        r = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        if r:
            return int(r[0]), r[1]
        return None, "No ID returned"
    except Exception as e:
        return None, str(e)

def get_chat_history_since(user_id, partner_id, since_id=0):
    conn = connect_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, from_user_id, message, created_at
            FROM messages
            WHERE ((from_user_id=%s AND to_user_id=%s) OR (from_user_id=%s AND to_user_id=%s))
              AND id > %s
            ORDER BY id ASC;
        """, (user_id, partner_id, partner_id, user_id, since_id))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except Exception:
        return []

def get_chat_history_all(user_id, partner_id):
    conn = connect_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, from_user_id, message, created_at
            FROM messages
            WHERE (from_user_id=%s AND to_user_id=%s) OR (from_user_id=%s AND to_user_id=%s)
            ORDER BY id ASC;
        """, (user_id, partner_id, partner_id, user_id))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except Exception:
        return []

def get_inbox_latest_per_partner(user_id, limit=100):
    conn = connect_db()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT DISTINCT ON (partner) partner, message, created_at
            FROM (
                SELECT CASE WHEN from_user_id=%s THEN to_user_id ELSE from_user_id END AS partner,
                       message, created_at
                FROM messages
                WHERE from_user_id=%s OR to_user_id=%s
                ORDER BY created_at DESC
            ) sub
            ORDER BY partner, created_at DESC;
        """, (user_id, user_id, user_id))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        rows_sorted = sorted(rows, key=lambda r: r[2], reverse=True)
        return rows_sorted[:limit]
    except Exception:
        return []

def clear_chat_between(user_id, partner_id):
    conn = connect_db()
    if not conn:
        return False, "DB connection failed"
    try:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM messages
            WHERE (from_user_id=%s AND to_user_id=%s) OR (from_user_id=%s AND to_user_id=%s);
        """, (user_id, partner_id, partner_id, user_id))
        conn.commit()
        cur.close()
        conn.close()
        return True, None
    except Exception as e:
        return False, str(e)

def clear_inbox_server(user_id):
    conn = connect_db()
    if not conn:
        return False, "DB connection failed"
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM messages WHERE to_user_id=%s;", (user_id,))
        conn.commit()
        cur.close()
        conn.close()
        return True, None
    except Exception as e:
        return False, str(e)

# ---------------- Poller thread ----------------
class ChatPoller(threading.Thread):
    def __init__(self, user, partner_id, on_new_callback, stop_event, last_seen_id=0):
        super().__init__(daemon=True)
        self.user = user
        self.partner_id = partner_id
        self.on_new = on_new_callback
        self.stop_event = stop_event
        self.last_id = last_seen_id

    def run(self):
        while not self.stop_event.is_set():
            try:
                rows = get_chat_history_since(self.user["user_id"], self.partner_id, self.last_id)
                if rows:
                    for rid, frm, msg, created in rows:
                        if rid > self.last_id:
                            self.last_id = rid
                        # callback prints message; callback should filter duplicates
                        self.on_new(rid, frm, msg, created)
                time.sleep(POLL_INTERVAL)
            except Exception:
                time.sleep(POLL_INTERVAL)

# ---------------- Chat session ----------------
def chat_session(user, partner_id, partner_name):
    clear_screen()
    print_banner(user)
    fast_print(Fore.CYAN + f"Chat with {partner_name} ({partner_id})")
    fast_print(Fore.CYAN + "Commands: [r] Reload  [c] Clear Chat  [=] Exit")

    # load full history and print fast (no slow_print)
    history = get_chat_history_all(user["user_id"], partner_id)
    displayed_ids = set()
    last_id = 0
    if history:
        for rid, frm, msg, created in history:
            displayed_ids.add(rid)
            last_id = max(last_id, rid)
            ts = created.strftime("%Y-%m-%d %H:%M")
            if frm == user["user_id"]:
                fast_print(Fore.YELLOW + f"You: {msg}")
                fast_print(Fore.WHITE + f"  [{ts}]")
            else:
                fast_print(Fore.BLUE + f"{partner_name}: {msg}")
                fast_print(Fore.WHITE + f"  [{ts}]")
    else:
        fast_print("(No history)")

    # callback for poller: append only new messages that weren't displayed
    def on_new(rid, frm, msg, created):
        if rid in displayed_ids:
            return
        displayed_ids.add(rid)
        ts = created.strftime("%Y-%m-%d %H:%M")
        if frm == user["user_id"]:
            # our own message echoed by DB (display as sent color)
            slow_print(f"You: {msg}", color=Fore.YELLOW)
            fast_print(Fore.WHITE + f"  [{ts}]")
        else:
            slow_print(f"{partner_name}: {msg}", color=Fore.BLUE)
            fast_print(Fore.WHITE + f"  [{ts}]")

    stop_event = threading.Event()
    poller = ChatPoller(user, partner_id, on_new, stop_event, last_seen_id=last_id)
    poller.start()

    try:
        while True:
            # read multiline input
            lines = []
            while True:
                try:
                    line = input()
                except (KeyboardInterrupt, EOFError):
                    line = "="
                if line.strip().lower() == "r" and not lines:
                    # manual reload: fetch messages since last_id and print only new ones
                    rows = get_chat_history_since(user["user_id"], partner_id, poller.last_id)
                    if rows:
                        for rid, frm, msg, created in rows:
                            on_new(rid, frm, msg, created)
                    continue
                if line.strip().lower() == "c" and not lines:
                    confirm = input("Clear chat with this user? This will delete messages for both (y/n): ").strip().lower()
                    if confirm == "y":
                        ok, err = clear_chat_between(user["user_id"], partner_id)
                        if ok:
                            fast_print(Fore.GREEN + "Chat cleared.")
                            displayed_ids.clear()
                        else:
                            fast_print(Fore.RED + f"Clear chat failed: {err}")
                    lines = []
                    break
                if line.strip() == "=" and not lines:
                    stop_event.set()
                    poller.join(timeout=1.0)
                    return
                # double enter detection
                if line == "":
                    if lines and lines[-1] == "":
                        lines.pop()
                        break
                lines.append(line)
            if not lines:
                continue
            message_text = "\n".join(lines).strip()
            if not message_text:
                continue
            # insert message and get id
            mid, created = insert_message_returning_id(user["user_id"], partner_id, message_text)
            if not mid:
                fast_print(Fore.RED + f"Send failed: {created}")
                continue
            # Immediately show sent message in YELLOW and record id to avoid duplication
            displayed_ids.add(mid)
            ts = created.strftime("%Y-%m-%d %H:%M") if hasattr(created, 'strftime') else datetime.now().strftime("%Y-%m-%d %H:%M")
            slow_print(f"You: {message_text}", color=Fore.YELLOW)
            fast_print(Fore.WHITE + f"  [{ts}]")
    finally:
        stop_event.set()
        try:
            poller.join(timeout=1.0)
        except Exception:
            pass

# ---------------- Inbox / Menu ----------------
def show_inbox(user, contacts):
    rows = get_inbox_latest_per_partner(user["user_id"])
    clear_screen()
    print_banner(user)
    fast_print(Fore.CYAN + "Inbox (conversations):")
    if not rows:
        fast_print(Fore.YELLOW + "(No conversations yet)")
    else:
        for i, (partner_id, message, created) in enumerate(rows, start=1):
            name = contacts.get(partner_id) or lookup_contact_on_server(partner_id) or partner_id
            preview = message.replace("\n", " ")[:50]
            ts = created.strftime("%Y-%m-%d %H:%M")
            fast_print(Fore.CYAN + f"{i}) {name:15} {preview:50} {ts}")
    fast_print("\nCommands: [number] Open chat  [a] Add contact  [l] List contacts  [clearinbox] Clear Inbox  [delme] Delete account  [q] Quit")

def inbox_select_partner(user, index):
    rows = get_inbox_latest_per_partner(user["user_id"])
    if index < 1 or index > len(rows):
        return None, None
    partner_id = rows[index-1][0]
    partner_name = lookup_contact_on_server(partner_id) or partner_id
    return partner_id, partner_name

# ---------------- User creation / load ----------------
def create_or_load_user():
    ensure_tables()
    if USER_FILE.exists():
        try:
            u = json.loads(USER_FILE.read_text(encoding="utf-8"))
            fast_print(Fore.CYAN + f"Loaded local user: {u.get('username')} ({u.get('user_id')})")
            return u
        except Exception:
            pass
    username = input("Enter your name: ").strip()
    if not username:
        fast_print(Fore.RED + "Name cannot be empty.")
        return create_or_load_user()
    existing = get_user_by_username(username)
    if existing:
        fast_print(Fore.YELLOW + f"Using existing account: {existing['username']} ({existing['user_id']})")
        USER_FILE.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
        return existing
    user_id = generate_user_id()
    user = {"username": username, "user_id": user_id}
    if register_user_on_server(user):
        real = get_user_by_username(username)
        if real:
            USER_FILE.write_text(json.dumps(real, ensure_ascii=False), encoding="utf-8")
            fast_print(Fore.GREEN + f"Registered and saved locally: {real['username']} ({real['user_id']})")
            return real
        else:
            USER_FILE.write_text(json.dumps(user, ensure_ascii=False), encoding="utf-8")
            fast_print(Fore.GREEN + f"Registered (local fallback): {user['username']} ({user['user_id']})")
            return user
    else:
        fast_print(Fore.RED + "Registration failed.")
        return None

# ---------------- Contacts functions used in menu ----------------
def add_contact_ui(contacts):
    uid = input("Enter the 8-digit ID of the contact: ").strip()
    if not uid:
        fast_print(Fore.RED + "Empty ID.")
        return contacts
    if uid in contacts:
        fast_print(Fore.YELLOW + "Contact already exists locally.")
        return contacts
    name = lookup_contact_on_server(uid)
    if name:
        contacts[uid] = name
        save_contacts(contacts)
        fast_print(Fore.GREEN + f"Added contact: {name} ({uid})")
    else:
        fast_print(Fore.RED + "User ID not found on server.")
    return contacts

def list_contacts_ui(contacts):
    if not contacts:
        fast_print(Fore.YELLOW + "No contacts yet.")
        return
    fast_print(Fore.CYAN + "--- Contacts ---")
    for i, (uid, name) in enumerate(contacts.items(), start=1):
        fast_print(Fore.CYAN + f"{i}. {name} ({uid})")

# ---------------- Banner/UI ----------------
def print_banner(user):
    b = pyfiglet.figlet_format("AG_catapult")
    fast_print(Fore.MAGENTA + b + Style.RESET_ALL)
    # show user id under banner as requested
    if user and isinstance(user, dict):
        fast_print(Fore.WHITE + f"Logged in as: {user.get('username')} (ID: {user.get('user_id')})")

# ---------------- Main menu ----------------
def main_menu():
    clear_screen()
    print_banner(None)
    user = create_or_load_user()
    if not user:
        fast_print(Fore.RED + "Cannot proceed without user.")
        return
    # show banner with user info
    clear_screen()
    print_banner(user)
    contacts = load_contacts()
    while True:
        fast_print("\nMain Menu:")
        fast_print("1. List Contacts")
        fast_print("2. Add Contact")
        fast_print("3. Inbox")
        fast_print("4. Send Message (open conversation)")
        fast_print("5. Clear Inbox")
        fast_print("6. Delete Account")
        fast_print("7. Exit")
        choice = input("Select an option: ").strip()
        if choice == "1":
            list_contacts_ui(contacts)
            input("Press Enter to continue...")
        elif choice == "2":
            contacts = add_contact_ui(contacts)
            time.sleep(0.3)
        elif choice == "3":
            show_inbox(user, contacts)
            cmd = input("Select (number) or command: ").strip()
            if cmd.lower() == "q":
                continue
            if cmd.lower() == "a":
                contacts = add_contact_ui(contacts)
                continue
            try:
                idx = int(cmd)
                partner_id, partner_name = inbox_select_partner(user, idx)
                if not partner_id:
                    fast_print(Fore.RED + "Invalid selection.")
                    time.sleep(0.5)
                    continue
                if partner_id not in contacts:
                    contacts[partner_id] = partner_name
                    save_contacts(contacts)
                chat_session(user, partner_id, partner_name)
            except ValueError:
                continue
        elif choice == "4":
            # open conversation by choosing contact from contacts list
            list_contacts_ui(contacts)
            sel = input("Select contact by number: ").strip()
            try:
                si = int(sel)
                if si < 1 or si > len(contacts):
                    raise ValueError
                partner_id = list(contacts.keys())[si-1]
                partner_name = contacts[partner_id]
                chat_session(user, partner_id, partner_name)
            except Exception:
                fast_print(Fore.RED + "Invalid selection.")
                time.sleep(0.3)
        elif choice == "5":
            ok, err = clear_inbox_server(user["user_id"])
            if ok:
                fast_print(Fore.GREEN + "Inbox cleared on server.")
            else:
                fast_print(Fore.RED + f"Clear inbox failed: {err}")
            time.sleep(0.3)
        elif choice == "6":
            confirm = input("Delete your account and all messages? (y/n): ").strip().lower()
            if confirm == "y":
                if delete_user_account(user):
                    fast_print(Fore.GREEN + "Account deleted. Exiting.")
                    return
                else:
                    fast_print(Fore.RED + "Account deletion failed.")
            time.sleep(0.3)
        elif choice == "7":
            fast_print(Fore.CYAN + "Goodbye!")
            return
        else:
            fast_print(Fore.RED + "Unknown option.")

# ---------------- Entry ----------------
if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        fast_print("\nExiting.")
