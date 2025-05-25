# File: app.py

from flask import Flask, request
import os, json, random, requests, pdfplumber
from PIL import Image
import google.generativeai as genai
from pytesseract import pytesseract, TesseractNotFoundError

app = Flask(__name__)

# ─── Configuration ────────────────────────────────────────────────────────
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "pushupai_verify_token")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME      = "models/gemini-pro"        # update if yours is different

# Initialize Gemini
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

# ephemeral per-chat state
user_states = {}

# ─── WhatsApp Send Helpers ───────────────────────────────────────────────
def send_text(phone, text):
    payload = {
        "messaging_product":"whatsapp","to":phone,
        "type":"text","text":{"body":text}
    }
    headers = {"Authorization":f"Bearer {ACCESS_TOKEN}"}
    r = requests.post(
        f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        headers=headers, json=payload
    )
    print("→ text", r.status_code, r.text)

def send_buttons(phone, text, two_buttons=True):
    """
    two_buttons=True: ["Understood","Explain more"]
    two_buttons=False: a single ["Explain more"]
    """
    btns = []
    if two_buttons:
        btns = [
            {"type":"reply","reply":{"id":"understood","title":"Understood"}},
            {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
        ]
    else:
        btns = [
            {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
        ]

    payload = {
      "messaging_product":"whatsapp","to":phone,
      "type":"interactive",
      "interactive":{
        "type":"button",
        "body":{"text": text[:1024] or " "}
        ,
        "action":{"buttons":btns}
      }
    }
    headers = {"Authorization":f"Bearer {ACCESS_TOKEN}"}
    r = requests.post(
      f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
      headers=headers, json=payload
    )
    print("→ buttons", r.status_code, r.text)

# ─── Media Download & Parsing ─────────────────────────────────────────────
def download_media(media_id, filename):
    # 1) fetch URL
    meta = requests.get(
        f"https://graph.facebook.com/v19.0/{media_id}",
        params={"fields":"url"},
        headers={"Authorization":f"Bearer {ACCESS_TOKEN}"}
    ).json()
    url = meta.get("url")
    if not url:
        raise Exception("no media url")
    # 2) download
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
    return pytesseract.image_to_string(Image.open(path))

# ─── Gemini Wrapper ──────────────────────────────────────────────────────
def get_reply(prompt, first_name):
    system = f"""
You are StudyMate AI, an empathetic tutor.  
Use the student’s first name ({first_name}) only once, and never at the start of every message—start directly.  
Be concise unless detail is required.  
If a question is ambiguous, ask one clarifying question.  
If asked “Who are you?”, reply “I’m StudyMate AI by ByteWave Media.”"""
    try:
        r = genai.GenerativeModel(MODEL_NAME).generate_content(
            system + "\n\nStudent: " + prompt
        )
        return r.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return "Sorry, I’m having trouble right now—try again soon."

# ─── Predefined sub-topics for broad categories ───────────────────────────
EXAMPLES = {
    "grammar": ["Punctuation","Sentence Structure","Tenses","Parts of Speech"],
    "english": ["Essay Writing","Literature Analysis","Vocabulary","Grammar"],
    "math":    ["Algebra","Geometry","Calculus","Probability"],
    "chemistry":["Periodic Table","Chemical Bonds","Reactions","Stoichiometry"],
    # add more as you like...
}

# ─── Main Webhook ─────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method=="GET":
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

    # load memory + state
    mem = load_mem(phone)
    user_states.setdefault(phone, {})

    # ─── 1) Onboarding → ask full name once ───────────────────────────────
    if "full_name" not in mem:
        # if looks like full name
        if len(text.split()) >= 2:
            full = text.strip()
            first = full.split()[0]
            mem["full_name"]  = full
            mem["first_name"] = first
            save_mem(phone, mem)
            send_text(phone,
                f"Great, {first}! I’ve saved you in my contacts. What would you like to study first?")
        else:
            variants = [
                "Hey! What’s your full name so I can save you in my contacts?",
                "Hi there—please send me your full name so I can add you.",
                "Hello! Drop your full name, and I’ll save it for our chats.",
                "Nice to meet you! What’s your full name for my contacts?",
                "Welcome aboard! Could I get your full name to save in my contacts?"
            ]
            send_text(phone, random.choice(variants))
        return "OK", 200

    # now we know their name
    first = mem["first_name"]

    # ─── 2) DOCUMENT upload (PDF) ─────────────────────────────────────────
    if mtype=="document":
        doc_id = msg["document"]["id"]
        try:
            path   = download_media(doc_id, f"uploads/{doc_id}.pdf")
            parsed = parse_pdf(path)
            user_states[phone]["last_text"] = parsed
            send_text(phone,
                "✅ PDF received! Reply *summarize* or *quiz* when ready.")
        except Exception as e:
            print("PDF error:", e)
            send_text(phone,
                "⚠️ Couldn’t fetch or parse that PDF. Please try again.")
        return "OK", 200

    # ─── 3) IMAGE upload ───────────────────────────────────────────────────
    if mtype=="image":
        img_id = msg["image"]["id"]
        try:
            path = download_media(img_id, f"uploads/{img_id}.jpg")
            parsed = parse_image(path)
            user_states[phone]["last_text"] = parsed
            send_text(phone,
                "🖼️ Image received! Text extracted. Reply *summarize* or *quiz*.")
        except TesseractNotFoundError:
            send_text(phone,
                "⚠️ OCR not installed on server. Please upload a PDF instead.")
        except Exception as e:
            print("IMG error:", e)
            send_text(phone,
                "⚠️ Couldn’t process that image. Please try again.")
        return "OK", 200

    # ─── 4) Summarize / Quiz commands ──────────────────────────────────────
    cmd = text.lower()
    if cmd in ("summarize","quiz"):
        last = user_states[phone].get("last_text","")
        if not last:
            send_text(phone,
                "No file/text loaded yet—please upload a PDF or image first.")
        else:
            prompt = ("Summarize this:\n" if cmd=="summarize"
                      else "Create a 5-question quiz on this:\n") + last
            ans = get_reply(prompt, first)
            user_states[phone]["last_user_q"] = prompt
            send_buttons(phone, ans, two_buttons=True)
        return "OK", 200

    # ─── 5) Button replies ─────────────────────────────────────────────────
    if mtype=="interactive" and "button_reply" in msg.get("interactive",{}):
        bid = msg["interactive"]["button_reply"]["id"]
        if bid=="understood":
            send_text(phone, "Awesome—what’s next on your agenda?")
        elif bid=="explain_more":
            last = user_states[phone].get("last_user_q")
            if last:
                ans = get_reply("Please explain more:\n"+last, first)
                send_buttons(phone, ans, two_buttons=True)
            else:
                send_text(phone, "Sure—what should I explain further?")
        else:
            send_text(phone, "Hm, I didn’t catch that—could you rephrase?")
        return "OK", 200

    # ─── 6) Broad-topic detection ───────────────────────────────────────────
    subj = text.lower()
    if subj in EXAMPLES:
        exs = EXAMPLES[subj][:3]
        send_text(phone,
            f"Great choice! What specifically in {subj.title()}? "
            f"For example: {', '.join(exs)}.")
        return "OK", 200

    # ─── 7) Free-form Q&A ──────────────────────────────────────────────────
    if text:
        ans = get_reply(text, first)
        # on genuine questions, offer buttons
        if text.endswith("?"):
            user_states[phone]["last_user_q"] = text
            send_buttons(phone, ans, two_buttons=True)
        else:
            send_text(phone, ans or
                "Sorry, I’m not sure—could you clarify?")
    else:
        send_text(phone, "I didn’t catch that—what would you like to study?")
    return "OK", 200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=10000)
