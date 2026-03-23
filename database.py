import psycopg2
from dotenv import load_dotenv
import os

load_dotenv()

def test_connection():
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        print("✅ SUCCESS: Connected to PostgreSQL")
        conn.close()
    except Exception as e:
        print("❌ FAILED:", e)

test_connection()