/* Frameline text layer: mounts the lines of a scroll film into a stage and shows each one
   for its stretch of the film. show(p) takes the film's progress, 0 to 1. */
window.FramelineText = (() => {
  const POS = ["tl", "tc", "tr", "ml", "c", "mr", "bl", "bc", "br"];
  const SIZES = ["s", "m", "l", "xl"];
  const ENTERS = ["rise", "fade", "blur", "wipe", "type"];
  const pad = (n) => String(n).padStart(2, "0");

  function mount(host, acts, layout) {
    layout = layout || {};
    const stage = document.createElement("div");
    stage.className = "fl-stage";
    stage.dataset.preset = layout.preset || "cinema";
    host.appendChild(stage);
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const n = acts.length;
    const items = acts.map((a, i) => {
      const L = (layout.beats || [])[i] || {};
      const el = document.createElement("div");
      el.className = "fl-beat";
      el.dataset.pos = POS.includes(L.pos) ? L.pos : "bc";
      el.dataset.size = SIZES.includes(L.size) ? L.size : "m";
      el.dataset.enter = ENTERS.includes(L.enter) ? L.enter : "rise";
      el.dataset.index = `${pad(i + 1)} / ${pad(n)}`;
      el.dataset.n = pad(i + 1);
      const t = document.createElement("span");
      t.className = "fl-text";
      t.dataset.edit = `beats.${i}`;
      t.textContent = a.text;
      el.appendChild(t);
      stage.appendChild(el);
      return { el, t, from: a.from, to: a.to };
    });

    // Typewriter entrance: characters appear over a moment; never while someone edits.
    function typeIn(it) {
      if (reduce || it.typing || document.body.classList.contains("fl-editing")) return;
      it.typing = true;
      const full = it.t.textContent;
      const start = performance.now();
      const dur = Math.min(1100, 24 * full.length + 200);
      const step = (now) => {
        const k = Math.min(1, (now - start) / dur);
        it.t.textContent = full.slice(0, Math.round(full.length * k));
        if (k < 1 && it.el.classList.contains("on")) requestAnimationFrame(step);
        else { it.t.textContent = full; it.typing = false; }
      };
      requestAnimationFrame(step);
    }

    function show(p) {
      for (const it of items) {
        const on = p >= it.from && p < it.to;
        if (on === it.el.classList.contains("on")) continue;
        it.el.classList.toggle("on", on);
        if (on && it.el.dataset.enter === "type") typeIn(it);
      }
    }
    return { stage, items, show };
  }
  return { mount, POS, SIZES, ENTERS };
})();
