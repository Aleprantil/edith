# EDITH nella barra di Hyprland

Un modulo per **Waybar**. Di serie è **compatto**: il pallino, «in corso» o i minuti se manca meno di un'ora,
e i promemoria scaduti. Per avere anche il titolo dell'impegno metti `EDITH_BAR_STYLE=full` in `~/.config/edith/waybar.conf`.

Mostra, nella barra:

- un pallino di stato: **verde** tutto bene, **rosso** il server ha un avviso, **grigio** EDITH non raggiungibile;
- il **prossimo impegno** con il conto alla rovescia («tra 40 min Robotics», «in corso…», «domani 08:30…»);
- i **promemoria scaduti** (☑ 2), se ce ne sono.

Passando sopra col mouse compare il riepilogo: meteo a tre giorni, alba e tramonto, impegni di oggi, i primi promemoria,
lo stato del server e la frase del giorno.

- **Clic:** apre o chiude l'**isola di EDITH** (cartella `island/`); senza l'isola, apre EDITH nel browser.
- **Clic destro:** l'isola pronta per scrivere; senza l'isola, una riga per scrivere e la risposta in una notifica.
- **Clic centrale:** un'altra frase.

Legge le stesse informazioni della pagina, passando da Tailscale: il server ti riconosce dal tuo accesso Tailscale,
quindi **non serve nessuna password** sul PC. Solo Python standard, nessuna dipendenza da installare.

## Installazione

```bash
mkdir -p ~/.config/waybar/scripts
cp edith-waybar.py ~/.config/waybar/scripts/
chmod +x ~/.config/waybar/scripts/edith-waybar.py
~/.config/waybar/scripts/edith-waybar.py        # prova: deve stampare una riga JSON con "text", "tooltip", "class"
```

Poi:

1. in `~/.config/waybar/config.jsonc` aggiungi `"custom/edith"` a `modules-center` (o dove preferisci) e copia il blocco di `waybar-modulo.jsonc`;
2. lo stile: `python3 waybar-stile-come-orologio.py` dà a EDITH lo stesso aspetto dell'orologio del tuo tema
   (sfondo, bordi, spazi). Fa una copia di sicurezza e si può rilanciare. In alternativa copia a mano `waybar-stile.css`;
3. ricarica Waybar: `pkill -SIGUSR2 waybar` (oppure chiudilo e riaprilo).

Facoltativo, una scorciatoia per chiedere a EDITH da qualunque finestra (in `~/.config/hypr/hyprland.conf`):

```
bind = SUPER, E, exec, ~/.config/waybar/scripts/edith-waybar.py --ask
```

Per scrivere la domanda usa il primo lanciatore che trova: `fuzzel`, `wofi`, `rofi` o `tofi`. Per la risposta serve `notify-send`
(di solito c'è, con mako, dunst o swaync).

## Impostazioni

Di serie usa `https://edith.your-tailnet.ts.net:8443`. Per cambiarlo, crea `~/.config/edith/waybar.conf`:

```
EDITH_URL=https://il-tuo-server.tailnet.ts.net:8443
EDITH_SIGNAL=8
```

`EDITH_SIGNAL` deve essere uguale al valore di `"signal"` nel modulo: serve ad aggiornare la barra subito dopo una domanda.

## Se qualcosa non va

- **«EDITH offline»**: Tailscale è spento sul PC, oppure il server non risponde. Se il server è giù da poco, la barra mostra gli
  ultimi dati salvati (pallino grigio) per qualche ora.
- **«EDITH: accesso negato»**: il PC è collegato a Tailscale con un account diverso dal tuo. Usa lo stesso account, oppure aggiungi
  quel login a `WEB_ALLOWED_LOGINS` nel `.env` del server.
- Le **conferme** (cancellare o spostare un evento) non si fanno dalla notifica: apri EDITH con un clic e premi Sì o No.
- I dati della barra si aggiornano ogni minuto. Lo script non resta mai appeso più di 4 secondi.
