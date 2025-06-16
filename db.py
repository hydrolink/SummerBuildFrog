from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Date
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, date

# Define base and engine
Base = declarative_base()
engine = create_engine("sqlite:///meetings.db", echo=False)
SessionLocal = sessionmaker(bind=engine)

# Define Meeting model
class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer, index=True)
    summary = Column(Text)                # Full summary text
    time = Column(String, nullable=True)  # Optional: extracted time (e.g., 3 PM)
    place = Column(String, nullable=True) # Optional: extracted place (e.g., Starbucks)
    pax = Column(String, nullable=True)   # Optional: number or list of people
    activity = Column(String, nullable=True)  # Optional: e.g., "study session"
    meet_date = Column(Date, nullable=True)   # Extracted meetup date
    created_at = Column(DateTime, default=datetime.utcnow)

# Define OutlookToken model
class OutlookToken(Base):
    __tablename__ = "outlook_tokens"

    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(String, unique=True, index=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

# Create tables
Base.metadata.create_all(bind=engine)

# Helper function to create a meeting and return its ID
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
