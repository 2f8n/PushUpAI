# File: app.py

from flask import Flask, request
import os, json, requests, pdfplumber
from PIL import Image
import google.generativeai as genai
from requests.exceptions import HTTPError

app = Flask(__name__)

# ─── Config ──────────────────────────────────────────────────────────────
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN", "your_verify_token")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME      = "gemini-1.5-pro-002"  # adjust as needed

genai.configure(api_key=GEMINI_API_KEY)

# ─── Memory Helpers ──────────────────────────────────────────────────────
user_states = {}
def mem_path(phone): return f"memory/{phone}.json"
def load_mem(phone):
    try: return json.load(open(mem_path(phone)))
    except: return {}
def save_mem(phone, data):
    os.makedirs("memory", exist_ok=True)
    json.dump(data, open(mem_path(phone),"w"))

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

def slugify(s):
    return "".join(c for c in s.lower() if c.isalnum() or c==" ").replace(" ","_")

def send_buttons(phone, text, options=None):
    # default Understood / Explain more
    if not options:
        buttons = [
            {"type":"reply","reply":{"id":"understood","title":"Understood"}},
            {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
        ]
    else:
        buttons=[]
        for opt in options[:3]:
            title = opt if len(opt)<=20 else opt[:17]+"..."
            bid = slugify(title)
            buttons.append({
                "type":"reply",
                "reply":{"id":bid,"title":title}
            })
            user_states[phone].setdefault("last_buttons", {})[bid]=opt

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

# ─── Gemini Q&A ─────────────────────────────────────────────────────────
def get_reply(prompt, name):
    system = f"""
You are StudyMate AI. Address the student by name ({name}), dive straight in—no “Hi.”"""
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        r = model.generate_content(system+"\n\nStudent: "+prompt)
        return r.text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return ""

# ─── PDF Parsing ────────────────────────────────────────────────────────
def parse_pdf(path):
    all_text=[]
    with pdfplumber.open(path) as pdf:
        for p in pdf.pages:
            all_text.append(p.extract_text() or "")
    return "\n".join(all_text)

# ─── Webhook ────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method=="GET":
        if (request.args.get("hub.mode")=="subscribe"
            and request.args.get("hub.verify_token")==VERIFY_TOKEN):
            return request.args.get("hub.challenge"),200
        return "Verification failed",403

    data = request.json
    val  = data["entry"][0]["changes"][0]["value"]
    if "messages" not in val:
        return "OK",200

    msg   = val["messages"][0]
    phone = msg["from"]
    mtype = msg.get("type")
    text  = msg.get("text",{}).get("body","").strip()
    mem   = load_mem(phone)
    user_states.setdefault(phone, {})

    # ─── 1) Onboarding ──────────────────────────────────────────────────
    if "name" not in mem:
        if len(text.split())>=2:
            mem["name"]=text
            save_mem(phone, mem)
            send_text(phone, f"Awesome, {text}! What topic first?")
        else:
            send_text(phone, "Welcome! What’s your full name? (so I can call you properly)")
        return "OK",200

    name = mem["name"]

    # ─── 2) Image Upload ────────────────────────────────────────────────
    if mtype=="image":
        send_text(phone,
            "Got your image! I can’t extract text right now—please upload a PDF or type your question.")
        return "OK",200

    # ─── 3) PDF Upload ──────────────────────────────────────────────────
    if mtype=="document":
        mid = msg["document"]["id"]
        try:
            # fetch URL
            url = requests.get(
                f"https://graph.facebook.com/v19.0/{mid}",
                headers={"Authorization":f"Bearer {ACCESS_TOKEN}"},
                params={"fields":"url"}
            ).json()["url"]
            content = requests.get(url).content
            os.makedirs("uploads", exist_ok=True)
            path = f"uploads/{mid}.pdf"
            open(path,"wb").write(content)
            parsed = parse_pdf(path)
            user_states[phone]["last_file_text"]=parsed
            send_text(phone,
                "PDF received! Reply *summarize* or *quiz* when you’re ready.")
        except Exception:
            send_text(phone, "Couldn’t fetch that PDF—please try again.")
        return "OK",200

    # ─── 4) Summarize / Quiz ────────────────────────────────────────────
    cmd = text.lower()
    if cmd in ("summarize","quiz"):
        parsed = user_states[phone].get("last_file_text","")
        if not parsed:
            send_text(phone, "No PDF loaded—please upload one first.")
        else:
            if cmd=="summarize":
                prompt = "Summarize:\n"+parsed
            else:
                prompt = "Create a 5-question quiz on:\n"+parsed
            ans = get_reply(prompt, name)
            user_states[phone]["last_user_q"]=prompt
            send_buttons(phone, ans)
        return "OK",200

    # ─── 5) Button Replies ──────────────────────────────────────────────
    if mtype=="interactive" and "button_reply" in msg.get("interactive",{}):
        bid = msg["interactive"]["button_reply"]["id"]
        # Understood
        if bid=="understood":
            send_text(phone, "Great! What next?")
        # Explain more
        elif bid=="explain_more":
            last = user_states[phone].get("last_user_q")
            if last:
                ans = get_reply("Please explain further: "+last, name)
                user_states[phone]["last_user_q"]=last
                send_buttons(phone, ans)
            else:
                send_text(phone, "What would you like me to dig into?")
        # Subtopic buttons
        elif bid in user_states[phone].get("last_buttons",{}):
            orig = user_states[phone]["last_buttons"][bid]
            user_states[phone]["last_user_q"]=orig
            ans = get_reply(orig, name)
            send_buttons(phone, ans)
        else:
            send_text(phone, "Sorry — didn’t catch that. Could you rephrase?")
        return "OK",200

    # ─── 6) Broad-subject detection ─────────────────────────────────────
    subj = text.lower()
    BROAD = {"chemistry","physics","biology","english","grammar",
             "math","history","coding","programming","literature"}
    if subj in BROAD:
        # get subtopics from Gemini
        resp = get_reply(
            f"List 4 common subtopics in {subj.title()}, comma-separated.", name)
        tops = [t.strip().title() for t in resp.split(",") if t.strip()]
        text2 = (f"Oh, great! What specifically in {subj.title()}? "
                 f"For example: {', '.join(tops[:3])}")
        send_buttons(phone, text2, tops[:3])
        return "OK",200

    # ─── 7) Q&A Fallback ────────────────────────────────────────────────
    if text:
        ans = get_reply(text, name)
        user_states[phone]["last_user_q"]=text
        # only buttons if user asked a question
        if text.endswith("?"):
            send_buttons(phone, ans)
        else:
            send_text(phone, ans or
                "Sorry, I’m not sure. Could you clarify?")
    else:
        send_text(phone, "I didn’t catch that—what would you like to study?")
    return "OK",200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=10000)
