# L'isola di EDITH (Hyprland)

Un pannello che scende da sotto la barra, al centro sotto l'orologio, in stile **personaggio**: niente chat, ti risponde lei.

- **EDITH in grande**: una sfera ambra con gli occhi e un satellite in orbita. Respira, sbatte le palpebre, segue il mouse,
  **muove la bocca mentre parla**, pensa (tre puntini) mentre lavora, si rattrista se qualcosa va storto, si addormenta se il server
  non risponde. **Toccala e ti saluta.** Si anima solo a isola aperta.
- **Il fumetto**: quando apri ti racconta la giornata («Il prossimo impegno è Robotics, tra 41 minuti. Hai un promemoria scaduto.»)
  con la **linea della giornata** sotto (fatto, adesso, prossimo). Quando chiedi, la risposta si scrive lettera per lettera
  (un clic sul fumetto la completa subito); sopra resta «Hai chiesto · …». Le conferme (Sì / Annulla, o la scelta tra più voci)
  stanno dentro il fumetto.
- **Le risposte di prima** si sfogliano con **‹ prima / dopo ›** dentro il fumetto (o Pag↑ / Pag↓).
- Sotto, i **tasti rapidi** (Oggi, Domani, Promemoria, Spese, Server) e la casella **Parla con EDITH…**

È solo un'altra finestra sul tuo EDITH: usa le stesse API della pagina (`/api/assistant`, `/api/confirm`, `/api/cards`),
passando da Tailscale. Nessuna logica duplicata, nessuna password sul PC. La cronologia resta **solo sul PC**
(`~/.local/share/edith/island-history.json`, leggibile solo da te).

Il widget di Waybar resta la «pillola»: un clic apre o chiude l'isola.

## Comandi
| Azione | Come |
|---|---|
| Apri / chiudi | **Super+E**, oppure clic sul widget di EDITH nella barra |
| Apri e scrivi | clic destro sul widget |
| Chiudi | **Esc**, la × o di nuovo Super+E |
| Parlarle | tieni premuto **🎙** (o **Super+V**), lascia per inviare |
| Voce sì / no | **🔊 / 🔇** nella testata |
| Ultima domanda | **↑** con la casella vuota |
| Risposte di prima | **‹ prima / dopo ›** nel fumetto, o **Pag↑ / Pag↓** |
| Completa subito la risposta | clic sul fumetto |
| Ricomincia (cancella la cronologia) | **Ctrl+L** o il tasto ⟲ |
| Apri la pagina completa | il tasto ↗ |

Mentre l'isola è aperta la tastiera è sua (si scrive subito, senza cliccare); appena la chiudi torna alle finestre.

## Installazione

1. I pacchetti (tutti nei repository ufficiali di Arch):
   ```bash
   sudo pacman -S --needed gtk4 gtk4-layer-shell python-gobject python-cairo
   ```
2. I file:
   ```bash
   mkdir -p ~/.local/share/edith
   cp -r island ~/.local/share/edith/
   chmod +x ~/.local/share/edith/island/edith-island
   ```
3. Prova: `~/.local/share/edith/island/edith-island show` deve far scendere l'isola. Chiudila con Esc.

4. Hyprland. Controlla la versione con `hyprctl version`, poi aggiungi a `~/.config/hypr/hyprland.conf`:
   ```
   exec-once = ~/.local/share/edith/island/edith-island --daemon
   bind = SUPER, E, exec, ~/.local/share/edith/island/edith-island toggle
   ```
   Facoltativo, la sfocatura dietro l'isola. **Hyprland 0.53 o più recente:**
   ```
   layerrule = blur on, ignore_alpha 0.3, match:namespace edith-island
   ```
   **Hyprland più vecchio di 0.53:**
   ```
   layerrule = blur, edith-island
   layerrule = ignorealpha 0.3, edith-island
   ```
   Se la tua configurazione è in **Lua** (`hyprland.lua`), dimmelo: la sintassi è diversa.

5. Waybar: nel blocco `"custom/edith"` di `~/.config/waybar/config.jsonc` metti
   ```
   "on-click": "~/.local/share/edith/island/edith-island toggle",
   "on-click-right": "~/.local/share/edith/island/edith-island ask",
   ```
   e ricarica con `pkill -SIGUSR2 waybar`.

## Parlarle, e sentirla rispondere
- **Tieni premuto 🎙** nell'isola (o **Super+V** da qualunque finestra, vedi sotto), parla, **lascia**: EDITH trascrive
  (con Gemini, come dall'iPhone) e risponde. Mentre ascolti lei si illumina e «sente» le onde della tua voce.
- Se le hai **parlato**, risponde **anche a voce**, con **Piper sul tuo PC**: la bocca si muove a tempo con l'audio vero
  e il fumetto si scrive allo stesso ritmo. Se hai scritto, risponde solo nel fumetto (si può cambiare, vedi sotto).
- **🔊 / 🔇** nella testata: accende o spegne la voce. Chiudendo l'isola smette di ascoltare e di parlare.
- Privacy: quello che dici va al server e a Gemini per la trascrizione (come dall'iPhone); la voce di EDITH invece
  si genera sul PC, e il testo delle risposte non esce dal PC.

### Installare la voce (una volta)
```bash
sudo pacman -S --needed libpulse                     # parecord per registrare (con PipeWire di solito c'è già)
python -m venv ~/.local/share/edith/piper
~/.local/share/edith/piper/bin/pip install piper-tts
~/.local/share/edith/piper/bin/python -m piper.download_voices | grep it_IT                  # le voci italiane
~/.local/share/edith/piper/bin/python -m piper.download_voices --download-dir ~/.local/share/edith/voices it_IT-paola-medium
```
Se la voce che preferisci ha un altro nome, scaricala allo stesso modo e indicala in `edith.conf`
(`EDITH_VOICE_MODEL=~/.local/share/edith/voices/<nome>.onnx`). Poi `edith-island quit` e riaprila.

### Premi e parla da qualunque finestra (Hyprland)
```
bind  = SUPER, V, exec, ~/.local/share/edith/island/edith-island listen
bindr = SUPER, V, exec, ~/.local/share/edith/island/edith-island stop
```
`bind` scatta quando premi, `bindr` quando lasci. Se Super+V lo usi già (per esempio per gli appunti), scegli un altro tasto.
Se il rilascio non scatta, usa al suo posto `bind = SUPER, V, exec, ~/.local/share/edith/island/edith-island talk`:
una pressione per iniziare, una per finire.

## Impostazioni
In `~/.config/edith/edith.conf` (se manca usa quello del widget, `waybar.conf`):
```
EDITH_URL=https://edith.your-tailnet.ts.net:8443
EDITH_ISLAND_POSITION=center      # center, left o right (se l'orologio non è al centro)
EDITH_ISLAND_WIDTH=440            # da 340 a 720
EDITH_NICKNAME=Mario
EDITH_ISLAND_VOICE=auto           # auto: a voce quando le parli · always: sempre · off: mai
EDITH_VOICE_SPEED=1.0             # più alto = più lenta (da 0.5 a 2)
EDITH_VOICE_MODEL=~/.local/share/edith/voices/it_IT-paola-medium.onnx
# EDITH_RECORDER_CMD=…  EDITH_PLAYER_CMD=…   solo se vuoi programmi diversi da parecord/arecord e pw-play/paplay/aplay
```
Dopo una modifica: `~/.local/share/edith/island/edith-island quit` e riaprila.

## Se qualcosa non va
- **Non si apre** o **la voce non parte:** guarda `~/.cache/edith/island.log` (le righe «Voce:» dicono il motivo).
- **Compare come finestra normale invece che sotto la barra:** manca `gtk4-layer-shell` (punto 1).
- **«accesso negato»** nella striscia: il PC non è tra gli accessi ammessi a EDITH (`WEB_ALLOWED_LOGINS` sul server).
- **«non raggiungibile»:** Tailscale spento sul PC, o server giù.
- **Colori del tema GTK che «sporcano» l'isola:** lo stile dell'isola ha la precedenza, ma se vedi qualcosa di strano mandami una schermata.
