# web_routes.py — All Flask routes (dashboard + admin), DB-backed and template-compatible

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, date as date_cls
from typing import Any, Dict

from flask import render_template, request, redirect, url_for, flash, session, jsonify
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)
UTC = timezone.utc


def register_routes(app, deps):
    """
    Injects dependencies from main.py and registers all Flask routes.

    Required keys in deps:
      ORDERED_TITLES, TITLES_CATALOG, REQUESTABLE, ADMIN_PIN
      parse_iso_utc, iso_slot_key_naive
      send_webhook_notification, get_shift_hours
      db, models (Title, Reservation, ActiveTitle, RequestLog)
      db_helpers (compute_slots, requestable_title_names, title_status_cards,
                  schedules_by_title, set_shift_hours, schedule_lookup)
      reserve_slot_core (callable)  # shared writer used by web + discord
      airtable_upsert (optional)
    """
    ORDERED_TITLES = deps['ORDERED_TITLES']
    TITLES_CATALOG = deps['TITLES_CATALOG']
    REQUESTABLE    = deps['REQUESTABLE']
    ADMIN_PIN      = deps['ADMIN_PIN']

    parse_iso_utc       = deps['parse_iso_utc']
    iso_slot_key_naive  = deps['iso_slot_key_naive']
    send_webhook_notification = deps['send_webhook_notification']
    get_shift_hours     = deps['get_shift_hours']      # DB-backed
    reserve_slot_core   = deps['reserve_slot_core']

    db = deps['db']
    M  = deps['models']
    H  = deps['db_helpers']

    airtable_upsert = deps.get('airtable_upsert')

    # ---------- small utils ----------
    def now_utc() -> datetime:
        return datetime.now(UTC)

    def is_admin() -> bool:
        return bool(session.get("is_admin"))

    # ---------- health check ----------
    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "time": now_utc().isoformat()}), 200
    
    @app.route("/__debug/schedules")
    def __debug_schedules():
        shift = int(get_shift_hours())
        hours = H["compute_slots"](shift)
        today = date_cls.today()
        days  = [today + timedelta(days=i) for i in range(12)]
        schedules = H["schedules_by_title"](days, hours)

        # Also show the visible keys we expect the template to look up
        visible_keys = [f"{d.isoformat()}T{h}:00" for d in days for h in hours]

        return jsonify({
            "db_uri": app.config.get("SQLALCHEMY_DATABASE_URI"),
            "shift_hours": shift,
            "hours": hours,
            "visible_keys_sample": visible_keys[:8],  # first few
            "schedules": schedules,                  # what the page uses
        })

    # ---------- public: dashboard ----------
    @app.route("/")
    def dashboard():
        # Cards
        titles_cards = H["title_status_cards"]()
        for card in titles_cards:
            meta = TITLES_CATALOG.get(card["name"], {})
            if meta:
                card["icon"]  = meta.get("image", card.get("icon", ""))
                card["buffs"] = meta.get("effects", card.get("buffs", ""))

        # Grid window (12 days) and sanctioned start times
        shift = int(get_shift_hours())
        hours = H["compute_slots"](shift)  # e.g., ["00:00","12:00"] when shift=12
        today = date_cls.today()
        days  = [today + timedelta(days=i) for i in range(12)]

        # Build schedules (title -> {slot_iso -> entry}) via slot_dt range queries
        schedules = H["schedules_by_title"](days, hours)

        # DEBUG: log counts to catch mismatches quickly
        try:
            total_cells = len(days) * len(hours)
            total_marks = sum(len(v) for v in schedules.values())
            logger.info("dashboard grid: %d days x %d hours = %d cells; %d reservations mapped",
                        len(days), len(hours), total_cells, total_marks)
            if total_marks == 0:
                logger.info("No reservations matched grid hours %s. If old reservations exist at off-hours (e.g. 05:00), they won't display.", hours)
        except Exception:
            pass

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

    # ---------- booking (web) ----------
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

        # Go through the shared writer (validates time, coords, duplicates, etc.)
        try:
            reserve_slot_core(
                title_name=title_name,
                ign=ign,
                coords=coords,
                start_dt=slot_start,
                source="Web Form",
                who="Web",
            )
        except ValueError as e:
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

        # Active titles summary
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

        # Upcoming reservations window (14 days) — uses range query on slot_dt via helpers
        today = date_cls.today()
        days  = [today + timedelta(days=i) for i in range(14)]
        slots = H["compute_slots"](get_shift_hours())
        sched_map = H["schedule_lookup"](days, slots)

        return render_template(
            "admin.html",
            active_titles=active_titles,
            all_titles=ORDERED_TITLES,
            requestable_titles=list(REQUESTABLE),
            today=today.isoformat(),
            days=days,
            slots=slots,
            schedule_lookup=sched_map,
            shift_hours=get_shift_hours(),
        )

    # ---------- admin actions (release / assign / set-slot / set-shift) ----------
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

            row = M["ActiveTitle"].query.filter_by(title_name=title).first()
            if not row:
                row = M["ActiveTitle"](title_name=title, holder=ign, claim_at=now, expiry_at=expiry_dt)
                db.session.add(row)
            else:
                row.holder   = ign
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

        try:
            start_dt = datetime.strptime(f"{date_str} {slot}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            end_dt   = start_dt + timedelta(hours=int(get_shift_hours()))
        except ValueError:
            flash("Invalid date or slot format.")
            return redirect(url_for("admin_home"))

        # Mirror string key (optional; keeps legacy tools happy)
        slot_ts = f"{date_str}T{slot}:00"

        try:
            # Upsert by (title_name, slot_dt)
            existing = (
                M["Reservation"].query
                .filter(M["Reservation"].title_name == title)
                .filter(M["Reservation"].slot_dt == start_dt)
                .first()
            )
            if not existing:
                db.session.add(M["Reservation"](
                    title_name=title,
                    ign=ign,
                    coords="-",
                    slot_dt=start_dt,
                    slot_ts=slot_ts,  # optional mirror
                ))
            else:
                existing.ign = ign
                existing.coords = "-"
                # keep slot_dt as-is; ensure mirror is set
                if hasattr(existing, "slot_ts"):
                    existing.slot_ts = slot_ts

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

        # If the slot is already started (or now), keep ActiveTitle in sync
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
    
    @app.route("/admin/release-reservation", methods=["POST"])
    def admin_release_reservation():
        if not is_admin():
            return redirect(url_for("admin_login"))

        title    = (request.form.get("title") or "").strip()
        date_str = (request.form.get("date") or "").strip()   # YYYY-MM-DD
        time_str = (request.form.get("time") or "").strip()   # HH:MM (e.g., 00:00 or 12:00)
        also_release_live = bool(request.form.get("also_release_live"))

        if not all([title, date_str, time_str]):
            flash("Missing title/date/time to release reservation.")
            return redirect(url_for("admin_home"))

        # Compute the UTC slot_dt we want to remove
        try:
            start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            flash("Invalid date/time to release.")
            return redirect(url_for("admin_home"))

        # For legacy rows, also compute slot_ts mirror
        slot_ts = f"{date_str}T{time_str}:00"

        # 1) Delete the reservation row (prefer slot_dt, fallback slot_ts)
        try:
            q = (
                M["Reservation"].query
                .filter(M["Reservation"].title_name == title)
                .filter(M["Reservation"].slot_dt == start_dt)
            )
            res = q.first()
            if not res:
                # fallback: legacy text key
                res = (
                    M["Reservation"].query
                    .filter(M["Reservation"].title_name == title)
                    .filter(M["Reservation"].slot_ts == slot_ts)
                ).first()

            if not res:
                flash("Reservation not found.")
                return redirect(url_for("admin_home"))

            res_ign = res.ign  # for optional live release check

            db.session.delete(res)
            db.session.commit()
            flash(f"Reservation for '{title}' at {date_str} {time_str} was released.")

        except Exception as e:
            db.session.rollback()
            logger.error("Admin release reservation failed: %s", e, exc_info=True)
            flash("Internal error while releasing reservation.")
            return redirect(url_for("admin_home"))

        # 2) (Optional) also release live assignment if it’s the current active slot
        if also_release_live:
            try:
                row = M["ActiveTitle"].query.filter_by(title_name=title).first()
                if row:
                    same_holder = (row.holder or "") == (res_ign or "")
                    # Compare claim_at to the removed slot's start
                    claim_at = row.claim_at if (row.claim_at and row.claim_at.tzinfo) else (row.claim_at.replace(tzinfo=UTC) if row and row.claim_at else None)
                    same_start = bool(claim_at and claim_at.replace(microsecond=0) == start_dt.replace(microsecond=0))
                    if same_holder and same_start:
                        db.session.delete(row)
                        db.session.commit()
                        flash(f"Live title '{title}' was also released.")
            except Exception as e:
                db.session.rollback()
                logger.error("Live release after reservation delete failed: %s", e, exc_info=True)
                flash("Reservation removed, but live title release failed.")
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