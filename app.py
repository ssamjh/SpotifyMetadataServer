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

# Set logging level for Werkzeug (Flask's server) to ERROR
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)


def token_refresher(stop_event):
    while not stop_event.is_set():
        token_info = sp.auth_manager.get_cached_token()
        if token_info:
            sp.auth_manager.refresh_access_token(token_info["refresh_token"])
        time.sleep(300)  # Sleep for 5 minutes


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


@app.route("/setup", methods=["GET"])
def setup():
    # Start the Spotify authentication process
    auth_url = sp.auth_manager.get_authorize_url()
    return redirect(auth_url)


@app.route("/metadata", methods=["GET"])
def get_metadata():
    device_name = config.get("SPOTIFY", "DEVICE_NAME", fallback=None)
    playback = sp.current_playback()

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

    except SpotifyException:
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
    playback = sp.current_playback()
    if device_name and (not playback or playback["device"]["name"] != device_name):
        # If a device name is specified in the config and the current playback is not from that device
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
        
        artists = [{"id": artist["id"], "name": artist["name"]} for artist in track["artists"]]
        album = track["album"]
        
        track_info = {
            "current": {
                "song": track["name"],
                "songid": track["id"],
                "artist": artists,
                "album": album["name"],
                "albumid": album["id"],
                "cover": next((image["url"] for image in album["images"] if image["height"] == 300), "")
            }
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


stop_server = False

if __name__ == "__main__":
    stop_event = Event()
    refresher_thread = Thread(target=token_refresher, args=(stop_event,))
    refresher_thread.start()

    webbrowser.open("http://localhost:8080/setup")
    try:
        app.run(host="0.0.0.0", port=8080)
    finally:
        stop_event.set()
        refresher_thread.join()
