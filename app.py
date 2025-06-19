from flask import Flask, jsonify, request, redirect
import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth
from flask_caching import Cache
import configparser
import webbrowser
import time
from threading import Thread, Event, Lock
import logging
from datetime import datetime, timedelta

# Set logging level for Werkzeug (Flask's server) to ERROR
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

# Create a logger for our app
app_logger = logging.getLogger(__name__)
app_logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
app_logger.addHandler(handler)


class SpotifyStateManager:
    def __init__(self, sp, device_name=None, poll_interval=1.0):
        self.sp = sp
        self.device_name = device_name
        self.poll_interval = poll_interval
        self.state_lock = Lock()
        self.current_state = self._get_empty_state()
        self.last_update = None
        self.stop_event = Event()
        self.polling_thread = None

    def _get_empty_state(self):
        return {
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

    def start(self):
        """Start the background polling thread"""
        if self.polling_thread is None or not self.polling_thread.is_alive():
            self.stop_event.clear()
            self.polling_thread = Thread(target=self._poll_spotify, daemon=True)
            self.polling_thread.start()
            app_logger.info("Started Spotify state polling thread")

    def stop(self):
        """Stop the background polling thread"""
        self.stop_event.set()
        if self.polling_thread and self.polling_thread.is_alive():
            self.polling_thread.join()
            app_logger.info("Stopped Spotify state polling thread")

    def _poll_spotify(self):
        """Background thread that continuously polls Spotify for playback state"""
        consecutive_errors = 0

        while not self.stop_event.is_set():
            try:
                playback = self.sp.current_playback()

                with self.state_lock:
                    if not playback:
                        self.current_state = self._get_empty_state()
                    elif (
                        self.device_name
                        and playback["device"]["name"] != self.device_name
                    ):
                        self.current_state = self._get_empty_state()
                    else:
                        track = playback["item"]
                        album = track["album"]

                        artists = [
                            {"name": artist["name"], "id": artist["id"]}
                            for artist in track["artists"]
                        ]

                        self.current_state = {
                            "current": {
                                "artist": artists,
                                "song": track["name"],
                                "album": album["name"],
                                "songid": track["id"],
                                "albumid": album["id"],
                                "cover": (
                                    album["images"][0]["url"] if album["images"] else ""
                                ),
                                "playing": playback["is_playing"],
                            }
                        }

                    self.last_update = datetime.now()

                consecutive_errors = 0

            except SpotifyException as e:
                app_logger.error(f"Spotify API error: {e}")
                consecutive_errors += 1

                # Exponential backoff for consecutive errors
                if consecutive_errors > 5:
                    sleep_time = min(60, 2 ** (consecutive_errors - 5))
                    app_logger.warning(
                        f"Multiple consecutive errors, sleeping for {sleep_time}s"
                    )
                    time.sleep(sleep_time)

                with self.state_lock:
                    self.current_state = self._get_empty_state()

            except Exception as e:
                app_logger.error(f"Unexpected error in polling thread: {e}")
                with self.state_lock:
                    self.current_state = self._get_empty_state()

            # Sleep for the poll interval
            self.stop_event.wait(self.poll_interval)

    def get_current_state(self):
        """Get the current cached state"""
        with self.state_lock:
            return self.current_state.copy()

    def is_stale(self, max_age_seconds=10):
        """Check if the cached data is stale"""
        with self.state_lock:
            if self.last_update is None:
                return True
            return (datetime.now() - self.last_update).total_seconds() > max_age_seconds


def token_refresher(sp, stop_event):
    while not stop_event.is_set():
        try:
            token_info = sp.auth_manager.get_cached_token()
            if token_info:
                sp.auth_manager.refresh_access_token(token_info["refresh_token"])
                app_logger.info("Refreshed Spotify access token")
        except Exception as e:
            app_logger.error(f"Error refreshing token: {e}")

        stop_event.wait(300)  # Sleep for 5 minutes


app = Flask(__name__)

# Read configuration from config.ini
config = configparser.ConfigParser()
config.read("config.ini")
client_id = config["SPOTIFY"]["CLIENT_ID"]
client_secret = config["SPOTIFY"]["CLIENT_SECRET"]
device_name = config["SPOTIFY"].get("DEVICE_NAME", None)

# Spotify API setup
sp = spotipy.Spotify(
    auth_manager=SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="http://localhost:8080/callback",
        scope="user-read-playback-state app-remote-control user-modify-playback-state",
        cache_path="./token_cache.txt",
    )
)

# Setup Flask-Caching
cache = Cache(app, config={"CACHE_TYPE": "simple"})

# Initialize the Spotify state manager
# You can adjust the poll_interval (in seconds) based on your needs
# Lower values = more real-time but more API calls
state_manager = SpotifyStateManager(sp, device_name, poll_interval=0.5)


@app.route("/setup", methods=["GET"])
def setup():
    # Start the Spotify authentication process
    auth_url = sp.auth_manager.get_authorize_url()
    return redirect(auth_url)


@app.route("/metadata", methods=["GET"])
def get_metadata():
    """Get metadata from the cached state - instant response"""
    current_state = state_manager.get_current_state()

    # Optional: Add a staleness check
    if state_manager.is_stale(max_age_seconds=30):
        app_logger.warning("Cached data is stale")

    return jsonify(current_state)


@app.route("/add", methods=["GET"])
def add_queue():
    # Check current state from cache for device validation
    current_state = state_manager.get_current_state()

    if device_name and not current_state["current"]["playing"]:
        return jsonify({"error": "Music is not playing from the specified device"}), 400

    track_id = request.args.get("trackid")
    if not track_id:
        return jsonify({"error": "trackid is required"}), 400

    try:
        sp.add_to_queue(uri=f"spotify:track:{track_id}")
        return jsonify({"message": "Song added to the queue successfully!"}), 200
    except SpotifyException as e:
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
    except SpotifyException as e:
        return jsonify({"error": str(e)}), 400


@app.route("/skip", methods=["POST"])
def skip_track():
    try:
        # Use real-time check for skip functionality
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
    except SpotifyException as e:
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
            "cover": album["images"][0]["url"] if album["images"] else "",
        }
        return jsonify(track_info), 200
    except SpotifyException as e:
        return jsonify({"error": str(e)}), 400


@app.route("/callback", methods=["GET"])
def callback():
    code = request.args.get("code")
    response_message = ""

    if code:
        token_info = sp.auth_manager.get_access_token(code, as_dict=False)
        response_message = (
            "Authentication successful! This window will close in 10 seconds."
        )
        # Start the state manager after successful authentication
        state_manager.start()
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


@app.route("/status", methods=["GET"])
def get_status():
    """Health check endpoint to monitor the state manager"""
    is_stale = state_manager.is_stale()
    return jsonify(
        {
            "polling_active": state_manager.polling_thread
            and state_manager.polling_thread.is_alive(),
            "last_update": (
                state_manager.last_update.isoformat()
                if state_manager.last_update
                else None
            ),
            "data_stale": is_stale,
            "poll_interval": state_manager.poll_interval,
        }
    )


if __name__ == "__main__":
    token_refresh_stop_event = Event()
    refresher_thread = Thread(
        target=token_refresher,
        args=(
            sp,
            token_refresh_stop_event,
        ),
    )
    refresher_thread.start()

    # Check if we already have a valid token
    token_info = sp.auth_manager.get_cached_token()
    if token_info and not sp.auth_manager.is_token_expired(token_info):
        # Start the state manager immediately if we have a valid token
        state_manager.start()
        app_logger.info("Started with existing valid token")
    else:
        # Open browser for authentication
        webbrowser.open("http://localhost:8080/setup")

    try:
        app.run(host="0.0.0.0", port=8080)
    finally:
        # Clean shutdown
        app_logger.info("Shutting down...")
        state_manager.stop()
        token_refresh_stop_event.set()
        refresher_thread.join()
        app_logger.info("Shutdown complete")
