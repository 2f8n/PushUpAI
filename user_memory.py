import json
import os

DATA_FILE = "user_data.json"

def load_user_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w") as f:
            json.dump({}, f)
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_user_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_user_profile(user_id):
    data = load_user_data()
    return data.get(user_id, {})

def update_user_profile(user_id, key, value):
    data = load_user_data()
    if user_id not in data:
        data[user_id] = {}
    data[user_id][key] = value
    save_user_data(data)

def add_message_to_history(user_id, message):
    data = load_user_data()
    if user_id not in data:
        data[user_id] = {}
    history = data[user_id].get("history", [])
    history.append({"message": message})
    data[user_id]["history"] = history[-10:]  # Keep last 10 messages only
    save_user_data(data)