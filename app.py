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
    "You are StudyMate AI, an engaging and enthusiastic academic tutor on WhatsApp. "
    "When assisting a student, show genuine excitement and passion about their learning journey. "
    "Offer detailed examples, step-by-step guidance, and words of encouragement. "
    "Avoid robotic or dry responses—be personable and empathetic. "
    "Never mention technical details or that you’re an AI model. "
    "Keep informal banter brief. "
    "End educational replies with: 'Did that make sense to you? ✅ Yes or ❓ Not yet?'."
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

# --- Name question detection ---
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
        f"Question: {user_text}\n---\n"
        "Tutor:"  # model completes here
    )

# --- Send WhatsApp message ---
def send_whatsapp_message(phone, text):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": text}
    }
    response = requests.post(url, headers=headers, json=payload)
    print("WhatsApp API response:", response.status_code, response.text)
    return response.json()

# --- Webhook endpoint ---
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return challenge, 200
        return "Verification failed", 403

    if request.method == "POST":
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

            # Onboarding: ask full name
            if not name:
                if len(text.split()) >= 2 and text.replace(" ", "").isalpha():
                    memory["name"] = text
                    save_user_memory(phone, memory)
                    send_whatsapp_message(phone, f"Awesome, {text.split()[0]}! Let’s get started with your studies 😊")
                else:
                    send_whatsapp_message(phone, "Hi there! Please share your *full name* (first and last) so I can personalize our tutoring.")
                return "OK", 200

            # If user asks their name
            if is_name_question(text):
                send_whatsapp_message(phone, f"You’re {name}! Let’s continue learning together 🙌")
                return "OK", 200

            # Thinking indicator
            send_whatsapp_message(phone, "🤖 Thinking...")

            # Generate and send tutor response
            prompt = build_tutor_prompt(name, text)
            response = model.generate_content(prompt)
            reply = response.text.strip()
            send_whatsapp_message(phone, reply)

        except Exception as e:
            print("Error handling message:", e)
        return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
