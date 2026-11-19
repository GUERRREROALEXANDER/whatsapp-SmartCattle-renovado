# WhatsApp de SmartCattle

Envía por WhatsApp los mensajes que el backend deja en su cola: alertas de ganado fuera de la zona y códigos de verificación.

Solo hace conexiones de salida, así que no necesita túnel ni abrir puertos. Si se apaga, los mensajes esperan en el backend: hasta 1 hora las alertas y 10 minutos los códigos.

## Uso

```powershell
npm install
node vincular.mjs                # solo la primera vez: escanea el QR
Copy-Item .env.example .env      # y completa BACKEND_URL y AI_API_KEY
npm start
```

Requiere Node 20.6 o superior, por la opción `--env-file`.

## Cuidado

- `auth_info/` da control de la cuenta de WhatsApp y `.env` tiene la clave del backend: no los compartas ni los subas al repositorio. Ya están en `.gitignore`.
- Baileys no es una API oficial y Meta puede bloquear el número. Usa un número dedicado.
- Baileys está fijado en `7.0.0-rc14`, una versión candidata. Revisa los cambios antes de actualizarla.

# Gestor de cámaras (Python)

Mantiene un proceso de FFmpeg por cámara, que lleva cada cámara RTSP a su propia ruta de MediaMTX en H.264 `yuv420p`. Retoma las cámaras guardadas al arrancar, se reconecta solo si una se cae y cierra FFmpeg al terminar, incluso si el servicio muere a la fuerza.

## Uso

Requiere FFmpeg (`winget install Gyan.FFmpeg`) y MediaMTX. Con `CAMERA_API_KEY` en `.env`:

```powershell
# Terminal 1
& "$env:LOCALAPPDATA\Programs\mediamtx\mediamtx.exe" mediamtx.local.yml

# Terminal 2
.\.venv\Scripts\python.exe -m uvicorn camara.api:app --host 127.0.0.1 --port 8095

# Terminal 3: configurar una cámara
$clave = (Select-String -Path .env -Pattern '^CAMERA_API_KEY=(.+)$').Matches.Groups[1].Value
Invoke-RestMethod -Method Post http://127.0.0.1:8095/api/configurar-camara -Headers @{ 'X-API-Key' = $clave } -ContentType 'application/json' -Body '{"url":"rtsp://USUARIO:CLAVE@IP:554/cam/realmonitor?channel=1&subtype=1"}'
Invoke-RestMethod http://127.0.0.1:8095/api/camaras -Headers @{ 'X-API-Key' = $clave }
```

## API

Todas las rutas exigen la cabecera `X-API-Key`.

| Método | Ruta | Qué hace |
|---|---|---|
| `POST` | `/api/configurar-camara` | Crea la cámara o cambia su URL, y la guarda en `config.json`. Cuerpo: `url` y, opcional, `camara_id` (por defecto `live`) |
| `GET` | `/api/camaras` | Lista las cámaras con su estado |
| `GET` | `/api/estado-camara?camara_id=live` | Estado de una cámara |
| `DELETE` | `/api/camaras/{camara_id}` | Cierra su FFmpeg y la borra de `config.json` |

Cada cámara se devuelve así, sin contraseña:

```json
{
  "camara_id": "live",
  "estado": "transmitiendo",
  "camara": "rtsp://***@192.168.1.50:554/cam/realmonitor?channel=1&subtype=1",
  "reintentos": 0,
  "cuadros": 1520,
  "ultimo_error": null,
  "rtsp_url": "rtsp://127.0.0.1:8554/live",
  "hls_url": "http://127.0.0.1:8888/live/index.m3u8"
}
```

- `estado`: `sin_configurar`, `conectando`, `transmitiendo`, `reconectando` o `detenido`.
- `camara_id`: letras, números, `-` y `_`, hasta 64 caracteres. Es también su ruta en MediaMTX. Para que lleguen alertas por WhatsApp debe ser el ID que el backend le dio a la cámara al guardarla.
- Errores: `401` sin clave, `404` cámara inexistente, `409` límite de cámaras (`MAX_CAMARAS`, 8 por defecto), `422` datos no válidos.
- Repetir la misma URL no corta el video. Si la cámara estaba esperando para reintentar, reintenta de inmediato.
- Si la contraseña de la cámara tiene símbolos como `@` o `/`, codifícalos en la URL (`@` es `%40`).

## Reconexión

Una cámara se reintenta sin límite, esperando de 1 a 30 s entre intentos. Un corte de red sin cierre de conexión deja de figurar como `transmitiendo` a los 5 s y reinicia FFmpeg a los 20 s. Cada cámara se reintenta por su cuenta.

## Consumirlo desde la IA

El servicio de IA lee las cámaras de este gestor por sí solo si tiene `SMARTCATTLE_GESTOR_KEY` con el valor de `CAMERA_API_KEY`. Cada cámara empieza a analizarse en 15 s como máximo, con el mismo ID. OpenCV entrega los cuadros en BGR. Las direcciones `rtsp_url` solo existen en este PC.

## Consumirlo desde un frontend

Para que un frontend en otro origen pueda llamar a la API, añade su origen en `.env`, y en `hlsAllowOrigins` de `mediamtx.local.yml` para el video (ya incluye `http://localhost:5173`):

```
CORS_ORIGINS=["http://localhost:5173"]
```

`hls_url` se reproduce con `hls.js` en Chrome y Firefox. La clave `X-API-Key` da control sobre las cámaras y todo lo que llega al navegador es público: úsala directamente solo en un panel local de este PC. Un frontend publicado debe hablar con el backend de SmartCattle.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest camara/tests -q
```

## Cuidado

- `config.json` y `.env` guardan secretos en texto plano: no los subas al repositorio. Ya están en `.gitignore`.
- Mientras corre, la URL con la contraseña se ve en la línea de comandos de FFmpeg para otros programas de este PC.
- Escucha solo en `127.0.0.1`. No lo expongas a la red ni por un túnel: quien tenga la clave decide a qué dirección se conecta este PC.
- `mediamtx.local.yml` no pide contraseña: sirve solo para este PC. Para acceso remoto se usa `deploy/mediamtx.yml` del backend.
