# main.py — CORE + DISCORD BOT + APP SETUP (routes registered from web_routes.py)

import os
import csv
import json
import logging
import asyncio
import requests
from threading import Thread, Lock
from datetime import datetime, timedelta, timezone

from flask import Flask
from waitress import serve

import discord
from discord.ext import commands, tasks

from web_routes import register_routes
from pyairtable import Api

# ========= Logging =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s:%(levelname)s:%(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ========= UTC helpers & constants =========
UTC = timezone.utc
SHIFT_HOURS = 12  # default reservation/assignment window

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
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0).isoformat()

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
    }
}
# Safety net in case of accidental trailing-comma tuple
if isinstance(TITLES_CATALOG, tuple) and len(TITLES_CATALOG) == 1 and isinstance(TITLES_CATALOG[0], dict):
    TITLES_CATALOG = TITLES_CATALOG[0]

ORDERED_TITLES = list(TITLES_CATALOG.keys())
REQUESTABLE = {t for t in ORDERED_TITLES if t != "Guardian of Harmony"}

# ========= Environment & Config =========
WEBHOOK_URL     = os.getenv("WEBHOOK_URL")
ADMIN_PIN       = os.getenv("ADMIN_PIN", "letmein")
DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN")
FLASK_SECRET    = os.getenv("FLASK_SECRET", "a-strong-dev-secret-key")
GUARDIAN_ROLE_ID = os.getenv("GUARDIAN_ROLE_ID")

# Airtable (optional)
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE   = os.getenv("AIRTABLE_TABLE", "TitleLog")

airtable_table = None
if AIRTABLE_API_KEY and AIRTABLE_BASE_ID:
    try:
        api = Api(AIRTABLE_API_KEY)
        airtable_table = api.table(AIRTABLE_BASE_ID, AIRTABLE_TABLE)
    except Exception as e:
        logger.warning(f"Airtable not configured: {e}")

def to_iso_utc(val) -> str:
    if isinstance(val, datetime):
        dt = val
    else:
        dt = parse_iso_utc(val) or datetime.fromisoformat(str(val)).replace(tzinfo=UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()

def airtable_upsert(record_type: str, payload: dict):
    """
    Writes a row to Airtable. Expected fields in your base:
      Type, Title, IGN, Coordinates, SlotStartUTC, SlotEndUTC, EventTimeUTC, Reason, Source, DiscordUser
    """
    if not airtable_table:
        return
    fields = {
        "Type": record_type,
        "Title": payload.get("Title"),
        "IGN": payload.get("IGN"),
        "Coordinates": payload.get("Coordinates"),
        "SlotStartUTC": None,
        "SlotEndUTC": None,
        "EventTimeUTC": now_utc().isoformat(),
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
        logger.error(f"Airtable create failed: {e}")

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
state_lock = Lock()

def initialize_state():
    global state
    state = {
        'titles': {},
        'config': {},
        'schedules': {},
        'sent_reminders': [],
        'activated_slots': {}  # dict[title][slot_key] = True
    }

def load_state():
    global state
    with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    state = json.load(f)
                # ensure keys
                state.setdefault('titles', {})
                state.setdefault('config', {})
                state.setdefault('schedules', {})
                state.setdefault('activated_slots', {})
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading state: {e}. Re-initializing.")
                initialize_state()
        else:
            initialize_state()

def save_state():
    with state_lock:
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=4)
            os.replace(tmp, STATE_FILE)
        except IOError as e:
            logger.error(f"Error saving state: {e}")

def log_to_csv(request_data):
    with state_lock:
        exists = os.path.isfile(CSV_FILE)
        try:
            with open(CSV_FILE, 'a', newline='', encoding='utf-8') as csvfile:
                fieldnames = ['timestamp', 'title_name', 'in_game_name', 'coordinates', 'discord_user']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                if not exists:
                    writer.writeheader()
                writer.writerow(request_data)
        except IOError as e:
            logger.error(f"Error writing CSV: {e}")

def initialize_titles():
    with state_lock:
        state.setdefault('titles', {})
        for title_name in TITLES_CATALOG:
            state['titles'].setdefault(title_name, {
                'holder': None, 'claim_date': None, 'expiry_date': None
            })
    save_state()

def title_is_vacant_now(title_name: str) -> bool:
    t = state.get('titles', {}).get(title_name, {})
    if not t.get('holder'):
        return True
    exp_str = t.get('expiry_date')
    if not exp_str:
        return False  # held indefinitely
    expiry_dt = parse_iso_utc(exp_str)
    return bool(expiry_dt and now_utc() >= expiry_dt)

# ========= Notification Helpers =========
def send_webhook_notification(data, reminder=False):
    if not WEBHOOK_URL:
        logger.warning("WEBHOOK_URL not set; skipping webhook.")
        return
    role_tag = f"<@&{GUARDIAN_ROLE_ID}>" if GUARDIAN_ROLE_ID else ""
    if reminder:
        title = f"Reminder: {data.get('title_name','-')} shift starts soon!"
        content = f"{role_tag} The {get_shift_hours()}-hour shift for **{data.get('title_name','-')}** by **{data.get('in_game_name','-')}** starts in 5 minutes!"
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
        r = requests.post(WEBHOOK_URL, json=payload, timeout=8)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Webhook send failed: {e}")

# ========= Config helper =========
def get_shift_hours():
    with state_lock:
        return state.get('config', {}).get('shift_hours', SHIFT_HOURS)

# ========= Blocking helpers (run off the event loop) =========
def _scan_expired_titles(now_dt):
    expired = []
    with state_lock:
        for title_name, data in state.get('titles', {}).items():
            exp = data.get('expiry_date')
            if data.get('holder') and exp:
                exp_dt = parse_iso_utc(exp)
                if exp_dt and now_dt >= exp_dt:
                    expired.append(title_name)
    return expired

def _release_title_blocking(title_name, reason):
    if title_name not in state.get('titles', {}):
        return False
    with state_lock:
        state['titles'][title_name].update({
            'holder': None, 'claim_date': None, 'expiry_date': None
        })
        save_state()
    return True

def _activate_slot_blocking(title_name: str, ign: str, start_dt: datetime):
    """Set holder & expiry; mark slot as activated; save state. Returns end_dt (or None for GoH)."""
    end_dt = start_dt + timedelta(hours=get_shift_hours())
    with state_lock:
        state['titles'].setdefault(title_name, {'holder': None, 'claim_date': None, 'expiry_date': None})
        state['titles'][title_name].update({
            'holder': {'name': ign, 'coords': '-', 'discord_id': 0},
            'claim_date': start_dt.isoformat(),
            'expiry_date': None if title_name == "Guardian of Harmony" else end_dt.isoformat(),
        })
        state.setdefault('activated_slots', {})
        already = state['activated_slots'].get(title_name) or {}
        already[iso_slot_key_naive(start_dt)] = True  # JSON-friendly boolean flag
        state['activated_slots'][title_name] = already
        save_state()
    return None if title_name == "Guardian of Harmony" else end_dt

# ========= Flask App Setup =========
app = Flask(__name__)
app.secret_key = FLASK_SECRET

def run_flask_app():
    port = int(os.getenv("PORT", "10000"))
    logger.info(f"Starting Flask server on port {port}")
    serve(app, host='0.0.0.0', port=port)

# ========= Discord Cog =========
class TitleCog(commands.Cog, name="TitleManager"):
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.title_check_loop.start()

    async def force_release_logic(self, title_name, reason):
        ok = await asyncio.to_thread(_release_title_blocking, title_name, reason)
        if not ok:
            return
        await self.announce(f"TITLE RELEASED: **'{title_name}'** is now available. Reason: {reason}")
        logger.info(f"[RELEASE] {title_name} released. Reason: {reason}")
        await asyncio.to_thread(airtable_upsert, "release", {
            "Title": title_name, "Reason": reason, "Source": "System", "DiscordUser": "-"
        })

    @tasks.loop(seconds=60)
    async def title_check_loop(self):
        """Release expired titles, auto-activate due slots (non-blocking)."""
        await self.bot.wait_until_ready()
        now = now_utc()

        # 1) Release expired
        titles_to_release = await asyncio.to_thread(_scan_expired_titles, now)
        for title_name in titles_to_release:
            await self.force_release_logic(title_name, "Title expired.")

        # 2) Collect due activations
        def _collect_due(now_dt):
            out = []
            with state_lock:
                schedules = state.get('schedules', {})
                activated = state.get('activated_slots', {})
                for title_name, slots in schedules.items():
                    for slot_key, entry in slots.items():
                        try:
                            start_dt = parse_iso_utc(slot_key) or datetime.fromisoformat(slot_key).replace(tzinfo=UTC)
                        except Exception:
                            continue
                        if start_dt > now_dt:
                            continue
                        if activated.get(title_name, {}).get(slot_key):
                            continue
                        ign = entry['ign'] if isinstance(entry, dict) else str(entry)
                        out.append((title_name, ign, start_dt))
            return out

        to_activate = await asyncio.to_thread(_collect_due, now)

        # 3) Activate
        for title_name, ign, start_dt in to_activate:
            end_dt = await asyncio.to_thread(_activate_slot_blocking, title_name, ign, start_dt)
            await self.announce(f"AUTO-ACTIVATED: **{title_name}** → **{ign}** (slot start reached).")
            logger.info(f"[AUTO-ACTIVATE] {title_name} -> {ign} at {start_dt.isoformat()}")
            await asyncio.to_thread(airtable_upsert, "activation", {
                "Title": title_name,
                "IGN": ign,
                "Coordinates": "-",
                "SlotStartUTC": start_dt,
                "SlotEndUTC": end_dt,
                "Source": "Auto-Activate",
                "DiscordUser": "-"
            })

    async def announce(self, message):
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
        expiry_date_iso = None if title_name == "Guardian of Harmony" else (now + timedelta(hours=get_shift_hours())).isoformat()

        # blocking mutation off-thread
        def _assign_blocking():
            with state_lock:
                state['titles'].setdefault(title_name, {'holder': None, 'claim_date': None, 'expiry_date': None})
                state['titles'][title_name].update({
                    'holder': {'name': ign, 'coords': '-', 'discord_id': ctx.author.id},
                    'claim_date': now.isoformat(),
                    'expiry_date': expiry_date_iso
                })
                save_state()

        await asyncio.to_thread(_assign_blocking)

        await asyncio.to_thread(airtable_upsert, "assignment", {
            "Title": title_name,
            "IGN": ign,
            "Coordinates": "-",
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
        def _set_channel_blocking():
            with state_lock:
                state.setdefault('config', {})['announcement_channel'] = channel.id
                save_state()
        await asyncio.to_thread(_set_channel_blocking)
        await ctx.send(f"Announcement channel set to {channel.mention}.")

# ========= Register routes (Flask) =========
register_routes(
    app=app,
    deps=dict(
        ORDERED_TITLES=ORDERED_TITLES,
        TITLES_CATALOG=TITLES_CATALOG,
        REQUESTABLE=REQUESTABLE,
        ADMIN_PIN=ADMIN_PIN,
        state=state,
        save_state=save_state,
        log_to_csv=log_to_csv,
        parse_iso_utc=parse_iso_utc,
        now_utc=now_utc,
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
    load_state()
    initialize_titles()
    await bot.add_cog(TitleCog(bot))
    logger.info(f'{bot.user.name} has connected to Discord!')
    # Run Flask in a background thread
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