/**
 * Cloudflare Worker — Game Vault API
 * ─────────────────────────────────────────────────────────────────────────────
 * Lista dinámicamente todos los juegos del bucket R2 y devuelve un JSON con
 * formato idéntico al _games_manifest.json, pero generado al instante sin
 * necesidad de ejecutar ningún script local.
 *
 * Añade cabeceras CORS para que index.html pueda hacer fetch() desde cualquier
 * origen (GitHub Pages, Cloudflare Pages, localhost, etc.).
 *
 * ── DESPLIEGUE ───────────────────────────────────────────────────────────────
 * 1. Instala Wrangler (una sola vez):
 *      npm install -g wrangler
 *
 * 2. Inicia sesión:
 *      wrangler login
 *
 * 3. Desde la carpeta GamesPage, despliega:
 *      wrangler deploy
 *
 * 4. Copia la URL que imprime Wrangler (ej. https://game-vault-api.TU_USUARIO.workers.dev)
 *    y pégala como WORKER_URL en index.html.
 *
 * ── CONFIGURACIÓN ────────────────────────────────────────────────────────────
 * El bucket R2 se conecta al Worker mediante el binding "GAMES" definido en
 * wrangler.toml — no hay credenciales en este archivo.
 * ─────────────────────────────────────────────────────────────────────────────
 */

// Orden de preferencia para la imagen de portada.
// portada.* tiene máxima prioridad — es descargada directamente de itch.io por el extractor.
const COVER_CANDIDATES = [
  "portada.jpg", "portada.png", "portada.webp", "portada.gif",
  "cover.png", "cover.jpg", "cover.jpeg", "cover.webp",
  "thumbnail.png", "thumbnail.jpg",
  "screenshot.png", "screenshot.jpg",
  "preview.png", "preview.jpg",
  "index.apple-touch-icon.png",
  "icon.png",
  "index.icon.png",
];

const CORS = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  "Access-Control-Allow-Headers": "*",
  "Access-Control-Max-Age":       "86400",
};

// ── helpers ───────────────────────────────────────────────────────────────────

/** "super-mario-64" → "Super Mario 64" */
function fmtName(id) {
  return id
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, c => c.toUpperCase())
    .trim();
}

/** Lista todos los objetos del bucket (paginado). */
async function listAllKeys(bucket) {
  const keys = new Set();
  let cursor;
  do {
    const page = await bucket.list({ limit: 1000, cursor });
    for (const obj of page.objects) keys.add(obj.key);
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return keys;
}

/** Detecta carpetas raíz que contienen index.html y no empiezan por "_". */
function findGameFolders(keys) {
  const folders = new Set();
  for (const key of keys) {
    const slash = key.indexOf("/");
    if (slash > 0) {
      const folder = key.slice(0, slash);
      if (!folder.startsWith("_") && keys.has(`${folder}/index.html`)) {
        folders.add(folder);
      }
    }
  }
  return [...folders].sort();
}

/** Devuelve la primera imagen de portada disponible (ruta relativa) o null. */
function findCover(folder, keys) {
  for (const c of COVER_CANDIDATES) {
    if (keys.has(`${folder}/${c}`)) return `${folder}/${c}`;
  }
  return null;
}

// ── handler ───────────────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    // Preflight CORS
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    try {
      const keys    = await listAllKeys(env.GAMES);
      const folders = findGameFolders(keys);

      const games = folders.map(folder => ({
        id:    folder,
        name:  fmtName(folder),
        path:  folder,
        index: `${folder}/index.html`,   // ruta relativa — index.html la convierte a URL completa
        cover: findCover(folder, keys),   // ruta relativa o null
      }));

      const body = JSON.stringify({
        games,
        total:     games.length,
        generated: new Date().toISOString(),
      });

      return new Response(body, {
        status: 200,
        headers: {
          "Content-Type":  "application/json; charset=utf-8",
          "Cache-Control": "public, max-age=60, must-revalidate",
          ...CORS,
        },
      });

    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 500,
        headers: { "Content-Type": "application/json", ...CORS },
      });
    }
  },
};
