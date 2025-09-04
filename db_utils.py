# db_utils.py — DB helpers used by routes and the Discord task loop

from __future__ import annotations
from datetime import datetime, timezone, date as date_cls, timedelta
from collections import defaultdict
from typing import List, Tuple, Dict, Any
import os

from models import db, Setting, Title, ActiveTitle, Reservation

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


def iso_date(d: date_cls) -> str:
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
    """Read shift hours; coerce to a safe int; default to 12 on any bad value."""
    row = db.session.get(Setting, "shift_hours")
    if not row or not str(row.value).strip():
        return default
    try:
        hours = int(row.value)
    except Exception:
        return default
    if hours < 1 or hours > 72:
        return default
    return hours


def set_shift_hours(hours: int) -> None:
    """Persist shift hours (validated here)."""
    hours = int(hours)
    if not (1 <= hours <= 72):
        raise ValueError("shift_hours must be between 1 and 72")
    row = db.session.get(Setting, "shift_hours")
    if row:
        row.value = str(hours)
    else:
        db.session.add(Setting(key="shift_hours", value=str(hours)))
    db.session.commit()


# ---------- Scheduling helpers ----------
def compute_slots(shift_hours: int) -> list[str]:
    """
    Return HH:MM starts for a 24h day. If the given shift doesn't divide 24 evenly,
    fall back to 12-hour slots to avoid drift (e.g., 00:00, 12:00).
    """
    try:
        sh = int(shift_hours)
    except Exception:
        sh = 12
    if sh <= 0:
        sh = 12
    if 24 % sh != 0:
        sh = 12
    return [f"{h:02d}:00" for h in range(0, 24, sh)]


def requestable_title_names() -> list[str]:
    return [t.name for t in Title.query.filter_by(requestable=True).order_by(Title.id.asc()).all()]


def all_titles() -> list[Title]:
    return Title.query.order_by(Title.id.asc()).all()


def title_status_cards() -> list[Dict[str, Any]]:
    """
    Returns:
      [{ "name", "icon", "holder", "expires_in", "held_for", "buffs" }, ...]
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
        expires_in = "—"
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
            "buffs": "",  # dashboard template may override from TITLES_CATALOG
        })

    return out


def schedules_by_title(days: list[date_cls], hours: list[str]) -> dict[str, dict[str, dict]]:
    """
    Return {title_name: {YYYY-MM-DDTHH:MM:SS: {'ign':..., 'coords':...}}}
    Only loads reservations within [days[0], days[-1]] and includes only times in `hours`.
    """
    if not days:
        return {}

    start_iso = f"{days[0].isoformat()}T00:00:00"
    end_iso = f"{(days[-1] + timedelta(days=1)).isoformat()}T00:00:00"  # exclusive
    hours_set = set(hours or [])  # e.g. {"00:00","12:00"}

    # Range filter is faster than IN for big windows; TEXT ISO compares lexicographically fine.
    rows = (
        Reservation.query
        .filter(Reservation.slot_ts >= start_iso)
        .filter(Reservation.slot_ts < end_iso)
        .all()
    )

    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        # slot_ts is stored as "YYYY-MM-DDTHH:MM:SS"
        try:
            dt = datetime.fromisoformat(r.slot_ts)
        except Exception:
            continue
        hhmm = dt.strftime("%H:%M")
        if hhmm not in hours_set:
            # Filter out old/invalid rows (e.g., from a 5-hour era).
            continue
        slot_iso = f"{dt.date().isoformat()}T{hhmm}:00"
        out[r.title_name][slot_iso] = {"ign": r.ign, "coords": (r.coords or "-")}
    return dict(out)


def schedule_lookup(days: list[date_cls], hours: list[str]) -> dict[str, dict[str, dict[str, dict]]]:
    """
    Return {YYYY-MM-DD: {HH:MM: {title: {'ign','coords'}}}}
    Used by admin page for a compact day/time → titles mapping.
    """
    by_title = schedules_by_title(days, hours)
    out: dict[str, dict[str, dict[str, dict]]] = defaultdict(dict)
    for title, slots in by_title.items():
        for slot_iso, entry in slots.items():
            d_str, t_full = slot_iso.split("T")
            t_key = t_full[:5]
            out.setdefault(d_str, {}).setdefault(t_key, {})[title] = entry
    return dict(out)


# ---------- Title lifecycle (used by admin & bot) ----------
def activate_slot_db(
    title: str,
    ign: str,
    start_dt: datetime,
    set_expiry: bool,
    shift_hours: int | None = None,
) -> None:
    """Upsert ActiveTitle with holder / claim_at / optional expiry_at (UTC-aware)."""
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
            continue

        out.append((r.title_name, r.ign, dt))

    return out