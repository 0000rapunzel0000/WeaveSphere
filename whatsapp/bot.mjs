// WhatsApp bridge for the mill assistant. Links to a WhatsApp account by QR (WhatsApp → Linked devices), sends each
// incoming text to the backend's /assistant/whatsapp and replies with its answer. The backend does all the work:
// Jev, the skills, the checks and the API calls. Demo-sized: one linked account, no queue.
//   cd whatsapp && npm install && npm start        (BOT_SECRET from backend/.env; MILL_API defaults to http://localhost:8000)
// One phone is enough: in "Message yourself" on the linked account, your own messages go to the assistant too.
// Linking: the QR is also saved as link-qr.png and opened (terminal QRs often don't scan). Or, with
// PAIR_PHONE=919876543210 npm start, use WhatsApp's "Link with phone number instead" and type the code it prints.
import makeWASocket, { DisconnectReason, fetchLatestBaileysVersion, useMultiFileAuthState } from '@whiskeysockets/baileys'
import pino from 'pino'
import QRCode from 'qrcode'
import { exec } from 'node:child_process'
import { existsSync, readFileSync, rmSync } from 'node:fs'

// BOT_SECRET comes from the environment, else from backend/.env (the backend reads the same file).
const env = Object.fromEntries((existsSync(new URL('../backend/.env', import.meta.url)) ? readFileSync(new URL('../backend/.env', import.meta.url), 'utf8') : '')
  .split('\n').map(l => l.trim().split(/=(.*)/s)).filter(([k, v]) => k && !k.startsWith('#') && v))
const API = process.env.MILL_API || 'http://localhost:8000'
const SECRET = process.env.BOT_SECRET || env.BOT_SECRET
if (!SECRET) { console.error('Set BOT_SECRET in backend/.env (any long random string) and restart the backend.'); process.exit(1) }

const AUTH = process.env.AUTH_DIR || 'auth'
const QR_PNG = process.env.QR_PNG || 'link-qr.png'
const PAIR_PHONE = (process.env.PAIR_PHONE || '').replace(/\D/g, '')
let opened = false, paired = false
const sent = new Set()  // ids of our own replies, so in "Message yourself" the bot never answers itself
const digits = jid => (jid || '').split('@')[0].split(':')[0]

async function ask(phone, text) {
  const r = await fetch(API + '/assistant/whatsapp', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'x-bot-secret': SECRET }, body: JSON.stringify({ phone, text }),
  })
  const body = await r.json().catch(() => ({}))
  if (body.status === 'unlinked') console.log(`Message from ${phone}, which isn't linked. Add it to BOT_PHONES in backend/.env, e.g. BOT_PHONES=${phone}=floor`)
  return r.ok ? body.reply : `Sorry, the mill system couldn't take that (${body.detail || r.status}).`
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH)  // the linked session; delete ./auth to unlink
  const { version } = await fetchLatestBaileysVersion()
  const sock = makeWASocket({ version, auth: state, logger: pino({ level: 'silent' }) })
  sock.ev.on('creds.update', saveCreds)
  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (qr && PAIR_PHONE && !paired) {
      paired = true  // one code per start; it stays valid for a few minutes
      const code = await sock.requestPairingCode(PAIR_PHONE)
      console.log(`\nPairing code: ${code.slice(0, 4)}-${code.slice(4)}\nOn the phone with +${PAIR_PHONE}: WhatsApp → Linked devices → Link a device → ` +
                  '"Link with phone number instead", and type the code.')
    } else if (qr && !PAIR_PHONE) {
      await QRCode.toFile(QR_PNG, qr, { width: 480, margin: 3 })  // WhatsApp changes the QR every ~20 s; the file follows
      console.log(await QRCode.toString(qr, { type: 'terminal' }))
      console.log(`On the phone: WhatsApp → Linked devices → Link a device, and scan the QR in ${QR_PNG} (opened for you) or above.` +
                  '\nIf neither scans: PAIR_PHONE=<the bot account\'s number> npm start, and link with a code instead.')
      if (!opened && process.platform === 'darwin' && !process.env.NO_OPEN) { opened = true; exec(`open ${QR_PNG}`) }
    }
    if (connection === 'open') { rmSync(QR_PNG, { force: true }); console.log(`Connected as +${digits(sock.user.id)}. Messages now reach the mill assistant at ${API}.`) }
    if (connection === 'close') {
      if (lastDisconnect?.error?.output?.statusCode === DisconnectReason.loggedOut) {
        console.log('Unlinked from the phone. Delete the auth folder and start again to link.'); process.exit(1)
      }
      setTimeout(start, 2000)  // a network blip or a WhatsApp restart: reconnect
    }
  })
  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return
    for (const m of messages) {
      const jid = m.key.remoteJid || ''
      if (!jid.endsWith('@s.whatsapp.net') && !jid.endsWith('@lid')) continue          // no groups, broadcasts or status
      const self = [digits(sock.user?.id), digits(sock.user?.lid)].includes(digits(jid))
      if (sent.has(m.key.id) || (m.key.fromMe && !self)) continue                        // our replies; our chats with others
      const text = (m.message?.conversation || m.message?.extendedTextMessage?.text || '').trim()
      if (!text) continue
      // Newer WhatsApp hides numbers behind "@lid" ids; the phone number comes alongside when WhatsApp shares it.
      const phone = self ? digits(sock.user.id) : digits(jid.endsWith('@lid') ? (m.key.senderPn || m.key.remoteJidAlt || jid) : jid)
      try {
        await sock.sendPresenceUpdate('composing', jid)
        const reply = await ask(phone, text)
        const out = await sock.sendMessage(jid, { text: reply }, { quoted: m })
        if (out?.key?.id) sent.add(out.key.id)
      } catch (e) { console.error('Could not answer:', e.message) }
    }
  })
}
start()
