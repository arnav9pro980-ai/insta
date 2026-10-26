
import os
import json
import logging
import threading
import hmac
import requests

from flask import Flask, request, jsonify
from instagrapi import Client
from functools import wraps
from urllib.parse import quote

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

API_KEY = "myapi"

IG_USERNAME = os.environ.get("IG_USERNAME") or os.environ.get("EMAIL", "")
IG_PASSWORD = os.environ.get("IG_PASSWORD") or os.environ.get("PASSWORD", "")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "https://launcher-c813d-default-rtdb.europe-west1.firebasedatabase.app"
).rstrip("/")

SESSION_URL = DATABASE_URL + "/instagram_bot/session.json"
REQUEST_TIMEOUT = 25

client = Client()
client.delay_range = [2, 5]

instagram_ready = False
instagram_lock = threading.RLock()
session_lock = threading.Lock()


def serialize(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {str(k): serialize(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [serialize(v) for v in value]

    if hasattr(value, "model_dump"):
        return serialize(value.model_dump(mode="json"))

    if hasattr(value, "dict") and callable(value.dict):
        return serialize(value.dict())

    if hasattr(value, "__dict__"):
        return serialize({
            k: v for k, v in vars(value).items()
            if not k.startswith("_")
        })

    return str(value)


def save_session():
    try:
        settings = client.get_settings()

        response = requests.put(
            SESSION_URL,
            json=settings,
            timeout=REQUEST_TIMEOUT,
        )

        if response.ok:
            app.logger.info("Instagram session saved to Firebase.")
        else:
            app.logger.warning(
                "Could not save session to Firebase. HTTP %s",
                response.status_code,
            )

    except Exception:
        app.logger.exception("Failed to save Instagram session.")


def load_session():
    try:
        response = requests.get(
            SESSION_URL,
            timeout=REQUEST_TIMEOUT,
        )

        if not response.ok:
            app.logger.warning(
                "Firebase session request returned HTTP %s.",
                response.status_code,
            )
            return False

        settings = response.json()

        if not isinstance(settings, dict) or not settings:
            app.logger.info("No saved Instagram session was found.")
            return False

        client.set_settings(settings)
        app.logger.info("Loaded Instagram session settings from Firebase.")
        return True

    except Exception:
        app.logger.exception("Could not load Instagram session.")
        return False


def initialize_instagram():
    global instagram_ready

    with instagram_lock:
        instagram_ready = False

        session_loaded = load_session()

        if not IG_USERNAME or not IG_PASSWORD:
            app.logger.error(
                "Instagram credentials are missing. Set IG_USERNAME and "
                "IG_PASSWORD, or the existing EMAIL and PASSWORD variables."
            )
            return

        try:
            app.logger.info(
                "Attempting Instagram authentication. Saved session: %s",
                session_loaded,
            )

            client.login(IG_USERNAME, IG_PASSWORD)

            if not client.user_id:
                raise RuntimeError(
                    "Login returned without an authenticated user ID."
                )

            instagram_ready = True
            app.logger.info(
                "Instagram authentication succeeded. User ID present: %s",
                bool(client.user_id),
            )

            save_session()

        except Exception:
            instagram_ready = False
            app.logger.exception(
                "Instagram login failed. Check credentials, session status, "
                "Instagram checkpoints, and rate limits."
            )


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        supplied_key = request.headers.get("X-API-Key", "")

        if not supplied_key or not hmac.compare_digest(
            supplied_key, API_KEY
        ):
            return jsonify({"error": "Unauthorized"}), 401

        return fn(*args, **kwargs)

    return wrapper


def instagram_error(exc):
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)

    if status == 429 or "throttl" in type(exc).__name__.lower():
        return jsonify({
            "error": "Instagram rate limit reached",
            "message": "Stop retrying and try again later.",
        }), 429

    if status in (401, 403):
        return jsonify({
            "error": "Instagram rejected the request",
            "message": "The session may have expired or require verification.",
        }), 502

    if "login required" in str(exc).lower():
        return jsonify({
            "error": "Instagram client is not authenticated",
            "message": "Check the Render startup logs and login configuration.",
        }), 503

    app.logger.exception("Instagram endpoint failed", exc_info=exc)

    return jsonify({
        "error": "Instagram request failed",
        "type": type(exc).__name__,
        "message": str(exc)[:300],
    }), 502


def require_instagram(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not instagram_ready or not client.user_id:
            return jsonify({
                "error": "Instagram is not authenticated",
                "message": "Check Render logs for the startup login error.",
            }), 503

        try:
            with instagram_lock:
                if not instagram_ready or not client.user_id:
                    return jsonify({
                        "error": "Instagram session is unavailable",
                    }), 503

                return fn(*args, **kwargs)

        except Exception as exc:
            return instagram_error(exc)

    return wrapper


def get_limit(default=20, maximum=50):
    try:
        value = int(request.args.get("limit", default))
    except (ValueError, TypeError):
        value = default

    return max(1, min(value, maximum))


@app.get("/")
def index():
    return jsonify({
        "name": "Instagram API",
        "status": "online",
        "instagram_authenticated": instagram_ready,
        "endpoints": {
            "GET /": "API information",
            "GET /health": "Service and authentication status",
            "GET /me": "Current Instagram account",
            "GET /users/<username>": "Profile details",
            "GET /inbox?limit=20": "Direct message inbox",
            "GET /threads/<thread_id>/messages?limit=20": "Thread messages",
            "POST /threads/<thread_id>/messages": "Send a thread message",
            "GET /users/<username>/followers?limit=20": "Followers",
            "GET /users/<username>/following?limit=20": "Following",
            "GET /media/<media_id>": "Media information",
            "GET /media/<media_id>/comments?limit=20": "Media comments",
        },
        "authentication": "X-API-Key header required except / and /health",
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "online",
        "instagram_authenticated": instagram_ready,
        "user_id_present": bool(client.user_id),
        "firebase_configured": bool(DATABASE_URL),
    })


@app.get("/me")
@require_api_key
@require_instagram
def me():
    return jsonify(serialize(client.account_info()))


@app.get("/users/<username>")
@require_api_key
@require_instagram
def user_info(username):
    username = username.lstrip("@").strip()

    if not username:
        return jsonify({"error": "Username is required"}), 400

    user = client.user_info_by_username(username)
    return jsonify(serialize(user))


@app.get("/inbox")
@require_api_key
@require_instagram
def inbox():
    limit = get_limit(default=20, maximum=50)

    threads = client.direct_threads(
        amount=limit,
        thread_message_limit=10,
    )

    return jsonify({
        "count": len(threads),
        "threads": serialize(threads),
    })


@app.get("/threads/<thread_id>/messages")
@require_api_key
@require_instagram
def thread_messages(thread_id):
    limit = get_limit(default=30, maximum=100)

    messages = client.direct_messages(
        thread_id,
        amount=limit,
    )

    return jsonify({
        "thread_id": thread_id,
        "count": len(messages),
        "messages": serialize(messages),
    })


@app.post("/threads/<thread_id>/messages")
@require_api_key
@require_instagram
def send_message(thread_id):
    body = request.get_json(silent=True) or {}
    message = body.get("text")

    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "JSON body must contain non-empty text"}), 400

    if len(message) > 2000:
        return jsonify({"error": "Message is too long"}), 400

    result = client.direct_answer(thread_id, message.strip())

    return jsonify({
        "success": True,
        "thread_id": thread_id,
        "result": serialize(result),
    })


@app.get("/users/<username>/followers")
@require_api_key
@require_instagram
def followers(username):
    username = username.lstrip("@").strip()
    limit = get_limit(default=20, maximum=50)

    user_id = client.user_id_from_username(username)
    result = client.user_followers(user_id, amount=limit)

    return jsonify({
        "username": username,
        "count": len(result),
        "followers": serialize(result),
    })


@app.get("/users/<username>/following")
@require_api_key
@require_instagram
def following(username):
    username = username.lstrip("@").strip()
    limit = get_limit(default=20, maximum=50)

    user_id = client.user_id_from_username(username)
    result = client.user_following(user_id, amount=limit)

    return jsonify({
        "username": username,
        "count": len(result),
        "following": serialize(result),
    })


@app.get("/media/<media_id>")
@require_api_key
@require_instagram
def media_info(media_id):
    result = client.media_info(media_id)
    return jsonify(serialize(result))


@app.get("/media/<media_id>/comments")
@require_api_key
@require_instagram
def media_comments(media_id):
    limit = get_limit(default=20, maximum=50)

    comments = client.media_comments(
        media_id,
        amount=limit,
    )

    return jsonify({
        "media_id": media_id,
        "count": len(comments),
        "comments": serialize(comments),
    })


@app.errorhandler(404)
def not_found(_error):
    return jsonify({"error": "Endpoint not found"}), 404


@app.errorhandler(405)
def method_not_allowed(_error):
    return jsonify({"error": "Method not allowed"}), 405


@app.errorhandler(500)
def internal_error(_error):
    return jsonify({"error": "Internal server error"}), 500


initialize_instagram()
