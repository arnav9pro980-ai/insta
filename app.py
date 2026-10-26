
import os
import secrets
import requests
from functools import wraps
from flask import Flask, request, jsonify
from instagrapi import Client

USERNAME = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")
API_KEY = os.environ.get("LOCAL_API_KEY")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "https://launcher-c813d-default-rtdb.europe-west1.firebasedatabase.app"
).rstrip("/")

if not USERNAME or not PASSWORD:
    raise RuntimeError("Set EMAIL and PASSWORD in Render environment variables.")

if not API_KEY:
    raise RuntimeError("Set LOCAL_API_KEY in Render environment variables.")

SESSION_URL = f"{DATABASE_URL}/instagram_bot/session.json"

firebase_http = requests.Session()
client = Client()
app = Flask(__name__)


def get_saved_session():
    response = firebase_http.get(SESSION_URL, timeout=15)
    response.raise_for_status()
    return response.json()


def save_session():
    response = firebase_http.put(
        SESSION_URL,
        json=client.get_settings(),
        timeout=15,
    )
    response.raise_for_status()


def login():
    settings = None

    try:
        settings = get_saved_session()
    except requests.RequestException as exc:
        print(f"[Firebase] Could not load session: {exc}")

    if settings:
        try:
            client.set_settings(settings)
            client.login(USERNAME, PASSWORD)
            client.get_timeline_feed()
            save_session()
            print("[Instagram] Existing session loaded.")
            return
        except Exception as exc:
            print(f"[Instagram] Saved session rejected: {exc}")

    client.login(USERNAME, PASSWORD)
    save_session()
    print("[Instagram] Login successful; session saved.")


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        supplied = request.headers.get("X-API-Key", "")

        if not secrets.compare_digest(supplied, API_KEY):
            return jsonify({"error": "Unauthorized"}), 401

        return fn(*args, **kwargs)

    return wrapper


def model_value(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def serialize_user(user):
    return {
        "pk": str(model_value(user, "pk", "")),
        "username": model_value(user, "username"),
        "full_name": model_value(user, "full_name"),
        "is_private": model_value(user, "is_private"),
        "is_verified": model_value(user, "is_verified"),
    }


def serialize_message(message):
    user = model_value(message, "user")

    return {
        "id": str(model_value(message, "id", "")),
        "text": model_value(message, "text", ""),
        "timestamp": str(model_value(message, "timestamp", "")),
        "is_sent_by_viewer": model_value(
            message, "is_sent_by_viewer", False
        ),
        "user": serialize_user(user) if user else None,
    }


def serialize_thread(thread):
    users = model_value(thread, "users", []) or []
    messages = model_value(thread, "messages", []) or []

    return {
        "id": str(model_value(thread, "id", "")),
        "title": model_value(thread, "thread_title"),
        "users": [serialize_user(user) for user in users],
        "messages": [serialize_message(message) for message in messages],
    }


def get_limit(default, maximum):
    value = request.args.get("limit", default, type=int)
    return max(1, min(value or default, maximum))


@app.get("/")
def index():
    return jsonify({
        "name": "Instagram API",
        "status": "running",
        "docs": [
            "GET /health",
            "GET /me",
            "GET /users/<username>",
            "GET /inbox?limit=20",
            "GET /threads/<thread_id>/messages?limit=30",
            "POST /threads/<thread_id>/messages",
            "GET /users/<username>/followers?limit=20",
            "GET /users/<username>/following?limit=20",
            "GET /media/<media_id>",
            "GET /media/<media_id>/comments?limit=20",
        ],
        "authentication": "X-API-Key header required for API endpoints",
    })


@app.get("/health")
@require_api_key
def health():
    return jsonify({
        "status": "ok",
        "instagram_logged_in": True,
    })


@app.get("/me")
@require_api_key
def me():
    return jsonify(serialize_user(client.account_info()))


@app.get("/users/<username>")
@require_api_key
def user_info(username):
    user = client.user_info_by_username(username)
    return jsonify(serialize_user(user))


@app.get("/inbox")
@require_api_key
def inbox():
    limit = get_limit(20, 50)

    threads = client.direct_threads(
        amount=limit,
        thread_message_limit=10,
    )

    return jsonify({
        "count": len(threads),
        "threads": [serialize_thread(thread) for thread in threads],
    })


@app.get("/threads/<thread_id>/messages")
@require_api_key
def thread_messages(thread_id):
    limit = get_limit(30, 100)
    messages = client.direct_messages(int(thread_id), amount=limit)

    return jsonify({
        "thread_id": thread_id,
        "count": len(messages),
        "messages": [serialize_message(m) for m in messages],
    })


@app.post("/threads/<thread_id>/messages")
@require_api_key
def send_message(thread_id):
    data = request.get_json(silent=True) or {}
    message = data.get("text")

    if not isinstance(message, str) or not message.strip():
        return jsonify({
            "error": "Provide a non-empty text field."
        }), 400

    if len(message) > 2000:
        return jsonify({"error": "Message is too long."}), 400

    result = client.direct_answer(int(thread_id), message.strip())

    return jsonify({
        "success": True,
        "thread_id": thread_id,
        "result": str(result) if result is not None else "sent",
    })


@app.get("/users/<username>/followers")
@require_api_key
def followers(username):
    user_id = client.user_id_from_username(username)
    limit = get_limit(20, 100)
    users = client.user_followers(user_id, amount=limit)

    return jsonify({
        "count": len(users),
        "users": [serialize_user(user) for user in users.values()],
    })


@app.get("/users/<username>/following")
@require_api_key
def following(username):
    user_id = client.user_id_from_username(username)
    limit = get_limit(20, 100)
    users = client.user_following(user_id, amount=limit)

    return jsonify({
        "count": len(users),
        "users": [serialize_user(user) for user in users.values()],
    })


@app.get("/media/<media_id>")
@require_api_key
def media_info(media_id):
    media = client.media_info(int(media_id))

    return jsonify({
        "id": str(model_value(media, "pk", media_id)),
        "caption": model_value(media, "caption_text", ""),
        "like_count": model_value(media, "like_count"),
        "comment_count": model_value(media, "comment_count"),
        "taken_at": str(model_value(media, "taken_at", "")),
    })


@app.get("/media/<media_id>/comments")
@require_api_key
def media_comments(media_id):
    limit = get_limit(20, 100)
    comments = client.media_comments(int(media_id), amount=limit)

    return jsonify({
        "count": len(comments),
        "comments": [
            {
                "id": str(model_value(comment, "pk", "")),
                "text": model_value(comment, "text", ""),
                "username": model_value(
                    model_value(comment, "user", {}),
                    "username",
                ),
            }
            for comment in comments
        ],
    })


if __name__ == "__main__":
    login()
    port = int(os.environ.get("PORT", "10000"))
    print(f"Instagram API starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
