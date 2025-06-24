from flask import Flask, jsonify, request, redirect
import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth
from flask_caching import Cache
import configparser
import webbrowser
import time
from threading import Thread, Event
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import functools

# Set logging level for Werkzeug (Flask's server) to ERROR
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

# Create a thread pool for timeout operations
executor = ThreadPoolExecutor(max_workers=10)


def with_timeout(timeout_seconds=1.0):
    """Decorator to add timeout to functions"""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            future = executor.submit(func, *args, **kwargs)
            try:
                return future.result(timeout=timeout_seconds)
            except TimeoutError:
                print(
                    f"Function {func.__name__} timed out after {timeout_seconds} seconds"
                )
                return None
            except Exception as e:
                print(f"Error in {func.__name__}: {e}")
                return None

        return wrapper

    return decorator


def get_current_playback_safe():
    """Safely get current playback with timeout"""
    try:

        @with_timeout(1.0)  # 1 second timeout
        def _get_playback():
            return sp.current_playback()

        return _get_playback()
    except Exception as e:
        print(f"Error getting playback: {e}")
        return None


def token_refresher(stop_event):
    while not stop_event.is_set():
        try:
            # Check if we have a valid auth manager
            if hasattr(sp, "auth_manager") and sp.auth_manager:
                token_info = sp.auth_manager.get_cached_token()

                # Only refresh if we have a valid token with a refresh_token
                if token_info and "refresh_token" in token_info:
                    # Check if token needs refreshing (e.g., if it expires in less than 10 minutes)
                    from datetime import datetime

                    if "expires_at" in token_info:
                        expires_at = token_info["expires_at"]
                        now = int(datetime.now().timestamp())

                        # Only refresh if token expires in less than 10 minutes
                        if expires_at - now < 600:
                            try:
                                sp.auth_manager.refresh_access_token(
                                    token_info["refresh_token"]
                                )
                                print("Token refreshed successfully")
                            except (Exception, requests.exceptions.Timeout) as e:
                                print(f"Error refreshing token: {e}")
                                # Don't crash the thread on refresh errors
                                pass

        except Exception as e:
            print(f"Error in token refresher: {e}")
            # Continue running even if there's an error

        time.sleep(300)  # Sleep for 5 minutes


app = Flask(__name__)

# Read configuration from config.ini
config = configparser.ConfigParser()
config.read("config.ini")
client_id = config["SPOTIFY"]["CLIENT_ID"]
client_secret = config["SPOTIFY"]["CLIENT_SECRET"]
device_name = config["SPOTIFY"].get("DEVICE_NAME", None)

# Spotify API setup with timeout
# requests_timeout=0.5 means all API calls will timeout after 500ms
sp = spotipy.Spotify(
    auth_manager=SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://127.0.0.1:8080/callback",
        scope="user-read-playback-state app-remote-control user-modify-playback-state",
        cache_path="./token_cache.txt",
    ),
    requests_timeout=0.5,  # 500ms timeout for all requests
)

# Setup Flask-Caching
cache = Cache(app, config={"CACHE_TYPE": "simple"})

# Global variables for thread management
refresher_thread = None
stop_event = Event()


@app.route("/setup", methods=["GET"])
def setup():
    # Start the Spotify authentication process
    auth_url = sp.auth_manager.get_authorize_url()
    return redirect(auth_url)


@app.route("/metadata", methods=["GET"])
def get_metadata():
    device_name = config.get("SPOTIFY", "DEVICE_NAME", fallback=None)

    try:
        playback = sp.current_playback()
    except (
        SpotifyException,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    ) as e:
        print(f"Error getting playback: {e}")
        # Return empty response on timeout or connection error
        return jsonify(
            {
                "current": {
                    "artist": [],
                    "song": "",
                    "album": "",
                    "songid": "",
                    "albumid": "",
                    "cover": "",
                    "playing": False,
                }
            }
        )

    if device_name and (not playback or playback["device"]["name"] != device_name):
        # If a device name is specified in the config and the current playback is not from that device
        return jsonify(
            {
                "current": {
                    "artist": [],
                    "song": "",
                    "album": "",
                    "songid": "",
                    "albumid": "",
                    "cover": "",
                    "playing": False,
                }
            }
        )

    try:
        track = playback["item"]
        album = track["album"]

        # Extracting the metadata for the currently playing song
        artists = [
            {"name": artist["name"], "id": artist["id"]} for artist in track["artists"]
        ]
        current = {
            "artist": artists,
            "song": track["name"],
            "album": album["name"],
            "songid": track["id"],
            "albumid": album["id"],
            "cover": next(
                (image["url"] for image in album["images"] if image["height"] == 300),
                "",
            ),
            "playing": playback["is_playing"],
        }

        return jsonify({"current": current})

    except (SpotifyException, AttributeError, TypeError, KeyError):
        # Return the default empty response if there is any exception (including token issues)
        return jsonify(
            {
                "current": {
                    "artist": [],
                    "song": "",
                    "album": "",
                    "songid": "",
                    "albumid": "",
                    "cover": "",
                    "playing": False,
                }
            }
        )


@app.route("/add", methods=["GET"])
def add_queue():
    try:
        playback = sp.current_playback()
    except (
        SpotifyException,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    ) as e:
        return jsonify({"error": f"Failed to get playback state: {str(e)}"}), 500

    if device_name and (not playback or playback["device"]["name"] != device_name):
        # If a device name is specified in the config and the current playback is not from that device
        return jsonify({"error": "Music is not playing from the specified device"}), 400

    track_id = request.args.get("trackid")
    if not track_id:
        return jsonify({"error": "trackid is required"}), 400

    try:
        sp.add_to_queue(uri=f"spotify:track:{track_id}")
        return jsonify({"message": "Song added to the queue successfully!"}), 200
    except (
        SpotifyException,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    ) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/search", methods=["GET"])
def search():
    query = request.args.get("q")
    if not query:
        return jsonify({"error": "Search query is required"}), 400

    try:
        results = sp.search(q=query, type="track", limit=10)
        tracks = results["tracks"]["items"]

        search_results = []
        for track in tracks:
            search_results.append(
                {
                    "id": track["id"],
                    "name": track["name"],
                    "artist": track["artists"][0]["name"],
                    "album": track["album"]["name"],
                    "cover": (
                        track["album"]["images"][0]["url"]
                        if track["album"]["images"]
                        else None
                    ),
                }
            )

        return jsonify({"results": search_results}), 200
    except (
        SpotifyException,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    ) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/skip", methods=["POST"])
def skip_track():
    try:
        playback = sp.current_playback()
        if not playback:
            return jsonify({"error": "No active playback found"}), 400

        config_device_name = config.get("SPOTIFY", "DEVICE_NAME", fallback=None)
        current_device_name = playback["device"]["name"]

        if config_device_name and current_device_name != config_device_name:
            return (
                jsonify(
                    {
                        "error": f"Music is not playing from the specified device ({config_device_name})"
                    }
                ),
                400,
            )

        sp.next_track()
        return jsonify({"message": "Skipped to next track"}), 200
    except (
        SpotifyException,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    ) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/trackinfo", methods=["GET"])
def get_track_info():
    track_id = request.args.get("trackid")
    if not track_id:
        return jsonify({"error": "trackid is required"}), 400
    try:
        track = sp.track(track_id)
        artists = [
            {"id": artist["id"], "name": artist["name"]} for artist in track["artists"]
        ]
        album = track["album"]
        track_info = {
            "song": track["name"],
            "songid": track["id"],
            "artist": artists,
            "album": album["name"],
            "albumid": album["id"],
            "cover": next(
                (image["url"] for image in album["images"] if image["height"] == 300),
                "",
            ),
        }
        return jsonify(track_info), 200
    except (
        SpotifyException,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    ) as e:
        return jsonify({"error": str(e)}), 400


@app.route("/callback", methods=["GET"])
def callback():
    code = request.args.get("code")
    response_message = ""

    if code:
        try:
            token_info = sp.auth_manager.get_access_token(code, as_dict=False)
            response_message = (
                "Authentication successful! This window will close in 10 seconds."
            )
            # Start the token refresher thread after successful authentication
            global refresher_thread
            if not refresher_thread or not refresher_thread.is_alive():
                refresher_thread = Thread(target=token_refresher, args=(stop_event,))
                refresher_thread.start()
                print("Started token refresher thread after authentication")
        except (Exception, requests.exceptions.Timeout) as e:
            response_message = f"Error during authentication: {str(e)}"
    else:
        response_message = "Error during authentication."

    return f"""
    <html>
    <head>
        <title>Spotify Callback</title>
        <script type="text/javascript">
            setTimeout(function() {{
                window.close();
            }}, 10000);
        </script>
    </head>
    <body>
        <p>{response_message}</p>
    </body>
    </html>
    """


@app.route("/auth_status", methods=["GET"])
def auth_status():
    try:
        token_info = sp.auth_manager.get_cached_token()
        if token_info and not sp.auth_manager.is_token_expired(token_info):
            return jsonify({"authenticated": True}), 200
        else:
            return jsonify({"authenticated": False}), 200
    except (Exception, requests.exceptions.Timeout):
        return jsonify({"authenticated": False}), 200


@app.route("/test", methods=["GET"])
def test_connection():
    """Test endpoint to check if Spotify connection works"""
    start_time = time.time()
    try:
        # Try a simple API call with timeout
        @with_timeout(2.0)
        def _test_call():
            return sp.current_user()

        user_info = _test_call()
        elapsed = time.time() - start_time

        if user_info:
            return (
                jsonify(
                    {
                        "status": "ok",
                        "user": user_info.get("display_name", "Unknown"),
                        "response_time": f"{elapsed:.2f}s",
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "status": "timeout",
                        "error": "API call timed out",
                        "response_time": f"{elapsed:.2f}s",
                    }
                ),
                500,
            )
    except Exception as e:
        elapsed = time.time() - start_time
        return (
            jsonify(
                {"status": "error", "error": str(e), "response_time": f"{elapsed:.2f}s"}
            ),
            500,
        )


if __name__ == "__main__":
    # Check if we already have a valid token
    try:
        token_info = sp.auth_manager.get_cached_token()
        if token_info and not sp.auth_manager.is_token_expired(token_info):
            # We have a valid token, start the refresher thread
            refresher_thread = Thread(target=token_refresher, args=(stop_event,))
            refresher_thread.start()
            print("Using existing authentication token")
        else:
            # Need to authenticate first
            webbrowser.open("http://127.0.0.1:8080/setup")
            print("Please authenticate in your browser")
    except:
        # No cached token, need to authenticate
        webbrowser.open("http://127.0.0.1:8080/setup")
        print("Please authenticate in your browser")

    try:
        app.run(host="0.0.0.0", port=8080)
    finally:
        stop_event.set()
        if refresher_thread and refresher_thread.is_alive():
            refresher_thread.join()
        # Shutdown the executor
        executor.shutdown(wait=True)
