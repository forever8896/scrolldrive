#!/usr/bin/env python3
"""Same film, different writers: alternate voices for a finished run, for the landing page.

  python3 scripts/voices.py <run-name>   ->  outputs/runs/<run>/voices.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import copywriter  # noqa: E402

VOICES = [
    ("Nature documentary", "Rewrite everything as a hushed wildlife documentary narrator observing a rare creature in its habitat. Scientific calm, reverent, dry."),
    ("Noir detective", "Rewrite everything as a world-weary 1940s private eye narrating a case. Short, hard sentences, rain, regret."),
    ("Tabloid scoop", "Rewrite everything as a breathless tabloid exclusive about a celebrity in crisis. Scandal, sources close to, shocking photos. No exclamation marks."),
    ("Wellness app", "Rewrite everything as a gentle mindfulness app talking the reader through a headache. Soft, second person, breathing."),
]

name = sys.argv[1]
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg = json.load(open(os.path.join(root, "configs", f"{name}.json")))
out = [{"voice": "Deadpan luxury", **{k: cfg["copy"][k] for k in ("title", "tagline", "beats", "cta")}}]
for label, note in VOICES:
    c = copywriter.write(cfg, note=note, fresh=True)
    out.append({"voice": label, **{k: c[k] for k in ("title", "tagline", "beats", "cta")}})
    print(label, "|", c["tagline"], "|", c["beats"])
json.dump(out, open(os.path.join(root, "outputs", "runs", name, "voices.json"), "w"), indent=2)
