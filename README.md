# Frameline

Websites that play like a film as you scroll. You talk to one agent, the director. It plans the shots, generates them, checks every still with its own eyes and reshoots weak ones, films the camera moves between them, writes the words and lays them out, all inside a budget it can see but not change. You pay once, then ask for changes in plain words or edit any word on the finished site.

Other agents can hire it too: `POST /api/agent/films` answers with an x402 payment challenge (USDC on Base), and once paid the director makes the whole site on autopilot and hands back a page a person can use, a live site, the film and a zip. See `/docs#hire` and `/llms.txt`.

The agent pays for its own generation from a wallet whose key it never holds: [1Claw](https://1claw.co) keeps the key in an HSM and signs only payments that fit rules a human set. LLM calls go through 1Claw Shroud, with the provider key in a 1Claw vault.

Full documentation is served by the app at `/docs`.

## Run locally

```bash
python3 app/server.py 8080   # http://localhost:8080
```

Needs Python 3.11+ and ffmpeg (or `pip install -r requirements.txt` for a bundled one), plus a 1Claw agent key. See `/docs#local`.

## Deploy on a 1Claw runtime

A `python` runtime cloning this repository, started with `HOST=0.0.0.0 python app/server.py`. Set `STUDIO_ACCESS_CODE` so only you can approve paid renders. See `/docs#deploy`.
