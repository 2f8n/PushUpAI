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
    "Never start responses with greetings. "
    "Provide detailed, step-by-step solutions and examples for study questions, followed by 'Did that make sense to you?' as a button prompt. "
    "For non-study chat, reply concisely without adding a check prompt. "
    "Do not mention technical details or that you are an AI."
)

# --- Ensure memory directory ---
if not os.path.exists("memory"):
    os.makedirs("memory")

# --- Memory helpers ---
def load_user_memory(phone):
    path = f"memory/{phone}.json"
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_user_memory(phone, data):
    path = f"memory/{phone}.json"
    with open(path, "w") as f:
        json.dump(data, f)

# --- Question type detection ---
def is_name_question(text):
    q = text.lower()
    return any(phrase in q for phrase in [
        "what's my name", "whats my name", "what is my name", "who am i"
    ])


def is_founder_question(text):
    return "found" in text.lower() or "bytewave" in text.lower()

# --- Build prompt for tutor ---
def build_tutor_prompt(user_name, user_text):
    return (
        SYSTEM_PROMPT + "\n---\n"
        f"Student Name: {user_name}\n"
        f"User Query: {user_text}\n---\n"
        "Solution:"  # model completes here
    )

# --- WhatsApp send helpers ---
def send_whatsapp_message(phone, text):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)


def send_whatsapp_buttons(phone, text):
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
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    data = request.json
    try:
        change = data["entry"][0]["changes"][0]["value"]
        messages = change.get("messages") or []
        if not messages:
            return "OK", 200

        msg = messages[0]
        phone = msg.get("from")
        text = msg.get("text", {}).get("body", "").strip()
        if not phone or not text:
            return "OK", 200

        # Load memory
        memory = load_user_memory(phone)
        name = memory.get("name")

        # Onboarding: ask for full name with friendly tone
        if not name:
            if len(text.split()) >= 2 and text.replace(" ", "").isalpha():
                memory["name"] = text
                save_user_memory(phone, memory)
                send_whatsapp_message(phone, f"Nice! I’ll call you {text.split()[0]}. What would you like to study today?")
            else:
                send_whatsapp_message(phone, "Hey there! Could you share your full name (first and last) so I know what to call you?")
            return "OK", 200

        # Handle identity questions
        if is_name_question(text):
            send_whatsapp_message(phone, f"You’re {name}! Let's keep going 👍")
            return "OK", 200

        # Handle founder questions
        if is_founder_question(text):
            send_whatsapp_message(phone, "StudyMate AI was founded by ByteWave Media to make learning fun and effective! 😃")
            return "OK", 200

        # Thinking indicator
        send_whatsapp_message(phone, "🤖 Thinking...")

        # Generate tutor response
        prompt = build_tutor_prompt(name, text)
        response = model.generate_content(prompt)
        reply = response.text.strip()

        # Strip any leading greetings
        for g in ["hi,", "hello,", "hey,"]:
            if reply.lower().startswith(g):
                reply = reply[len(g):].strip()

        # Determine if multi-sentence => educational
        sentences = [s for s in reply.split('.') if s.strip()]
        if len(sentences) > 1:
            # send solution and then buttons
            send_whatsapp_message(phone, reply)
            send_whatsapp_buttons(phone, "Did that make sense to you?")
        else:
            send_whatsapp_message(phone, reply)

    except Exception as e:
        print("Error handling message:", e)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
