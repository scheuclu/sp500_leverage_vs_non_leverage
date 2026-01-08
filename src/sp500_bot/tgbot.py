import requests
import json

BOT_TOKEN = "8527180594:AAHwZ7RgtWcY_KI3go1rLADPqnhS49xZd-Q"
CHAT_ID = "5126573205"


def get_chat_ids():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

    response = requests.get(url)
    data = response.json()

    print(json.dumps(data, indent=2))

    # Extract chat IDs
    for result in data.get("result", []):
        message = result.get("message") or result.get("channel_post")
        if message:
            chat = message.get("chat", {})
            chat_id = chat.get("id")
            chat_type = chat.get("type")
            print(f"Chat ID: {chat_id} | Type: {chat_type}")

def send_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
    r = requests.post(url, json=payload)
    assert r.status_code == 200


if __name__ == '__main__':
    # get_chat_ids()
    send_message("ABC")