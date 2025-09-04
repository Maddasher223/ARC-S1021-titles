# web_routes.py — All Flask routes (dashboard + admin), DB-backed and template-compatible

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, date as date_cls
from typing import Any, Dict

from flask import (
    render_template, request, redirect, url_for, flash, session, jsonify
)
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)
UTC = timezone.utc


def register_routes(app, deps):
    """
    Injects dependencies from main.py and registers all Flask routes.

    Expected keys in `deps` (from main.py):
      ORDERED_TITLES, TITLES_CATALOG, REQUESTABLE, ADMIN_PIN
      parse_iso_utc, iso_slot_key_naive
      send_webhook_notification, get_shift_hours
      db, models (Title, Reservation, ActiveTitle, RequestLog)
      db_helpers (compute_slots, requestable_title_names, title_status_cards,
                  schedules_by_title, set_shift_hours, schedule_lookup)
      airtable_upsert (optional)
    """
    # ----- Unpack deps -----
    ORDERED_TITLES = deps['ORDERED_TITLES']
    TITLES_CATALOG = deps['TITLES_CATALOG']
    REQUESTABLE    = deps['REQUESTABLE']
    ADMIN_PIN      = deps['ADMIN_PIN']

    parse_iso_utc       = deps['parse_iso_utc']
    iso_slot_key_naive  = deps['iso_slot_key_naive']
    send_webhook_notification = deps['send_webhook_notification']
    get_shift_hours     = deps['get_shift_hours']  # DB-backed
    reserve_slot_core = deps.get('reserve_slot_core')

    db = deps['db']
    M  = deps['models']      # {"Title", "Reservation", "ActiveTitle", "RequestLog"}
    H  = deps['db_helpers']  # compute_slots, requestable_title_names, title_status_cards, schedules_by_title, set_shift_hours, schedule_lookup

    airtable_upsert = deps.get('airtable_upsert')  # optional

    # ---------- small utils ----------
    def now_utc() -> datetime:
        return datetime.now(UTC)

    def is_admin() -> bool:
        return bool(session.get("is_admin"))

    # ---------- health check ----------
    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "time": now_utc().isoformat()}), 200

    # ---------- public: dashboard ----------
    @app.route("/")
    def dashboard():
        # Cards
        titles_cards = H["title_status_cards"]()  # [{name, holder, expires_in, icon, buffs, held_for}, ...]
        # Keep your local icon/effects if available
        for card in titles_cards:
            meta = TITLES_CATALOG.get(card["name"], {})
            if meta:
                card["icon"]  = meta.get("image", card.get("icon", ""))
                card["buffs"] = meta.get("effects", card.get("buffs", ""))

        # Calendar window
        shift = int(get_shift_hours())
        hours = H["compute_slots"](shift)  # e.g. ["00:00", "12:00"] for 12h shifts
        today = date_cls.today()
        days  = [today + timedelta(days=i) for i in range(12)]  # your page shows 12 days

        # Schedules: {title: {slot_iso: {"ign":..., "coords":...}}}
        schedules = H["schedules_by_title"](days, hours)

        return render_template(
            "dashboard.html",
            titles=titles_cards,
            days=days,
            hours=hours,
            schedules=schedules,
            today=today.isoformat(),
            requestable_titles=H["requestable_title_names"](),
            shift_hours=shift,
        )

    # ---------- view log ----------
    @app.route("/log")
    def view_log():
        logs = M["RequestLog"].query.order_by(M["RequestLog"].id.desc()).all()
        return render_template("log.html", logs=logs)

    # ---------- booking ----------
    # NOTE: function name is 'book_slot' so url_for('book_slot') in your template works.
    @app.route("/book-slot", methods=["POST"])
    def book_slot():
        title_name = (request.form.get("title")  or "").strip()
        ign        = (request.form.get("ign")    or "").strip()
        coords     = (request.form.get("coords") or "").strip()
        date_str   = (request.form.get("date")   or "").strip()  # YYYY-MM-DD
        time_str   = (request.form.get("time")   or "").strip()  # HH:MM

        if not all([title_name, ign, date_str, time_str, coords]):
            flash("All fields (Title, IGN, Coords, Date, Time) are required.")
            return redirect(url_for("dashboard"))
        if title_name not in REQUESTABLE:
            flash("This title cannot be requested.")
            return redirect(url_for("dashboard"))

        # Parse datetime (UTC)
        try:
            slot_start = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            flash("Invalid date or time format.")
            return redirect(url_for("dashboard"))

        # Route everything through the same core used by Discord
        try:
            if not reserve_slot_core:
                raise RuntimeError("Reservation core not available.")
            reserve_slot_core(
                title_name=title_name,
                ign=ign,
                coords=coords,
                start_dt=slot_start,
                source="Web Form",
                who="Web"
            )
        except ValueError as e:
            # e.g., past time, bad coords, time not 00:00/12:00, or duplicate
            flash(str(e))
            return redirect(url_for("dashboard"))
        except Exception as e:
            logger.error("Book slot failed: %s", e, exc_info=True)
            flash("Internal error while booking. Please try again.")
            return redirect(url_for("dashboard"))

        flash(f"Reserved {title_name} for {ign} on {date_str} at {time_str} UTC.")
        return redirect(url_for("dashboard"))

    # ---------- admin auth ----------
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            pin = (request.form.get("pin") or "").strip()
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

        # Active titles table
        active_titles = []
        for row in M["ActiveTitle"].query.all():
            expires_str = "Never"
            if row.expiry_at:
                try:
                    dt = row.expiry_at
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    expires_str = dt.astimezone(UTC).isoformat()
                except Exception:
                    expires_str = "Invalid"
            active_titles.append({
                "title": row.title_name,
                "holder": row.holder or "-",
                "expires": expires_str,
            })

        # Upcoming reservations (next 14 days) -> {YYYY-MM-DD: {HH:MM: {title: entry}}}
        today = date_cls.today()
        days  = [today + timedelta(days=i) for i in range(14)]
        slots = H["compute_slots"](get_shift_hours())

        start_iso = days[0].isoformat()
        end_iso   = (days[-1] + timedelta(days=1)).isoformat()  # exclusive upper day
        res_rows = (
            M["Reservation"]
            .query
            .filter(M["Reservation"].slot_ts >= f"{start_iso}T00:00:00")
            .filter(M["Reservation"].slot_ts <  f"{end_iso}T00:00:00")
            .all()
        )

        schedule_lookup: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for r in res_rows:
            try:
                dt = parse_iso_utc(r.slot_ts) or datetime.fromisoformat(r.slot_ts).replace(tzinfo=UTC)
            except Exception:
                continue
            dkey = dt.date().isoformat()        # "YYYY-MM-DD"
            tkey = dt.strftime("%H:%M")         # "HH:MM"
            schedule_lookup.setdefault(dkey, {}).setdefault(tkey, {})[r.title_name] = {
                "ign": r.ign, "coords": r.coords
            }

        return render_template(
            "admin.html",
            active_titles=active_titles,
            all_titles=ORDERED_TITLES,
            requestable_titles=list(REQUESTABLE),
            today=today.isoformat(),
            days=days,
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

        try:
            row = M["ActiveTitle"].query.filter_by(title_name=title).first()
            if row:
                db.session.delete(row)
                db.session.commit()
            flash(f"Force-released title '{title}'.")
        except Exception as e:
            db.session.rollback()
            logger.error("Force-release failed: %s", e, exc_info=True)
            flash("Internal error while releasing. The incident was logged.")
        return redirect(url_for("admin_home"))

    @app.route("/admin/manual-assign", methods=["POST"])
    def admin_manual_assign():
        if not is_admin():
            return redirect(url_for("admin_login"))

        try:
            title    = (request.form.get("title") or "").strip()
            ign      = (request.form.get("ign") or "").strip()
            goh_only = (request.form.get("goh_only") or "").strip()

            if goh_only and title != "Guardian of Harmony":
                flash("This assignment form is only for Guardian of Harmony.")
                return redirect(url_for("admin_home"))

            if not title or not ign:
                flash("Bad manual assignment. Title and IGN required.")
                return redirect(url_for("admin_home"))
            if title not in ORDERED_TITLES:
                flash(f"Unknown title: {title}")
                return redirect(url_for("admin_home"))

            now  = now_utc()
            expiry_dt = None if title == "Guardian of Harmony" else now + timedelta(hours=int(get_shift_hours()))

            # Upsert ActiveTitle
            row = M["ActiveTitle"].query.filter_by(title_name=title).first()
            if not row:
                row = M["ActiveTitle"](title_name=title, holder=ign, claim_at=now, expiry_at=expiry_dt)
                db.session.add(row)
            else:
                row.holder  = ign
                row.claim_at = now
                row.expiry_at = expiry_dt

            db.session.commit()

            if airtable_upsert:
                try:
                    airtable_upsert("assignment", {
                        "Title": title,
                        "IGN": ign,
                        "Coordinates": "-",
                        "SlotStartUTC": now,
                        "SlotEndUTC": expiry_dt,
                        "Source": "Admin Manual Assign",
                        "DiscordUser": "Admin",
                    })
                except Exception as e:
                    logger.warning("Airtable upsert error (non-fatal): %s", e)

            flash(f"Manually assigned '{title}' to {ign}.")
            return redirect(url_for("admin_home"))

        except Exception as e:
            db.session.rollback()
            logger.error("Manual assign failed: %s", e, exc_info=True)
            flash("Internal error while assigning. The incident was logged.")
            return redirect(url_for("admin_home"))

    @app.route("/admin/manual-set-slot", methods=["POST"])
    def admin_manual_set_slot():
        if not is_admin():
            return redirect(url_for("admin_login"))

        title    = (request.form.get("title") or "").strip()
        ign      = (request.form.get("ign") or "").strip()
        date_str = (request.form.get("date") or "").strip()
        slot     = (request.form.get("slot") or "").strip()  # "HH:MM"

        if not all([title, ign, date_str, slot]):
            flash("Missing data for manual slot assignment.")
            return redirect(url_for("admin_home"))
        if title == "Guardian of Harmony":
            flash("'Guardian of Harmony' cannot be assigned to a timed slot.")
            return redirect(url_for("admin_home"))
        if title not in ORDERED_TITLES:
            flash("Unknown title.")
            return redirect(url_for("admin_home"))

        # Parse slot start/end
        try:
            start_dt = datetime.strptime(f"{date_str} {slot}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            end_dt   = start_dt + timedelta(hours=int(get_shift_hours()))
        except ValueError:
            flash("Invalid date or slot format.")
            return redirect(url_for("admin_home"))

        slot_ts = f"{date_str}T{slot}:00"

        # 1) Upsert reservation row for that slot (so it appears on the calendar)
        try:
            existing = M["Reservation"].query.filter_by(title_name=title, slot_ts=slot_ts).first()
            if not existing:
                db.session.add(M["Reservation"](title_name=title, ign=ign, coords="-", slot_ts=slot_ts))
            else:
                existing.ign = ign
                existing.coords = "-"
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("That slot is already taken.")
            return redirect(url_for("admin_home"))
        except Exception as e:
            db.session.rollback()
            logger.error("Manual set slot (reservation) failed: %s", e, exc_info=True)
            flash("Internal error while writing reservation.")
            return redirect(url_for("admin_home"))

        # 2) If slot is now/past, reflect live in ActiveTitle
        try:
            if start_dt <= now_utc():
                row = M["ActiveTitle"].query.filter_by(title_name=title).first()
                if not row:
                    row = M["ActiveTitle"](title_name=title, holder=ign, claim_at=start_dt, expiry_at=end_dt)
                    db.session.add(row)
                else:
                    row.holder   = ign
                    row.claim_at = start_dt
                    row.expiry_at = end_dt
                db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error("Manual set slot (active) failed: %s", e, exc_info=True)
            flash("Reservation saved, but live assignment failed to update.")
            return redirect(url_for("admin_home"))

        if airtable_upsert:
            try:
                airtable_upsert("assignment", {
                    "Title": title,
                    "IGN": ign,
                    "Coordinates": "-",
                    "SlotStartUTC": start_dt,
                    "SlotEndUTC": end_dt,
                    "Source": "Admin Forced Slot",
                    "DiscordUser": "Admin",
                })
            except Exception as e:
                logger.warning("Airtable upsert error (non-fatal): %s", e)

        flash(f"Manually set '{title}' for {ign} in the {date_str} {slot} slot.")
        return redirect(url_for("admin_home"))

    @app.route("/admin/set-shift-hours", methods=["POST"])
    def admin_set_shift_hours():
        """Admin can adjust the duration (hours) for timed roles (DB-backed)."""
        if not is_admin():
            return redirect(url_for("admin_login"))

        raw = (request.form.get("hours") or request.form.get("shift_hours") or "").strip()
        try:
            hours = int(raw)
        except ValueError:
            flash("Shift hours must be a whole number.")
            return redirect(url_for("admin_home"))

        if not (1 <= hours <= 72):
            flash("Shift hours must be between 1 and 72.")
            return redirect(url_for("admin_home"))

        try:
            H["set_shift_hours"](hours)
            flash(f"Shift hours updated to {hours} hours.")
        except Exception as e:
            logger.error("Set shift hours failed: %s", e, exc_info=True)
            flash("Internal error while updating shift hours.")
        return redirect(url_for("admin_home"))

    # ---------- global error safety net ----------
    @app.errorhandler(Exception)
    def handle_unexpected_error(err):
        logger.error("Unhandled exception: %s", err, exc_info=True)
        try:
            flash("Unexpected server error. It was logged and will be investigated.")
        except Exception:
            return jsonify({"ok": False, "error": "Unexpected server error"}), 500
        return redirect(url_for("dashboard"))