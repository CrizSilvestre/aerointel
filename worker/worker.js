// worker/worker.js — Botón "Actualizar" del dashboard (Cloudflare Worker).
// El navegador NUNCA ve el token de GitHub: vive aquí como secret (GITHUB_TOKEN).
// Límite de 30 min DEL LADO DEL SERVIDOR: se consulta a GitHub la última corrida del
// workflow (de cualquier tipo: cron o manual); si es reciente, se responde 429 con la
// espera restante. Sin KV ni estado propio: GitHub es la fuente de verdad del cooldown.

const REPO = "CrizSilvestre/aerointel";
const WORKFLOW = "update.yml";
const COOLDOWN_MIN = 30;
const ALLOWED_ORIGINS = [
  "https://crizsilvestre.github.io",   // producción (GitHub Pages)
  "http://localhost:8200",             // desarrollo local (serve.mjs)
];

function corsHeaders(origin) {
  const allowed = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return {
    "Access-Control-Allow-Origin": allowed,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json; charset=utf-8",
  };
}

const json = (obj, status, headers) =>
  new Response(JSON.stringify(obj), { status, headers });

export default {
  async fetch(req, env) {
    const cors = corsHeaders(req.headers.get("Origin") || "");
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    if (req.method !== "POST") return json({ error: "solo POST" }, 405, cors);

    const gh = {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "AeroIntel-Refresh-Worker",
    };

    // 1) ¿Cuándo fue la última corrida (cron o manual)? — el cooldown real
    const runsRes = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/runs?per_page=1`,
      { headers: gh });
    if (!runsRes.ok) return json({ error: `github runs ${runsRes.status}` }, 502, cors);
    const last = (await runsRes.json()).workflow_runs?.[0]?.created_at;
    if (last) {
      const mins = (Date.now() - Date.parse(last)) / 60000;
      if (mins < COOLDOWN_MIN) {
        return json({ ok: false, wait_min: Math.ceil(COOLDOWN_MIN - mins), last_run: last },
                    429, cors);
      }
    }

    // 2) Disparar el workflow (workflow_dispatch)
    const disp = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
      { method: "POST", headers: { ...gh, "Content-Type": "application/json" },
        body: JSON.stringify({ ref: "main" }) });
    if (disp.status !== 204) return json({ error: `dispatch ${disp.status}` }, 502, cors);
    return json({ ok: true }, 200, cors);
  },
};
