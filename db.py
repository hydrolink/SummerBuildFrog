from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Date
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime

Base = declarative_base()
engine = create_engine("sqlite:///meetings.db", echo=False)
SessionLocal = sessionmaker(bind=engine)

class Meeting(Base):
    __tablename__ = "meetings"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer)
    summary = Column(Text)
    time = Column(String)
    place = Column(String)
    pax = Column(String)
    activity = Column(String)
    meet_date = Column(Date) # <- NEW DATE COLUMN
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)
