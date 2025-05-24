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

# --- System instruction for StudyMate AI ---
SYSTEM_PROMPT = (
    "You are StudyMate AI, founded by ByteWave Media, an enthusiastic academic tutor on WhatsApp. "
    "Never start responses with greetings like 'hi' or 'hello'. "
    "When a user asks a study-related question, provide a clear, step-by-step solution, detailed examples, and encouragement. "
    "At the end of educational explanations, ask 'Did that make sense to you?' and expect a button response. "
    "For casual or non-study chats, reply concisely without adding 'Did that make sense?'. "
    "Do not mention that you are an AI model or technical implementation details."
)

# --- Ensure memory directory exists ---
if not os.path.exists("memory"):
    os.makedirs("memory")

# --- User memory functions ---
def load_user_memory(phone):
    path = f"memory/{phone}.json"
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_user_memory(phone, data):
    path = f"memory/{phone}.json"
    with open(path, "w") as f:
        json.dump(data, f)

# --- Name detection ---
def is_name_question(text):
    q = text.lower()
    return any(phrase in q for phrase in [
        "what's my name", "whats my name", "what is my name", "who am i"
    ])

# --- Build tutor prompt ---
def build_tutor_prompt(user_name, user_text):
    return (
        SYSTEM_PROMPT + "\n---\n"
        f"Student Name: {user_name}\n"
        f"User Query: {user_text}\n---\n"
        "Solution:"  # Model completes here
    )

# --- WhatsApp messaging helpers ---
def send_whatsapp_message(phone, text):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=payload)

# --- Send interactive buttons ---
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
        messages = change.get("messages")
        if not messages:
            return "OK", 200

        msg = messages[0]
        phone = msg["from"]
        text = msg.get("text", {}).get("body", "").strip()
        if not text:
            return "OK", 200

        memory = load_user_memory(phone)
        name = memory.get("name")

        # Onboarding: ask for full name
        if not name:
            # user entering name
            if len(text.split()) >= 2 and text.replace(" ", "").isalpha():
                memory["name"] = text
                save_user_memory(phone, memory)
                send_whatsapp_message(phone, f"Sweet, thanks {text.split()[0]}! What would you like to study today?")
            else:
                send_whatsapp_message(phone, "Hey! Could you share your full name (first and last) so I know what to call you?")
            return "OK", 200

        # Identity check
        if is_name_question(text):
            send_whatsapp_message(phone, f"Of course – you are {name}! Ready to continue? 😊")
            return "OK", 200

        # Send thinking placeholder
        send_whatsapp_message(phone, "🤖 Thinking...")

        # Build prompt and get solution
        prompt = build_tutor_prompt(name, text)
        response = model.generate_content(prompt)
        reply = response.text.strip()

        # Remove any unintended greeting from model
        reply = reply.lstrip().lstrip('Hi,').lstrip('Hello,').lstrip('Hey,').strip()

        # Determine if this is an educational explanation requiring check
        if 'Did that make sense to you?' in reply:
            core_text = reply.replace('Did that make sense to you?', '').strip()
            send_whatsapp_buttons(phone, core_text)
        else:
            send_whatsapp_message(phone, reply)

    except Exception as e:
        print("Error handling message:", e)
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
