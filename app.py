import os
import io
import json
import tempfile
import requests
from flask import Flask, request
from google.cloud import vision_v1
from google.oauth2 import service_account
import PyPDF2
import google.generativeai as genai
import firebase_admin
from firebase_admin import firestore

# ─── FLASK / CONFIG ────────────────────────────────────────────────────────────
app = Flask(__name__)

ACCESS_TOKEN     = os.environ["ACCESS_TOKEN"]
PHONE_NUMBER_ID  = os.environ["PHONE_NUMBER_ID"]
VERIFY_TOKEN     = os.environ["VERIFY_TOKEN"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]

# ─── FIRESTORE ──────────────────────────────────────────────────────────────────
firebase_admin.initialize_app()
db = firestore.Client()

# ─── VISION OCR CLIENT ─────────────────────────────────────────────────────────
VISION_KEY_PATH = "/etc/secrets/studymate-ai-credentials.json"
vision_creds    = service_account.Credentials.from_service_account_file(VISION_KEY_PATH)
vision_client   = vision_v1.ImageAnnotatorClient(credentials=vision_creds)

# ─── GEMINI CONFIG ─────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)

# ─── HELPERS ───────────────────────────────────────────────────────────────────

def send_whatsapp_message(to: str, text: str):
    """Sends a plain text message via WhatsApp API."""
    url = f"https://graph.facebook.com/v15.0/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    requests.post(url, json=payload, headers=headers)

def send_interactive_buttons(to: str):
    """Sends the standard 'Did that make sense?' buttons."""
    url = f"https://graph.facebook.com/v15.0/{PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
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
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    requests.post(url, json=payload, headers=headers)

def download_media(media_id: str) -> bytes:
    """Fetches binary content from WhatsApp Media API."""
    # step 1: fetch the temporary URL
    url1 = f"https://graph.facebook.com/v15.0/{media_id}"
    r1  = requests.get(url1, params={"access_token": ACCESS_TOKEN})
    r1.raise_for_status()
    download_url = r1.json().get("url")
    # step 2: download the content
    r2 = requests.get(download_url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
    r2.raise_for_status()
    return r2.content

# ─── WEBHOOK ────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # ─ verification handshake ──────────────────────────────────────────────────
    if request.method == "GET":
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if token == VERIFY_TOKEN:
            return challenge, 200
        return "Invalid verify token", 403

    # ─ incoming message ─────────────────────────────────────────────────────────
    payload = request.json
    try:
        entry = payload["entry"][0]["changes"][0]["value"]
        messages = entry.get("messages")
        if not messages:
            return "OK", 200
        msg = messages[0]
        phone = msg["from"]
    except Exception:
        return "OK", 200

    # ─ IMAGE OCR ────────────────────────────────────────────────────────────────
    if msg.get("type") == "image":
        try:
            media_id = msg["image"]["id"]
            img_bytes = download_media(media_id)
            vision_img = vision_v1.Image(content=img_bytes)
            ocr_resp = vision_client.text_detection(image=vision_img)
            texts = [t.description for t in ocr_resp.text_annotations]
            extracted = texts[0] if texts else "(no text found)"
            send_whatsapp_message(phone, f"User image text:\n{extracted}")
        except Exception:
            send_whatsapp_message(phone, "Sorry, I couldn’t process that image. Please try again.")
        return "OK", 200

    # ─ PDF / DOCUMENT TEXT ───────────────────────────────────────────────────────
    if msg.get("type") == "document":
        try:
            media_id = msg["document"]["id"]
            file_bytes = download_media(media_id)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(file_bytes)
                tmp.flush()
                try:
                    reader = PyPDF2.PdfReader(tmp.name)
                    pages = [p.extract_text() or "" for p in reader.pages]
                    full_text = "\n\n".join(pages).strip()
                    snippet = full_text[:500] + ("…" if len(full_text) > 500 else "")
                    send_whatsapp_message(phone, f"PDF preview:\n{snippet}")
                except PyPDF2.errors.PdfReadError:
                    send_whatsapp_message(phone, "Couldn’t read that PDF. It may be corrupted.")
        except Exception:
            send_whatsapp_message(phone, "Sorry, I couldn’t download or read that document.")
        return "OK", 200

    # ─ ACADEMIC Q&A ──────────────────────────────────────────────────────────────
    # load / update user in Firestore, track last 5 messages…
    user_ref = db.collection("users").document(phone)
    user_doc = user_ref.get()
    if not user_doc.exists or not user_doc.get("first_name"):
        # ask for their name
        user_ref.set({"last_messages": [], **(user_doc.to_dict() or {})}, merge=True)
        send_whatsapp_message(phone, "Please share your full name (first and last).")
        return "OK", 200

    # append this message to last_messages (keep only 5)
    last = user_doc.get("last_messages", [])
    last.append(msg.get("text", {}).get("body", ""))
    last = last[-5:]
    user_ref.update({"last_messages": last})

    # prepare Gemini prompt
    gemini_prompt = (
        f'You are StudyMate AI, a warm, curious academic tutor.\n'
        f'Use the student’s first name "{user_doc.get("first_name")}".\n'
        f'Keep steps ≤3 sentences each, include analogies/pitfalls/resources when helpful.\n'
        f'If there’s enough detail, answer fully; else ask exactly one clarifying question.\n'
        f'Last 5 messages: {last}\n'
        f'Current message: "{msg.get("text", {}).get("body", "")}"\n'
        f'Always reply in JSON only:{{"type":"clarification"|"answer","content":"…"}}\n'
    )

    try:
        gen_resp = genai.chat.create(model="gemini-pro", prompt=gemini_prompt)
        reply = gen_resp.last.response
        # send the JSON blob as text
        send_whatsapp_message(phone, reply)
        # after an "answer", add buttons
        data = json.loads(reply)
        if data.get("type") == "answer":
            send_interactive_buttons(phone)
    except Exception:
        send_whatsapp_message(phone, "Oops, something went wrong. Please try again.")
    return "OK", 200

# ─── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
