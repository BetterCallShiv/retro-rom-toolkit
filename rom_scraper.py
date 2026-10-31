#!/usr/bin/env python3

##########################################################################
#   Script: RomsGames Scraper & Bulk Downloader
#   Author: @BetterCallShiv
#   Features: 
#       - Scrapes complete game libraries & file sizes directly from romsgames.net
#       - High-speed multi-connection downloads powered by aria2 package
#       - Generates structured database.json files with direct download endpoints
#       - Metadata-only mode (--metadata-only) to catalog collections without downloading
#       - Automatic compression prompt / pipeline for converting downloads into .chd / .cso / extracted ROMs
#   How to Use:
#       1. Install the required packages: pip install requests beautifulsoup4 tqdm aria2
#       2. Run the script in your terminal: python rom_scraper.py
#       3. Or pass a link directly:
#          python rom_scraper.py "https://www.romsgames.net/roms/playstation-portable/?page=1&sort=popularity"
#       4. Check your console folder for downloaded ROMs and database.json
##########################################################################

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

try:
    import aria2c
except ImportError:
    aria2c = None

if sys.platform == "win32":
    try:
        os.system("")
    except Exception:
        pass
    try:
        import ctypes
    except ImportError:
        ctypes = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
else:
    ctypes = None
    msvcrt = None

try:
    from rom_compressor import compress_rom
except ImportError:
    compress_rom = None

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRAPE_WORKERS = 30
DOWNLOAD_WORKERS = 1
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
GAME_PATH = re.compile(r"^/([a-z0-9][a-z0-9-]*?)-rom-[^/]+/?$", re.I)
_local = threading.local()

_active_procs = set()
_active_procs_lock = threading.Lock()

def get_aria2_executable():
    if aria2c is not None and hasattr(aria2c, "ARIA2C") and os.path.isfile(str(aria2c.ARIA2C)):
        return str(aria2c.ARIA2C)
    bin_path = shutil.which("aria2c") or shutil.which("aria2c.exe")
    if bin_path:
        return bin_path
    return None

def force_exit(msg="\n[!] Force quit requested (Ctrl+C). Exiting immediately..."):
    with _active_procs_lock:
        for p in list(_active_procs):
            try:
                p.kill()
            except Exception:
                pass
    print(msg, flush=True)
    os._exit(0)

def sigint_handler(signum=None, frame=None):
    force_exit("\n[!] Force quit requested (Ctrl+C). Exiting immediately...")

signal.signal(signal.SIGINT, sigint_handler)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, sigint_handler)

if sys.platform == "win32" and ctypes:
    try:
        _CTRL_HANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
        def _win_ctrl_handler(ctrl_type):
            if ctrl_type in (0, 1, 2):
                force_exit("\n[!] Force quit requested (Ctrl+C). Exiting immediately...")
            return False
        _global_handler = _CTRL_HANDLER(_win_ctrl_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_global_handler, True)
    except Exception:
        pass

class HotkeyWatcher:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if sys.platform != "win32" or not msvcrt:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        if not msvcrt:
            return
        while not self._stop.is_set():
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch == b"\x03":
                        force_exit("\n[!] Force quit requested (Ctrl+C). Exiting immediately...")
                time.sleep(0.04)
            except Exception:
                pass

def safe_input(prompt):
    try:
        val = input(prompt).strip()
        if val.lower() in ("x", "q", "exit", "quit"):
            force_exit("\n[!] Exited by user.")
        return val
    except (KeyboardInterrupt, EOFError):
        force_exit("\n[!] Force quit requested (Ctrl+C). Exiting immediately...")

def get_session():
    if not hasattr(_local, "session"):
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.session = s
    return _local.session

class Blocked(Exception):
    pass

def fetch(url, retries=5):
    session = get_session()
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=(10, 25))
        except requests.RequestException:
            time.sleep(2 * attempt)
            continue
        if r.status_code == 200 and "Just a moment" not in r.text[:3000]:
            return r.text
        if r.status_code in (403, 503) or "Just a moment" in r.text[:3000]:
            raise Blocked(f"{url} -> HTTP {r.status_code} (bot protection)")
        if r.status_code == 404:
            return None
        wait = int(r.headers.get("Retry-After", 3 * attempt)) if r.status_code == 429 else 2 * attempt
        time.sleep(wait)
    return None

def parse_listing_url(url):
    if "://" not in url:
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc:
        raise ValueError(f"Invalid URL: {url}")
    base = f"{p.scheme}://{p.netloc}"
    segs = [s for s in p.path.split("/") if s]
    category = segs[-1] if segs else "games"
    query = dict(parse_qsl(p.query, keep_blank_values=True))
    def page_url(n):
        q = dict(query)
        q["page"] = n
        return f"{base}{p.path}?{urlencode(q)}"
    return base, p.path, category, page_url

def _host(netloc):
    return re.sub(r"^www\.", "", netloc.lower())

def parse_listing(html, base, prefix=None):
    soup = BeautifulSoup(html, "html.parser")
    host = _host(urlparse(base).netloc)
    games = {}
    for a in soup.select("a[href]"):
        url = urljoin(base + "/", a["href"]).split("#")[0].split("?")[0]
        p = urlparse(url)
        m = GAME_PATH.match(p.path)
        if not m or _host(p.netloc) != host:
            continue
        if prefix and m.group(1).lower() != prefix:
            continue
        img = a.find("img")
        title = (img.get("alt") if img and img.get("alt") else a.get_text(" ", strip=True)).strip()
        games.setdefault(url, title)
    return games

def detect_prefix(games):
    c = Counter(GAME_PATH.match(urlparse(u).path).group(1).lower() for u in games)
    return c.most_common(1)[0][0] if c else ""

def parse_total(html):
    m = re.search(r"Showing\s+\d+\s+to\s+\d+\s+of\s+([\d,]+)", html)
    return int(m.group(1).replace(",", "")) if m else None

def parse_max_page(html, base, path):
    soup = BeautifulSoup(html, "html.parser")
    best = 0
    for a in soup.select("a[href]"):
        p = urlparse(urljoin(base + "/", a["href"]))
        if p.path.rstrip("/") != path.rstrip("/"):
            continue
        for k, v in parse_qsl(p.query):
            if k == "page" and v.isdigit():
                best = max(best, int(v))
    return best

def parse_size_to_bytes(size_str):
    if not size_str:
        return 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*([A-Za-z]+)", size_str.strip())
    if not m:
        return 0
    try:
        val = float(m.group(1))
    except (ValueError, TypeError):
        return 0
    unit = m.group(2).upper()
    multipliers = {
        "B": 1,
        "BYTES": 1,
        "KB": 1024,
        "K": 1024,
        "MB": 1024 ** 2,
        "M": 1024 ** 2,
        "GB": 1024 ** 3,
        "G": 1024 ** 3,
        "TB": 1024 ** 4,
    }
    return int(val * multipliers.get(unit, 1))

def format_bytes(num_bytes):
    if not num_bytes or num_bytes <= 0:
        return "Unknown size"
    b = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024.0:
            return f"{b:.2f} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"

def format_eta(seconds):
    if seconds is None or seconds < 0 or seconds > 86400 * 7:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m:02d}m {s:02d}s"

def make_bar(percent, width=16):
    percent = max(0.0, min(1.0, percent))
    filled = int(width * percent)
    return "█" * filled + "░" * (width - filled)

def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    ids = [el["data-media-id"] for el in soup.select("[data-media-id]") if el["data-media-id"].strip()]
    if not ids:
        ids = re.findall(r'data-media-id=["\'](\d+)["\']', html)
    h1 = soup.select_one('h1[itemprop="name"]') or soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    file_size_str = ""
    for el in soup.find_all(["div", "span", "td", "p"]):
        txt = el.get_text(strip=True)
        if re.match(r"^\d+(?:\.\d+)?\s*(?:GB|MB|KB|kB|B|bytes)$", txt, re.I):
            file_size_str = txt
            break
    if not file_size_str:
        for s in soup.find_all("script", type="application/ld+json"):
            if s.string and "fileSize" in s.string:
                m = re.search(r'"fileSize"\s*:\s*"([^"]+)"', s.string)
                if m:
                    file_size_str = m.group(1).strip()
                    break
    if not file_size_str:
        m = re.search(r'"fileSize"\s*:\s*"([^"]+)"', html)
        if m:
            file_size_str = m.group(1).strip()
    if not file_size_str:
        m = re.search(r">\s*(\d+(?:\.\d+)?\s*(?:GB|MB|KB|kB|B|bytes))\s*<", html, re.I)
        if m:
            file_size_str = m.group(1).strip()
    file_size_bytes = parse_size_to_bytes(file_size_str)
    return title, "|".join(dict.fromkeys(ids)), file_size_str, file_size_bytes

def sanitize_filename(name, fallback="rom.zip"):
    if not name:
        return fallback
    name = unquote(name)
    name = "".join(
        char for char in name
        if char.isprintable() and char not in "\u200b\u200e\u200f\ufeff"
    )
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    name = re.sub(r"\s*-\s*", " - ", name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    if not name:
        return fallback
    base, ext = os.path.splitext(name)
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if base.upper() in reserved:
        name = f"_{name}"
    if not re.search(r"\.[a-zA-Z0-9]{1,10}$", name):
        name += ".zip"
    if len(name) > 200:
        base, ext = os.path.splitext(name)
        name = base[:200 - len(ext)].rstrip(" .") + ext
    name = name.rstrip(" .")
    return name or fallback

def trigger_download(game_url, media_id, delay=0.1):
    session = get_session()
    post_url = game_url.rstrip("/") + "/?download"
    p = urlparse(game_url)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": game_url,
        "Origin": f"{p.scheme}://{p.netloc}",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    try:
        r = session.post(post_url, data={"mediaId": media_id}, headers=headers, timeout=(10, 25))
    except requests.RequestException:
        return "", ""
    if delay > 0:
        time.sleep(delay)
    if r.status_code in (403, 503) or "Just a moment" in r.text[:3000]:
        raise Blocked(f"{post_url} -> HTTP {r.status_code} (bot protection)")
    if r.status_code != 200:
        return "", ""
    if r.url and r.url != post_url:
        return r.url, ""
    try:
        data = r.json()
        if isinstance(data, dict):
            url_val = ""
            for k in ("downloadUrl", "download_url", "url", "link", "href", "file"):
                v = data.get(k)
                if isinstance(v, str) and v:
                    url_val = v
                    break
            if not url_val and isinstance(data.get("data"), dict):
                inner = data.get("data")
                for k in ("downloadUrl", "download_url", "url", "link", "href", "file"):
                    v = inner.get(k)
                    if isinstance(v, str) and v:
                        url_val = v
                        break
            name_val = ""
            for k in ("downloadName", "download_name", "fileName", "filename", "name"):
                v = data.get(k) or (data.get("data") if isinstance(data.get("data"), dict) else {}).get(k)
                if isinstance(v, str) and v:
                    name_val = v
                    break
            if url_val:
                return url_val, name_val
    except ValueError:
        pass
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href]"):
            h = a["href"]
            if re.search(r"\.(zip|7z|rar|iso|cso|gba|nds|nes|sfc|n64|z64|gb|gbc|psp|bin|cue)$", h, re.I):
                return urljoin(post_url, h), os.path.basename(h)
        m = re.search(r'url=([^"\']+)', r.text, re.I)
        if m:
            return urljoin(post_url, m.group(1)), ""
    except Exception:
        pass
    return "", ""

def load_games_json(dest_dir):
    candidates = [
        os.path.join(dest_dir, "database.json"),
        os.path.join(os.getcwd(), "database.json"),
    ]
    for json_path in candidates:
        if json_path and os.path.isfile(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and (data.get("games") or data.get("priority_games")):
                        return data
                    if isinstance(data, list) and len(data) > 0:
                        return {"games": data}
            except Exception:
                continue
    return {}

def save_games_json(dest_dir, category, url, games_list):
    os.makedirs(dest_dir, exist_ok=True)
    json_path = os.path.join(dest_dir, "database.json")
    temp_path = json_path + ".tmp"
    total_bytes = sum(g.get("size_bytes", 0) for g in games_list)
    data = {
        "category": category,
        "url": url,
        "total_games": len(games_list),
        "total_size": format_bytes(total_bytes),
        "total_bytes": total_bytes,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "games": games_list,
    }
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if os.path.exists(json_path):
            os.remove(json_path)
        os.replace(temp_path, json_path)
    except Exception:
        pass
    return json_path

def scrape_game_worker(item, delay=0.1):
    try:
        url, list_title = item
        html = fetch(url)
        if not html:
            return None
        title, media_id, size_str, size_bytes = parse_detail(html)
        first_id = media_id.split("|")[0].strip() if media_id else ""
        dl_url, dl_name = "", ""
        if first_id:
            try:
                dl_url, dl_name = trigger_download(url, first_id, delay)
            except Blocked:
                pass
        if not dl_name:
            dl_name = f"{title or list_title}.zip"
        safe_name = sanitize_filename(dl_name, fallback=f"game_{first_id or 'rom'}.zip")
        return {
            "title": title or list_title,
            "url": url,
            "media_id": first_id,
            "size": size_str or (format_bytes(size_bytes) if size_bytes else "Unknown size"),
            "size_bytes": size_bytes,
            "download_url": dl_url,
            "filename": safe_name,
            "status": "pending",
        }
    except Exception:
        return None

_claimed_filenames = set()
_claimed_lock = threading.Lock()

def _deduplicate_filename(filename, dest_dir):
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    with _claimed_lock:
        while candidate.lower() in _claimed_filenames or (
            os.path.exists(os.path.join(dest_dir, candidate))
            and candidate.lower() not in _claimed_filenames
        ):
            candidate = f"{base} ({counter}){ext}"
            counter += 1
        _claimed_filenames.add(candidate.lower())
    return candidate

class UniversalProgress:
    def __init__(self, total_games, total_bytes, completed_games=0, completed_bytes=0):
        self.total_games = total_games
        self.total_bytes = max(1, total_bytes)
        self.completed_games = completed_games
        self.completed_bytes = completed_bytes
        self.rendered_lines = 0
        self._lock = threading.Lock()

    def _render(self, line1, line2):
        with self._lock:
            try:
                cols = shutil.get_terminal_size((80, 24)).columns
            except Exception:
                cols = 80
            max_w = max(40, cols - 1)

            l1 = line1[:max_w]
            l2 = line2[:max_w]

            if self.rendered_lines == 3:
                sys.stdout.write(f"\033[2A\r\033[K\n\r\033[K{l1}\n\r\033[K{l2}")
            else:
                sys.stdout.write(f"\n\r\033[K{l1}\n\r\033[K{l2}")
                self.rendered_lines = 3
            sys.stdout.flush()

    def update(self, current_title, current_done, current_total, speed_bps):
        curr_pct = (current_done / current_total * 100) if current_total > 0 else 0.0
        speed_str = f"{format_bytes(speed_bps)}/s" if speed_bps > 0 else "--/s"
        rem_bytes = max(0, current_total - current_done)
        eta_sec = (rem_bytes / speed_bps) if speed_bps > 0 else None
        eta_str = format_eta(eta_sec)
        curr_mb = f"{current_done / (1024**2):.0f}MB/{current_total / (1024**2):.0f}MB"

        line1 = f"> {current_title}: {curr_pct:.0f}% ({curr_mb}) {speed_str} ETA:{eta_str}"

        overall_done_b = self.completed_bytes + current_done
        overall_pct = min(1.0, overall_done_b / self.total_bytes)
        overall_gb = overall_done_b / (1024 ** 3)
        total_gb = self.total_bytes / (1024 ** 3)

        bar_len = 16
        bar_str = f"[{make_bar(overall_pct, bar_len)}]"
        line2 = f"Overall: [{self.completed_games}/{self.total_games} | {overall_gb:.1f}/{total_gb:.1f}GB {overall_pct*100:.0f}%] {bar_str}"

        self._render(line1, line2)

    def update_stage(self, current_title, stage_msg):
        line1 = f"> {current_title} -> [{stage_msg}]"

        overall_done_b = self.completed_bytes
        overall_pct = min(1.0, overall_done_b / self.total_bytes)
        overall_gb = overall_done_b / (1024 ** 3)
        total_gb = self.total_bytes / (1024 ** 3)

        bar_len = 16
        bar_str = f"[{make_bar(overall_pct, bar_len)}]"
        line2 = f"Overall: [{self.completed_games}/{self.total_games} | {overall_gb:.1f}/{total_gb:.1f}GB {overall_pct*100:.0f}%] {bar_str}"

        self._render(line1, line2)

    def print_log(self, msg):
        with self._lock:
            if self.rendered_lines == 3:
                sys.stdout.write("\033[2A\r\033[K\n\r\033[K\n\r\033[K\033[2A\r\033[K")
                self.rendered_lines = 0
            sys.stdout.write(f"{msg}\n")
            sys.stdout.flush()

    def game_finished(self, log_msg, completed_bytes_delta=0):
        with self._lock:
            if self.rendered_lines == 3:
                sys.stdout.write("\033[2A\r\033[K\n\r\033[K\n\r\033[K\033[2A\r\033[K")
                self.rendered_lines = 0
            self.completed_games += 1
            if completed_bytes_delta > 0:
                self.completed_bytes += completed_bytes_delta
            sys.stdout.write(f"  ✓ {log_msg}\n")
            sys.stdout.flush()

    def close(self):
        with self._lock:
            if self.rendered_lines == 3:
                sys.stdout.write("\033[2A\r\033[K\n\r\033[K\n\r\033[K\033[2A\r\033[K")
                self.rendered_lines = 0
            sys.stdout.flush()

def log_msg(msg, ui=None):
    if ui and hasattr(ui, "print_log"):
        ui.print_log(msg)
    elif ui and hasattr(ui, "write"):
        ui.write(msg)
    elif tqdm and hasattr(tqdm, "write"):
        tqdm.write(msg)
    else:
        print(msg, flush=True)

def _finalize_saved_file(dest_path, dest_dir, game, auto_compress=False, ui=None):
    saved_size = os.path.getsize(dest_path)

    if auto_compress and dest_path.lower().endswith((".zip", ".7z", ".rar", ".bin", ".cue", ".iso", ".img")):
        if not compress_rom:
            if ui and hasattr(ui, "game_finished"):
                ui.game_finished(f"Saved: {game['filename']} ({format_bytes(saved_size)})", saved_size)
            else:
                log_msg(f"  ✓ Saved: {game['filename']} ({format_bytes(saved_size)})", ui)
            game["status"] = "downloaded"
            return True
        try:
            if ui and hasattr(ui, "update_stage"):
                ui.update_stage(game["filename"], "Compressing...")
            else:
                log_msg(f"  [IDENTIFYING & COMPRESSING] Processing {game['filename']}...", ui)
            ok_c, msg_c, sz_orig, sz_final, created_outs = compress_rom(dest_path, dest_dir, keep_source=False)
            if ok_c and created_outs:
                out_names = ", ".join(os.path.basename(c) for c in created_outs)
                if sz_orig > sz_final and sz_orig > 0:
                    pct_saved = round((1.0 - (sz_final / sz_orig)) * 100)
                    ratio_str = f"{format_bytes(sz_final)} from {format_bytes(sz_orig)}, {pct_saved:.0f}% saved"
                else:
                    ratio_str = f"{format_bytes(sz_final)} from {format_bytes(sz_orig)}"
                if ui and hasattr(ui, "game_finished"):
                    ui.game_finished(f"[READY: {msg_c}] {out_names} ({ratio_str})", saved_size)
                else:
                    log_msg(f"  ✓ [READY: {msg_c}] {out_names} ({ratio_str})", ui)
                game["compressed_files"] = [os.path.basename(c) for c in created_outs]
                game["filename"] = os.path.basename(created_outs[0])
                game["size_bytes"] = sz_final
                game["size"] = format_bytes(sz_final)
            else:
                if ui and hasattr(ui, "game_finished"):
                    ui.game_finished(f"Saved: {game['filename']} ({format_bytes(saved_size)})", saved_size)
                else:
                    log_msg(f"  ✓ Saved: {game['filename']} ({format_bytes(saved_size)})", ui)
        except Exception as ec:
            if ui and hasattr(ui, "game_finished"):
                ui.game_finished(f"Saved: {game['filename']} ({format_bytes(saved_size)}) [compress error: {ec}]", saved_size)
            else:
                log_msg(f"  ✓ Saved: {game['filename']} ({format_bytes(saved_size)})", ui)
    else:
        if ui and hasattr(ui, "game_finished"):
            ui.game_finished(f"Saved: {game['filename']} ({format_bytes(saved_size)})", saved_size)
        else:
            log_msg(f"  ✓ Saved: {game['filename']} ({format_bytes(saved_size)})", ui)

    game["status"] = "downloaded"
    return True

def download_single_file(
    game,
    dest_dir,
    ui=None,
    file_lock=None,
    overwrite=False,
    use_aria2=True,
    split_connections=16,
    auto_compress=False,
):
    original_name = game["filename"]
    game["filename"] = _deduplicate_filename(original_name, dest_dir)
    dest_path = os.path.join(dest_dir, game["filename"])

    if not overwrite and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        if ui and hasattr(ui, "game_finished"):
            ui.game_finished(f"Already exists: {game['filename']}", game.get("size_bytes", 0))
        else:
            log_msg(f"  ✓ Already exists: {game['filename']}", ui)
        game["status"] = "downloaded"
        return True

    dl_url = game.get("download_url")
    if not dl_url:
        if game.get("media_id"):
            fresh_url, _ = trigger_download(game["url"], game["media_id"])
            if fresh_url:
                dl_url = fresh_url
                game["download_url"] = fresh_url
        if not dl_url:
            log_msg(f"  [NO LINK] {game['title']} (Missing download URL)", ui)
            game["status"] = "failed"
            return False

    session = get_session()
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": "https://www.romsgames.net/",
    }
    thread_id = threading.current_thread().ident
    temp_filename = f".dl_{thread_id}_{os.getpid()}_{game['filename']}.tmp"
    temp_path = os.path.join(dest_dir, temp_filename)
    aria2_ctrl_path = temp_path + ".aria2"

    aria2_bin = get_aria2_executable() if use_aria2 else None

    if ui and hasattr(ui, "update"):
        ui.update(game["filename"], 0, game.get("size_bytes", 0), 0)

    max_retries = 4
    for attempt in range(max_retries):
        if aria2_bin:
            try:
                cmd = [
                    aria2_bin,
                    f"--header=Referer: {headers['Referer']}",
                    f"--header=User-Agent: {headers['User-Agent']}",
                    "-x", str(min(16, max(1, split_connections))),
                    "-s", str(min(16, max(1, split_connections))),
                    "-k", "1M",
                    "--file-allocation=none",
                    "--summary-interval=1",
                    "--show-console-readout=false",
                    "--console-log-level=warn",
                    "--download-result=hide",
                    "--allow-overwrite=true",
                    "--auto-file-renaming=false",
                    "--dir", dest_dir,
                    "--out", temp_filename,
                    dl_url,
                ]

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                with _active_procs_lock:
                    _active_procs.add(proc)

                mults = {
                    "b": 1,
                    "k": 1024, "kib": 1024, "kb": 1024,
                    "m": 1024**2, "mib": 1024**2, "mb": 1024**2,
                    "g": 1024**3, "gib": 1024**3, "gb": 1024**3,
                }
                total_file_bytes = game.get("size_bytes", 0)

                if proc.stdout:
                    for line in proc.stdout:
                        m_bytes = re.search(r'\[#\w+\s+([\d\.]+)([A-Za-z]+)/', line)
                        m_speed = re.search(r'DL:([\d\.]+)([A-Za-z]+)', line)
                        speed_bps = 0
                        if m_speed:
                            try:
                                speed_bps = int(float(m_speed.group(1)) * mults.get(m_speed.group(2).lower(), 1))
                            except Exception:
                                pass
                        if m_bytes:
                            val_str, unit_str = m_bytes.groups()
                            try:
                                curr_bytes = int(float(val_str) * mults.get(unit_str.lower(), 1))
                                if ui and hasattr(ui, "update"):
                                    ui.update(game["filename"], curr_bytes, total_file_bytes or curr_bytes, speed_bps)
                            except (ValueError, TypeError):
                                pass

                proc.wait()
                with _active_procs_lock:
                    _active_procs.discard(proc)

                if proc.returncode == 0 and os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                    if os.path.exists(aria2_ctrl_path):
                        try:
                            os.remove(aria2_ctrl_path)
                        except OSError:
                            pass

                    with file_lock:
                        if os.path.exists(dest_path):
                            os.remove(dest_path)
                        os.replace(temp_path, dest_path)

                    return _finalize_saved_file(dest_path, dest_dir, game, auto_compress, ui)

                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                if os.path.exists(aria2_ctrl_path):
                    try:
                        os.remove(aria2_ctrl_path)
                    except OSError:
                        pass

                if attempt < max_retries - 1 and game.get("media_id"):
                    log_msg(f"  [REFRESHING LINK] {game['filename']} (CDN token expired, refreshing...)", ui)
                    fresh_url, _ = trigger_download(game["url"], game["media_id"])
                    if fresh_url:
                        dl_url = fresh_url
                        game["download_url"] = fresh_url
                        continue
            except Exception:
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                if os.path.exists(aria2_ctrl_path):
                    try:
                        os.remove(aria2_ctrl_path)
                    except OSError:
                        pass

        try:
            with session.get(dl_url, headers=headers, stream=True, timeout=(10, 35)) as r:
                if r.status_code in (403, 410, 503) and attempt < max_retries - 1 and game.get("media_id"):
                    log_msg(f"  [REFRESHING LINK] {game['filename']} (CDN token expired, refreshing...)", ui)
                    fresh_url, _ = trigger_download(game["url"], game["media_id"])
                    if fresh_url:
                        dl_url = fresh_url
                        game["download_url"] = fresh_url
                        continue
                if r.status_code != 200:
                    log_msg(f"  [HTTP {r.status_code}] Failed to download: {game['filename']}", ui)
                    game["status"] = "failed"
                    return False
                total_len = int(r.headers.get("content-length", 0))
                if game.get("size_bytes", 0) == 0 and total_len > 0:
                    game["size_bytes"] = total_len
                    game["size"] = format_bytes(total_len)
                downloaded = 0
                t_last = time.time()
                last_b = 0
                with open(temp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=128 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            elapsed = now - t_last
                            if elapsed >= 0.3:
                                speed = int((downloaded - last_b) / elapsed) if elapsed > 0 else 0
                                t_last = now
                                last_b = downloaded
                                if ui and hasattr(ui, "update"):
                                    ui.update(game["filename"], downloaded, total_len, speed)
            if os.path.exists(temp_path):
                with file_lock:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                    os.replace(temp_path, dest_path)
                return _finalize_saved_file(dest_path, dest_dir, game, auto_compress, ui)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            if attempt < max_retries - 1:
                wait = 3 * (2 ** attempt)
                log_msg(f"  [RETRY {attempt+1}/{max_retries}] {game['filename']}: {e} (waiting {wait}s...)", ui)
                time.sleep(wait)
            else:
                log_msg(f"  [ERROR] {game['filename']}: {e}", ui)
    game["status"] = "failed"
    return False

def process_category(
    listing_url,
    output_base=None,
    refresh_cache=False,
    auto=False,
    metadata_only=False,
    scrape_threads=30,
    download_threads=1,
    use_aria2=True,
    split_connections=8,
    auto_compress=None,
):
    try:
        base, path, category, page_url = parse_listing_url(listing_url)
    except Exception as e:
        print(f"Error parsing URL {listing_url}: {e}")
        return

    base_dir = os.path.abspath(output_base) if output_base else os.getcwd()
    dest_dir = os.path.join(base_dir, category)
    os.makedirs(dest_dir, exist_ok=True)
    cached_data = load_games_json(dest_dir) if not refresh_cache else {}
    cached_games_map = {g["url"]: g for g in cached_data.get("games", [])} if cached_data else {}

    print(f"\nInspecting listing for '{category}'...")
    html = fetch(page_url(1))
    if html is None:
        print(f"Error: Could not load page 1 for '{category}'. Skipping.")
        return

    candidates = parse_listing(html, base)
    if not candidates:
        print(f"Error: No game links found for '{category}'. Skipping.")
        return

    prefix = detect_prefix(candidates)
    first_page_games = parse_listing(html, base, prefix)
    per_page = len(first_page_games)
    total_games = parse_total(html) or per_page
    total_pages = max(math.ceil(total_games / per_page) if total_games else 1, parse_max_page(html, base, path))
    cached_count = len(cached_games_map)
    cached_bytes = sum(g.get("size_bytes", 0) for g in cached_games_map.values())
    cached_size_label = f" ({format_bytes(cached_bytes)})" if cached_bytes > 0 else ""

    print(f"""
===================================
 Console / Category : {category}
 Total Games Found  : {total_games:,}
 Total Pages        : {total_pages:,} ({per_page} games per page)
 Cached in JSON     : {cached_count:,} games{cached_size_label}
 Destination Folder : {dest_dir}
===================================
""")

    def get_listing_games(max_pages):
        collected = dict(first_page_games)
        if max_pages > 1:
            pages_to_fetch = list(range(2, max_pages + 1))
            print(f"\nCollecting links across {max_pages} listing pages...", flush=True)
            watcher = HotkeyWatcher()
            watcher.start()
            try:
                with ThreadPoolExecutor(max_workers=min(scrape_threads, len(pages_to_fetch))) as ex:
                    futs = {ex.submit(fetch, page_url(p)): p for p in pages_to_fetch}
                    for fut in as_completed(futs):
                        try:
                            p_html = fut.result()
                        except Exception:
                            p_html = None
                        if p_html:
                            p_games = parse_listing(p_html, base, prefix)
                            collected.update(p_games)
            finally:
                watcher.stop()
        return collected

    cached_list = cached_data.get("games") or cached_data.get("priority_games") or []
    if isinstance(cached_data, list):
        cached_list = cached_data

    all_listing_games = None
    if cached_list:
        all_listing_games = {g["url"]: g.get("title", "") for g in cached_list if g.get("url")}

    if cached_count >= total_games and cached_bytes > 0:
        print(f"[Cache Hit] All {total_games:,} games already cached in database! Total Library Size: {format_bytes(cached_bytes)}\n")
    else:
        if auto or metadata_only:
            ask_fetch = "y"
            print(f"[AUTO] Fetching total size for all {total_games:,} games & saving database...", flush=True)
        else:
            ask_fetch = safe_input(f"Fetch total size for all {total_games:,} games & save database? (y/n) [default: y]:\n> ").lower()
        if ask_fetch in ("", "y", "yes"):
            scraped_listing = get_listing_games(total_pages)
            if all_listing_games is None:
                all_listing_games = {}
            all_listing_games.update(scraped_listing)
            all_items = list(all_listing_games.items())
            missing = [
                (u, t) for u, t in all_items
                if u not in cached_games_map or not cached_games_map[u].get("download_url")
            ]
            if missing:
                print(f"\n[Scraping] Fetching sizes & download links for {len(missing):,} games with {scrape_threads} parallel threads...", flush=True)
                watcher = HotkeyWatcher()
                watcher.start()
                try:
                    with ThreadPoolExecutor(max_workers=scrape_threads) as ex:
                        futs = [ex.submit(scrape_game_worker, item, 0.1) for item in missing]
                        done = 0
                        for fut in as_completed(futs):
                            try:
                                res = fut.result()
                            except Exception:
                                res = None
                            done += 1
                            if res:
                                cached_games_map[res["url"]] = res
                                if done % 10 == 0 or done == len(missing):
                                    print(f"  [{done}/{len(missing)}] {res['title']} ({res['size']})", flush=True)
                            else:
                                if done % 10 == 0 or done == len(missing):
                                    print(f"  [{done}/{len(missing)}] Scraping...", flush=True)
                finally:
                    watcher.stop()
            ordered_games = [cached_games_map[u] for u, _ in all_items if u in cached_games_map]
            json_path = save_games_json(dest_dir, category, listing_url, ordered_games)
            total_lib_bytes = sum(g.get("size_bytes", 0) for g in ordered_games)
            print(f"""
===================================
     TOTAL SIZE & DATABASE SAVED     
===================================
 Category            : {category}
 Total Games Scraped : {len(ordered_games):,} / {total_games:,}
 Total Library Size  : {format_bytes(total_lib_bytes)}
 Database Saved To   : {json_path}
===================================
""")

    if metadata_only:
        print(f"[METADATA ONLY] Skipping downloads. Database is complete at: {os.path.join(dest_dir, 'database.json')}")
        return

    if auto:
        wanted = total_games
        print(f"[AUTO] Downloading all {total_games:,} games...", flush=True)
    else:
        while True:
            ans = safe_input(f"Enter number of games to download (e.g. 10, 50), 'all' for all {total_games:,} or '0' to skip [default: all]:\n> ").lower()
            if ans in ("0", "skip", "s", "none"):
                json_path = os.path.join(dest_dir, "database.json")
                print(f"\nSkipping download. Your metadata database is saved at: {json_path}")
                return auto_compress
            if not ans or ans == "all":
                wanted = total_games
                break
            if ans.isdigit() and int(ans) > 0:
                wanted = min(int(ans), total_games)
                break
            print("Invalid input. Please enter a positive number, 'all' or '0' to skip.")

    if auto_compress is None:
        if not auto:
            compress_ans = safe_input("Automatically compress/convert downloaded ROMs (.chd / .cso / clean)? (y/n) [default: n]:\n> ").strip().lower()
            auto_compress = compress_ans in ("y", "yes")
        else:
            auto_compress = False

    pages_needed = max(1, math.ceil(wanted / per_page))
    if all_listing_games is not None and len(all_listing_games) >= wanted:
        todo_games = list(all_listing_games.items())[:wanted]
    else:
        collected = get_listing_games(pages_needed)
        if all_listing_games is not None:
            collected.update(all_listing_games)
        todo_games = list(collected.items())[:wanted]

    need_scraping = [
        (u, t) for u, t in todo_games
        if u not in cached_games_map or not cached_games_map[u].get("download_url")
    ]
    cached_ready_count = len(todo_games) - len(need_scraping)
    if cached_ready_count > 0:
        print(f"\n[Cache Hit] {cached_ready_count:,} / {len(todo_games):,} game metadata ready in database!", flush=True)

    if need_scraping:
        print(f"\nScraping {len(need_scraping):,} games needed for download with {scrape_threads} parallel threads...", flush=True)
        watcher = HotkeyWatcher()
        watcher.start()
        try:
            with ThreadPoolExecutor(max_workers=scrape_threads) as ex:
                futs = [ex.submit(scrape_game_worker, item, 0.1) for item in need_scraping]
                n = 0
                for fut in as_completed(futs):
                    try:
                        res = fut.result()
                    except Exception:
                        res = None
                    n += 1
                    if res:
                        cached_games_map[res["url"]] = res
                        print(f"  [{n}/{len(need_scraping)}] {res['title']} | Size: {res['size']} | ID: {res['media_id']}", flush=True)
                    else:
                        print(f"  [{n}/{len(need_scraping)}] Failed to scrape game info", flush=True)
        finally:
            watcher.stop()
    else:
        print(f"\nAll {len(todo_games):,} games loaded from database! (Zero scraping needed)", flush=True)

    final_games = [cached_games_map[u] for u, _ in todo_games if u in cached_games_map]
    json_path = save_games_json(dest_dir, category, listing_url, final_games)
    total_bytes = sum(g.get("size_bytes", 0) for g in final_games)
    total_size_str = format_bytes(total_bytes)

    aria2_avail = get_aria2_executable() if use_aria2 else None
    engine_name = f"aria2 (pip module, {split_connections} split connections per file)" if aria2_avail else "Python requests (streaming)"

    print(f"\nSaved metadata database: {json_path}")
    print(f"""
===================================
          STARTING DOWNLOAD          
===================================
 Category            : {category}
 Games to Download   : {len(final_games)}
 Total Download Size : {total_size_str}
 Destination Folder  : {dest_dir}
 Download Engine     : {engine_name}
 Download Threads    : {download_threads} parallel workers
 Auto-Compress       : {'Enabled' if auto_compress else 'Disabled'}
 Database File       : {json_path}
===================================
""", flush=True)

    file_lock = threading.Lock()
    with _claimed_lock:
        _claimed_filenames.clear()

    ui = UniversalProgress(
        total_games=len(final_games),
        total_bytes=total_bytes,
        completed_games=0,
        completed_bytes=0,
    )

    successful = 0
    watcher = HotkeyWatcher()
    watcher.start()
    try:
        with ThreadPoolExecutor(max_workers=download_threads) as ex:
            futs = [
                ex.submit(
                    download_single_file,
                    g,
                    dest_dir,
                    ui,
                    file_lock,
                    False,
                    use_aria2,
                    split_connections,
                    auto_compress,
                )
                for g in final_games
            ]
            for fut in as_completed(futs):
                if fut.result():
                    successful += 1
    finally:
        watcher.stop()
        if ui:
            ui.close()

    save_games_json(dest_dir, category, listing_url, final_games)
    print(f"""
===================================
          DOWNLOAD COMPLETE!         
===================================
 Successfully Downloaded : {successful} / {len(final_games)} games
 Total Size              : {total_size_str}
 Folder Location         : {dest_dir}
 Metadata JSON File      : {json_path}
===================================
""", flush=True)
    return auto_compress

def main():
    parser = argparse.ArgumentParser(
        description="High-throughput scraper and downloader for retro ROMs with database generation."
    )
    parser.add_argument(
        "urls",
        nargs="*",
        default=[],
        help="One or more romsgames.net category listing URLs",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=None,
        help="Base destination directory (defaults to current working directory)",
    )
    parser.add_argument(
        "-t", "--scrape-threads",
        type=int,
        default=30,
        help="Concurrent scraping worker threads (default: 30)",
    )
    parser.add_argument(
        "-d", "--download-threads",
        type=int,
        default=1,
        help="Concurrent download worker threads (default: 1)",
    )
    parser.add_argument(
        "-a", "--auto",
        action="store_true",
        help="Automated mode: skip all prompts, scrape and download all games automatically",
    )
    parser.add_argument(
        "-m", "--metadata-only",
        action="store_true",
        help="Metadata mode: scrape sizes, media IDs and generate database.json without downloading ROM files",
    )
    parser.add_argument(
        "-r", "--refresh-cache",
        action="store_true",
        help="Ignore cached database.json and re-scrape all metadata from scratch",
    )
    parser.add_argument(
        "--no-aria2",
        action="store_true",
        help="Disable aria2 turbo multi-connection download and fallback to standard requests",
    )
    parser.add_argument(
        "-s", "--split-connections",
        type=int,
        default=8,
        help="Split connections per file when using aria2 (default: 8)",
    )
    parser.add_argument(
        "-c", "--compress", "--convert-chd",
        dest="compress",
        action="store_true",
        default=None,
        help="Automatically compress downloaded games based on format (.cso for PSP, .chd for PS1/PS2, clean extraction for cartridge ROMs)",
    )
    parser.add_argument(
        "--no-compress",
        dest="compress",
        action="store_false",
        help="Disable automatic compression and skip interactive compression prompt",
    )
    args = parser.parse_args()

    print("""
===================================
   ROMSGAMES.NET ROM DOWNLOADER   
===================================
""")

    urls = []
    if args.urls:
        for u_arg in args.urls:
            for part in re.split(r"[,;\s]+", u_arg):
                p = part.strip().strip("'\"")
                if p.startswith("http"):
                    urls.append(p)

    if not urls:
        if args.auto or args.metadata_only:
            sys.exit("Error: Automated and metadata-only modes require at least one URL argument.\nUsage: python rom_scraper.py <URL> [--auto | --metadata-only]")
        raw_input = safe_input("Enter romsgames.net listing URL(s) (comma-separated for multiple):\n> ")
        for part in re.split(r"[,;\n\r]+", raw_input):
            p = part.strip().strip("'\"")
            if p:
                urls.append(p)

    if not urls:
        sys.exit("Error: No URL provided.")

    total_categories = len(urls)
    if total_categories > 1:
        print(f"\n[+] Queued {total_categories} categories to process.")

    user_compress = args.compress

    for idx, u in enumerate(urls, 1):
        if total_categories > 1:
            print(f"""
===================================
 CATEGORY [{idx}/{total_categories}]
 URL: {u}
===================================
""")
        chosen_compress = process_category(
            listing_url=u,
            output_base=args.output_dir,
            refresh_cache=args.refresh_cache,
            auto=args.auto,
            metadata_only=args.metadata_only,
            scrape_threads=args.scrape_threads,
            download_threads=args.download_threads,
            use_aria2=not args.no_aria2,
            split_connections=args.split_connections,
            auto_compress=user_compress,
        )
        if user_compress is None and chosen_compress is not None:
            user_compress = chosen_compress

    if total_categories > 1:
        print(f"""
===================================
 ALL {total_categories} CATEGORIES PROCESSED!
===================================
""")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        force_exit("\n[!] Force quit requested (Ctrl+C). Exiting immediately...")
