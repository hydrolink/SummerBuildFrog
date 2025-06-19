from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Text, DateTime, Date
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime, date
import os
from dotenv import load_dotenv

load_dotenv()

# Use Railway PostgreSQL URL from your .env
DATABASE_URL = os.getenv("DATABASE_URL")  # e.g., postgresql://...

Base = declarative_base()
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)

# Define models
class Meeting(Base):
    __tablename__ = "meetings"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(BigInteger, index=True)  # ✅ Updated
    summary = Column(Text)
    time = Column(String, nullable=True)
    place = Column(String, nullable=True)
    pax = Column(String, nullable=True)
    activity = Column(String, nullable=True)
    meet_date = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class OutlookToken(Base):
    __tablename__ = "outlook_tokens"
    id = Column(Integer, primary_key=True, index=True)
    telegram_user_id = Column(BigInteger, unique=True, index=True)  # ✅ Updated
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

# Initialize tables
def init_db():
    Base.metadata.create_all(bind=engine)

if __name__ == "__main__":
    init_db()
    print("✅ Database and tables initialized.")
