# main.py — CORE + DISCORD BOT + APP SETUP (routes registered from web_routes.py)

from __future__ import annotations

import os
import csv
import json
import logging
import asyncio
import requests
from threading import Thread, RLock
from datetime import datetime, timedelta, timezone

from flask import Flask
from waitress import serve

import discord
from discord.ext import commands, tasks

from web_routes import register_routes

# ===== Airtable (optional; safe import) =====
try:
    from pyairtable import Api
except Exception:  # package not installed or other import issue
    Api = None

# ===== NEW: SQLAlchemy + helpers =====
from dotenv import load_dotenv
from sqlalchemy import event
from models import db, Title, Reservation, ActiveTitle, RequestLog
from db_utils import (
    get_shift_hours as db_get_shift_hours,
    set_shift_hours as db_set_shift_hours,
    compute_slots,
    requestable_title_names,
    title_status_cards,
    schedules_by_title,
    schedule_lookup,
)

load_dotenv()

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE = os.getenv("AIRTABLE_TABLE", "TitleLog")

airtable_table = None
if Api and AIRTABLE_API_KEY and AIRTABLE_BASE_ID:
    try:
        api = Api(AIRTABLE_API_KEY)
        airtable_table = api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE)
    except Exception as e:
        logging.getLogger(__name__).warning(f"Airtable not configured: {e}")

# ========= UTC helpers & constants =========
UTC = timezone.utc
SHIFT_HOURS = 12  # default shift window

def now_utc() -> datetime:
    return datetime.now(UTC)

def parse_iso_utc(s: str) -> datetime | None:
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
    """Naive ISO key 'YYYY-MM-DDTHH:MM:SS' (UTC, no tzinfo, :00 seconds)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0).isoformat()

def to_iso_utc(val) -> str:
    """Normalize datetime/iso-ish string to ISO8601 in UTC."""
    if isinstance(val, datetime):
        dt = val
    else:
        dt = parse_iso_utc(val) or datetime.fromisoformat(str(val)).replace(tzinfo=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()

def airtable_upsert(record_type: str, payload: dict):
    """Write a row to Airtable using standard schema; no-op if not configured."""
    if not airtable_table:
        return
    fields = {
        "Type": record_type,  # reservation | activation | assignment | release
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
    if payload.get("SlotStartUTC"):
        fields["SlotStartUTC"] = to_iso_utc(payload["SlotStartUTC"])
    if payload.get("SlotEndUTC"):
        fields["SlotEndUTC"] = to_iso_utc(payload["SlotEndUTC"])
    try:
        airtable_table.create(fields)
    except Exception as e:
        logging.getLogger(__name__).error(f"Airtable create failed: {e}")

# ========= Static Titles (local icons) =========
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
    }
}

if isinstance(TITLES_CATALOG, tuple) and len(TITLES_CATALOG) == 1 and isinstance(TITLES_CATALOG[0], dict):
    TITLES_CATALOG = TITLES_CATALOG[0]

ORDERED_TITLES = list(TITLES_CATALOG.keys())
REQUESTABLE = {t for t in ORDERED_TITLES if t != "Guardian of Harmony"}

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

# ========= Persistence & Thread Safety (legacy JSON/CSV) =========
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATIC_DIR = os.path.join(BASE_DIR, "static", "icons")
os.makedirs(STATIC_DIR, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "titles_state.json")
CSV_FILE   = os.path.join(DATA_DIR, "requests.csv")

state: dict = {}
state_lock = RLock()

logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger(__name__)

# ========= State & Log Helpers (legacy; safe to keep while you migrate) =========
def initialize_state():
    global state
    state = {
        'titles': {},
        'config': {},
        'schedules': {},
        'sent_reminders': [],
        'activated_slots': {}
    }

def load_state():
    global state
    with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                state.setdefault('titles', {})
                state.setdefault('config', {})
                state.setdefault('schedules', {})
                state.setdefault('activated_slots', {})
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading state file: {e}. Re-initializing.")
                initialize_state()
        else:
            initialize_state()

def _save_state_unlocked():
    temp_file = STATE_FILE + ".tmp"
    try:
        with open(temp_file, 'w') as f:
            json.dump(state, f, indent=4)
        os.replace(temp_file, STATE_FILE)
    except IOError as e:
        logger.error(f"Error saving state file: {e}")

def save_state():
    with state_lock:
        _save_state_unlocked()

def log_to_csv(request_data: dict):
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
    with state_lock:
        titles = state.setdefault('titles', {})
        for title_name in TITLES_CATALOG:
            if title_name not in titles:
                titles[title_name] = {'holder': None, 'claim_date': None, 'expiry_date': None}
    save_state()

def get_shift_hours():
    with state_lock:
        return state.get('config', {}).get('shift_hours', SHIFT_HOURS)

# ========= Notification Helpers =========
def send_webhook_notification(data, reminder=False):
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL is not set. Skipping notification.")
        return
    role_tag = f"<@&{GUARDIAN_ROLE_ID}>" if GUARDIAN_ROLE_ID else ""
    if reminder:
        title = f"Reminder: {data.get('title_name','-')} shift starts soon!"
        content = f"{role_tag} The {db_get_shift_hours()}-hour shift for **{data.get('title_name','-')}** by **{data.get('in_game_name','-')}** starts in 5 minutes!"
    else:
        title = "New Title Reservation"
        content = f"{role_tag} A new title was reserved via the web form."
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["roles"]},
        "embeds": [{
            "title": title,
            "color": 5814783,
            "fields": [
                {"name": "Title", "value": data.get('title_name','-'), "inline": True},
                {"name": "In-Game Name", "value": data.get('in_game_name','-'), "inline": True},
                {"name": "Coordinates", "value": data.get('coordinates','-'), "inline": True},
                {"name": "Submitted By", "value": data.get('discord_user','Web Form'), "inline": False}
            ],
            "timestamp": data.get('timestamp')
        }]
    }
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=8).raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Webhook send failed: {e}")

def title_is_vacant_now(title_name: str) -> bool:
    with state_lock:
        t = state.get('titles', {}).get(title_name, {})
        if not t.get('holder'):
            return True
        exp_str = t.get('expiry_date')
    if not exp_str:
        return False
    expiry_dt = parse_iso_utc(exp_str)
    return bool(expiry_dt and now_utc() >= expiry_dt)

# ========= Activation / Release Helpers (legacy JSON path for auto-activate) =========
def activate_slot(title_name: str, ign: str, start_dt: datetime):
    end_dt = start_dt + timedelta(hours=db_get_shift_hours())
    with state_lock:
        state['titles'][title_name].update({
            'holder': {'name': ign, 'coords': '-', 'discord_id': 0},
            'claim_date': start_dt.isoformat(),
            'expiry_date': None if title_name == "Guardian of Harmony" else end_dt.isoformat(),
        })
        activated = state.setdefault('activated_slots', {})
        already = activated.get(title_name) or {}
        already[iso_slot_key_naive(start_dt)] = True
        activated[title_name] = already
    _save_state_unlocked()

    airtable_upsert("activation", {
        "Title": title_name,
        "IGN": ign,
        "Coordinates": "-",
        "SlotStartUTC": start_dt,
        "SlotEndUTC": None if title_name == "Guardian of Harmony" else end_dt,
        "Source": "Auto-Activate",
        "DiscordUser": "-"
    })

def _scan_expired_titles(now_dt: datetime) -> list[str]:
    expired = []
    with state_lock:
        for title_name, data in state.get('titles', {}).items():
            exp = data.get('expiry_date')
            if data.get('holder') and exp:
                exp_dt = parse_iso_utc(exp)
                if exp_dt and now_dt >= exp_dt:
                    expired.append(title_name)
    return expired

def _release_title_blocking(title_name: str) -> bool:
    with state_lock:
        titles = state.get('titles', {})
        if title_name not in titles:
            return False
        titles[title_name].update({'holder': None, 'claim_date': None, 'expiry_date': None})
    _save_state_unlocked()
    return True

# ========= Flask App Setup =========
app = Flask(__name__)
app.secret_key = FLASK_SECRET

# ===== NEW: SQLAlchemy config (same app) =====
# Local default: instance/app.db; on Render Disk: set DATABASE_URL=sqlite:////opt/render/data/app.db
os.makedirs(app.instance_path, exist_ok=True)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///instance/app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Ensure the directory for the SQLite file exists (both relative and absolute forms)
uri = app.config["SQLALCHEMY_DATABASE_URI"]

def _ensure_sqlite_dir(sqlite_uri: str) -> None:
    # sqlite:///relative/path.db    -> relative to CWD
    # sqlite:////absolute/path.db   -> absolute path
    if not sqlite_uri.startswith("sqlite:"):
        return
    path_part = sqlite_uri.replace("sqlite:///", "", 1)
    is_abs = sqlite_uri.startswith("sqlite:////")
    if is_abs:
        path_part = "/" + path_part  # -> "/opt/render/data/app.db"
    db_dir = os.path.dirname(os.path.abspath(path_part))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

_ensure_sqlite_dir(uri)

db.init_app(app)

def _sqlite_pragmas(dbapi_connection, connection_record):
    # Safe, per-connection PRAGMAs for SQLite
    try:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.close()
    except Exception:
        pass  # ignore for non-sqlite or if PRAGMAs fail

with app.app_context():
    if uri.startswith("sqlite:"):
        event.listen(db.engine, "connect", _sqlite_pragmas)
    db.create_all()

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}, 200

def run_flask_app():
    port = int(os.getenv("PORT", "10000"))
    logger.info(f"Starting Flask server on port {port}")
    serve(app, host='0.0.0.0', port=port)

# ========= Discord Cog =========
class TitleCog(commands.Cog, name="TitleManager"):
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.title_check_loop.start()

    async def announce(self, message: str):
        channel_id = None
        with state_lock:
            channel_id = state.get('config', {}).get('announcement_channel')
        if not channel_id:
            return
        try:
            channel = await self.bot.fetch_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(message)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.error(f"Could not send to announcement channel {channel_id}: {e}")

    async def force_release_logic(self, title_name: str, reason: str):
        ok = await asyncio.to_thread(_release_title_blocking, title_name)
        if not ok:
            return
        await self.announce(f"TITLE RELEASED: **'{title_name}'** is now available. Reason: {reason}")
        logger.info(f"[RELEASE] {title_name} released. Reason: {reason}")
        await asyncio.to_thread(airtable_upsert, "release", {
            "Title": title_name, "Reason": reason, "Source": "System", "DiscordUser": "-"
        })

    @tasks.loop(seconds=60)
    async def title_check_loop(self):
        await self.bot.wait_until_ready()
        now = now_utc()

        # Legacy JSON auto-release/activate still running; DB is initialized for routes/templates.
        to_release = await asyncio.to_thread(_scan_expired_titles, now)
        for title_name in to_release:
            await self.force_release_logic(title_name, "Title expired.")

        to_activate: list[tuple[str, str, datetime]] = []
        with state_lock:
            schedules = state.get('schedules', {})
            activated = state.get('activated_slots', {})
            for title_name, slots in schedules.items():
                for slot_key, entry in slots.items():
                    start_dt = parse_iso_utc(slot_key) or datetime.fromisoformat(slot_key).replace(tzinfo=UTC)
                    if start_dt > now:
                        continue
                    if activated.get(title_name, {}).get(slot_key):
                        continue
                    ign = entry['ign'] if isinstance(entry, dict) else str(entry)
                    to_activate.append((title_name, ign, start_dt))
        for title_name, ign, start_dt in to_activate:
            activate_slot(title_name, ign, start_dt)
            await self.announce(f"AUTO-ACTIVATED: **{title_name}** → **{ign}** (slot start reached).")
            logger.info(f"[AUTO-ACTIVATE] {title_name} -> {ign} at {start_dt.isoformat()}")

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
                        if expiry:
                            remaining = max(0, int((expiry - now_utc()).total_seconds()))
                            status += f"**Held by:** {holder_name}\n*Expires in: {str(timedelta(seconds=remaining))}*"
                        else:
                            status += f"**Held by:** {holder_name}\n*Expiry: Invalid*"
                    else:
                        status += f"**Held by:** {holder_name}\n*Expires: Never*"
                else:
                    status += "**Status:** Available"
                embed.add_field(name=title_name, value=status, inline=False)
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
        expiry_date_iso = None if title_name == "Guardian of Harmony" else (now + timedelta(hours=db_get_shift_hours())).isoformat()
        with state_lock:
            state['titles'][title_name].update({
                'holder': {'name': ign, 'coords': '-', 'discord_id': ctx.author.id},
                'claim_date': now.isoformat(),
                'expiry_date': expiry_date_iso
            })
        _save_state_unlocked()

        airtable_upsert("assignment", {
            "Title": title_name,
            "IGN": ign,
            "Coordinates": "-",
            "SlotStartUTC": now,
            "SlotEndUTC": expiry_date_iso,
            "Source": "Discord Command",
            "DiscordUser": getattr(ctx.author, "display_name", str(ctx.author))
        })
        await self.announce(f"SHIFT CHANGE: **{ign}** has been granted **'{title_name}'**.")
        logger.info(f"[ASSIGN] {getattr(ctx.author, 'display_name', 'admin')} assigned {title_name} -> {ign}")

    @commands.command(help="Set the announcement channel. Usage: !set_announce <#channel>")
    @commands.has_permissions(administrator=True)
    async def set_announce(self, ctx, channel: discord.TextChannel):
        with state_lock:
            state.setdefault('config', {})['announcement_channel'] = channel.id
        _save_state_unlocked()
        await ctx.send(f"Announcement channel set to {channel.mention}.")

# ========= Register Flask routes from web_routes.py =========
register_routes(
    app=app,
    deps=dict(
        ORDERED_TITLES=ORDERED_TITLES, TITLES_CATALOG=TITLES_CATALOG,
        REQUESTABLE=REQUESTABLE, ADMIN_PIN=ADMIN_PIN,
        state=state, save_state=save_state, log_to_csv=log_to_csv,
        parse_iso_utc=parse_iso_utc, now_utc=now_utc,
        iso_slot_key_naive=iso_slot_key_naive,
        title_is_vacant_now=title_is_vacant_now,
        get_shift_hours=db_get_shift_hours,  # DB-backed shift hours for templates
        bot=bot,
        state_lock=state_lock,
        send_webhook_notification=send_webhook_notification,

        # DB + models + helpers (web_routes may or may not use them directly)
        db=db,
        models=dict(Title=Title, Reservation=Reservation, ActiveTitle=ActiveTitle, RequestLog=RequestLog),
        db_helpers=dict(
            compute_slots=compute_slots,
            requestable_title_names=requestable_title_names,
            title_status_cards=title_status_cards,
            schedules_by_title=schedules_by_title,
            set_shift_hours=db_set_shift_hours,
            schedule_lookup=schedule_lookup,
        )
    )
)

# ========= Discord Bot Lifecycle =========
@bot.event
async def on_ready():
    """Start Flask (waitress) once bot is ready; initialize legacy state."""
    load_state()
    initialize_titles()
    await bot.add_cog(TitleCog(bot))
    logger.info(f'{bot.user.name} has connected to Discord!')
    Thread(target=run_flask_app, daemon=True).start()

# ========= Main Entry Point =========
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.critical("FATAL: DISCORD_TOKEN environment variable not set.")
    else:
        try:
            bot.run(DISCORD_TOKEN)
        except discord.errors.LoginFailure:
            logger.critical("FATAL: Improper token has been passed.")