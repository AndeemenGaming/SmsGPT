import os
from dotenv import load_dotenv
from flask import Flask, request
import requests
import hashlib
import time
from threading import Thread, Timer
from collections import defaultdict  # CONTEXT MEMORY

app = Flask(__name__)

# --- CONFIGURATION ---
load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MODEL_NAME = "deepseek/deepseek-r1:free"

# --- DUAL TELERIVET GATEWAYS ---
GATEWAYS = [
    {
        "API_KEY": os.getenv("TELERIVET_API_KEY_1"),
        "PROJECT_ID": os.getenv("TELERIVET_PROJECT_ID_1"),
        "PHONE_ID": os.getenv("TELERIVET_PHONE_ID_1")
    },
    {
        "API_KEY": os.getenv("TELERIVET_API_KEY_2"),
        "PROJECT_ID": os.getenv("TELERIVET_PROJECT_ID_2"),
        "PHONE_ID": os.getenv("TELERIVET_PHONE_ID_2")
    }
]
gateway_index = 0
from threading import Lock
gateway_lock = Lock()

def pick_gateway():
    global gateway_index
    with gateway_lock:
        gw = GATEWAYS[gateway_index]
        gateway_index = (gateway_index + 1) % len(GATEWAYS)
    return gw

# --- WHITELIST + PREFIX ---
whitelist_str = os.getenv("PHONE_NUMBER", "")
WHITELIST = set(whitelist_str.split(",")) if whitelist_str else set()
TRIGGER_PREFIX = "Chat"

# SMS behavior
MAX_SMS_CHARS = 2400

# Message deduplication
recent_messages = {}  # key = from_number, value = (hash, timestamp)
REPEAT_TIMEOUT = 30   # seconds to ignore repeated messages

# Timers to delay sending SMS per user, to wait for the last part
send_timers = {}  # key = from_number, value = Timer object
pending_replies = {}  # key = from_number, value = reply

# --- CONTEXT MEMORY ---
user_contexts = defaultdict(list)  # key = phone number, value = list of chat history
MAX_CONTEXT_LEN = 10  # Keep recent 10 exchanges only

# --- ROUTES ---
@app.route("/incoming", methods=["POST"], strict_slashes=False)
def incoming():
    print(f"📩 Headers: {request.headers}")
    print(f"📩 Body: {request.get_data()}")

    if request.is_json:
        data = request.get_json()
    else:
        data = request.form.to_dict()

    if not data:
        print("❌ No data received.")
        return "Bad Request", 400

    from_number = data.get("from_number")
    content = data.get("content", "")

    if not from_number or from_number not in WHITELIST:
        print(f"⛔ Unauthorized sender: {from_number}")
        return "Unauthorized", 403

    if not content.strip().lower().startswith(TRIGGER_PREFIX.lower()):
        print("🚫 Ignoring non-GPT message.")
        return "Ignored", 200

    msg_hash = hashlib.sha256(content.encode()).hexdigest()
    last_hash, last_time = recent_messages.get(from_number, (None, 0))

    if msg_hash == last_hash and time.time() - last_time < REPEAT_TIMEOUT:
        print("🔁 Duplicate message received recently. Ignoring.")
        return "Duplicate ignored", 200

    recent_messages[from_number] = (msg_hash, time.time())
    prompt = content[len(TRIGGER_PREFIX):].strip()
    print(f"✅ Prompt from {from_number}: {prompt}")

    Thread(target=process_prompt_with_delay, args=(from_number, prompt)).start()
    return "OK", 200

@app.route("/", methods=["GET"])
def home():
    return "Flask GPT-SMS server (Dual Telerivet) is running!", 200

# --- GPT + REPLY HANDLING ---
def process_prompt_with_delay(from_number, prompt):
    try:
        reply = get_deepseek_response(from_number, prompt)
    except Exception as e:
        print(f"❗ DeepSeek error: {e}")
        reply = "⚠️ DeepSeek is currently unavailable or quota has been exceeded."

    pending_replies[from_number] = reply

    if from_number in send_timers:
        send_timers[from_number].cancel()

    timer = Timer(2.0, send_pending_reply, args=(from_number,))
    send_timers[from_number] = timer
    timer.start()

def send_pending_reply(from_number):
    reply = pending_replies.pop(from_number, None)
    send_timers.pop(from_number, None)
    if reply:
        send_sms(from_number, reply)

def get_deepseek_response(from_number, prompt):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    user_contexts[from_number].append({"role": "user", "content": prompt})
    if len(user_contexts[from_number]) > MAX_CONTEXT_LEN:
        user_contexts[from_number] = user_contexts[from_number][-MAX_CONTEXT_LEN:]

    payload = {
        "model": MODEL_NAME,
        "messages": user_contexts[from_number],
        "stream": False
    }

    print("📡 Querying DeepSeek via OpenRouter...")
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 200:
        reply = response.json()['choices'][0]['message']['content'].strip()
        user_contexts[from_number].append({"role": "assistant", "content": reply})
        if len(reply) > MAX_SMS_CHARS:
            print(f"⚠️ Message too long ({len(reply)} chars), truncating.")
            reply = reply[:MAX_SMS_CHARS] + "\n[...truncated]"
        return reply
    else:
        print(f"❌ Error {response.status_code}: {response.text}")
        return "⚠️ DeepSeek API error. Try again later."

def send_sms(to_number, message):
    gateway = pick_gateway()
    url = f"https://api.telerivet.com/v1/projects/{gateway['PROJECT_ID']}/messages/send"
    headers = {"Content-Type": "application/json"}
    auth = (gateway["API_KEY"], '')
    payload = {
        "to_number": to_number,
        "content": message,
        "phone_id": gateway["PHONE_ID"]
    }

    print(f"📤 Sending via Gateway {GATEWAYS.index(gateway)+1} to {to_number}")
    r = requests.post(url, json=payload, auth=auth, headers=headers)
    print(f"📬 Telerivet response: {r.status_code} - {r.text}")

# --- MAIN ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
