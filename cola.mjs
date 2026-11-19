// Habla con la cola del backend. No sabe nada de WhatsApp: recibe la funcion que envia.

export class NumeroSinWhatsapp extends Error {}

const PAUSA_ENTRE_MENSAJES_MS = 1500

function headers(apiKey) {
  return { 'X-API-Key': apiKey, 'Content-Type': 'application/json' }
}

async function pedir(url, apiKey, body) {
  const respuesta = await fetch(url, {
    method: 'POST',
    headers: headers(apiKey),
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(90_000), // Render puede tardar en despertar.
  })
  if (!respuesta.ok) throw new Error(`El backend respondio ${respuesta.status}`)
  return respuesta
}

export async function procesarCola({ backendUrl, apiKey, enviar, pausaMs = PAUSA_ENTRE_MENSAJES_MS }) {
  const base = `${backendUrl.replace(/\/+$/, '')}/api/ai/whatsapp`
  const { items } = await (await pedir(`${base}/claim`, apiKey)).json()
  let enviados = 0
  for (const mensaje of items) {
    try {
      await enviar(mensaje.phone, mensaje.body)
      await pedir(`${base}/${mensaje.id}/sent`, apiKey)
      enviados += 1
    } catch (error) {
      if (error instanceof NumeroSinWhatsapp) {
        await pedir(`${base}/${mensaje.id}/failed`, apiKey, { error: 'number is not on WhatsApp' })
      } else {
        // Sin avisar al backend: el mensaje vuelve a la cola en un minuto, hasta tres intentos.
        console.error(`No se pudo enviar el mensaje ${mensaje.id}: ${error.message}`)
      }
    }
    if (pausaMs) await new Promise((listo) => setTimeout(listo, pausaMs))
  }
  return enviados
}
