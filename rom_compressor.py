#!/usr/bin/env python3

##########################################################################
#   Script: Retro ROM Compressor
#   Author: @BetterCallShiv
#   Features: 
#       - Automatically identifies console platform from archives & disc headers.
#       - PSP: Automatically compresses ISOs to high-performance .cso (CISO).
#       - PS2: Automatically compresses DVD/CD images to .chd (createdvd/createcd).
#       - PS1 / Sega: Automatically converts CUE/BIN/ECM to verified .chd (createcd).
#       - Extracts .zip, .7z and .rar archives before converting.
#       - Decompresses .ecm files and auto-generates missing .cue sheets on the fly.
#       - Strict integrity verification (chdman verify) before removing original files.
#       - Zero hardcoded paths: auto-prompts to download dependencies if missing.
#       - Multi-threaded batch conversion support for entire game libraries.
#   How to Use:
#       1. Run on any library folder: python rom_compressor.py "path/to/roms"
#       2. Or compress a single game: python rom_compressor.py "game.zip"
#       3. Preview without converting: python rom_compressor.py "path/to/roms" --dry-run
##########################################################################

import argparse
import io
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_active_work_dirs = set()
_active_procs = set()
_lock = threading.Lock()


def find_tool(name):
    found = shutil.which(name) or shutil.which(f"{name}.exe")
    if found:
        return found
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, f"{name}.exe"),
        os.path.join(script_dir, name),
        os.path.join(script_dir, "tools", f"{name}.exe"),
        os.path.join(script_dir, "tools", name),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


CHDMAN_BIN = find_tool("chdman")
MAXCSO_BIN = find_tool("maxcso")
UNECM_BIN = find_tool("unecm")
SEVENZIP_BIN = find_tool("7z")
FFMPEG_BIN = find_tool("ffmpeg")


def convert_to_chd(*args, **kwargs):
    return compress_rom(*args, **kwargs)


def force_exit(msg="\n[!] Force quit requested (Ctrl+C). Cleaning up and exiting..."):
    with _lock:
        for p in list(_active_procs):
            try:
                p.kill()
            except Exception:
                pass
        for wd in list(_active_work_dirs):
            try:
                shutil.rmtree(wd, ignore_errors=True)
            except Exception:
                pass
    print(msg, flush=True)
    os._exit(0)


def sigint_handler(signum=None, frame=None):
    force_exit()
signal.signal(signal.SIGINT, sigint_handler)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, sigint_handler)


def format_bytes(b):
    if b < 1024:
        return f"{b} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.2f} KB"
    elif b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.2f} MB"
    else:
        return f"{b / (1024 * 1024 * 1024):.2f} GB"


def auto_download_chdman(target_dir=None):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if not target_dir:
        target_dir = os.path.join(script_dir, "tools")
    os.makedirs(target_dir, exist_ok=True)
    target_exe = os.path.join(target_dir, "chdman.exe" if sys.platform == "win32" else "chdman")
    if os.path.isfile(target_exe):
        return target_exe
    pkg_map = {
        "win32": "@emmercm/chdman-win32-x64",
        "darwin": "@emmercm/chdman-darwin-arm64",
        "linux": "@emmercm/chdman-linux-x64",
    }
    pkg = pkg_map.get(sys.platform, "@emmercm/chdman-win32-x64")
    metadata_url = f"https://registry.npmjs.org/{pkg}/latest"
    print(f"[*] Fetching chdman package metadata...", flush=True)
    req = urllib.request.Request(metadata_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
        tarball_url = data["dist"]["tarball"]
    print(f"[*] Downloading chdman binary...", flush=True)
    req_tb = urllib.request.Request(tarball_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req_tb, timeout=40) as resp_tb:
        tb_bytes = resp_tb.read()
    with tarfile.open(fileobj=io.BytesIO(tb_bytes), mode="r:gz") as tf:
        for member in tf.getmembers():
            if member.name.endswith("chdman.exe") or member.name.endswith("/chdman"):
                extracted_f = tf.extractfile(member)
                if extracted_f:
                    with open(target_exe, "wb") as out_f:
                        out_f.write(extracted_f.read())
                    if sys.platform != "win32":
                        os.chmod(target_exe, 0o755)
                    print(f"[OK] chdman installed successfully: {target_exe}", flush=True)
                    return target_exe
    return None


def ensure_chdman(interactive=True):
    global CHDMAN_BIN
    if CHDMAN_BIN and os.path.isfile(CHDMAN_BIN):
        return CHDMAN_BIN
    found = find_tool("chdman")
    if found and os.path.isfile(found):
        CHDMAN_BIN = found
        return CHDMAN_BIN
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tools_dir = os.path.join(script_dir, "tools")
    print(f"""
========================================================================
[!] 'chdman' was not found in your system PATH or script directory.
    chdman is required to convert disc-based games into .chd files.
========================================================================
""")
    if interactive:
        try:
            choice = input(f"Would you like to auto-download chdman to '{tools_dir}'? (y/n) [default: y]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n[!] Exited by user.")
            sys.exit(0)
    else:
        choice = "n"
    if choice in ("", "y", "yes"):
        try:
            downloaded = auto_download_chdman(tools_dir)
            if downloaded and os.path.isfile(downloaded):
                CHDMAN_BIN = downloaded
                return CHDMAN_BIN
        except Exception as e:
            print(f"[!] Auto-download failed: {e}")
    print(f"""
[!] Please download 'chdman.exe' manually and place it in:
    {tools_dir} (or your system PATH), then run the script again.
""")
    return None


def auto_download_maxcso(target_dir=None):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if not target_dir:
        target_dir = os.path.join(script_dir, "tools")
    os.makedirs(target_dir, exist_ok=True)
    exe_name = "maxcso.exe" if sys.platform == "win32" else "maxcso"
    target_exe = os.path.join(target_dir, exe_name)
    if os.path.isfile(target_exe):
        return target_exe
    machine = platform.machine().lower()
    is_arm = "arm" in machine or "aarch64" in machine
    if sys.platform == "win32":
        pkg = "@emmercm/maxcso-win32-x64"
    elif sys.platform == "darwin":
        pkg = "@emmercm/maxcso-darwin-arm64" if is_arm else "@emmercm/maxcso-darwin-x64"
    else:
        pkg = "@emmercm/maxcso-linux-arm64" if is_arm else "@emmercm/maxcso-linux-x64"
    metadata_url = f"https://registry.npmjs.org/{pkg}/latest"
    print(f"[*] Fetching maxcso binary for {sys.platform} ({machine})...", flush=True)
    try:
        req = urllib.request.Request(metadata_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            tarball_url = data["dist"]["tarball"]
        print(f"[*] Downloading maxcso binary...", flush=True)
        req_tb = urllib.request.Request(tarball_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req_tb, timeout=30) as resp_tb:
            tb_bytes = resp_tb.read()
        with tarfile.open(fileobj=io.BytesIO(tb_bytes), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.name.endswith("maxcso.exe") or member.name.endswith("/maxcso"):
                    extracted_f = tf.extractfile(member)
                    if extracted_f:
                        with open(target_exe, "wb") as out_f:
                            out_f.write(extracted_f.read())
                        if sys.platform != "win32":
                            os.chmod(target_exe, 0o755)
                        print(f"[OK] maxcso installed successfully: {target_exe}", flush=True)
                        return target_exe
    except Exception as e:
        print(f"[!] Auto-download failed: {e}", flush=True)
    return None


def ensure_maxcso(interactive=True):
    global MAXCSO_BIN
    if MAXCSO_BIN and os.path.isfile(MAXCSO_BIN):
        return MAXCSO_BIN
    found = find_tool("maxcso")
    if found and os.path.isfile(found):
        MAXCSO_BIN = found
        return MAXCSO_BIN
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tools_dir = os.path.join(script_dir, "tools")
    print(f"""
========================================================================
[!] 'maxcso' was not found in your system PATH or script directory.
Downlaod maxcso from the official GitHub repository:
https://github.com/unknownbrackets/maxcso
========================================================================
""")
    if interactive:
        try:
            choice = input(f"Would you like to auto-download maxcso to '{tools_dir}'? (y/n) [default: y]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n[!] Exited by user.")
            sys.exit(0)
    else:
        choice = "n"
    if choice in ("", "y", "yes"):
        try:
            downloaded = auto_download_maxcso(tools_dir)
            if downloaded and os.path.isfile(downloaded):
                MAXCSO_BIN = downloaded
                return MAXCSO_BIN
        except Exception as e:
            print(f"[!] Auto-download failed: {e}")
    if sys.platform == "darwin":
        manual_hint = "macOS: brew install maxcso\n    Or compile: git clone https://github.com/unknownbrackets/maxcso.git && cd maxcso && make"
    elif sys.platform.startswith("linux"):
        manual_hint = "Arch Linux: sudo pacman -S maxcso\n    Ubuntu/Debian: git clone https://github.com/unknownbrackets/maxcso.git && cd maxcso && make"
    else:
        manual_hint = f"Windows: Download maxcso_windows.7z from https://github.com/unknownbrackets/maxcso/releases\n    and place 'maxcso.exe' in: {tools_dir} (or add to system PATH)."
    print(f"""
[!] Manual Installation Instructions:
    {manual_hint}
    Then run the script again.
""")
    return None


def create_cue_sheet(bin_path, cue_path):
    bin_name = os.path.basename(bin_path)
    cue_content = f'FILE "{bin_name}" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
    with open(cue_path, "w", encoding="utf-8") as f:
        f.write(cue_content)


def compress_iso_to_cso(iso_path, cso_path, num_threads=None):
    maxcso_tool = ensure_maxcso(interactive=True) or find_tool("maxcso")
    if not maxcso_tool or not os.path.isfile(maxcso_tool):
        return False
    threads_arg = f"--threads={num_threads or os.cpu_count() or 4}"
    cmd = [maxcso_tool, iso_path, "-o", cso_path, threads_arg, "--quiet"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with _lock:
        _active_procs.add(proc)
    res_code = proc.wait()
    with _lock:
        _active_procs.discard(proc)
    return res_code == 0 and os.path.isfile(cso_path) and os.path.getsize(cso_path) > 0


def inspect_disc_image(file_path):
    fn = os.path.basename(file_path).lower()
    parent = os.path.basename(os.path.dirname(file_path)).lower()
    context = fn + " " + parent
    if any(k in context for k in ["psp", "playstation-portable", "playstation portable", "umd"]):
        return "psp"
    if any(k in context for k in ["ps2", "playstation-2", "playstation 2"]):
        return "ps2"
    if any(k in context for k in ["ps1", "psx", "playstation-1", "playstation"]):
        return "ps1"
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".iso", ".bin", ".img"):
        try:
            with open(file_path, "rb") as f:
                header = f.read(2 * 1024 * 1024)
            if b"PSP_GAME" in header or b"UMD_DATA.BIN" in header:
                return "psp"
            if b"BOOT2" in header:
                return "ps2"
            if b"BOOT = cdrom:" in header or b"PLAYSTATION" in header:
                return "ps1"
        except Exception:
            pass
    size = os.path.getsize(file_path) if os.path.isfile(file_path) else 0
    if size > 750 * 1024 * 1024:
        return "ps2"
    return "ps1"


def compress_rom(src_path, dest_dir=None, keep_source=False, num_threads=None):
    if not os.path.isfile(src_path):
        return False, "Source file not found", 0, 0, []
    src_path = os.path.abspath(src_path)
    if dest_dir is None:
        dest_dir = os.path.dirname(src_path)
    os.makedirs(dest_dir, exist_ok=True)
    src_fn = os.path.basename(src_path)
    base_title = os.path.splitext(src_fn)[0]
    ext = os.path.splitext(src_fn)[1].lower()
    src_size = os.path.getsize(src_path)
    if ext in (".chd", ".cso"):
        target_f = os.path.join(dest_dir, src_fn)
        if target_f != src_path and not os.path.exists(target_f):
            shutil.copy2(src_path, target_f)
        return True, f"Already {ext.upper()}", src_size, src_size, [target_f]
    work_id = f"cmp_{os.getpid()}_{threading.current_thread().ident}_{int(time.time()*1000)}"
    work_dir = os.path.join(tempfile.gettempdir(), work_id)
    os.makedirs(work_dir, exist_ok=True)
    with _lock:
        _active_work_dirs.add(work_dir)
    try:
        if ext in (".zip", ".7z", ".rar"):
            extracted = False
            if ext == ".zip":
                try:
                    with zipfile.ZipFile(src_path, "r") as zf:
                        zf.extractall(work_dir)
                    extracted = True
                except Exception:
                    extracted = False
            if not extracted:
                if not SEVENZIP_BIN:
                    return False, "7z executable required to extract .7z/.rar archives", src_size, 0, []
                cmd_7z = [SEVENZIP_BIN, "x", "-y", f"-o{work_dir}", src_path]
                res_x = subprocess.run(cmd_7z, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res_x.returncode not in (0, 1):
                    return False, f"Archive extraction failed (code {res_x.returncode})", src_size, 0, []
        else:
            shutil.copy2(src_path, work_dir)
        extracted_files = []
        for dp, _, filenames in os.walk(work_dir):
            for f in filenames:
                extracted_files.append(os.path.join(dp, f))
        iso_files = [f for f in extracted_files if f.lower().endswith(".iso")]
        cue_files = [f for f in extracted_files if f.lower().endswith(".cue")]
        toc_files = [f for f in extracted_files if f.lower().endswith(".toc")]
        gdi_files = [f for f in extracted_files if f.lower().endswith(".gdi")]
        cart_files = [f for f in extracted_files if f.lower().endswith((".gba", ".nds", ".gb", ".gbc", ".nes", ".sfc", ".smc", ".n64", ".z64", ".md"))]
        is_psp = False
        psp_iso = None
        if iso_files:
            for iso in iso_files:
                if inspect_disc_image(iso) == "psp" or "psp" in src_fn.lower() or "playstation-portable" in src_path.lower():
                    is_psp = True
                    psp_iso = iso
                    break
        if is_psp and psp_iso:
            target_cso = os.path.join(dest_dir, f"{base_title}.cso")
            temp_cso = os.path.join(work_dir, "output.cso")
            ok_cso = compress_iso_to_cso(psp_iso, temp_cso)
            if not ok_cso or not os.path.exists(temp_cso):
                return False, "PSP CSO compression failed", src_size, 0, []
            if os.path.exists(target_cso):
                os.remove(target_cso)
            shutil.move(temp_cso, target_cso)
            chd_size = os.path.getsize(target_cso)
            if not keep_source and ext in (".zip", ".7z", ".rar") and os.path.exists(src_path):
                try:
                    os.remove(src_path)
                except OSError:
                    pass
            return True, "PSP CSO Compressed", src_size, chd_size, [target_cso]
        if cart_files and not iso_files and not cue_files:
            created = []
            for cf in cart_files:
                target_rom = os.path.join(dest_dir, os.path.basename(cf))
                if os.path.exists(target_rom):
                    os.remove(target_rom)
                shutil.move(cf, target_rom)
                created.append(target_rom)
            if not keep_source and ext in (".zip", ".7z", ".rar") and os.path.exists(src_path):
                try:
                    os.remove(src_path)
                except OSError:
                    pass
            final_size = sum(os.path.getsize(c) for c in created)
            return True, "Extracted Clean ROM", src_size, final_size, created
        chdman_tool = ensure_chdman(interactive=False)
        if not chdman_tool:
            return False, "chdman not found! Run 'python rom_compressor.py' to auto-download.", src_size, 0, []
        ecm_files = [f for f in extracted_files if f.lower().endswith(".ecm")]
        if ecm_files:
            if not UNECM_BIN:
                return False, "unecm executable required for .ecm sector files", src_size, 0, []
            for ecm in ecm_files:
                out_bin = os.path.splitext(ecm)[0]
                res_u = subprocess.run([UNECM_BIN, ecm, out_bin], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if res_u.returncode != 0 or not os.path.exists(out_bin):
                    return False, f"Failed to decompress ECM file: {os.path.basename(ecm)}", src_size, 0, []
                try:
                    os.remove(ecm)
                except OSError:
                    pass
            for dp, _, filenames in os.walk(work_dir):
                for f in filenames:
                    if f.lower().endswith(".cue"):
                        cue_path = os.path.join(dp, f)
                        try:
                            with open(cue_path, "r", encoding="utf-8", errors="replace") as cf:
                                txt = cf.read()
                            txt = re.sub(r'\.bin\.ecm', '.bin', txt, flags=re.I)
                            txt = re.sub(r'\.img\.ecm', '.img', txt, flags=re.I)
                            with open(cue_path, "w", encoding="utf-8") as cf:
                                cf.write(txt)
                        except Exception:
                            pass
        if FFMPEG_BIN:
            for dp, _, filenames in os.walk(work_dir):
                for f in filenames:
                    if f.lower().endswith(".ape"):
                        ape_p = os.path.join(dp, f)
                        out_bin = os.path.splitext(ape_p)[0] + ".bin"
                        subprocess.run([FFMPEG_BIN, "-y", "-i", ape_p, "-f", "s16le", "-ar", "44100", "-ac", "2", out_bin], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        if os.path.exists(out_bin):
                            try:
                                os.remove(ape_p)
                            except OSError:
                                pass
        all_now = []
        for dp, _, filenames in os.walk(work_dir):
            for f in filenames:
                all_now.append(os.path.join(dp, f))
        cue_now = [f for f in all_now if f.lower().endswith(".cue")]
        toc_now = [f for f in all_now if f.lower().endswith(".toc")]
        gdi_now = [f for f in all_now if f.lower().endswith(".gdi")]
        iso_now = [f for f in all_now if f.lower().endswith(".iso")]
        input_discs = []
        if cue_now:
            input_discs = [(c, "createcd") for c in sorted(cue_now)]
        elif gdi_now:
            input_discs = [(g, "createcd") for g in sorted(gdi_now)]
        elif toc_now:
            input_discs = [(t, "createcd") for t in sorted(toc_now)]
        elif iso_now:
            for i_file in sorted(iso_now):
                sys_type = inspect_disc_image(i_file)
                mode = "createdvd" if sys_type == "ps2" else "createcd"
                input_discs.append((i_file, mode))
        else:
            raw_discs = [
                f for f in all_now
                if f.lower().endswith((".bin", ".img", ".mdf"))
                and not f.lower().endswith(".ecm")
            ]
            if raw_discs:
                discs_map = {}
                for rf in sorted(raw_discs):
                    fn = os.path.basename(rf)
                    m = re.search(r'(Disc\s*\d+|CD\s*\d+)', fn, re.I)
                    disc_key = m.group(1).lower() if m else "main"
                    if disc_key not in discs_map:
                        discs_map[disc_key] = rf
                    else:
                        if "track 1" in fn.lower() or "track 01" in fn.lower():
                            discs_map[disc_key] = rf

                for dkey, rf in sorted(discs_map.items()):
                    gen_cue = os.path.splitext(rf)[0] + ".cue"
                    create_cue_sheet(rf, gen_cue)
                    input_discs.append((gen_cue, "createcd"))
        if not input_discs:
            return False, "No convertible disc image or .cue sheet found", src_size, 0, []
        created_chds = []
        cpus = str(num_threads or os.cpu_count() or 4)
        for idx, (disc_input, cmd_mode) in enumerate(input_discs, 1):
            disc_dir = os.path.dirname(disc_input)
            disc_fn = os.path.basename(disc_input)
            temp_chd = os.path.join(work_dir, f"_out_{idx}.chd")
            if len(input_discs) == 1:
                target_name = f"{base_title}.chd"
            else:
                disc_raw = os.path.splitext(disc_fn)[0]
                m_disc = re.search(r'(Disc\s*\d+|CD\s*\d+)', disc_raw, re.I)
                sub_label = m_disc.group(1).title() if m_disc else f"Disc {idx}"
                target_name = f"{base_title} ({sub_label}).chd"
            final_chd = os.path.join(dest_dir, target_name)
            cmd_create = [chdman_tool, cmd_mode, "-i", disc_fn, "-o", temp_chd, "--numprocessors", cpus]
            proc = subprocess.Popen(cmd_create, cwd=disc_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with _lock:
                _active_procs.add(proc)
            res_code = proc.wait()
            with _lock:
                _active_procs.discard(proc)
            if res_code != 0 or not os.path.exists(temp_chd) or os.path.getsize(temp_chd) == 0:
                return False, f"chdman {cmd_mode} failed for {disc_fn}", src_size, 0, []
            cmd_verify = [chdman_tool, "verify", "-i", temp_chd]
            proc_v = subprocess.Popen(cmd_verify, cwd=disc_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with _lock:
                _active_procs.add(proc_v)
            res_v = proc_v.wait()
            with _lock:
                _active_procs.discard(proc_v)
            if res_v != 0:
                return False, f"chdman verification failed for {disc_fn}", src_size, 0, []
            if os.path.exists(final_chd):
                os.remove(final_chd)
            shutil.move(temp_chd, final_chd)
            created_chds.append(final_chd)
        total_chd_bytes = sum(os.path.getsize(c) for c in created_chds)
        if not keep_source and ext in (".zip", ".7z", ".rar") and os.path.exists(src_path):
            try:
                os.remove(src_path)
            except OSError:
                pass
        return True, "CHD Verified", src_size, total_chd_bytes, created_chds
    finally:
        with _lock:
            _active_work_dirs.discard(work_dir)
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


convert_to_chd = compress_rom


def main():
    parser = argparse.ArgumentParser(
        description="Retro ROM auto-identifier and compressor: converts PSP ISOs to .cso, PS1/PS2/Saturn/Dreamcast to .chd."
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="One or more directories or game archives to compress",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=None,
        help="Target folder for converted files (defaults to same folder as source)",
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=2,
        help="Number of games to convert in parallel (default: 2)",
    )
    parser.add_argument(
        "-k", "--keep-source",
        action="store_true",
        help="Keep original .zip/.7z archive after conversion (default: delete on verified success)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List files that would be converted without making changes",
    )
    args = parser.parse_args()
    print("""
===================================
   RETRO ROM AUTO-COMPRESSOR 
===================================
""")
    chdman_tool = ensure_chdman(interactive=True)
    if not chdman_tool:
        sys.exit(1)
    print(f"  chdman Engine : {chdman_tool}")
    print(f"  maxcso Engine : {MAXCSO_BIN or 'Auto-download on demand via GitHub'}")
    print(f"  unecm Engine  : {UNECM_BIN or 'Not found (optional for .ecm)'}")
    print(f"  7z Engine     : {SEVENZIP_BIN or 'Not found (using zipfile fallback)'}")
    print("=" * 60)
    candidates = []
    for p in args.paths:
        if os.path.isfile(p):
            candidates.append(p)
        elif os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in sorted(files):
                    if f.lower().endswith((".zip", ".7z", ".cue", ".iso", ".img", ".bin")):
                        candidates.append(os.path.join(root, f))
    candidates = sorted(list(set(candidates)))
    if not candidates:
        print("No convertible disc archives or images found.")
        sys.exit(0)
    print(f"\nFound {len(candidates):,} candidate game(s) to process.")
    if args.dry_run:
        print("\n[DRY RUN] The following files would be compressed:")
        for idx, c in enumerate(candidates, 1):
            print(f"  [{idx}] {os.path.basename(c)} ({format_bytes(os.path.getsize(c))})")
        return
    success = 0
    failed = 0
    total_src_bytes = 0
    total_final_bytes = 0
    t0 = time.time()
    print(f"\nStarting compression using {args.threads} worker(s)...\n", flush=True)


    def _worker(file_path):
        out_d = args.output_dir or os.path.dirname(file_path)
        ok, msg, sz_src, sz_final, outs = compress_rom(
            file_path,
            dest_dir=out_d,
            keep_source=args.keep_source,
            num_threads=None,
        )
        return file_path, ok, msg, sz_src, sz_final, outs
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = {ex.submit(_worker, c): c for c in candidates}
        for fut in as_completed(futs):
            f_path, ok, msg, sz_src, sz_final, outs = fut.result()
            fn = os.path.basename(f_path)
            total_src_bytes += sz_src
            total_final_bytes += sz_final
            if ok:
                success += 1
                saved = sz_src - sz_final
                saved_str = f"| Saved: {format_bytes(saved)}" if saved > 0 else ""
                out_names = ", ".join(os.path.basename(c) for c in outs)
                print(f"  [OK] {fn} -> {out_names} ({format_bytes(sz_final)} from {format_bytes(sz_src)} {saved_str})", flush=True)
            else:
                failed += 1
                print(f"  [FAILED] {fn}: {msg}", flush=True)
    elapsed = time.time() - t0
    saved_total = total_src_bytes - total_final_bytes
    saved_pct = (saved_total / total_src_bytes * 100) if total_src_bytes > 0 else 0

    print(f"""
===================================
       COMPRESSION COMPLETE!    
===================================
 Successfully Converted : {success:,} / {len(candidates):,}
 Failed                 : {failed:,}
 Original Size          : {format_bytes(total_src_bytes)}
 Compressed Size        : {format_bytes(total_final_bytes)}
 Space Saved            : {format_bytes(saved_total)} ({saved_pct:.1f}% reduction)
 Time Taken             : {elapsed:.1f}s
===================================
""")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        force_exit()
