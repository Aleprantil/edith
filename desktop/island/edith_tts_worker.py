"""
Tiene Piper acceso con la voce già caricata: così ogni frase di EDITH parte subito.
Si avvia con il Python dell'ambiente di Piper:   <python> edith_tts_worker.py <voce.onnx> [velocità]
Protocollo (una riga per richiesta): {"text": "...", "out": "/percorso.wav"}  ->  "ok"  oppure  "err <motivo>".
All'avvio scrive "ready" quando la voce è caricata.
"""
import json
import os
import sys
import wave


def main() -> int:
    if len(sys.argv) < 2:
        print("err manca il percorso della voce", flush=True)
        return 2
    try:
        from piper import PiperVoice
        voice = PiperVoice.load(sys.argv[1])
    except Exception as e:                              # noqa: BLE001 — il motivo va all'isola, che lo mostra
        print("err " + str(e).replace("\n", " ")[:200], flush=True)
        return 1
    speed = 1.0
    try:
        speed = min(2.0, max(0.5, float(sys.argv[2]))) if len(sys.argv) > 2 else 1.0
    except ValueError:
        pass
    config = None
    try:
        from piper import SynthesisConfig
        config = SynthesisConfig(length_scale=speed)    # length_scale > 1 = più lenta
    except Exception:                                   # noqa: BLE001 — versioni vecchie: velocità di serie
        config = None
    print("ready", flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            text, out = str(req["text"])[:2000], str(req["out"])
            wf = wave.open(out, "wb")
            try:
                if hasattr(voice, "synthesize_wav"):
                    voice.synthesize_wav(text, wf, syn_config=config) if config else voice.synthesize_wav(text, wf)
                else:
                    voice.synthesize(text, wf, length_scale=speed)
            except Exception:
                # il file a metà, chiudendosi, darebbe un secondo errore che copre il motivo vero: lo chiudo in silenzio e lo tolgo
                try:
                    wf.close()
                except Exception:                       # noqa: BLE001
                    pass
                try:
                    os.unlink(out)
                except OSError:
                    pass
                raise
            wf.close()
            print("ok", flush=True)
        except Exception as e:                          # noqa: BLE001
            print("err " + str(e).replace("\n", " ")[:200], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
