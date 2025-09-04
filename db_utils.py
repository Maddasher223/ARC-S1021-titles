# db_utils.py — DB helpers used by routes and the Discord task loop

from __future__ import annotations
from datetime import datetime, timezone, date, timedelta
from collections import defaultdict
from typing import List, Tuple, Dict, Any

from models import db, Setting, Title, ActiveTitle, Reservation
import os

UTC = timezone.utc


# ---------- misc ----------
def ensure_instance_dir(app):
    """Make sure instance/ exists so sqlite:///instance/app.db can be created."""
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except Exception:
        pass


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _human_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs <= 0:
        return "0m"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    return " ".join(parts)


# ---------- Settings ----------
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


# ---------- Scheduling helpers ----------
def compute_slots(shift_hours: int) -> list[str]:
    """Return the HH:MM starts for a 24h day given a slot size."""
    hours = []
    h = 0
    while h < 24:
        hours.append(f"{h:02d}:00")
        h += max(1, int(shift_hours or 12))
    if not hours:
        hours = ["00:00", "12:00"]
    return hours


def requestable_title_names() -> list[str]:
    return [
        t.name
        for t in Title.query.filter_by(requestable=True).order_by(Title.name.asc()).all()
    ]


def all_titles() -> list[Title]:
    return Title.query.order_by(Title.name.asc()).all()


def title_status_cards() -> list[Dict[str, Any]]:
    """
    Returns:
      [{ "name", "icon", "holder", "expires_in", "held_for" }, ...]
    Uses ActiveTitle.claim_at / ActiveTitle.expiry_at.
    """
    titles = all_titles()
    active_by_title = {a.title_name: a for a in ActiveTitle.query.all()}
    now = now_utc()

    out: list[Dict[str, Any]] = []
    for t in titles:
        a = active_by_title.get(t.name)
        holder = a.holder if a else None

        # held_for
        held_for = None
        if a and a.claim_at:
            claimed_dt = a.claim_at if a.claim_at.tzinfo else a.claim_at.replace(tzinfo=UTC)
            held_for = _human_duration(now - claimed_dt) if now >= claimed_dt else "0m"

        # expires_in
        expires_in = "N/A"
        if a:
            if t.name == "Guardian of Harmony" and holder:
                expires_in = "Never"
            elif a.expiry_at:
                exp_dt = a.expiry_at if a.expiry_at.tzinfo else a.expiry_at.replace(tzinfo=UTC)
                delta = exp_dt - now
                expires_in = "Expired" if delta.total_seconds() <= 0 else _human_duration(delta)
            else:
                expires_in = "Does not expire"

        out.append({
            "name": t.name,
            "icon": t.icon_url or "",
            "holder": holder or "-- Available --",
            "expires_in": expires_in,
            "held_for": held_for,
        })

    return out


def schedules_by_title(days: list[date], hours: list[str]) -> dict[str, dict[str, dict | str]]:
    """
    Return {title_name: {slot_ts: {'ign':..., 'coords':...} | ign}} for the provided grid.
    Only loads reservations visible in the window.
    """
    visible = {f"{iso_date(d)}T{h}:00" for d in days for h in hours}
    rows = Reservation.query.filter(Reservation.slot_ts.in_(visible)).all()
    out: dict[str, dict[str, dict | str]] = defaultdict(dict)
    for r in rows:
        out[r.title_name][r.slot_ts] = {"ign": r.ign, "coords": r.coords} if r.coords else r.ign
    return dict(out)


def schedule_lookup(days: list[date], slots: list[str]) -> dict[str, dict[str, dict[str, dict | str]]]:
    """
    Return {YYYY-MM-DD: {HH:MM: {title: {'ign','coords'} | ign}}}
    (Used by admin page for a compact day/time → titles mapping.)
    """
    window_days = {iso_date(d) for d in days}
    rows = Reservation.query.all()
    out: dict[str, dict[str, dict[str, dict | str]]] = defaultdict(dict)
    for r in rows:
        if "T" not in r.slot_ts:
            continue
        d_str, t_full = r.slot_ts.split("T")
        if d_str not in window_days:
            continue
        t_key = t_full[:5]
        cell = out[d_str].get(t_key, {})
        cell[r.title_name] = {"ign": r.ign, "coords": r.coords} if r.coords else r.ign
        out[d_str][t_key] = cell
    return out


# ---------- Title lifecycle (used by admin & bot) ----------
def activate_slot_db(
    title: str,
    ign: str,
    start_dt: datetime,
    set_expiry: bool,
    shift_hours: int | None = None,
) -> None:
    """
    Upsert ActiveTitle with holder / claim_at / optional expiry_at.
    Writes real datetimes, not strings.
    """
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)

    exp_dt = None
    if set_expiry:
        hours = shift_hours if shift_hours is not None else get_shift_hours()
        exp_dt = start_dt + timedelta(hours=int(hours))

    row = ActiveTitle.query.filter_by(title_name=title).one_or_none()
    if row:
        row.holder = ign
        row.claim_at = start_dt
        row.expiry_at = exp_dt
    else:
        db.session.add(ActiveTitle(
            title_name=title,
            holder=ign,
            claim_at=start_dt,
            expiry_at=exp_dt,
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
    Return reservations whose slot has started (<= now) and either:
      - the title is not active, or
      - it's active but claim_at < slot (so this slot hasn't been auto-activated)
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    out: List[Tuple[str, str, datetime]] = []
    rows = Reservation.query.all()
    active = {a.title_name: a for a in ActiveTitle.query.all()}

    for r in rows:
        try:
            dt = datetime.fromisoformat(r.slot_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        except Exception:
            continue
        if dt > now:
            continue

        a = active.get(r.title_name)
        if not a:
            out.append((r.title_name, r.ign, dt))
            continue

        claim_at = a.claim_at if a.claim_at and a.claim_at.tzinfo else (a.claim_at.replace(tzinfo=UTC) if a and a.claim_at else None)
        if claim_at and claim_at >= dt:
            # already activated at or after this slot
            continue

        out.append((r.title_name, r.ign, dt))

    return out