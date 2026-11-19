"""Finge ser FFmpeg en las pruebas. El modo y lo que recibio se comunican por variables de entorno y un archivo."""

import os
import sys
import threading
import time


def cerrar_con_q():
    if sys.stdin.read(1) == "q":
        os._exit(0)


threading.Thread(target=cerrar_con_q, daemon=True).start()
modo = os.environ.get("FALSO_MODO", "ok")
registro = os.environ.get("FALSO_REGISTRO")
if registro:
    with open(registro, "a", encoding="utf-8") as archivo:
        archivo.write(sys.argv[sys.argv.index("-i") + 1] + "\n")

if modo == "falla":
    print("Connection to rtsp://admin:secreto@10.0.0.9/live failed", file=sys.stderr, flush=True)
    sys.exit(1)

if modo == "cuelga":
    # Sigue escribiendo progreso con el mismo numero, como FFmpeg cuando la camara deja de enviar.
    while True:
        print("frame=7", flush=True)
        time.sleep(0.05)

cuadro = 0
while True:
    cuadro += 1
    print(f"frame={cuadro}", flush=True)
    time.sleep(0.02)
