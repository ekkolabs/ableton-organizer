#!/usr/bin/env python3
"""
Ableton project overview.

Scans a folder of Ableton project folders and serves a browser UI with an
inline looping player, a BPM field and a notes field for each project.

Usage:
    python3 organizer.py [FOLDER] [--port 8765] [--rescan] [--no-open]
"""

import argparse
import base64
import ctypes
import ctypes.util
import gzip
import hashlib
import hmac
import html
import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ProcessPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")

# Notes, BPM cache and waveforms live in ".data" inside the library folder, not
# next to the code: Dropbox then syncs them, so a second machine reads BPMs and
# waveforms computed on the first without downloading a single .als or .wav.
# Keys are relative to the library root and stamped with mtime+size, both of
# which Dropbox preserves, so they match across machines.
DATA_DIRNAME = ".data"
ROOT = None
DATA_DIR = os.path.join(APP_DIR, "data")
PEAKS_DIR = os.path.join(DATA_DIR, "peaks")
CACHE_PATH = os.path.join(DATA_DIR, "cache.json")
META_PATH = os.path.join(DATA_DIR, "meta.json")


def bind_data_dir(root):
    """Point the data paths at <root>/.data, falling back if it isn't writable."""
    global ROOT, DATA_DIR, PEAKS_DIR, CACHE_PATH, META_PATH
    ROOT = root
    target = os.path.join(root, DATA_DIRNAME)
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, ".writable")
        with open(probe, "w"):
            pass
        os.remove(probe)
        DATA_DIR = target
    except OSError:
        DATA_DIR = os.path.join(APP_DIR, "data")
        os.makedirs(DATA_DIR, exist_ok=True)
    PEAKS_DIR = os.path.join(DATA_DIR, "peaks")
    CACHE_PATH = os.path.join(DATA_DIR, "cache.json")
    META_PATH = os.path.join(DATA_DIR, "meta.json")

    # carry over anything from the old app-local location, once
    legacy = os.path.join(APP_DIR, "data", "meta.json")
    if DATA_DIR != os.path.join(APP_DIR, "data") and os.path.exists(legacy) \
            and not os.path.exists(META_PATH):
        try:
            shutil.copy2(legacy, META_PATH)
            print("  migrated notes from %s" % legacy)
        except OSError:
            pass
    return DATA_DIR


def read_json_bounded(path, seconds=20):
    """Load JSON, giving up if the file doesn't arrive in time.

    A cloud placeholder hydrates on read, and a sick sync daemon can make that
    block forever. The database is a cache, so continuing without it is always
    better than hanging.
    """
    if not os.path.exists(path):
        return None
    result = {}

    def work():
        try:
            with open(path) as fh:
                result["data"] = json.load(fh)
        except Exception as exc:
            result["error"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        print("  warning: %s did not load in %ds (cloud file not syncing?) — "
              "continuing without it" % (os.path.basename(path), seconds))
        return None
    return result.get("data")


def rel_key(path):
    """Path relative to the library root, so cache keys match on any machine."""
    try:
        return os.path.relpath(path, ROOT) if ROOT else path
    except ValueError:
        return path


def stamp_of(path):
    """mtime+size, readable on a cloud placeholder without downloading it."""
    st = os.stat(path, follow_symlinks=False)
    return "%d|%d" % (int(st.st_mtime), st.st_size)

DEFAULT_ROOT = os.path.expanduser(
    os.environ.get("ORGANIZER_ROOT") or "~/Music/Ableton Projects"
)

# .m4a is deliberately absent: at project root those are voice memos that were
# sampled into the track, never the bounce.
AUDIO_EXTS = (".wav", ".mp3", ".aif", ".aiff", ".flac")
OUTPUT_DIRNAME = "_OUTPUT"
# top-level folders that are containers, not projects (matched case-insensitively)
DEFAULT_IGNORE = {"archive"}

#==============================================================================
# Optional single-password gate. Off unless a password is supplied, so running
# it on your own machine is unchanged; required before putting it on the
# internet, where the only thing between a stranger and the library is this.

AUTH_PASSWORD = None
AUTH_SECRET = None
SESSION_COOKIE = "organizer_session"
SESSION_DAYS = 14
LOGIN_PATH = "/login"

_attempts = {}                    # client -> [failures, first failure time]
ATTEMPT_LIMIT = 8
ATTEMPT_WINDOW = 900              # seconds


def enable_auth(password):
    """Derive the signing key from the password so sessions survive a restart
    but die the moment the password changes."""
    global AUTH_PASSWORD, AUTH_SECRET
    AUTH_PASSWORD = password
    AUTH_SECRET = hashlib.sha256(b"organizer-session-v1|" + password.encode("utf-8")).digest()


def issue_token():
    expiry = str(int(time.time() + SESSION_DAYS * 86400))
    sig = hmac.new(AUTH_SECRET, expiry.encode(), hashlib.sha256).hexdigest()[:32]
    return expiry + "." + sig


def token_valid(token):
    if not AUTH_SECRET or not token or "." not in token:
        return False
    expiry, _, sig = token.partition(".")
    try:
        if int(expiry) < time.time():
            return False
    except ValueError:
        return False
    want = hmac.new(AUTH_SECRET, expiry.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, want)


def throttled(client):
    hit = _attempts.get(client)
    if not hit:
        return False
    if time.time() - hit[1] > ATTEMPT_WINDOW:
        _attempts.pop(client, None)
        return False
    return hit[0] >= ATTEMPT_LIMIT


def note_failure(client):
    hit = _attempts.get(client)
    if not hit or time.time() - hit[1] > ATTEMPT_WINDOW:
        _attempts[client] = [1, time.time()]
    else:
        hit[0] += 1


LOGIN_PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Ableton Projects</title><link rel=icon href="/favicon.png">
<style>
 body{background:#0b0c0e;color:#e9ebee;font:14px -apple-system,BlinkMacSystemFont,sans-serif;
      height:100vh;margin:0;display:grid;place-items:center}
 form{display:flex;flex-direction:column;gap:12px;width:250px;text-align:center}
 h1{font-size:15px;font-weight:600;margin:0 0 4px}
 input{background:#0d0e11;border:1px solid #23262c;border-radius:6px;padding:9px 11px;
       color:#e9ebee;font-size:13px;outline:none}
 input:focus{border-color:#3d434c;box-shadow:0 0 0 3px rgba(200,247,81,.07)}
 button{background:rgba(200,247,81,.13);border:1px solid rgba(200,247,81,.35);color:#c8f751;
        border-radius:6px;padding:9px;cursor:pointer;font-size:13px}
 button:hover{background:rgba(200,247,81,.22)}
 .err{color:#e8705d;font:11px ui-monospace,Menlo,monospace;min-height:14px}
</style>
<form method=post action="/login">
 <h1>Ableton Projects</h1>
 <input type=password name=password placeholder="Password" autofocus
        autocomplete=current-password autocapitalize=none autocorrect=off spellcheck=false>
 <button type=submit>Enter</button>
 <div class=err>__ERROR__</div>
</form>"""


STATIC_MIME = {
    ".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml",
    ".webp": "image/webp", ".css": "text/css", ".js": "text/javascript",
}

MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".flac": "audio/flac",
}


# ---------------------------------------------------------------- utilities

def norm_name(s):
    """Normalise a project or bounce name so the two can be matched."""
    s = s.lower().strip()
    s = re.sub(r"\s*project\s*$", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


# macOS marks cloud placeholders (Dropbox online-only, iCloud evicted) with
# SF_DATALESS. Reading one blocks while it downloads, so bulk scanning skips
# them; clicking play is an explicit choice and is allowed to pull the file.
SF_DATALESS = 0x40000000
MATERIALIZE = False


def is_cloud_only(path):
    """True if the file is a placeholder whose bytes aren't on this disk."""
    if MATERIALIZE:
        return False
    try:
        return bool(os.stat(path, follow_symlinks=False).st_flags & SF_DATALESS)
    except OSError:
        return False


_LIBC = None


def _getxattr(path, name):
    """macOS getxattr via ctypes — os.getxattr is Linux-only."""
    global _LIBC
    if _LIBC is None:
        _LIBC = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _LIBC.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        _LIBC.getxattr.restype = ctypes.c_ssize_t
    cpath, cname = path.encode("utf-8"), name.encode("utf-8")
    size = _LIBC.getxattr(cpath, cname, None, 0, 0, 0)
    if size <= 0:
        return None
    buf = ctypes.create_string_buffer(size)
    got = _LIBC.getxattr(cpath, cname, buf, size, 0, 0)
    return buf.raw[:got] if got > 0 else None


def finder_tags(path):
    """Finder tags as [{name, color}]. Stored as "Name\nIndex" in a binary plist.

    The index is what matters: the same colour is named differently per
    language (Purple / Roxo both carry 3), so colour 0-7 is the stable key.
    """
    raw = _getxattr(path, "com.apple.metadata:_kMDItemUserTags")
    if not raw:
        return []
    try:
        items = plistlib.loads(raw)
    except Exception:
        return []
    out = []
    for item in items:
        if not isinstance(item, str):
            continue
        name, _, idx = item.partition("\n")
        try:
            color = int(idx)
        except ValueError:
            color = 0
        if name or color:
            out.append({"name": name or None, "color": color})
    return out


def als_tempo(path):
    """Read the song tempo out of a gzipped .als by finding the main track."""
    try:
        with gzip.open(path, "rb") as fh:
            buf = b""
            anchored = False
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return None
                buf += chunk
                if not anchored:
                    i = buf.find(b"<MainTrack")
                    if i < 0:
                        i = buf.find(b"<MasterTrack")
                    if i >= 0:
                        anchored = True
                        buf = buf[i:]
                    elif len(buf) > (1 << 20):
                        # keep an overlap so a split tag is still found
                        buf = buf[-4096:]
                if anchored:
                    m = re.search(rb'<Tempo>.*?<Manual Value="([0-9.]+)"', buf, re.S)
                    if m:
                        return round(float(m.group(1)), 3)
                    if len(buf) > (1 << 22):
                        return None
    except Exception:
        return None


def audio_duration(path):
    """Duration in seconds via CoreAudio, falling back to a WAV header parse."""
    try:
        out = subprocess.run(
            ["afinfo", path], capture_output=True, text=True, timeout=20
        ).stdout
        m = re.search(r"estimated duration:\s*([0-9.]+)\s*sec", out)
        if m:
            return round(float(m.group(1)), 2)
    except Exception:
        pass
    if path.lower().endswith(".wav"):
        try:
            with open(path, "rb") as fh:
                head = fh.read(65536)
            rate = chans = bits = None
            pos = 12
            while pos + 8 <= len(head):
                cid = head[pos:pos + 4]
                size = struct.unpack("<I", head[pos + 4:pos + 8])[0]
                if cid == b"fmt ":
                    chans, rate = struct.unpack("<HI", head[pos + 10:pos + 16])
                    bits = struct.unpack("<H", head[pos + 22:pos + 24])[0]
                elif cid == b"data" and rate:
                    nbytes = size or (os.path.getsize(path) - pos - 8)
                    return round(nbytes / (rate * chans * max(bits, 8) / 8), 2)
                pos += 8 + size + (size & 1)
        except Exception:
            pass
    return None


def audio_stamp(path):
    """(created, size) for a bounce; creation time is what dates a version."""
    st = os.stat(path, follow_symlinks=False)
    return (getattr(st, "st_birthtime", None) or st.st_mtime), st.st_size


def collect_versions(pdir, entries, folder, output_group, cache, materialize):
    """Every bounce for this project — project root first, then _OUTPUT — newest first.

    Anything audio in the folder is listed rather than guessed at: some of these
    are genuine earlier bounces, some are reference material the name makes
    obvious, and no rule separates them reliably.
    """
    seen, out = set(), []

    paths = [os.path.join(pdir, f) for f in sorted(entries)
             if not f.startswith(".") and f.lower().endswith(AUDIO_EXTS)]
    paths += list(output_group or [])

    for fp in paths:
        real = os.path.realpath(fp)
        if real in seen:
            continue
        seen.add(real)

        try:
            created, size = audio_stamp(fp)
        except OSError:
            continue

        cloud = is_cloud_only(fp)
        duration = None
        key = None
        try:
            key = "dur|%s|%s" % (rel_key(fp), stamp_of(fp))
        except OSError:
            pass
        hit = cache.get(key) if key else None
        if hit is not None:
            duration = hit.get("duration")
        elif not cloud:
            duration = audio_duration(fp)

        out.append({
            "path": fp,
            "name": os.path.basename(fp),
            "created": created,
            "size": size,
            "duration": duration,
            "cloud": cloud,
            "source": "output" if os.path.basename(os.path.dirname(fp)) == OUTPUT_DIRNAME
                      else "bounce",
            "durKey": key,
        })

    out.sort(key=lambda v: v["created"], reverse=True)
    return out


def scan_one(args):
    """Worker: describe a single project folder. Runs in a subprocess."""
    root, folder, output_index, output_groups, cache, materialize = args
    global MATERIALIZE, ROOT
    MATERIALIZE = materialize
    ROOT = root
    pdir = os.path.join(root, folder)
    try:
        entries = os.listdir(pdir)
    except OSError:
        return None

    # --- the Live set: newest .als at the project root -------------------
    sets = []
    for f in entries:
        if f.lower().endswith(".als") and not f.startswith("."):
            fp = os.path.join(pdir, f)
            try:
                sets.append((os.path.getmtime(fp), fp, f))
            except OSError:
                pass
    sets.sort(reverse=True)
    als_path = sets[0][1] if sets else None
    als_name = sets[0][2] if sets else None
    als_mtime = sets[0][0] if sets else None

    bpm = None
    cloud_als = bool(als_path) and is_cloud_only(als_path)
    cache_entries = []
    if als_path:
        try:
            key = "bpm|%s|%s" % (rel_key(als_path), stamp_of(als_path))
        except OSError:
            key = None
        hit = cache.get(key) if key else None
        if hit is not None:
            bpm = hit.get("bpm")          # from a sibling machine, maybe
        elif not cloud_als:
            bpm = als_tempo(als_path)
            if key:
                cache_entries.append((key, {"bpm": bpm}))

    # --- the audio to loop ----------------------------------------------
    # 1. a bounce sitting at the project root
    # 2. a name-matched bounce in _OUTPUT/
    # Bounces only — never a file from Samples/.
    audio = None
    source = None
    roots = []
    for f in entries:
        if f.startswith("._") or f.startswith("."):
            continue
        if f.lower().endswith(AUDIO_EXTS):
            fp = os.path.join(pdir, f)
            try:
                roots.append((os.path.getmtime(fp), fp))
            except OSError:
                pass
    if roots:
        roots.sort(reverse=True)
        audio = roots[0][1]
        source = "bounce"
    else:
        cand = output_index.get(norm_name(folder))
        if cand:
            audio = cand
            source = "output"
        # Nothing else: Samples/ holds one-shots and loops, not the track, so a
        # project with no bounce is reported as having no audio.

    cloud_audio = bool(audio) and is_cloud_only(audio)
    duration = None
    if audio:
        try:
            akey = "dur|%s|%s" % (rel_key(audio), stamp_of(audio))
        except OSError:
            akey = None
        hit = cache.get(akey) if akey else None
        if hit is not None:
            duration = hit.get("duration")
        elif not cloud_audio:
            duration = audio_duration(audio)
            if akey:
                cache_entries.append((akey, {"duration": duration}))
    size = None
    audio_created = None
    if audio:
        try:
            ast_ = os.stat(audio, follow_symlinks=False)
            size = ast_.st_size
            # when the bounce was made; falls back to mtime off HFS/APFS
            audio_created = getattr(ast_, "st_birthtime", None) or ast_.st_mtime
        except OSError:
            pass

    versions = collect_versions (pdir, entries, folder,
                                output_groups.get (norm_name (folder)), cache, materialize)

    for v in versions:                       # durations computed here also feed the cache
        if v["durKey"] and v["duration"] is not None and v["durKey"] not in cache:
            cache_entries.append((v["durKey"], {"duration": v["duration"]}))
        v.pop("durKey", None)

    try:
        folder_mtime = os.path.getmtime(pdir)
    except OSError:
        folder_mtime = 0
    modified = max([t for t in (folder_mtime, als_mtime) if t] or [0])

    return {
        "id": hashlib.sha1(folder.encode("utf-8")).hexdigest()[:12],
        "key": folder,
        "name": re.sub(r"\s*[Pp]roject\s*$", "", folder).strip() or folder,
        "folder": pdir,
        "als": als_path,
        "alsName": als_name,
        "bpm": bpm,
        "audio": audio,
        "audioName": os.path.basename(audio) if audio else None,
        "audioSource": source,
        "duration": duration,
        "size": size,
        "audioCreated": audio_created,
        "modified": modified,
        "finderTags": finder_tags(pdir),
        "cloudAudio": cloud_audio,
        "cloudAls": cloud_als,
        "versions": versions,
    }, cache_entries


def build_output_groups(root):
    """Map normalised names -> every matching file inside _OUTPUT/, newest last."""
    odir = os.path.join(root, OUTPUT_DIRNAME)
    groups = {}
    if not os.path.isdir(odir):
        return groups
    for f in sorted(os.listdir(odir)):
        if f.startswith(".") or not f.lower().endswith(AUDIO_EXTS):
            continue
        base = re.sub(r"\.(mp3|wav|aif|aiff|flac)+$", "", f, flags=re.I)
        base = re.sub(r"^_+", "", base)
        base = re.sub(r"[\s_-]*\d+$", "", base)
        key = norm_name(base)
        if key:
            groups.setdefault(key, []).append(os.path.join(odir, f))
    return groups


def build_output_index(root):
    """Map normalised names -> best bounce file inside _OUTPUT/."""
    odir = os.path.join(root, OUTPUT_DIRNAME)
    index = {}
    if not os.path.isdir(odir):
        return index
    for f in sorted(os.listdir(odir)):
        if f.startswith(".") or not f.lower().endswith(AUDIO_EXTS):
            continue
        base = re.sub(r"\.(mp3|wav|aif|aiff|flac)+$", "", f, flags=re.I)
        base = re.sub(r"^_+", "", base)
        base = re.sub(r"[\s_-]*\d+$", "", base)
        key = norm_name(base)
        if not key:
            continue
        fp = os.path.join(odir, f)
        prev = index.get(key)
        # prefer wav over mp3, then the newer file
        def rank(p):
            return (1 if p.lower().endswith(".wav") else 0, os.path.getmtime(p))
        if prev is None or rank(fp) > rank(prev):
            index[key] = fp
    return index


def scan(root, progress=None, ignore=None, materialize=False):
    os.makedirs(DATA_DIR, exist_ok=True)
    ignore = {n.lower() for n in (ignore if ignore is not None else DEFAULT_IGNORE)}
    cache = read_json_bounded(CACHE_PATH) or {}

    folders = sorted(
        f for f in os.listdir(root)
        if os.path.isdir(os.path.join(root, f))
        and not f.startswith("_")
        and not f.startswith(".")
        and f.lower() not in ignore
    )
    output_index = build_output_index(root)
    output_groups = build_output_groups(root)
    jobs = [(root, f, output_index, output_groups, cache, materialize) for f in folders]

    projects = []
    done = 0
    workers = min(8, (os.cpu_count() or 4))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(scan_one, jobs, chunksize=1):
            done += 1
            if progress:
                progress(done, len(jobs))
            if not res:
                continue
            proj, entries = res
            projects.append(proj)
            for key, value in entries:
                cache[key] = value

    with open(CACHE_PATH, "w") as fh:
        json.dump(cache, fh)
    projects.sort(key=lambda p: p["modified"], reverse=True)
    return projects


# ------------------------------------------------------------------ peaks

def compute_peaks(path, buckets=800):
    """Waveform peaks via a tiny 4 kHz mono downmix made by afconvert."""
    os.makedirs(PEAKS_DIR, exist_ok=True)
    try:
        stamp = stamp_of(path)
    except OSError:
        return None
    # keyed on the relative path so a waveform computed on another machine,
    # synced through .data/, is found here
    digest = hashlib.sha1((rel_key(path) + "|" + stamp).encode("utf-8")).hexdigest()[:16]
    cached = os.path.join(PEAKS_DIR, digest + ".json")
    hit = read_json_bounded(cached, 10)
    if hit is not None:
        return hit
    # only now does it cost a download
    if is_cloud_only(path):
        return None

    tmp = os.path.join(PEAKS_DIR, digest + ".tmp.wav")
    try:
        r = subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@4000", "-c", "1", path, tmp],
            capture_output=True, timeout=120,
        )
        if r.returncode != 0 or not os.path.exists(tmp):
            return None
        with open(tmp, "rb") as fh:
            raw = fh.read()
        # find the data chunk
        pos, start, size = 12, None, 0
        while pos + 8 <= len(raw):
            cid = raw[pos:pos + 4]
            csz = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
            if cid == b"data":
                start, size = pos + 8, csz
                break
            pos += 8 + csz + (csz & 1)
        if start is None:
            return None
        size = min(size or (len(raw) - start), len(raw) - start)
        n = size // 2
        if n <= 0:
            return None
        samples = struct.unpack("<%dh" % n, raw[start:start + n * 2])
        step = max(1, n // buckets)
        out = []
        for i in range(0, n, step):
            window = samples[i:i + step]
            if not window:
                break
            out.append(round(max(max(window), -min(window)) / 32768.0, 4))
        with open(cached, "w") as fh:
            json.dump(out, fh)
        return out
    except Exception:
        return None
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ------------------------------------------------------------------ state

class Library:
    def __init__(self, root, ignore=None, materialize=False):
        self.root = root
        self.ignore = ignore if ignore is not None else set(DEFAULT_IGNORE)
        self.materialize = materialize
        self.lock = threading.Lock()
        self.projects = []
        self.by_id = {}
        self.scanning = False
        self.progress = (0, 0)

    def load_or_scan(self, force=False):
        self.refresh(force)

    def refresh(self, force=False):
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        try:
            def prog(d, t):
                self.progress = (d, t)
            projects = scan(self.root, prog, self.ignore, self.materialize)
            with self.lock:
                self.projects = projects
                self.by_id = {p["id"]: p for p in projects}
        finally:
            self.scanning = False


def load_meta():
    return read_json_bounded(META_PATH) or {}


META_LOCK = threading.Lock()


def save_meta(meta):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = META_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(meta, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, META_PATH)


# ----------------------------------------------------------------- server

class Server(ThreadingHTTPServer):
    daemon_threads = True
    tls_context = None          # set when serving https

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        # Clients hang up, scanners probe, handshakes fail. None of it is a
        # server fault and none of it deserves a traceback in the log.
        if isinstance(exc, (ssl.SSLError, BrokenPipeError, ConnectionResetError,
                            TimeoutError, socket.timeout)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 120               # bounds an idle keep-alive or a stalled read
    library = None

    def setup(self):
        ctx = getattr(self.server, "tls_context", None)
        if ctx is not None:
            # Wrap the accepted socket here rather than the listening socket.
            # setup() runs in this connection's own thread, so a client that
            # opens a socket and never completes the handshake stalls only
            # itself. Wrapping the listener does the handshake inside accept()
            # on the main thread, where one dead connection blocks everything.
            self.request.settimeout(20)
            self.request = ctx.wrap_socket(self.request, server_side=True)
            self.connection = self.request
        super().setup()

    def log_message(self, fmt, *a):
        pass

    # -- helpers
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_range(self, path):
        """Serve audio with Range support so seeking and looping work."""
        try:
            size = os.path.getsize(path)
        except OSError:
            self.send_error(404)
            return
        ctype = MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m:
                g1, g2 = m.group(1), m.group(2)
                if g1:
                    start = int(g1)
                    if g2:
                        end = min(int(g2), size - 1)
                elif g2:
                    start = max(0, size - int(g2))
                partial = True
        if start >= size:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- auth
    def client_id(self):
        # Behind a tunnel every request arrives from localhost; the edge passes
        # the real address on. Used for throttling only, never for access.
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def is_authed(self):
        if AUTH_PASSWORD is None:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and token_valid(value):
                return True
        return False

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1") \
            and not self.headers.get("CF-Connecting-IP")

    def send_redirect(self, location, cookie=None):
        # HTTP/1.1 keep-alive: without an explicit length the client waits for a
        # body that never arrives, and the navigation just hangs.
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def send_login(self, error="", status=200):
        body = LOGIN_PAGE.replace("__ERROR__", html.escape(error)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def reject(self, path):
        """API callers get a status they can act on; a browser gets the form."""
        if path.startswith("/api/"):
            self.send_json({"error": "authentication required"}, 401)
        else:
            self.send_login()

    def project_from(self, path, prefix):
        pid = path[len(prefix):].split("/")[0]
        return self.library.by_id.get(pid)

    def audio_from(self, path, prefix):
        """The primary bounce, or version n when the path carries an index."""
        parts = path[len(prefix):].split("/")
        project = self.library.by_id.get(parts[0])
        if project is None:
            return None
        if len(parts) < 2 or parts[1] == "":
            return project.get("audio")
        try:
            return project["versions"][int(parts[1])]["path"]
        except (ValueError, IndexError, KeyError):
            return None

    # -- routing
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path == LOGIN_PATH:
            if AUTH_PASSWORD:
                self.send_login()
            else:
                self.send_redirect("/")
            return

        if not self.is_authed():
            # the icon is needed by the login page itself
            if path in ("/favicon.png", "/favicon.ico"):
                pass
            else:
                self.reject(path)
                return

        if path == "/":
            self.send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
            return

        # icons and any other asset living beside index.html
        if path.startswith("/static/") or path in ("/favicon.ico", "/favicon.png",
                                                   "/apple-touch-icon.png"):
            name = os.path.basename(path)
            if name == "favicon.ico":
                name = "favicon.png"        # we ship a png
            fp = os.path.join(STATIC_DIR, name)
            if os.path.isfile(fp) and os.path.dirname(os.path.abspath(fp)) == STATIC_DIR:
                ext = os.path.splitext(name)[1].lower()
                self.send_file(fp, STATIC_MIME.get(ext, "application/octet-stream"))
            else:
                self.send_error(404)
            return

        if path == "/api/library":
            meta = load_meta()
            with self.library.lock:
                projects = list(self.library.projects)
            out = []
            for p in projects:
                d = dict(p)
                d.pop("folder", None)
                d.pop("als", None)
                d.pop("audio", None)
                d["versions"] = [{k: v for k, v in item.items() if k != "path"}
                                 for item in p.get("versions", [])]
                m = meta.get(p["key"], {})
                d["notes"] = m.get("notes", "")
                d["stars"] = int(m.get("stars", 5 if m.get("starred") else 0))
                d["hidden"] = bool(m.get("hidden"))
                out.append(d)
            self.send_json({
                "root": self.library.root,
                "projects": out,
                "scanning": self.library.scanning,
            })
            return

        if path == "/api/status":
            self.send_json({
                "scanning": self.library.scanning,
                "progress": self.library.progress,
                "count": len(self.library.projects),
            })
            return

        if path.startswith("/api/audio/"):
            target = self.audio_from(path, "/api/audio/")
            if not target:
                self.send_error(404)
                return
            self.send_range(target)
            return

        if path.startswith("/api/peaks/"):
            target = self.audio_from(path, "/api/peaks/")
            if not target:
                self.send_json({"peaks": None}, 404)
                return
            self.send_json({"peaks": compute_peaks(target)})
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"

        if path == LOGIN_PATH:
            if AUTH_PASSWORD is None:
                self.send_redirect("/")
                return
            who = self.client_id()
            if throttled(who):
                sys.stderr.write("login THROTTLED from %s\n" % who)
                sys.stderr.flush()
                self.send_login("Too many attempts. Wait 15 minutes.", 429)
                return
            fields = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
            given = (fields.get("password") or [""])[0]
            agent = (self.headers.get("User-Agent") or "?")[:60]
            ok = hmac.compare_digest(given, AUTH_PASSWORD)
            sys.stderr.write("login %s from %s len=%d ua=%s\n"
                             % ("OK" if ok else "FAILED", who, len(given), agent))
            sys.stderr.flush()

            if ok:
                _attempts.pop(who, None)
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" \
                                    or getattr(self.server, "tls_context", None) else ""
                self.send_redirect("/",
                                   "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d%s"
                                   % (SESSION_COOKIE, issue_token(), SESSION_DAYS * 86400, secure))
            else:   # noqa: E301
                note_failure(who)
                self.send_login("Wrong password.", 401)
            return

        if not self.is_authed():
            self.reject(path)
            return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        if path.startswith("/api/meta/"):
            pid = path[len("/api/meta/"):]
            p = self.library.by_id.get(pid)
            if not p:
                self.send_error(404)
                return
            with META_LOCK:
                meta = load_meta()
                entry = meta.get(p["key"], {})
                if "notes" in payload:
                    entry["notes"] = str(payload["notes"])[:20000]
                if "stars" in payload:
                    try:
                        n = max(0, min(5, int(payload["stars"])))
                    except (TypeError, ValueError):
                        n = 0
                    entry.pop("starred", None)   # superseded by the rating
                    if n:
                        entry["stars"] = n
                    else:
                        entry.pop("stars", None)
                if "hidden" in payload:
                    if payload["hidden"]:
                        entry["hidden"] = True
                    else:
                        entry.pop("hidden", None)
                meta[p["key"]] = entry
                save_meta(meta)
            self.send_json({"ok": True, "entry": entry})
            return

        if path.startswith("/api/reveal/"):
            # Opens a window on the host. Pointless from somewhere else, and not
            # something a remote visitor should be able to do at all.
            if not self.is_local():
                self.send_json({"ok": False, "error": "only available on this machine"}, 403)
                return
            p = self.project_from(path, "/api/reveal/")
            if not p:
                self.send_error(404)
                return
            target = p.get("audio") or p.get("folder")
            subprocess.Popen(["open", "-R", target])
            self.send_json({"ok": True})
            return

        if path == "/api/rescan":
            threading.Thread(target=self.library.refresh, args=(True,), daemon=True).start()
            self.send_json({"ok": True})
            return

        self.send_error(404)


def lan_ip():
    """This machine's address on the local network, or None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # no packets are sent; this just picks the outbound interface
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def main():
    ap = argparse.ArgumentParser(description="Browser overview of Ableton projects")
    ap.add_argument("root", nargs="?", default=DEFAULT_ROOT, help="folder of project folders")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true",
                    help="serve to other devices on the local network "
                         "(binds 0.0.0.0 instead of localhost)")
    ap.add_argument("--host", default=None,
                    help="explicit bind address; overrides --lan")
    ap.add_argument("--password-file", default=None, metavar="PATH",
                    help="require this password (first line of the file) before "
                         "anything is served. Also read from $ORGANIZER_PASSWORD. "
                         "Required in practice before exposing this to the internet.")
    ap.add_argument("--cert", default=None,
                    help="TLS certificate chain (PEM); serves https when given")
    ap.add_argument("--key", default=None,
                    help="TLS private key (PEM), paired with --cert")
    ap.add_argument("--warm", action="store_true",
                    help="precompute every waveform up front instead of on "
                         "demand, so .data/ carries them to other machines")
    ap.add_argument("--wait-for-root", type=int, default=0, metavar="SECONDS",
                    help="wait for the library to appear before scanning. At "
                         "login the sync client may not have mounted yet, so a "
                         "service should set this instead of exiting.")
    ap.add_argument("--materialize", action="store_true",
                    help="read cloud-only files too, downloading them. Off by "
                         "default so a scan never pulls gigabytes from Dropbox.")
    ap.add_argument("--no-open", action="store_true", help="don't open a browser")
    ap.add_argument("--ignore", action="append", metavar="NAME",
                    help="top-level folder to skip; repeatable. "
                         "Defaults to: " + ", ".join(sorted(DEFAULT_IGNORE)))
    ap.add_argument("--no-ignore", action="store_true",
                    help="scan every folder, including the defaults")
    args = ap.parse_args()

    if args.no_ignore:
        ignore = set()
    else:
        ignore = set(DEFAULT_IGNORE) | {n.lower() for n in (args.ignore or [])}

    root = os.path.abspath(os.path.expanduser(args.root))
    if not os.path.isdir(root) and args.wait_for_root:
        print("waiting up to %ds for %s ..." % (args.wait_for_root, root))
        deadline = time.time() + args.wait_for_root
        while time.time() < deadline and not os.path.isdir(root):
            time.sleep(5)
    if not os.path.isdir(root):
        sys.exit("not a folder: %s" % root)

    global MATERIALIZE
    MATERIALIZE = args.materialize
    data_dir = bind_data_dir(root)
    password = os.environ.get("ORGANIZER_PASSWORD")
    if args.password_file:
        try:
            with open(os.path.expanduser(args.password_file)) as fh:
                password = fh.readline().strip()
        except OSError as e:
            sys.exit("could not read the password file: %s" % e)
    if password:
        if len(password) < 8:
            sys.exit("that password is too short to put on the internet")
        enable_auth(password)

    lib = Library(root, ignore, args.materialize)
    print("scanning %s ..." % root)
    print("  database: %s" % data_dir)
    if ignore:
        print("  ignoring: %s" % ", ".join(sorted(ignore)))
    t0 = time.time()
    last = [0]

    def prog(d, t):
        if d - last[0] >= 10 or d == t:
            last[0] = d
            sys.stdout.write("\r  %d/%d projects" % (d, t))
            sys.stdout.flush()

    projects = scan(root, prog, ignore, args.materialize)
    lib.projects = projects
    lib.by_id = {p["id"]: p for p in projects}
    playable = sum(1 for p in projects if p["audio"])
    withbpm = sum(1 for p in projects if p["bpm"])
    cloud = sum(1 for p in projects if p.get("cloudAls") or p.get("cloudAudio"))
    print("\n  %d projects, %d playable, %d with BPM  (%.1fs)"
          % (len(projects), playable, withbpm, time.time() - t0))
    if cloud:
        print("  %d touch cloud-only files — not downloaded, so no BPM or waveform"
              % cloud)
        print("  (they still play on click; --materialize reads everything)")

    if args.warm:
        todo = [p for p in projects if p["audio"] and not p.get("cloudAudio")]
        print("warming %d waveforms ..." % len(todo))
        for i, p in enumerate(todo, 1):
            compute_peaks(p["audio"])
            if i % 10 == 0 or i == len(todo):
                sys.stdout.write("\r  %d/%d" % (i, len(todo)))
                sys.stdout.flush()
        print()

    Handler.library = lib
    host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")
    try:
        srv = Server((host, args.port), Handler)
    except OSError as e:
        sys.exit("could not bind %s:%d — %s" % (host, args.port, e))

    scheme = "http"
    if args.cert or args.key:
        if not (args.cert and args.key):
            sys.exit("--cert and --key must be given together")
        for f in (args.cert, args.key):
            if not os.path.exists(f):
                sys.exit("no such file: %s" % f)
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(args.cert, args.key)
        except (ssl.SSLError, OSError) as e:
            sys.exit("could not load certificate: %s" % e)
        srv.tls_context = ctx
        scheme = "https"

    local = "%s://127.0.0.1:%d/" % (scheme, args.port)
    print("serving %s" % local)
    if host not in ("127.0.0.1", "localhost"):
        ip = lan_ip()
        if ip:
            print("        %s://%s:%d/   ← other machines" % (scheme, ip, args.port))
        if AUTH_PASSWORD:
            print("a password is required before anything is served")
        else:
            print("NO PASSWORD — everyone on this network can read the library and")
            print("edit notes. Fine at home; pass --password-file before exposing it.")
        print("macOS may ask you to allow incoming/local network connections.")
    if scheme == "https":
        print("TLS enabled (%s)" % os.path.basename(args.cert))
    print("(ctrl-c to stop)")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(local)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
