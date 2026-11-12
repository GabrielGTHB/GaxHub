"""Genera version.json (version de la pantalla, novedades, hashes y datos del Gax.exe) para GitHub.
Uso: python tools/make_version.py [--exe dist/Gax.exe --repo usuario/repo --tag v2]"""
import argparse, hashlib, json, os, re, sys

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def grab(path, rx):
    m = re.search(rx, open(path, encoding="utf-8").read(6000 if path.endswith(".html") else None))
    if not m:
        sys.exit("No encontre %s en %s" % (rx, path))
    return int(m.group(1))

ap = argparse.ArgumentParser()
ap.add_argument("--exe"); ap.add_argument("--repo"); ap.add_argument("--tag")
ap.add_argument("--out", default="version.json")
a = ap.parse_args()

v = grab("win.html", r"GAX_V\s*=\s*(\d+)")
if grab("apk.html", r"GAX_V\s*=\s*(\d+)") != v:
    sys.exit("win.html y apk.html deben tener el mismo GAX_V")
meta = {"v": v, "win": sha("win.html"), "apk": sha("apk.html")}
nmin = 0
if os.path.exists("native_min.txt"):
    nmin = int(open("native_min.txt").read().strip() or 0)
if nmin:
    meta["native_min"] = nmin
# novedades.txt: una linea por novedad; es lo que ve la persona en la ventana "Hay una versión nueva"
if os.path.exists("novedades.txt"):
    notes = []
    for ln in open("novedades.txt", encoding="utf-8"):
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            notes.append(ln.lstrip("-*• ").strip()[:240])
    if notes:
        meta["notes"] = notes[:12]
if a.exe:
    nv = grab("windows/app.py", r"(?m)^NATIVE_V\s*=\s*(\d+)")
    meta["native"] = {"v": nv, "exe": "https://github.com/%s/releases/download/%s/Gax.exe" % (a.repo, a.tag), "sha256": sha(a.exe)}
elif os.path.exists(a.out):  # conserva los datos del exe de la ultima release
    try:
        old = json.load(open(a.out))
        if isinstance(old.get("native"), dict):
            meta["native"] = old["native"]
    except Exception:
        pass
json.dump(meta, open(a.out, "w"), indent=1)
open(a.out, "a").write("\n")
print(open(a.out).read())
