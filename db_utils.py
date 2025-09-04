# db_utils.py — DB helpers used by routes and the Discord task loop

from __future__ import annotations
from datetime import datetime, timezone, date, timedelta
from collections import defaultdict
from typing import List, Tuple

from models import db, Setting, Title, ActiveTitle, Reservation
import os

def ensure_instance_dir(app):
    """Make sure instance/ exists so sqlite:///instance/app.db can be created."""
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except Exception:
        pass

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def iso_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")

# -------- Settings --------
def get_shift_hours(default: int = 12) -> int:
    row = db.session.get(Setting, "shift_hours")
    if not row:
        return default
    try:
        return int(row.value)
    except Exception:
        return default

def set_shift_hours(hours: int) -> None:
    row = db.session.get(Setting, "shift_hours")
    if row:
        row.value = str(hours)
    else:
        db.session.add(Setting(key="shift_hours", value=str(hours)))
    db.session.commit()

# -------- Scheduling helpers --------
def compute_slots(shift_hours: int) -> list[str]:
    hours = []
    h = 0
    while h < 24:
        hours.append(f"{h:02d}:00")
        h += shift_hours
    # Ensure we at least show something sane
    if not hours:
        hours = ["00:00", "12:00"]
    return hours

def requestable_title_names() -> list[str]:
    return [t.name for t in Title.query.filter_by(requestable=True).order_by(Title.name).all()]

def all_titles() -> list[Title]:
    return Title.query.order_by(Title.name).all()

def title_status_cards():
    """Return list of {name, icon, holder, expires_in, held_for?} for the dashboard."""
    titles = all_titles()
    active = {a.title_name: a for a in ActiveTitle.query.all()}
    out = []
    for t in titles:
        a = active.get(t.name)
        item = {
            "name": t.name,
            "icon": t.icon_url,  # your templates use {{ title.icon }}
            "holder": "-- Available --",
            "expires_in": "N/A",
        }
        if a:
            item["holder"] = a.holder
            # expires
            if a.expires_at:
                try:
                    exp = datetime.fromisoformat(a.expires_at)
                    delta = exp - now_utc()
                    secs = int(delta.total_seconds())
                    if secs <= 0:
                        item["expires_in"] = "Expired"
                    else:
                        # hours floor
                        hrs = secs // 3600
                        item["expires_in"] = f"In {hrs} hours"
                except Exception:
                    item["expires_in"] = a.expires_at
            else:
                item["expires_in"] = "Does not expire"
            # held_for
            if a.assigned_at:
                try:
                    asg = datetime.fromisoformat(a.assigned_at)
                    delta = now_utc() - asg
                    hrs = max(0, int(delta.total_seconds()) // 3600)
                    item["held_for"] = f"For {hrs} hours"
                except Exception:
                    pass
        out.append(item)
    return out

def schedules_by_title(days: list[date], hours: list[str]) -> dict[str, dict[str, dict | str]]:
    """Return {title: {slot_ts: entry}} for visible window."""
    # Collect ISO keys for displayed grid
    visible = set()
    for d in days:
        for h in hours:
            visible.add(f"{iso_date(d)}T{h}:00")
    rows = Reservation.query.filter(Reservation.slot_ts.in_(visible)).all()
    out = defaultdict(dict)
    for r in rows:
        out[r.title_name][r.slot_ts] = {"ign": r.ign, "coords": r.coords} if r.coords else r.ign
    return dict(out)

def schedule_lookup(days: list[date], slots: list[str]):
    """Return {YYYY-MM-DD: {HH:MM: {title: entry}}}"""
    # Build keys for window (not strictly required, but trims in big DBs)
    window_days = [iso_date(d) for d in days]
    rows = Reservation.query.all()
    out = defaultdict(dict)
    for r in rows:
        if "T" not in r.slot_ts:
            continue
        d, t = r.slot_ts.split("T")
        if d not in window_days:
            continue
        t = t[:5]
        cell = out[d].get(t, {})
        cell[r.title_name] = {"ign": r.ign, "coords": r.coords} if r.coords else r.ign
        out[d][t] = cell
    return out

# -------- Title lifecycle (used by admin & bot) --------
def activate_slot_db(title: str, ign: str, start_dt: datetime, set_expiry: bool, shift_hours: int | None = None) -> None:
    """Upsert ActiveTitle with holder/assigned_at/optional expires_at."""
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    row = ActiveTitle.query.filter_by(title_name=title).one_or_none()
    exp = None
    if set_expiry:
        hours = shift_hours if shift_hours is not None else get_shift_hours()
        exp = start_dt + timedelta(hours=hours)

    if row:
        row.holder = ign
        row.assigned_at = iso(start_dt)
        row.expires_at = iso(exp) if exp else None
    else:
        db.session.add(ActiveTitle(
            title_name=title, holder=ign,
            assigned_at=iso(start_dt),
            expires_at=iso(exp) if exp else None
        ))
    db.session.commit()

def release_title_db(title: str) -> bool:
    row = ActiveTitle.query.filter_by(title_name=title).one_or_none()
    if not row:
        return False
    db.session.delete(row)
    db.session.commit()
    return True

def upcoming_unactivated_reservations(now: datetime) -> List[Tuple[str, str, datetime]]:
    """
    Return reservations whose slot has started (<= now) and the title is either:
      - not active, or
      - active but (a) no expiry (GoH) and assigned_at < slot, or (b) assigned_at < slot
    This is a pragmatic dedup to avoid re-activating the same slot.
    """
    out: List[Tuple[str, str, datetime]] = []
    rows = Reservation.query.all()
    active = {a.title_name: a for a in ActiveTitle.query.all()}
    for r in rows:
        try:
            dt = datetime.fromisoformat(r.slot_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt > now:
            continue

        a = active.get(r.title_name)
        if not a:
            out.append((r.title_name, r.ign, dt))
            continue

        # If already active, skip if assigned_at >= slot_ts (already activated/overridden)
        try:
            assigned_at = datetime.fromisoformat(a.assigned_at) if a.assigned_at else None
        except Exception:
            assigned_at = None
        if assigned_at and assigned_at >= dt:
            continue

        out.append((r.title_name, r.ign, dt))
    return out