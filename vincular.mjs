import makeWASocket, { DisconnectReason, fetchLatestBaileysVersion, useMultiFileAuthState } from '@whiskeysockets/baileys'
import pino from 'pino'
import qrcode from 'qrcode-terminal'

const { state, saveCreds } = await useMultiFileAuthState('auth_info')
const { version } = await fetchLatestBaileysVersion()

function conectar() {
  const socket = makeWASocket({ auth: state, version, logger: pino({ level: 'silent' }) })
  socket.ev.on('creds.update', saveCreds)
  socket.ev.on('connection.update', ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.clear()
      console.log('En el telefono: WhatsApp > Dispositivos vinculados > Vincular un dispositivo\n')
      qrcode.generate(qr, { small: true })
    }
    if (connection === 'open') {
      console.log('Vinculado. La sesion quedo guardada en auth_info.')
      process.exit(0)
    }
    if (connection === 'close') {
      const codigo = lastDisconnect?.error?.output?.statusCode
      // WhatsApp cierra la conexion justo despues de escanear y espera que se vuelva a abrir.
      if (codigo === DisconnectReason.restartRequired) return conectar()
      const motivo = codigo === DisconnectReason.loggedOut
        ? 'La sesion fue cerrada. Borra la carpeta auth_info y vuelve a intentarlo.'
        : `Se cerro la conexion (codigo ${codigo}). Vuelve a ejecutar el comando para ver un QR nuevo.`
      console.log(motivo)
      process.exit(1)
    }
  })
}

conectar()
