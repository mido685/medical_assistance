# 🏥 MedAssist — AI Medication Tracker

FastAPI + NER Model + PostgreSQL + Twilio WhatsApp

## Project Structure
```
medical_app/
├── main.py            ← FastAPI backend
├── requirements.txt   ← Python dependencies
├── .env.example       ← Environment variables template
└── static/
    └── index.html     ← Chat UI
```

## Setup Steps

### 1. Copy your model checkpoint
```bash
cp -r "/mnt/c/Users/HP/OneDrive/Desktop/medical_assistance/checkpoint-5000" ~/checkpoint-5000
```

### 2. Create PostgreSQL database
```bash
psql -U postgres
CREATE DATABASE medical_db;
\q
```

### 3. Install dependencies
```bash
conda activate medical_assistance
pip install -r requirements.txt
```

### 4. Set environment variables
```bash
cp .env.example .env
# Edit .env with your actual values
```

### 5. Run the app
```bash
# Load .env and start server
export $(cat .env | xargs)
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### 6. Open in browser
```
http://localhost:8000
```

---

## Twilio WhatsApp Setup

1. Sign up at https://twilio.com (free)
2. Go to **Messaging → Try it out → Send a WhatsApp message**
3. Join the sandbox by sending the join code from your phone
4. Copy your `Account SID` and `Auth Token` into `.env`
5. Set `PATIENT_WHATSAPP=whatsapp:+20XXXXXXXXXX` to your number

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Chat UI |
| POST | `/chat` | Send message, extract entities, save & notify |
| GET | `/medications` | List all saved medications |
| DELETE | `/medications/{id}` | Delete a medication |
# medical_assistance
