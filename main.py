# main.py — CORE + DISCORD BOT + APP SETUP (routes registered from web_routes.py)

import os
import csv
import json
import logging
import asyncio
import requests
from threading import Thread, Lock
from datetime import datetime, timedelta, timezone
from web_routes import register_routes

from flask import Flask
from waitress import serve

import discord
from discord.ext import commands, tasks

# ===== Airtable config (optional) =====
from pyairtable import Api

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE = os.getenv("AIRTABLE_TABLE", "TitleLog")

airtable_table = None
if AIRTABLE_API_KEY and AIRTABLE_BASE_ID:
    try:
        api = Api(AIRTABLE_API_KEY)
        airtable_table = api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE)
    except Exception as e:
        logging.getLogger(__name__).warning(f"Airtable not configured: {e}")

def to_iso_utc(val) -> str:
    """
    Accepts a datetime or ISO-ish string and returns a proper ISO8601 UTC string.
    """
    if isinstance(val, datetime):
        dt = val
    else:
        dt = parse_iso_utc(val) or datetime.fromisoformat(str(val)).replace(tzinfo=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()

def airtable_upsert(record_type: str, payload: dict):
    """
    Write a row to Airtable using our standard schema.
    Fields: Type, Title, IGN, Coordinates, SlotStartUTC, SlotEndUTC, Timestamp, Reason, Source, DiscordUser
    """
    if not airtable_table:
        return  # Airtable not configured — silently skip

    fields = {
        "Type": record_type,                          # reservation | activation | assignment | release
        "Title": payload.get("Title"),
        "IGN": payload.get("IGN"),
        "Coordinates": payload.get("Coordinates"),
        "SlotStartUTC": None,
        "SlotEndUTC": None,
        "Timestamp": now_utc().isoformat(),
        "Reason": payload.get("Reason"),
        "Source": payload.get("Source"),
        "DiscordUser": payload.get("DiscordUser"),
    }

    # Normalize times if present
    if payload.get("SlotStartUTC"):
        fields["SlotStartUTC"] = to_iso_utc(payload["SlotStartUTC"])
    if payload.get("SlotEndUTC"):
        fields["SlotEndUTC"] = to_iso_utc(payload["SlotEndUTC"])

    try:
        airtable_table.create(fields)
    except Exception as e:
        logging.getLogger(__name__).error(f"Airtable create failed: {e}")

# ========= UTC helpers & constants =========
UTC = timezone.utc
# --- FEATURE: Default shift hours updated to 12 ---
SHIFT_HOURS = 12  # default 12-hour reservation/assignment windows

def now_utc() -> datetime:
    """Returns the current time in UTC."""
    return datetime.now(UTC)

def parse_iso_utc(s: str) -> datetime | None:
    """
    Parse ISO strings; make them UTC-aware if they were saved naive.
    Returns None if the string is invalid or None.
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None

def iso_slot_key_naive(dt: datetime) -> str:
    """
    Normalized slot key we use everywhere in schedules:
    'YYYY-MM-DDTHH:MM:SS' naive (no timezone), seconds forced to :00.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0).isoformat()

def title_is_vacant_now(title_name: str) -> bool:
    """Checks if a title is currently available."""
    t = state.get('titles', {}).get(title_name, {})
    if not t.get('holder'):
        return True
    
    exp_str = t.get('expiry_date')
    # An empty or null expiry_date means it's held indefinitely (GoH)
    if not exp_str:
        return False

    expiry_dt = parse_iso_utc(exp_str)
    if expiry_dt and now_utc() >= expiry_dt:
        return True  # Expired, so it's vacant
            
    return False

# ========= Static Titles =========
TITLES_CATALOG = {
    "Guardian of Harmony": {
        "effects": "All benders' ATK +5%, All benders' DEF +5%, All Benders' recruiting speed +15%",
        "image": "/static/icons/guardian_harmony.png"
    },
    "Guardian of Air": {
        "effects": "All Resource Gathering Speed +20%, All Resource Production +20%",
        "image": "/static/icons/guardian_air.png"
    },
    "Guardian of Water": {
        "effects": "All Benders' recruiting speed +15%",
        "image": "/static/icons/guardian_water.png"
    },
    "Guardian of Earth": {
        "effects": "Construction Speed +10%, Research Speed +10%",
        "image": "/static/icons/guardian_earth.png"
    },
    "Guardian of Fire": {
        "effects": "All benders' ATK +5%, All benders' DEF +5%",
        "image": "/static/icons/guardian_fire.png"
    },
    "Architect": {
        "effects": "Construction Speed +10%",
        "image": "/static/icons/architect.png"
    },
    "General": {
        "effects": "All benders' ATK +5%",
        "image": "/static/icons/general.png"
    },
    "Governor": {
        "effects": "All Benders' recruiting speed +10%",
        "image": "/static/icons/governor.png"
    },
    "Prefect": {
        "effects": "Research Speed +10%",
        "image": "/static/icons/prefect.png"
    },
},

ORDERED_TITLES = list(TITLES_CATALOG.keys())
REQUESTABLE = {title for title in ORDERED_TITLES if title != "Guardian of Harmony"}

# ========= Environment & Config =========
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_PIN = os.getenv("ADMIN_PIN", "letmein")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FLASK_SECRET = os.getenv("FLASK_SECRET", "a-strong-dev-secret-key")
GUARDIAN_ROLE_ID = os.getenv("GUARDIAN_ROLE_ID")

# ========= Discord setup =========
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ========= Persistence & Thread Safety =========
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATIC_DIR = os.path.join(BASE_DIR, "static", "icons")
os.makedirs(STATIC_DIR, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "titles_state.json")
CSV_FILE   = os.path.join(DATA_DIR, "requests.csv")

state: dict = {}
# --- HARDENING: Use a thread-safe lock for all state access ---
state_lock = Lock()

logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger(__name__)

# ========= State & Log Helpers =========
def initialize_state():
    """Sets up the initial dictionary structure for the state."""
    global state
    state = {
        'titles': {},
        'config': {},
        'schedules': {},
        'sent_reminders': [],
        'activated_slots': {}  # dict[title][slot_key] = True for already-activated slots
    }

def load_state():
    """Loads state from JSON file, ensuring thread safety."""
    global state
    with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                    # Ensure required top-level keys exist even in older files
                    state.setdefault('activated_slots', {})
                    state.setdefault('schedules', {})
                    state.setdefault('titles', {})
                    state.setdefault('config', {})
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading state file: {e}. Re-initializing.")
                initialize_state()
        else:
            initialize_state()

def save_state():
    """Saves state to JSON file atomically and with thread safety."""
    with state_lock:
        temp_file = STATE_FILE + ".tmp"
        try:
            with open(temp_file, 'w') as f:
                json.dump(state, f, indent=4)
            os.replace(temp_file, STATE_FILE)  # Atomic rename
        except IOError as e:
            logger.error(f"Error saving state file: {e}")

def log_to_csv(request_data):
    """Logs a request to the CSV file with thread safety."""
    with state_lock:
        file_exists = os.path.isfile(CSV_FILE)
        try:
            with open(CSV_FILE, 'a', newline='', encoding='utf-8') as csvfile:
                fieldnames = ['timestamp', 'title_name', 'in_game_name', 'coordinates', 'discord_user']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(request_data)
        except IOError as e:
            logger.error(f"Error writing to CSV: {e}")

def initialize_titles():
    """Ensures all titles from the catalog exist in the state."""
    with state_lock:
        state.setdefault('titles', {})
        for title_name in TITLES_CATALOG:
            if title_name not in state['titles']:
                state['titles'][title_name] = {
                    'holder': None, 'claim_date': None, 'expiry_date': None
                }
    save_state()

# ========= Notification Helpers =========
def send_webhook_notification(data, reminder=False):
    """Sends a formatted notification to the configured Discord webhook."""
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL is not set. Skipping notification.")
        return
        
    role_tag = f"<@&{GUARDIAN_ROLE_ID}>" if GUARDIAN_ROLE_ID else ""

    if reminder:
        title = f"Reminder: {data.get('title_name','-')} shift starts soon!"
        content = f"{role_tag} The {get_shift_hours()}-hour shift for **{data.get('title_name','-')}** by **{data.get('in_game_name','-')}** starts in 5 minutes!"
    else:
        title = "New Title Reservation"
        content = f"{role_tag} A new title was reserved via the web form."

    payload = {
        "content": content, "allowed_mentions": {"parse": ["roles"]},
        "embeds": [{"title": title, "color": 5814783, "fields": [
                {"name": "Title", "value": data.get('title_name','-'), "inline": True},
                {"name": "In-Game Name", "value": data.get('in_game_name','-'), "inline": True},
                {"name": "Coordinates", "value": data.get('coordinates','-'), "inline": True},
                {"name": "Submitted By", "value": data.get('discord_user','Web Form'), "inline": False}
            ], "timestamp": data.get('timestamp')
        }]
    }
    try:
        # HARDENING: Add timeout to webhook requests
        r = requests.post(WEBHOOK_URL, json=payload, timeout=8)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Webhook send failed: {e}")

# ========= Activation Helper =========
def get_shift_hours():
    """Gets shift hours from config, defaulting to the constant."""
    with state_lock:
        return state.get('config', {}).get('shift_hours', SHIFT_HOURS)

def activate_slot(title_name: str, ign: str, start_dt: datetime):
    """Activate a scheduled slot: set holder & expiry for the title."""
    end_dt = start_dt + timedelta(hours=get_shift_hours())
    with state_lock:
        state['titles'][title_name].update({
            'holder': {'name': ign, 'coords': '-', 'discord_id': 0},
            'claim_date': start_dt.isoformat(),
            'expiry_date': None if title_name == "Guardian of Harmony" else end_dt.isoformat(),
        })
        state.setdefault('activated_slots', {})
        state.setdefault('activated_slots', {})
        already = state['activated_slots'].get(title_name) or {}
        # store as dict of booleans (JSON-friendly)
        already[iso_slot_key_naive(start_dt)] = True
        state['activated_slots'][title_name] = already
    save_state()

    def activate_slot(title_name: str, ign: str, start_dt: datetime):
        """Activate a scheduled slot: set holder & expiry for the title."""
    end_dt = start_dt + timedelta(hours=get_shift_hours())
    with state_lock:
        state['titles'][title_name].update({
            'holder': {'name': ign, 'coords': '-', 'discord_id': 0},
            'claim_date': start_dt.isoformat(),
            'expiry_date': None if title_name == "Guardian of Harmony" else end_dt.isoformat(),
        })
        state.setdefault('activated_slots', {})
        already = state['activated_slots'].get(title_name) or {}
        already[iso_slot_key_naive(start_dt)] = True
        state['activated_slots'][title_name] = already
    save_state()

    # ✅ Log to Airtable
    airtable_upsert("activation", {
        "Title": title_name,
        "IGN": ign,
        "Coordinates": "-",                       # unknown here
        "SlotStartUTC": start_dt,
        "SlotEndUTC": None if title_name == "Guardian of Harmony" else end_dt,
        "Source": "Auto-Activate",
        "DiscordUser": "-"
    })

# ========= Flask App Setup =========
app = Flask(__name__)
app.secret_key = FLASK_SECRET

def run_flask_app():
    """Runs the Flask web server using Waitress for cross-platform compatibility."""
    port = int(os.getenv("PORT", "10000"))
    logger.info(f"Starting Flask server on port {port}")
    serve(app, host='0.0.0.0', port=port)

# ========= Discord Cog for Bot Commands & Tasks =========
class TitleCog(commands.Cog, name="TitleManager"):
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.title_check_loop.start()

    async def force_release_logic(self, title_name, reason):
        """Internal logic to release a title, callable from tasks or commands."""
        with state_lock:
            if title_name not in state.get('titles', {}):
                return
            state['titles'][title_name].update({
                'holder': None, 'claim_date': None, 'expiry_date': None
            })
        save_state()
        airtable_upsert("release", {
            "Title": title_name,
            "Reason": reason,
            "Source": "System",
            "DiscordUser": "-"
        })
        await self.announce(f"TITLE RELEASED: **'{title_name}'** is now available. Reason: {reason}")
        logger.info(f"[RELEASE] {title_name} released. Reason: {reason}")

    @tasks.loop(minutes=1)
    async def title_check_loop(self):
        """Periodic task to check for expired titles and auto-activate due slots."""
        await self.bot.wait_until_ready()
        now = now_utc()
        
        # 1) Release expired titles (existing behavior)
        titles_to_release = []
        with state_lock:
            for title_name, data in state.get('titles', {}).items():
                if data.get('holder') and data.get('expiry_date'):
                    expiry_dt = parse_iso_utc(data['expiry_date'])
                    if expiry_dt and now >= expiry_dt:
                        titles_to_release.append(title_name)
        for title_name in titles_to_release:
            await self.force_release_logic(title_name, "Title expired.")
        
        # 2) Auto-activate scheduled slots whose start time has arrived
        to_activate: list[tuple[str, str, datetime]] = []  # (title, ign, start_dt)
        with state_lock:
            schedules = state.get('schedules', {})
            activated = state.get('activated_slots', {})
            for title_name, slots in schedules.items():
                for slot_key, entry in slots.items():
                    # slot_key is stored naive ISO (by iso_slot_key_naive); parse & make UTC-aware
                    start_dt = parse_iso_utc(slot_key) or datetime.fromisoformat(slot_key).replace(tzinfo=UTC)
                    # skip if not due
                    if start_dt > now:
                        continue
                    # already activated?
                    if activated.get(title_name, {}).get(slot_key):
                        continue
                    # Get reserving IGN
                    ign = entry['ign'] if isinstance(entry, dict) else str(entry)
                    to_activate.append((title_name, ign, start_dt))

        # Perform activations outside the lock
        for title_name, ign, start_dt in to_activate:
            activate_slot(title_name, ign, start_dt)
            await self.announce(f"AUTO-ACTIVATED: **{title_name}** → **{ign}** (slot start reached).")
            logger.info(f"[AUTO-ACTIVATE] {title_name} -> {ign} at {start_dt.isoformat()}")

    async def announce(self, message):
        """Sends a message to the configured announcement channel."""
        channel_id = state.get('config', {}).get('announcement_channel')
        if channel_id:
            try:
                channel = await self.bot.fetch_channel(channel_id)
                if isinstance(channel, discord.TextChannel):
                    await channel.send(message)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                logger.error(f"Could not send to announcement channel {channel_id}: {e}")
    
    @commands.command(help="List all titles and their current status.")
    async def titles(self, ctx):
        embed = discord.Embed(title="Title Status", color=discord.Color.blue())
        with state_lock:
            for title_name in ORDERED_TITLES:
                data = state['titles'].get(title_name, {})
                status = ""
                if data.get('holder'):
                    holder_name = data['holder'].get('name', 'Unknown')
                    if data.get('expiry_date'):
                        expiry = parse_iso_utc(data['expiry_date'])
                        remaining = expiry - now_utc() if expiry else None
                        if remaining is not None:
                            seconds = max(0, int(remaining.total_seconds()))
                            status += f"**Held by:** {holder_name}\n*Expires in: {str(timedelta(seconds=seconds))}*"
                        else:
                            status += f"**Held by:** {holder_name}\n*Expiry: Invalid*"
                    else:  # Guardian of Harmony case
                        status += f"**Held by:** {holder_name}\n*Expires: Never*"
                else:
                    status += "**Status:** Available"
                embed.add_field(name=f"{title_name}", value=status, inline=False)
        await ctx.send(embed=embed)

    @commands.command(help="Assign a title. Usage: !assign <Title Name> | <In-Game Name>")
    @commands.has_permissions(administrator=True)
    async def assign(self, ctx, *, args: str):
        try:
            title_name, ign = [arg.strip() for arg in args.split('|')]
        except ValueError:
            await ctx.send("Invalid format. Use `!assign <Title Name> | <In-Game Name>`")
            return

        if title_name not in ORDERED_TITLES:
            await ctx.send(f"Title '{title_name}' does not exist.")
            return

        now = now_utc()
        expiry_date_iso = None  # Default for Guardian of Harmony
        if title_name != "Guardian of Harmony":
            expiry_date_iso = (now + timedelta(hours=get_shift_hours())).isoformat()

        with state_lock:
            state['titles'][title_name].update({
                'holder': {'name': ign, 'coords': '-', 'discord_id': ctx.author.id},
                'claim_date': now.isoformat(), 'expiry_date': expiry_date_iso
            })
        save_state()

        airtable_upsert("assignment", {
            "Title": title_name,
            "IGN": ign,
            "Coordinates": "-",  # not provided in command
            "SlotStartUTC": now,
            "SlotEndUTC": expiry_date_iso,
            "Source": "Discord Command",
            "DiscordUser": getattr(ctx.author, "display_name", str(ctx.author))
        })
        await self.announce(f"SHIFT CHANGE: **{ign}** has been granted **'{title_name}'**.")
        logger.info(f"[ASSIGN] {ctx.author.display_name} assigned {title_name} -> {ign}")

    @commands.command(help="Set the announcement channel. Usage: !set_announce <#channel>")
    @commands.has_permissions(administrator=True)
    async def set_announce(self, ctx, channel: discord.TextChannel):
        with state_lock:
            state.setdefault('config', {})['announcement_channel'] = channel.id
        save_state()
        await ctx.send(f"Announcement channel set to {channel.mention}.")

# ========= Register routes from web_routes.py =========
app = Flask(__name__)
app.secret_key = FLASK_SECRET

register_routes(
    app=app,
    deps=dict(
        ORDERED_TITLES=ORDERED_TITLES, TITLES_CATALOG=TITLES_CATALOG,
        REQUESTABLE=REQUESTABLE, ADMIN_PIN=ADMIN_PIN,
        state=state, save_state=save_state, log_to_csv=log_to_csv,
        parse_iso_utc=parse_iso_utc, now_utc=now_utc,
        iso_slot_key_naive=iso_slot_key_naive,
        title_is_vacant_now=title_is_vacant_now,
        get_shift_hours=get_shift_hours,
        bot=bot,
        state_lock=state_lock,
        send_webhook_notification=send_webhook_notification
    )
)

# ========= Discord Bot Lifecycle =========
@bot.event
async def on_ready():
    """Event handler for when the bot is connected and ready."""
    load_state()
    initialize_titles()
    await bot.add_cog(TitleCog(bot))
    logger.info(f'{bot.user.name} has connected to Discord!')
    # Start Flask in a separate, daemonized thread
    flask_thread = Thread(target=run_flask_app, daemon=True)
    flask_thread.start()

# ========= Main Entry Point =========
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.critical("FATAL: DISCORD_TOKEN environment variable not set.")
    else:
        try:
            bot.run(DISCORD_TOKEN)
        except discord.errors.LoginFailure:
            logger.critical("FATAL: Improper token has been passed.")