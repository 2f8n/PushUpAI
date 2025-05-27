import os
import base64
import json
import tempfile
import requests
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from google.cloud import firestore, vision
import google.generativeai as genai
import PyPDF2

# ——— Configuration ———
WABA_TOKEN       = os.environ["WHATSAPP_TOKEN"]
WABA_PHONE_ID    = os.environ["WHATSAPP_PHONE_ID"]
GENAI_API_KEY    = os.environ["GENAI_API_KEY"]
PROJECT_ID       = os.environ.get("GCP_PROJECT")  # optional

# decode service account JSON from base64
creds = None
sa_json_b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
if sa_json_b64:
    info = json.loads(base64.b64decode(sa_json_b64))
    creds = service_account.Credentials.from_service_account_info(info)
    PROJECT_ID = PROJECT_ID or info.get("project_id")

# Firestore client
db = firestore.Client(credentials=creds, project=PROJECT_ID) if creds or PROJECT_ID else firestore.Client()

# Vision client
vision_client = vision.ImageAnnotatorClient(credentials=creds) if creds else vision.ImageAnnotatorClient()

# Generative AI client
genai.configure(api_key=GENAI_API_KEY)

app = Flask(__name__)

def fetch_whatsapp_media(media_id):
    url = f"https://graph.facebook.com/v17.0/{media_id}"
    params = {"access_token": WABA_TOKEN, "fields": "url"}
    r = requests.get(url, params=params)
    r.raise_for_status()
    return r.json()["url"]

def download_to_temp(url):
    r = requests.get(url)
    r.raise_for_status()
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "wb") as f:
        f.write(r.content)
    return path

def extract_text_from_image(path):
    with open(path, "rb") as img:
        resp = vision_client.text_detection({"content": img.read()})
    return resp.text_annotations[0].description if resp.text_annotations else ""

def extract_text_from_pdf(path):
    try:
        reader = PyPDF2.PdfReader(path)
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception as e:
        return f"[PDF error: {e}]"

def get_user_doc(phone):
    return db.collection("users").document(phone)

def append_message(phone, text):
    doc = get_user_doc(phone)
    doc.set({"msgs": firestore.ArrayUnion([text])}, merge=True)
    # trim to last 5
    msgs = doc.get().to_dict().get("msgs", [])[-5:]
    doc.update({"msgs": msgs})

def build_genai_prompt(name, recent_msgs, incoming):
    base = (
        f"You are StudyMate AI, an academic tutor on WhatsApp.\n"
        f"Student: {name}\n"
        f"Context (last 5 msgs):\n" + "\n".join(f"- {m}" for m in recent_msgs) + "\n"
        f"New msg: {incoming}\n"
        "Respond JSON only: {\"type\":\"clarification\"|\"answer\",\"content\":\"...\"}"
    )
    return base

def send_whatsapp_reply(phone, payload):
    url = f"https://graph.facebook.com/v17.0/{WABA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WABA_TOKEN}", "Content-Type": "application/json"}
    body = {
        "messaging_product": "whatsapp",
        "to": phone,
        "text": {"body": json.dumps(payload)}
    }
    r = requests.post(url, headers=headers, json=body)
    r.raise_for_status()

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    entry = data["entry"][0]["changes"][0]["value"]
    msg = entry["messages"][0]
    phone = msg["from"]
    text = msg.get("text", {}).get("body", "")
    media_id = msg.get("image", {}).get("id") or msg.get("document", {}).get("id")

    # 1) Handle media
    if media_id:
        url = fetch_whatsapp_media(media_id)
        path = download_to_temp(url)
        if msg.get("image"):
            text = extract_text_from_image(path)
        else:
            text = extract_text_from_pdf(path)

    # 2) Get or ask for name
    user_doc = get_user_doc(phone).get().to_dict() or {}
    name = user_doc.get("name")
    if not name:
        append_message(phone, text)
        send_whatsapp_reply(phone, {"type":"clarification","content":"Please share your full name (first & last)."})
        return jsonify(success=True)

    # 3) Save message & build prompt
    append_message(phone, text)
    recent = get_user_doc(phone).get().to_dict().get("msgs", [])
    prompt = build_genai_prompt(name, recent, text)

    # 4) Call Gemini
    resp = genai.chat.create(
        model="models/chat-bison-001",
        temperature=0.2,
        candidate_count=1,
        prompt=genai.Prompt.from_dict({"messages":[{"author":"user","content":prompt}]})
    )
    reply = resp.candidates[0].content

    # 5) Send JSON reply back
    payload = json.loads(reply) if reply.strip().startswith("{") else {"type":"answer","content":reply}
    send_whatsapp_reply(phone, payload)
    return jsonify(success=True)

# Verification endpoint (optional)
@app.route("/webhook", methods=["GET"])
def verify():
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if token == os.environ.get("VERIFY_TOKEN"):
        return challenge
    return "Invalid verify token", 403

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 8080)))
