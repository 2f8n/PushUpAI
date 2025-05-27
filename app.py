import os, re, json, logging
from collections import deque
from datetime import datetime, timedelta

import requests
from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore
import google.generativeai as genai

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger("StudyMate")

# Load .env locally if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Required environment variables
for v in ("VERIFY_TOKEN","ACCESS_TOKEN","PHONE_NUMBER_ID","GEMINI_API_KEY"):
    if not os.getenv(v):
        logger.error(f"Missing environment variable: {v}")
        raise SystemExit("Please set all required environment variables.")

VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN")
ACCESS_TOKEN    = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
PORT            = int(os.getenv("PORT", 10000))

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# Firestore setup
KEY_FILE    = "studymate-ai-9197f-firebase-adminsdk-fbsvc-5a52d9ff48.json"
SECRET_PATH = f"/etc/secrets/{KEY_FILE}"
cred_path   = SECRET_PATH if os.path.exists(SECRET_PATH) else KEY_FILE
cred        = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

# System prompt
BASE_PROMPT = """
You are a friendly academic tutor.
Always respond with JSON ONLY: {"type":"clarification"|"answer","content":"…"}
Rules:
1. If name unknown, type="clarification" and ask: "Please share your full name (first and last)."
2. If user has given enough detail (e.g., "essay 10 lines global warming", a clear math problem, etc.), default to type="answer" and provide the full response—no further questions.
3. Only ask one clarifying question (type="clarification") if user’s request truly lacks key details.
4. Answers must be step-by-step academic guidance (≤3 sentences per step).
5. Do NOT include interactive instructions in "content"—buttons are appended by the system when type="answer".
"""

app = Flask(__name__)
sessions = {}  # phone → {"history": deque(maxlen=5), "clarification_count": int}

def ensure_session(phone):
    s = sessions.get(phone)
    if not s:
        s = {"history": deque(maxlen=5), "clarification_count": 0}
        sessions[phone] = s
    return s

def safe_post(url, payload):
    try:
        r = requests.post(url,
                          headers={"Authorization":f"Bearer {ACCESS_TOKEN}",
                                   "Content-Type":"application/json"},
                          json=payload)
        if r.status_code != 200:
            logger.error(f"WhatsApp API {r.status_code}: {r.text}")
    except Exception:
        logger.exception("Failed WhatsApp send")

def send_text(phone, text):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product":"whatsapp","to":phone,
        "type":"text","text":{"body": text}
    })

def send_buttons(phone):
    safe_post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages", {
        "messaging_product":"whatsapp","to":phone,"type":"interactive",
        "interactive":{
            "type":"button","body":{"text":"Did that make sense to you?"},
            "action":{"buttons":[
                {"type":"reply","reply":{"id":"understood","title":"Understood"}},
                {"type":"reply","reply":{"id":"explain_more","title":"Explain more"}}
            ]}
        }
    })

def strip_fences(s):
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[\w]*|```$", "", s).strip()
    return s

def get_gemini(prompt):
    try:
        return model.generate_content(prompt).text.strip()
    except Exception:
        logger.exception("Gemini error")
        return json.dumps({
            "type":"clarification",
            "content":"Sorry, I encountered an error. Please try again."
        })

def get_or_create_user(phone):
    ref = db.collection("users").document(phone)
    doc = ref.get()
    if not doc.exists:
        user = {
            "phone": phone,
            "name": None,
            "account_type": "free",
            "credit_remaining": 20,
            "credit_reset": datetime.utcnow() + timedelta(days=1),
            "last_prompt": None
        }
        ref.set(user)
        return user
    return doc.to_dict()

def update_user(phone, **fields):
    db.collection("users").document(phone).update(fields)
    logger.info(f"Updated {phone}: {fields}")

def build_prompt(user, history, text):
    name_line = f'User name: "{user["name"]}"\n' if user.get("name") else ""
    hist_block = ""
    if history:
        hist_block = "Recent messages:\n" + "\n".join(f"- {m}" for m in history) + "\n"
    return (
        BASE_PROMPT.strip() + "\n" +
        name_line +
        hist_block +
        f'Current message: "{text}"\nJSON:'
    )

@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        if (request.args.get("hub.mode")=="subscribe" and
            request.args.get("hub.verify_token")==VERIFY_TOKEN):
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    data = request.json or {}
    entry = data.get("entry", [])
    if not entry or not entry[0].get("changes"):
        return "OK", 200

    msg   = entry[0]["changes"][0]["value"].get("messages",[{}])[0]
    phone = msg.get("from")
    text  = msg.get("text",{}).get("body","").strip()
    if not phone or not text:
        return "OK", 200

    user = get_or_create_user(phone)
    now  = datetime.utcnow()

    # 1) Onboarding by name
    if user["name"] is None:
        if len(text.split()) >= 2:
            first = text.split()[0]
            update_user(phone, name=text)
            send_text(phone, f"Nice to meet you, {first}! What would you like to study today?")
        else:
            send_text(phone, "Please share your full name (first and last).")
        return "OK", 200

    # 2) Credit logic for free users
    if user["account_type"] == "free":
        rt = user["credit_reset"]
        if hasattr(rt, "to_datetime"): rt = rt.to_datetime()
        if isinstance(rt, datetime) and rt.tzinfo: rt = rt.replace(tzinfo=None)
        if now >= rt:
            update_user(phone, credit_remaining=20,
                        credit_reset=now + timedelta(days=1))
            user["credit_remaining"] = 20
        if user["credit_remaining"] <= 0:
            send_text(phone, "Free limit reached (20/day). Upgrade for unlimited usage.")
            return "OK", 200
        update_user(phone, credit_remaining=user["credit_remaining"] - 1)

    # 3) Handle button replies
    if msg.get("type") == "interactive":
        ir = msg.get("interactive",{})
        if ir.get("type") == "button_reply":
            bid = ir["button_reply"]["id"]
            if bid == "understood":
                send_text(phone, "Great! 🎉 What’s next?")
            elif bid == "explain_more" and user.get("last_prompt"):
                more = get_gemini(user["last_prompt"] + "\n\nPlease explain in more detail.")
                send_text(phone, strip_fences(more))
                send_buttons(phone)
        return "OK", 200

    # 4) Session management & switching
    sess = ensure_session(phone)
    # If user asks to switch topic/subject, reset context
    if re.search(r"\b(switch|change)\b.*\b(topic|subject)\b", text, re.IGNORECASE):
        sess["history"].clear()
        sess["clarification_count"] = 0
        send_text(phone, "Sure—what would you like to study now?")
        return "OK", 200

    # 5) Track history
    history = list(sess["history"])
    sess["history"].append(text)

    # 6) Dynamic Gemini flow
    prompt = build_prompt(user, history, text)
    raw    = get_gemini(prompt)
    clean  = strip_fences(raw)
    try:
        j = json.loads(clean)
        typ = j.get("type")
        content = j.get("content","")
    except Exception:
        logger.error(f"JSON parse failed: {clean}")
        typ = "answer"
        content = clean

    # 7) Clarification streak handling
    if typ == "clarification":
        sess["clarification_count"] += 1
        if sess["clarification_count"] > 1:
            examples = [
                "Write a 5-line essay on the causes of global warming.",
                "Simplify the expression √5 (square root of 5).",
                "Explain photosynthesis in three steps."
            ]
            send_text(phone,
                "I’m still unclear—here are examples of clearer requests:\n" +
                "\n".join(f"- {e}" for e in examples)
            )
            sess["clarification_count"] = 0
        else:
            send_text(phone, content)
        return "OK", 200

    # 8) Reset clarification count on answer
    sess["clarification_count"] = 0

    # 9) Format structured answers
    formatted = None
    try:
        struct = json.loads(content)
        if isinstance(struct, dict) and "title" in struct and "steps" in struct:
            lines = [struct["title"]]
            for step in struct["steps"]:
                lines.append(f"{step.get('heading')}: {step.get('explanation')}")
            formatted = "\n\n".join(lines)
    except Exception:
        pass

    # 10) Send answer
    send_text(phone, formatted or content)
    send_buttons(phone)

    # 11) Save last prompt
    update_user(phone, last_prompt=prompt)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
