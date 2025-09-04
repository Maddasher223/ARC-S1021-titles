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
# models.py (add at bottom or inside the models file)

class ServerConfig(db.Model):
    __tablename__ = "server_config"
    guild_id = db.Column(db.String, primary_key=True)
    webhook_url = db.Column(db.String, nullable=False)
    guardian_role_id = db.Column(db.String, nullable=True)
    is_default = db.Column(db.Boolean, default=False)

    @classmethod
    def clear_default(cls):
        db.session.query(cls).update({cls.is_default: False})
        db.session.commit()

class Setting(db.Model):
    __tablename__ = "setting"
    key = db.Column(db.String, primary_key=True)
    value = db.Column(db.String, nullable=False)

    @classmethod
    def set(cls, key, val):
        row = db.session.get(cls, key)
        if not row:
            row = cls(key=key, value=str(val))
            db.session.add(row)
        else:
            row.value = str(val)
        db.session.commit()