"""Non-secret, installation-specific exact-device identity."""
import json
import os
from pathlib import Path
ROOT=Path(__file__).resolve().parent
path=ROOT/'robot.json'
config=json.loads(path.read_text()) if path.exists() else {}
TARGET=config.get('sku','UNCONFIGURED')
FINGERPRINT=config.get('fingerprint','UNCONFIGURED')
ROBOT_NAME=config.get('name','Roomba')
PORT=int(config.get('port',8766))
BASE_URL=f'http://127.0.0.1:{PORT}'
SERVICE='io.vacuumgpt.irobot'
