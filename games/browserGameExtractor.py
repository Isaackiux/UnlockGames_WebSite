"""
itch.io Browser Game Extractor  ─  v5.0
=========================================
Extrae completamente juegos web de itch.io para ejecución local offline.
Totalmente automático: no requiere interacción manual ni jugar el juego.

Uso:
    python itchiobrowserGameExtractor.py
    python itchiobrowserGameExtractor.py <url> <carpeta>

Fases automáticas:
  1. Crawl estático profundo  — sigue todos los JS/CSS/JSON/XML encontrados
  2. Predicción por motor     — prueba rutas canónicas según el motor detectado
  3. Manifests profundos      — re-escanea manifests descargados para descubrir más
  4. Captura headless         — ejecuta el juego 30 s en segundo plano sin ventana

Motores soportados: Unity, Godot, GameMaker, Construct, Phaser, RPG Maker MV/MZ,
  PixiJS, Cocos, Defold, GBStudio, Twine, Ink/Inkle, Ren'Py web, Bitsy, p5.js,
  Kaboom.js, MelonJS, PlayCanvas, ct.js, LittleJS, PuzzleScript, Emscripten,
  Tiled (TMX), GDevelop, Love.js, Tic-80, Pico-8 web, BabylonJS, ThreeJS.
"""

from __future__ import annotations

import os
import re
import sys
import time
import json
import threading
import queue as q_module
import logging
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote
from collections import defaultdict
from typing import Optional

import concurrent.futures

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from tqdm import tqdm
from playwright.sync_api import sync_playwright, Response as PWResponse

# ─────────────────────────────────────────────
#  Logging: silencia líneas mientras la barra
#  de progreso está activa
# ─────────────────────────────────────────────

class ProgressAwareHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            msg = self.format(record)
            if _progress.bar is not None:
                tqdm.write(msg)
            else:
                print(msg)
        except Exception:
            self.handleError(record)

class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG:    "\033[90m",
        logging.INFO:     "\033[0m",
        logging.WARNING:  "\033[33m",
        logging.ERROR:    "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"
    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        return f"{color}{super().format(record)}{self.RESET}"

handler = ProgressAwareHandler()
handler.setFormatter(ColorFormatter("%(message)s"))
log = logging.getLogger("extractor")
log.addHandler(handler)
log.setLevel(logging.INFO)

# ─────────────────────────────────────────────
#  Barra de progreso global
# ─────────────────────────────────────────────

class ProgressTracker:
    def __init__(self):
        self.bar: Optional[tqdm] = None
        self.lock = threading.Lock()
        self.downloaded = 0
        self.total_bytes = 0
        self.current_file = ""
        self.queued = 0
        self._start_time = 0.0
        self._last_bytes = 0
        self._last_time  = 0.0

    def start(self, desc: str = "Descargando"):
        self._start_time = time.time()
        self._last_time  = time.time()
        self._last_bytes = 0
        # Barra sin porcentaje: el total crece dinámicamente y confunde.
        # Se muestra: archivos descargados · MB acumulados · velocidad actual.
        self.bar = tqdm(
            total=None,
            unit="arch",
            unit_scale=False,
            dynamic_ncols=True,
            bar_format="{desc}: {n_fmt} arch {postfix}",
            colour="cyan",
            desc=desc,
        )

    def update_file(self, filename: str, size_bytes: int):
        with self.lock:
            self.downloaded += 1
            self.total_bytes += size_bytes
            self.current_file = filename
            now = time.time()
            dt  = now - self._last_time
            # Velocidad en ventana de ~2 s para que sea estable
            if dt >= 2.0:
                speed_mb = (self.total_bytes - self._last_bytes) / dt / 1024 / 1024
                self._last_bytes = self.total_bytes
                self._last_time  = now
            else:
                elapsed = max(now - self._start_time, 0.001)
                speed_mb = self.total_bytes / elapsed / 1024 / 1024

        if self.bar is not None:
            queued = STATE.download_queue.unfinished_tasks
            self.bar.set_postfix_str(
                f"| {self.total_bytes/1024/1024:.1f} MB "
                f"@ {speed_mb:.2f} MB/s "
                f"| cola: {queued} "
                f"| {self._trim(filename)}",
                refresh=False,
            )
            self.bar.update(1)

    def set_phase(self, phase: str):
        if self.bar is not None:
            self.bar.set_description(phase)

    def close(self):
        if self.bar is not None:
            self.bar.close()
            self.bar = None

    @staticmethod
    def _trim(s: str, maxlen: int = 38) -> str:
        return s if len(s) <= maxlen else "…" + s[-(maxlen - 1):]


_progress = ProgressTracker()

# ─────────────────────────────────────────────
#  Constantes
# ─────────────────────────────────────────────

DOWNLOAD_WORKERS  = 12   # hilos para descargar assets
PROBE_WORKERS     = 24   # hilos para HEAD (predict phase)
SCAN_LIMIT_BYTES  = 512 * 1024   # máx. bytes a escanear por archivo con regex genérico
MAX_QUEUED_URLS   = 20_000       # techo duro: evita explosión de descubrimiento

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

ASSET_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".ico", ".bmp", ".tga", ".dds", ".ktx", ".basis", ".ktx2",
    ".ogg", ".mp3", ".wav", ".flac", ".aac", ".m4a", ".opus",
    ".mp4", ".webm", ".ogv",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".js", ".mjs", ".cjs", ".wasm", ".css", ".ts",
    ".json", ".xml", ".csv", ".ini", ".toml", ".yaml", ".yml",
    ".dat", ".bin", ".pak", ".data", ".buffer", ".bytes",
    ".html", ".htm",
    ".unityweb", ".resource", ".gz", ".br",
    ".pck",
    ".rpgmvp", ".rpgmvm", ".rpgmvo", ".rpgmvw",
    ".atlas", ".skel",
    ".zip", ".tar",
    ".tmx", ".tsx",
    ".map",
    ".bsy",
    ".ink", ".inkb",
    ".glsl", ".vert", ".frag",    # shaders
    ".gd", ".tres", ".tscn",      # Godot text resources
    ".res",                        # Godot binary resources
    ".darc",                       # Defold archive
    ".love",                       # Love.js
    ".wbp",                        # WebBundle
    ".webmanifest",
    ".avif",                       # modern image format
    ".opus",
})

# Archivos binarios que no vale la pena escanear como texto
BINARY_EXTENSIONS = frozenset({
    ".wasm", ".data", ".bin", ".pak", ".pck", ".gz", ".br", ".zip", ".tar",
    ".unityweb", ".rpgmvp", ".rpgmvm", ".rpgmvo", ".rpgmvw",
    ".mp3", ".ogg", ".wav", ".flac", ".aac", ".m4a", ".opus",
    ".mp4", ".webm", ".ogv",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tga", ".avif",
    ".dds", ".ktx", ".ktx2", ".basis",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".ico",
    ".atlas", ".skel", ".res", ".darc", ".love",
})

# Extensiones alternativas para recuperación (se prueban en orden)
AUDIO_ALTERNATIVES = [".ogg", ".mp3", ".m4a", ".wav", ".opus", ".aac"]
IMAGE_ALTERNATIVES = [".png", ".webp", ".jpg", ".jpeg", ".gif", ".avif"]

SKIP_DOMAINS = frozenset({
    "www.google-analytics.com", "analytics.google.com",
    "googletagmanager.com", "www.googletagmanager.com",
    "doubleclick.net", "adservice.google.com",
    "facebook.com", "www.facebook.com",
    "twitter.com", "www.twitter.com", "x.com",
    "sentry.io", "bugsnag.com", "mixpanel.com",
    "segment.io", "segment.com",
    "static.itch.io", "itch.io", "www.itch.io", "img.itch.zone",
    "fonts.googleapis.com",  # skip Google Fonts API (not game assets)
})

TRUSTED_CDN_SUFFIXES = (
    # CDNs de distribución clásicos
    "hwcdn.net", "amazonaws.com", "cloudfront.net", "b-cdn.net",
    "azureedge.net", "fastly.net", "akamaized.net",
    "cdnjs.cloudflare.com", "storage.googleapis.com",
    "digitaloceanspaces.com", "fly.storage.tigris.dev",
    # CDNs de paquetes JS
    "jsdelivr.net", "unpkg.com", "esm.sh", "skypack.dev",
    # Hosting de juegos / páginas estáticas
    "github.io", "raw.githubusercontent.com", "raw.github.com",
    "itch.zone",           # CDN propio de itch.io para assets de juegos
    "r2.dev",              # Cloudflare R2
    "pages.dev",           # Cloudflare Pages
    "workers.dev",         # Cloudflare Workers
    "vercel.app",          # Vercel
    "netlify.app",         # Netlify
    "onrender.com",        # Render.com
    "glitch.me",           # Glitch
    "web.app",             # Firebase Hosting
    "appspot.com",         # Google App Engine
    "surge.sh",            # Surge.sh
    "tiiny.host",          # Tiiny Host
    "itch.io",             # itch.io directo (juegos propios)
    "gstatic.com",         # Google Static (fonts, etc.)
    "ctfassets.net",       # Contentful CDN
    "backblazeb2.com",     # Backblaze B2
)

ENGINE_SIGNATURES = {
    "unity":       [r"UnityLoader", r"UnityProgress", r"buildUrl", r"\.unityweb",
                    r"\.data\.gz", r"\.wasm\.gz", r"\.framework\.js", r"createUnityInstance",
                    r"unityFramework", r"UnityWebGL"],
    "godot":       [r"GodotLoader", r"godot\.js", r"\.pck", r"engine\.startGame",
                    r"Godot\.init", r"GODOT_CONFIG", r"godot\.wasm"],
    "construct":   [r"c3runtime", r"C3\.Plugins", r"construct-core", r"C3Runtime",
                    r"C3\.New\(", r"cr\.plugins_"],
    "phaser":      [r"Phaser\.Game", r"phaser(?:\.min)?\.js", r"Phaser\.AUTO",
                    r"Phaser\.Scene", r"Phaser\.Physics", r"Phaser3"],
    "rpgmaker":    [r"rpg_core\.js", r"RPG\.?Maker", r"\.rpgmvp", r"Scene_Boot",
                    r"RMMZ", r"RMMV", r"VisuMZ", r"Yanfly"],
    "gamemaker":   [r"html5game/", r"GFMsettings", r"HitboxData", r"yyAssetPacker",
                    r"GameMaker", r"YYCreateInstance", r"gml_"],
    "pixijs":      [r"PIXI\.Application", r"pixi(?:\.min)?\.js", r"PIXI\.Renderer",
                    r"PIXI\.Sprite"],
    "cocos":       [r"cc\.game", r"cocos2d", r"CocosCreator", r"cc\.Class",
                    r"cc\.director"],
    "defold":      [r"dmloader\.js", r"defold", r"Defold", r"dmengine"],
    "gbstudio":    [r"gbstudio", r"gbsound", r"GBStudio"],
    "twine":       [r"SugarCube", r"Harlowe", r"Chapbook", r"Snowman", r"window\.story",
                    r"Story\.prototype"],
    "inkle":       [r"inkjs", r"ink\.js", r"Story\.prototype", r"inkle"],
    "renpy":       [r"renpy", r"RenPy", r"renpy-web"],
    "bitsy":       [r"bitsy", r"bitsybox", r"bitsy_engine"],
    "p5js":        [r"p5\.min\.js", r"new p5\(", r"createCanvas", r"p5\.Vector"],
    "kaboom":      [r"kaboom\(", r"kaboom\.js", r"loadSprite", r"scene\(", r"add\(\["],
    "melonjs":     [r"me\.game", r"melonJS", r"me\.loader", r"me\.state"],
    "playcanvas":  [r"pc\.Application", r"PlayCanvas", r"pc\.app", r"pc\.Asset"],
    "littlejs":    [r"LittleJS", r"engineInit\(", r"littlejs"],
    "ctjs":        [r"ct\.js", r"ct\.rooms", r"ct\.templates", r"ct\.res"],
    "puzzlescript": [r"PuzzleScript", r"title\s+\w"],
    "gdevelop":    [r"gdjs", r"gdevelop", r"GDevelop", r"GDJS\.RuntimeGame"],
    "babylonjs":   [r"BABYLON\.", r"babylonjs", r"BabylonEngine"],
    "threejs":     [r"THREE\.", r"three\.js", r"THREE\.WebGLRenderer"],
    "lovejs":      [r"love\.js", r"LOVE", r"love\.filesystem"],
    "pico8":       [r"pico8_gpio", r"pico8", r"pico-8", r"p8\.js"],
    "tic80":       [r"tic80", r"TIC-80", r"tic\.wasm"],
    "emscripten":  [r"HEAPU8", r"_emscripten_", r"WebAssembly\.instantiate",
                    r"asm2wasm", r"Module\["],
}

ENGINE_ASSET_PATTERNS: dict[str, list[str]] = {
    "unity": [
        r'(?:buildUrl|dataUrl|frameworkUrl|codeUrl|loaderUrl|workerUrl)\s*[+:]?=?\s*["\'\`]([^"\'\`\s]+)["\'\`]',
        r'["\'\`](Build/[^"\'\`\s]+)["\'\`]',
        r'["\'\`]([^"\'\`\s]+\.(?:data|framework\.js|wasm|loader\.js)(?:\.gz|\.br)?)["\'\`]',
        r'streamingAssetsUrl\s*[+:=]+\s*["\'\`]([^"\'\`\s]+)["\'\`]',
        r'["\'\`](StreamingAssets/[^"\'\`\s]+)["\'\`]',
        r'"(?:unityFramework|loaderUrl|dataUrl|frameworkUrl|codeUrl)"\s*:\s*"([^"]+)"',
    ],
    "godot": [
        r'engine\.startGame\(\s*["\'\`]([^"\'\`]+)["\'\`]',
        r'["\'\`]([^"\'\`\s]+\.pck)["\'\`]',
        r'GODOT_CONFIG\s*=\s*\{[^}]*"executable"\s*:\s*"([^"]+)"',
        r'"executable"\s*:\s*"([^"]+)"',
        r'["\'\`]([^"\'\`\s]+\.(?:wasm|audio\.worklet\.js|wasm\.js))["\'\`]',
    ],
    "gamemaker": [
        r'["\'](html5game/[^"\']+)["\']',
        r'["\'](HitboxData|GFMsettings|game_save[^"\']*)["\']',
        r'AssetPackerLoad\s*\(\s*["\']([^"\']+)["\']',
        r'["\'](datafiles/[^"\']+)["\']',
    ],
    "rpgmaker": [
        r'["\'](audio/[^"\']+\.(?:ogg|m4a|rpgmvo))["\']',
        r'["\'](img/[^"\']+\.(?:png|rpgmvp))["\']',
        r'["\'](movies/[^"\']+)["\']',
        r'["\'](data/[^"\']+\.(?:json|rpgmvw))["\']',
        r'["\'](js/[^"\']+\.js)["\']',
        r'["\'](fonts/[^"\']+)["\']',
    ],
    "construct": [
        r'["\'\`](c3runtime/[^"\'\`\s]+)["\'\`]',
        r'["\'\`](files/[^"\'\`\s]+)["\'\`]',
        r'["\'\`](images/[^"\'\`\s]+)["\'\`]',
        r'["\'\`](sounds/[^"\'\`\s]+)["\'\`]',
        r'["\'\`](fonts/[^"\'\`\s]+)["\'\`]',
        r'["\'\`](icons/[^"\'\`\s]+)["\'\`]',
    ],
    "phaser": [
        r'this\.load\.(?:image|spritesheet|atlas|json|binary|text|tilemapTiledJSON|bitmapFont|svg|glsl|shader|html)\s*\(\s*["\'][^"\']+["\']\s*,\s*["\'\`]([^"\'\`]+)["\'\`]',
        r'this\.load\.audio\s*\(\s*["\'][^"\']+["\']\s*,\s*["\'\`]([^"\'\`]+)["\'\`]',
        r'["\'\`](assets/[^"\'\`\s]+)["\'\`]',
        r'["\'\`](src/[^"\'\`\s]+\.(?:png|jpg|mp3|ogg|wav|json|atlas))["\'\`]',
        r'["\'\`](public/[^"\'\`\s]+)["\'\`]',
    ],
    "kaboom": [
        r'load(?:Sprite|Sound|Font|Aseprite|JSON|Shader)\s*\(\s*["\'][^"\']+["\']\s*,\s*["\']([^"\']+)["\']',
        r'load(?:Music|Bitmap)\s*\(\s*["\'][^"\']+["\']\s*,\s*["\']([^"\']+)["\']',
        r'["\'\`](sprites/[^"\'\`\s]+)["\'\`]',
        r'["\'\`](sounds/[^"\'\`\s]+)["\'\`]',
        r'["\'\`](fonts/[^"\'\`\s]+)["\'\`]',
    ],
    "melonjs": [
        r'me\.loader\.add(?:Image|Audio|TMXLevel|JSON|Binary|Font)\s*\(\s*["\'][^"\']+["\']\s*,\s*["\']([^"\']+)["\']',
        r'["\'\`](data/[^"\'\`\s]+)["\'\`]',
    ],
    "playcanvas": [
        r'["\'\`](__game-scripts\.js)["\'\`]',
        r'["\'\`](files/assets/[^"\'\`\s]+)["\'\`]',
        r'pc\.app\.assets\.load\s*\(\s*["\']([^"\']+)["\']',
        r'"url"\s*:\s*"([^"]+\.[a-z0-9]+)"',
    ],
    "gdevelop": [
        r'["\'\`](res/[^"\'\`\s]+)["\'\`]',
        r'["\'\`](audio/[^"\'\`\s]+)["\'\`]',
        r'"resourcesLoader"\s*:\s*\{[^}]*"url"\s*:\s*"([^"]+)"',
    ],
    "cocos": [
        r'cc\.loader\.load\s*\(\s*["\']([^"\']+)["\']',
        r'["\'\`](res/[^"\'\`\s]+)["\'\`]',
        r'["\'\`](resources/[^"\'\`\s]+)["\'\`]',
    ],
    "emscripten": [
        r'["\'\`]([^"\'\`\s]+\.wasm)["\'\`]',
        r'["\'\`]([^"\'\`\s]+\.data)["\'\`]',
        r'wasmBinaryFile\s*=\s*["\']([^"\']+)["\']',
        r'locateFile\s*\([^)]+["\']([^"\']+)["\']',
        r'["\'\`]([^"\'\`\s]+\.js\.mem)["\'\`]',
    ],
    "lovejs": [
        r'["\'\`](([^"\'\`\s]+\.love))["\'\`]',
        r'["\'\`](([^"\'\`\s]+\.wasm))["\'\`]',
    ],
}

# Manifests canónicos a probar por motor (fase de predicción)
ENGINE_MANIFESTS: dict[str, list[str]] = {
    "unity":      ["Build/", "StreamingAssets/"],
    "godot":      ["godot.js", "godot.wasm", "index.pck"],
    "construct":  ["c3runtime/manifest.json", "c3runtime/c3runtime.js", "sw.js",
                   "offline.html"],
    "rpgmaker":   ["data/System.json", "data/MapInfos.json", "data/Actors.json",
                   "js/rpg_core.js", "js/plugins.js", "js/plugins/",
                   "audio/bgm/", "audio/bgs/", "audio/se/", "audio/me/",
                   "img/system/", "img/titles1/", "img/titles2/"],
    "gamemaker":  ["html5game/", "GFMsettings", "HitboxData", "options.json"],
    "phaser":     ["assets/", "preload.json", "asset-manifest.json", "manifest.json",
                   "src/", "public/assets/"],
    "kaboom":     ["sprites/", "sounds/", "fonts/", "assets/"],
    "melonjs":    ["data/img/", "data/audio/", "data/map/", "data/"],
    "playcanvas": ["__game-scripts.js", "files/assets/", "__modules__.js"],
    "bitsy":      ["bitsy.html", "game.bsy"],
    "renpy":      ["index.html", "game/", "renpy/", "fonts/"],
    "twine":      ["story.html"],
    "defold":     ["dmloader.js", "game.darc", "archive"],
    "gbstudio":   ["rom.gb", "rom.js", "src/"],
    "gdevelop":   ["data.json", "res/", "audio/", "index.html"],
    "lovejs":     ["game.love", "love.js", "game.data"],
    "pico8":      ["cart.js", "cart.png", "index.html"],
    "tic80":      ["cart.js", "cart.wasm", "index.html"],
    "_generic":   [
        "manifest.json", "asset-manifest.json", "preload.json",
        "assets.json", "resources.json", "config.json", "game.json",
        "package.json", "index.json", "data.json", "assets/",
        "sw.js", "service-worker.js", "offline.html",
        "webmanifest.json", "site.webmanifest",
        "vite-manifest.json", ".vite/manifest.json",
    ],
}

_ext_alt = "|".join(
    re.escape(e.lstrip(".")) for e in sorted(ASSET_EXTENSIONS, key=len, reverse=True)
)
GENERIC_ASSET_RE = re.compile(
    rf'["\'\`]((?:[a-zA-Z0-9_\-./% ]+/)?[a-zA-Z0-9_\-. %]+\.(?:{_ext_alt})(?:\?[^"\'\`]*)?)["\'\`]',
    re.IGNORECASE,
)
GENERIC_FULL_URL_RE = re.compile(
    rf'["\'\`](https?://[a-zA-Z0-9_\-./% ?=&#+:@]+\.(?:{_ext_alt})(?:\?[^"\'\`]*)?)["\'\`]',
    re.IGNORECASE,
)
DYNAMIC_IMPORT_RE = re.compile(
    r'(?:import\s*\(|importScripts\s*\(|require\s*\()\s*["\'\`]([^"\'\`\?#\s]+)["\'\`]',
    re.IGNORECASE,
)
SW_CACHE_RE = re.compile(r'cache\.addAll\s*\(\s*\[([^\]]+)\]', re.IGNORECASE | re.DOTALL)
OFFLINE_CACHE_RE = re.compile(
    r'(?:urlsToCache|fileCache|CACHE_FILES|precacheUrls|staticFiles|urlsToPrefetch'
    r'|filesToCache|PRECACHE_ASSETS)\s*=\s*\[([^\]]+)\]',
    re.IGNORECASE | re.DOTALL,
)

# ─────────────────────────────────────────────
#  Estado global
# ─────────────────────────────────────────────

class ExtractorState:
    def __init__(self):
        self.lock = threading.Lock()
        self.saved_urls: set[str] = set()
        self.saved_paths: set[str] = set()
        self.queued_urls: set[str] = set()
        self.download_queue: q_module.Queue = q_module.Queue()
        self.base_folder: str = ""
        self.game_base_path: str = ""   # prefijo de ruta URL a ignorar (p.ej. "html/12345/")
        self.primary_domain: str = ""
        self.trusted_domains: set[str] = set()
        self.game_base_url: str = ""
        self.detected_engine: str = "unknown"
        self.stats: dict[str, int] = defaultdict(int)
        self.missing_assets: list[str] = []
        # Mapa path_local → URL original (para reconstruir URLs en post-procesado)
        self.path_to_url_map: dict[str, str] = {}

    def add_trusted_domain(self, domain: str):
        with self.lock:
            self.trusted_domains.add(domain)

    def is_queued(self, url: str) -> bool:
        with self.lock:
            return url in self.queued_urls

    def mark_saved_url(self, url: str) -> bool:
        with self.lock:
            if url in self.saved_urls:
                return False
            self.saved_urls.add(url)
            return True

    def mark_saved_path(self, path: str) -> bool:
        with self.lock:
            if path in self.saved_paths:
                return False
            self.saved_paths.add(path)
            return True

    def enqueue(self, url: str):
        url = url.split("#")[0].strip()
        if not url:
            return
        with self.lock:
            if url in self.queued_urls:
                return
            if len(self.queued_urls) >= MAX_QUEUED_URLS:
                self.stats["capped"] += 1
                return
            self.queued_urls.add(url)
            self.download_queue.put(url)


STATE = ExtractorState()

# ─────────────────────────────────────────────
#  Utilidades de URL / rutas
# ─────────────────────────────────────────────

def sanitize_path(raw_path: str) -> str:
    path = raw_path.split("?")[0].split("#")[0]
    try:
        path = unquote(path)
    except Exception:
        pass
    path = path.lstrip("/")
    path = re.sub(r'[<>:"|?*\x00-\x1f\\]', "_", path)
    if not path or path.endswith("/"):
        path += "index.html"
    return path


def url_to_local(url: str) -> str:
    parsed = urlparse(url)
    path = sanitize_path(parsed.path)   # sin slash inicial, p.ej. "html/12345/js/game.js"
    base = STATE.base_folder
    if parsed.netloc in STATE.trusted_domains:
        # Eliminar el prefijo de ruta del juego (p.ej. "html/12345/") para que
        # los archivos queden directamente en base_folder sin subcarpetas ajenas.
        bp = STATE.game_base_path
        if bp and path.startswith(bp):
            path = path[len(bp):]               # "js/game.js"
        elif bp and path == bp.rstrip("/"):
            path = "index.html"                 # URL exacta del directorio sin "/"
        return os.path.join(base, path) if path else os.path.join(base, "index.html")
    safe_domain = re.sub(r'[<>:"|?*\x00-\x1f\\/\s]', "_", parsed.netloc)
    return os.path.join(base, "_ext", safe_domain, path)


def _is_cdn_domain(domain: str) -> bool:
    return any(domain.endswith(s) for s in TRUSTED_CDN_SUFFIXES)


def should_download(url: str) -> bool:
    if not url:
        return False
    if url.startswith(("data:", "javascript:", "blob:", "mailto:", "tel:")):
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    domain = parsed.netloc
    if domain in SKIP_DOMAINS:
        return False
    if domain in STATE.trusted_domains:
        return True
    if _is_cdn_domain(domain):
        STATE.add_trusted_domain(domain)
        log.debug(f"  [CDN] {domain}")
        return True
    # Auto-confiar subdominios del dominio primario
    primary = STATE.primary_domain
    if primary:
        base = primary.split(".", 1)[-1] if "." in primary else primary
        if domain == primary or domain.endswith("." + base):
            STATE.add_trusted_domain(domain)
            log.debug(f"  [Subdominio] {domain}")
            return True
    ext = Path(sanitize_path(parsed.path)).suffix.lower()
    return ext in ASSET_EXTENSIONS

# ─────────────────────────────────────────────
#  Detección de URL real (itch.io)
# ─────────────────────────────────────────────

def _download_game_cover(itch_page_url: str, folder: str, session: requests.Session):
    """
    Descarga la imagen de portada del juego desde la página de itch.io y la
    guarda como 'portada.jpg/png/webp' en la raíz de la carpeta del juego.
    Solo actúa cuando la URL original era una página de itch.io (*.itch.io).
    """
    parsed = urlparse(itch_page_url)
    domain = parsed.netloc.lower()
    is_itch_page = domain.endswith(".itch.io") or domain in ("itch.io", "www.itch.io")
    if not is_itch_page:
        return  # URL directa — no hay página de itch.io de dónde sacar portada

    try:
        res = session.get(itch_page_url, timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")

        cover_url: Optional[str] = None

        # 1. og:image (el más fiable — es la imagen principal del juego en itch.io)
        for meta_name, meta_attr in [
            ("property", "og:image"),
            ("name",     "twitter:image"),
        ]:
            tag = soup.find("meta", attrs={meta_name: meta_attr})
            if tag and tag.get("content"):
                cover_url = tag["content"]
                break

        # 2. Imagen de portada en el cuerpo de la página
        if not cover_url:
            for sel in [
                ".game_thumb img",
                ".header_image img",
                ".screenshot_list img",
                "img.game_thumb",
            ]:
                img = soup.select_one(sel)
                if img:
                    cover_url = img.get("src") or img.get("data-src") or ""
                    if cover_url:
                        break

        if not cover_url:
            log.debug("  [portada] No se encontró imagen de portada en la página de itch.io")
            return

        # Convertir a URL absoluta si es relativa
        cover_url = urljoin(itch_page_url, cover_url)

        r = session.get(cover_url, timeout=30)
        if r.status_code != 200:
            log.debug(f"  [portada] HTTP {r.status_code} al descargar portada")
            return

        # Detectar formato real por Content-Type
        ct = r.headers.get("content-type", "").lower()
        if "png" in ct:
            ext = ".png"
        elif "webp" in ct:
            ext = ".webp"
        elif "gif" in ct:
            ext = ".gif"
        else:
            ext = ".jpg"

        dest = os.path.join(os.path.abspath(folder), f"portada{ext}")
        with open(dest, "wb") as f:
            f.write(r.content)
        log.info(f"  [✓] Portada guardada: portada{ext} ({len(r.content) // 1024} KB)")

    except Exception as e:
        log.warning(f"  [!] No se pudo descargar portada: {e}")


def get_real_game_url(itch_url: str) -> str:
    parsed = urlparse(itch_url)
    domain = parsed.netloc.lower()

    # Si la URL ya es directa al juego (no es una página wrapper *.itch.io),
    # devolverla inmediatamente sin gastar tiempo buscando iframes.
    is_itch_page = domain.endswith(".itch.io") or domain in ("itch.io", "www.itch.io")
    if not is_itch_page:
        log.info(f"  [✓ URL directa del juego] {itch_url}")
        return itch_url

    log.info(f"[*] Buscando URL del juego en: {itch_url}")
    try:
        res = requests.get(itch_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")

        for iframe in soup.find_all("iframe", src=True):
            src = iframe["src"]
            if src and any(x in src for x in ("html", "hwcdn", ".io", "uploads", "games", "storage")):
                log.info(f"  [✓ iframe] → {src}")
                return src if src.startswith("http") else urljoin(itch_url, src)

        for attr in ("data-iframe", "data-src", "data-url", "data-game-url"):
            tag = soup.find(attrs={attr: True})
            if tag:
                val = tag[attr]
                log.info(f"  [✓ {attr}] → {val}")
                return val if val.startswith("http") else urljoin(itch_url, val)

        for script in soup.find_all("script"):
            text = script.string or ""
            for pattern in [
                r'"url"\s*:\s*"(https?://[^"]+\.html[^"]*)"',
                r'"embedUrl"\s*:\s*"(https?://[^"]+)"',
                r'I\.ViewGame\(\{.*?"url"\s*:\s*"(https?://[^"]+)"',
                r'window\.current_game\s*=\s*\{.*?"url"\s*:\s*"(https?://[^"]+)"',
                r'"url"\s*:\s*"(https?://[^"]+\.itch\.zone[^"]*)"',
                r'"url"\s*:\s*"(https?://[^"]+hwcdn[^"]*)"',
            ]:
                m = re.search(pattern, text, re.DOTALL)
                if m:
                    url = m.group(1)
                    log.info(f"  [✓ script] → {url}")
                    return url if url.startswith("http") else urljoin(itch_url, url)

    except Exception as e:
        log.warning(f"  [!] Error estático: {e}")

    log.info("  [*] Usando Playwright para encontrar iframe…")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(itch_url, timeout=30000, wait_until="domcontentloaded")

            # itch.io muestra un botón "Run game" que hay que pulsar para cargar el iframe
            try:
                run_btn = page.query_selector(".load_iframe_btn, [class*='load_iframe']")
                if run_btn:
                    log.info("  [*] Haciendo clic en 'Run game'…")
                    run_btn.click()
                    page.wait_for_selector("iframe[src]", timeout=15000)
            except Exception:
                pass

            frame_url = page.evaluate("""() => {
                for (const sel of ['iframe[src*="itch.zone"]','iframe[src*="hwcdn"]',
                                   'iframe[src*="html"]','iframe[src*="uploads"]',
                                   'iframe[src*=".io"]','iframe']) {
                    const el = document.querySelector(sel);
                    if (el && el.src) return el.src;
                }
                return null;
            }""")
            browser.close()
            if frame_url:
                log.info(f"  [✓ JS iframe] → {frame_url}")
                return frame_url
    except Exception as e:
        log.warning(f"  [!] Error Playwright: {e}")

    log.info(f"  [=] URL original: {itch_url}")
    return itch_url

# ─────────────────────────────────────────────
#  Detección de motor
# ─────────────────────────────────────────────

def detect_engine(content: str) -> str:
    scores: dict[str, int] = defaultdict(int)
    for engine, patterns in ENGINE_SIGNATURES.items():
        for p in patterns:
            if re.search(p, content, re.IGNORECASE):
                scores[engine] += 1
    return max(scores, key=lambda e: scores[e]) if scores else "unknown"

# ─────────────────────────────────────────────
#  Auto-confianza de dominios vinculados en HTML
# ─────────────────────────────────────────────

def _sniff_referenced_domains(html_content: str, base_url: str):
    """
    Si el HTML del juego carga scripts/hojas de estilo desde un dominio externo,
    ese dominio probablemente sirve assets del juego: auto-confiar.
    """
    try:
        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup.find_all(["script", "link"]):
            src = tag.get("src") or tag.get("href") or ""
            if not src:
                continue
            full_url = urljoin(base_url, src)
            if not full_url.startswith("http"):
                continue
            domain = urlparse(full_url).netloc
            if not domain or domain in SKIP_DOMAINS or domain in STATE.trusted_domains:
                continue
            ext = Path(sanitize_path(urlparse(full_url).path)).suffix.lower()
            if ext in (".js", ".mjs", ".css", ".wasm", ".ts"):
                STATE.add_trusted_domain(domain)
                log.debug(f"  [Auto-trust] {domain} (vinculado desde HTML del juego)")
    except Exception:
        pass

# ─────────────────────────────────────────────
#  Escáneres de contenido
# ─────────────────────────────────────────────

def scan_html(content: str, base_url: str) -> list[str]:
    urls: list[str] = []
    try:
        soup = BeautifulSoup(content, "html.parser")

        # Respetar <base href> si está presente
        base_tag = soup.find("base", href=True)
        effective_base = urljoin(base_url, base_tag["href"]) if base_tag else base_url

        for tag_name, attr in [
            ("script","src"),("link","href"),("img","src"),("img","data-src"),
            ("img","srcset"),("source","src"),("source","srcset"),("video","src"),
            ("video","poster"),("audio","src"),("a","href"),("embed","src"),
            ("object","data"),("track","src"),("iframe","src"),
        ]:
            for tag in soup.find_all(tag_name, **{attr: True}):
                val = tag.get(attr, "") or ""
                for part in val.split(","):
                    raw = part.strip().split()[0]
                    if raw and not raw.startswith(("data:","javascript:","#","mailto:")):
                        full = urljoin(effective_base, raw)
                        if full.startswith("http"):
                            urls.append(full)

        # Atributos data-* que contienen rutas de recursos
        for tag in soup.find_all(True):
            for attr, val in tag.attrs.items():
                if not isinstance(val, str):
                    continue
                if attr.startswith("data-") and any(
                    kw in attr for kw in ("url","src","path","href","file","asset","resource")
                ):
                    if val and not val.startswith(("data:", "javascript:", "#")):
                        full = urljoin(effective_base, val)
                        if full.startswith("http"):
                            urls.append(full)

        for style in soup.find_all("style"):
            if style.string:
                urls.extend(scan_css(style.string, effective_base))
        for tag in soup.find_all(style=True):
            urls.extend(scan_css(tag["style"], effective_base))
        for meta in soup.find_all("meta", attrs={"http-equiv": re.compile("refresh", re.I)}):
            m = re.search(r"url\s*=\s*(.+)", meta.get("content",""), re.I)
            if m:
                urls.append(urljoin(effective_base, m.group(1).strip().strip("'\"")))
        for link in soup.find_all("link", href=True):
            rels = link.get("rel", [])
            if isinstance(rels, str):
                rels = [rels]
            if any(r in rels for r in ("manifest","preload","prefetch","modulepreload",
                                        "icon","apple-touch-icon")):
                urls.append(urljoin(effective_base, link["href"]))

        # TODOS los scripts inline (no solo type="module")
        for script in soup.find_all("script"):
            text = script.string or ""
            if text.strip():
                urls.extend(scan_js(text, effective_base, STATE.detected_engine))

    except Exception as e:
        log.debug(f"  [!] scan_html: {e}")
    return urls


def scan_css(content: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for m in re.finditer(r'url\(\s*["\']?([^"\')\s]+)["\']?\s*\)', content):
        raw = m.group(1).strip()
        if not raw.startswith("data:"):
            urls.append(urljoin(base_url, raw))
    for m in re.finditer(r'@import\s+["\']([^"\']+)["\']', content):
        urls.append(urljoin(base_url, m.group(1)))
    return urls


def scan_xml(content: str, base_url: str) -> list[str]:
    """Escanea archivos XML/TMX/TSX (Tiled, Defold, etc.) buscando referencias a assets."""
    urls: list[str] = []
    base_dir = base_url.rsplit("/", 1)[0] + "/"
    for m in re.finditer(
        r'\b(?:source|src|href|file|image)\s*=\s*["\']([^"\']+)["\']',
        content, re.IGNORECASE,
    ):
        raw = m.group(1)
        if raw.startswith("data:") or raw.startswith("#"):
            continue
        candidate = raw if raw.startswith("http") else urljoin(base_dir, raw)
        ext = Path(candidate.split("?")[0]).suffix.lower()
        if ext in ASSET_EXTENSIONS:
            urls.append(candidate)
    return urls


def scan_js(content: str, base_url: str, engine: str,
            generic_text: Optional[str] = None) -> list[str]:
    """
    generic_text: texto a usar para GENERIC_ASSET_RE y GENERIC_FULL_URL_RE.
    Si se omite, se usa el mismo `content`.  Permite pasar el chunk recortado
    mientras los patrones de motor usan el archivo completo.
    """
    urls: list[str] = []
    base_dir = base_url.rsplit("/", 1)[0] + "/"
    seen: set[str] = set()
    gen = generic_text if generic_text is not None else content

    def add(raw: str):
        raw = raw.strip().split("?")[0]
        if not raw or len(raw) > 512 or raw.startswith(("$","{","[","//","/*")):
            return
        if re.match(r'^[0-9a-f]{20,}$', raw, re.IGNORECASE):
            return
        if raw.startswith("http://") or raw.startswith("https://"):
            candidate = raw
        elif raw.startswith("//"):
            candidate = "https:" + raw
        elif raw.startswith("/"):
            parsed = urlparse(base_url)
            candidate = f"{parsed.scheme}://{parsed.netloc}{raw}"
        else:
            candidate = urljoin(base_dir, raw)
        if candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)

    # Patrones específicos del motor (texto completo — son precisos y rápidos)
    for pattern in ENGINE_ASSET_PATTERNS.get(engine, []):
        for m in re.finditer(pattern, content, re.IGNORECASE):
            add(m.group(1))

    # URLs absolutas con extensión conocida (texto limitado)
    for m in GENERIC_FULL_URL_RE.finditer(gen):
        add(m.group(1))

    # Rutas relativas con extensión conocida (texto limitado)
    for m in GENERIC_ASSET_RE.finditer(gen):
        raw = m.group(1)
        if not re.match(r'^[0-9a-f]{20,}$', raw, re.IGNORECASE):
            add(raw)

    # import() dinámico, importScripts(), require()
    for m in DYNAMIC_IMPORT_RE.finditer(content):
        add(m.group(1))

    # ES module imports estáticos: import ... from "..."
    for m in re.finditer(
        r'^(?:import|export)\b.*?\bfrom\s+["\'\`]([^"\'\`\?#\s]+)["\'\`]',
        content, re.IGNORECASE | re.MULTILINE,
    ):
        add(m.group(1))

    # new URL("./asset.png", import.meta.url)  — Vite/ESBuild
    for m in re.finditer(
        r'new\s+URL\s*\(\s*["\'\`]([^"\'\`\?#\s]+)["\'\`]\s*,\s*import\.meta\.url',
        content, re.IGNORECASE,
    ):
        add(m.group(1))

    # Web Workers: new Worker("...") / new SharedWorker("...")
    for m in re.finditer(
        r'new\s+(?:Shared)?Worker\s*\(\s*["\'\`]([^"\'\`\?#\s]+)["\'\`]',
        content, re.IGNORECASE,
    ):
        add(m.group(1))

    # importScripts("...") dentro de workers
    for m in re.finditer(
        r'importScripts\s*\(\s*([^)]+)\)',
        content, re.IGNORECASE,
    ):
        for fm in re.finditer(r'["\'\`]([^"\'\`\?#\s]+)["\'\`]', m.group(1)):
            add(fm.group(1))

    # Registro de Service Worker
    for m in re.finditer(
        r'serviceWorker\.register\s*\(\s*["\'\`]([^"\'\`\?#\s]+)["\'\`]',
        content, re.IGNORECASE,
    ):
        add(m.group(1))

    # fetch() y XMLHttpRequest
    for m in re.finditer(
        r'(?:fetch|XMLHttpRequest\.open)\s*\(\s*["\']([^"\'?#\s]+)["\']', content, re.I
    ):
        add(m.group(1))

    # Phaser 3 audio arrays: this.load.audio("key", ["a.ogg","a.mp3"])
    for m in re.finditer(
        r'this\.load\.audio\s*\(\s*["\'][^"\']+["\']\s*,\s*\[([^\]]+)\]',
        content, re.IGNORECASE,
    ):
        for fm in re.finditer(r'["\'\`]([^"\'\`]+)["\'\`]', m.group(1)):
            add(fm.group(1))

    # Lista de archivos en Service Worker (cache.addAll)
    for m in SW_CACHE_RE.finditer(content):
        for fm in re.finditer(r'["\']([^"\']+)["\']', m.group(1)):
            add(fm.group(1))

    # Lista de archivos offline (Construct 3 y otros PWAs)
    for m in OFFLINE_CACHE_RE.finditer(content):
        for fm in re.finditer(r'["\']([^"\']+)["\']', m.group(1)):
            add(fm.group(1))

    # WebAssembly.instantiate(fetch("..."))
    for m in re.finditer(
        r'WebAssembly\.(?:instantiate|instantiateStreaming)\s*\(\s*fetch\s*\(\s*["\']([^"\']+)["\']',
        content, re.I,
    ):
        add(m.group(1))

    # Webpack/Vite: chunks con hash
    for m in re.finditer(
        r'["\'\`]([^"\'\`\s]*(?:chunk|bundle|vendor|runtime|app)\.[a-f0-9]{6,}\.[a-z0-9]+)["\'\`]',
        content, re.IGNORECASE,
    ):
        add(m.group(1))

    return urls


def scan_json(content: str, base_url: str) -> list[str]:
    urls: list[str] = []
    base_dir = base_url.rsplit("/", 1)[0] + "/"
    MANIFEST_KEYS = {
        "src","url","path","file","audio","image","atlas","spritesheetUrl",
        "dataUrl","frameworkUrl","codeUrl","loaderUrl","workerUrl","wasmUrl",
        "tilemapUrl","fontUrl","files","assets","pack",
        # Claves adicionales para motores modernos
        "filename","source","href","texture","texturePath","sprite","sound",
        "music","background","tileset","sheet","script","stylesheet",
        "resource","uri","asset","entry","chunk","bundle","bitmapFont",
        "spineData","spineAtlas","spineImage","bitmap","glsl","vert","frag",
    }
    for m in GENERIC_ASSET_RE.finditer(content):
        raw = m.group(1)
        urls.append(raw if raw.startswith("http") else urljoin(base_dir, raw))
    try:
        data = json.loads(content)
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str) and k.lower() in MANIFEST_KEYS:
                        if "." in v and not v.startswith("data:"):
                            urls.append(urljoin(base_dir, v))
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, str) and "." in item:
                        if Path(item.split("?")[0]).suffix.lower() in ASSET_EXTENSIONS:
                            urls.append(urljoin(base_dir, item))
                    elif isinstance(item, (dict, list)):
                        stack.append(item)
    except Exception:
        pass
    return urls

# ─────────────────────────────────────────────
#  Descarga
# ─────────────────────────────────────────────

def _make_session() -> requests.Session:
    """Sesión requests con pool de conexiones grande."""
    session = requests.Session()
    session.headers.update(HEADERS)
    # Solo pool — los reintentos los maneja download_bytes manualmente para
    # evitar que el adaptador duerma en 429 mientras 16 workers atacan el mismo host.
    adapter = HTTPAdapter(
        pool_connections=DOWNLOAD_WORKERS,
        pool_maxsize=DOWNLOAD_WORKERS * 2,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def download_bytes(url: str, session: requests.Session, max_retries: int = 3) -> Optional[bytes]:
    for attempt in range(max_retries):
        try:
            r = session.get(url, stream=True, timeout=(10, 60), headers=HEADERS)
            if r.status_code == 200:
                chunks = []
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        chunks.append(chunk)
                return b"".join(chunks)
            elif r.status_code == 404:
                STATE.missing_assets.append(url)
                return None
            elif r.status_code in (401, 403):
                return None
            elif r.status_code in (429, 503):
                time.sleep(2 ** attempt)
        except requests.exceptions.Timeout:
            time.sleep(1)
        except Exception as e:
            if attempt == max_retries - 1:
                log.debug(f"  [!] {url}: {e}")
    return None


# Elimina el script de protección anti-hotlink que itch.io inyecta en los HTML.
# Sin esto, el juego redirige a https://itch.io/embed-hotlink al cargarse fuera
# del dominio de itch.io.
_ITCHIO_HOTLINK_RE = re.compile(
    rb'<script[^>]*src=["\']https://static\.itch\.io/htmlgame\.js["\'][^>]*>\s*</script>',
    re.IGNORECASE,
)

def _strip_itchio_hotlink(content: bytes) -> bytes:
    """Elimina el script anti-hotlink de itch.io de un HTML."""
    return _ITCHIO_HOTLINK_RE.sub(b"", content)


def save_content(local_path: str, content: bytes) -> bool:
    # Eliminar protección anti-hotlink antes de guardar cualquier HTML
    if local_path.lower().endswith(".html"):
        content = _strip_itchio_hotlink(content)
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(content)
        return True
    except Exception as e:
        log.debug(f"  [!] Guardar {local_path}: {e}")
        return False


def _sniff_cdns(text: str):
    for m in re.finditer(r'https?://([a-zA-Z0-9._-]+)/', text):
        domain = m.group(1)
        if _is_cdn_domain(domain) and domain not in STATE.trusted_domains:
            STATE.add_trusted_domain(domain)
            log.debug(f"  [CDN] {domain}")


def process_content(url: str, content: bytes, session: requests.Session):
    parsed = urlparse(url)
    path = sanitize_path(parsed.path)
    ext = Path(path).suffix.lower()

    # No escanear archivos binarios como texto — ahorrar tiempo y evitar falsos positivos
    if ext in BINARY_EXTENSIONS:
        return

    is_sw = "sw" in Path(path).stem.lower() or "serviceworker" in Path(path).stem.lower()

    try:
        text = content.decode("utf-8", errors="ignore")
    except Exception:
        return

    # Texto completo para patrones específicos del motor (precisos y rápidos).
    # Texto limitado para regex genérico — evita que bundles de 5 MB+ generen
    # miles de falsos positivos y expandan la cola indefinidamente.
    text_full    = text
    text_limited = text[:SCAN_LIMIT_BYTES] if len(text) > SCAN_LIMIT_BYTES else text
    is_large     = len(text) > SCAN_LIMIT_BYTES

    new_urls: list[str] = []

    if ext in (".html", ".htm"):
        engine = detect_engine(text_full)
        if engine != "unknown" and STATE.detected_engine == "unknown":
            STATE.detected_engine = engine
            log.info(f"  \033[32m[Motor detectado] → {engine.upper()}\033[0m")
        _sniff_referenced_domains(text_full, url)
        new_urls = scan_html(text_full, url)   # HTML siempre completo (pequeños)
        _sniff_cdns(text_limited)

    elif ext == ".css":
        new_urls = scan_css(text_full, url)

    elif ext in (".js", ".mjs", ".cjs") or is_sw:
        engine = detect_engine(text_limited)
        if engine != "unknown" and STATE.detected_engine == "unknown":
            STATE.detected_engine = engine
            log.info(f"  \033[32m[Motor detectado] → {engine.upper()}\033[0m")
        # Patrones del motor: texto completo (precisos, sin backtracking)
        # GENERIC_ASSET_RE y GENERIC_FULL_URL_RE: solo primeros SCAN_LIMIT_BYTES
        new_urls = scan_js(text_full if not is_large else text_limited,
                           url, STATE.detected_engine,
                           generic_text=text_limited)
        _sniff_cdns(text_limited)

    elif ext in (".json", ".map", ".webmanifest"):
        new_urls = scan_json(text_full, url)

    elif ext in (".xml", ".tmx", ".tsx"):
        new_urls = scan_xml(text_full, url)

    elif ext in (".ini", ".toml", ".yaml", ".yml", ".csv"):
        for m in GENERIC_ASSET_RE.finditer(text_limited):
            raw = m.group(1)
            base_dir = url.rsplit("/", 1)[0] + "/"
            candidate = raw if raw.startswith("http") else urljoin(base_dir, raw)
            new_urls.append(candidate)

    for u in new_urls:
        if should_download(u) and not STATE.is_queued(u):
            STATE.enqueue(u)


def download_and_save(url: str, session: requests.Session) -> bool:
    if not STATE.mark_saved_url(url):
        return False
    local_path = url_to_local(url)
    if not STATE.mark_saved_path(local_path):
        return False

    # Saltar archivos ya descargados (re-runs o interrupciones previas)
    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        fname = os.path.basename(local_path)
        _progress.update_file(fname, os.path.getsize(local_path))
        STATE.stats["skipped"] += 1
        return True

    content = download_bytes(url, session)
    if content is None:
        return False

    if save_content(local_path, content):
        abs_path = os.path.abspath(local_path)
        with STATE.lock:
            STATE.path_to_url_map[abs_path] = url
        fname = os.path.basename(local_path)
        _progress.update_file(fname, len(content))
        STATE.stats["downloaded"] += 1
        process_content(url, content, session)
        return True
    return False

# ─────────────────────────────────────────────
#  Cookies
# ─────────────────────────────────────────────

def sync_cookies(playwright_cookies: list[dict], session: requests.Session):
    for cookie in playwright_cookies:
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain",""))


# ─────────────────────────────────────────────
#  Interceptor Playwright
# ─────────────────────────────────────────────

def on_response(response: PWResponse, session: requests.Session):
    url = response.url
    if url.startswith(("data:","blob:","javascript:")):
        return
    if response.request.resource_type in ("websocket","eventsource"):
        return
    if response.request.method in ("OPTIONS","HEAD"):
        return
    parsed = urlparse(url)
    domain = parsed.netloc
    if domain in SKIP_DOMAINS:
        return
    if _is_cdn_domain(domain):
        STATE.add_trusted_domain(domain)
    if not should_download(url):
        return
    if STATE.is_queued(url) or not STATE.mark_saved_url(url):
        return
    local_path = url_to_local(url)
    if not STATE.mark_saved_path(local_path):
        return
    try:
        body = response.body()
        if not body:
            return
        if save_content(local_path, body):
            abs_path = os.path.abspath(local_path)
            with STATE.lock:
                STATE.path_to_url_map[abs_path] = url
            fname = os.path.basename(local_path)
            _progress.update_file(fname, len(body))
            STATE.stats["intercepted"] += 1
            process_content(url, body, session)
    except Exception:
        STATE.stats["queued_fallback"] += 1
        STATE.enqueue(url)


# ─────────────────────────────────────────────
#  Worker de cola
# ─────────────────────────────────────────────

def queue_worker(session: requests.Session, stop_event: threading.Event):
    while not stop_event.is_set() or not STATE.download_queue.empty():
        try:
            url = STATE.download_queue.get(timeout=1)
        except q_module.Empty:
            continue
        try:
            if should_download(url):
                download_and_save(url, session)
        except Exception as e:
            log.debug(f"  [!] worker: {e}")
        finally:
            STATE.download_queue.task_done()


def _wait_queue(stop: threading.Event, workers: list, phase: str, timeout: int = 180):
    """
    Espera a que la cola quede vacía con feedback periódico y timeout de seguridad.
    Reemplaza el patrón queue.join() que bloquea indefinidamente.
    """
    deadline = time.time() + timeout
    last_log  = time.time()
    last_count = -1

    while True:
        pending = STATE.download_queue.unfinished_tasks
        if pending == 0:
            break
        now = time.time()
        if now > deadline:
            log.warning(f"  [!] {phase}: timeout ({timeout}s) con {pending} items aún en vuelo")
            break
        # Log de estado cada 15 s si el número cambió o pasó tiempo
        if now - last_log >= 15 or pending != last_count:
            if pending != last_count:
                log.info(f"  [→] {phase}: {pending} descarga(s) en progreso…")
                last_count = pending
                last_log   = now
        time.sleep(0.5)

    stop.set()
    for w in workers:
        w.join(timeout=10)


# ─────────────────────────────────────────────
#  Fases de extracción automática
# ─────────────────────────────────────────────

def probe_url(url: str, session: requests.Session) -> bool:
    """HEAD rápido para comprobar si una URL existe (200)."""
    try:
        r = session.head(url, timeout=8, headers=HEADERS, allow_redirects=True)
        return r.status_code == 200
    except Exception:
        return False


def static_crawl_phase(base_url: str, session: requests.Session):
    """
    Fase 1: Crawl estático profundo.
    Descarga el HTML raíz y sigue recursivamente todos los JS/CSS/JSON/XML.
    Sale cuando la cola lleva 8 s sin actividad nueva, o tras 5 minutos.
    Los assets binarios grandes (PNG, OGG, MP4…) se descargan en las fases
    siguientes mientras aquí solo se prioriza el descubrimiento de rutas.
    """
    _progress.set_phase("Fase 1: Crawl estático")
    log.info("\n[Fase 1] Crawl estático profundo…")
    STATE.enqueue(base_url)

    stop = threading.Event()
    workers = [threading.Thread(target=queue_worker, args=(session, stop), daemon=True)
               for _ in range(DOWNLOAD_WORKERS)]
    for w in workers:
        w.start()

    # Salir cuando la cola lleva IDLE_LIMIT s sin tareas pendientes,
    # o cuando se supera el tiempo máximo de fase.
    IDLE_LIMIT = 8
    MAX_PHASE  = 300   # 5 min máximo en fase 1
    idle_s     = 0.0
    t0         = time.time()
    while idle_s < IDLE_LIMIT and (time.time() - t0) < MAX_PHASE:
        pending = STATE.download_queue.unfinished_tasks
        if pending == 0:
            time.sleep(0.5)
            idle_s += 0.5
        else:
            idle_s = 0.0
            time.sleep(0.3)

    log.info(f"  Fase 1 descubrimiento finalizado "
             f"({'tiempo límite' if (time.time()-t0) >= MAX_PHASE else f'idle {IDLE_LIMIT}s'}) "
             f"— {len(STATE.saved_paths)} archivos, cola: "
             f"{STATE.download_queue.unfinished_tasks} en vuelo")

    # Esperar que terminen los items ya en vuelo (máx. 90 s)
    _wait_queue(stop, workers, "Crawl estático", timeout=90)


def predict_assets_phase(base_url: str, session: requests.Session):
    """
    Fase 2: Predicción por convención de carpetas / rutas canónicas.
    Prueba las rutas típicas de cada motor con HEAD antes de descargar.
    """
    engine = STATE.detected_engine
    _progress.set_phase(f"Fase 2: Predicción ({engine})")
    log.info(f"\n[Fase 2] Predicción de assets para motor: {engine.upper()}")

    base_dir = base_url.rsplit("/", 1)[0] + "/"
    candidates: list[str] = []

    # Manifests del motor detectado
    for path in ENGINE_MANIFESTS.get(engine, []):
        candidates.append(urljoin(base_dir, path))

    # Siempre probar manifests genéricos
    for path in ENGINE_MANIFESTS["_generic"]:
        candidates.append(urljoin(base_dir, path))

    # Unity: inferir nombre del build desde archivos ya descargados
    if engine == "unity":
        for saved in list(STATE.saved_paths):
            m = re.search(r'(Build/[^/]+)\.loader\.js', saved)
            if m:
                stem = m.group(1)
                for ext in [".data.gz", ".framework.js.gz", ".wasm.gz",
                             ".data", ".framework.js", ".wasm"]:
                    candidates.append(urljoin(base_dir, stem + ext))

    # RPGMaker: enumerar todos los mapas y plugins
    if engine == "rpgmaker":
        # Intentar obtener la lista exacta de mapas desde MapInfos.json
        map_infos_url = urljoin(base_dir, "data/MapInfos.json")
        map_ids: list[int] = []
        if map_infos_url not in STATE.saved_urls:
            data = download_bytes(map_infos_url, session)
            if data:
                try:
                    infos = json.loads(data.decode("utf-8", errors="ignore"))
                    map_ids = [
                        entry["id"] for entry in infos
                        if isinstance(entry, dict) and entry and "id" in entry
                    ]
                    log.info(f"  MapInfos.json → {len(map_ids)} mapas encontrados")
                except Exception:
                    pass

        if map_ids:
            for mid in map_ids:
                candidates.append(urljoin(base_dir, f"data/Map{mid:03d}.json"))
        else:
            # Sin MapInfos, probar hasta el mapa 150
            for i in range(1, 151):
                candidates.append(urljoin(base_dir, f"data/Map{i:03d}.json"))

        # JSON de datos estándar de RPGMaker MV/MZ
        rpg_jsons = [
            "System","Actors","Armors","Classes","CommonEvents","Enemies",
            "Items","MapInfos","Skills","States","Tilesets","Troops",
            "Weapons","Animations",
        ]
        for name in rpg_jsons:
            candidates.append(urljoin(base_dir, f"data/{name}.json"))

    # Godot: probar variantes de nombre ejecutable
    if engine == "godot":
        for saved in list(STATE.saved_paths):
            m = re.search(r'([^/\\]+)\.html$', saved, re.IGNORECASE)
            if m:
                stem = m.group(1)
                for ext2 in [".pck", ".js", ".wasm", ".audio.worklet.js",
                              ".wasm.js", ".side.wasm"]:
                    candidates.append(urljoin(base_dir, stem + ext2))

    # Construct 3: sw.js contiene la lista completa de assets
    if engine == "construct":
        candidates.append(urljoin(base_dir, "sw.js"))
        candidates.append(urljoin(base_dir, "c3runtime/manifest.json"))

    to_probe = [u for u in candidates if not STATE.is_queued(u) and u not in STATE.saved_urls]

    found = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        futures = {pool.submit(probe_url, u, session): u for u in to_probe}
        for fut in concurrent.futures.as_completed(futures):
            if fut.result():
                STATE.enqueue(futures[fut])
                found += 1

    log.info(f"  Probadas: {len(to_probe)} rutas → {found} encontradas")

    if found > 0:
        stop = threading.Event()
        workers = [threading.Thread(target=queue_worker, args=(session, stop), daemon=True)
                   for _ in range(DOWNLOAD_WORKERS // 2)]
        for w in workers:
            w.start()
        _wait_queue(stop, workers, "Predicción", timeout=240)


def manifest_deep_follow(session: requests.Session):
    """
    Fase 3: Re-procesar manifests descargados para descubrir referencias tardías.
    """
    _progress.set_phase("Fase 3: Manifests profundos")
    log.info("\n[Fase 3] Siguiendo manifests descargados…")

    manifest_exts = {".json", ".xml", ".tmx", ".tsx", ".ini", ".toml", ".yaml", ".yml",
                     ".webmanifest"}
    to_reprocess: list[str] = []

    for saved_path in list(STATE.saved_paths):
        if Path(saved_path).suffix.lower() in manifest_exts:
            to_reprocess.append(saved_path)

    new_queued = 0
    for path in to_reprocess:
        url = _local_path_to_url(path)
        if not url:
            continue
        try:
            with open(path, "rb") as f:
                content = f.read()
            before = len(STATE.queued_urls)
            process_content(url, content, session)
            new_queued += len(STATE.queued_urls) - before
        except Exception:
            pass

    if new_queued > 0:
        log.info(f"  {new_queued} nuevas URLs encoladas desde manifests")
        stop = threading.Event()
        workers = [threading.Thread(target=queue_worker, args=(session, stop), daemon=True)
                   for _ in range(DOWNLOAD_WORKERS // 2)]
        for w in workers:
            w.start()
        _wait_queue(stop, workers, "Manifests", timeout=180)
    else:
        log.info("  Sin URLs nuevas en manifests")


def _local_path_to_url(local_path: str) -> Optional[str]:
    """Reconstruye la URL original a partir de la ruta local usando el mapa registrado."""
    abs_path = os.path.abspath(local_path)
    # Búsqueda directa en el mapa URL↔path
    url = STATE.path_to_url_map.get(abs_path)
    if url:
        return url
    # Fallback: reconstruir desde la estructura de carpetas
    try:
        rel = os.path.relpath(local_path, STATE.base_folder)
        rel = rel.replace("\\", "/")
        if rel.startswith("_ext/"):
            parts = rel.split("/", 2)
            if len(parts) == 3:
                # Reconstruir dominio: solo reemplazar _ por . no es fiable
                # Buscar en el mapa alguna URL que contenga esa parte del path
                path_fragment = "/" + parts[2]
                for url, lpath in STATE.path_to_url_map.items():
                    if lpath.endswith(parts[2]):
                        return url
                # Último recurso: asumir separadores _ → .
                domain = parts[1].replace("_", ".")
                return f"https://{domain}/{parts[2]}"
        else:
            base_parsed = urlparse(STATE.game_base_url)
            # Reintroducir el prefijo de ruta eliminado durante la descarga
            bp = STATE.game_base_path
            rel_with_bp = f"{bp}{rel}" if bp else rel
            return f"{base_parsed.scheme}://{base_parsed.netloc}/{rel_with_bp}"
    except Exception:
        return None


def playwright_headless_phase(real_url: str, session: requests.Session, duration: int = 20):
    """
    Fase 4: Playwright headless durante `duration` segundos.
    Captura todo el tráfico de red del inicio del juego sin interacción.
    """
    _progress.set_phase("Fase 4: Captura headless")
    log.info(f"\n[Fase 4] Playwright headless ({duration} s) para capturar assets de inicio…")

    stop = threading.Event()
    workers = [threading.Thread(target=queue_worker, args=(session, stop), daemon=True)
               for _ in range(DOWNLOAD_WORKERS // 2)]
    for w in workers:
        w.start()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-web-security", "--allow-running-insecure-content",
                      "--disable-features=IsolateOrigins,site-per-process"],
            )
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent=HEADERS["User-Agent"],
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.on("response", lambda r: on_response(r, session))
            page.on("request", lambda req: STATE.enqueue(req.url)
                    if should_download(req.url) else None)

            try:
                page.goto(real_url, timeout=60_000, wait_until="domcontentloaded")
            except Exception as e:
                log.warning(f"  [!] Carga: {e}")

            # Esperar en intervalos simulando interacción para disparar la carga del juego.
            # Parada anticipada: si no llegan archivos nuevos en 8 s, el juego ya cargó todo.
            _last_count  = len(STATE.saved_paths)
            _idle_secs   = 0
            _IDLE_LIMIT  = 8

            for elapsed in range(0, duration, 2):
                time.sleep(2)
                try:
                    page.evaluate("""() => {
                        window.scrollTo(0, document.body.scrollHeight / 2);
                        document.dispatchEvent(new Event('visibilitychange'));
                        window.dispatchEvent(new Event('focus'));
                        const canvas = document.querySelector('canvas');
                        if (canvas) {
                            canvas.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                            canvas.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                            canvas.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                        }
                        document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
                        document.dispatchEvent(new KeyboardEvent('keyup',   {key: 'Enter', keyCode: 13, bubbles: true}));
                    }""")
                except Exception:
                    pass

                current_count = len(STATE.saved_paths)
                if current_count == _last_count:
                    _idle_secs += 2
                    if _idle_secs >= _IDLE_LIMIT:
                        log.info(f"  [✓] Sin actividad por {_IDLE_LIMIT} s — terminando fase 4 anticipadamente")
                        break
                else:
                    _idle_secs  = 0
                    _last_count = current_count

            try:
                sync_cookies(context.cookies(), session)
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    except Exception as e:
        log.warning(f"  [!] Playwright headless: {e}")

    _wait_queue(stop, workers, "Captura headless", timeout=180)
    log.info(f"  Fase 4 completada. Total archivos: {len(STATE.saved_paths)}")


# ─────────────────────────────────────────────
#  Post-procesamiento
# ─────────────────────────────────────────────

def rewrite_urls_for_local(folder: str, game_base_url: str):
    """
    Reescribe URLs absolutas en archivos de texto para que apunten
    a las copias locales usando rutas relativas.
    """
    log.info("\n[*] Reescribiendo URLs para ejecución local…")
    rewritten = 0

    for root, dirs, files in os.walk(folder):
        # Incluir _ext/ — también tiene archivos JS/CSS que referencian assets
        for fname in files:
            fpath = os.path.join(root, fname)
            ext = Path(fpath).suffix.lower()
            if ext not in (".html",".htm",".js",".mjs",".cjs",".css",".json",
                           ".xml",".tmx",".tsx",".webmanifest"):
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    original = f.read()
            except Exception:
                continue

            content = original

            def replace_abs(m, _root=root):
                abs_url = m.group(2)
                url_parsed = urlparse(abs_url)
                if url_parsed.netloc in SKIP_DOMAINS:
                    return m.group(0)
                local = url_to_local(abs_url)
                if os.path.exists(local):
                    try:
                        rel = os.path.relpath(local, _root).replace("\\", "/")
                        return m.group(1) + rel + m.group(3)
                    except ValueError:
                        pass
                return m.group(0)

            # Reemplazar URLs absolutas entre comillas/backticks
            content = re.sub(
                r'(["\'\`])(https?://[^"\'\`\s>]+)(["\'\`])',
                replace_abs,
                content,
            )
            # Reemplazar también URLs sin comillas en atributos HTML (src=https://...)
            content = re.sub(
                r'((?:src|href|action|data|poster)=)(https?://[^\s"\'>\)]+)',
                lambda m: (
                    m.group(1) + os.path.relpath(url_to_local(m.group(2)), root).replace("\\", "/")
                    if os.path.exists(url_to_local(m.group(2))) else m.group(0)
                ),
                content,
            )

            if content != original:
                try:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(content)
                    rewritten += 1
                except Exception:
                    pass

    log.info(f"  [✓] {rewritten} archivos reescritos")


# ─────────────────────────────────────────────────────────────────────────────
#  Service Worker que añade headers COOP/COEP desde el cliente.
#  Necesario para SharedArrayBuffer en GitHub Pages y cualquier hosting estático.
#  Basado en https://github.com/gzuidhof/coi-serviceworker
# ─────────────────────────────────────────────────────────────────────────────
COI_SERVICEWORKER_JS = """\
/* coi-serviceworker — habilita SharedArrayBuffer en hostings estáticos */
if (typeof window === 'undefined') {
  /* ── Contexto: Service Worker ─────────────────────────────────────── */
  self.addEventListener('install', () => self.skipWaiting());
  self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
  self.addEventListener('fetch', e => {
    if (e.request.method !== 'GET') return;
    e.respondWith(
      fetch(e.request).then(r => {
        if (r.status === 0) return r;
        const h = new Headers(r.headers);
        h.set('Cross-Origin-Opener-Policy', 'same-origin');
        h.set('Cross-Origin-Embedder-Policy', 'require-corp');
        h.set('Cross-Origin-Resource-Policy', 'cross-origin');
        return new Response(r.body, {status: r.status, statusText: r.statusText, headers: h});
      }).catch(() => fetch(e.request))
    );
  });
} else {
  /* ── Contexto: ventana del navegador ──────────────────────────────── */
  if (!self.crossOriginIsolated && 'serviceWorker' in navigator) {
    /* Al activarse el SW llama a clients.claim(), lo que dispara
       controllerchange → recarga automática con los headers correctos. */
    navigator.serviceWorker.addEventListener('controllerchange', () => location.reload());
    navigator.serviceWorker
      .register(document.currentScript.src)
      .catch(err => console.warn('[coi-sw]', err));
  }
}
"""

_SERVER_SCRIPT = '''\
"""Servidor local para juegos web — headers COOP/COEP + MIME correcto para WASM."""
import http.server, sys, os

class GameHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
        super().end_headers()
    def guess_type(self, path):
        if str(path).endswith(".wasm"):
            return "application/wasm"
        return super().guess_type(path)
    def log_message(self, fmt, *args):
        pass

os.chdir(os.path.dirname(os.path.abspath(__file__)))
port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
print(f"  http://localhost:{port}/")
print("  Ctrl+C para detener.")
http.server.test(HandlerClass=GameHandler, port=port, bind="localhost")
'''

def generate_server_script(folder: str) -> bool:
    """
    Genera _server.py en la carpeta del juego.
    Detecta si el juego usa SharedArrayBuffer para avisar al usuario.
    Siempre se genera: el servidor corrige MIME de .wasm y es seguro para cualquier juego.
    """
    needs_coop = False
    coop_markers = [
        b"SharedArrayBuffer", b"ENVIRONMENT_IS_PTHREAD",
        b"crossOriginIsolated", b"require-corp",
    ]
    for root, _, files in os.walk(folder):
        if needs_coop:
            break
        for fname in files:
            if Path(fname).suffix.lower() not in (".js", ".mjs", ".html"):
                continue
            try:
                with open(os.path.join(root, fname), "rb") as f:
                    chunk = f.read(300_000)
                if any(m in chunk for m in coop_markers):
                    needs_coop = True
                    break
            except Exception:
                pass

    server_path = os.path.join(folder, "_server.py")
    try:
        with open(server_path, "w", encoding="utf-8") as f:
            f.write(_SERVER_SCRIPT)
    except Exception as e:
        log.warning(f"  [!] No se pudo crear _server.py: {e}")

    return needs_coop


def inject_coi_for_github_pages(folder: str):
    """
    Escribe coi-serviceworker.js junto a cada HTML principal del juego
    e inyecta el <script> de registro como primer elemento de <head>.

    Esto permite que SharedArrayBuffer funcione en GitHub Pages y cualquier
    hosting estático que no soporte configurar headers HTTP propios.

    Flujo en primera visita:
      1. index.html carga → registra el Service Worker
      2. SW instala → activa → llama clients.claim()
      3. controllerchange dispara → location.reload() automático
      4. En la recarga el SW intercepta todas las peticiones y añade
         Cross-Origin-Opener-Policy + Cross-Origin-Embedder-Policy
      5. crossOriginIsolated = true → SharedArrayBuffer disponible → juego OK
    """
    # Encontrar todos los HTML que actúan como punto de entrada del juego
    html_files: list[str] = []
    for root, _, files in os.walk(folder):
        for fname in files:
            if fname.lower() in ("index.html", "index.htm"):
                html_files.append(os.path.join(root, fname))

    if not html_files:
        # Fallback: cualquier .html en la raíz
        for fname in os.listdir(folder):
            if Path(fname).suffix.lower() in (".html", ".htm"):
                html_files.append(os.path.join(folder, fname))

    if not html_files:
        log.warning("  [!] No se encontró index.html para inyectar coi-serviceworker")
        return

    for html_path in html_files:
        html_dir = os.path.dirname(html_path)

        # Escribir coi-serviceworker.js junto al HTML
        sw_path = os.path.join(html_dir, "coi-serviceworker.js")
        try:
            with open(sw_path, "w", encoding="utf-8") as f:
                f.write(COI_SERVICEWORKER_JS)
        except Exception as e:
            log.warning(f"  [!] No se pudo escribir coi-serviceworker.js: {e}")
            continue

        # Inyectar la etiqueta <script> en el HTML
        try:
            with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
                html = f.read()

            if "coi-serviceworker" in html:
                log.debug(f"  [~] coi-serviceworker ya presente en {os.path.basename(html_path)}")
                continue

            tag = '<script src="coi-serviceworker.js"></script>'

            # Insertar como primer hijo de <head> para que se ejecute lo antes posible
            m = re.search(r'(<head[^>]*>)', html, re.IGNORECASE)
            if m:
                insert_pos = m.end()
                html = html[:insert_pos] + "\n  " + tag + html[insert_pos:]
            else:
                html = tag + "\n" + html

            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)

            log.info(f"  [✓] coi-serviceworker inyectado → {os.path.basename(html_path)}")
        except Exception as e:
            log.warning(f"  [!] No se pudo inyectar en {os.path.basename(html_path)}: {e}")



def generate_local_index(folder: str, game_base_url: str) -> Optional[str]:
    parsed = urlparse(game_base_url)
    game_path = sanitize_path(parsed.path)
    game_local = os.path.join(folder, game_path)
    if os.path.exists(game_local):
        return os.path.relpath(game_local, folder)
    for root, _, files in os.walk(folder):
        for f in files:
            if f.lower() in ("index.html", "index.htm"):
                return os.path.relpath(os.path.join(root, f), folder)
    return None


def verify_extraction(folder: str):
    missing = STATE.missing_assets
    if not missing:
        log.info("  [✓] Sin assets faltantes (sin 404s)")
        return
    log.warning(f"\n  [!] {len(missing)} assets no encontrados (404):")
    for url in missing[:20]:
        log.warning(f"      → {url}")
    if len(missing) > 20:
        log.warning(f"      … y {len(missing) - 20} más")
    report_path = os.path.join(folder, "_missing_assets.txt")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("Assets no descargados (HTTP 404):\n")
            f.write("\n".join(missing))
        log.warning(f"  Reporte: {report_path}")
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Recuperación de assets faltantes
# ─────────────────────────────────────────────

def _try_wayback(url: str, session: requests.Session) -> Optional[str]:
    """Consulta Wayback Machine CDX API. Devuelve URL archivada o None."""
    try:
        api = f"https://archive.org/wayback/available?url={url}"
        r = session.get(api, timeout=(5, 20), headers=HEADERS)
        if r.status_code == 200:
            closest = r.json().get("archived_snapshots", {}).get("closest", {})
            if closest.get("available") and closest.get("status") == "200":
                return closest["url"]
    except Exception:
        pass
    return None


def _try_alt_extensions(url: str, session: requests.Session) -> Optional[str]:
    """Prueba extensiones alternativas de audio/imagen para una URL que dio 404."""
    raw = url.split("?")[0]
    dot = raw.rfind(".")
    if dot == -1:
        return None
    ext = raw[dot:].lower()
    if ext in AUDIO_ALTERNATIVES:
        alts = [e for e in AUDIO_ALTERNATIVES if e != ext]
    elif ext in IMAGE_ALTERNATIVES:
        alts = [e for e in IMAGE_ALTERNATIVES if e != ext]
    else:
        return None
    stem = url[:url.rfind(".")]
    for alt in alts:
        alt_url = stem + alt
        if probe_url(alt_url, session):
            return alt_url
    return None


def recover_missing_phase(session: requests.Session, use_wayback: bool = True):
    """
    Fase de recuperación: intenta obtener assets que dieron 404.

    Para cada URL faltante prueba en orden:
      1. Extensiones alternativas de audio/imagen (rápido, paralelo)
      2. Wayback Machine / Internet Archive (más lento, último recurso)

    Actualiza _missing_assets.txt dejando solo los irrecuperables.
    """
    missing_file = os.path.join(STATE.base_folder, "_missing_assets.txt")
    if not os.path.exists(missing_file):
        return

    with open(missing_file, "r", encoding="utf-8") as f:
        missing_urls = [
            ln.strip() for ln in f
            if ln.strip() and ln.strip().startswith("http")
        ]

    if not missing_urls:
        return

    _progress.set_phase("Recuperación")
    log.info(f"\n[Recuperación] {len(missing_urls)} assets faltantes — probando alternativas…")

    recovered: list[str] = []   # URLs alternativas listas para descargar
    still_missing: list[str] = []

    def try_one(orig_url: str) -> tuple[str, Optional[str]]:
        alt = _try_alt_extensions(orig_url, session)
        if alt:
            return orig_url, alt
        if use_wayback:
            wb = _try_wayback(orig_url, session)
            if wb:
                return orig_url, wb
        return orig_url, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        futures = {pool.submit(try_one, u): u for u in missing_urls}
        for fut in concurrent.futures.as_completed(futures):
            orig, result = fut.result()
            if result:
                recovered.append(result)
                log.debug(f"  [✓ Recuperado] {result}")
            else:
                still_missing.append(orig)

    log.info(f"  Recuperados  : {len(recovered)}/{len(missing_urls)}")
    log.info(f"  Irrecuperables: {len(still_missing)}")

    for url in recovered:
        if should_download(url):
            STATE.enqueue(url)

    if recovered:
        stop = threading.Event()
        workers = [threading.Thread(target=queue_worker, args=(session, stop), daemon=True)
                   for _ in range(DOWNLOAD_WORKERS // 2)]
        for w in workers:
            w.start()
        _wait_queue(stop, workers, "Recuperación", timeout=180)

    # Actualizar el archivo: dejar solo los que siguen sin descargarse
    try:
        with open(missing_file, "w", encoding="utf-8") as f:
            if still_missing:
                f.write("Assets no descargados (HTTP 404 — irrecuperables):\n")
                f.write("\n".join(still_missing))
            else:
                f.write("Todos los assets fueron recuperados.\n")
    except Exception:
        pass


def _save_extraction_info(folder: str, game_url: str):
    """Guarda metadatos de la extracción para poder re-ejecutar --recover."""
    import datetime
    info = {
        "game_url":  game_url,
        "engine":    STATE.detected_engine,
        "extracted": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(os.path.join(folder, "_info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
    except Exception:
        pass


def _load_extraction_info(folder: str) -> dict:
    """Lee _info.json de una carpeta ya extraída."""
    try:
        with open(os.path.join(folder, "_info.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ─────────────────────────────────────────────
#  Función principal
# ─────────────────────────────────────────────

def _setup_state(folder: str, real_url: str):
    parsed = urlparse(real_url)

    # Calcular el prefijo de ruta del juego que se eliminará de todas las URLs
    # del dominio principal, de modo que los archivos queden en la raíz de base_folder.
    # Ej: "https://host/html/12345/index.html" → game_base_path = "html/12345/"
    game_path = sanitize_path(parsed.path)          # "html/12345/index.html"
    last_slash = game_path.rfind("/")
    game_base_path = game_path[:last_slash + 1] if last_slash >= 0 else ""
    # Si la URL del juego ya estaba en la raíz del dominio, game_base_path queda "".

    STATE.base_folder    = os.path.abspath(folder)
    STATE.game_base_path = game_base_path
    STATE.primary_domain = parsed.netloc
    STATE.game_base_url  = real_url
    STATE.trusted_domains.update([
        STATE.primary_domain,
        STATE.primary_domain.replace("www.", ""),
        "www." + STATE.primary_domain,
    ])
    os.makedirs(STATE.base_folder, exist_ok=True)


def _print_summary(index_rel: Optional[str], needs_coop: bool):
    total    = len(STATE.saved_paths)
    total_mb = _progress.total_bytes / 1024 / 1024
    entry    = (index_rel or "").replace(chr(92), "/")
    skipped  = STATE.stats.get("skipped", 0)
    capped   = STATE.stats.get("capped", 0)

    print("\n" + "═" * 62)
    print("  Extracción completada  ✓")
    print(f"  Motor detectado  : {STATE.detected_engine.upper()}")
    print(f"  Interceptados    : {STATE.stats['intercepted']}")
    print(f"  Descargados      : {STATE.stats['downloaded']}")
    if skipped:
        print(f"  Omitidos (ya ok) : {skipped}")
    if capped:
        print(f"  URLs cortadas    : {capped}  (límite {MAX_QUEUED_URLS:,} alcanzado)")
    print(f"  Total archivos   : {total}")
    print(f"  Tamaño total     : {total_mb:.1f} MB")
    print(f"  Assets faltantes : {len(STATE.missing_assets)}")
    print(f"  Carpeta          : {STATE.base_folder}")
    if needs_coop:
        print("  SharedArrayBuffer: coi-serviceworker.js inyectado ✓")
    print("═" * 62)
    print("\nPara probar localmente:")
    print(f'  cd "{STATE.base_folder}"')
    print("  python _server.py")
    print(f"  Abre: http://localhost:8080/{entry}")
    print("\nPara subir a GitHub Pages:")
    print("  Mueve la carpeta al repositorio y haz git push.")
    if needs_coop:
        print("  (coi-serviceworker.js ya fue inyectado — funciona sin configuración extra)")


def run_extractor():
    # ── Modo recuperación: python extractor.py --recover <carpeta> ──────────
    if len(sys.argv) >= 2 and sys.argv[1] == "--recover":
        if len(sys.argv) < 3:
            print("Uso: python itchiobrowserGameExtractor.py --recover <carpeta_del_juego>")
            sys.exit(1)

        folder = os.path.abspath(sys.argv[2].strip())
        if not os.path.isdir(folder):
            print(f"[!] Carpeta no encontrada: {folder}")
            sys.exit(1)

        info = _load_extraction_info(folder)
        real_url = info.get("game_url", "")
        if not real_url:
            print("[!] No se encontró _info.json en la carpeta.")
            print("    Proporciona la URL manualmente:")
            real_url = input("URL del juego: ").strip()
            if not real_url:
                sys.exit(1)

        print("=" * 62)
        print("  itch.io Game Extractor  —  Modo Recuperación")
        print("=" * 62)
        print(f"  Carpeta : {folder}")
        print(f"  URL     : {real_url}")
        print(f"  Motor   : {info.get('engine','?').upper()}")
        print("=" * 62)

        _setup_state(folder, real_url)
        STATE.detected_engine = info.get("engine", "unknown")

        session = _make_session()
        _progress.start("Recuperando")

        # 1. Recuperar assets faltantes (alt. extensiones + Wayback Machine)
        recover_missing_phase(session, use_wayback=True)

        # 2. Playwright extendido para capturar assets dinámicos no descubiertos
        log.info("\n[*] Sesión Playwright extendida (60 s) para assets dinámicos…")
        playwright_headless_phase(real_url, session, duration=60)

        _progress.close()

        rewrite_urls_for_local(STATE.base_folder, STATE.game_base_url)
        index_rel = generate_local_index(STATE.base_folder, STATE.game_base_url)

        log.info("\n[*] Verificando extracción…")
        verify_extraction(STATE.base_folder)

        needs_coop = generate_server_script(STATE.base_folder)
        if needs_coop:
            inject_coi_for_github_pages(STATE.base_folder)

        _print_summary(index_rel, needs_coop)
        return

    # ── Extracción normal ────────────────────────────────────────────────────
    if len(sys.argv) >= 3:
        url_input  = sys.argv[1].strip()
        portal_url = sys.argv[2].strip()
    else:
        print("=" * 62)
        print("  itch.io Browser Game Extractor  v5.0")
        print("=" * 62)
        url_input  = input("\nURL directa del juego: ").strip()
        portal_url = input("URL del portal de itch.io: ").strip()

    # Derivar nombre de carpeta del último segmento de la URL del portal
    _pp = urlparse(portal_url)
    _portal_is_itch = _pp.netloc.endswith(".itch.io") or _pp.netloc in ("itch.io", "www.itch.io")
    if _portal_is_itch:
        _seg = [s for s in _pp.path.strip("/").split("/") if s]
        folder_name = _seg[-1] if _seg else "game_output"
    else:
        # Fallback: último segmento de la URL del juego
        _gp = urlparse(url_input)
        _seg = [s for s in _gp.path.strip("/").split("/") if s]
        folder_name = _seg[-1] if _seg else "game_output"

    folder   = re.sub(r'[\\/*?:"<>|]', "_", folder_name).strip() or "game_output"
    real_url = url_input  # URL directa — sin resolución automática

    _setup_state(folder, real_url)

    log.info(f"\n[+] URL     : {real_url}")
    log.info(f"[+] Portal  : {portal_url}")
    log.info(f"[+] Dominio : {STATE.primary_domain}")
    log.info(f"[+] Carpeta : {STATE.base_folder}")

    session = _make_session()

    # Descargar portada desde el portal de itch.io
    log.info("[*] Descargando portada del juego…")
    _download_game_cover(portal_url or url_input, folder, session)

    _progress.start("Extrayendo")

    log.info("\n" + "═" * 62)
    log.info("  Extracción automática completa — 4 fases + recuperación")
    log.info("═" * 62)

    static_crawl_phase(real_url, session)
    predict_assets_phase(real_url, session)
    manifest_deep_follow(session)
    playwright_headless_phase(real_url, session, duration=30)

    # Fase 5: recuperar assets con 404 mediante extensiones alt. + Wayback Machine
    if STATE.missing_assets:
        recover_missing_phase(session, use_wayback=True)

    _progress.close()

    rewrite_urls_for_local(STATE.base_folder, STATE.game_base_url)
    index_rel = generate_local_index(STATE.base_folder, STATE.game_base_url)

    log.info("\n[*] Verificando extracción…")
    verify_extraction(STATE.base_folder)

    needs_coop = generate_server_script(STATE.base_folder)
    if needs_coop:
        log.info("\n[*] Juego con SharedArrayBuffer — inyectando coi-serviceworker…")
        inject_coi_for_github_pages(STATE.base_folder)

    _save_extraction_info(STATE.base_folder, real_url)

    _print_summary(index_rel, needs_coop)


if __name__ == "__main__":
    run_extractor()
