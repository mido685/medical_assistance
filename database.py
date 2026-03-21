import psycopg2

from dotenv import load_dotenv
import os

load_dotenv()
print(os.getenv("DATABASE_URL"))
def get_connection():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def get_medications(email: str) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT u.name, m.drug_name, m.dose, m.time
        FROM medications m
        JOIN users u ON m.user_id = u.id
        WHERE u.email = %s
    """, (email,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [
        {
            "patient": row[0],
            "drug": row[1],
            "dose": row[2],
            "time": str(row[3])
        }
        for row in rows
    ]