from flask import Flask, request
import requests
import os
import json
import google.generativeai as genai

app = Flask(__name__)

# --- Environment variables ---
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "studymate_verify")
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# --- Configure Gemini ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-pro-002")

# --- System prompt for StudyMate AI ---
SYSTEM_PROMPT = (
    "You are StudyMate AI, founded by ByteWave Media, an enthusiastic academic tutor on WhatsApp. "
    "Never start with a greeting. Maintain context of the conversation and provide accurate, step-by-step educational answers when asked study questions. "
    "For non-study messages, reply concisely. "
    "Do not mention you're an AI or technical details."
)

# --- Ensure memory directory ---
if not os.path.exists("memory"):
    os.makedirs("memory")

# --- Memory helpers ---
def load_memory(phone):
    path = f"memory/{phone}.json"
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"history": []}


def save_memory(phone, data):
    path = f"memory/{phone}.json"
    with open(path, "w") as f:
        json.dump(data, f)

# --- Check types ---
def is_name_question(text):
    q = text.lower()
    return any(phrase in q for phrase in ["what's my name", "whats my name", "who am i"])

# --- Build prompt including history ---
def build_prompt(user_name, history, user_text):
    convo = "".join([f"Student: {h['user']}\nTutor: {h.get('bot','')}\n" for h in history[-4:]])
    return (
        SYSTEM_PROMPT + "\n--- Conversation so far: ---\n" + convo +
        f"Student: {user_text}\nTutor:"
    )

# --- WhatsApp helpers ---
def send_text(phone, text):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product":"whatsapp","to":phone,"type":"text","text":{"body":text}}
    requests.post(url, headers=headers, json=payload)


def send_buttons(phone, text):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "understood", "title": "Understood"}},
                {"type": "reply", "reply": {"id": "explain_more", "title": "Explain more"}}
            ]}
        }
    }
    requests.post(url, headers=headers, json=payload)

# --- Webhook endpoint ---
@app.route("/webhook", methods=["GET","POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token")==VERIFY_TOKEN:
            return request.args.get("hub.challenge"),200
        return "Verification failed",403

    data = request.json
    try:
        change = data["entry"][0]["changes"][0]["value"]
        msgs = change.get("messages") or []
        if not msgs:
            return "OK",200

        msg = msgs[0]
        phone = msg.get("from")
        text = msg.get("text",{}).get("body","").strip()
        if not phone or not text:
            return "OK",200

        mem = load_memory(phone)
        name = mem.get("name")
        history = mem.get("history", [])

        # Onboarding name
        if not name:
            if len(text.split())>=2 and text.replace(" ","").isalpha():
                mem["name"] = text
                save_memory(phone,mem)
                send_text(phone, f"Great! I'll call you {text.split()[0]}. What do you want to study today?")
            else:
                send_text(phone, "Hey! What's your full name so I know what to call you?")
            return "OK",200

        # Identity check
        if is_name_question(text):
            send_text(phone, f"You're {name}! Let's continue.")
            return "OK",200

        # Build AI response
        prompt = build_prompt(name, history, text)
        res = model.generate_content(prompt)
        reply = res.text.strip()

        # Clean reply
        for g in ["Hi,","Hello,","Hey,"]:
            if reply.startswith(g): reply=reply[len(g):].strip()

        # Update history
        history.append({"user":text, "bot":reply})
        mem["history"] = history[-20:]
        save_memory(phone, mem)

        # Decide interactivity
        # if explanation (multiple sentences)
        if '.' in reply and len(reply.split('.'))>1:
            send_text(phone, reply)
            send_buttons(phone, "Did that make sense to you?")
        else:
            send_text(phone, reply)

    except Exception as e:
        print("Error:",e)
    return "OK",200

if __name__=="__main__":
    app.run(host="0.0.0.0",port=10000)
