# File: app.py

from flask import Flask, request
import os, json, random, requests, pdfplumber
from PIL import Image
import google.generativeai as genai
from pytesseract import pytesseract, TesseractNotFoundError

app = Flask(__name__)

# ─── Configuration ────────────────────────────────────────────────────────
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "your_verify_token")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME      = "gemini-1.5-pro-002"  # adjust per your account

genai.configure(api_key=GEMINI_API_KEY)

# ─── Memory Helpers ──────────────────────────────────────────────────────
def mem_path(phone): return f"memory/{phone}.json"
def load_mem(phone):
    try:
        return json.load(open(mem_path(phone)))
    except FileNotFoundError:
        return {}
def save_mem(phone, data):
    os.makedirs("memory", exist_ok=True)
    json.dump(data, open(mem_path(phone),"w"))

user_states = {}  # ephemeral per‐conversation state

# ─── WhatsApp Send Utilities ─────────────────────────────────────────────
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

def send_buttons(phone, text, options=None):
    # options=None ⇒ default Understood/Explain more
    if options is None:
        buttons = [
            {"type":"reply","reply":{"id":"understood","title":"Understood"}},
            {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
        ]
    else:
        # single “Explain more” button only
        buttons = [
            {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
        ]
    payload = {
        "messaging_product":"whatsapp","to":phone,
        "type":"interactive",
        "interactive":{
            "type":"button",
            "body":{"text":text[:1024]},
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

# ─── Media Download & Parsing ─────────────────────────────────────────────
def download_media(media_id, filename):
    # 1) fetch temporary URL
    resp = requests.get(
        f"https://graph.facebook.com/v19.0/{media_id}",
        params={"fields":"url"},
        headers={"Authorization":f"Bearer {ACCESS_TOKEN}"}
    ).json()
    url = resp.get("url")
    if not url:
        raise Exception("No media URL")
    # 2) download binary
    data = requests.get(url).content
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    open(filename,"wb").write(data)
    return filename

def parse_pdf(path):
    text = []
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            text.append(p.extract_text() or "")
    return "\n".join(text)

def parse_image(path):
    # requires tesseract‐ocr installed on the server
    return pytesseract.image_to_string(Image.open(path))

# ─── Gemini Chat ─────────────────────────────────────────────────────────
def get_reply(prompt, name):
    system = f"""
You are StudyMate AI. Address the student by name ({name}) and dive straight in—no “Hi.”  
If their question is ambiguous, ask a clarifying question instead."""
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        r = model.generate_content(system + "\n\nStudent: " + prompt)
        return r.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return ""

# ─── Webhook Handler ─────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        if (request.args.get("hub.mode")=="subscribe"
            and request.args.get("hub.verify_token")==VERIFY_TOKEN):
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    data = request.json
    val  = data["entry"][0]["changes"][0]["value"]
    if "messages" not in val:
        return "OK", 200

    msg   = val["messages"][0]
    phone = msg["from"]
    mtype = msg.get("type")
    text  = msg.get("text",{}).get("body","").strip()

    # load or init memory + ephemeral state
    mem = load_mem(phone)
    user_states.setdefault(phone, {})

    # ─── 1) Onboarding / Name ────────────────────────────────────────────
    if "name" not in mem:
        # if they've sent two+ words, treat it as full name
        if len(text.split()) >= 2:
            mem["name"] = text
            save_mem(phone, mem)
            send_text(phone, f"Awesome, {text}! What topic would you like to study first?")
        else:
            variants = [
                "Hey there! What’s your full name so I can save you in my contacts?",
                "Hi! I’d love to call you by name—what’s your full name for my contact list?",
                "Hello! Please share your full name so I can add you to my contacts.",
                "Nice to meet you! Drop your full name so I can save it in my contacts.",
                "Welcome! Mind telling me your full name so I can save you in my contacts?"
            ]
            send_text(phone, random.choice(variants))
        return "OK", 200

    name = mem["name"]

    # ─── 2) PDF Upload ───────────────────────────────────────────────────
    if mtype == "document":
        doc_id = msg["document"]["id"]
        try:
            path = download_media(doc_id, f"uploads/{doc_id}.pdf")
            parsed = parse_pdf(path)
            user_states[phone]["last_file_text"] = parsed
            send_text(phone,
                "PDF received and text extracted! Reply *summarize* or *quiz* when you’re ready.")
        except Exception as e:
            print("PDF error:", e)
            send_text(phone, "Couldn’t fetch or parse that PDF—please try again.")
        return "OK", 200

    # ─── 3) IMAGE Upload ─────────────────────────────────────────────────
    if mtype == "image":
        img_id = msg["image"]["id"]
        try:
            path = download_media(img_id, f"uploads/{img_id}.jpg")
            extracted = parse_image(path)
            user_states[phone]["last_file_text"] = extracted
            send_text(phone,
                "Image received and text extracted! Reply *summarize* or *quiz* when you’re ready.")
        except TesseractNotFoundError:
            send_text(phone,
                "⚠️ OCR isn’t available on this server. Please upload a PDF, or install tesseract-ocr.")
        except Exception as e:
            print("IMG error:", e)
            send_text(phone, "Couldn’t process that image—please try again.")
        return "OK", 200

    # ─── 4) Summarize / Quiz Commands ─────────────────────────────────────
    cmd = text.lower()
    if cmd in ("summarize", "quiz"):
        parsed = user_states[phone].get("last_file_text","")
        if not parsed:
            send_text(phone, "No file/text loaded—please upload a PDF or image first.")
        else:
            prompt = ("Create a 5-question quiz on:\n" if cmd=="quiz"
                      else "Summarize:\n") + parsed
            ans = get_reply(prompt, name)
            user_states[phone]["last_user_q"] = prompt
            send_buttons(phone, ans)  # default two-button
        return "OK", 200

    # ─── 5) Button Replies ────────────────────────────────────────────────
    if mtype=="interactive" and "button_reply" in msg.get("interactive",{}):
        bid = msg["interactive"]["button_reply"]["id"]
        if bid=="understood":
            send_text(phone, "Great! What would you like to do next?")
        elif bid=="explain_more":
            last = user_states[phone].get("last_user_q")
            if last:
                ans = get_reply("Please explain further: "+last, name)
                user_states[phone]["last_user_q"] = last
                send_buttons(phone, ans)
            else:
                send_text(phone, "Sure—what should I explain more?")
        else:
            send_text(phone, "Huh—didn’t catch that. Could you rephrase?")
        return "OK", 200

    # ─── 6) Broad‐Topic Detection ─────────────────────────────────────────
    subj = text.lower()
    BROAD = {"chemistry","physics","biology","english","grammar",
             "math","history","coding","programming","literature"}
    if subj in BROAD:
        # ask for specificity + examples from Gemini
        resp = get_reply(
            f"List the top 4 most asked subtopics in {subj.title()}, comma-separated.", name)
        tops = [t.strip().title() for t in resp.split(",") if t.strip()]
        examples = ", ".join(tops[:3])
        send_text(phone,
            f"Oh, great! What specifically in {subj.title()}? For example: {examples}")
        send_buttons(phone, "", options=[])  # only “Explain more”
        return "OK", 200

    # ─── 7) General Q&A Fallback ─────────────────────────────────────────
    if text:
        ans = get_reply(text, name)
        user_states[phone]["last_user_q"] = text
        if text.endswith("?"):
            send_buttons(phone, ans)  # two-button for genuine questions
        else:
            send_text(phone, ans or "Sorry, I’m not sure—could you clarify?")
    else:
        send_text(phone, "I didn’t catch that—what would you like to study?")
    return "OK", 200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=10000)
