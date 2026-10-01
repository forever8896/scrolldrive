# Deploy

This folder is a static site. Upload it as-is to any static host:

- **Vercel:** `npx vercel deploy --prod` in this folder
- **Netlify:** drag the folder onto app.netlify.com/drop
- **Cloudflare Pages:** `npx wrangler pages deploy .`
- **Any web server:** serve the folder; make sure `.mp4` is sent as `video/mp4` and HTTP range requests are enabled (needed for scrubbing).
