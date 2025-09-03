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
    get_shift_hours = deps['get_shift_hours']
    state_lock = deps['state_lock']
    send_webhook_notification = deps['send_webhook_notification']
    UTC = timezone.utc

    # ----- Helpers -----
    def is_admin() -> bool:
        """Checks if the current session belongs to an admin."""
        return bool(session.get("is_admin"))

    # ----- Public pages -----
    @app.route("/")
    def dashboard():
        titles_data = []
        schedules = {}
        with state_lock:
            # Deep copy to prevent modification during iteration
            titles_dict = state.get('titles', {})
            schedules = state.get('schedules', {})
            
            for title_name in ORDERED_TITLES:
                data = titles_dict.get(title_name, {})
                holder_info = "None"
                if data.get('holder'):
                    holder = data['holder']
                    holder_info = f"{holder.get('name','?')} ({holder.get('coords','-')})"

                remaining = "Never" if title_name == "Guardian of Harmony" and data.get('holder') else "N/A"
                if data.get('expiry_date'):
                    expiry = parse_iso_utc(data['expiry_date'])
                    if expiry:
                        delta = expiry - now_utc()
                        remaining = str(timedelta(seconds=int(delta.total_seconds()))) if delta.total_seconds() > 0 else "Expired"
                    else:
                        remaining = "Invalid Date"
                
                titles_data.append({
                    'name': title_name, 'holder': holder_info, 'expires_in': remaining,
                    'icon': TITLES_CATALOG[title_name]['image'], 'buffs': TITLES_CATALOG[title_name]['effects'],
                })
        
        today = now_utc().date()
        days = [(today + timedelta(days=i)) for i in range(14)]
        hours = ["00:00", "12:00"]

        return render_template(
            'dashboard.html', titles=titles_data, days=days, hours=hours,
            schedules=schedules, today=today.strftime('%Y-%m-%d'),
            requestable_titles=REQUESTABLE
        )

    @app.route("/log")
    def view_log():
        log_data = []
        csv_path = os.path.join(os.path.dirname(__file__), "data", "requests.csv")
        # No lock needed for read-only access to a file that is append-only
        if os.path.exists(csv_path):
            try:
                with open(csv_path, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    log_data = list(reader)
            except IOError as e:
                flash(f"Could not read log file: {e}")
        return render_template('log.html', logs=reversed(log_data))

    @app.route("/book-slot", methods=['POST'])
    def book_slot():
        # --- FAILPROOFING: Sanitize and validate all inputs ---
        title_name = (request.form.get('title') or '').strip()
        ign = (request.form.get('ign') or '').strip()
        coords = (request.form.get('coords') or '').strip()
        date_str = (request.form.get('date') or '').strip()
        time_str = (request.form.get('time') or '').strip()

        if not all([title_name, ign, date_str, time_str, coords]):
            flash("All fields (Title, IGN, Coords, Date, Time) are required.")
            return redirect(url_for("dashboard"))
        if title_name not in REQUESTABLE:
            flash("This title cannot be requested.")
            return redirect(url_for("dashboard"))

        try:
            schedule_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            flash("Invalid date or time format.")
            return redirect(url_for("dashboard"))
        
        if schedule_time < now_utc():
            flash("Cannot schedule a time in the past.")
            return redirect(url_for("dashboard"))

        schedule_key = iso_slot_key_naive(schedule_time)

        with state_lock:
            schedules_for_title = state.setdefault('schedules', {}).setdefault(title_name, {})
            if schedule_key in schedules_for_title:
                reserver = schedules_for_title[schedule_key]
                ign_val = reserver.get('ign') if isinstance(reserver, dict) else reserver
                flash(f"That slot for {title_name} is already reserved by {ign_val}.")
                return redirect(url_for("dashboard"))

            # Reserve the slot
            schedules_for_title[schedule_key] = {"ign": ign, "coords": coords}
            
            csv_data = {
                "timestamp": now_utc().isoformat(), "title_name": title_name,
                "in_game_name": ign, "coordinates": coords, "discord_user": "Web Form"
            }
            log_to_csv(csv_data)
            send_webhook_notification(csv_data, reminder=False)
            save_state()

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
        if not is_admin(): return redirect(url_for("admin_login"))

        active_titles = []
        with state_lock:
            titles_dict = state.get('titles', {})
            for title_name in ORDERED_TITLES:
                t = titles_dict.get(title_name, {})
                if t and t.get("holder"):
                    expires_str = "Never"
                    if t.get("expiry_date"):
                        exp_dt = parse_iso_utc(t["expiry_date"])
                        expires_str = exp_dt.isoformat() if exp_dt else "Invalid"
                    active_titles.append({
                        "title": title_name, "holder": t["holder"].get("name", "-"), "expires": expires_str
                    })
        
        return render_template("admin.html", active_titles=active_titles, all_titles=ORDERED_TITLES)

    @app.route("/admin/force-release", methods=["POST"])
    def admin_force_release():
        if not is_admin(): return redirect(url_for("admin_login"))
        title = (request.form.get("title") or "").strip()
        
        if title in ORDERED_TITLES:
            with state_lock:
                state["titles"][title].update({
                    'holder': None, 'claim_date': None, 'expiry_date': None
                })
                save_state()
            flash(f"Force-released title '{title}'.")
        else:
            flash(f"Title '{title}' not found.")
            
        return redirect(url_for("admin_home"))

    @app.route("/admin/manual-assign", methods=["POST"])
    def admin_manual_assign():
        if not is_admin(): return redirect(url_for("admin_login"))
        title = (request.form.get("title") or "").strip()
        ign = (request.form.get("ign") or "").strip()

        if not (title and ign and title in ORDERED_TITLES):
            flash("Bad manual assignment. Title and IGN required.")
            return redirect(url_for("admin_home"))

        now = now_utc()
        expiry_date_iso = None # Guardian of Harmony is permanent
        if title != "Guardian of Harmony":
            expiry_date_iso = (now + timedelta(hours=get_shift_hours())).isoformat()

        with state_lock:
            state["titles"][title].update({
                "holder": {"name": ign, "coords": "-", "discord_id": 0},
                "claim_date": now.isoformat(), "expiry_date": expiry_date_iso,
            })
            save_state()
        flash(f"Manually assigned '{title}' to {ign}.")
        return redirect(url_for("admin_home"))

    @app.route("/admin/manual-set-slot", methods=["POST"])
    def admin_manual_set_slot():
        if not is_admin(): return redirect(url_for("admin_login"))
        
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

        try:
            start_dt = datetime.strptime(f"{date_str} {slot}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            end_dt = start_dt + timedelta(hours=get_shift_hours())
        except ValueError:
            flash("Invalid date or slot format.")
            return redirect(url_for("admin_home"))

        with state_lock:
            state["titles"][title].update({
                "holder": {"name": ign, "coords": "-", "discord_id": 0},
                "claim_date": start_dt.isoformat(), "expiry_date": end_dt.isoformat(),
            })
            save_state()
        flash(f"Manually set '{title}' for {ign} in the {date_str} {slot} slot.")
        return redirect(url_for("admin_home"))

