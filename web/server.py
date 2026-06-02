import logging
import os
import traceback
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
    try:
        logger.info(f"Starting Flask server on {host}:{port}")
        app.run(host=host, port=port, debug=False, use_reloader=False)
    except Exception:
        print("FATAL ERROR: Flask server failed to start")
        print(traceback.format_exc())
        logger.exception("Flask server failed to start")
        raise


def run_flask_in_thread(host="0.0.0.0", port=5000):
    port = int(os.getenv("PORT", port) or port)
    logger.info("Preparing Flask thread for host=%s port=%s", host, port)
    server_thread = threading.Thread(target=run_server, args=(host, port), daemon=True)
    server_thread.start()
    logger.info("Flask server started in background thread")
    return server_thread
