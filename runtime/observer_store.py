"""Append-only local telemetry and map retention; no auth material."""
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import time

PRIVATE_KEYS = {"password", "user_cert", "secretkey", "sessiontoken", "iot_token", "iot_signature", "accesskeyid", "authorization", "token", "map_url", "livemap_url", "livemap_url_raw"}

def sanitize(value):
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items() if k.lower() not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str) and ("https://" in value or "http://" in value):
        return "[URL omitted]"
    return value

class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.db = sqlite3.connect(self.directory / "observer.sqlite3")
        os.chmod(self.directory / "observer.sqlite3", 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, mission TEXT, segment INTEGER, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS event_kind ON events(kind,id);
        CREATE TABLE IF NOT EXISTS maps (id INTEGER PRIMARY KEY, at REAL NOT NULL, mission TEXT, source TEXT, digest TEXT UNIQUE, filename TEXT NOT NULL, layers TEXT NOT NULL);
        """)
        self.db.commit()
        self.segment = int(self.db.execute("SELECT COALESCE(MAX(segment),0) FROM events").fetchone()[0]) + 1
        self.mission = None
        self.last = None
        self.map_id = None
        self.map_version = None
        self.event("segment_start", {"reason": "process_start_or_restart", "coordinate_continuity": "unknown"})

    def event(self, kind, data, observed_at=None):
        self.db.execute("INSERT INTO events(at,kind,mission,segment,data) VALUES(?,?,?,?,?)",
                        (time.time() if observed_at is None else observed_at, kind, self.mission, self.segment, json.dumps(sanitize(data))))
        self.db.commit()

    def set_mission(self, mission):
        if mission != self.mission:
            old = self.mission
            self.mission = mission
            self.segment += 1
            self.last = None
            self.event("segment_start", {"reason": "mission_identity_change", "previous_mission": old, "coordinate_continuity": "unknown"})

    def position(self, frame):
        frame = dict(frame)
        frame.setdefault('map_id', self.map_id)
        frame.setdefault('map_version', self.map_version)
        sequence = frame["sequence"]
        if self.last is not None:
            last_seq, last_time, last_points = self.last
            if sequence < last_seq:
                self.segment += 1
                self.event("coordinate_gap", {"reason": "sequence_reset", "previous_sequence": last_seq, "sequence": sequence})
            elif sequence > last_seq + last_points:
                self.event("coordinate_gap", {"reason": "sequence_gap", "previous_sequence": last_seq, "sequence": sequence, "missing_count_estimate": sequence-last_seq-last_points})
            if frame["timestamp"] - last_time > 15:
                self.event("coordinate_gap", {"reason": "timestamp_gap", "seconds": frame["timestamp"]-last_time})
        self.event("position", frame, frame["timestamp"])
        self.last = (sequence, frame["timestamp"], len(frame["points"]))

    def map_bundle(self, data, source, mission_id=None):
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("Map bundle exceeds retention limit")
        contents = {}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > 500:
                raise ValueError("Too many bundle members")
            total = 0
            for member in members:
                if not member.isfile():
                    continue
                total += member.size
                if total > 64 * 1024 * 1024:
                    raise ValueError("Expanded bundle exceeds retention limit")
                raw = archive.extractfile(member).read()
                try:
                    contents[member.name] = sanitize(json.loads(raw))
                except (ValueError, UnicodeError):
                    # Retain only known map raster formats, no executable or arbitrary binary content.
                    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                        import base64
                        contents[member.name] = {"png_base64": base64.b64encode(raw).decode()}
                    else:
                        contents[member.name] = {"unparsed_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        layers = []
        for filename, obj in contents.items():
            if isinstance(obj, dict):
                for feature in obj.get("features", []) + obj.get("experimentalFeatures", []):
                    if isinstance(feature, dict) and feature.get("filepath"):
                        target = feature["filepath"]
                        layers.append({"type": feature.get("type"), "filepath": target,
                                       "retained": target in contents or any(p.endswith('/'+target) for p in contents)})
            stem = Path(filename).stem
            if stem.lower() in ("coverage", "trajectories", "trajectory"):
                layers.append({"type": stem, "filepath": filename, "retained": True})
        bound_mission = self.mission if mission_id is None else mission_id
        document = {"source": source, "mission": bound_mission, "layers": layers, "files": contents,
                    "semantics": "Trajectory is observed travel. Coverage retains vendor data without inferring cleaned area from travel."}
        encoded = json.dumps(document, sort_keys=True).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        folder = self.directory / "maps"
        folder.mkdir(mode=0o700, exist_ok=True)
        filename = f"maps/{digest}.json"
        if not (self.directory / filename).exists():
            fd = os.open(self.directory / filename, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(encoded)
        self.db.execute("INSERT OR IGNORE INTO maps(at,mission,source,digest,filename,layers) VALUES(?,?,?,?,?,?)",
                        (time.time(), bound_mission, source, digest, filename, json.dumps(layers)))
        self.db.commit()
        self.event("map_retained", {"digest": digest, "source": source, "bound_mission": bound_mission, "layers": layers, "file_count": len(contents)})
        return layers

    def close(self):
        self.db.close()
