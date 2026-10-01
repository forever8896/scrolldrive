// Frameline home: one example site playing in a small window, its lines following the film.
import { $, api, reduceMotion } from "/static/common.js";

(async () => {
  let S;
  try { S = await api("/api/showcase"); } catch { return; }
  const video = $("#loop");
  video.poster = S.poster;
  video.src = S.film_mobile;
  if (reduceMotion) { video.removeAttribute("autoplay"); video.pause(); }

  const text = FramelineText.mount($("#screen"), S.copy.acts || [], S.layout);
  const tick = () => {
    if (video.duration) text.show(video.currentTime / video.duration);
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
})();
