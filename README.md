# 🎮 Retro ROM Toolkit

A fast and reliable Python toolkit to download retro game collections, rank games by review scores and compress disc images to save storage space.

---

## 📌 Overview

Retro ROM Toolkit provides three standalone tools designed to work together or independently.

| Tool | Purpose | Primary Output |
| :--- | :--- | :--- |
| **rom_scraper.py** | Scrape game lists and download ROMs with high speed | Game files + database.json |
| **rom_ranker.py** | Score and rank games using Metacritic and ScreenScraper ratings | sorted_database.json |
| **rom_compressor.py** | Auto-detect game formats and compress them to save disk space | .cso (PSP) and .chd (PS1/PS2) |

---

## ✨ Features

### 1. Game Scraper & Downloader (rom_scraper.py)
- **Fast Accelerated Downloads**: Powered by aria2 for stable, high-speed downloads.
- **Automatic Fallback**: Falls back to standard Python downloads if aria2 is not installed.
- **Link Refresh**: Automatically refreshes download links if a link expires during long downloads.
- **Custom Game Fetch Limit**: Choose how many games to fetch (e.g. top 100 or 500) instead of being forced to scrape all thousands of games (`-l` / `--limit`).
- **Rich Metadata Collection**: Builds a complete catalog (`database.json`) storing game titles, file sizes, direct download URLs, community user ratings (out of 5.0) and vote counts (`--metadata-only`).
- **Auto-Compress on Download**: Automatically compress downloaded games based on their format (.cso for PSP, .chd for PS1/PS2/discs) as soon as each download finishes (`--compress`).

### 2. ROM Ranker (rom_ranker.py)
- **Dual Review Scoring**: Combines **70% Metacritic** (critic score) and **30% ScreenScraper.fr** (community score) into an overall rating out of 100.
- **Multi-Account Support**: Add multiple free ScreenScraper accounts to run many search requests in parallel without hitting rate limits.
- **Leaderboard Preview**: Shows a clean preview of your highest-ranked games directly in your terminal.

### 3. ROM Compressor (rom_compressor.py)
- **Automatic Console Detection**: Inspects file headers inside archives to detect the correct console platform automatically.
- **PSP Games**: Compresses PSP ISOs into high-performance **.cso** format using **maxcso** (fully compatible with PPSSPP and PS Vita).
- **PS2 Games**: Converts DVD and CD images into verified **.chd** files using MAME chdman.
- **PS1 & Sega Games**: Decompresses .ecm files, creates missing .cue sheets when needed and converts discs into verified **.chd** files.
- **Cartridge Games**: Extracts clean ROM files (.gba, .nds, .gb, etc.) from .zip and .7z archives.
- **Safe Verification**: Tests the converted file before deleting original files so you never lose data. Saves 40% to 70% disk space.

---

## 📦 Installation

Ensure you have **Python 3.8 or newer** installed.

```bash
git clone https://github.com/your-username/retro-rom-toolkit.git
cd retro-rom-toolkit
pip install -r requirements.txt
```

---

## 🚀 Usage Guide

### 1. Download Games (rom_scraper.py)

#### Basic interactive mode

Run the script and paste any [romsgames.net](https://www.romsgames.net/) category URL when prompted:
```bash
python rom_scraper.py
```

#### Only collect game lists and sizes (No ROM downloads)

This creates a database.json file with all game titles, file sizes and URLs:
```bash
python rom_scraper.py "https://www.romsgames.net/roms/playstation-portable/?page=1&sort=popularity" --metadata-only
```

#### Download a complete system automatically

```bash
python rom_scraper.py "https://www.romsgames.net/roms/gameboy-advance/?page=1&sort=popularity" --auto -t 30 -d 1
```

#### Available Options

| Flag | Description | Default |
| :--- | :--- | :--- |
| `-o`, `--output-dir` | Folder where games and database will be saved | Current directory |
| `-l`, `--limit` | Maximum number of games to scrape/fetch from web (e.g. `100`, `500`) | All games |
| `-t`, `--scrape-threads` | Number of worker threads used to scan web pages | `30` |
| `-d`, `--download-threads` | Number of games to download at the same time | `1` |
| `-s`, `--split-connections` | Number of download connections per file (for aria2) | `8` |
| `-c`, `--compress` | Automatically compress downloaded games without prompting | Prompts if omitted |
| `--no-compress` | Keep downloads as-is and skip compression prompt | `False` |
| `-m`, `--metadata-only` | Only build database.json, download zero game files | `False` |
| `-a`, `--auto` | Skip confirmation prompts and download everything | `False` |
| `-r`, `--refresh-cache` | Ignore saved database.json and scrape fresh data | `False` |
| `--no-aria2` | Disable aria2 and use standard Python requests | `False` |

---

### 2. Rank Your Collection (rom_ranker.py)

#### Step 1: Add your ScreenScraper account
ScreenScraper allows free accounts 1 request at a time. You can add one or more free accounts to speed up searches:
```bash
python rom_ranker.py --add-account "YOUR_USERNAME" "YOUR_PASSWORD"
```

To view your configured accounts and test their connection:
```bash
python rom_ranker.py --list-accounts
```

#### Step 2: Rank your games
Run the script in the folder where your database.json file is located:
```bash
python rom_ranker.py -t 15
```

You can also specify a target platform manually:
```bash
python rom_ranker.py database.json --platform psp
python rom_ranker.py database.json --platform ps1
python rom_ranker.py database.json --platform gba
```

#### Available Options
| Flag | Description | Default |
| :--- | :--- | :--- |
| `-o`, `--output` | Name of the output JSON file | `sorted_<input>.json` |
| `-i`, `--in-place` | Save results directly back into the input JSON file | `False` |
| `-t`, `--threads` | Number of games to score in parallel | `15` |
| `--platform` | Force a specific platform (psp, ps1, ps2, gba, snes, etc.) | Auto-detected |
| `--top` | Number of top games to display in the terminal leaderboard | Auto-scaled |

---

### 3. Compress Games to Save Space (rom_compressor.py)

The compressor automatically identifies the game system, extracts archives and converts discs into the best compressed format:
- **PSP games** $\rightarrow$ Compressed to **.cso**
- **PS2 DVD games** $\rightarrow$ Compressed to **.chd** (DVD mode)
- **PS1 / Sega games** $\rightarrow$ Compressed to **.chd** (CD mode)
- **Cartridge games** $\rightarrow$ Extracted to clean ROM files

#### Compress an entire folder of games
```bash
python rom_compressor.py "./roms/psp" -t 4
python rom_compressor.py "./roms/playstation" -t 4
```

#### Compress a single game archive
```bash
python rom_compressor.py "God of War Ghost of Sparta.zip"
python rom_compressor.py "Final Fantasy VII.zip"
python rom_compressor.py "Gran Turismo 4.iso"
```

#### Test run without making any changes
```bash
python rom_compressor.py "./roms/playstation" --dry-run
```

#### Available Options
| Flag | Description | Default |
| :--- | :--- | :--- |
| `-o`, `--output-dir` | Folder to save converted games | Same as source |
| `-t`, `--threads` | Number of games to compress at the same time | `2` |
| `-k`, `--keep-source` | Keep the original archive files after successful conversion | `False` |
| `--dry-run` | Show which files would be converted without making changes | `False` |

---

## 📊 Sample Output

### 1. Scraper Catalog (`database.json`)
Generated by `rom_scraper.py`, capturing sizes, direct download links, user ratings, and community votes:

```json
{
  "title": "Super Mario 64",
  "url": "https://www.romsgames.net/nintendo-64-rom-super-mario-64/",
  "media_id": "3452",
  "size": "6.10 MB",
  "size_bytes": 6396313,
  "rating": 4.04,
  "votes": 17911,
  "download_url": "https://static.romsgames.net/...",
  "filename": "Super Mario 64.zip",
  "status": "pending"
}
```

### 2. Ranked Masterpieces (`sorted_database.json`)
Generated by `rom_ranker.py`, computing composite scores combining 70% Metacritic and 30% ScreenScraper:

```json
{
  "db_title": "God of War: Chains of Olympus",
  "overall_rank_score": 89.2,
  "ratings": {
    "metacritic": {
      "metascore": 91,
      "user_score": 8.8,
      "review_count": 72,
      "sentiment": "Universal Acclaim",
      "url": "https://www.metacritic.com/game/god-of-war-chains-of-olympus/"
    },
    "screenscraper": {
      "rating": 17.0,
      "rating_max": 20,
      "percentage": 85.0,
      "url": "https://www.screenscraper.fr/gameinfos.php?plateforme=61&gameid=27863"
    }
  }
}
```

---

## 🎖️ Credits & Acknowledgements

This project wouldn't have been possible without the work of these amazing projects and their contributors:

* **[aria2](https://github.com/aria2/aria2)**: High-speed multi-connection download utility.
* **[maxcso](https://github.com/unknownbrackets/maxcso)**: Fast multi-threaded ISO to CSO compressor for PSP games.
* **[chdman](https://www.mamedev.org/)**: Official disc image compressor for lossless .chd files.
* **[unecm](https://github.com/neillcorlett/corlett-source/tree/master/ecm)**: Utility for decompressing PlayStation ECM files.
* **[7-Zip](https://www.7-zip.org/)**: File archiver used for extracting .7z and .rar files.
* **[ScreenScraper.fr](https://www.screenscraper.fr/)**: Comprehensive database for retro game metadata and player ratings.
* **[Metacritic](https://www.metacritic.com/)**: Video game review aggregation and critic scores.
* **[romsgames.net](https://www.romsgames.net/)**: Online catalog for retro video game archives.

---

## 👨‍💻 Author

**Shivam Raj** ([@BetterCallShiv](https://github.com/BetterCallShiv))
- Email: [bettercallshiv@gmail.com](mailto:bettercallshiv@gmail.com)
- GitHub: [github.com/BetterCallShiv](https://github.com/BetterCallShiv)

