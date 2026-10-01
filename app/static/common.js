// Shared helpers for the landing page and the studio.
export const $ = (sel, root = document) => root.querySelector(sel);
export const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
export const usd = (n) => `$${Number(n).toFixed(2)}`;
export const EASE = "cubic-bezier(0.16, 1, 0.3, 1)";

// A deployed studio may need an access code for paid actions: open it once with ?code=...
const urlCode = new URLSearchParams(location.search).get("code");
if (urlCode) { try { localStorage.setItem("frameline-code", urlCode); } catch { /* fine */ } }
const accessCode = () => { try { return localStorage.getItem("frameline-code") || ""; } catch { return ""; } };

export async function api(path, body) {
  const isBlob = body instanceof Blob;
  const code = accessCode();
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      ...(isBlob ? { "Content-Type": body.type || "application/octet-stream" } : { "Content-Type": "application/json" }),
      ...(code ? { "X-Studio-Code": code } : {}),
    },
    body: body === undefined ? undefined : isBlob ? body : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// Motion helper: WAAPI entrance that is skipped entirely under reduced motion.
export function enter(el, from = { opacity: 0, transform: "translateY(12px)" }, opts = {}) {
  if (reduceMotion || !el) return;
  el.animate([from, { opacity: 1, transform: "none", filter: "none" }], { duration: 600, easing: EASE, fill: "backwards", ...opts });
}

export const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable: fine */ } },
};
