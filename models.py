from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, Index

db = SQLAlchemy()


# ---------------- Settings ----------------
class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String, primary_key=True)
    value = db.Column(db.String, nullable=False)


# ---------------- Titles ----------------
class Title(db.Model):
    __tablename__ = "titles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, unique=True, nullable=False, index=True)
    icon_url = db.Column(db.String)
    requestable = db.Column(db.Boolean, default=True)


# ---------------- Active titles (live holders) ----------------
class ActiveTitle(db.Model):
    __tablename__ = "active_title"
    id = db.Column(db.Integer, primary_key=True)
    title_name = db.Column(db.String, db.ForeignKey("titles.name"), nullable=False, unique=True, index=True)
    holder = db.Column(db.String(80), nullable=False)
    claim_at = db.Column(db.DateTime(timezone=True), nullable=False)   # UTC
    expiry_at = db.Column(db.DateTime(timezone=True), nullable=True)   # None for Harmony


# ---------------- Reservations (calendar) ----------------
class Reservation(db.Model):
    __tablename__ = "reservation"  # singular; matches the SQL you were running
    id = db.Column(db.Integer, primary_key=True)

    title_name = db.Column(db.String, db.ForeignKey("titles.name"), nullable=False, index=True)
    ign = db.Column(db.String(120), nullable=False)
    coords = db.Column(db.String(32))

    # New canonical time column (UTC-aware)
    slot_dt = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    # Legacy text column kept only for back-compat during migration (YYYY-MM-DDTHH:MM:SS)
    # Make it nullable so we can phase it out later.
    slot_ts = db.Column(db.String(19), nullable=True)

    __table_args__ = (
        # New uniqueness (enforced for new data)
        UniqueConstraint("title_name", "slot_dt", name="uq_res_title_slotdt"),
        # Legacy uniqueness to avoid accidental duplicate legacy rows
        UniqueConstraint("title_name", "slot_ts", name="uq_res_title_slottxt"),
        Index("ix_reservation_slot_dt", "slot_dt"),
        Index("ix_reservation_slot_ts", "slot_ts"),
    )


# ---------------- Web form / Discord request log ----------------
class RequestLog(db.Model):
    __tablename__ = "request_log"
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.String, nullable=False)  # store as ISO8601 string
    title_name = db.Column(db.String, nullable=False)
    in_game_name = db.Column(db.String, nullable=False)
    coordinates = db.Column(db.String)
    discord_user = db.Column(db.String)