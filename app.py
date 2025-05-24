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

# ─── State & Memory ───────────────────────────────────────────────────────
user_states = {}
def memory_path(phone): return f"memory/{phone}.json"

def load_user_memory(phone):
    try:
        return json.load(open(memory_path(phone)))
    except:
        return {}

def save_user_memory(phone, data):
    os.makedirs("memory", exist_ok=True)
    with open(memory_path(phone), "w") as f:
        json.dump(data, f)

# ─── Media Helpers ───────────────────────────────────────────────────────
def get_media_url(media_id):
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers, params={"fields":"url"})
    r.raise_for_status()
    return r.json()["url"]

def download_media(url, dest):
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(r.content)
    return dest

# ─── Parsers ──────────────────────────────────────────────────────────────
def parse_pdf(path):
    texts = []
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            texts.append(p.extract_text() or "")
    return "\n".join(texts)

def parse_image(path):
    return pytesseract.image_to_string(Image.open(path))

# ─── WhatsApp Senders ────────────────────────────────────────────────────
def send_text(phone, text):
    payload = {
        "messaging_product":"whatsapp","to":phone,
        "type":"text","text":{"body":text}
    }
    headers = {"Authorization":f"Bearer {ACCESS_TOKEN}",
               "Content-Type":"application/json"}
    r = requests.post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        headers=headers, json=payload
    )
    print("→ text", r.status_code, r.text)

def send_buttons(phone, text, options=None):
    """If options is None, fall back to Understood/Explain more."""
    if not options:
        buttons = [
            {"type":"reply","reply":{"id":"understood","title":"Understood"}},
            {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
        ]
    else:
        buttons = []
        for opt in options[:3]:
            buttons.append({
                "type":"reply",
                "reply":{"id":opt.lower().replace(" ","_"),"title":opt}
            })
    payload = {
        "messaging_product":"whatsapp","to":phone,
        "type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text":text[:1024]},
            "action":{"buttons":buttons}
        }
    }
    headers = {"Authorization":f"Bearer {ACCESS_TOKEN}",
               "Content-Type":"application/json"}
    r = requests.post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        headers=headers, json=payload
    )
    print("→ buttons", r.status_code, r.text)

# ─── Gemini Query ────────────────────────────────────────────────────────
def get_gemini_reply(prompt, name):
    system = f"""
You are StudyMate AI, a warm, enthusiastic tutor (founded by ByteWave Media; mention only if asked).
Always address the student by name ({name}).
• Never start with "Hi"—dive straight in.
• If the question is vague, ask: "Could you clarify what specific aspect you’d like to focus on?"
• Provide concise, step-by-step explanations."""
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        res = model.generate_content(system + "\n\nStudent: " + prompt)
        return res.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return ""

# ─── Subject Detection ────────────────────────────────────────────────────
BROAD_SUBJECTS = {
    "chemistry","physics","biology","english","grammar",
    "math","history","coding","programming","literature"
}

def handle_broad_subject(phone, subject, name):
    # Ask Gemini for 4 common subtopics
    prompt = f"List four common subtopics in {subject.title()} that students often ask about, comma-separated."
    resp = get_gemini_reply(prompt, name)
    topics = [t.strip().title() for t in resp.split(",") if t.strip()]
    text = f"Great—what specifically in {subject.title()} would you like to focus on? For example: {', '.join(topics[:3])}."
    send_buttons(phone, text, topics[:3])

# ─── Main Webhook ────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method=="GET":
        if (request.args.get("hub.mode")=="subscribe"
            and request.args.get("hub.verify_token")==VERIFY_TOKEN):
            return request.args.get("hub.challenge"),200
        return "Verification failed",403

    data = request.json
    changes = data["entry"][0]["changes"][0]["value"]
    if "messages" not in changes:
        return "OK",200

    msg   = changes["messages"][0]
    phone = msg["from"]
    mtype = msg.get("type")
    text  = msg.get("text",{}).get("body","").strip()
    memory = load_user_memory(phone)
    user_states.setdefault(phone, {})

    # 1) Onboard for Name
    if "name" not in memory:
        if len(text.split())>=2:
            memory["name"]=text; save_user_memory(phone,memory)
            send_text(phone, f"Nice to meet you, {text}! What topic should we tackle today?")
        else:
            send_text(phone, "Welcome! What’s your full name (first & last)?")
        return "OK",200

    name = memory["name"]

    # 2) Button Reply Handling
    payload = None
    if mtype=="interactive" and "button_reply" in msg.get("interactive",{}):
        payload = msg["interactive"]["button_reply"]["id"]
    if payload:
        last_q = user_states[phone].get("last_user_q","")
        if payload=="understood":
            send_text(phone, "Great! What shall we study next?")
        elif payload=="explain_more" and last_q:
            more = get_gemini_reply("Please explain further: "+ last_q, name)
            user_states[phone]["last_user_q"]=last_q
            send_buttons(phone, more)
        else:
            send_text(phone, "Could you remind me of your question?")
        return "OK",200

    # 3) File / Image Upload
    if mtype in ("document","image"):
        media = msg[mtype]; mid=media["id"]
        try:
            url = get_media_url(mid)
            loc = download_media(url, f"uploads/{mid}")
            parsed = parse_pdf(loc) if mtype=="document" else parse_image(loc)
            user_states[phone]["last_file_text"]=parsed
            send_text(phone, f"Got your {mtype}! Reply *summarize* or *quiz* when ready.")
        except HTTPError as he:
            print("Media error:",he)
            send_text(phone, "Sorry, I couldn’t fetch that file—try again?")
        return "OK",200

    # 4) Summarize / Quiz Commands
    cmd = text.lower()
    if cmd=="summarize":
        parsed = user_states[phone].get("last_file_text","")
        if not parsed:
            send_text(phone, "No file in memory—please upload one first.")
        else:
            summ = get_gemini_reply("Summarize:\n"+parsed, name)
            user_states[phone]["last_user_q"]=summ
            send_buttons(phone, summ)
        return "OK",200

    if cmd=="quiz":
        parsed = user_states[phone].get("last_file_text","")
        if not parsed:
            send_text(phone, "No file in memory—please upload one first.")
        else:
            quiz = get_gemini_reply("Create a 5-question quiz on:\n"+parsed, name)
            user_states[phone]["last_user_q"]=quiz
            send_buttons(phone, quiz)
        return "OK",200

    # 5) Broad Subject Detection
    if text.lower() in BROAD_SUBJECTS:
        handle_broad_subject(phone, text.lower(), name)
        return "OK",200

    # 6) Pure Q&A
    if not text:
        send_text(phone, "I didn’t catch any text—what would you like to study?")
        return "OK",200

    user_states[phone]["last_user_q"] = text
    answer = get_gemini_reply(text, name)
    if not answer:
        send_text(phone, "Sorry, I’m having trouble—could you rephrase?")
    else:
        send_buttons(phone, answer)
    return "OK",200

if __name__=="__main__":
    app.run(host="0.0.0.0",port=10000)
