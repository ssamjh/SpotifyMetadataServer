from flask import Flask, jsonify, request, redirect
import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth
from flask_caching import Cache
import configparser
import webbrowser
import time
from threading import Thread, Event


def token_refresher(stop_event):
    while not stop_event.is_set():
        token_info = sp.auth_manager.get_cached_token()
        if token_info:
            sp.auth_manager.refresh_access_token(token_info['refresh_token'])
        time.sleep(300)  # Sleep for 5 minutes


app = Flask(__name__)

# Read configuration from config.ini
config = configparser.ConfigParser()
config.read('config.ini')
client_id = config['SPOTIFY']['CLIENT_ID']
client_secret = config['SPOTIFY']['CLIENT_SECRET']
device_name = config['SPOTIFY'].get('DEVICE_NAME', None)

# Spotify API setup
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=client_id,
                                               client_secret=client_secret,
                                               redirect_uri="http://localhost:8080/callback",
                                               scope="user-read-playback-state app-remote-control user-modify-playback-state",
                                               cache_path="./token_cache.txt"))


# Setup Flask-Caching
cache = Cache(app, config={'CACHE_TYPE': 'simple'})


@app.route('/setup', methods=['GET'])
def setup():
    # Start the Spotify authentication process
    auth_url = sp.auth_manager.get_authorize_url()
    return redirect(auth_url)


@app.route('/get_metadata', methods=['GET'])
@cache.cached(timeout=10)
def get_metadata():
    device_name = config.get('SPOTIFY', 'DEVICE_NAME', fallback=None)
    playback = sp.current_playback()

    if device_name and (not playback or playback['device']['name'] != device_name):
        # If a device name is specified in the config and the current playback is not from that device
        return jsonify({
            "current": {
                "artist": [],
                "song": "",
                "album": "",
                "songid": "",
                "albumid": "",
                "cover": "",
                "playing": False
            },
            "queue": []
        })

    try:
        queue_data = sp.queue()

        # Extracting the metadata for the currently playing song
        track = queue_data['currently_playing']
        album = track.get("album", {})
        artists = [{
            "name": artist['name'],
            "id": artist['id']
        } for artist in track.get("artists", [])]

        current = {
            "artist": artists,
            "song": track.get("name"),
            "album": album.get("name"),
            "songid": track.get("id"),
            "albumid": album.get("id"),
            "cover": next((image['url'] for image in album.get("images", []) if image['height'] == 300), ""),
            "playing": True  # Assuming the currently playing song is always playing
        }

        # Extracting the queue
        queue = []
        for item in queue_data.get('queue', [])[:3]:
            if item:  # Check if the item is not None
                track = item
                album = track.get("album", {})
                artists = [{
                    "name": artist['name'],
                    "id": artist['id']
                } for artist in track.get("artists", [])]

                queue.append({
                    "artist": artists,
                    "song": track.get("name"),
                    "album": album.get("name"),
                    "songid": track.get("id"),
                    "albumid": album.get("id"),
                    "cover": next((image['url'] for image in album.get("images", []) if image['height'] == 64), ""),
                })

        return jsonify({"current": current, "queue": queue})

    except SpotifyException:
        # Return the default empty response if there is any exception (including token issues)
        return jsonify({
            "current": {
                "artist": [],
                "song": "",
                "album": "",
                "songid": "",
                "albumid": "",
                "cover": "",
                "playing": False
            },
            "queue": []
        })


@app.route('/add_queue', methods=['GET'])
def add_queue():
    playback = sp.current_playback()
    if device_name and (not playback or playback['device']['name'] != device_name):
        # If a device name is specified in the config and the current playback is not from that device
        return jsonify({"error": "Music is not playing from the specified device"}), 400

    track_id = request.args.get('trackid')
    if not track_id:
        return jsonify({"error": "trackid is required"}), 400

    try:
        sp.add_to_queue(uri=f"spotify:track:{track_id}")
        return jsonify({"message": "Song added to the queue successfully!"}), 200
    except SpotifyException as e:
        return jsonify({"error": str(e)}), 400


@app.route('/callback', methods=['GET'])
def callback():
    code = request.args.get('code')
    response_message = ""

    if code:
        token_info = sp.auth_manager.get_access_token(code)
        if token_info:
            # Manually save the expires_at to the token info
            token_info['expires_at'] = int(
                time.time()) + token_info['expires_in']
            sp.auth_manager._save_token_info(token_info)
            response_message = "Authentication successful! This window will close in 10 seconds."
        else:
            response_message = "Error during authentication."
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
        app.run(host='0.0.0.0', port=8080)
    finally:
        stop_event.set()
        refresher_thread.join()
