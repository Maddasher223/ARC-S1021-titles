# web_routes.py — Public Flask routes (dashboard + booking), DB-backed and template-compatible

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, date as date_cls

from flask import render_template, request, redirect, url_for, flash, jsonify

logger = logging.getLogger(__name__)
UTC = timezone.utc


def register_routes(app, deps):
    """
    Injects dependencies from main.py and registers public Flask routes.

    Required keys in deps:
      ORDERED_TITLES, TITLES_CATALOG, REQUESTABLE
      parse_iso_utc, iso_slot_key_naive
      send_webhook_notification, get_shift_hours
      db, models (Title, Reservation, ActiveTitle, RequestLog)
      db_helpers (compute_slots, requestable_title_names, title_status_cards,
                  schedules_by_title, set_shift_hours, schedule_lookup)
      reserve_slot_core (callable)  # shared writer used by web + discord
      airtable_upsert (optional)
    """
    ORDERED_TITLES = deps["ORDERED_TITLES"]
    TITLES_CATALOG = deps["TITLES_CATALOG"]
    REQUESTABLE = deps["REQUESTABLE"]

    # Not used directly here, but kept for parity/possible template macros
    parse_iso_utc = deps["parse_iso_utc"]
    iso_slot_key_naive = deps["iso_slot_key_naive"]
    send_webhook_notification = deps["send_webhook_notification"]

    get_shift_hours = deps["get_shift_hours"]  # DB-backed
    reserve_slot_core = deps["reserve_slot_core"]

    db = deps["db"]
    M = deps["models"]
    H = deps["db_helpers"]

    airtable_upsert = deps.get("airtable_upsert")

    # ---------- small utils ----------
    def now_utc() -> datetime:
        return datetime.now(UTC)

    # ---------- health check ----------
    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "time": now_utc().isoformat()}), 200

    @app.route("/__debug/schedules")
    def __debug_schedules():
        shift = int(get_shift_hours())
        hours = H["compute_slots"](shift)
        today = date_cls.today()
        days = [today + timedelta(days=i) for i in range(12)]
        schedules = H["schedules_by_title"](days, hours)

        # Also show the visible keys we expect the template to look up
        visible_keys = [f"{d.isoformat()}T{h}:00" for d in days for h in hours]

        return jsonify(
            {
                "db_uri": app.config.get("SQLALCHEMY_DATABASE_URI"),
                "shift_hours": shift,
                "hours": hours,
                "visible_keys_sample": visible_keys[:8],  # first few
                "schedules": schedules,  # what the page uses
            }
        )

    # ---------- public: dashboard ----------
    @app.route("/")
    def dashboard():
        # Cards
        titles_cards = H["title_status_cards"]()
        for card in titles_cards:
            meta = TITLES_CATALOG.get(card["name"], {})
            if meta:
                card["icon"] = meta.get("image", card.get("icon", ""))
                card["buffs"] = meta.get("effects", card.get("buffs", ""))

        # Same window + sanctioned start times
        shift = int(get_shift_hours())
        hours = H["compute_slots"](shift)  # e.g. ["00:00","12:00"]
        today = date_cls.today()
        days = [today + timedelta(days=i) for i in range(14)]

        # Compact day → time → {title: {ign, coords}} mapping
        sched_map = H["schedule_lookup"](days, hours)

        return render_template(
            "dashboard.html",
            titles=titles_cards,
            days=days,
            hours=hours,
            schedule_lookup=sched_map,
            today=today.isoformat(),
            shift_hours=shift,
            requestable_titles=list(REQUESTABLE),
        )

    # ---------- public: view request log ----------
    @app.route("/log")
    def view_log():
        logs = M["RequestLog"].query.order_by(M["RequestLog"].id.desc()).all()
        return render_template("log.html", logs=logs)

    # ---------- booking (web) ----------
    @app.route("/book-slot", methods=["POST"])
    def book_slot():
        title_name = (request.form.get("title") or "").strip()
        ign = (request.form.get("ign") or "").strip()
        coords = (request.form.get("coords") or "").strip()
        date_str = (request.form.get("date") or "").strip()  # YYYY-MM-DD
        time_str = (request.form.get("time") or "").strip()  # HH:MM

        if not all([title_name, ign, date_str, time_str, coords]):
            flash("All fields (Title, IGN, Coords, Date, Time) are required.")
            return redirect(url_for("dashboard"))
        if title_name not in REQUESTABLE:
            flash("This title cannot be requested.")
            return redirect(url_for("dashboard"))

        # Parse datetime (UTC)
        try:
            slot_start = (
                datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                .replace(tzinfo=UTC)
            )
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

    # ---------- global error safety net (public) ----------
    @app.errorhandler(Exception)
    def handle_unexpected_error(err):
        # Avoid swallowing Werkzeug HTTPExceptions raised intentionally by Flask; only log generic exceptions.
        try:
            from werkzeug.exceptions import HTTPException
            if isinstance(err, HTTPException):
                return err
        except Exception:
            pass

        logger.error("Unhandled exception (public): %s", err, exc_info=True)
        try:
            flash("Unexpected server error. It was logged and will be investigated.")
        except Exception:
            return jsonify({"ok": False, "error": "Unexpected server error"}), 500
        return redirect(url_for("dashboard"))