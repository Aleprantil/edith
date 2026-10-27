#!/usr/bin/env python3
"""
Dà al modulo di EDITH lo stesso aspetto dell'orologio nella tua Waybar: stesso sfondo, bordi, spazi e carattere.

    python3 waybar-stile-come-orologio.py                 usa ~/.config/waybar/style.css
    python3 waybar-stile-come-orologio.py percorso.css

Cosa fa: in ogni regola del tuo tema che nomina #clock aggiunge #custom-edith (anche :hover e simili),
toglie il vecchio stile di EDITH aggiunto in fondo (quello con lo sfondo trasparente) e lascia solo i colori di stato.
Prima salva una copia (style.css.prima-di-edith). Si può rilanciare: non duplica nulla.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

OLD_MARKER = "/* Da aggiungere in fondo a ~/.config/waybar/style.css"
OLD_END = 'tooltip label { color: #f3ede4; font-family: "Space Mono", monospace; }'
MARKER = "/* EDITH: solo i colori di stato, il resto è quello dell'orologio */"
STATE = MARKER + """
#custom-edith.alert { color: #ff5d4d; }
#custom-edith.offline,
#custom-edith.auth { color: #8d8071; }
"""
CLOCK = re.compile(r"(?<![\w-])#clock(?![\w-])")


def remove_old_block(css: str) -> str:
    """Toglie lo stile messo con la prima versione del widget (dal suo commento fino all'ultima sua riga)."""
    start = css.find(OLD_MARKER)
    if start < 0:
        return css
    end = css.find(OLD_END, start)
    if end < 0:
        return css                                       # non riconosco la fine: meglio non toccare nulla
    end += len(OLD_END)
    return css[:start].rstrip() + "\n" + css[end:].lstrip("\n")


def add_edith(css: str):
    """In ogni regola che nomina #clock aggiunge la stessa regola per #custom-edith. Ritorna (css, quante regole)."""
    out, pos, count = [], 0, 0
    for m in re.finditer(r"[^{}]*\{", css):
        chunk = m.group(0)[:-1]
        cut = chunk.rfind("*/")                          # i commenti prima del selettore restano dove sono
        prefix, sel = (chunk[: cut + 2], chunk[cut + 2:]) if cut >= 0 else ("", chunk)
        parts = [p.strip() for p in sel.split(",")]
        if "@" in sel or any("#custom-edith" in p for p in parts):
            continue
        extra = [CLOCK.sub("#custom-edith", p) for p in parts if CLOCK.search(p)]
        if not extra:
            continue
        count += 1
        out.append(css[pos:m.start()] + prefix + sel.rstrip() + ",\n" + ",\n".join(extra) + " {")
        pos = m.end()
    out.append(css[pos:])
    return "".join(out), count


def transform(css: str):
    css = remove_old_block(css)
    css, count = add_edith(css)
    if MARKER not in css:
        css = css.rstrip() + "\n\n" + STATE
    return css, count


def main(argv) -> int:
    path = Path(argv[1]).expanduser() if len(argv) > 1 else Path.home() / ".config" / "waybar" / "style.css"
    if not path.exists():
        print(f"Non trovo {path}.")
        return 1
    css = path.read_text(encoding="utf-8")
    new, count = transform(css)
    if count == 0 and "#custom-edith," not in css and ",\n#custom-edith" not in css:
        print("Nel tuo tema non trovo regole per #clock: non cambio nulla.\n"
              "Mandami l'uscita di:  grep -n -B2 -A8 -i clock " + str(path))
        return 2
    if new == css:
        print("Già a posto: niente da cambiare.")
        return 0
    backup = path.with_name(path.name + ".prima-di-edith")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(new, encoding="utf-8")
    print(f"Fatto: EDITH ha lo stile dell'orologio ({count} regol{'a' if count == 1 else 'e'}). Copia di prima: {backup}")
    if shutil.which("pkill"):
        subprocess.run(["pkill", "-SIGUSR2", "waybar"], check=False)
        print("Waybar ricaricata.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
