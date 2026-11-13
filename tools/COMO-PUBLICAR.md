# Como publicar una version nueva

## Solo cambios de pantalla (win.html / apk.html)
1. Edita `win.html` y `apk.html` y sube `GAX_V` (el MISMO numero en los dos).
2. Escribe en `novedades.txt` que trae la version (una linea por novedad). Eso es lo que la gente ve en la
   ventana "Hay una versión nueva" cuando abre Gax.
3. Corre `python tools/make_version.py` y sube todo (html + novedades.txt + version.json) en el mismo commit a `main`.
   Al abrir Gax (PC o celular) aparece la ventana con las novedades y cada persona elige si instalar o no.
   Copia tambien `win.html` a `windows/static/index.html` (son el mismo archivo).

## Cambios del programa de Windows (app.py)
1. Cambia lo que necesites en `windows/app.py` y sube `NATIVE_V` (ej. de 1 a 2).
2. Si la pantalla nueva USA algo nuevo de app.py, escribe en `native_min.txt` ese mismo numero (ej. 2).
   Asi las PCs con el .exe viejo no aplican la pantalla nueva hasta actualizar el programa.
3. Sube los cambios a `main` y crea la release:
   `git tag v2` y `git push origin v2`  (o en GitHub: Actions > Release > Run workflow).
4. GitHub compila Gax.exe, lo publica en Releases y actualiza `version.json` solo.
   Las PCs con Gax.exe lo descargan y lo instalan al reabrir.

## Celular (APK)
- La logica de la app del celular vive en `apk.html`, asi que se actualiza sola igual que arriba.
- Un APK nuevo solo hace falta si cambias la parte nativa (permisos, icono). Ponlo en la carpeta `apk/` antes de crear
  la etiqueta y se adjunta a la release. Para instalarlo/actualizarlo en el celular sin hacerlo a mano usa la app Obtainium
  apuntando a `https://github.com/TU_USUARIO/TU_REPO` (avisa de cada release y la instala con un toque).
  El APK debe ir firmado con la MISMA llave que el instalado, si no Android lo rechaza.

## Archivos ocultos (los que empiezan con punto)
`.github`, `.gitattributes` y `.gitignore` son carpetas/archivos ocultos. Al subir a GitHub desde la web, actívalos en tu explorador
(Windows: pestaña Ver > Elementos ocultos; Mac: Cmd+Shift+punto) para que se suban también.
`.gitattributes` evita que Git cambie los saltos de línea; así el SHA-256 de `version.json` siempre coincide con los archivos.

## Cómo se actualiza Gax.exe (sin carpetas extra)
Al tocar «Instalar ahora» sale una barra de descarga; el programa nuevo se baja, se prueba (--selftest), reemplaza al viejo y se
reabre solo. No queda carpeta «actualizacion» ni Gax.exe.bak. Si cambias app.py, sube `NATIVE_V` y crea la etiqueta (git tag vN).
