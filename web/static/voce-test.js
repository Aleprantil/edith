/* Prova voce: dice se il browser ha il riconoscimento vocale e se il microfono riparte dopo che il telefono ha parlato. Non salva e non invia nulla. */
(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const out = $('#log'), t0 = performance.now();
  const log = (msg) => { out.textContent += `[${((performance.now() - t0) / 1000).toFixed(1).padStart(5)} s] ${msg}\n`; out.scrollTop = out.scrollHeight; };
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = 'speechSynthesis' in window ? window.speechSynthesis : null;
  const standalone = window.navigator.standalone === true || (window.matchMedia && matchMedia('(display-mode: standalone)').matches);

  const rows = [
    ['Aperta come app dalla Home', standalone ? 'SÌ: qui il riconoscimento di solito non funziona' : 'no, scheda normale del browser'],
    ['Riconoscimento vocale', SR ? 'disponibile' : 'NON disponibile'],
    ['Voce di risposta', synth ? 'disponibile' : 'NON disponibile'],
    ['Microfono', navigator.mediaDevices && navigator.mediaDevices.getUserMedia ? 'disponibile' : 'NON disponibile'],
    ['Browser', (navigator.userAgent.replace(/\(.*?\)/, '') || '').trim().slice(0, 100)],
  ];
  for (const [k, v] of rows) { const d = document.createElement('div'); const b = document.createElement('b'); b.textContent = k + ': '; d.append(b, document.createTextNode(v)); $('#env').append(d); }
  log('Ambiente: ' + rows.slice(0, 4).map((r) => r[0] + ' = ' + r[1]).join(' | '));

  // Un solo ascolto, con un oggetto NUOVO ogni volta (come farà l'app). Si chiude da solo dopo `limit` ms.
  function listen(label, limit = 9000) {
    return new Promise((resolve) => {
      if (!SR) { resolve({ ok: false, error: 'riconoscimento non disponibile', ms: 0 }); return; }
      const r = new SR(), t = performance.now();
      r.lang = 'it-IT'; r.interimResults = false; r.continuous = false; r.maxAlternatives = 1;
      let done = false, heard = false, timer = 0;
      const finish = (res) => { if (done) return; done = true; clearTimeout(timer); try { r.abort(); } catch (e) { /* già chiuso */ } resolve({ ...res, ms: Math.round(performance.now() - t) }); };
      timer = setTimeout(() => finish({ ok: false, error: `nessun risultato in ${limit / 1000} s (microfono bloccato?)` }), limit);
      r.onstart = () => log(`${label}: in ascolto, parla ora`);
      r.onaudiostart = () => log(`${label}: microfono aperto`);
      r.onspeechstart = () => { heard = true; log(`${label}: sento la voce`); };
      r.onresult = (e) => finish({ ok: true, text: e.results[0][0].transcript });
      r.onerror = (e) => finish({ ok: false, error: e.error || 'errore sconosciuto' });
      r.onend = () => finish({ ok: false, error: heard ? 'finito senza testo' : 'finito senza aver sentito nulla' });
      try { r.start(); } catch (e) { finish({ ok: false, error: 'avvio: ' + e.message }); }
    });
  }

  function speak(text) {
    return new Promise((resolve) => {
      if (!synth) { resolve(false); return; }
      synth.cancel();
      const u = new SpeechSynthesisUtterance(text); u.lang = 'it-IT';
      const v = synth.getVoices().find((x) => (x.lang || '').toLowerCase().replace('_', '-').startsWith('it'));
      if (v) { u.voice = v; log('voce usata: ' + v.name); } else log('voce italiana non trovata: uso quella predefinita');
      const guard = setTimeout(() => resolve(false), 9000);
      u.onend = () => { clearTimeout(guard); resolve(true); };
      u.onerror = () => { clearTimeout(guard); resolve(false); };
      synth.speak(u);
    });
  }

  let busy = false;
  async function run(fn) { if (busy) return; busy = true; try { await fn(); } finally { busy = false; } }

  $('#t1').addEventListener('click', () => run(async () => {
    log('— PROVA 1: solo riconoscimento —');
    const r = await listen('prova 1');
    log(r.ok ? `✅ capito in ${r.ms} ms: «${r.text}»` : `❌ ${r.error} (dopo ${r.ms} ms)`);
  }));

  $('#t2').addEventListener('click', () => run(async () => {
    log('— PROVA 2: parlo, risponde a voce, riascolto —');
    const a = await listen('1° ascolto');
    if (!a.ok) { log(`❌ il primo ascolto non è riuscito: ${a.error}`); return; }
    log(`✅ 1° ascolto in ${a.ms} ms: «${a.text}»`);
    const spoke = await speak(`Ho sentito: ${a.text}. Ora dimmi qualcos'altro.`);
    log(spoke ? 'risposta a voce terminata' : '⚠️ la voce non è partita o non ha segnalato la fine');
    const b = await listen('2° ascolto');
    log(b.ok ? `✅ 2° ascolto riuscito in ${b.ms} ms: «${b.text}» → il «sì o no» a voce può funzionare` : `❌ 2° ascolto: ${b.error} → dopo la voce il microfono non riparte`);
  }));

  $('#copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(out.textContent); $('#copy').textContent = 'Copiato ✓'; } catch (e) { $('#copy').textContent = 'Non riesco a copiare: fai una schermata'; }
  });
})();
