# app.py
import os, base64, json, tempfile, requests
from flask import Flask, request, jsonify
from google.oauth2 import service_account
from google.cloud import firestore, vision
import google.generativeai as genai
import PyPDF2

# ——— Required env-vars ———
WABA_TOKEN    = os.getenv("WHATSAPP_TOKEN")
WABA_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
GENAI_API_KEY = os.getenv("GENAI_API_KEY")

missing = [k for k,v in [
    ("WHATSAPP_TOKEN", WABA_TOKEN),
    ("WHATSAPP_PHONE_ID", WABA_PHONE_ID),
    ("GENAI_API_KEY", GENAI_API_KEY),
] if not v]
if missing:
    raise RuntimeError(f"Missing environment variable(s): {', '.join(missing)}")

# ——— GCP creds ———
PROJECT_ID    = os.getenv("GCP_PROJECT")  # optional fallback
sa_b64        = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

creds = None
if sa_b64:
    info  = json.loads(base64.b64decode(sa_b64))
    creds = service_account.Credentials.from_service_account_info(info)
    PROJECT_ID = PROJECT_ID or info.get("project_id")

# ——— Clients ———
db = (
    firestore.Client(credentials=creds, project=PROJECT_ID)
    if (creds or PROJECT_ID) else firestore.Client()
)
vision_client = (
    vision.ImageAnnotatorClient(credentials=creds)
    if creds else vision.ImageAnnotatorClient()
)
genai.configure(api_key=GENAI_API_KEY)

app = Flask(__name__)

# ——— Helpers ———
def fetch_media_url(mid):
    r = requests.get(
        f"https://graph.facebook.com/v17.0/{mid}",
        params={"access_token":WABA_TOKEN,"fields":"url"}
    ); r.raise_for_status()
    return r.json()["url"]

def download_temp(url):
    r = requests.get(url); r.raise_for_status()
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd,"wb") as f: f.write(r.content)
    return path

def ocr_image(path):
    with open(path,"rb") as img:
        res = vision_client.text_detection({"content": img.read()})
    anns = res.text_annotations
    return anns[0].description if anns else ""

def ocr_pdf(path):
    try:
        pdf = PyPDF2.PdfReader(path)
        return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        return f"[PDF error: {e}]"

def user_doc(phone):
    return db.collection("users").document(phone)

def append_msg(phone, txt):
    doc = user_doc(phone)
    doc.set({"msgs": firestore.ArrayUnion([txt])}, merge=True)
    msgs = doc.get().to_dict().get("msgs", [])[-5:]
    doc.update({"msgs": msgs})

def build_prompt(name, history, incoming):
    ctx = "\n".join(f"- {m}" for m in history)
    return (
        f"You are StudyMate AI, an academic tutor on WhatsApp.\n"
        f"Student: {name}\n"
        f"Last 5 messages:\n{ctx}\n"
        f"New: {incoming}\n"
        "Reply in JSON: {\"type\":\"clarification\"|\"answer\",\"content\":\"...\"}"
    )

def send_reply(to, payload):
    resp = requests.post(
        f"https://graph.facebook.com/v17.0/{WABA_PHONE_ID}/messages",
        headers={"Authorization":f"Bearer {WABA_TOKEN}"},
        json={
            "messaging_product":"whatsapp",
            "to": to,
            "text":{"body": json.dumps(payload)}
        }
    )
    resp.raise_for_status()

# ——— Webhook ———
@app.route("/webhook", methods=["POST"])
def webhook():
    data  = request.json
    msg   = data["entry"][0]["changes"][0]["value"]["messages"][0]
    phone = msg["from"]
    text  = msg.get("text",{}).get("body","")
    media = msg.get("image",{}).get("id") or msg.get("document",{}).get("id")

    # 1) If there’s media, OCR it
    if media:
        url  = fetch_media_url(media)
        path = download_temp(url)
        text = ocr_image(path) if msg.get("image") else ocr_pdf(path)

    # 2) Check for name
    user = user_doc(phone).get().to_dict() or {}
    name = user.get("name")
    if not name:
        append_msg(phone, text)
        send_reply(phone, {"type":"clarification","content":"Please send me your full name (first & last)."})
        return jsonify(success=True)

    # 3) Append & build prompt
    append_msg(phone, text)
    history = user_doc(phone).get().to_dict().get("msgs",[])
    prompt  = build_prompt(name, history, text)

    # 4) Gemini call
    resp = genai.chat.create(
        model="models/chat-bison-001",
        temperature=0.2,
        prompt=genai.Prompt.from_dict({"messages":[{"author":"user","content":prompt}]})
    )
    reply = resp.candidates[0].content
    payload = json.loads(reply) if reply.strip().startswith("{") else {"type":"answer","content":reply}

    # 5) Reply
    send_reply(phone, payload)
    return jsonify(success=True)

# ——— Verification ———
@app.route("/webhook", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token")==os.getenv("VERIFY_TOKEN"):
        return request.args.get("hub.challenge")
    return "Forbidden", 403

if __name__=="__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
