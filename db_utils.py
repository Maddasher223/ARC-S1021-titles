from datetime import datetime, timezone, date
from collections import defaultdict
from models import db, Setting, Title, ActiveTitle, Reservation
import math

def now_utc(): return datetime.now(timezone.utc)
def iso(dt: datetime) -> str:
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def iso_date(d: date) -> str: return d.strftime("%Y-%m-%d")

def get_shift_hours(default=12):
    row = Setting.query.get("shift_hours")
    try: return int(row.value) if row else default
    except: return default

def set_shift_hours(hours: int):
    row = Setting.query.get("shift_hours")
    if not row: db.session.add(Setting(key="shift_hours", value=str(hours)))
    else: row.value = str(hours)
    db.session.commit()

def compute_slots(shift_hours: int):
    return [f"{h:02d}:00" for h in range(0, 24, shift_hours)]

def requestable_title_names():
    return [t.name for t in Title.query.filter_by(requestable=True).order_by(Title.name).all()]

def all_titles(): return Title.query.order_by(Title.name).all()

def title_status_cards():
    titles = all_titles()
    active = {a.title_name: a for a in ActiveTitle.query.all()}
    out = []
    for t in titles:
        a = active.get(t.name)
        item = {"name": t.name, "icon": t.icon_url, "holder": "-- Available --", "expires_in": "N/A"}
        if a:
            item["holder"] = a.holder
            if a.expires_at:
                try:
                    exp = datetime.fromisoformat(a.expires_at)
                    hours = math.floor((exp - now_utc()).total_seconds() / 3600)
                    item["expires_in"] = f"In {max(hours, 0)} hours"
                except: item["expires_in"] = a.expires_at
            else:
                item["expires_in"] = "Does not expire"
            if a.assigned_at:
                try:
                    asg = datetime.fromisoformat(a.assigned_at)
                    hours = math.floor((now_utc() - asg).total_seconds() / 3600)
                    item["held_for"] = f"For {max(hours, 0)} hours"
                except: pass
        out.append(item)
    return out

def schedules_by_title(days, hours):
    # fetch all reservations (simple: just pull all and filter by displayed keys)
    rows = Reservation.query.all()
    out = defaultdict(dict)
    for r in rows:
        out[r.title_name][r.slot_ts] = {"ign": r.ign, "coords": r.coords} if r.coords else r.ign
    return dict(out)

def schedule_lookup(days, slots):
    out = defaultdict(dict)  # { "YYYY-MM-DD": { "HH:MM": {title: entry} } }
    rows = Reservation.query.all()
    for r in rows:
        d, t = r.slot_ts.split("T")
        t = t[:5]
        cell = out[d].get(t, {})
        cell[r.title_name] = {"ign": r.ign, "coords": r.coords} if r.coords else r.ign
        out[d][t] = cell
    return out