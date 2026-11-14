import os
import re
import sys
import json
import time
import base64
import hashlib
import subprocess
import shutil
import tempfile
import urllib.request
import sqlite3
import threading
import pathlib
import webview
import librouteros
from librouteros.query import Key

FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(os.path.abspath(__file__))
RES = getattr(sys, "_MEIPASS", HERE)
DATA = os.path.dirname(sys.executable) if FROZEN else HERE
DB = os.path.join(DATA, "clientes.db")
CFG = os.path.join(DATA, "config.json")
DEFAULT = {"host": "192.168.88.1", "port": 8728, "user": "admin", "pass": "", "sync": True, "repo": "GabrielGTHB/GaxHub"}
LIMIT_RE = re.compile(r"^\d+(\.\d+)?[kM]/\d+(\.\d+)?[kM]$")


def load_cfg():
    c = dict(DEFAULT)
    try:
        with open(CFG, encoding="utf-8") as f:
            c.update(json.load(f))
    except Exception:
        pass
    c.pop("panel_pass", None)
    return c


def save_cfg(c):
    c = dict(c)
    c.pop("panel_pass", None)
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(c, f)


def connect(c):
    kw = dict(host=c["host"], port=int(c["port"]), username=c["user"], password=c["pass"], timeout=8)
    try:
        return librouteros.connect(encoding="utf-8", **kw)
    except TypeError:
        return librouteros.connect(**kw)


def db():
    con = sqlite3.connect(DB)
    con.execute("create table if not exists kv (id integer primary key, data text not null)")
    return con


def load_store():
    con = db()
    r1 = con.execute("select data from kv where id = 1").fetchone()
    r2 = con.execute("select data from kv where id = 2").fetchone()
    con.close()
    return (json.loads(r1[0]) if r1 else []), (json.loads(r2[0]) if r2 else [])


def save_store(clients, dead):
    con = db()
    con.execute("insert or replace into kv (id, data) values (1, ?)", (json.dumps(clients),))
    con.execute("insert or replace into kv (id, data) values (2, ?)", (json.dumps(dead),))
    con.commit()
    con.close()


def ip_of(t):
    if isinstance(t, (list, tuple)):
        t = t[0] if t else ""
    t = str(t or "").split(",")[0].strip()
    return t[:-3] if t.endswith("/32") else t


def to_bps(s):
    m = re.match(r"^(\d+(?:\.\d+)?)([kKmMgG]?)$", str(s).strip())
    if not m:
        return None
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]


_conn = {"api": None}
_lock = threading.Lock()


def drop_conn():
    api = _conn["api"]
    _conn["api"] = None
    if api is not None:
        try:
            api.close()
        except Exception:
            pass


def pair(s):
    try:
        a, b = str(s).split("/")
        return int(float(a)), int(float(b))
    except Exception:
        return 0, 0


# ===================== SINCRONIZACION (PC <-> celular, via scripts del MikroTik) =====================
SYNC_CHUNK = 8000
DEAD_TTL = 180 * 86400 * 1000


def fnv(s):
    h = 0x811C9DC5
    for ch in s.encode("utf-8"):
        h = ((h ^ ch) * 0x01000193) & 0xFFFFFFFF
    return "%08x" % h


def _ts(c):
    try:
        return int(c.get("ts") or 0)
    except (TypeError, ValueError):
        return 0


def _rank(c):
    return (_ts(c), str(c.get("paid") or ""))


def _qkey(c):
    return str(c.get("queue") or "").strip().lower()


def _body(c):
    return {k: v for k, v in c.items() if k != "ts"}


def merge(local, ldead, remote, rdead, now):
    """Mezcla por cliente: gana la version mas nueva (ts); los borrados se propagan con 'tumbas'.
    Clientes que existen solo de un lado con la misma queue se consideran la misma persona."""
    L = {c["id"]: c for c in local}
    R = {c["id"]: c for c in remote}
    dead = {}
    for d in list(ldead) + list(rdead):
        dead[d["id"]] = max(dead.get(d["id"], -1), int(d["ts"]))
    res = {}
    for i in set(L) | set(R):
        a, b = L.get(i), R.get(i)
        if a is None:
            res[i] = b
        elif b is None:
            res[i] = a
        else:
            res[i] = b if _rank(b) >= _rank(a) else a
    lo = sorted(i for i in L if i not in R)
    byq = {}
    for j in sorted(i for i in R if i not in L):
        q = _qkey(R[j])
        if q:
            byq.setdefault(q, []).append(j)
    for i in lo:
        q = _qkey(L[i])
        lst = byq.get(q) if q else None
        if lst:
            j = lst.pop(0)
            a, b = L[i], R[j]
            w = dict(b if _rank(b) >= _rank(a) else a)
            keep, drop = min(i, j), max(i, j)
            w["id"] = keep
            res.pop(i, None)
            res.pop(j, None)
            res[keep] = w
            dead[drop] = max(dead.get(drop, -1), _ts(a), _ts(b))
    out = [c for i, c in res.items() if not (i in dead and dead[i] >= _ts(c))]
    out.sort(key=lambda c: c["id"])
    live = {c["id"] for c in out}
    kd = [{"id": i, "ts": t} for i, t in sorted(dead.items()) if i not in live and now - t < DEAD_TTL]
    return out, kd


def local_put(stored, sdead, incoming, now):
    """Aplica lo que guarda la interfaz: sella con ts solo lo que cambio y deja tumba de lo borrado."""
    mx = 0
    for c in stored:
        mx = max(mx, _ts(c))
    for d in sdead:
        mx = max(mx, int(d["ts"]))
    t = max(now, mx + 1)
    sm = {c["id"]: c for c in stored}
    dead = {d["id"]: d for d in sdead}
    out, seen = [], set()
    for c in incoming:
        c = dict(c)
        seen.add(c["id"])
        old = sm.get(c["id"])
        c["ts"] = _ts(old) if old is not None and _body(old) == _body(c) else t
        out.append(c)
    for i in sm:
        if i not in seen:
            dead[i] = {"id": i, "ts": t}
    for c in out:
        d = dead.get(c["id"])
        if d and d["ts"] < c["ts"]:
            del dead[c["id"]]
    return out, sorted(dead.values(), key=lambda d: d["id"])


def canon(clients, dead):
    cs = sorted(clients, key=lambda c: c["id"])
    ds = sorted(dead, key=lambda d: d["id"])
    return json.dumps([cs, ds], sort_keys=True, separators=(",", ":"), ensure_ascii=False)



def ms():
    return int(time.time() * 1000)


class SyncEngine:
    """Motor de sincronizacion. load()->(clientes, tumbas); save(clientes, tumbas); remote_ctx(fn) ejecuta fn(remoto)."""

    def __init__(self, load, save, remote_ctx):
        self.load, self.save, self.remote_ctx = load, save, remote_ctx
        self.rev, self.gen, self.dirty, self.v = None, 0, False, 0
        self.mu, self.net = threading.RLock(), threading.Lock()

    def snap(self):
        with self.mu:
            return {"clients": self.load()[0], "v": self.v}

    def put(self, incoming, now=None):
        with self.mu:
            sc, sd = self.load()
            out, dead = local_put(sc, sd, incoming, now or ms())
            self.save(out, dead)
            self.gen += 1
            self.dirty = True
            self.v += 1

    def sync(self):
        if not self.net.acquire(timeout=40):
            return dict(ok=False, msg="sincronizacion en curso", **self.snap())
        try:
            for _ in range(3):
                if not self.remote_ctx(self._once):
                    break
            return dict(ok=True, **self.snap())
        except Exception as e:
            return dict(ok=False, msg=str(e), **self.snap())
        finally:
            self.net.release()

    def _once(self, rm):
        with self.mu:
            gen0, dirty0 = self.gen, self.dirty
        meta = rm.get_meta()
        if meta and meta["rev"] == self.rev and not dirty0:
            return False
        R = rm.read(meta) if meta and meta["rev"] != self.rev else None
        with self.mu:
            sc, sd = self.load()
            if R is not None:
                mc, md = merge(sc, sd, R.get("clients", []), R.get("dead", []), ms())
            else:
                mc, md = sc, sd
            if canon(mc, md) != canon(sc, sd):
                self.save(mc, md)
                self.v += 1
        push = dirty0 or (R is not None and canon(mc, md) != canon(R.get("clients", []), R.get("dead", []))) or (meta is None and bool(mc or md))
        if push:
            self.rev = rm.write({"v": 1, "clients": mc, "dead": md}, meta)
        else:
            self.rev = meta["rev"] if meta else None
        with self.mu:
            if self.gen == gen0:
                self.dirty = False
            return self.gen != gen0


class RouterRemote:
    """Guarda los datos en /system/script del MikroTik: gax-meta + gax-d-<rev>-<n> (base64 en trozos)."""

    def __init__(self, api):
        self.sc = api.path("system", "script")

    def _find(self, name):
        return list(self.sc.select(".id", "name", "source").where(Key("name") == name))

    def _add(self, name, source):
        try:
            self.sc.add(name=name, source=source, policy="read")
        except Exception:
            self.sc.add(name=name, source=source)

    def get_meta(self):
        rows = self._find("gax-meta")
        if not rows:
            return None
        try:
            m = json.loads(rows[0]["source"])
            m["_id"] = rows[0][".id"]
            return m
        except (ValueError, KeyError):
            return None

    def read(self, meta):
        parts = []
        for i in range(int(meta["n"])):
            rows = self._find("gax-d-%s-%d" % (meta["rev"], i))
            if not rows:
                raise RuntimeError("datos incompletos en el MikroTik, reintentando")
            parts.append(rows[0]["source"])
        b64 = "".join(parts)
        if fnv(b64) != meta["sum"]:
            raise RuntimeError("datos danados en el MikroTik")
        return json.loads(base64.b64decode(b64).decode("utf-8"))

    def write(self, data, old):
        rev = max(ms(), int(old["rev"]) + 1 if old else 0)
        b64 = base64.b64encode(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).decode("ascii")
        chunks = [b64[i:i + SYNC_CHUNK] for i in range(0, len(b64), SYNC_CHUNK)] or [""]
        for i, ch in enumerate(chunks):
            self._add("gax-d-%d-%d" % (rev, i), ch)
        meta = json.dumps({"v": 1, "rev": rev, "n": len(chunks), "sum": fnv(b64)}, separators=(",", ":"))
        if old:
            self.sc.update(**{".id": old["_id"], "source": meta})
            for i in range(int(old["n"])):
                for r in self._find("gax-d-%s-%d" % (old["rev"], i)):
                    self.sc.remove(r[".id"])
        else:
            self._add("gax-meta", meta)
        return rev


def api_ctx(fn):
    """Ejecuta fn(remoto) reutilizando la conexion al MikroTik (reintenta una vez si se cayo)."""
    err = None
    with _lock:
        for _ in range(2):
            try:
                if _conn["api"] is None:
                    _conn["api"] = connect(load_cfg())
                return fn(RouterRemote(_conn["api"]))
            except Exception as e:
                err = e
                drop_conn()
    raise err


SYNC = SyncEngine(load_store, save_store, api_ctx)


def kick_sync():
    if load_cfg().get("sync", True):
        threading.Thread(target=SYNC.sync, daemon=True).start()


IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$")
MARK = "[GAX]"
SEP = " || "


def merge_comment(old, new):
    """Pone el comentario de GAX al inicio y conserva lo que ya hubiera escrito el usuario en esa queue."""
    old = str(old or "")
    if old.startswith(MARK):
        i = old.find(SEP)
        tail = old[i + len(SEP):] if i >= 0 else ""
    else:
        tail = old
    return new + (SEP + tail if tail else "")


def _same_limit(a, b):
    try:
        return [to_bps(x) for x in str(a).split("/")] == [to_bps(x) for x in str(b).split("/")]
    except Exception:
        return False



# ===================== ACTUALIZACIONES AUTOMATICAS (GitHub) =====================
def _upd_base():
    """Donde se guardan las actualizaciones de la pantalla: carpeta oculta de Windows (AppData), no al lado de Gax."""
    b = os.environ.get("LOCALAPPDATA")
    return os.path.join(b, "Gax") if b else os.path.join(tempfile.gettempdir(), "Gax")


UPD_DIR = _upd_base()
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
RAW = "https://raw.githubusercontent.com/%s/main/%s"
MAX_UPD = 3 * 1024 * 1024
MAX_EXE = 300 * 1024 * 1024
# Version del programa (la parte de Python/.exe). Subela cada vez que publiques un Gax.exe nuevo.
# Es distinta de GAX_V (version de la pantalla/HTML).
NATIVE_V = 6
RUN_V = [0]  # version de la pantalla que se esta mostrando


def html_version(path):
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r"GAX_V\s*=\s*(\d+)", f.read(6000))
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


def _upd(name):
    return os.path.join(UPD_DIR, name)


def _clean_notes(a):
    """Novedades de version.json: maximo 12 lineas de 240 caracteres."""
    return [str(x).strip()[:240] for x in (a if isinstance(a, list) else []) if str(x).strip()][:12]


def _jload(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _clear_update():
    for n in ("app.html", "meta.json"):
        try:
            os.remove(_upd(n))
        except OSError:
            pass


def _save_meta(meta):
    with open(_upd("meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _mark_bad(v):
    os.makedirs(UPD_DIR, exist_ok=True)
    with open(_upd("bad.json"), "w", encoding="utf-8") as f:
        json.dump({"v": v}, f)
    _clear_update()


BUNDLED = [""]


def pick_index(bundled):
    """Elige que pantalla abrir: la actualizada (si es valida y mas nueva) o la que viene en el programa.
    Una actualizacion sin confirmar se prueba como maximo 2 veces; si no abre bien, se descarta."""
    BUNDLED[0] = bundled
    bv = html_version(bundled)
    meta = _jload(_upd("meta.json"))
    app_p = _upd("app.html")
    if not meta or not os.path.exists(app_p):
        return bundled, bv
    try:
        v = int(meta.get("v") or 0)
        if v <= bv:
            _clear_update()
            return bundled, bv
        with open(app_p, "rb") as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() != meta.get("sha") or html_version(app_p) != v:
            _clear_update()
            return bundled, bv
        if not meta.get("ok"):
            n = int(meta.get("n") or 0)
            if n >= 2:
                _mark_bad(v)
                return bundled, bv
            meta["n"] = n + 1
            _save_meta(meta)
        return app_p, v
    except Exception:
        _clear_update()
        return bundled, bv


def _http_get(url):
    req = urllib.request.Request(url + "?t=%d" % time.time(), headers={"User-Agent": "Gax"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read(MAX_UPD + 1)
    if len(data) > MAX_UPD:
        raise ValueError("archivo demasiado grande")
    return data


# ---- actualizacion del propio programa (Gax.exe) desde GitHub Releases ----
def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class NotReady(Exception):
    """La version nueva todavia no esta completa en GitHub (los archivos no coinciden con version.json)."""


NOT_READY_MSG = "La versión nueva todavía se está publicando en GitHub. Prueba de nuevo en unos minutos."
UPD_ST = {"phase": "idle", "label": "", "done": 0, "total": 0, "result": None}  # avance para la barra de descarga


def _match_variant(data, sha):
    """Devuelve los bytes que coinciden con el SHA-256 de version.json (con saltos de linea LF o CRLF), o None."""
    lf = data.replace(b"\r\n", b"\n")
    for d in (data, lf, lf.replace(b"\n", b"\r\n")):
        if hashlib.sha256(d).hexdigest() == sha:
            return d
    return None


def _download_exe(url, sha, dest):
    part = dest + ".part"
    h, n = hashlib.sha256(), 0
    req = urllib.request.Request(url, headers={"User-Agent": "Gax"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            try:
                total = int(r.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            UPD_ST.update(label="Descargando", done=0, total=total)
            while True:
                b = r.read(256 * 1024)
                if not b:
                    break
                n += len(b)
                if n > MAX_EXE:
                    raise ValueError("archivo demasiado grande")
                h.update(b)
                f.write(b)
                UPD_ST["done"] = n
        if h.hexdigest() != sha:
            raise NotReady(NOT_READY_MSG)
        with open(part, "rb") as f:
            if f.read(2) != b"MZ":
                raise ValueError("el archivo descargado no es un programa de Windows")
        os.replace(part, dest)
    finally:
        try:
            os.remove(part)
        except OSError:
            pass


def _exe_dest():
    """El .exe nuevo se baja al lado del actual (mismo disco: el cambio es instantaneo y no deja carpetas).
    Si ahi no se puede escribir, a la carpeta temporal de Windows."""
    here = os.path.dirname(os.path.abspath(sys.executable))
    for d in (here, tempfile.gettempdir()):
        try:
            probe = os.path.join(d, "Gax.new.exe.part")
            with open(probe, "wb"):
                pass
            os.remove(probe)
            return os.path.join(d, "Gax.new.exe")
        except OSError:
            continue
    raise OSError("no hay donde guardar la descarga")


def _selftest(path):
    """Prueba que el programa nuevo arranca (con --selftest sale solo) antes de reemplazar al actual."""
    try:
        r = subprocess.run([path, "--selftest"], timeout=120, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0)
        return r.returncode == 0
    except Exception:
        return False


def _stage_exe(repo, native):
    """Descarga el Gax.exe nuevo, verifica su SHA-256 y comprueba que arranca. Devuelve True si queda listo."""
    v = int(native.get("v") or 0)
    sha = str(native.get("sha256") or "").lower()
    url = str(native.get("exe") or "")
    if v <= NATIVE_V or not re.fullmatch(r"[0-9a-f]{64}", sha):
        return False
    pre = "https://github.com/%s/releases/download/" % repo
    if not url.startswith(pre) or not re.fullmatch(r"[A-Za-z0-9_.%+-]+(/[A-Za-z0-9_.%+-]+)*", url[len(pre):]):
        raise ValueError("direccion del programa no valida")
    done = _jload(_upd("exe.json")) or {}
    if int(done.get("v") or 0) == v and done.get("sha") == sha and os.path.isfile(str(done.get("path") or "")):
        return True
    _clear_exe()
    dest = _exe_dest()
    os.makedirs(UPD_DIR, exist_ok=True)
    _download_exe(url, sha, dest)
    UPD_ST.update(label="Verificando", done=0, total=0)
    if not _selftest(dest):
        try:
            os.remove(dest)
        except OSError:
            pass
        raise ValueError("el programa nuevo no arrancó en la prueba; se queda la versión actual")
    with open(_upd("exe.json"), "w", encoding="utf-8") as f:
        json.dump({"v": v, "sha": sha, "path": dest}, f)
    return True


def _clear_exe():
    meta = _jload(_upd("exe.json")) or {}
    path = str(meta.get("path") or "")
    for q in ((path if os.path.basename(path) == "Gax.new.exe" else ""), _upd("exe.json")):
        if q:
            try:
                os.remove(q)
            except OSError:
                pass


def ensure_home():
    """Si Gax.exe esta suelto (no dentro de una carpeta llamada Gax), crea la carpeta Gax a su lado,
    copia ahi el programa y sus datos, crea un acceso directo en el Escritorio y se reabre desde la carpeta.
    Lo de antes se borra solo despues de cerrar. Devuelve True si hay que cerrar este proceso."""
    if not FROZEN:
        return False
    home = ""
    made = []
    try:
        exe = os.path.abspath(sys.executable)
        here = os.path.dirname(exe)
        if os.path.basename(here).lower() == "gax":
            return False
        home = os.path.join(here, "Gax")
        os.makedirs(home, exist_ok=True)
        target = os.path.join(home, os.path.basename(exe))
        shutil.copy2(exe, target)
        if os.path.getsize(target) != os.path.getsize(exe):
            raise OSError("copia incompleta")
        old = []
        for n in ("clientes.db", "config.json", "datos-ventana"):
            src, dst = os.path.join(here, n), os.path.join(home, n)
            if not os.path.exists(src) or os.path.exists(dst):
                continue
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            made.append(dst)
            old.append(src)
        try:  # acceso directo en el Escritorio (si falla, no pasa nada)
            q = lambda t: t.replace("'", "''")
            ps = ("$d=[Environment]::GetFolderPath('Desktop');$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Gax.lnk'));"
                  "$s.TargetPath='%s';$s.WorkingDirectory='%s';$s.IconLocation='%s,0';$s.Save()") % (q(target), q(home), q(target))
            subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], creationflags=0x08000000, timeout=25)
        except Exception:
            pass
        bat = os.path.join(home, "limpiar.bat")
        lines = [
            "@echo off",
            "set n=0",
            ":w",
            "ping -n 2 127.0.0.1 >nul",
            'del /f /q "%s" >nul 2>&1' % exe,
            'if not exist "%s" goto ok' % exe,
            "set /a n+=1",
            "if %n% lss 30 goto w",
            ":ok",
        ]
        for s in old:
            lines.append(('rd /s /q "%s" >nul 2>&1' if os.path.isdir(s) else 'del /f /q "%s" >nul 2>&1') % s)
        lines.append('rd /s /q "%s" >nul 2>&1' % os.path.join(here, "actualizacion"))
        lines.append('(goto) 2>nul & del "%~f0"')
        with open(bat, "w", encoding="mbcs" if os.name == "nt" else "utf-8", newline="\r\n") as f:
            f.write("\n".join(lines) + "\n")
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
        subprocess.Popen([target], cwd=home, creationflags=flags, close_fds=True)
        subprocess.Popen(["cmd", "/c", bat], cwd=home, creationflags=flags, close_fds=True)
        return True
    except Exception:
        for m in made:  # si algo fallo, no se toca lo de antes: sigue funcionando donde estaba
            try:
                shutil.rmtree(m) if os.path.isdir(m) else os.remove(m)
            except Exception:
                pass
        return False


def apply_pending_exe():
    """Si hay un Gax.exe nuevo descargado y verificado, lo pone en lugar del actual y lo reabre.
    El cambio lo hace un script temporal (deja de existir al terminar): no quedan copias ni carpetas.
    Devuelve True si hay que cerrar este proceso."""
    if not FROZEN:
        return False
    meta = _jload(_upd("exe.json"))
    if not meta:
        return False
    try:
        newp = str(meta.get("path") or "")
        if int(meta.get("v") or 0) <= NATIVE_V or not os.path.isfile(newp) or os.path.basename(newp) != "Gax.new.exe" \
                or _sha_file(newp) != meta.get("sha"):
            _clear_exe()
            return False
        exe = os.path.abspath(sys.executable)
        bat = os.path.join(tempfile.gettempdir(), "Gax-actualizar.bat")
        lines = [
            "@echo off",
            "set n=0",
            ":w",
            "ping -n 2 127.0.0.1 >nul",
            'move /y "%s" "%s" >nul 2>&1' % (newp, exe),
            "if not errorlevel 1 goto ok",
            "set /a n+=1",
            "if %n% lss 30 goto w",
            'del /f /q "%s" >nul 2>&1' % newp,
            ":ok",
            'del /f /q "%s" >nul 2>&1' % _upd("exe.json"),
            'start "" "%s"' % exe,
            '(goto) 2>nul & del "%~f0"',
        ]
        with open(bat, "w", encoding="mbcs" if os.name == "nt" else "utf-8", newline="\r\n") as f:
            f.write("\n".join(lines) + "\n")
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
        subprocess.Popen(["cmd", "/c", bat], cwd=tempfile.gettempdir(), creationflags=flags, close_fds=True)
        return True
    except Exception:
        return False


def cleanup_old():
    """Quita lo que dejaban las versiones anteriores al actualizar: la carpeta «actualizacion» y las copias .bak."""
    try:
        here = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else DATA
        shutil.rmtree(os.path.join(here, "actualizacion"), ignore_errors=True)
        for n in ("Gax.exe.bak",):
            try:
                os.remove(os.path.join(here, n))
            except OSError:
                pass
        stale = os.path.join(here, "Gax.new.exe")
        meta = _jload(_upd("exe.json")) or {}
        if os.path.exists(stale) and os.path.abspath(str(meta.get("path") or "")) != os.path.abspath(stale):
            os.remove(stale)
        try:
            os.remove(stale + ".part")
        except OSError:
            pass
    except Exception:
        pass


def do_check_update(repo_in=None, mode=None):
    """mode='check': solo mira si hay una version nueva y devuelve lo que trae (no descarga nada).
    mode='install' (o sin modo, como las pantallas viejas): descarga y deja lista la version nueva."""
    cur = RUN_V[0]
    c = load_cfg()
    if repo_in is not None:
        repo_in = str(repo_in).strip()
        if repo_in and not REPO_RE.match(repo_in):
            return {"ok": False, "state": "error", "v": cur, "msg": "El repositorio debe verse como usuario/gax-panel"}
        c["repo"] = repo_in
        save_cfg(c)
    repo = str(c.get("repo") or "").strip()
    if not repo:
        return {"ok": True, "state": "off", "v": cur, "msg": "Escribe tu usuario/repositorio de GitHub para activar las actualizaciones."}
    if not REPO_RE.match(repo):
        return {"ok": False, "state": "error", "v": cur, "msg": "El repositorio debe verse como usuario/gax-panel"}
    try:
        meta = json.loads(_http_get(RAW % (repo, "version.json")).decode("utf-8"))
        v = int(meta["v"])
        sha = str(meta["win"]).lower()
        if v <= 0 or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("version.json no es valido")
        native = meta.get("native") if isinstance(meta.get("native"), dict) else {}
        nmin = int(meta.get("native_min") or 0)
        exe_v = int(native.get("v") or 0)
        exe_new = exe_v > NATIVE_V and FROZEN
        staged = int((_jload(_upd("meta.json")) or {}).get("v") or 0) if os.path.exists(_upd("app.html")) else 0
        bad = int((_jload(_upd("bad.json")) or {}).get("v") or 0)

        if mode == "check":
            # la pantalla nueva solo sirve si este programa ya trae lo que ella necesita
            html_new = nmin <= NATIVE_V and v > cur and v > bad
            if not (html_new or exe_new):
                if nmin > NATIVE_V:
                    return {"ok": True, "state": "none", "v": cur, "msg": "Hay una versión nueva que necesita actualizar el programa (Gax.exe). Descarga la versión nueva desde GitHub."}
                if v <= cur:
                    return {"ok": True, "state": "none", "v": cur, "msg": "Estás al día (versión %d)." % cur}
                return {"ok": True, "state": "none", "v": cur, "msg": "La versión %d falló antes. Esperando una más nueva." % v}
            done = _jload(_upd("exe.json")) or {}
            exe_ok = (not exe_new) or (int(done.get("v") or 0) == exe_v and os.path.isfile(str(done.get("path") or "")))
            html_ok = (not html_new) or staged >= v
            return {"ok": True, "state": "available", "v": max(v, cur), "cur": cur,
                    "notes": _clean_notes(meta.get("notes")), "restart": bool(exe_new), "staged": bool(exe_ok and html_ok)}

        exe_msg = ""
        if exe_new and _stage_exe(repo, native):
            exe_msg = "Programa nuevo (versión %d) listo. Se instala al reabrir la app." % exe_v

        def fin(d):
            if exe_msg:
                d = {"ok": True, "state": "ready", "v": d["v"], "msg": ((d["msg"] + " ") if d.get("state") == "ready" else "") + exe_msg}
            return d

        if nmin > NATIVE_V:
            # esta pantalla necesita un programa mas nuevo: no se aplica hasta que el .exe se actualice
            if exe_msg:
                return fin({"ok": True, "state": "none", "v": cur, "msg": ""})
            return {"ok": True, "state": "none", "v": cur, "msg": "Hay una versión nueva que necesita actualizar el programa (Gax.exe). Descarga la versión nueva desde GitHub."}
        if v <= cur:
            return fin({"ok": True, "state": "none", "v": cur, "msg": "Estás al día (versión %d)." % cur})
        if v <= bad:
            return fin({"ok": True, "state": "none", "v": cur, "msg": "La versión %d falló antes. Esperando una más nueva." % v})
        if staged >= v:
            if mode == "install":
                return fin({"ok": True, "state": "ready", "v": v, "msg": "Versión %d lista. Se aplica al reabrir la app." % v})
            return fin({"ok": True, "state": "none", "v": cur, "msg": "Estás al día (versión %d)." % cur})
        data = None
        for i in range(2):  # una segunda vez por si GitHub todavia estaba publicando
            data = _match_variant(_http_get(RAW % (repo, "win.html")), sha)
            if data is not None:
                break
            if i == 0:
                time.sleep(1.5)
        if data is None:
            raise NotReady(NOT_READY_MSG)
        text = data.decode("utf-8")
        m = re.search(r"GAX_V\s*=\s*(\d+)", text[:6000])
        if not m or int(m.group(1)) != v or "</html>" not in text:
            raise ValueError("archivo no valido")
        os.makedirs(UPD_DIR, exist_ok=True)
        _clear_update()
        tmp = _upd("app.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, _upd("app.html"))
        _save_meta({"v": v, "sha": sha})
        return fin({"ok": True, "state": "ready", "v": v, "msg": "Versión %d lista. Se aplica al reabrir la app." % v})
    except NotReady as e:
        return {"ok": False, "state": "error", "v": cur, "msg": str(e)}
    except Exception as e:
        return {"ok": False, "state": "error", "v": cur, "msg": "No se pudo actualizar: " + str(e)}


class Api:
    def router_push(self, items):
        """Refleja los clientes en Winbox: crea la simple queue (si falta y hay IP) y escribe el comentario."""
        if not isinstance(items, list):
            return {"results": []}
        results = []
        try:
            api = connect(load_cfg())
        except Exception as e:
            return {"error": "no se pudo conectar al MikroTik: " + str(e)}
        try:
            q = api.path("queue", "simple")
            rows = {}
            for r in q.select(".id", "name", "target", "max-limit", "comment"):
                if r.get("name") and r["name"] not in rows:
                    rows[r["name"]] = r
            for it in items:
                name = str(it.get("queue", "")).strip()
                old = str(it.get("old", "")).strip()
                res = {"id": it.get("id"), "queue": name, "ok": False, "skip": False, "action": "", "error": ""}
                results.append(res)
                try:
                    ip = str(it.get("ip", "")).strip()
                    limit = str(it.get("limit", "")).strip()
                    comment = str(it.get("comment", ""))[:200]
                    limit_ok = bool(LIMIT_RE.match(limit)) and all(float(p[:-1]) > 0 for p in limit.split("/"))
                    if ip and not IP_RE.match(ip):
                        res["error"] = "IP invalida"
                        res["skip"] = True
                        continue
                    target = (ip if "/" in ip else ip + "/32") if ip else ""
                    row = rows.get(name)
                    if row is None and old and old != name and old in rows:
                        r2 = rows[old]
                        q.update(**{".id": r2[".id"], "name": name})
                        r2["name"] = name
                        rows[name] = r2
                        del rows[old]
                        row = r2
                    if row is None:
                        if not target:
                            res["error"] = "sin IP: no se creo la queue en Winbox"
                            res["skip"] = True
                            continue
                        if not limit_ok:
                            res["error"] = "sin plan: no se creo la queue en Winbox"
                            res["skip"] = True
                            continue
                        q.add(**{"name": name, "target": target, "max-limit": limit, "comment": comment})
                        res["ok"], res["action"] = True, "creada"
                        continue
                    upd = {}
                    new_c = merge_comment(row.get("comment"), comment)
                    if new_c != str(row.get("comment") or ""):
                        upd["comment"] = new_c
                    if it.get("setIp") and target and ip_of(row.get("target")) != ip_of(target):
                        upd["target"] = target
                    if it.get("setLimit") and limit_ok and not _same_limit(row.get("max-limit", ""), limit):
                        upd["max-limit"] = limit
                    if upd:
                        upd[".id"] = row[".id"]
                        q.update(**upd)
                        res["action"] = "actualizada"
                    else:
                        res["action"] = "sin cambios"
                    res["ok"] = True
                except Exception as e:
                    res["error"] = str(e)
        except Exception as e:
            return {"error": "no se pudo leer el MikroTik: " + str(e)}
        finally:
            try:
                api.close()
            except Exception:
                pass
        return {"results": results}

    def router_delete(self, names):
        """Borra de Winbox la simple queue de un cliente borrado. Solo si hay exactamente una con ese nombre."""
        if not isinstance(names, list):
            return {"results": []}
        try:
            api = connect(load_cfg())
        except Exception as e:
            return {"error": "no se pudo conectar al MikroTik: " + str(e)}
        results = []
        try:
            q = api.path("queue", "simple")
            rows = {}
            for r in q.select(".id", "name", "dynamic"):
                if r.get("dynamic") in (True, "true", "yes") or not r.get("name"):
                    continue
                rows.setdefault(r["name"], []).append(r)
            for n in names:
                name = str(n).strip()
                res = {"queue": name, "ok": False, "action": "", "error": ""}
                results.append(res)
                try:
                    found = rows.get(name, [])
                    if not name:
                        res["error"] = "nombre vacio"
                    elif not found:
                        res["ok"], res["action"] = True, "no existia"
                    elif len(found) > 1:
                        res["error"] = "hay %d queues con ese nombre; borrala a mano en Winbox" % len(found)
                    else:
                        q.remove(found[0][".id"])
                        res["ok"], res["action"] = True, "borrada"
                except Exception as e:
                    res["error"] = str(e)
        except Exception as e:
            return {"error": "no se pudo leer el MikroTik: " + str(e)}
        finally:
            try:
                api.close()
            except Exception:
                pass
        return {"results": results}

    def get_config(self):
        c = load_cfg()
        return {
            "host": c["host"], "port": c["port"], "user": c["user"],
            "has_pass": bool(c["pass"]), "configured": os.path.exists(CFG),
            "sync": bool(c.get("sync", True)), "repo": str(c.get("repo") or ""),
        }

    def put_config(self, d):
        d = d or {}
        c = load_cfg()
        c["host"] = str(d.get("host") or c["host"]).strip()
        try:
            c["port"] = int(d.get("port") or c["port"])
        except ValueError:
            return {"ok": False, "msg": "Puerto invalido"}
        c["user"] = str(d.get("user") or c["user"]).strip()
        if d.get("pass"):
            c["pass"] = str(d["pass"])
        if isinstance(d.get("sync"), bool):
            c["sync"] = d["sync"]
        if isinstance(d.get("repo"), str):
            r = d["repo"].strip()
            c["repo"] = r if (not r or REPO_RE.match(r)) else c.get("repo", "")
        save_cfg(c)
        with _lock:
            drop_conn()
        SYNC.rev = None
        try:
            connect(c).close()
            return {"ok": True, "msg": "Guardado. Conexion exitosa con el MikroTik."}
        except Exception as e:
            return {"ok": False, "msg": "Guardado, pero no se pudo conectar: " + str(e)}

    def check_update(self, repo=None, mode=None):
        return do_check_update(repo, mode)

    def update_start(self):
        """Empieza a descargar la version nueva en segundo plano; la pantalla pregunta el avance con update_status."""
        if UPD_ST["phase"] == "busy":
            return {"ok": True}
        UPD_ST.update(phase="busy", label="Buscando", done=0, total=0, result=None)

        def run():
            try:
                res = do_check_update(None, "install")
            except Exception as e:
                res = {"ok": False, "state": "error", "msg": "No se pudo actualizar: " + str(e)}
            UPD_ST["result"] = res
            UPD_ST["phase"] = "done"

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def update_status(self):
        return {"ok": True, "phase": UPD_ST["phase"], "label": UPD_ST["label"], "done": UPD_ST["done"],
                "total": UPD_ST["total"], "result": UPD_ST["result"] if UPD_ST["phase"] == "done" else None}

    def apply_update(self):
        """Aplica ya lo descargado: con Gax.exe nuevo se cierra y se reabre solo; si es solo la pantalla, se recarga."""
        try:
            if apply_pending_exe():
                def bye():
                    time.sleep(0.4)
                    for w in list(webview.windows):
                        try:
                            w.destroy()
                        except Exception:
                            pass
                threading.Thread(target=bye, daemon=True).start()
                return {"ok": True, "restart": True}
            idx, RUN_V[0] = pick_index(BUNDLED[0])
            w = webview.windows[0]
            url = pathlib.Path(idx).resolve().as_uri()
            threading.Timer(0.3, lambda: w.load_url(url)).start()
            return {"ok": True, "restart": False}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def ui_ready(self):
        meta = _jload(_upd("meta.json"))
        if meta and int(meta.get("v") or 0) == RUN_V[0] and not meta.get("ok"):
            meta["ok"] = True
            _save_meta(meta)
        return {"ok": True, "reload": False}

    def ui_fail(self):
        """La pantalla actualizada tuvo errores al abrir: se descarta y se vuelve a la instalada."""
        meta = _jload(_upd("meta.json"))
        if meta and int(meta.get("v") or 0) == RUN_V[0]:
            _mark_bad(RUN_V[0])
            try:
                w = webview.windows[0]
                RUN_V[0] = html_version(BUNDLED[0])
                w.load_url(BUNDLED[0])
            except Exception:
                pass
        return {"ok": True, "reload": False}

    def get_clients(self):
        return load_store()[0]

    def put_clients(self, data):
        if not isinstance(data, list):
            return {"error": "formato invalido"}
        SYNC.put([c for c in data if isinstance(c, dict) and "id" in c])
        kick_sync()
        return {"ok": True}

    def sync_clients(self):
        if not load_cfg().get("sync", True):
            r = SYNC.snap()
            r.update(ok=True, off=True)
            return r
        return SYNC.sync()

    def apply_limits(self, items):
        if not isinstance(items, list):
            return {"results": []}
        results = []
        try:
            api = connect(load_cfg())
        except Exception as e:
            msg = "no se pudo conectar al MikroTik: " + str(e)
            for it in items:
                results.append({"queue": str(it.get("queue", "")), "limit": "", "ok": False, "error": msg})
            return {"results": results}
        try:
            queues = api.path("queue", "simple")
            for it in items:
                name = str(it.get("queue", ""))
                limit = str(it.get("limit", ""))
                res = {"queue": name, "limit": limit, "ok": False, "error": ""}
                if not name or not LIMIT_RE.match(limit) or not all(float(p[:-1]) > 0 for p in limit.split("/")):
                    res["error"] = "datos invalidos"
                else:
                    try:
                        rows = list(queues.select(".id", "name").where(Key("name") == name))
                        if not rows:
                            res["error"] = "no existe una queue con ese nombre"
                        else:
                            queues.update(**{".id": rows[0][".id"], "max-limit": limit})
                            res["ok"] = True
                    except Exception as e:
                        res["error"] = str(e)
                results.append(res)
        finally:
            api.close()
        return {"results": results}

    def router_usage(self):
        err = ""
        rows = None
        with _lock:
            for _ in range(2):
                try:
                    if _conn["api"] is None:
                        _conn["api"] = connect(load_cfg())
                    rows = list(_conn["api"].path("queue", "simple").select("name", "bytes", "rate", "dynamic"))
                    break
                except Exception as e:
                    err = str(e)
                    drop_conn()
        if rows is None:
            return {"error": "no se pudo conectar al MikroTik: " + err}
        usage = {}
        for row in rows:
            if row.get("dynamic") in (True, "true", "yes") or not row.get("name"):
                continue
            bu, bd = pair(row.get("bytes", "0/0"))
            ru, rd = pair(row.get("rate", "0/0"))
            usage[str(row["name"]).lower()] = {"bu": bu, "bd": bd, "ru": ru, "rd": rd}
        return {"usage": usage}

    def router_arp(self):
        try:
            api = connect(load_cfg())
        except Exception as e:
            return {"error": "no se pudo conectar al MikroTik: " + str(e)}
        out = []
        try:
            for row in api.path("ip", "arp").select("address", "mac-address", "interface", "status", "comment", "complete"):
                st = row.get("status") or ("reachable" if row.get("complete") in (True, "true", "yes") else "")
                out.append({"address": row.get("address", ""), "mac": row.get("mac-address", ""), "interface": row.get("interface", ""), "status": st, "comment": row.get("comment", "")})
        except Exception as e:
            return {"error": "no se pudo leer el MikroTik: " + str(e)}
        finally:
            api.close()
        return {"arp": out}

    def router_queues(self):
        try:
            api = connect(load_cfg())
        except Exception as e:
            return {"error": "no se pudo conectar al MikroTik: " + str(e)}
        out, skipped = [], 0
        try:
            for row in api.path("queue", "simple").select("name", "max-limit", "dynamic", "target"):
                if row.get("dynamic") in (True, "true", "yes"):
                    continue
                parts = str(row.get("max-limit", "0/0")).split("/")
                up = to_bps(parts[0])
                down = to_bps(parts[1]) if len(parts) > 1 else up
                if not row.get("name") or not up or not down:
                    skipped += 1
                    continue
                out.append({
                    "name": row["name"],
                    "up": round(up / 1e6, 2),
                    "down": round(down / 1e6, 2),
                    "cut": up <= 64000 and down <= 64000,
                    "ip": ip_of(row.get("target")),
                })
        except Exception as e:
            return {"error": "no se pudo leer el MikroTik: " + str(e)}
        finally:
            api.close()
        return {"queues": out, "skipped": skipped}


def main():
    # permite que los sonidos suenen sin un clic previo (entrada y pasar el mouse)
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--autoplay-policy=no-user-gesture-required")
    index = os.path.join(RES, "static", "index.html")
    if not os.path.exists(index):
        index = os.path.join(RES, "index.html")
    if ensure_home():
        return
    if apply_pending_exe():
        return
    cleanup_old()
    index, RUN_V[0] = pick_index(index)
    webview.create_window(
        "Gax · Panel de clientes", index, js_api=Api(),
        width=1280, height=820, min_size=(760, 560),
    )
    webview.start(private_mode=False, storage_path=os.path.join(DATA, "datos-ventana"))


if __name__ == "__main__":
    if "--selftest" in sys.argv:  # lo usa la actualizacion para probar que el programa nuevo arranca
        sys.exit(0)
    main()
