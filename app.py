import os
import json
import logging
from collections import deque
from datetime import datetime, timedelta

import requests
from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore

import google.generativeai as genai
from google.cloud import vision
from google.cloud import speech_v1p1beta1 as speech

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
logger = logging.getLogger("StudyMate")

# Load environment variables
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))

# Validate environment
if not all([VERIFY_TOKEN, ACCESS_TOKEN, PHONE_NUMBER_ID, GEMINI_API_KEY]):
    logger.error("Missing required environment variables")
    raise SystemExit("Missing required environment variables")

# Initialize Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Initialize Firebase
cred = credentials.Certificate(
    "/etc/secrets/studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
)
firebase_admin.initialize_app(cred)
db = firestore.client()

# Initialize Google Vision and Speech clients
vision_client = vision.ImageAnnotatorClient()
speech_client = speech.SpeechClient()

# Load system prompt from file
with open("studymate_prompt.txt", "r") as f:
    SYSTEM_PROMPT = f.read().strip()

# Create Flask app
app = Flask(__name__)
# Session storage: maps phone number to conversation history
sessions = {}  # phone -> {"history": deque(maxlen=5)}


# Helper: ensure session exists
def ensure_session(phone):
    return sessions.setdefault(phone, {"history": deque(maxlen=5)})


# Helper: send HTTP POST to WhatsApp API
def safe_post(url, payload):
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if r.status_code not in (200, 201):
            logger.error(f"WhatsApp API error {r.status_code}: {r.text}")
        return r
    except Exception:
        logger.exception("Failed WhatsApp send")
        return None


# Helper: send a text message
def send_text(phone, text):
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text},
    }
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    return safe_post(url, payload)


# Helper: send interactive buttons
def send_buttons(phone):
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Did that make sense to you?"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "understood", "title": "Understood"}},
                    {"type": "reply", "reply": {"id": "explain_more", "title": "Explain more"}},
                ]
            },
        },
    }
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    return safe_post(url, payload)


# Helper: strip code fences and JSON header
def strip_fences_and_header(text):
    t = text.strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 3:
            t = parts[1]
    lines = t.splitlines()
    if lines and lines[0].strip().lower() == "json":
        lines = lines[1:]
    return "\n".join(lines).strip()


# Helper: call Gemini with prompt
def get_gemini(prompt):
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception:
        logger.exception("Gemini API error")
        return json.dumps({
            "type": "clarification",
            "content": "Sorry, I encountered an error. Please try again.",
        })


# Helper: analyze image with Google Vision OCR
def analyze_image_with_vision(image_bytes):
    image = vision.Image(content=image_bytes)
    response = vision_client.text_detection(image=image)
    texts = response.text_annotations
    if texts:
        return texts[0].description.strip()
    return ""


# Helper: get media URL from WhatsApp
def get_whatsapp_media_url(media_id):
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()
    return data.get("url")


# Helper: download binary media
def download_media(url):
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.content


# Helper: transcribe audio (original logic)
def transcribe_audio_with_speech(audio_bytes):
    try:
        audio = speech.RecognitionAudio(content=audio_bytes)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.ENCODING_UNSPECIFIED,
            language_code="en-US",
            audio_channel_count=1,
        )
        response = speech_client.recognize(config=config, audio=audio)
        transcript = "".join(
            result.alternatives[0].transcript for result in response.results
        )
        return transcript.strip()
    except Exception as e:
        logger.error(f"Speech recognition error: {e}")
        return None


# Helper: load or create user in Firestore
def get_or_create_user(phone):
    ref = db.collection("users").document(phone)
    doc = ref.get()
    if not doc.exists:
        user = {
            "phone": phone,
            "name": None,
            "account_type": "free",
            "credit_remaining": 20,
            "credit_reset": datetime.utcnow() + timedelta(days=1),
            "last_prompt": None,
        }
        ref.set(user)
        return user
    return doc.to_dict()


# Helper: update user fields
def update_user(phone, **fields):
    db.collection("users").document(phone).update(fields)
    logger.info(f"Updated user {phone} with {fields}")


# Helper: build system prompt for Gemini
def build_prompt(user, history, message, first_name):
    parts = [SYSTEM_PROMPT]
    if first_name:
        parts.append(f'User name: "{first_name}"')
    if history:
        parts.append("Recent messages:")
        parts.extend(f"- {h}" for h in history)
    parts.append(f'Current message: "{message}"')
    parts.append("JSON:")
    return "\n".join(parts)


# Flask endpoint for WhatsApp webhook
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    # POST: handle incoming message
    data = request.json or {}
    entry = data.get("entry", [])
    if not entry or not entry[0].get("changes"):
        return "OK", 200

    msg = entry[0]["changes"][0]["value"].get("messages", [{}])[0]
    phone = msg.get("from")
    if not phone:
        return "OK", 200

    # Load or init user
    user = get_or_create_user(phone)
    session = ensure_session(phone)
    history = list(session["history"])
    now = datetime.utcnow()
    first_name = user.get("name", "").split()[0] if user.get("name") else ""

    # --- Onboarding: collect full name ---
    if user.get("name") is None:
        text_body = msg.get("text", {}).get("body", "").strip()
        if text_body and len(text_body.split()) >= 2:
            first = text_body.split()[0]
            update_user(phone, name=text_body)
            send_text(phone, f"What would you like to study today, {first}?")
        else:
            send_text(phone, "Please share your full name (first and last).")
        return "OK", 200

    # --- Free account credit handling ---
    if user.get("account_type") == "free":
        reset_time = user.get("credit_reset")
        if hasattr(reset_time, "to_datetime"):
            reset_time = reset_time.to_datetime()
        if isinstance(reset_time, datetime) and reset_time.tzinfo:
            reset_time = reset_time.replace(tzinfo=None)
        if now >= reset_time:
            update_user(phone, credit_remaining=20, credit_reset=now + timedelta(days=1))
            user["credit_remaining"] = 20
        if user.get("credit_remaining", 0) <= 0:
            send_text(phone, "Free limit reached (20/day). Upgrade for unlimited usage.")
            return "OK", 200
        update_user(phone, credit_remaining=user.get("credit_remaining") - 1)

    # --- Interactive button replies ---
    if msg.get("type") == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            bid = ir["button_reply"]["id"]
            if bid == "understood":
                send_text(phone, "Great—what’s next?")
            elif bid == "explain_more" and user.get("last_prompt"):
                more = get_gemini(user.get("last_prompt") + "\n\nPlease explain in more detail.")
                refined = strip_fences_and_header(more)
                send_text(phone, refined)
                send_buttons(phone)
        return "OK", 200

    # --- Handle different message types ---
    if msg.get("type") == "text":
        text_body = msg["text"]["body"].strip()
        gemini_input = text_body

    elif msg.get("type") == "image":
        media_id = msg["image"]["id"]
        try:
            media_url = get_whatsapp_media_url(media_id)
            image_bytes = download_media(media_url)
            extracted = analyze_image_with_vision(image_bytes)
            gemini_input = extracted or "I received an image but couldn't extract text. Please describe it."
        except Exception as e:
            logger.error(f"Image processing error: {e}")
            gemini_input = "Sorry, I had trouble processing your image. Please try again."

    elif msg.get("type") == "audio":
        media_id = msg["audio"]["id"]
        try:
            media_url = get_whatsapp_media_url(media_id)
            audio_bytes = download_media(media_url)
            transcript = transcribe_audio_with_speech(audio_bytes)
            if transcript:
                gemini_input = transcript
            else:
                gemini_input = "Sorry, I couldn't understand the audio. Please try again."
        except Exception as e:
            logger.error(f"Audio processing error: {e}")
            send_text(phone, f"No worries, {first_name}! What can I help you with next?")
            return "OK", 200

    else:
        # Unsupported message type
        return "OK", 200

    # Append user input to session history
    session["history"].append(gemini_input)

    # Build Gemini prompt
    prompt = build_prompt(user, history, gemini_input, first_name)

    # Call Gemini
    raw_response = get_gemini(prompt)
    logger.info(f"Gemini raw response:\n{raw_response}")
    cleaned = strip_fences_and_header(raw_response)

    # Try JSON parse
    try:
        parsed = json.loads(cleaned)
        rtype = parsed.get("type", "answer")
        content = parsed.get("content", "")
    except Exception:
        rtype = "answer"
        content = cleaned

    # Normalize newlines
    if isinstance(content, str):
        content = content.replace("\\n", "\n").replace("/n/", "\n")

    # Send reply content
    send_text(phone, content)

    # Send interactive buttons after academic answers
    academic_keys = ["step-by-step", "essay", "project", "exam", "solution", "problem", "question"]
    if rtype == "answer" and any(k in content.lower() for k in academic_keys):
        send_buttons(phone)

    # Update last prompt for explain_more
    update_user(phone, last_prompt=prompt)

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
