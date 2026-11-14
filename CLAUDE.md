# Recordatorio para Claude: cada vez que subas la version de Gax

Haz TODO esto tu mismo, sin que te lo pidan:

1. Sube `GAX_V` en el/los html que cambien (`win.html` y/o `apk.html`) y copia `win.html` a `windows/static/index.html`.
2. **Log nuevo:** crea `novedades/v<N>.txt` (N = nuevo `GAX_V` de `win.html`) con SOLO lo que trae esa version
   (una linea por novedad, en espanol simple, sin repetir nada de versiones anteriores).
3. **Borra el log viejo:** elimina el `novedades/v<N-1>.txt` anterior. En `novedades/` solo debe quedar el de la version actual.
4. Corre `python tools/make_version.py` (falla si falta el log nuevo o si queda uno viejo) y deja `version.json` actualizado.
5. `novedades_apk.txt` (log del celular) solo se toca si el usuario cambia algo del celular: mismo criterio, reemplazalo
   con SOLO lo nuevo del APK. Si el usuario dice que el celular quede intacto, no lo toques.
6. Entrega el repo completo en un zip.
