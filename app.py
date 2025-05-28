import os
import json
import logging
import re
from collections import deque
from datetime import datetime, timedelta

import requests
from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai
from google.cloud import speech_v1p1beta1 as speech

# Setup logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger("StudyMate")

# Load .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Required env vars
REQUIRED_VARS = ("VERIFY_TOKEN", "ACCESS_TOKEN", "PHONE_NUMBER_ID", "GEMINI_API_KEY")
for v in REQUIRED_VARS:
    if not os.getenv(v):
        logger.error(f"Missing environment variable: {v}")
        raise SystemExit("Please set all required environment variables.")

VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
PORT            = int(os.getenv("PORT", 10000))

# Initialize Gemini AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Initialize Firebase with secret from Render
FIREBASE_SECRET_PATH = "/etc/secrets/studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
cred = credentials.Certificate(FIREBASE_SECRET_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()

# Initialize Google Cloud Speech client
speech_client = speech.SpeechClient()

# Load system prompt
with open("studymate_prompt.txt", "r") as f:
    SYSTEM_PROMPT = f.read().strip()

app = Flask(__name__)
sessions = {}  # phone -> {"history": deque(maxlen=5)}

def ensure_session(phone):
    if phone not in sessions:
        sessions[phone] = {"history": deque(maxlen=5)}
    return sessions[phone]

def safe_post(url, payload):
    try:
        r = requests.post(url,
                          headers={"Authorization": f"Bearer {ACCESS_TOKEN}",
                                   "Content-Type": "application/json"},
                          json=payload)
        if r.status_code not in (200, 201):
            logger.error(f"WhatsApp API error {r.status_code}: {r.text}")
    except Exception:
        logger.exception("Failed sending message via WhatsApp API")

def send_text(phone, text):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    })

def send_buttons(phone):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Did that make sense to you?"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "understood", "title": "Understood"}},
                    {"type": "reply", "reply": {"id": "explain_more", "title": "Explain more"}}
                ]
            }
        }
    })

def extract_json_content(response_text):
    """
    Extract JSON object from Gemini's markdown code block, then parse it and return the (type, content).
    Return (None, None) if fails.
    """
    try:
        # Remove markdown fences ``` or ```json
        clean_text = re.sub(r"^```json\s*|^```|```$", "", response_text.strip(), flags=re.MULTILINE).strip()
        logger.debug(f"Cleaned Gemini response for JSON parse:\n{clean_text}")

        data = json.loads(clean_text)

        if isinstance(data, dict) and "content" in data:
            return data.get("type", "answer"), data["content"]
        else:
            logger.warning("Parsed JSON missing 'content' or not a dict")
            return None, None
    except Exception as e:
        logger.warning(f"Failed to parse Gemini JSON: {e}")
        return None, None

def get_gemini_response(prompt):
    try:
        resp = model.generate_content(prompt)
        logger.info(f"Gemini raw response:\n{resp.text}")
        return resp.text.strip()
    except Exception:
        logger.exception("Gemini call error")
        fallback = json.dumps({"type": "clarification", "content": "Sorry, I encountered an error. Please try again."})
        return fallback

def transcribe_audio_from_url(media_url):
    """
    Download media from WhatsApp, send to Google Cloud Speech-to-Text, return transcript text.
    """
    try:
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        r = requests.get(media_url, headers=headers)
        r.raise_for_status()
        audio_bytes = r.content

        audio = speech.RecognitionAudio(content=audio_bytes)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
            sample_rate_hertz=16000,
            language_code="en-US",
            enable_automatic_punctuation=True,
        )

        response = speech_client.recognize(config=config, audio=audio)
        transcript = ""
        for result in response.results:
            transcript += result.alternatives[0].transcript + " "

        transcript = transcript.strip()
        logger.info(f"Transcribed audio: {transcript}")
        return transcript if transcript else None

    except Exception as e:
        logger.error(f"Speech recognition error: {e}")
        return None

def get_or_create_user(phone):
    ref = db.collection("users").document(phone)
    doc = ref.get()
    if not doc.exists:
        user_data = {
            "phone": phone,
            "name": None,
            "account_type": "free",
            "credit_remaining": 20,
            "credit_reset": datetime.utcnow() + timedelta(days=1),
            "last_prompt": None
        }
        ref.set(user_data)
        return user_data
    return doc.to_dict()

def update_user(phone, **kwargs):
    db.collection("users").document(phone).update(kwargs)
    logger.info(f"Updated user {phone} with {kwargs}")

def build_prompt(user, history, message):
    parts = [SYSTEM_PROMPT]
    if user.get("name"):
        first_name = user["name"].split()[0]
        parts.append(f'User name: "{first_name}"')
    if history:
        parts.append("Recent messages:")
        parts.extend(f"- {msg}" for msg in history)
    parts.append(f'Current message: "{message}"')
    parts.append("JSON:")
    return "\n".join(parts)

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    data = request.json or {}
    entry = data.get("entry", [])
    if not entry or not entry[0].get("changes"):
        return "OK", 200

    msg = entry[0]["changes"][0]["value"].get("messages", [{}])[0]
    phone = msg.get("from")

    if not phone:
        return "OK", 200

    # Check message type
    msg_type = msg.get("type", "")

    # Initialize text input for GPT prompt
    user_input_text = ""

    user = get_or_create_user(phone)
    now = datetime.utcnow()

    # Handle audio message
    if msg_type == "audio" or msg_type == "voice":
        media_id = msg.get(msg_type, {}).get("id")
        if media_id:
            # Get media URL from WhatsApp
            media_url_resp = requests.get(
                f"https://graph.facebook.com/v19.0/{media_id}",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
            )
            if media_url_resp.status_code == 200:
                media_url = media_url_resp.json().get("url")
                if media_url:
                    transcript = transcribe_audio_from_url(media_url)
                    if transcript:
                        user_input_text = transcript
                        logger.info(f"Using transcribed text from audio: {user_input_text}")
                    else:
                        send_text(phone, "Sorry, I couldn't understand the audio. Please try again.")
                        return "OK", 200
                else:
                    send_text(phone, "Sorry, I couldn't retrieve the audio file. Please try again.")
                    return "OK", 200
            else:
                logger.error(f"Failed to get media URL: {media_url_resp.status_code} {media_url_resp.text}")
                send_text(phone, "Sorry, there was an error retrieving your audio. Please try again.")
                return "OK", 200
        else:
            send_text(phone, "Sorry, no audio found in the message.")
            return "OK", 200

    # If text message, grab text
    if msg_type == "text":
        user_input_text = msg.get("text", {}).get("body", "").strip()

    # User onboarding for name
    if user["name"] is None:
        if len(user_input_text.split()) >= 2:
            first_name = user_input_text.split()[0]
            update_user(phone, name=user_input_text)
            send_text(phone, f"What would you like to study today, {first_name}?")
        else:
            send_text(phone, "Please share your full name (first and last).")
        return "OK", 200

    if not user_input_text:
        # No input text after all checks
        return "OK", 200

    # Free tier credit reset and check
    if user["account_type"] == "free":
        credit_reset_time = user["credit_reset"]
        if hasattr(credit_reset_time, "to_datetime"):
            credit_reset_time = credit_reset_time.to_datetime()
        if isinstance(credit_reset_time, datetime) and credit_reset_time.tzinfo:
            credit_reset_time = credit_reset_time.replace(tzinfo=None)
        if now >= credit_reset_time:
            update_user(phone, credit_remaining=20, credit_reset=now + timedelta(days=1))
            user["credit_remaining"] = 20

        if user["credit_remaining"] <= 0:
            send_text(phone, "You have reached your free usage limit (20 messages per day). Please consider upgrading.")
            return "OK", 200

        update_user(phone, credit_remaining=user["credit_remaining"] - 1)

    # Handle interactive button replies (if any)
    if msg_type == "interactive":
        ir = msg.get("interactive", {})
        if ir.get("type") == "button_reply":
            bid = ir["button_reply"]["id"]
            if bid == "understood":
                send_text(phone, "Great! What would you like to learn next?")
            elif bid == "explain_more" and user.get("last_prompt"):
                more_response_raw = get_gemini_response(user["last_prompt"] + "\n\nPlease explain in more detail.")
                mtype, mcontent = extract_json_content(more_response_raw)
                if not mcontent:
                    mcontent = more_response_raw
                send_text(phone, mcontent)
                send_buttons(phone)
        return "OK", 200

    # Normal text conversation processing
    sess = ensure_session(phone)
    history = list(sess["history"])
    sess["history"].append(user_input_text)

    prompt = build_prompt(user, history, user_input_text)
    raw_response = get_gemini_response(prompt)

    # Extract content only from JSON response Gemini gives
    rtype, content = extract_json_content(raw_response)

    # Fallback if parsing fails
    if content is None:
        rtype = "answer"
        content = raw_response

    # Ensure content is string
    if not isinstance(content, str):
        content = str(content)

    # Send only the content text back (no JSON or fences)
    send_text(phone, content)

    # Send buttons ONLY if type=="answer" (academic solving)
    if rtype == "answer":
        send_buttons(phone)

    update_user(phone, last_prompt=prompt)

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
