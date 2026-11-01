#!/usr/bin/env python3

##########################################################################
#   Script: Retro ROM Ranker
#   Author: @BetterCallShiv
#   Features: 
#       - Sorts entire ROM libraries from best to worst in descending quality order
#       - Multi-account session leasing pool to bypass ScreenScraper 1-thread limit
#       - Auto-detects console platforms (PSP, PS1, Game Boy, GBA, SNES, etc.)
#       - Dynamic Hall of Fame leaderboard preview
#   How to Use:
#       1. Install the required packages: pip install requests beautifulsoup4
#       2. Add your ScreenScraper account: python rom_ranker.py --add-account "USER" "PASS"
#       3. Run the script in your library folder: python rom_ranker.py -t 15
#       4. Check the generated sorted_database.json for your ranked collection
##########################################################################

import argparse
import json
import os
import queue
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup

if sys.platform == "win32":
    try:
        import ctypes
    except ImportError:
        ctypes = None
else:
    ctypes = None

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

_is_exiting = False


def force_exit(msg="\n[!] Interrupted by user (Ctrl+C). Exiting immediately..."):
    global _is_exiting
    if _is_exiting:
        os._exit(0)
    _is_exiting = True
    print(msg, flush=True)
    os._exit(0)


def sigint_handler(signum=None, frame=None):
    force_exit()
signal.signal(signal.SIGINT, sigint_handler)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, sigint_handler)
if sys.platform == "win32" and ctypes:
    try:
        _CTRL_HANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
        def _win_ctrl_handler(ctrl_type):
            if ctrl_type in (0, 1, 2):
                force_exit()
                return True
            return False
        _global_handler = _CTRL_HANDLER(_win_ctrl_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_global_handler, True)
    except Exception:
        pass
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenscraper_config.json")
PLATFORMS_MAP = {
    "psp": "61", "playstation portable": "61", "playstation-portable": "61",
    "ps1": "57", "psx": "57", "playstation": "57",
    "ps2": "58", "playstation 2": "58", "playstation-2": "58",
    "ps3": "59", "playstation 3": "59",
    "gb": "9", "gameboy": "9", "game boy": "9", "game-boy": "9",
    "gbc": "10", "gameboy color": "10", "game boy color": "10", "gameboy-color": "10",
    "gba": "12", "gameboy advance": "12", "game boy advance": "12", "gameboy-advance": "12",
    "nds": "15", "ds": "15", "nintendo ds": "15", "nintendo-ds": "15",
    "3ds": "17", "nintendo 3ds": "17",
    "nes": "3", "famicom": "3",
    "snes": "4", "super nintendo": "4", "super-nintendo": "4", "super famicom": "4",
    "n64": "14", "nintendo 64": "14", "nintendo-64": "14",
    "gamecube": "13", "gc": "13",
    "wii": "16", "wii u": "18",
    "genesis": "1", "megadrive": "1", "sega genesis": "1", "sega-genesis": "1", "sega megadrive": "1",
    "dreamcast": "23", "saturn": "22",
    "game gear": "21", "gg": "21",
    "master system": "2",
    "atari 2600": "26", "atari 7800": "41",
}


class ScreenScraperSessionPool:
    def __init__(self, accounts):
        self.q = queue.Queue()
        self.active_accounts = []
        for acc in accounts:
            u = (acc.get("username", "") or acc.get("user", "")).strip()
            p = (acc.get("password", "") or acc.get("pass", "")).strip()
            if not u or not p:
                continue
            sess = requests.Session()
            sess.headers.update(HEADERS)
            connected = False
            for _ in range(2):
                try:
                    r = sess.post(
                        "https://www.screenscraper.fr/index.php?action=login",
                        data={"loginuser": u, "loginpassword": p},
                        headers=HEADERS,
                        timeout=20,
                    )
                    txt = r.text.lower()
                    if "deconnexion" in txt or "logout" in txt or u.lower() in txt:
                        self.q.put((u, sess))
                        self.active_accounts.append(u)
                        connected = True
                        break
                except Exception:
                    time.sleep(1)
            if not connected:
                print(f"[!] Warning: Could not log in to account '{u}' (network timeout).")
        if self.q.empty():
            sess = requests.Session()
            sess.headers.update(HEADERS)
            self.q.put(("Anonymous", sess))


    def get_session(self):
        return self.q.get()


    def return_session(self, item):
        self.q.put(item)


def save_accounts(accounts):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"accounts": accounts}, f, indent=2)
        return True
    except Exception as e:
        print(f"[!] Error saving to {CONFIG_FILE}: {e}")
        return False


def test_single_account(username, password, retries=2):
    sess = requests.Session()
    sess.headers.update(HEADERS)
    last_err = ""
    for _ in range(retries):
        try:
            r = sess.post(
                "https://www.screenscraper.fr/index.php?action=login",
                data={"loginuser": username, "loginpassword": password},
                headers=HEADERS,
                timeout=20,
            )
            txt = r.text.lower()
            if "deconnexion" in txt or "logout" in txt or username.lower() in txt:
                return True, "Login successful"
            return False, "Invalid username or password"
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
    return False, f"Network timeout/error: {last_err}"


def load_accounts(cli_user=None, cli_pass=None):
    accounts = []
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                if "accounts" in cfg and isinstance(cfg["accounts"], list):
                    accounts.extend(cfg["accounts"])
                elif "username" in cfg and "password" in cfg:
                    accounts.append({"username": cfg["username"], "password": cfg["password"]})
        except Exception:
            pass
    if cli_user and cli_pass:
        accounts.append({"username": cli_user, "password": cli_pass})
        save_accounts(accounts)
    seen = set()
    unique_accounts = []
    for a in accounts:
        u = (a.get("username", "") or a.get("user", "")).strip()
        p = (a.get("password", "") or a.get("pass", "")).strip()
        if u and p and u.lower() not in seen:
            seen.add(u.lower())
            unique_accounts.append({"username": u, "password": p})
    return unique_accounts


def detect_platform_id(data=None, file_path=None, user_override=None):
    if user_override:
        uo = str(user_override).strip().lower()
        if uo.isdigit():
            return uo, f"User override ID ({uo})"
        if uo in PLATFORMS_MAP:
            return PLATFORMS_MAP[uo], f"User override '{user_override}'"
    if isinstance(data, dict):
        cat = str(data.get("category", "")).lower()
        if cat in PLATFORMS_MAP:
            return PLATFORMS_MAP[cat], f"Detected from JSON category '{cat}'"
        for k, pid in PLATFORMS_MAP.items():
            if k in cat:
                return pid, f"Detected from JSON category '{cat}'"
        url = str(data.get("url", "")).lower()
        for k, pid in PLATFORMS_MAP.items():
            if f"/{k}/" in url or f"/{k}?" in url:
                return pid, f"Detected from URL path '{k}'"
    if file_path:
        norm_path = os.path.normpath(file_path).lower()
        parts = norm_path.replace("\\", "/").split("/")
        for part in reversed(parts):
            if part in PLATFORMS_MAP:
                return PLATFORMS_MAP[part], f"Detected from directory '{part}'"
    return "", "All platforms (unrestricted)"


def clean_base_title(raw_title):
    t = re.sub(r"\[.*?\]|\(.*?\)", "", raw_title).strip()
    return re.sub(r"\s+", " ", t).strip()


def clean_filename_to_title(filename):
    if not filename:
        return ""
    name = re.sub(r"\.[a-zA-Z0-9]{2,4}$", "", filename)
    name = re.sub(r"^\s*\[?\d{3,5}\]?\s*[-–—]\s*", "", name)
    name = re.sub(r"\[.*?\]", "", name)
    name = re.sub(
        r"\((?:[^\)]*(?:japan|usa|europe|asia|korea|world|en|ja|fr|de|es|it|v\d|beta|proto|disc|cd|track|dlc|psn|psp|demo|guide)[^\)]*)\)",
        "",
        name,
        flags=re.I,
    )
    name = re.sub(r"\s*\([^\)]*\)\s*$", "", name)
    return re.sub(r"\s+", " ", name).strip(" -–—_")


def strip_dlc_and_addons(title):
    if not title:
        return ""
    for delim in [" - ", " – ", " — ", ": "]:
        if delim in title:
            parts = title.split(delim)
            for i in range(len(parts) - 1, 0, -1):
                tail = " ".join(parts[i:])
                if re.search(r"\b(dlc|level\s*pack|quest\s*pack|booster\s*pack|mission\s*pack|track\s*pack|map\s*pack|pack|guide|addon|add-on|costume|bonus|expansion)\b", tail, re.I):
                    return delim.join(parts[:i]).strip(" -–—_")
    cleaned = re.sub(
        r"\s+[-:]?\s*(?:bui\s*bui|official|koushiki)?\s*(?:level\s*pack|quest\s*pack|booster\s*pack|strategy\s*guide|guide\s*book|dlc|expansion)\b.*$",
        "",
        title,
        flags=re.I,
    ).strip(" -–—_")
    return cleaned if cleaned and cleaned != title else ""


def clean_title_for_screenscraper(raw_title):
    base = clean_base_title(raw_title)
    queries = []
    clean = re.sub(r"\s+featuring\s+.*$", "", base, flags=re.I)
    clean = re.sub(r"^(disney[-']s|disney-pixar|disney|tom clancy[-']s|marvel[-']s)\s*[-:]?\s*", "", clean, flags=re.I)
    clean = clean.replace("Vs.", "Vs").replace("vs.", "vs")
    clean = re.sub(r"[!?]", "", clean).strip()
    if clean and clean not in queries:
        queries.append(clean)

    dlc_clean = re.sub(
        r"\s+(?:cloud|lightning|sephiroth|squall|tifa|zack|aerith|mirage)?\s*(?:alt\s*\d*|costume\s*\d*|pack|dlc|skin|gear|expansion|bundle|bonus|add-?on)\b.*$",
        "",
        base,
        flags=re.I,
    ).strip()
    if dlc_clean and dlc_clean not in queries:
        queries.append(dlc_clean)

    base_lower = base.lower()
    for jp_k, alias in JP_WESTERN_ALIASES.items():
        if jp_k in base_lower:
            alias_title = alias.replace("-", " ").title()
            if alias_title not in queries:
                queries.append(alias_title)

    for delim in [":", " - "]:
        if delim in base:
            p1 = base.split(delim)[0].strip()
            p1 = re.sub(r"^(disney-pixar|disney|tom clancy\'s|marvel\'s)\s*[-:]?\s*", "", p1, flags=re.I)
            p1 = re.sub(r"[!?]", "", p1).strip()
            if p1 and p1 not in queries:
                queries.append(p1)
            p2 = base.split(delim)[1].strip()
            p2 = re.sub(r"[!?]", "", p2).strip()
            if p2 and len(p2) > 3 and p2 not in queries:
                queries.append(p2)
    expanded = list(queries)
    for q in queries:
        if re.search(r"ssho\b", q, re.I):
            var = re.sub(r"ssho\b", "ssyo", q, flags=re.I)
            if var not in expanded:
                expanded.append(var)
        elif re.search(r"ssyo\b", q, re.I):
            var = re.sub(r"ssyo\b", "ssho", q, flags=re.I)
            if var not in expanded:
                expanded.append(var)
    return expanded


JP_WESTERN_ALIASES = {
    "goemon's great adventure": "mystical-ninja-2-starring-goemon",
    "fighters destiny": "fighters-destiny",
    "roadsters trophy": "roadsters",
    "puzzle bobble 64": "bust-a-move-99",
    "quake 64": "quake-ii",
    "kobe bryant's nba courtside": "nba-courtside",
}


def generate_metacritic_slugs(raw_title):
    clean = clean_base_title(raw_title)
    slugs = []
    def make_slug(t):
        t = re.sub(r"[\'’\.]", "", t.lower())
        t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
        return t
    base_slug = make_slug(clean)
    if base_slug:
        slugs.append(base_slug)

    clean_lower = clean.lower()
    for jp_key, alias_slug in JP_WESTERN_ALIASES.items():
        if jp_key in clean_lower and alias_slug not in slugs:
            slugs.append(alias_slug)

    dlc_clean = re.sub(
        r"\s+(?:cloud|lightning|sephiroth|squall|tifa|zack|aerith|mirage)?\s*(?:alt\s*\d*|costume\s*\d*|pack|dlc|skin|gear|expansion|bundle|bonus|add-?on)\b.*$",
        "",
        clean,
        flags=re.I,
    ).strip()
    if dlc_clean and dlc_clean != clean:
        s_dlc = make_slug(dlc_clean)
        if s_dlc and s_dlc not in slugs:
            slugs.append(s_dlc)

    no_portable = re.sub(r"\s+(?:portable|edition|version)\b.*$", "", clean, flags=re.I).strip()
    if no_portable and no_portable != clean:
        s_np2 = make_slug(no_portable)
        if s_np2 and s_np2 not in slugs:
            slugs.append(s_np2)

    no_prefix = re.sub(r"^(disney-pixar|disney|tom clancy\'s|marvel\'s|ea sports)\s*[-:]?\s*", "", clean, flags=re.I)
    if no_prefix != clean:
        s_np = make_slug(no_prefix)
        if s_np and s_np not in slugs:
            slugs.append(s_np)
    no_feat = re.sub(r"\s+featuring\s+.*$", "", clean, flags=re.I)
    if no_feat != clean:
        s_nf = make_slug(no_feat)
        if s_nf and s_nf not in slugs:
            slugs.append(s_nf)
    for delim in [":", " - "]:
        if delim in clean:
            part1 = clean.split(delim)[0].strip()
            s_p1 = make_slug(part1)
            if s_p1 and s_p1 not in slugs:
                slugs.append(s_p1)
    roman_map = {"viii": "8", "vii": "7", "vi": "6", "iv": "4", "ix": "9", "iii": "3", "ii": "2", "v": "5"}
    expanded = []
    for s in slugs:
        if s not in expanded:
            expanded.append(s)
        words = s.split("-")
        converted = [roman_map[w] if w in roman_map else w for w in words]
        s2 = "-".join(converted)
        if s2 not in expanded:
            expanded.append(s2)
    return expanded


def fetch_metacritic(session, raw_title):
    slugs = generate_metacritic_slugs(raw_title)
    for slug in slugs:
        url = f"https://www.metacritic.com/game/{slug}/"
        soup = None
        for attempt in range(2):
            try:
                r = session.get(url, headers=HEADERS, timeout=10)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, "html.parser")
                    break
                elif r.status_code == 404:
                    break
            except Exception:
                if attempt == 0:
                    time.sleep(0.5)
                continue
        if not soup:
            continue
        try:
            hero = soup.find(class_="hero-scores")
            metascore = None
            sentiment = "Mixed or Average"
            reviews_count = None
            user_score = None
            user_ratings_count = None
            if hero:
                txt = hero.get_text(separator=" | ")
                if "critic reviews are not available" in txt.lower() or "metascore | tbd" in txt.lower():
                    metascore = None
                else:
                    m_meta = re.search(r"Metascore\s*\|\s*([^|]+)\s*\|\s*Based on (\d+) Critic Reviews\s*\|\s*(\d{1,3})", txt, re.I)
                    if m_meta:
                        sentiment = m_meta.group(1).strip()
                        reviews_count = int(m_meta.group(2))
                        val = int(m_meta.group(3))
                        if 0 <= val <= 100:
                            metascore = val
                    else:
                        m_num = re.search(r"Metascore\s*\|\s*(\d{1,3})\b", txt, re.I)
                        if m_num:
                            val = int(m_num.group(1))
                            if 0 <= val <= 100:
                                metascore = val
                m_user = re.search(r"User score\s*\|\s*([^|]+)\s*\|\s*Based on ([\d,]+) User Ratings\s*\|\s*([\d\.]+)", txt, re.I)
                if m_user:
                    user_ratings_count = int(m_user.group(2).replace(",", ""))
                    user_score = float(m_user.group(3))
                else:
                    m_u_val = re.search(r"User score[^\d]*(\d+(?:\.\d+)?)", txt, re.I)
                    if m_u_val:
                        try:
                            user_score = float(m_u_val.group(1))
                        except Exception:
                            pass
            if metascore is None:
                el_score = soup.find("div", class_=re.compile(r"c-productScoreInfo|product-score-card__score"))
                if el_score and "tbd" not in el_score.get_text().lower():
                    m = re.search(r"\b(\d{1,3})\b", el_score.get_text())
                    if m:
                        val = int(m.group(1))
                        if 0 <= val <= 100:
                            metascore = val
            if metascore is not None and not (0 <= metascore <= 100):
                metascore = None
            if metascore is None:
                continue
            if metascore >= 90:
                sentiment = "Universal Acclaim"
            elif metascore >= 75:
                sentiment = "Generally Favorable"
            elif metascore >= 50:
                sentiment = "Mixed or Average"
            else:
                sentiment = "Generally Unfavorable"

            mc_title = ""
            h1 = soup.find("h1")
            if h1 and h1.get_text(strip=True):
                mc_title = h1.get_text(strip=True)
            elif hero:
                og = soup.find("meta", property="og:title")
                if og and og.get("content"):
                    mc_title = re.sub(r"\s+Reviews.*$", "", og.get("content")).strip()
            if not mc_title:
                mc_title = slug.replace("-", " ").title()

            return {
                "metascore": metascore,
                "user_score": user_score,
                "user_ratings_count": user_ratings_count,
                "review_count": reviews_count,
                "sentiment": sentiment,
                "url": url,
                "canonical_title": mc_title,
            }
        except Exception:
            continue
    return None


def parse_screenscraper_rating(html):
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a"):
        txt = a.get_text(strip=True)
        m = re.match(r"^(\d+(?:\.\d+)?)\s*/\s*20$", txt)
        if m:
            return float(m.group(1))
    for tr in soup.find_all("tr"):
        row_txt = tr.get_text()
        if "rating" in row_txt.lower() or "note" in row_txt.lower():
            for sel in tr.find_all(["select", "option"]):
                sel.decompose()
            m = re.search(r"\b(\d+(?:\.\d+)?)\s*/\s*20\b", tr.get_text())
            if m:
                return float(m.group(1))
    return None


def fetch_screenscraper(session_pool, raw_title, platform_id=""):
    queries = clean_title_for_screenscraper(raw_title)
    user_name, sess = session_pool.get_session()
    try:
        for q in queries:
            try:
                payload = {
                    "rechercheplateforme": platform_id or "",
                    "recherchegenre": "",
                    "recherchecritere": "all",
                    "recherche": q,
                }
                r = sess.post(
                    "https://www.screenscraper.fr/recherche.php?numpage=0&action=newrecherche",
                    data=payload,
                    timeout=12,
                )
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                candidates = []
                for a in soup.find_all("a"):
                    href = a.get("href", "")
                    if "gameinfos.php" in href:
                        candidates.append((href, a.get_text(strip=True)))

                norm_raw = re.sub(r"[^a-z0-9]", "", raw_title.lower())
                raw_nums = set(re.findall(r"\b(?:2|3|4|5|6|7|8|9|ii|iii|iv|v|vi|vii|viii)\b", raw_title.lower()))

                valid_candidates = []
                for href, c_name in candidates:
                    c_nums = set(re.findall(r"\b(?:2|3|4|5|6|7|8|9|ii|iii|iv|v|vi|vii|viii)\b", c_name.lower()))
                    if c_nums - raw_nums:
                        continue
                    valid_candidates.append((href, c_name))

                def score_cand(item):
                    href, c_name = item
                    norm_c = re.sub(r"[^a-z0-9]", "", c_name.lower())
                    if norm_c == norm_raw:
                        return 1000
                    c_words = set(c_name.lower().split())
                    raw_words = set(raw_title.lower().split())
                    return len(c_words & raw_words)

                valid_candidates.sort(key=score_cand, reverse=True)

                found_result = None
                for href, c_name in valid_candidates[:3]:
                    target_url = "https://www.screenscraper.fr/" + href.lstrip("/")
                    try:
                        rg = sess.get(target_url, timeout=12)
                        if rg.status_code != 200:
                            continue
                        val = parse_screenscraper_rating(rg.text)
                        if val is not None:
                            page_title = c_name
                            try:
                                soup_g = BeautifulSoup(rg.text, "html.parser")
                                h1 = soup_g.find("h1")
                                if h1 and h1.get_text(strip=True):
                                    page_title = h1.get_text(strip=True)
                            except Exception:
                                pass
                            found_result = {
                                "rating": val,
                                "rating_max": 20,
                                "percentage": round((val / 20.0) * 100, 1),
                                "url": target_url,
                                "scraped_by": user_name,
                                "matched_title": c_name,
                                "page_title": page_title,
                            }
                            break
                    except Exception:
                        continue

                if found_result:
                    return found_result

                if platform_id:
                    payload["rechercheplateforme"] = ""
                    r_unrest = sess.post(
                        "https://www.screenscraper.fr/recherche.php?numpage=0&action=newrecherche",
                        data=payload,
                        timeout=12,
                    )
                    soup_unrest = BeautifulSoup(r_unrest.text, "html.parser")
                    unrest_candidates = []
                    for a in soup_unrest.find_all("a"):
                        href = a.get("href", "")
                        if "gameinfos.php" in href:
                            c_name = a.get_text(strip=True)
                            c_nums = set(re.findall(r"\b(?:2|3|4|5|6|7|8|9|ii|iii|iv|v|vi|vii|viii)\b", c_name.lower()))
                            if not (c_nums - raw_nums):
                                unrest_candidates.append((href, c_name))
                    unrest_candidates.sort(key=score_cand, reverse=True)
                    for href, c_name in unrest_candidates[:3]:
                        target_url = "https://www.screenscraper.fr/" + href.lstrip("/")
                        try:
                            rg = sess.get(target_url, timeout=12)
                            if rg.status_code != 200:
                                continue
                            val = parse_screenscraper_rating(rg.text)
                            if val is not None:
                                page_title = c_name
                                try:
                                    soup_g = BeautifulSoup(rg.text, "html.parser")
                                    h1 = soup_g.find("h1")
                                    if h1 and h1.get_text(strip=True):
                                        page_title = h1.get_text(strip=True)
                                except Exception:
                                    pass
                                return {
                                    "rating": val,
                                    "rating_max": 20,
                                    "percentage": round((val / 20.0) * 100, 1),
                                    "url": target_url,
                                    "scraped_by": user_name,
                                    "matched_title": c_name,
                                    "page_title": page_title,
                                }
                        except Exception:
                            continue
            except Exception:
                continue
    finally:
        session_pool.return_session((user_name, sess))
    return None


def calculate_overall_score(ratings):
    meta = ratings.get("metacritic")
    ss = ratings.get("screenscraper")
    meta_score = meta.get("metascore") if meta else None
    ss_pct = ss.get("percentage") if ss else None

    has_meta = meta_score is not None and 0 <= meta_score <= 100
    has_ss = ss_pct is not None and 0 <= ss_pct <= 100

    if has_meta and has_ss:
        # Weighted composite: 70% Metacritic (critic consensus) + 30% ScreenScraper (retro community)
        return round(float(meta_score) * 0.7 + float(ss_pct) * 0.3, 1)
    elif has_meta:
        return round(float(meta_score), 1)
    elif has_ss:
        return round(float(ss_pct), 1)
    return 0.0


_progress_counter = 0
_progress_lock = threading.Lock()


def process_single_game(game, total, session_pool, platform_id=""):
    global _progress_counter
    mc_session = requests.Session()
    primary_title = (
        game.get("db_title")
        or game.get("title")
        or game.get("user_request")
        or game.get("searched_as")
        or "Unknown"
    )
    raw_fn = game.get("filename") or ""
    fn_title = clean_filename_to_title(raw_fn) if raw_fn else ""
    dlc_base_title = strip_dlc_and_addons(fn_title) or strip_dlc_and_addons(primary_title)

    candidates = []
    for c in [primary_title, fn_title, dlc_base_title]:
        if c and c not in candidates:
            candidates.append(c)

    ratings = game.get("ratings") or {}
    if "metacritic" not in ratings or not ratings["metacritic"]:
        for cand in candidates:
            mc_data = fetch_metacritic(mc_session, cand)
            if mc_data:
                ratings["metacritic"] = mc_data
                break

    if "screenscraper" not in ratings or not ratings["screenscraper"]:
        for cand in candidates:
            ss_data = fetch_screenscraper(session_pool, cand, platform_id)
            if ss_data:
                ratings["screenscraper"] = ss_data
                break

    if ("metacritic" not in ratings or not ratings["metacritic"]) and ("screenscraper" in ratings and ratings["screenscraper"]):
        ss_info = ratings["screenscraper"]
        ss_titles = []
        for key in ["page_title", "matched_title"]:
            t = ss_info.get(key)
            if t and t not in candidates and t not in ss_titles:
                ss_titles.append(t)
                dlc_t = strip_dlc_and_addons(t)
                if dlc_t and dlc_t not in candidates and dlc_t not in ss_titles:
                    ss_titles.append(dlc_t)
        for sst in ss_titles:
            mc_data = fetch_metacritic(mc_session, sst)
            if mc_data:
                ratings["metacritic"] = mc_data
                break

    if ("screenscraper" not in ratings or not ratings["screenscraper"]) and ("metacritic" in ratings and ratings["metacritic"]):
        mc_info = ratings["metacritic"]
        mc_canon = mc_info.get("canonical_title")
        if mc_canon and mc_canon not in candidates:
            mc_titles = [mc_canon]
            dlc_mc = strip_dlc_and_addons(mc_canon)
            if dlc_mc and dlc_mc not in candidates and dlc_mc not in mc_titles:
                mc_titles.append(dlc_mc)
            for mct in mc_titles:
                ss_data = fetch_screenscraper(session_pool, mct, platform_id)
                if ss_data:
                    ratings["screenscraper"] = ss_data
                    break

    overall = calculate_overall_score(ratings)
    game["overall_rank_score"] = overall
    game["ratings"] = ratings
    with _progress_lock:
        _progress_counter += 1
        curr = _progress_counter
        pct = (curr / total) * 100
    mc_str = f"Metacritic: {ratings['metacritic']['metascore']}" if "metacritic" in ratings else "Metacritic: --"
    ss_str = f"ScreenScraper: {ratings['screenscraper']['rating']}/20 ({ratings['screenscraper']['percentage']}%)" if "screenscraper" in ratings else "ScreenScraper: --"
    print(f"[{curr:4d}/{total:4d} | {pct:4.1f}%] {primary_title[:42]:42} -> Score: {overall:5.1f} | {mc_str} | {ss_str}")
    return game


def find_default_input():
    cwd = os.getcwd()
    named_candidates = ["database.json", "metadata.json", "games.json", "roms.json"]
    for name in named_candidates:
        candidate_path = os.path.join(cwd, name)
        if os.path.exists(candidate_path):
            return os.path.abspath(candidate_path)
    try:
        for fname in os.listdir(cwd):
            if fname.endswith(".json") and not fname.startswith("sorted_") and fname != "screenscraper_config.json":
                candidate_path = os.path.join(cwd, fname)
                try:
                    with open(candidate_path, "r", encoding="utf-8") as f:
                        content = json.load(f)
                        if isinstance(content, list) or (isinstance(content, dict) and "games" in content):
                            return os.path.abspath(candidate_path)
                except Exception:
                    pass
    except Exception:
        pass
    return os.path.join(cwd, "database.json")


def main():
    parser = argparse.ArgumentParser(
        description="Enrich retro game metadata with Metacritic & ScreenScraper scores and sort descending."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="Input JSON file path (defaults to auto-detected database.json / metadata.json)",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output JSON file path (defaults to sorted_<input>.json)",
    )
    parser.add_argument(
        "-i", "--in-place",
        dest="in_place",
        action="store_true",
        help="Overwrite input JSON file directly",
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=15,
        help="Number of concurrent worker threads (default: 15)",
    )
    parser.add_argument(
        "--ss-user",
        default=None,
        help="ScreenScraper username",
    )
    parser.add_argument(
        "--ss-pass",
        default=None,
        help="ScreenScraper password",
    )
    parser.add_argument(
        "--platform",
        default=None,
        help="Target gaming platform (e.g. ps1, gb, gba, snes, psp, nes) or ID. Auto-detected by default.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Number of top games to display in the Hall of Fame preview (auto-scales: 2k->200, 1k->100, 100->20)",
    )
    parser.add_argument(
        "--add-account",
        nargs=2,
        metavar=("USERNAME", "PASSWORD"),
        help="Add and test a new ScreenScraper account in screenscraper_config.json",
    )
    parser.add_argument(
        "--list-accounts",
        action="store_true",
        help="List all configured ScreenScraper accounts and test authentication",
    )
    args = parser.parse_args()
    if args.add_account:
        new_u, new_p = args.add_account[0].strip(), args.add_account[1].strip()
        print(f"\n[*] Testing ScreenScraper credentials for '{new_u}'...")
        ok, msg = test_single_account(new_u, new_p)
        if ok:
            print(f"[✓] Success: Account authenticated successfully!")
            accs = load_accounts()
            accs = [a for a in accs if a.get("username", "").lower() != new_u.lower()]
            accs.append({"username": new_u, "password": new_p})
            save_accounts(accs)
            print(f"[✓] Saved to {CONFIG_FILE}. Total accounts: {len(accs)}")
        else:
            print(f"[!] Authentication Failed: {msg}")
        sys.exit(0 if ok else 1)
    if args.list_accounts:
        accs = load_accounts()
        print("\n" + "=" * 60)
        print("CONFIGURED SCREENSCRAPER ACCOUNTS")
        print("=" * 60)
        if not accs:
            print("[!] No accounts found in", CONFIG_FILE)
        for i, a in enumerate(accs, 1):
            u = a.get("username", "")
            p = a.get("password", "")
            ok, msg = test_single_account(u, p)
            status = "✓ Authenticated (1 thread capacity)" if ok else f"✗ Failed ({msg})"
            print(f" {i}. {u:20} -> {status}")
        print("=" * 60)
        print(f"Total Parallel Capacity: {len(accs)} ScreenScraper threads\n")
        sys.exit(0)

    accounts = load_accounts(args.ss_user, args.ss_pass)
    session_pool = ScreenScraperSessionPool(accounts)

    print("\n" + "=" * 68)
    print("🎮 RETRO GAME RATING & RANKING ENGINE (METACRITIC & SCREENSCRAPER)")
    print("=" * 68)
    if session_pool.active_accounts:
        acc_list = ", ".join(session_pool.active_accounts)
        print(f"[*] ScreenScraper Accounts: {len(session_pool.active_accounts)} authenticated ({acc_list})")
        print(f"[*] ScreenScraper Capacity: {len(session_pool.active_accounts)} concurrent threads (Pool active)")
    else:
        print(f"[!] ScreenScraper Accounts: Anonymous mode (no accounts logged in)")
    print(f"[*] Account Config File   : {CONFIG_FILE}")

    input_arg = args.input or find_default_input()
    input_path = os.path.abspath(input_arg)
    if not os.path.exists(input_path):
        print(f"[!] Error: Input file '{input_path}' not found.")
        sys.exit(1)

    print(f"[*] Input Metadata File   : {input_path}")
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    is_dict = isinstance(data, dict)
    games = data.get("games", []) if is_dict else data
    total_games = len(games)
    platform_id, plat_reason = detect_platform_id(data, input_path, args.platform)

    print(f"[*] Target Platform       : {plat_reason} (ScreenScraper ID: {platform_id or 'All'})")
    print(f"[*] Loaded Games Count    : {total_games} games")
    print(f"[*] Worker Threads        : {args.threads}")
    print(f"[*] Press Ctrl+C at any time to instantly stop.")
    print("=" * 68 + "\n")

    if args.in_place:
        output_path = input_path
    elif args.output:
        output_path = os.path.abspath(args.output)
    else:
        dirname, basename = os.path.split(input_path)
        output_path = os.path.join(dirname, f"sorted_{basename}")
    enriched_games = []
    executor = ThreadPoolExecutor(max_workers=args.threads)
    try:
        future_map = {
            executor.submit(process_single_game, g, total_games, session_pool, platform_id): idx
            for idx, g in enumerate(games)
        }
        for future in as_completed(future_map):
            try:
                res = future.result()
                enriched_games.append(res)
            except Exception:
                pass
    except KeyboardInterrupt:
        force_exit("\n[!] Interrupted by user (Ctrl+C). Terminated immediately.")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    print("\n[*] Sorting entire collection by overall_rank_score (highest to lowest)...")
    enriched_games.sort(key=lambda x: x.get("overall_rank_score", 0.0), reverse=True)

    if is_dict:
        data["games"] = enriched_games
        data["total_games"] = len(enriched_games)
        data["sorted_by"] = "overall_rank_score (descending)"
        data["last_ranked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        final_output = data
    else:
        final_output = enriched_games

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_output, f, indent=2, ensure_ascii=False)

    print(f"\n[✓] Successfully saved {len(enriched_games)} sorted games to:")
    print(f"    -> {output_path}")

    total_scraped = len(enriched_games)
    if args.top is not None and args.top > 0:
        top_n = min(total_scraped, args.top)
    elif total_scraped >= 2000:
        top_n = 200
    elif total_scraped >= 1000:
        top_n = 100
    elif total_scraped >= 500:
        top_n = 50
    elif total_scraped >= 100:
        top_n = 20
    else:
        top_n = min(15, total_scraped)

    print("\n" + "=" * 80)
    print(f"TOP {top_n} HIGHEST RATED GAMES (HALL OF FAME)")
    print("=" * 80)
    seen_titles = set()
    rank = 1
    for g in enriched_games:
        if rank > top_n:
            break
        title = g.get("db_title") or g.get("user_request") or g.get("title")
        norm_t = re.sub(r"[^a-z0-9]", "", str(title).lower())
        if norm_t in seen_titles:
            continue
        seen_titles.add(norm_t)
        score = g.get("overall_rank_score", 0.0)
        ratings = g.get("ratings", {})
        mc = ratings.get("metacritic", {}).get("metascore", "--")
        ss = ratings.get("screenscraper", {}).get("rating", "--")
        ss_str = f"{ss}/20" if ss != "--" else "--"
        print(f" {rank:3d}. [{score:5.1f}/100] {title[:40]:40} (Metascore: {str(mc):>2} | ScreenScraper: {ss_str})")
        rank += 1
    print("=" * 80)

if __name__ == "__main__":
    main()
