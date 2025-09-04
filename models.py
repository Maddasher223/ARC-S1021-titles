from flask_sqlalchemy import SQLAlchemy
db = SQLAlchemy()

class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String, primary_key=True)
    value = db.Column(db.String, nullable=False)

class Title(db.Model):
    __tablename__ = "titles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, unique=True, nullable=False)
    icon_url = db.Column(db.String)
    requestable = db.Column(db.Boolean, default=True)

class ActiveTitle(db.Model):
    __tablename__ = "active_titles"
    id = db.Column(db.Integer, primary_key=True)
    title_name = db.Column(db.String, db.ForeignKey("titles.name"), nullable=False, unique=True)
    holder = db.Column(db.String, nullable=False)
    expires_at = db.Column(db.String)     # ISO string or None
    assigned_at = db.Column(db.String)    # ISO string

class Reservation(db.Model):
    __tablename__ = "reservations"
    id = db.Column(db.Integer, primary_key=True)
    title_name = db.Column(db.String, db.ForeignKey("titles.name"), nullable=False)
    ign = db.Column(db.String, nullable=False)
    coords = db.Column(db.String)
    slot_ts = db.Column(db.String, nullable=False)  # YYYY-MM-DDTHH:MM:SS
    __table_args__ = (db.UniqueConstraint("title_name", "slot_ts", name="uix_title_slot"),)

class RequestLog(db.Model):
    __tablename__ = "request_log"
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.String, nullable=False)
    title_name = db.Column(db.String, nullable=False)
    in_game_name = db.Column(db.String, nullable=False)
    coordinates = db.Column(db.String)
    discord_user = db.Column(db.String)