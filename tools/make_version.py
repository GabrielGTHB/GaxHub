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
va = grab("apk.html", r"GAX_V\s*=\s*(\d+)")
if va < v:
    sys.exit("apk.html no puede tener un GAX_V menor que win.html")
for f in ("win.html", "apk.html"):
    if b"\r\n" in open(f, "rb").read():
        print("Aviso: %s tiene saltos de linea de Windows (CRLF). Las apps lo aceptan, pero conviene guardarlo con LF." % f)
meta = {"v": v, "win": sha("win.html"), "apk": sha("apk.html")}
if va != v:  # el celular va adelantado: la PC sigue leyendo "v" y no ve ninguna actualizacion
    meta["v_apk"] = va
nmin = 0
if os.path.exists("native_min.txt"):
    nmin = int(open("native_min.txt").read().strip() or 0)
if nmin:
    meta["native_min"] = nmin
# novedades/vN.txt (N = version de win.html): una linea por novedad; es lo que ve la persona en la ventana "Hay una versión nueva".
# Debe existir el archivo de la version actual y NO debe quedar ningun log de versiones anteriores (asi no se repite nada).
nf = os.path.join("novedades", "v%d.txt" % v)
if not os.path.exists(nf):
    sys.exit("Falta %s: crea el log de esta version con SOLO lo nuevo (una linea por novedad)." % nf)
viejos = [x for x in os.listdir("novedades") if x.endswith(".txt") and x != "v%d.txt" % v]
if viejos:
    sys.exit("Hay logs viejos en novedades/ (%s): borralos, solo debe quedar v%d.txt." % (", ".join(sorted(viejos)), v))
notes = []
for ln in open(nf, encoding="utf-8"):
    ln = ln.strip()
    if ln and not ln.startswith("#"):
        notes.append(ln.lstrip("-*• \t").strip()[:240])
if not notes:
    sys.exit("%s esta vacio: escribe que trae esta version." % nf)
meta["notes"] = notes[:12]
# novedades_apk.txt (opcional): lo que ve SOLO el celular en la ventana de version nueva (la PC sigue leyendo "notes")
if os.path.exists("novedades_apk.txt"):
    na = []
    for ln in open("novedades_apk.txt", encoding="utf-8"):
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            na.append(ln.lstrip("-*• \t").strip()[:240])
    if na:
        meta["notes_apk"] = na[:12]
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
