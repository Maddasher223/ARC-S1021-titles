# seed.py — safe, standalone seeder (no import of main.py)

import os
from pathlib import Path
from flask import Flask
from dotenv import load_dotenv

load_dotenv()

# ---- Build a tiny app just for DB seeding ----
app = Flask(__name__)

# Ensure ./instance exists, or honor DATABASE_URL if provided
repo_root = Path(__file__).resolve().parent
instance_dir = repo_root / "instance"
instance_dir.mkdir(parents=True, exist_ok=True)

# Prefer DATABASE_URL if set (e.g., sqlite:////opt/render/data/app.db on Render)
env_db_url = os.getenv("DATABASE_URL", "").strip()

if env_db_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = env_db_url
else:
    # Absolute path is safest for SQLite
    sqlite_path = instance_dir / "app.db"
    # SQLAlchemy wants 4 slashes for absolute unix paths
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlite_path}" if os.name == "nt" else f"sqlite:////{sqlite_path}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "dev-seed-secret")

# Import your models and bind db to this tiny app
from models import db, Title, Setting  # noqa: E402

db.init_app(app)

DEFAULT_TITLES = [
    {"name": "Guardian of Harmony", "icon_url": "/static/icons/guardian_harmony.png", "requestable": False},
    {"name": "Guardian of Fire",    "icon_url": "/static/icons/guardian_fire.png",    "requestable": True},
    {"name": "Guardian of Water",   "icon_url": "/static/icons/guardian_water.png",   "requestable": True},
    {"name": "Guardian of Earth",   "icon_url": "/static/icons/guardian_earth.png",   "requestable": True},
    {"name": "Guardian of Air",     "icon_url": "/static/icons/guardian_air.png",     "requestable": True},
    {"name": "Architect",           "icon_url": "/static/icons/architect.png",        "requestable": True},
    {"name": "General",             "icon_url": "/static/icons/general.png",          "requestable": True},
    {"name": "Governor",            "icon_url": "/static/icons/governor.png",         "requestable": True},
    {"name": "Prefect",             "icon_url": "/static/icons/prefect.png",          "requestable": True},
]

DEFAULT_SETTINGS = {"shift_hours": "12"}

def upsert_title(name: str, icon_url: str | None, requestable: bool) -> bool:
    t = Title.query.filter_by(name=name).first()
    if t:
        changed = False
        if icon_url and not t.icon_url:
            t.icon_url = icon_url
            changed = True
        if t.requestable != requestable:
            t.requestable = requestable
            changed = True
        return changed
    else:
        db.session.add(Title(name=name, icon_url=icon_url, requestable=requestable))
        return True

def upsert_setting(key: str, value: str):
    row = Setting.query.get(key)
    if row:
        if row.value != value:
            row.value = value
    else:
        db.session.add(Setting(key=key, value=value))

if __name__ == "__main__":
    with app.app_context():
        # Create tables if they don't exist
        db.create_all()

        changed = 0
        for t in DEFAULT_TITLES:
            if upsert_title(t["name"], t["icon_url"], t["requestable"]):
                changed += 1

        for k, v in DEFAULT_SETTINGS.items():
            upsert_setting(k, v)

        db.session.commit()
        print(f"Seed complete. Titles added/updated: {changed}. DB URI: {app.config['SQLALCHEMY_DATABASE_URI']}")