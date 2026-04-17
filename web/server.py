import logging
from flask import Flask
import threading

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/")
def index():
    return "Bot running"


@app.route("/health")
def health():
    return "OK"


def run_server(host="0.0.0.0", port=5000):
    logger.info(f"Starting Flask server on {host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


def run_in_background():
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    logger.info("Flask server started in background thread")