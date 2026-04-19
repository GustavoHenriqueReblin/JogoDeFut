#!/usr/bin/env python3
import os
import sys
import subprocess

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as env_file:
            for line in env_file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

def install(pkg):
    subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

try:
    from flask import Flask, render_template, request, send_file, send_from_directory
    from flask_cors import CORS
except ImportError:
    print("Instalando dependências...")
    install("flask")
    install("flask-cors")
    from flask import Flask, render_template, request, send_file, send_from_directory
    from flask_cors import CORS

app = Flask(__name__)
CORS(app)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('PORT', 5000))
DEFAULT_URL = os.environ.get('START_URL', '')
current_url = DEFAULT_URL

@app.route("/")
def index():
    url = request.args.get('url', current_url)
    show_log = 'showLog' in request.args
    return render_template("player.html", url=url, show_log=show_log)

@app.route("/current_url")
def get_current_url():
    return current_url

@app.route("/manifest.json")
def manifest():
    return send_from_directory(BASE_DIR, 'manifest.json', mimetype='application/manifest+json')

@app.route("/sw.js")
def sw():
    return send_from_directory(BASE_DIR, 'sw.js', mimetype='application/javascript')

@app.route("/favicon.ico")
def favicon():
    return send_file(os.path.join(BASE_DIR, "static", "icons", "logo-48.png"), mimetype="image/png")

@app.route("/admin", methods=["GET", "POST"])
def admin():
    global current_url
    message = ""
    if request.method == "POST":
        new_url = request.form.get("url", "").strip()
        current_url = new_url
        message = f"URL atualizada com sucesso para: {new_url}"
    return render_template("admin.html", current_url=current_url, message=message)

if __name__ == "__main__":
    print(f"App is running on port {PORT}. Access via the Railway URL assigned to your project.")
    if DEFAULT_URL:
        print(f"Default stream URL from START_URL={DEFAULT_URL}")
    print(f"Current URL: {current_url}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
