# web_routes.py — All Flask routes (dashboard + admin)

import os
import csv
from datetime import datetime, timedelta, timezone
from flask import render_template, request, redirect, url_for, flash, session

def register_routes(app, deps):
    """
    Injects dependencies from main.py and registers all Flask routes.
    """
    # ----- Unpack deps -----
    ORDERED_TITLES = deps['ORDERED_TITLES']
    TITLES_CATALOG = deps['TITLES_CATALOG']
    REQUESTABLE = deps['REQUESTABLE']
    ADMIN_PIN = deps['ADMIN_PIN']

    state = deps['state']
    save_state = deps['save_state']
    log_to_csv = deps['log_to_csv']

    parse_iso_utc = deps['parse_iso_utc']
    now_utc = deps['now_utc']
    iso_slot_key_naive = deps['iso_slot_key_naive']
    title_is_vacant_now = deps['title_is_vacant_now']
    get_shift_hours = deps['get_shift_hours']

    state_lock = deps['state_lock']
    send_webhook_notification = deps['send_webhook_notification']

    UTC = timezone.utc

    # ----- Helpers -----
    def is_admin() -> bool:
        return bool(session.get("is_admin"))

    def _safe_parse_slot(date_str: str, time_str: str) -> datetime | None:
        try:
            # time_str expected like '00:00' or '12:00'
            return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            return None

    # Build a lookup: { 'YYYY-MM-DD': { 'HH:MM': { title: entry } } }
    def _build_schedule_lookup(schedules: dict) -> dict:
        out: dict[str, dict[str, dict]] = {}
        for title, slots in schedules.items():
            for slot_key, entry in (slots or {}).items():
                dt = parse_iso_utc(slot_key) or datetime.fromisoformat(slot_key).replace(tzinfo=UTC)
                day_key = dt.date().isoformat()
                time_key = dt.strftime("%H:%M")
                out.setdefault(day_key, {}).setdefault(time_key, {})[title] = entry
        return out

    # ----- Public pages -----
    @app.route("/")
    def dashboard():
        # Titles panel
        titles_data = []
        schedules = {}
        with state_lock:
            titles_dict = state.get('titles', {})
            schedules = state.get('schedules', {}) or {}

            for title_name in ORDERED_TITLES:
                data = titles_dict.get(title_name, {}) or {}
                holder_info = "None"
                if data.get('holder'):
                    holder = data['holder'] or {}
                    holder_info = f"{holder.get('name','?')} ({holder.get('coords','-')})"

                remaining = "Never" if (title_name == "Guardian of Harmony" and data.get('holder')) else "N/A"
                if data.get('expiry_date'):
                    expiry = parse_iso_utc(data['expiry_date'])
                    if expiry:
                        delta = expiry - now_utc()
                        remaining = str(timedelta(seconds=max(0, int(delta.total_seconds()))))
                    else:
                        remaining = "Invalid Date"

                titles_data.append({
                    'name': title_name,
                    'holder': holder_info,
                    'expires_in': remaining,
                    'icon': TITLES_CATALOG[title_name]['image'],
                    'buffs': TITLES_CATALOG[title_name]['effects'],
                })

        # Calendar grid (2 weeks, 12-hour slot headings are based on shift hours)
        today = now_utc().date()
        days = [(today + timedelta(days=i)) for i in range(14)]
        # Show 00:00 and 12:00 columns (visual schedule), even if shift_hours changes
        hours = ["00:00", "12:00"]

        return render_template(
            'dashboard.html',
            titles=titles_data,
            days=days,
            hours=hours,
            schedules=schedules,
            today=today.strftime('%Y-%m-%d'),
            requestable_titles=REQUESTABLE,
            shift_hours=get_shift_hours()
        )

    @app.route("/log")
    def view_log():
        log_data = []
        csv_path = os.path.join(os.path.dirname(__file__), "data", "requests.csv")
        if os.path.exists(csv_path):
            try:
                with open(csv_path, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    log_data = list(reader)
            except IOError as e:
                flash(f"Could not read log file: {e}")
        # show newest first
        return render_template('log.html', logs=reversed(log_data))

    @app.route("/book-slot", methods=['POST'])
    def book_slot():
        title_name = (request.form.get('title') or '').strip()
        ign = (request.form.get('ign') or '').strip()
        coords = (request.form.get('coords') or '').strip()
        date_str = (request.form.get('date') or '').strip()
        time_str = (request.form.get('time') or '').strip()

        # Validation
        if not all([title_name, ign, date_str, time_str, coords]):
            flash("All fields (Title, IGN, Coords, Date, Time) are required.")
            return redirect(url_for("dashboard"))
        if title_name not in REQUESTABLE:
            flash("This title cannot be requested.")
            return redirect(url_for("dashboard"))

        schedule_time = _safe_parse_slot(date_str, time_str)
        if not schedule_time:
            flash("Invalid date or time format.")
            return redirect(url_for("dashboard"))
        if schedule_time < now_utc():
            flash("Cannot schedule a time in the past.")
            return redirect(url_for("dashboard"))

        schedule_key = iso_slot_key_naive(schedule_time)

        # Reserve if free
        with state_lock:
            schedules_for_title = state.setdefault('schedules', {}).setdefault(title_name, {})
            if schedule_key in schedules_for_title:
                reserver = schedules_for_title[schedule_key]
                ign_val = reserver.get('ign') if isinstance(reserver, dict) else reserver
                # already reserved
                flash(f"That slot for {title_name} is already reserved by {ign_val}.")
                return redirect(url_for("dashboard"))

            schedules_for_title[schedule_key] = {"ign": ign, "coords": coords}

        # Persist (after lock)
        save_state()

        # Notify + CSV log (no lock)
        csv_data = {
            "timestamp": now_utc().isoformat(),
            "title_name": title_name,
            "in_game_name": ign,
            "coordinates": coords,
            "discord_user": "Web Form"
        }
        log_to_csv(csv_data)
        send_webhook_notification({
            "title_name": title_name,
            "in_game_name": ign,
            "coordinates": coords,
            "timestamp": now_utc().isoformat(),
            "discord_user": "Web Form"
        }, reminder=False)

        flash(f"Reserved {title_name} for {ign} on {date_str} at {time_str} UTC.")
        return redirect(url_for("dashboard"))

    # ----- Admin Authentication -----
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            pin = (request.form.get("pin") or "").strip()
            if pin == ADMIN_PIN:
                session["is_admin"] = True
                flash("Welcome, admin.")
                return redirect(url_for("admin_home"))
            flash("Incorrect PIN.")
        return render_template("admin_login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        flash("Logged out.")
        return redirect(url_for("dashboard"))

    # ----- Admin Dashboard & Actions -----
    @app.route("/admin")
    def admin_home():
        if not is_admin():
            return redirect(url_for("admin_login"))

        active_titles = []
        schedules = {}
        with state_lock:
            titles_dict = state.get('titles', {}) or {}
            schedules = state.get('schedules', {}) or {}
            for title_name in ORDERED_TITLES:
                t = titles_dict.get(title_name, {}) or {}
                if t.get("holder"):
                    expires_str = "Never"
                    if t.get("expiry_date"):
                        exp_dt = parse_iso_utc(t["expiry_date"])
                        expires_str = exp_dt.isoformat() if exp_dt else "Invalid"
                    active_titles.append({
                        "title": title_name,
                        "holder": t["holder"].get("name", "-"),
                        "expires": expires_str
                    })

        # Build a compact view of reservations for the next 14 days
        today = now_utc().date()
        days = [(today + timedelta(days=i)) for i in range(14)]
        slots = ["00:00", "12:00"]
        schedule_lookup = _build_schedule_lookup(schedules)

        return render_template(
            "admin.html",
            active_titles=active_titles,
            all_titles=ORDERED_TITLES,
            requestable_titles=REQUESTABLE,
            today=today.strftime('%Y-%m-%d'),
            days=days,
            slots=slots,
            schedule_lookup=schedule_lookup,
            shift_hours=get_shift_hours()
        )

    @app.route("/admin/force-release", methods=["POST"])
    def admin_force_release():
        if not is_admin():
            return redirect(url_for("admin_login"))
        title = (request.form.get("title") or "").strip()
        if title not in ORDERED_TITLES:
            flash(f"Title '{title}' not found.")
            return redirect(url_for("admin_home"))

        with state_lock:
            state["titles"].setdefault(title, {})
            state["titles"][title].update({'holder': None, 'claim_date': None, 'expiry_date': None})
        save_state()

        flash(f"Force-released title '{title}'.")
        return redirect(url_for("admin_home"))

    @app.route("/admin/manual-assign", methods=["POST"])
    def admin_manual_assign():
        if not is_admin():
            return redirect(url_for("admin_login"))
        title = (request.form.get("title") or "").strip()
        ign = (request.form.get("ign") or "").strip()

        if not title or not ign or title not in ORDERED_TITLES:
            flash("Bad manual assignment. Title and IGN required.")
            return redirect(url_for("admin_home"))

        now_dt = now_utc()
        expiry_date_iso = None if title == "Guardian of Harmony" else (now_dt + timedelta(hours=get_shift_hours())).isoformat()

        with state_lock:
            state["titles"].setdefault(title, {})
            state["titles"][title].update({
                "holder": {"name": ign, "coords": "-", "discord_id": 0},
                "claim_date": now_dt.isoformat(),
                "expiry_date": expiry_date_iso,
            })
        save_state()

        flash(f"Manually assigned '{title}' to {ign}.")
        return redirect(url_for("admin_home"))

    @app.route("/admin/manual-set-slot", methods=["POST"])
    def admin_manual_set_slot():
        if not is_admin():
            return redirect(url_for("admin_login"))

        title = (request.form.get("title") or "").strip()
        ign = (request.form.get("ign") or "").strip()
        date_str = (request.form.get("date") or "").strip()
        slot = (request.form.get("slot") or "").strip()

        if not all([title, ign, date_str, slot]):
            flash("Missing data for manual slot assignment.")
            return redirect(url_for("admin_home"))
        if title == "Guardian of Harmony":
            flash("'Guardian of Harmony' cannot be assigned to a timed slot.")
            return redirect(url_for("admin_home"))

        start_dt = _safe_parse_slot(date_str, slot)
        if not start_dt:
            flash("Invalid date or slot format.")
            return redirect(url_for("admin_home"))
        end_dt = start_dt + timedelta(hours=get_shift_hours())

        with state_lock:
            state["titles"].setdefault(title, {})
            state["titles"][title].update({
                "holder": {"name": ign, "coords": "-", "discord_id": 0},
                "claim_date": start_dt.isoformat(),
                "expiry_date": end_dt.isoformat(),
            })
        save_state()

        flash(f"Manually set '{title}' for {ign} in the {date_str} {slot} slot.")
        return redirect(url_for("admin_home"))

    @app.route("/admin/set-shift-hours", methods=["POST"])
    def admin_set_shift_hours():
        """
        Optional: update the global shift duration (hours).
        This affects future assignments/activations.
        """
        if not is_admin():
            return redirect(url_for("admin_login"))
        raw = (request.form.get("hours") or "").strip()
        try:
            hours = int(raw)
            if not (1 <= hours <= 48):
                raise ValueError
        except ValueError:
            flash("Shift hours must be an integer between 1 and 48.")
            return redirect(url_for("admin_home"))

        with state_lock:
            cfg = state.setdefault("config", {})
            cfg["shift_hours"] = hours
        save_state()

        flash(f"Shift hours updated to {hours} hours.")
        return redirect(url_for("admin_home"))