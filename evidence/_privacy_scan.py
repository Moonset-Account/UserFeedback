import json
import re

d = json.load(open("evidence/local_verify_all.json"))
text = json.dumps(d)

hex64 = re.findall(r"[0-9a-f]{40,}", text)
print("long hex strings (>=40 chars):", hex64)
print("PEM/PRIVATE markers present:", "BEGIN" in text or "PRIVATE" in text)
print("truncated keyid entries:", text.count("(truncated)"))

c01 = json.dumps(d["cases"][0])
print(
    "C01 keyids sample:",
    re.findall(r'"keyid": "([^"]+)"', c01)[:6],
)

c04 = next(c for c in d["cases"] if c["case"] == "C04")
for scn in c04["scenarios"]:
    print(
        scn["name"],
        "->",
        json.dumps(scn.get("seed", {}), default=str)[:110],
    )
