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

# Spotify API setup
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=client_id,
                                               client_secret=client_secret,
                                               redirect_uri="http://localhost:8080/callback",
                                               scope="user-read-playback-state",
                                               cache_path="./token_cache.txt"))

# Setup Flask-Caching
cache = Cache(app, config={'CACHE_TYPE': 'simple'})


@app.route('/setup', methods=['GET'])
def setup():
    # Start the Spotify authentication process
    auth_url = sp.auth_manager.get_authorize_url()
    return redirect(auth_url)


@app.route('/current_song', methods=['GET'])
@cache.cached(timeout=10)
def current_song():
    try:
        playback_info = sp.current_playback()

        # Check if playback_info is None or if the playback is paused/stopped
        if not playback_info or not playback_info.get('is_playing', False):
            return jsonify({
                "artist": [],
                "song": "",
                "album": "",
                "songid": "",
                "albumid": "",
                "cover": "",
                "playing": False
            })

        # Extracting the metadata
        track = playback_info.get('item', {})
        album = track.get("album", {})
        artists = [{
            "name": artist['name'],
            "id": artist['id']
        } for artist in track.get("artists", [])]

        song_metadata = {
            "artist": artists,
            "song": track.get("name"),
            "album": album.get("name"),
            "songid": track.get("id"),
            "albumid": album.get("id"),
            "cover": next((image['url'] for image in album.get("images", []) if image['height'] == 300), ""),
            "playing": True
        }

        return jsonify(song_metadata)

    except SpotifyException:
        # Return the default empty response if there is any exception (including token issues)
        return jsonify({
            "artist": [],
            "song": "",
            "album": "",
            "songid": "",
            "albumid": "",
            "cover": "",
            "playing": False
        })


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
