# File: app.py

from flask import Flask, request
import os, json, requests
import pdfplumber
from PIL import Image
import pytesseract
import google.generativeai as genai
from requests.exceptions import HTTPError

app = Flask(__name__)

# ─── Configuration ────────────────────────────────────────────────────────
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "your_verify_token")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME      = "gemini-1.5-pro-002"  # adjust as needed

# ─── Initialize Gemini ───────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)

# ─── In-Memory State & Persistent Memory ─────────────────────────────────
user_states = {}  # ephemeral per-phone conversation context

def memory_path(phone): return f"memory/{phone}.json"

def load_user_memory(phone):
    try:
        return json.load(open(memory_path(phone)))
    except FileNotFoundError:
        return {}

def save_user_memory(phone, data):
    os.makedirs("memory", exist_ok=True)
    with open(memory_path(phone), "w") as f:
        json.dump(data, f)

# ─── Helpers: Fetch Media URL & Download ─────────────────────────────────
def get_media_url(media_id):
    """Fetch the expiring download URL for a document/image."""
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    resp = requests.get(url, headers=headers, params={"fields":"url"})
    resp.raise_for_status()
    return resp.json()["url"]

def download_media(url, dest):
    """Download the actual bytes of the PDF/image, using the same bearer token."""
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(r.content)
    return dest

# ─── Parsers ──────────────────────────────────────────────────────────────
def parse_pdf(path):
    text = []
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            text.append(p.extract_text() or "")
    return "\n".join(text)

def parse_image(path):
    img = Image.open(path)
    return pytesseract.image_to_string(img)

# ─── WhatsApp Senders ────────────────────────────────────────────────────
def send_text(phone, text):
    payload = {
        "messaging_product":"whatsapp","to":phone,
        "type":"text","text":{"body":text}
    }
    headers = {
        "Authorization":f"Bearer {ACCESS_TOKEN}",
        "Content-Type":"application/json"
    }
    r = requests.post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        headers=headers, json=payload
    )
    print("→ text", r.status_code, r.text)

def send_buttons(phone, text):
    """Quick-reply buttons: Understood / Explain more"""
    if not text:
        text = "Sorry—that response was empty. Can you rephrase?"
    buttons = [
        {"type":"reply","reply":{"id":"understood","title":"Understood"}},
        {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
    ]
    payload = {
        "messaging_product":"whatsapp","to":phone,
        "type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text": text[:1024]},  # max 1024 chars
            "action":{"buttons":buttons}
        }
    }
    headers = {
        "Authorization":f"Bearer {ACCESS_TOKEN}",
        "Content-Type":"application/json"
    }
    r = requests.post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        headers=headers, json=payload
    )
    print("→ buttons", r.status_code, r.text)

# ─── Gemini Query ────────────────────────────────────────────────────────
def get_gemini_reply(prompt, name):
    system_prompt = f"""
You are StudyMate AI, a warm, enthusiastic tutor (founded by ByteWave Media; mention only if asked).
Always address the student by name ({name}).  
• Never start with "Hi" or long greetings—dive straight in.  
• If the question is vague, ask for clarification: "Could you clarify what specific aspect you’d like to focus on?"  
• Give concise answers, step by step.  """
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        res = model.generate_content(system_prompt + "\n\nUser: " + prompt)
        return res.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return ""

# ─── Webhook Endpoint ────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        if (request.args.get("hub.mode")=="subscribe"
            and request.args.get("hub.verify_token")==VERIFY_TOKEN):
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    data = request.json
    changes = data["entry"][0]["changes"][0]["value"]
    if "messages" not in changes:
        return "OK", 200

    msg   = changes["messages"][0]
    phone = msg["from"]
    mtype = msg.get("type")
    text  = msg.get("text",{}).get("body","").strip()

    # Load or create memory + ephemeral state
    memory = load_user_memory(phone)
    user_states.setdefault(phone, {})

    # ─── 1) Onboard: ask for full name ─────────────────────────────────
    if "name" not in memory:
        if len(text.split()) >= 2:
            memory["name"] = text
            save_user_memory(phone, memory)
            send_text(phone, f"Nice to meet you, {memory['name']}! What topic should we tackle today?")
        else:
            send_text(phone, "Welcome! What’s your full name (first & last)?")
        return "OK", 200

    # ─── 2) Handle button replies ────────────────────────────────────────
    payload_id = None
    if mtype == "interactive" and "button_reply" in msg.get("interactive",{}):
        payload_id = msg["interactive"]["button_reply"]["id"]
    elif mtype == "button" and "payload" in msg.get("button",{}):
        payload_id = msg["button"]["payload"]

    if payload_id:
        last_q = user_states[phone].get("last_user_q","")
        if payload_id == "understood":
            send_text(phone, "Great! What shall we study next?")
        elif payload_id == "explain_more" and last_q:
            more = get_gemini_reply("Please explain in more detail: "+ last_q,
                                    memory["name"])
            user_states[phone]["last_user_q"] = last_q
            send_buttons(phone, more)
        else:
            send_text(phone, "Could you remind me your question?")
        return "OK", 200

    # ─── 3) Handle PDF / Image uploads ──────────────────────────────────
    if mtype in ("document","image"):
        media = msg[mtype]
        try:
            mid = media["id"]
            url = get_media_url(mid)
            local = f"uploads/{mid}"
            path = download_media(url, local)
            parsed = parse_pdf(path) if mtype=="document" else parse_image(path)
            user_states[phone]["last_file_text"] = parsed
            send_text(phone, f"I’ve read your {mtype}. Reply *summarize* or *quiz* when ready.")
        except HTTPError as he:
            print("Media download error:", he)
            send_text(phone, "Sorry, I couldn’t fetch that file—please try again.")
        except Exception as e:
            print("Parsing error:", e)
            send_text(phone, "Oops, something went wrong parsing your file.")
        return "OK", 200

    # ─── 4) Summarize / Quiz commands ───────────────────────────────────
    cmd = text.lower()
    if cmd == "summarize":
        parsed = user_states[phone].get("last_file_text","")
        if not parsed:
            send_text(phone, "No file on record—please upload a PDF or image first.")
        else:
            summ = get_gemini_reply("Summarize the following:\n\n"+parsed,
                                    memory["name"])
            user_states[phone]["last_user_q"] = "Summarize:\n"+parsed
            send_buttons(phone, summ)
        return "OK", 200

    if cmd == "quiz":
        parsed = user_states[phone].get("last_file_text","")
        if not parsed:
            send_text(phone, "No file on record—please upload a PDF or image first.")
        else:
            quiz = get_gemini_reply("Create a 5-question quiz on:\n\n"+parsed,
                                    memory["name"])
            user_states[phone]["last_user_q"] = "Quiz:\n"+parsed
            send_buttons(phone, quiz)
        return "OK", 200

    # ─── 5) Pure Q&A ────────────────────────────────────────────────────
    if not text:
        send_text(phone, "I didn’t catch any text—what would you like to study?")
        return "OK", 200

    # store and ask Gemini
    user_states[phone]["last_user_q"] = text
    answer = get_gemini_reply(text, memory["name"])
    if not answer:
        send_text(phone, "Sorry—I'm struggling right now. Could you try again?")
    else:
        send_buttons(phone, answer)

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
