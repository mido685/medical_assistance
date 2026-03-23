"""
Medical Assistant API — Production-Ready
========================================
Best practices applied:
  - Environment-based config via pydantic-settings
  - Connection pooling with psycopg2 pool
  - Structured logging (JSON-ready)
  - Global exception handler + HTTP error handler
  - Rate limiting per IP
  - Input validation & sanitization
  - Dependency injection for DB
  - Graceful scheduler shutdown
  - Health-check & readiness endpoints
  - Type hints everywhere
  - No secrets hardcoded
"""

# ── Standard Library ──────────────────────────────────────────────────────────
import logging
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Third-Party ───────────────────────────────────────────────────────────────
import dateparser
import psycopg2
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from pydantic import BaseModel, EmailStr, Field, field_validator
from pydantic_settings import BaseSettings
from transformers import (
    AutoModelForTokenClassification,
    AutoTokenizer,
    pipeline,
)

# ═════════════════════════════════════════════════════════════════════════════
# 1. CONFIGURATION
from pydantic_settings import BaseSettings
from pathlib import Path

class Settings(BaseSettings):
    database_url        : str       # must be set as ENV
    telegram_bot_token  : str
    telegram_chat_id    : str
    model_path          : str
    log_level           : str       = "INFO"
    allowed_origins     : list[str] = ["*"]
    rate_limit_requests : int       = 30
    rate_limit_window   : int       = 60
    db_pool_min         : int       = 2
    db_pool_max         : int       = 10
    timezone            : str       = "Africa/Cairo"
    class Config:
        # no .env needed, just read from ENV vars
        env_file = ".env"  # Specify the path to your .env file

settings = Settings()

# ═════════════════════════════════════════════════════════════════════════════
# 2. LOGGING
# ═════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("medical_api")

# ═════════════════════════════════════════════════════════════════════════════
# 3. DATABASE — connection pool
# ═════════════════════════════════════════════════════════════════════════════

_pool: Optional[ThreadedConnectionPool] = None


def get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(
            settings.db_pool_min,
            settings.db_pool_max,
            settings.database_url,
            cursor_factory=RealDictCursor,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
        logger.info("DB connection pool created (min=%d, max=%d)",
                    settings.db_pool_min, settings.db_pool_max)
    return _pool


def get_db():
    """FastAPI dependency — yields a validated connection from the pool."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        conn.cursor().execute("SELECT 1")
    except psycopg2.OperationalError:
        logger.warning("Stale DB connection detected — replacing with a fresh one")
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_db() -> None:
    pool = get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id               SERIAL PRIMARY KEY,
                name             TEXT        NOT NULL,
                age              INTEGER     NOT NULL CHECK (age > 0 AND age < 130),
                email            TEXT UNIQUE NOT NULL,
                telegram_chat_id TEXT,
                created_at       TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Migration: safely add telegram_chat_id to existing DB
        cur.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS telegram_chat_id TEXT
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS medications (
                id             SERIAL PRIMARY KEY,
                user_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
                drug           TEXT        NOT NULL,
                dose           TEXT,
                frequency      TEXT,
                time_of_day    TEXT,
                reminder_times TEXT[],
                next_reminder  TEXT,
                raw_text       TEXT,
                telegram_sent  BOOLEAN     DEFAULT FALSE,
                created_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_med_reminder_times
            ON medications USING GIN (reminder_times)
        """)
        conn.commit()
        cur.close()
        logger.info("Database initialised successfully")
    except Exception as exc:
        conn.rollback()
        logger.error("Database init failed: %s", exc)
        raise
    finally:
        pool.putconn(conn)


# ═════════════════════════════════════════════════════════════════════════════
# 4. TIME / SCHEDULE UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

VAGUE_TIME_MAP: dict[str, str] = {
    "morning"                      : "07:00",
    "every morning"                : "07:00",
    "after breakfast"              : "08:00",
    "breakfast"                    : "08:00",
    "noon"                         : "12:00",
    "afternoon"                    : "14:00",
    "after lunch"                  : "13:00",
    "lunch"                        : "13:00",
    "evening"                      : "18:00",
    "every evening"                : "18:00",
    "after dinner"                 : "20:00",
    "dinner"                       : "19:00",
    "night"                        : "21:00",
    "at night"                     : "21:00",
    "once at night"                : "21:00",
    "before bed"                   : "22:00",
    "bedtime"                      : "22:00",
    "after meals"                  : "08:00",
    "with meals"                   : "08:00",
    "every 8 hours"                : "08:00",
    "breakfast, lunch, and dinner" : "08:00",
    "8 am and 8 pm"                : "08:00",
    "breakfast and dinner"         : "08:00",
    "morning and evening"          : "07:00",
}

FREQ_TO_TIMES: dict[str, list[str]] = {
    "once daily"        : ["08:00"],
    "every morning"     : ["07:00"],
    "every evening"     : ["18:00"],
    "once at night"     : ["21:00"],
    "twice a day"       : ["08:00", "20:00"],
    "twice daily"       : ["08:00", "20:00"],
    "three times daily" : ["08:00", "14:00", "20:00"],
    "three times a day" : ["08:00", "14:00", "20:00"],
    "every 8 hours"     : ["08:00", "16:00", "00:00"],
    "every 6 hours"     : ["06:00", "12:00", "18:00", "00:00"],
    "four times daily"  : ["08:00", "12:00", "16:00", "20:00"],
    "four times a day"  : ["08:00", "12:00", "16:00", "20:00"],
}

FREQ_PATTERNS = re.compile(
    r"\b(every\s+\d+\s+hours?|every\s+morning|every\s+evening|every\s+night|"
    r"once\s+daily|once\s+at\s+night|twice\s+a\s+day|twice\s+daily|"
    r"three\s+times\s+daily|three\s+times\s+a\s+day|"
    r"four\s+times\s+daily|four\s+times\s+a\s+day|\d+\s+times\s+daily)\b",
    re.IGNORECASE,
)


def normalize_time(time_str: str) -> str:
    if not time_str or time_str in ("N/A", ""):
        return "08:00"
    clean = time_str.strip().lower()
    if clean in VAGUE_TIME_MAP:
        return VAGUE_TIME_MAP[clean]
    parsed = dateparser.parse(
        time_str,
        settings={"PREFER_DAY_OF_MONTH": "first", "RETURN_AS_TIMEZONE_AWARE": False},
    )
    return parsed.strftime("%H:%M") if parsed else "08:00"


def clean_time_string(time_str: str) -> str:
    """Fix NER-fragmented time strings like '8 : 35 AM' → '8:35 AM'."""
    if not time_str or time_str in ("N/A", ""):
        return ""
    fixed = re.sub(r"(\d)\s*:\s*(\d)", r"\1:\2", time_str.strip())
    fixed = re.sub(r"\d+\s*:\s*$", lambda m: m.group().split(":")[0], fixed).strip()
    return fixed


def get_reminder_times(frequency: str, time_of_day: str) -> list[str]:
    freq_clean = (frequency or "").strip().lower()
    time_clean = (time_of_day or "").strip().lower()

    # Step 1 — explicit clock time (highest priority)
    if time_of_day and time_of_day not in ("N/A", "") and time_clean not in VAGUE_TIME_MAP:
        cleaned     = clean_time_string(time_of_day)
        clock_match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", cleaned, re.IGNORECASE)
        if clock_match:
            hour   = int(clock_match.group(1))
            minute = int(clock_match.group(2))
            ampm   = (clock_match.group(3) or "").upper()
            if ampm == "PM" and hour != 12:
                hour += 12
            elif ampm == "AM" and hour == 12:
                hour = 0
            h = hour % 24
            m = minute
            if freq_clean in ("twice a day", "twice daily"):
                return [f"{h:02d}:{m:02d}", f"{(h+12)%24:02d}:{m:02d}"]
            if freq_clean in ("three times daily", "three times a day"):
                return [f"{h:02d}:{m:02d}", f"{(h+8)%24:02d}:{m:02d}", f"{(h+16)%24:02d}:{m:02d}"]
            if freq_clean in ("four times daily", "four times a day"):
                return [f"{h:02d}:{m:02d}", f"{(h+6)%24:02d}:{m:02d}", f"{(h+12)%24:02d}:{m:02d}", f"{(h+18)%24:02d}:{m:02d}"]
            if "every 8 hours" in freq_clean:
                return [f"{h:02d}:{m:02d}", f"{(h+8)%24:02d}:{m:02d}", f"{(h+16)%24:02d}:{m:02d}"]
            if "every 6 hours" in freq_clean:
                return [f"{h:02d}:{m:02d}", f"{(h+6)%24:02d}:{m:02d}", f"{(h+12)%24:02d}:{m:02d}", f"{(h+18)%24:02d}:{m:02d}"]
            return [f"{h:02d}:{m:02d}"]

    # Step 2 — frequency map defaults
    if freq_clean in FREQ_TO_TIMES:
        return FREQ_TO_TIMES[freq_clean]

    # Step 3 — vague time or final fallback
    return [normalize_time(time_of_day)]


def build_schedule(drug: str, dose: str, frequency: str, time_of_day: str) -> dict:
    reminder_times = get_reminder_times(frequency, time_of_day)
    return {
        "drug"           : drug,
        "dose"           : dose,
        "frequency"      : frequency,
        "time_of_day"    : time_of_day,
        "reminder_times" : reminder_times,
        "reminders_count": len(reminder_times),
        "next_reminder"  : reminder_times[0] if reminder_times else "08:00",
    }


# ═════════════════════════════════════════════════════════════════════════════
# 5. NER MODEL
# ═════════════════════════════════════════════════════════════════════════════

_ner_pipeline = None


def load_model() -> None:
    global _ner_pipeline
    logger.info("loading model from hugging face: %s", settings.model_path)
    tokenizer = AutoTokenizer.from_pretrained(settings.model_path)
    model     = AutoModelForTokenClassification.from_pretrained(settings.model_path)
    _ner_pipeline = pipeline(
        "ner",
        model=model,
        tokenizer=tokenizer,
        aggregation_strategy="first"
    )
    logger.info("model loaded successfully from hugging face!")

def extract_entities(text: str) -> dict[str, list[str]]:
    if _ner_pipeline is None:
        raise RuntimeError("NER model is not loaded")

    results: list[dict] = _ner_pipeline(text)

    for entity in results:
        if entity["entity_group"] == "TIME" and FREQ_PATTERNS.search(entity["word"]):
            entity["entity_group"] = "FREQ"

    detected_spans = [(e["start"], e["end"]) for e in results]
    for match in FREQ_PATTERNS.finditer(text):
        if not any(s <= match.start() < e for s, e in detected_spans):
            results.append({
                "entity_group": "FREQ",
                "word"        : match.group(),
                "start"       : match.start(),
                "end"         : match.end(),
                "score"       : 1.0,
            })

    results  = sorted(results, key=lambda x: x["start"])
    entities : dict[str, list[str]] = {"DRUG": [], "DOSE": [], "FREQ": [], "TIME": []}
    for e in results:
        if e["entity_group"] in entities:
            group = e["entity_group"]
            word  = e["word"]
            if group == "TIME" and entities["TIME"]:
                prev = entities["TIME"][-1]
                if re.search(r"[\d:]\s*$", prev) and re.match(r"^\s*[\d:]", word):
                    entities["TIME"][-1] = prev.rstrip() + " " + word.lstrip()
                    continue
            entities[group].append(word)
    return entities


# ═════════════════════════════════════════════════════════════════════════════
# 6. TELEGRAM
# ═════════════════════════════════════════════════════════════════════════════

_TELEGRAM_URL = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"


def send_telegram(schedule: dict, label: str = "Reminder") -> bool:
    times_str = " | ".join(schedule.get("reminder_times", [])) or "N/A"
    user_name = schedule.get("user_name") or "there"
    user_age  = schedule.get("user_age")
    age_note  = f" (age {user_age})" if user_age else ""
    msg = (
        f"👋 <b>Hello, {user_name}{age_note}!</b>\n\n"
        f"💊 <b>Medication {label}</b>\n\n"
        f"🔹 Drug: <b>{schedule['drug']}</b>\n"
        f"🔹 Dose: <b>{schedule['dose']}</b>\n"
        f"🔹 Frequency: <b>{schedule['frequency']}</b>\n"
        f"🔹 Time: <b>{schedule['time_of_day']}</b>\n"
        f"⏰ Reminder times: <b>{times_str}</b>\n\n"
        f"<i>Stay healthy, {user_name}! Your medical assistant is tracking your medication.</i>"
    )
    try:
        # Use the user's own Telegram chat ID if available,
        # fall back to the global chat ID from settings
        chat_id = schedule.get("telegram_chat_id") or settings.telegram_chat_id
        if not chat_id:
            logger.warning("No Telegram chat ID for user %s — skipping", user_name)
            return False
        resp   = requests.post(_TELEGRAM_URL, json={
            "chat_id"   : chat_id,
            "text"      : msg,
            "parse_mode": "HTML",
        }, timeout=10)
        result = resp.json()
        if resp.status_code == 200 and result.get("ok"):
            logger.info("Telegram %s sent for %s → %s", label, user_name, schedule["drug"])
            return True
        logger.warning("Telegram error %d: %s", resp.status_code, result)
        return False
    except Exception as exc:
        logger.error("Telegram exception: %s", exc)
        return False


# ═════════════════════════════════════════════════════════════════════════════
# 7. SCHEDULER
# ═════════════════════════════════════════════════════════════════════════════

def send_scheduled_reminders() -> None:
    import pytz
    from datetime import datetime as dt
    tz  = pytz.timezone(settings.timezone)
    now = dt.now(tz).strftime("%H:%M")
    logger.info("Scheduler tick — checking reminders for %s (tz: %s)", now, settings.timezone)
    pool = get_pool()
    conn = pool.getconn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT m.*, u.name AS user_name, u.age AS user_age,
                   u.telegram_chat_id AS user_telegram_chat_id
            FROM medications m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE %s = ANY(m.reminder_times)
        """, (now,))
        meds = cur.fetchall()
        cur.close()
        for med in meds:
            schedule = {
                "drug"               : med["drug"],
                "dose"               : med["dose"],
                "frequency"          : med["frequency"],
                "time_of_day"        : med["time_of_day"],
                "reminder_times"     : list(med["reminder_times"]),
                "user_name"          : med.get("user_name"),
                "user_age"           : med.get("user_age"),
                "telegram_chat_id"   : med.get("user_telegram_chat_id"),
            }
            send_telegram(schedule, label="Scheduled Reminder")
            logger.info("Reminder dispatched for %s → %s at %s",
                        med.get("user_name") or "unknown", med["drug"], now)
    except Exception as exc:
        logger.error("Scheduler error: %s", exc)
    finally:
        pool.putconn(conn)


# ═════════════════════════════════════════════════════════════════════════════
# 8. RATE LIMITER
# ═════════════════════════════════════════════════════════════════════════════

_rate_store: dict[str, list[float]] = defaultdict(list)


def rate_limit(request: Request) -> None:
    ip           = request.client.host
    now          = time.time()
    window_start = now - settings.rate_limit_window
    _rate_store[ip] = [t for t in _rate_store[ip] if t > window_start]
    if len(_rate_store[ip]) >= settings.rate_limit_requests:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Max {settings.rate_limit_requests} requests per {settings.rate_limit_window}s.",
        )
    _rate_store[ip].append(now)


# ═════════════════════════════════════════════════════════════════════════════
# 9. APP LIFESPAN
# ═════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_model()
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(send_scheduled_reminders, "cron", minute="*")
    scheduler.start()
    logger.info("Scheduler started — checking reminders every minute")
    yield
    scheduler.shutdown(wait=False)
    if _pool:
        _pool.closeall()
    logger.info("Shutdown complete")


# ═════════════════════════════════════════════════════════════════════════════
# 10. APP INSTANCE + MIDDLEWARE
# ═════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Medical Assistant API",
    version="1.0.0",
    description="NER-powered medication tracker with Telegram reminders",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "type": type(exc).__name__},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ═════════════════════════════════════════════════════════════════════════════
# 11. SCHEMAS
# ═════════════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    name             : str           = Field(..., min_length=2, max_length=100)
    age              : int           = Field(..., gt=0, lt=130)
    email            : EmailStr
    telegram_chat_id : Optional[str] = None   # user's own Telegram chat ID


class UserResponse(BaseModel):
    id               : int
    name             : str
    age              : int
    email            : str
    telegram_chat_id : Optional[str] = None
    created_at       : str


class ChatRequest(BaseModel):
    message    : str           = Field(..., min_length=3, max_length=1000)
    user_email : Optional[str] = None   # ← identify user by email (recommended)
    user_name  : Optional[str] = None   # ← identify user by name (fallback)
    user_id    : Optional[int] = None   # ← identify user by id (fallback)

    @field_validator("message")
    @classmethod
    def sanitize_message(cls, v: str) -> str:
        return " ".join(v.split())


class MedicationResponse(BaseModel):
    reply        : str
    entities     : dict
    saved        : bool
    telegram_sent: bool
    medications  : list
    schedules    : list


# ═════════════════════════════════════════════════════════════════════════════
# 12. HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _serialize_row(row: dict) -> dict:
    for key, val in row.items():
        if isinstance(val, datetime):
            row[key] = val.isoformat()
    return row


def get_all_medications(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM medications ORDER BY created_at DESC LIMIT 50")
    rows = [_serialize_row(dict(r)) for r in cur.fetchall()]
    cur.close()
    return rows


def resolve_user(req: ChatRequest, conn) -> tuple[Optional[int], Optional[str], Optional[int]]:
    """
    Resolve user_id, user_name, user_age from email, name, or id.
    Returns (resolved_user_id, user_name, user_age).
    """
    if not any([req.user_email, req.user_name, req.user_id]):
        return None, None, None

    cur = conn.cursor()
    if req.user_email:
        cur.execute("SELECT id, name, age FROM users WHERE email = %s", (req.user_email,))
    elif req.user_id:
        cur.execute("SELECT id, name, age FROM users WHERE id = %s", (req.user_id,))
    else:
        cur.execute("SELECT id, name, age FROM users WHERE LOWER(name) = LOWER(%s) LIMIT 1", (req.user_name,))

    row = cur.fetchone()
    cur.close()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found. Please register first at POST /register.",
        )
    return row["id"], row["name"], row["age"]


# ═════════════════════════════════════════════════════════════════════════════
# 13. ROUTES
# ═════════════════════════════════════════════════════════════════════════════

@app.get("/", tags=["UI"], include_in_schema=False)
def serve_ui():
    """Serve the STARK frontend UI."""
    # Try stark_final.html first, fallback to stark_connected.html
    for name in ["stark_final.html", "stark_connected.html"]:
        html_path = Path(__file__).parent / name
        if html_path.exists():
            return FileResponse(html_path)
    raise HTTPException(status_code=404, detail="UI file not found. Make sure stark_final.html is in the same folder as main.py")


@app.get("/health", tags=["System"])
def health_check():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/ready", tags=["System"])
def readiness_check():
    issues = []
    try:
        pool = get_pool()
        conn = pool.getconn()
        conn.cursor().execute("SELECT 1")
        pool.putconn(conn)
    except Exception as exc:
        issues.append(f"DB: {exc}")
    if _ner_pipeline is None:
        issues.append("NER model not loaded")
    if issues:
        return JSONResponse(status_code=503, content={"status": "not ready", "issues": issues})
    return {"status": "ready"}


@app.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED, tags=["Users"])
def register_user(req: RegisterRequest, _: None = Depends(rate_limit), conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE email = %s", (req.email,))
    if cur.fetchone():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with email '{req.email}' already exists.",
        )
    cur.execute(
        """INSERT INTO users (name, age, email, telegram_chat_id)
           VALUES (%s, %s, %s, %s)
           RETURNING id, name, age, email, telegram_chat_id, created_at""",
        (req.name, req.age, req.email, req.telegram_chat_id),
    )
    user = _serialize_row(dict(cur.fetchone()))
    conn.commit()
    cur.close()
    logger.info("New user registered: %s (id=%s)", req.email, user["id"])
    return user


@app.get("/users/{user_id}", tags=["Users"])
def get_user(user_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT id, name, age, email, telegram_chat_id, created_at FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found.")
    return _serialize_row(dict(row))


@app.get("/users/by-email", tags=["Users"], summary="Get user by email")
def get_user_by_email(email: str, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT id, name, age, email, telegram_chat_id, created_at FROM users WHERE email = %s", (email,))
    row = cur.fetchone()
    cur.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found.")
    return _serialize_row(dict(row))


@app.post("/chat", response_model=MedicationResponse, tags=["Medications"])
def chat(req: ChatRequest, _: None = Depends(rate_limit), conn=Depends(get_db)):

    # ── Resolve user from email / name / id ──
    resolved_user_id, user_name, user_age = resolve_user(req, conn)

    # ── Extract entities from message ──
    entities = extract_entities(req.message)
    drugs = entities.get("DRUG", [])
    doses = entities.get("DOSE", [])
    freqs = entities.get("FREQ", [])
    times = entities.get("TIME", [])

    if not drugs:
        return MedicationResponse(
            reply=(
                "I couldn't detect any medication in your message. "
                "Please mention a drug name, dose, and frequency."
            ),
            entities=entities,
            saved=False,
            telegram_sent=False,
            medications=get_all_medications(conn),
            schedules=[],
        )

    telegram_sent = False
    schedules     = []
    cur           = conn.cursor()

    for i, drug in enumerate(drugs):
        dose      = doses[i] if i < len(doses) else "N/A"
        frequency = freqs[i] if i < len(freqs) else (freqs[0] if freqs else "N/A")
        time_val  = times[i] if i < len(times) else (times[0] if times else "N/A")

        schedule = build_schedule(drug, dose, frequency, time_val)
        schedule["user_name"]        = user_name
        schedule["user_age"]         = user_age
        # Fetch user's telegram_chat_id for this message
        cur2 = conn.cursor()
        cur2.execute("SELECT telegram_chat_id FROM users WHERE id = %s", (resolved_user_id,))
        u_row = cur2.fetchone()
        cur2.close()
        schedule["telegram_chat_id"] = u_row["telegram_chat_id"] if u_row else None
        schedules.append(schedule)

        cur.execute("""
            INSERT INTO medications
                (user_id, drug, dose, frequency, time_of_day, reminder_times, next_reminder, raw_text)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            resolved_user_id,
            drug, dose, frequency, time_val,
            schedule["reminder_times"],
            schedule["next_reminder"],
            req.message,
        ))
        row_id = cur.fetchone()["id"]

        if send_telegram(schedule, label="Saved"):
            cur.execute("UPDATE medications SET telegram_sent = TRUE WHERE id = %s", (row_id,))
            telegram_sent = True

    conn.commit()
    cur.close()

    drug_list     = ", ".join(drugs)
    times_summary = " | ".join(f"{s['drug']}: {', '.join(s['reminder_times'])}" for s in schedules)
    reply = f"✅ Saved {drug_list}. ⏰ Reminders set for: {times_summary}."
    reply += " 📱 Telegram confirmation sent!" if telegram_sent else " ❌ Telegram failed."

    return MedicationResponse(
        reply=reply,
        entities=entities,
        saved=True,
        telegram_sent=telegram_sent,
        medications=get_all_medications(conn),
        schedules=schedules,
    )


@app.get("/medications", tags=["Medications"])
def list_medications(conn=Depends(get_db)):
    return get_all_medications(conn)


@app.get("/medications/user/{user_id}", tags=["Medications"])
def list_user_medications(user_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("SELECT * FROM medications WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
    rows = [_serialize_row(dict(r)) for r in cur.fetchall()]
    cur.close()
    return rows


@app.delete("/medications/{med_id}", tags=["Medications"])
def delete_medication(med_id: int, conn=Depends(get_db)):
    cur = conn.cursor()
    cur.execute("DELETE FROM medications WHERE id = %s RETURNING id", (med_id,))
    deleted = cur.fetchone()
    conn.commit()
    cur.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="Medication not found.")
    return {"deleted": True, "id": med_id}