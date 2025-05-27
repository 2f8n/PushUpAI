 import os
import requests
from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import vision
from google.oauth2 import service_account
import google.generativeai as genai
from io import BytesIO
import logging

app = Flask(__name__)

# --- Setup secrets and environment variables ---
SECRET_JSON_PATH = "/etc/secrets/studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SECRET_JSON_PATH

# WhatsApp details - set these as environment variables on Render
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
WHATSAPP_MEDIA_URL = "https://graph.facebook.com/v19.0/"

# Gemini API key
GENAI_API_KEY = os.getenv("GENAI_API_KEY")

# --- Initialize Firebase Admin ---
cred = credentials.Certificate(SECRET_JSON_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- Initialize Google Vision Client ---
vision_creds = service_account.Credentials.from_service_account_file(SECRET_JSON_PATH)
vision_client = vision.ImageAnnotatorClient(credentials=vision_creds)

# --- Configure Gemini API ---
genai.configure(api_key=GENAI_API_KEY)

# Setup basic logging
logging.basicConfig(level=logging.INFO)

# --- Helper Functions ---

def send_whatsapp_message(to, message):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    resp = requests.post(WHATSAPP_API_URL, headers=headers, json=data)
    if resp.status_code != 200:
        logging.error(f"WhatsApp send message failed: {resp.status_code} {resp.text}")
    return resp.json()

def fetch_whatsapp_media(media_id):
    # Get media download URL from WhatsApp API
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    media_info_resp = requests.get(f"{WHATSAPP_MEDIA_URL}{media_id}", headers=headers)
    if media_info_resp.status_code != 200:
        logging.error(f"Failed to get media URL: {media_info_resp.text}")
        return None
    media_info = media_info_resp.json()
    media_url = media_info.get("url")
    if not media_url:
        logging.error("No media URL found in WhatsApp media info")
        return None
    # Download media content
    media_resp = requests.get(media_url, headers=headers)
    if media_resp.status_code != 200:
        logging.error(f"Failed to download media content: {media_resp.text}")
        return None
    return media_resp.content

def analyze_image(image_bytes):
    try:
        image = vision.Image(content=image_bytes)
        response = vision_client.label_detection(image=image)
        labels = response.label_annotations
        descriptions = [label.description for label in labels]
        return ", ".join(descriptions) if descriptions else "No labels detected."
    except Exception as e:
        logging.error(f"Google Vision API error: {e}")
        return "Sorry, I couldn't analyze the image."

# --- Flask route for webhook ---

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    logging.debug(f"Incoming webhook data: {data}")

    entry = data.get("entry", [{}])[0]
    changes = entry.get("changes", [{}])[0]
    value = changes.get("value", {})
    messages = value.get("messages", [])

    if not messages:
        return jsonify({}), 200

    message = messages[0]
    from_number = message.get("from")

    if not from_number:
        return jsonify({}), 200

    try:
        if "text" in message:
            user_text = message["text"]["body"]

            # Gemini AI chat response (no academic restriction)
            response = genai.chat.get_message(
                model="models/chat-bison-001",
                messages=[{"author": "user", "content": user_text}]
            )
            answer = response.text

            send_whatsapp_message(from_number, answer)

        elif "image" in message:
            media_id = message["image"]["id"]
            image_bytes = fetch_whatsapp_media(media_id)

            if image_bytes:
                labels = analyze_image(image_bytes)
                send_whatsapp_message(from_number, f"I see: {labels}")
            else:
                send_whatsapp_message(from_number, "Sorry, I could not fetch or analyze your image.")

        else:
            send_whatsapp_message(from_number, "Sorry, I can only process text and images.")

        # Save message + metadata to Firestore for logging/analytics
        doc_ref = db.collection("messages").document()
        doc_ref.set({
            "from": from_number,
            "message": message,
            "timestamp": firestore.SERVER_TIMESTAMP
        })

    except Exception as e:
        logging.error(f"Error handling message: {e}")

    return jsonify({}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
