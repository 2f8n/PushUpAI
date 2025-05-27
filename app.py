import os
import requests
from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud import vision
from google.oauth2 import service_account
import google.generativeai as genai
import logging
from datetime import datetime, timezone

app = Flask(__name__)

# --- Setup secrets and environment variables ---
SECRET_JSON_PATH = "/etc/secrets/studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SECRET_JSON_PATH

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_API_URL = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
WHATSAPP_MEDIA_URL = "https://graph.facebook.com/v19.0/"

GENAI_API_KEY = os.getenv("GENAI_API_KEY")

# --- Initialize Firebase Admin ---
cred = credentials.Certificate(SECRET_JSON_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()

# --- Initialize Google Vision Client ---
vision_creds = service_account.Credentials.from_service_account_file(SECRET_JSON_PATH)
vision_client = vision.ImageAnnotatorClient(credentials=vision_creds)

# --- Configure Gemini API ---
genai.api_key = GENAI_API_KEY

logging.basicConfig(level=logging.INFO)

# --- Helper functions ---

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


def send_whatsapp_buttons(to, message, buttons):
    """
    Send WhatsApp message with buttons.
    buttons: list of dicts [{type: 'reply', reply: {id: 'id1', title: 'Yes'}}]
    """
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": message},
            "action": {"buttons": buttons}
        }
    }
    resp = requests.post(WHATSAPP_API_URL, headers=headers, json=data)
    if resp.status_code != 200:
        logging.error(f"WhatsApp send buttons failed: {resp.status_code} {resp.text}")
    return resp.json()


def fetch_whatsapp_media(media_id):
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


def get_user_doc(from_number):
    return db.collection("users").document(from_number)


def get_user_credits(from_number):
    doc_ref = get_user_doc(from_number)
    doc = doc_ref.get()
    if doc.exists:
        data = doc.to_dict()
        return data.get("credits", 20)
    else:
        doc_ref.set({"credits": 20, "created_at": datetime.now(timezone.utc)})
        return 20


def update_user_credits(from_number, new_credits):
    doc_ref = get_user_doc(from_number)
    doc_ref.update({"credits": new_credits})


def get_last_messages(from_number, limit=5):
    messages_ref = db.collection("messages")
    query = (
        messages_ref.where("from", "==", from_number)
        .order_by("timestamp", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    docs = query.stream()
    messages = []
    for doc in docs:
        data = doc.to_dict()
        text = ""
        if "text" in data["message"]:
            text = data["message"]["text"].get("body", "")
        elif "image" in data["message"]:
            text = "[Image]"
        if text:
            messages.append({"author": "user", "content": text})
    return list(reversed(messages))


def add_message_to_history(from_number, message):
    doc_ref = db.collection("messages").document()
    doc_ref.set({
        "from": from_number,
        "message": message,
        "timestamp": firestore.SERVER_TIMESTAMP
    })


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
        credits = get_user_credits(from_number)
        if credits <= 0:
            send_whatsapp_message(from_number, "Sorry, you have no remaining credits. Please contact support to recharge.")
            return jsonify({}), 200

        # Handle reply button inputs for feedback
        if "button" in message:
            button_id = message["button"].get("payload", "")
            if button_id == "did_make_sense":
                send_whatsapp_message(from_number, "Great! Feel free to ask me anything else.")
            elif button_id == "did_not_make_sense":
                send_whatsapp_message(from_number, "Sorry about that. Please rephrase your question or ask something else.")
            else:
                send_whatsapp_message(from_number, "Thanks for your feedback!")
            return jsonify({}), 200

        if "text" in message:
            user_text = message["text"]["body"]

            context_messages = get_last_messages(from_number)
            context_messages.append({"author": "user", "content": user_text})

            response = genai.chat.completions.create(
                model="models/chat-bison-001",
                messages=context_messages
            )
            answer = response.choices[0].message.content

            send_whatsapp_message(from_number, answer)

            # Send buttons for feedback
            buttons = [
                {"type": "reply", "reply": {"id": "did_make_sense", "title": "Yes"}},
                {"type": "reply", "reply": {"id": "did_not_make_sense", "title": "No"}}
            ]
            send_whatsapp_buttons(from_number, "Did that make sense?", buttons)

            update_user_credits(from_number, credits - 1)

        elif "image" in message:
            media_id = message["image"]["id"]
            image_bytes = fetch_whatsapp_media(media_id)

            if image_bytes:
                labels = analyze_image(image_bytes)
                send_whatsapp_message(from_number, f"I see: {labels}")
            else:
                send_whatsapp_message(from_number, "Sorry, I could not fetch or analyze your image.")

            update_user_credits(from_number, credits - 1)

        else:
            send_whatsapp_message(from_number, "Sorry, I can only process text and images.")

        add_message_to_history(from_number, message)

    except Exception as e:
        logging.error(f"Error handling message: {e}")
        send_whatsapp_message(from_number, "Sorry, an error occurred processing your message.")

    return jsonify({}), 200


@app.route("/")
def index():
    return "StudyMate AI is running."


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
