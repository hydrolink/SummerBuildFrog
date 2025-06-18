from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Date
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, date

# Base and engine setup
Base = declarative_base()
engine = create_engine("sqlite:///meetings.db", echo=False)
SessionLocal = sessionmaker(bind=engine)

# Meeting model
class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer, index=True)
    summary = Column(Text)
    time = Column(String, nullable=True)
    place = Column(String, nullable=True)
    pax = Column(String, nullable=True)
    activity = Column(String, nullable=True)
    meet_date = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

# OutlookToken model
class OutlookToken(Base):
    __tablename__ = "outlook_tokens"

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(String, unique=True, index=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

# Table creation
def init_db():
    Base.metadata.create_all(bind=engine)

# Optional helper function
def create_meeting(chat_id: int, summary: str, time: str = None, place: str = None,
                   pax: str = None, activity: str = None, meet_date: date = None) -> int:
    db = SessionLocal()
    meeting = Meeting(
        chat_id=chat_id,
        summary=summary,
        time=time,
        place=place,
        pax=pax,
        activity=activity,
        meet_date=meet_date
    )
    db.add(meeting)
    db.commit()
    db.refresh(meeting)
    db.close()
    return meeting.id

# Only run if this file is executed directly
if __name__ == "__main__":
    init_db()
    print("✅ Database and tables initialized.")
