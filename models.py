"""ShareHope database schema (MySQL via the existing Flask-SQLAlchemy `db`).

Tables are created safely with ``db.create_all()`` — which only creates
missing tables and never drops or alters existing ones. Repeatable via::

    flask init-db          # or
    python -c "from app import app, db; import models; \
               app.app_context().push(); db.create_all()"

No fabricated donations or NGOs are inserted here. Passwords are never
stored in plain text: use ``User.set_password`` / ``check_password``.
"""

from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db

# Status vocabularies (also enforced at the application layer).
OFFER_STATUSES = ("pending", "accepted", "handed_over", "declined")
REQUIREMENT_STATUSES = ("open", "fulfilled", "closed")
URGENCIES = ("urgent", "soon", "open")
ACCEPTANCE_STATUSES = ("pending", "accepted", "declined")
HANDOVER_STATUSES = ("pending", "handed_over")
NOTIFICATION_TYPES = ("info", "accept", "need", "reminder", "handover")


class User(db.Model, UserMixin):
    """Donors, NGOs and admins. One row per login email."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="donor", index=True)
    # Optional profile fields collected at registration. Nullable so the
    # original 4-column rows keep working; added safely, never dropped.
    city = db.Column(db.String(160), nullable=True)
    org_name = db.Column(db.String(160), nullable=True)
    org_reg_id = db.Column(db.String(80), nullable=True)
    # Email verification (added backward-compatibly; nullable/default so
    # old rows keep working). See ensure_email_columns().
    email_verified = db.Column(db.Boolean, nullable=False, default=False)
    verified_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.CheckConstraint(
            "role IN ('donor', 'ngo', 'admin')", name="ck_users_role"
        ),
    )

    offers = db.relationship(
        "DonationOffer", back_populates="donor",
        foreign_keys="DonationOffer.donor_id",
    )
    requirements = db.relationship(
        "NGORequirement", back_populates="ngo",
        foreign_keys="NGORequirement.ngo_id",
    )
    sent_messages = db.relationship(
        "Message", back_populates="sender",
        foreign_keys="Message.sender_id",
    )
    received_messages = db.relationship(
        "Message", back_populates="receiver",
        foreign_keys="Message.receiver_id",
    )
    notifications = db.relationship(
        "Notification", back_populates="recipient",
        foreign_keys="Notification.recipient_id",
        cascade="all, delete-orphan",
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.id} {self.email} ({self.role})>"


class DonationOffer(db.Model):
    """Something a donor offers: title, category, quantity, place, status."""

    __tablename__ = "donation_offers"

    id = db.Column(db.Integer, primary_key=True)
    donor_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(40), nullable=False, index=True)
    quantity = db.Column(db.String(120), nullable=True)
    location = db.Column(db.String(160), nullable=True, index=True)
    availability = db.Column(
        db.String(160), nullable=False, default="Flexible"
    )
    status = db.Column(
        db.String(20), nullable=False, default="pending", index=True
    )
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('pending', 'accepted', 'handed_over', 'declined')",
            name="ck_offers_status",
        ),
    )

    donor = db.relationship(
        "User", back_populates="offers", foreign_keys=[donor_id]
    )
    tracking_links = db.relationship(
        "DonationTracking", back_populates="offer",
        foreign_keys="DonationTracking.offer_id",
    )

    def __repr__(self):
        return f"<DonationOffer {self.id} {self.title!r} ({self.status})>"


class NGORequirement(db.Model):
    """Something an NGO asks for: category, required quantity, urgency."""

    __tablename__ = "ngo_requirements"

    id = db.Column(db.Integer, primary_key=True)
    ngo_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(40), nullable=False, index=True)
    required_quantity = db.Column(db.String(120), nullable=True)
    location = db.Column(db.String(160), nullable=True, index=True)
    urgency = db.Column(db.String(20), nullable=False, default="open")
    status = db.Column(db.String(20), nullable=False, default="open", index=True)
    deadline = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        db.CheckConstraint(
            "urgency IN ('urgent', 'soon', 'open')",
            name="ck_requirements_urgency",
        ),
        db.CheckConstraint(
            "status IN ('open', 'fulfilled', 'closed')",
            name="ck_requirements_status",
        ),
    )

    ngo = db.relationship(
        "User", back_populates="requirements", foreign_keys=[ngo_id]
    )
    tracking_links = db.relationship(
        "DonationTracking", back_populates="requirement",
        foreign_keys="DonationTracking.requirement_id",
    )

    def __repr__(self):
        return f"<NGORequirement {self.id} {self.title!r} ({self.status})>"


class DonationTracking(db.Model):
    """Link between an offer and a requirement, with accept/handover trail."""

    __tablename__ = "donation_tracking"

    id = db.Column(db.Integer, primary_key=True)
    offer_id = db.Column(
        db.Integer, db.ForeignKey("donation_offers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    requirement_id = db.Column(
        db.Integer, db.ForeignKey("ngo_requirements.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    acceptance_status = db.Column(
        db.String(20), nullable=False, default="pending"
    )
    handover_status = db.Column(
        db.String(20), nullable=False, default="pending"
    )
    accepted_at = db.Column(db.DateTime, nullable=True)
    handed_over_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        db.CheckConstraint(
            "acceptance_status IN ('pending', 'accepted', 'declined')",
            name="ck_tracking_acceptance",
        ),
        db.CheckConstraint(
            "handover_status IN ('pending', 'handed_over')",
            name="ck_tracking_handover",
        ),
        db.UniqueConstraint(
            "offer_id", "requirement_id", name="uq_tracking_offer_requirement"
        ),
    )

    offer = db.relationship(
        "DonationOffer", back_populates="tracking_links",
        foreign_keys=[offer_id],
    )
    requirement = db.relationship(
        "NGORequirement", back_populates="tracking_links",
        foreign_keys=[requirement_id],
    )

    def __repr__(self):
        return (
            f"<DonationTracking {self.id} offer={self.offer_id} "
            f"req={self.requirement_id} accept={self.acceptance_status} "
            f"handover={self.handover_status}>"
        )


class DonationHistory(db.Model):
    """Append-only log of donation status changes."""

    __tablename__ = "donation_history"

    id = db.Column(db.Integer, primary_key=True)
    offer_id = db.Column(
        db.Integer, db.ForeignKey("donation_offers.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    requirement_id = db.Column(
        db.Integer, db.ForeignKey("ngo_requirements.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    old_status = db.Column(db.String(30), nullable=True)
    new_status = db.Column(db.String(30), nullable=False)
    changed_by = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, index=True
    )

    offer = db.relationship("DonationOffer", foreign_keys=[offer_id])
    requirement = db.relationship("NGORequirement", foreign_keys=[requirement_id])
    changer = db.relationship("User", foreign_keys=[changed_by])

    def __repr__(self):
        return (
            f"<DonationHistory {self.id} offer={self.offer_id} "
            f"{self.old_status}->{self.new_status}>"
        )


class Message(db.Model):
    """Donor–NGO messages, one thread per donation exchange."""

    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    receiver_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    content = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, index=True
    )
    read_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.Index("ix_messages_receiver_read", "receiver_id", "is_read"),
    )

    sender = db.relationship(
        "User", back_populates="sent_messages", foreign_keys=[sender_id]
    )
    receiver = db.relationship(
        "User", back_populates="received_messages",
        foreign_keys=[receiver_id],
    )

    def __repr__(self):
        return (
            f"<Message {self.id} {self.sender_id}->{self.receiver_id} "
            f"read={self.is_read}>"
        )


class FeaturedSlip(db.Model):
    """Which donation slip the Home page shows — admin's choice.

    Exactly one row (id=1). ``offer_id`` is NULL when no slip is
    featured, which is what makes the Home page fall back to its empty
    state. Adding this table never alters any existing table or row.
    """

    __tablename__ = "featured_slip"

    id = db.Column(db.Integer, primary_key=True)
    offer_id = db.Column(
        db.Integer, db.ForeignKey("donation_offers.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    updated_by = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    offer = db.relationship(
        "DonationOffer", foreign_keys=[offer_id],
    )
    updater = db.relationship(
        "User", foreign_keys=[updated_by],
    )

    def __repr__(self):
        return f"<FeaturedSlip offer={self.offer_id}>"


class Notification(db.Model):
    """In-app alerts: accepts, new needs, reminders, handovers."""

    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    recipient_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    content = db.Column(db.String(500), nullable=False)
    type = db.Column(db.String(30), nullable=False, default="info")
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, index=True
    )

    __table_args__ = (
        db.CheckConstraint(
            "type IN ('info', 'accept', 'need', 'reminder', 'handover')",
            name="ck_notifications_type",
        ),
        db.Index("ix_notifications_recipient_read", "recipient_id", "is_read"),
    )

    recipient = db.relationship(
        "User", back_populates="notifications",
        foreign_keys=[recipient_id],
    )

    def __repr__(self):
        return (
            f"<Notification {self.id} to={self.recipient_id} "
            f"({self.type}) read={self.is_read}>"
        )


#: All tables managed by ``flask init-db``.
TABLES = (
    "users",
    "donation_offers",
    "ngo_requirements",
    "donation_tracking",
    "donation_history",
    "messages",
    "notifications",
    "featured_slip",
)


def ensure_profile_columns():
    """Add nullable profile columns to ``users`` if an older table lacks them.

    Only ADDs missing columns; never drops, renames or alters existing ones.
    Safe to run repeatedly on any database state.
    """
    from sqlalchemy import text

    wanted = {
        "city": "VARCHAR(160) NULL",
        "org_name": "VARCHAR(160) NULL",
        "org_reg_id": "VARCHAR(80) NULL",
    }
    with db.engine.begin() as conn:
        existing = {
            col["name"]
            for col in db.inspect(conn).get_columns("users")
        }
        added = []
        for name, ddl in wanted.items():
            if name not in existing:
                conn.execute(
                    text(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
                )
                added.append(name)
    return added


def ensure_featured_slip_table():
    """Create the ``featured_slip`` table when an older database lacks it.

    Additive only: creates one new empty table, never touches or drops
    an existing one. Safe to run repeatedly on any database state.
    """
    from sqlalchemy import text

    with db.engine.begin() as conn:
        if "featured_slip" in db.inspect(conn).get_table_names():
            return False
        conn.execute(text(
            "CREATE TABLE featured_slip ("
            "id INTEGER NOT NULL, "
            "offer_id INTEGER NULL, "
            "updated_by INTEGER NULL, "
            "updated_at DATETIME NOT NULL, "
            "PRIMARY KEY (id), "
            "FOREIGN KEY(offer_id) REFERENCES donation_offers (id) "
            "ON DELETE SET NULL, "
            "FOREIGN KEY(updated_by) REFERENCES users (id) "
            "ON DELETE SET NULL)"
        ))
        return True


def ensure_email_columns():
    """Add email-verification columns to ``users`` if an older table lacks them.

    Only ADDs missing columns; never drops or alters existing ones.
    Needed because ``db.create_all()`` creates missing *tables* but never
    adds columns to existing tables. Safe to run repeatedly.
    """
    from sqlalchemy import text

    wanted = {
        # TINYINT(1) is the MySQL boolean; SQLite accepts it too.
        "email_verified": "TINYINT(1) NOT NULL DEFAULT 0",
        "verified_at": "DATETIME NULL",
    }
    with db.engine.begin() as conn:
        existing = {
            col["name"]
            for col in db.inspect(conn).get_columns("users")
        }
        added = []
        for name, ddl in wanted.items():
            if name not in existing:
                conn.execute(
                    text(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
                )
                added.append(name)
    return added