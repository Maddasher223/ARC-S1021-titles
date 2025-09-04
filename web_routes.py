# web_routes.py — All Flask routes (dashboard + admin), hardened for responsiveness

from __future__ import annotations

import os
import csv
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread
from typing import Any, Dict

from flask import (
    render_template, request, redirect, url_for, flash, session, jsonify
)

logger = logging.getLogger(__name__)
UTC = timezone.utc


def register_routes(app, deps):
    """
    Injects dependencies from main.py and registers all Flask routes.
    Expected keys in `deps`:
      ORDERED_TITLES, TITLES_CATALOG, REQUESTABLE, ADMIN_PIN,
      state, save_state, log_to_csv, parse_iso_utc, now_utc,
      iso_slot_key_naive, get_shift_hours, state_lock, send_webhook_notification
      (bot is not required here)
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

    # ---------- utilities ----------
    def run_bg(func, *args, **kwargs) -> None:
        """Fire-and-forget background worker with error protection."""
        def _runner():
            try:
                func(*args, **kwargs)
            except Exception as e:
                logger.error("Background task error in %s: %s", getattr(func, '__name__', 'func'), e, exc_info=True)
        t = Thread(target=_runner, daemon=True)
        t.start()

    def is_admin() -> bool:
        return bool(session.get("is_admin"))

    def _copy_schedules() -> Dict[str, Dict[str, Any]]:
        """Shallow, read-only copy of schedules to avoid holding the lock while rendering."""
        with state_lock:
            schedules = state.get('schedules', {})
            # shallow copy is enough (we never mutate this copy)
            return {title: dict(slots) for title, slots in schedules.items()}

    def _copy_titles() -> Dict[str, Dict[str, Any]]:
        with state_lock:
            titles = state.get('titles', {})
            return {k: dict(v) for k, v in titles.items()}

    # ---------- health check ----------
    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "time": now_utc().isoformat()}), 200

    # ---------- public: dashboard ----------
    @app.route("/")
    def dashboard():
        titles_data = []
        titles_snapshot = _copy_titles()

        for title_name in ORDERED_TITLES:
            data = titles_snapshot.get(title_name, {})
            holder_info = "None"
            if data.get('holder'):
                holder = data['holder'] or {}
                nm = holder.get('name', '?')
                cr = holder.get('coords', '-')
                holder_info = f"{nm} ({cr})"

            remaining = "Never" if (title_name == "Guardian of Harmony" and data.get('holder')) else "N/A"
            exp_str = data.get('expiry_date')
            if exp_str:
                expiry = parse_iso_utc(exp_str)
                if expiry:
                    delta = expiry - now_utc()
                    remaining = (
                        "Expired" if delta.total_seconds() <= 0
                        else str(timedelta(seconds=int(delta.total_seconds())))
                    )
                else:
                    remaining = "Invalid Date"

            titles_data.append({
                'name': title_name,
                'holder': holder_info,
                'expires_in': remaining,
                'icon': TITLES_CATALOG.get(title_name, {}).get('image', ''),
                'buffs': TITLES_CATALOG.get(title_name, {}).get('effects', ''),
            })

        # two-week grid: 00:00 / 12:00
        today = now_utc().date()
        days = [(today + timedelta(days=i)) for i in range(14)]
        hours = ["00:00", "12:00"]

        return render_template(
            'dashboard.html',
            titles=titles_data,
            days=days,
            hours=hours,
            schedules=_copy_schedules(),
            today=today.strftime('%Y-%m-%d'),
            requestable_titles=REQUESTABLE,
            shift_hours=get_shift_hours(),  # keep page text accurate
        )

    # ---------- view log ----------
    @app.route("/log")
    def view_log():
        log_data = []
        csv_path = os.path.join(os.path.dirname(__file__), "data", "requests.csv")
        # Read-only, best-effort
        if os.path.exists(csv_path):
            try:
                with open(csv_path, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    log_data = list(reader)
            except Exception as e:
                logger.error("Log read error: %s", e)
                flash("Could not read log file.")
        return render_template('log.html', logs=reversed(log_data))

    # ---------- booking ----------
    @app.route("/book-slot", methods=['POST'])
    def book_slot():
        # sanitize/validate
        title_name = (request.form.get('title') or '').strip()
        ign = (request.form.get('ign') or '').strip()
        coords = (request.form.get('coords') or '').strip()
        date_str = (request.form.get('date') or '').strip()
        time_str = (request.form.get('time') or '').strip()

        if not all([title_name, ign, coords, date_str, time_str]):
            flash("All fields (Title, IGN, Coords, Date, Time) are required.")
            return redirect(url_for("dashboard"))
        if title_name not in REQUESTABLE:
            flash("This title cannot be requested.")
            return redirect(url_for("dashboard"))

        # Parse date/time -> UTC-aware datetime
        try:
            schedule_time = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            flash("Invalid date or time format.")
            return redirect(url_for("dashboard"))

        if schedule_time < now_utc():
            flash("Cannot schedule a time in the past.")
            return redirect(url_for("dashboard"))

        schedule_key = iso_slot_key_naive(schedule_time)

        # state mutation under short lock
        reserved_ok = False
        with state_lock:
            schedules_for_title = state.setdefault('schedules', {}).setdefault(title_name, {})
            if schedule_key in schedules_for_title:
                reserver = schedules_for_title[schedule_key]
                ign_val = reserver.get('ign') if isinstance(reserver, dict) else reserver
                flash(f"That slot for {title_name} is already reserved by {ign_val}.")
            else:
                schedules_for_title[schedule_key] = {"ign": ign, "coords": coords}
                reserved_ok = True

        if not reserved_ok:
            return redirect(url_for("dashboard"))

        # kick off background side-effects (disk + webhook)
        csv_payload = {
            "timestamp": now_utc().isoformat(),
            "title_name": title_name,
            "in_game_name": ign,
            "coordinates": coords,
            "discord_user": "Web Form",
        }
        run_bg(log_to_csv, csv_payload)
        run_bg(save_state)  # persist state without blocking
        run_bg(send_webhook_notification, {
            "title_name": title_name,
            "in_game_name": ign,
            "coordinates": coords,
            "discord_user": "Web Form",
            "timestamp": now_utc().isoformat(),
        }, False)

        flash(f"Reserved {title_name} for {ign} on {date_str} at {time_str} UTC.")
        return redirect(url_for("dashboard"))

    # ---------- admin auth ----------
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            pin = (request.form.get("pin") or "").strip()  # field name stays 'pin'
            if pin == ADMIN_PIN:
                session["is_admin"] = True
                flash("Welcome, admin.")
                return redirect(url_for("admin_home"))
            flash("Incorrect password.")
        return render_template("admin_login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        flash("Logged out.")
        return redirect(url_for("dashboard"))

    # ---------- admin home ----------
    @app.route("/admin")
    def admin_home():
        if not is_admin():
            return redirect(url_for("admin_login"))

        active_titles = []
        titles_snapshot = _copy_titles()

        for title_name in ORDERED_TITLES:
            t = titles_snapshot.get(title_name, {})
            if t and t.get("holder"):
                expires_str = "Never"
                if t.get("expiry_date"):
                    exp_dt = parse_iso_utc(t["expiry_date"])
                    expires_str = exp_dt.astimezone(UTC).isoformat() if exp_dt else "Invalid"
                active_titles.append({
                    "title": title_name,
                    "holder": (t.get("holder") or {}).get("name", "-"),
                    "expires": expires_str
                })

        # Build a {YYYY-MM-DD: {HH:MM: {title: entry}}} lookup for upcoming 14 days
        days = [(now_utc().date() + timedelta(days=i)) for i in range(14)]
        slots = ["00:00", "12:00"]
        schedule_lookup: Dict[str, Dict[str, Dict[str, Any]]] = {}
        schedules_snapshot = _copy_schedules()

        for title_name, slots_map in schedules_snapshot.items():
            for key, entry in slots_map.items():
                # key is naive ISO string like 'YYYY-MM-DDTHH:MM:SS'
                try:
                    dt = parse_iso_utc(key) or datetime.fromisoformat(key).replace(tzinfo=UTC)
                except Exception:
                    continue
                dkey = dt.date().isoformat()
                tkey = dt.strftime("%H:%M")
                schedule_lookup.setdefault(dkey, {}).setdefault(tkey, {})[title_name] = entry

        return render_template(
            "admin.html",
            active_titles=active_titles,
            all_titles=ORDERED_TITLES,
            requestable_titles=list(REQUESTABLE),
            today=now_utc().date().isoformat(),
            days=[datetime.combine(d, datetime.min.time(), tzinfo=UTC).date() for d in days],
            slots=slots,
            schedule_lookup=schedule_lookup,
            shift_hours=get_shift_hours(),
        )

    # ---------- admin actions ----------
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
            state["titles"][title].update({
                'holder': None, 'claim_date': None, 'expiry_date': None
            })
        run_bg(save_state)

        flash(f"Force-released title '{title}'.")
        return redirect(url_for("admin_home"))

    @app.route("/admin/manual-assign", methods=["POST"])
    def admin_manual_assign():
        if not is_admin():
            return redirect(url_for("admin_login"))

        try:
            title = (request.form.get("title") or "").strip()
            ign   = (request.form.get("ign") or "").strip()

            # Defensive validation
            if not title or not ign:
                flash("Bad manual assignment. Title and IGN required.")
                return redirect(url_for("admin_home"))
            if title not in ORDERED_TITLES:
                flash(f"Unknown title: {title}")
                return redirect(url_for("admin_home"))

            # compute expiry (Guardian of Harmony never expires)
            now = now_utc()
            expiry_date_iso = None
            if title != "Guardian of Harmony":
                expiry_date_iso = (now + timedelta(hours=get_shift_hours())).isoformat()

            # Safe mutation under short lock
            with state_lock:
                titles = state.setdefault("titles", {})
                t = titles.setdefault(title, {})
                t.update({
                    "holder": {"name": ign, "coords": "-", "discord_id": 0},
                    "claim_date": now.isoformat(),
                    "expiry_date": expiry_date_iso,
                })

            # Save asynchronously so we don’t block request
            run_bg(save_state)

            flash(f"Manually assigned '{title}' to {ign}.")
            return redirect(url_for("admin_home"))
        
        except Exception as e:
            # Log the full stack; show friendly message to user
            logger.error("Manual assign failed: %s", e, exc_info=True)
            flash("Internal error while assigning. The incident was logged.")
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
        if title not in ORDERED_TITLES:
            flash("Unknown title.")
            return redirect(url_for("admin_home"))
        if title == "Guardian of Harmony":
            flash("'Guardian of Harmony' cannot be assigned to a timed slot.")
            return redirect(url_for("admin_home"))

        try:
            start_dt = datetime.strptime(f"{date_str} {slot}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
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

        run_bg(save_state)
        flash(f"Manually set '{title}' for {ign} in the {date_str} {slot} slot.")
        return redirect(url_for("admin_home"))

    @app.route("/admin/set-shift-hours", methods=["POST"])
    def admin_set_shift_hours():
        """Admin can adjust the duration (hours) for timed roles."""
        if not is_admin():
            return redirect(url_for("admin_login"))

        raw = (request.form.get("shift_hours") or "").strip()
        try:
            hours = int(raw)
        except ValueError:
            flash("Shift hours must be a whole number.")
            return redirect(url_for("admin_home"))

        # sensible bounds; adjust if your game rules differ
        if not (1 <= hours <= 72):
            flash("Shift hours must be between 1 and 72.")
            return redirect(url_for("admin_home"))

        with state_lock:
            cfg = state.setdefault('config', {})
            cfg['shift_hours'] = hours

        run_bg(save_state)
        flash(f"Shift hours updated to {hours} hours.")
        return redirect(url_for("admin_home"))
    
    @app.errorhandler(Exception)
    def handle_unexpected_error(err):
        # Avoid leaking details to the browser, but keep full trace in logs
        logger.error("Unhandled exception: %s", err, exc_info=True)
        flash("Unexpected server error. It was logged and will be investigated.")
        # Send users to a safe page
        return redirect(url_for("dashboard"))