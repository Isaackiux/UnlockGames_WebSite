import os
import json
import datetime
from pathlib import Path
import boto3
from tqdm import tqdm

# ==========================
# CONFIGURACIÓN
# ==========================

ACCESS_KEY  = "875e08b2fc2591bfb645fbfde6772dbc"
SECRET_KEY  = "fbc2b4724e23841bb27e342979ff3ace9758adeb3940a0d0687ba27fb77e5f81"
ACCOUNT_ID  = "5875217a70ee1440398281dacf58fe13"
BUCKET_NAME = "games"
ENDPOINT    = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"

# ==========================

s3 = boto3.client(
    "s3",
    endpoint_url=ENDPOINT,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name="auto",
)

# Imágenes de portada buscadas en este orden dentro de cada carpeta de juego
COVER_CANDIDATES = [
    "portada.jpg", "portada.png", "portada.webp", "portada.gif",   # descargada por el extractor
    "cover.png", "cover.jpg", "cover.jpeg", "cover.webp",
    "thumbnail.png", "thumbnail.jpg",
    "screenshot.png", "screenshot.jpg",
    "preview.png", "preview.jpg",
    "index.apple-touch-icon.png",   # Godot — 180 × 180
    "icon.png",
    "index.icon.png",               # Godot — 32 × 32 (último recurso)
]


# ── helpers ──────────────────────────────────────────────────────────────────

def get_all_objects() -> dict[str, dict]:
    """Devuelve un dict {key: object_metadata} con todos los objetos del bucket."""
    objs: dict[str, dict] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET_NAME):
        for obj in page.get("Contents", []):
            objs[obj["Key"]] = obj
    return objs


def get_existing_game_folders(objs: dict) -> set[str]:
    """Devuelve el conjunto de carpetas raíz que contienen un index.html."""
    folders: set[str] = set()
    for key in objs:
        parts = key.split("/")
        if len(parts) >= 2 and not parts[0].startswith("_"):
            # Confirmar que tiene index.html
            idx_key = f"{parts[0]}/index.html"
            if idx_key in objs:
                folders.add(parts[0])
    return folders


def find_cover(folder: str, objs: dict) -> str | None:
    """Busca la primera imagen de portada disponible para un juego."""
    for candidate in COVER_CANDIDATES:
        key = f"{folder}/{candidate}"
        if key in objs:
            return key
    return None


def fmt_name(folder_id: str) -> str:
    """Convierte 'super-mario-127' → 'Super Mario 127'."""
    return " ".join(w.capitalize() for w in folder_id.replace("-", " ").replace("_", " ").split())


# ── CORS ─────────────────────────────────────────────────────────────────────

def ensure_cors():
    """
    Configura la política CORS del bucket para permitir peticiones GET/HEAD
    desde cualquier origen (necesario para que index.html cargue desde
    GitHub Pages, Cloudflare Pages, etc.)
    """
    try:
        s3.put_bucket_cors(
            Bucket=BUCKET_NAME,
            CORSConfiguration={
                "CORSRules": [{
                    "AllowedMethods": ["GET", "HEAD"],
                    "AllowedOrigins": ["*"],
                    "AllowedHeaders": ["*"],
                    "ExposeHeaders":  ["Content-Length", "Content-Type", "ETag"],
                    "MaxAgeSeconds":  3600,
                }]
            },
        )
        print("  [✓] CORS configurado")
    except Exception as e:
        print(f"  [!] No se pudo configurar CORS: {e}")
        print("      Configúralo manualmente en el panel de Cloudflare R2.")


# ── MANIFEST ─────────────────────────────────────────────────────────────────

def generate_and_upload_manifest(objs: dict | None = None) -> dict:
    """
    Genera _games_manifest.json con la lista de todos los juegos y sus
    portadas, y lo sube al bucket.
    """
    if objs is None:
        print("\nListando objetos del bucket…")
        objs = get_all_objects()

    folders = get_existing_game_folders(objs)
    games: list[dict] = []

    for folder in sorted(folders):
        games.append({
            "id":    folder,
            "name":  fmt_name(folder),
            "path":  folder,
            "index": f"{folder}/index.html",
            "cover": find_cover(folder, objs),
        })

    manifest = {
        "games":     games,
        "total":     len(games),
        "generated": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    body = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key="_games_manifest.json",
        Body=body,
        ContentType="application/json; charset=utf-8",
        CacheControl="public, max-age=60, must-revalidate",
    )
    print(f"  [✓] _games_manifest.json subido ({len(games)} juego(s))")
    return manifest


# ── UPLOAD ───────────────────────────────────────────────────────────────────

def upload_folder(folder_path: Path):
    files = [
        Path(root) / filename
        for root, _, filenames in os.walk(folder_path)
        for filename in filenames
    ]
    print(f"\nSubiendo '{folder_path.name}' ({len(files)} archivos)")

    for file in tqdm(files, unit="arch"):
        relative = file.relative_to(folder_path.parent)
        key      = relative.as_posix()
        # Detectar Content-Type para archivos importantes
        extra: dict = {}
        suffix = file.suffix.lower()
        mime_map = {
            ".html": "text/html; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".mjs":  "application/javascript; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".wasm": "application/wasm",
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif":  "image/gif",
            ".svg":  "image/svg+xml",
            ".ogg":  "audio/ogg",
            ".mp3":  "audio/mpeg",
            ".wav":  "audio/wav",
            ".mp4":  "video/mp4",
            ".webm": "video/webm",
        }
        if suffix in mime_map:
            extra["ContentType"] = mime_map[suffix]

        s3.upload_file(str(file), BUCKET_NAME, key, ExtraArgs=extra or None)


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    current  = Path.cwd()
    print("\nListando objetos existentes en el bucket…")
    objs     = get_all_objects()
    existing = get_existing_game_folders(objs)

    print("\nJuegos ya en el bucket:")
    for f in sorted(existing):
        print(f"  • {f}")

    # ── subir carpetas nuevas y sincronizar portadas faltantes ──
    uploaded_new = False
    PORTADA_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    for item in sorted(current.iterdir()):
        if not item.is_dir():
            continue
        if item.name.startswith("_") or item.name.startswith("."):
            continue
        if item.name == "__pycache__":
            continue
        if item.name in existing:
            # Juego ya en bucket: subir portada.* si existe localmente pero no en R2
            synced = False
            for ext in PORTADA_EXTS:
                local_portada = item / f"portada{ext}"
                r2_key = f"{item.name}/portada{ext}"
                if local_portada.exists() and r2_key not in objs:
                    print(f"\nSubiendo portada de '{item.name}': portada{ext}")
                    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                            ".png": "image/png", ".webp": "image/webp",
                            ".gif": "image/gif"}.get(ext, "image/jpeg")
                    s3.upload_file(str(local_portada), BUCKET_NAME, r2_key,
                                   ExtraArgs={"ContentType": mime})
                    synced = True
            if not synced:
                print(f"\nSaltando '{item.name}' (ya existe en el bucket)")
            else:
                uploaded_new = True
            continue
        upload_folder(item)
        uploaded_new = True

    # ── re-listar si subimos algo nuevo ─────────────────
    if uploaded_new:
        print("\nActualizando listado tras la subida…")
        objs = get_all_objects()

    # ── CORS ────────────────────────────────────────────
    print("\nConfigurando CORS…")
    ensure_cors()

    # ── manifest ────────────────────────────────────────
    print("\nGenerando manifest de juegos…")
    manifest = generate_and_upload_manifest(objs)

    print(f"\n{'─'*52}")
    print(f"  Proceso terminado — {manifest['total']} juego(s) en la librería")
    print(f"{'─'*52}")
    print("\nPróximos pasos:")
    print("  1. Activa el acceso público del bucket en el panel de Cloudflare R2")
    print("     (Bucket → Settings → Public Access → Allow Access)")
    print("  2. Copia la URL pública que te da R2 (pub-XXXX.r2.dev)")
    print("  3. Pégala en index.html como valor de R2_PUBLIC_URL")
    print("  4. Sube index.html a GitHub Pages o cualquier hosting estático")
    print()


if __name__ == "__main__":
    main()
