/* =====================================================================
   Control Center — cliente do iPad
   Compatível com Safari 15 (iPadOS 15): ES2017, sem módulos, sem build,
   sem optional chaining (?.) nem nullish (??) por segurança.
   ===================================================================== */
(function () {
  'use strict';

  // ---------------------------------------------------------------- utils
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  // Cache de texto: só toca no DOM quando o valor muda (reduz reflow no A8X).
  var textCache = new WeakMap();
  function setText(el, txt) {
    if (!el) return;
    txt = String(txt);
    if (textCache.get(el) === txt) return;
    textCache.set(el, txt);
    el.textContent = txt;
  }
  function setTransform(el, t) { el.style.webkitTransform = t; el.style.transform = t; }
  function fmtRate(bps) {
    if (bps == null) return '—';
    if (bps >= 1048576) return (bps / 1048576).toFixed(1) + ' MB/s';
    if (bps >= 1024) return Math.round(bps / 1024) + ' KB/s';
    return Math.round(bps) + ' B/s';
  }
  function fmtTime(s) {
    s = Math.max(0, Math.round(s || 0));
    var m = Math.floor(s / 60), r = s % 60;
    if (m >= 60) return Math.floor(m / 60) + ':' + ('0' + (m % 60)).slice(-2) + ':' + ('0' + r).slice(-2);
    return m + ':' + ('0' + r).slice(-2);
  }
  function fmtUptime(s) {
    var d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return (d ? d + 'd ' : '') + h + 'h ' + m + 'min';
  }
  function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }

  var toastTimer = null;
  function toast(msg, isErr) {
    var t = $('#toast');
    t.textContent = msg;
    t.className = 'show' + (isErr ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = ''; }, isErr ? 3500 : 1800);
  }

  // ---------------------------------------------------------------- token
  function readToken() {
    var m = /[?&]token=([^&#]+)/.exec(location.search);
    if (m) { var t = decodeURIComponent(m[1]); store('cc_token', t); return t; }
    return store('cc_token') || '';
  }
  var token = readToken();

  function showTokenModal(msg) {
    $('#overlay').classList.add('hidden');
    $('#token-modal').classList.remove('hidden');
    if (msg) toast(msg, true);
  }
  $('#token-save').addEventListener('click', function () {
    var v = $('#token-input').value.trim();
    if (!v) return;
    token = v; store('cc_token', v);
    $('#token-modal').classList.add('hidden');
    hardReconnect();
  });

  // ---------------------------------------------------------------- WebSocket
  var ws = null, backoff = 500, reconnectTimer = null, lastMsgAt = 0, reqId = 0;
  var pending = {};

  // Token é opcional: só é enviado se existir (servidor com "require_token": true).
  function tokenQuery() { return token ? 'token=' + encodeURIComponent(token) : ''; }
  function wsUrl() {
    return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws?' + tokenQuery();
  }
  function setOverlay(show, msg) {
    $('#overlay').classList.toggle('hidden', !show);
    if (msg) setText($('#overlay-msg'), msg);
  }
  function setConn(ok) { $('#conn-dot').classList.toggle('ok', ok); }

  function connect() {
    clearTimeout(reconnectTimer); reconnectTimer = null;
    var sock;
    try { sock = new WebSocket(wsUrl()); } catch (e) { scheduleReconnect(); return; }
    ws = sock;
    sock.onopen = function () {
      backoff = 500; lastMsgAt = Date.now();
      setConn(true); setOverlay(false); ping();
      setTimeout(function () { diag('ws-open+1s'); }, 1000);
    };
    sock.onmessage = function (ev) {
      if (sock !== ws) return;
      lastMsgAt = Date.now();
      var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      onMessage(m);
    };
    sock.onclose = function (ev) {
      if (sock !== ws) return;   // socket antigo descartado pelo watchdog
      ws = null; setConn(false); failPending();
      if (window.__ccReport) window.__ccReport('ws-close', { code: ev.code, reason: ev.reason, clean: ev.wasClean });
      if (ev.code === 4401) { showTokenModal('Token inválido'); return; }
      setOverlay(true, 'Reconectando ao PC…');
      scheduleReconnect();
    };
    sock.onerror = function () { try { sock.close(); } catch (e) { /* ignore */ } };
  }
  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 5000);
  }
  // Descarta o socket atual sem esperar o onclose (que pode demorar minutos
  // num socket "zumbi" depois que o Wi-Fi do iPad dormiu) e conecta de novo.
  function hardReconnect() {
    var old = ws; ws = null;
    if (old) { try { old.onclose = null; old.close(); } catch (e) { /* ignore */ } }
    setConn(false); failPending(); backoff = 500;
    connect();
  }
  function failPending() {
    Object.keys(pending).forEach(function (id) {
      clearTimeout(pending[id].t); pending[id].reject(new Error('Conexão perdida'));
    });
    pending = {};
  }
  // Watchdog: o servidor manda estado a cada 1 s; 6 s de silêncio = conexão morta.
  setInterval(function () {
    if (ws && ws.readyState === 1 && Date.now() - lastMsgAt > 6000) hardReconnect();
  }, 2000);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && (!ws || Date.now() - lastMsgAt > 3000)) hardReconnect();
  });
  window.addEventListener('pageshow', function (e) { if (e.persisted) hardReconnect(); });
  window.addEventListener('online', function () { hardReconnect(); });

  function send(action, value) {
    return new Promise(function (resolve, reject) {
      if (!ws || ws.readyState !== 1) { reject(new Error('Sem conexão com o PC')); return; }
      var id = ++reqId;
      pending[id] = {
        resolve: resolve, reject: reject,
        t: setTimeout(function () { delete pending[id]; reject(new Error('PC não respondeu')); }, 8000)
      };
      ws.send(JSON.stringify({ t: 'cmd', a: action, v: value === undefined ? null : value, id: id }));
    });
  }
  var lastQuietErr = 0;
  function sendQuiet(action, value) {
    send(action, value).catch(function (e) {
      if (Date.now() - lastQuietErr > 3000) { lastQuietErr = Date.now(); toast(e.message, true); }
    });
  }

  function onMessage(m) {
    if (m.t === 'ack') {
      var p = pending[m.id];
      if (!p) return;
      delete pending[m.id]; clearTimeout(p.t);
      if (m.ok) p.resolve(m.r); else p.reject(new Error(m.err || 'erro'));
    } else if (m.t === 'state') {
      renderState(m);
    } else if (m.t === 'config') {
      applyConfig(m);
    } else if (m.t === 'notify') {
      onNotify(m);
    } else if (m.t === 'notify_list') {
      notifs = m.items || []; renderNotifs();
    } else if (m.t === 'event') {
      if (m.name === 'switch_tab' && m.tab) switchTab(m.tab);
    }
  }

  // Latência (RTT) a cada 3 s
  function ping() {
    var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
    send('ping', t0).then(function () {
      var t1 = (window.performance && performance.now) ? performance.now() : Date.now();
      setText($('#latency'), Math.round(t1 - t0) + ' ms');
    }).catch(function () { setText($('#latency'), '-- ms'); });
  }
  setInterval(function () { if (ws && ws.readyState === 1) ping(); }, 3000);

  // ---------------------------------------------------------------- sliders táteis
  function makeSlider(el, opts) {
    var fill = el.querySelector('.fill');
    var vertical = !!opts.vertical;
    var throttleMs = opts.throttle || 50;
    var s = { el: el, value: -1, dragging: false, lockUntil: 0 };
    var lastSent = 0, timer = null;

    function draw() { setTransform(fill, (vertical ? 'scaleY(' : 'scaleX(') + s.value + ')'); }
    s.set = function (v, fromServer) {
      if (fromServer && (s.dragging || Date.now() < s.lockUntil)) return;
      v = clamp(+v || 0, 0, 1);
      if (Math.abs(v - s.value) < 0.002) return;
      s.value = v; draw();
    };
    function emit() { lastSent = Date.now(); if (opts.onInput) opts.onInput(s.value); }
    function schedule() {
      var dt = Date.now() - lastSent;
      if (dt >= throttleMs) { clearTimeout(timer); timer = null; emit(); }
      else if (!timer) timer = setTimeout(function () { timer = null; emit(); }, throttleMs - dt);
    }
    function fromEvent(e) {
      var r = el.getBoundingClientRect();
      var v = vertical ? 1 - (e.clientY - r.top) / r.height : (e.clientX - r.left) / r.width;
      v = clamp(v, 0, 1);
      if (v < 0.02) v = 0; if (v > 0.98) v = 1;   // "ímã" nas pontas
      s.value = v; draw();
      if (opts.onLocal) opts.onLocal(v);
      schedule();
    }
    el.addEventListener('pointerdown', function (e) {
      e.preventDefault(); e.stopPropagation();
      s.dragging = true; el.classList.add('dragging');
      try { el.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      fromEvent(e);
    });
    el.addEventListener('pointermove', function (e) { if (s.dragging) fromEvent(e); });
    function end() {
      if (!s.dragging) return;
      s.dragging = false; el.classList.remove('dragging');
      s.lockUntil = Date.now() + 900;           // ignora eco do servidor por um instante
      clearTimeout(timer); timer = null; emit(); // envia o valor final
    }
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    el.addEventListener('lostpointercapture', end);
    return s;
  }

  // ---------------------------------------------------------------- botões (tap / hold)
  var HOLD_MS = 1200;
  var press = null; // {btn, x, y, timer, fired}

  function clearPress() {
    if (!press) return;
    clearTimeout(press.timer);
    press.btn.classList.remove('pressed', 'holding');
    press = null;
  }
  document.addEventListener('pointerdown', function (e) {
    var btn = e.target.closest ? e.target.closest('.btn, .tab') : null;
    if (!btn) return;
    clearPress();
    press = { btn: btn, x: e.clientX, y: e.clientY, timer: null, fired: false };
    btn.classList.add('pressed');
    if (btn.classList.contains('hold')) {
      btn.classList.add('holding');
      var p = press;
      p.timer = setTimeout(function () {
        if (press !== p) return;
        p.fired = true;
        fire(btn);
        clearPress();
      }, HOLD_MS);
    }
  });
  document.addEventListener('pointermove', function (e) {
    if (press && (Math.abs(e.clientX - press.x) > 30 || Math.abs(e.clientY - press.y) > 30)) clearPress();
  });
  document.addEventListener('pointerup', function () {
    if (!press) return;
    var p = press;
    if (p.btn.classList.contains('hold')) {
      if (!p.fired) toast('Segure para confirmar');
      clearPress();
      return;
    }
    clearPress();
    fire(p.btn);
  });
  document.addEventListener('pointercancel', clearPress);

  // Bloqueia gestos nativos (zoom por pinça, rubber-band).
  document.addEventListener('gesturestart', function (e) { e.preventDefault(); });
  document.addEventListener('touchmove', function (e) { e.preventDefault(); }, { passive: false });
  document.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  function payloadFor(btn) {
    var d = btn.dataset, v = null;
    if (d.key) v = { key: d.key };
    else if (d.v !== undefined) v = d.v;
    if (btn.classList.contains('hold')) {
      if (v === null) v = { confirm: true };
      else if (typeof v === 'string') v = { id: v, confirm: true };
      else v.confirm = true;
    }
    return v;
  }

  var clickHandlers = {};   // por id do elemento
  var ackMessages = {
    'win.screenshot': 'Screenshot salva 📸',
    'power.lock': 'Bloqueando…', 'power.sleep': 'Suspendendo…',
    'power.restart': 'Reiniciando…', 'power.shutdown': 'Desligando…', 'power.explorer': 'Reiniciando Explorer…'
  };

  // Diagnóstico de "pulos" de layout: mede os blocos antes e 800 ms depois de
  // cada toque e reporta ao servidor só o que mudou mais de 2 px.
  function layoutKey(el) {
    var k = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
      (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).slice(0, 3).join('.') : '');
    if (el.dataset && (el.dataset.action || el.dataset.tab || el.dataset.k)) k += '[' + (el.dataset.action || el.dataset.tab || el.dataset.k) + ']';
    return k;
  }
  function layoutSnapshot() {
    var els = $$('#topbar, #telemetry, #pages, #tabbar, .page.active, .page.active > *, .page.active > * > *, ' +
      '.page.active .btn, .page.active .slider, .strip');
    var snap = {};
    els.forEach(function (el, i) {
      var r = el.getBoundingClientRect();
      snap[i + ':' + layoutKey(el)] = [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
    });
    return snap;
  }
  function trackShift(label) {
    if (!window.__ccReport) return;
    var before = layoutSnapshot();
    var rowsBefore = getComputedStyle($('#app')).gridTemplateRows;
    setTimeout(function () {
      var after = layoutSnapshot(), diffs = [];
      Object.keys(after).forEach(function (k) {
        var a = before[k], b = after[k];
        if (!a) { diffs.push({ el: k, novo: b }); return; }
        for (var i = 0; i < 4; i++) {
          if (Math.abs(a[i] - b[i]) > 2) { diffs.push({ el: k, antes: a, depois: b }); return; }
        }
      });
      Object.keys(before).forEach(function (k) { if (!after[k]) diffs.push({ el: k, removido: before[k] }); });
      if (diffs.length) {
        window.__ccReport('click-shift', { action: label, rows: [rowsBefore, getComputedStyle($('#app')).gridTemplateRows],
          n: diffs.length, diffs: diffs.slice(0, 40) });
      }
    }, 800);
  }

  function fire(btn) {
    if (!btn.classList.contains('tab')) trackShift(btn.dataset.action || btn.id || layoutKey(btn));
    if (btn.classList.contains('tab')) { switchTab(btn.dataset.tab); return; }
    if (btn.id && clickHandlers[btn.id]) { clickHandlers[btn.id](btn); return; }
    if (btn.dataset.lpage !== undefined) { renderLauncher(+btn.dataset.lpage); return; }
    var action = btn.dataset.action;
    if (!action) return;
    send(action, payloadFor(btn)).then(function () {
      btn.classList.add('flash');
      setTimeout(function () { btn.classList.remove('flash'); }, 260);
      if (ackMessages[action]) toast(ackMessages[action]);
    }).catch(function (e) {
      btn.classList.add('fail');
      setTimeout(function () { btn.classList.remove('fail'); }, 400);
      toast(e.message, true);
    });
  }

  // ---------------------------------------------------------------- abas
  var currentTab = 'media';
  function switchTab(name) {
    if (!$('.page[data-page="' + name + '"]')) name = 'media';
    currentTab = name;
    $$('.tab').forEach(function (t) { t.classList.toggle('active', t.dataset.tab === name); });
    $$('.page').forEach(function (p) { p.classList.toggle('active', p.dataset.page === name); });
    store('cc_tab', name);
    if (name === 'system' && !monitorsLoaded) loadMonitors();
    if (name === 'media' || name === 'mixer') { lastDevSig = ''; lastMixSig = ''; if (lastState) renderAudio(lastState.audio); }
  }

  // ---------------------------------------------------------------- telemetria
  var ARC = 188.5;
  var gauges = {};
  $$('.gauge-card').forEach(function (c) {
    gauges[c.dataset.gauge] = { card: c, arc: $('.g-value', c), num: $('.g-num', c), last: -1 };
  });
  function setGauge(name, value, sub, subEl) {
    var g = gauges[name];
    g.card.classList.toggle('na', value == null);
    if (value == null) {
      setText(g.num, '—');
      g.arc.style.strokeDashoffset = ARC;
    } else {
      var v = Math.round(value);
      setText(g.num, v);
      if (v !== g.last) {
        g.last = v;
        g.arc.style.strokeDashoffset = ARC * (1 - clamp(v, 0, 100) / 100);
        g.card.classList.toggle('warm', v >= 70 && v < 90);
        g.card.classList.toggle('hot', v >= 90);
      }
    }
    setText(subEl, sub);
  }
  function setTemp(barId, valId, t) {
    setText($(valId), t == null ? '—' : Math.round(t) + '°C');
    setTransform($(barId), 'scaleX(' + (t == null ? 0 : clamp((t - 30) / 70, 0, 1)) + ')');
  }

  // Sparkline de rede (canvas, 60 amostras)
  var spark = $('#net-spark'), sctx = spark.getContext('2d');
  var netDown = [], netUp = [];
  function sizeSpark() {
    var dpr = window.devicePixelRatio || 1;
    spark.width = Math.round(spark.clientWidth * dpr);
    spark.height = Math.round(spark.clientHeight * dpr);
    drawSpark();
  }
  function drawSpark() {
    var w = spark.width, h = spark.height;
    sctx.clearRect(0, 0, w, h);
    var max = 1;
    for (var i = 0; i < netDown.length; i++) max = Math.max(max, netDown[i], netUp[i]);
    function line(arr, color, fillColor) {
      if (arr.length < 2) return;
      var step = w / 59;
      sctx.beginPath();
      for (var j = 0; j < arr.length; j++) {
        var x = w - (arr.length - 1 - j) * step, y = h - 2 - (arr[j] / max) * (h - 6);
        if (j === 0) sctx.moveTo(x, y); else sctx.lineTo(x, y);
      }
      sctx.strokeStyle = color; sctx.lineWidth = 2 * (window.devicePixelRatio || 1); sctx.stroke();
      sctx.lineTo(w, h); sctx.lineTo(w - (arr.length - 1) * step, h); sctx.closePath();
      sctx.fillStyle = fillColor; sctx.fill();
    }
    line(netDown, '#38bdf8', 'rgba(56,189,248,0.12)');
    line(netUp, '#a78bfa', 'rgba(167,139,250,0.10)');
  }
  window.addEventListener('resize', function () { setTimeout(sizeSpark, 200); });

  function renderSys(s) {
    if (!s) return;
    setGauge('cpu', s.cpu, s.cpu_power != null ? Math.round(s.cpu_power) + ' W' : 'uso total', $('#cpu-sub'));
    var vram = (s.vram_used != null && s.vram_total) ? (s.vram_used / 1024).toFixed(1) + ' / ' + Math.round(s.vram_total / 1024) + ' GB'
      : (s.lhm ? '' : 'requer LHM');
    setGauge('gpu', s.gpu, vram, $('#gpu-sub'));
    setGauge('ram', s.ram, s.ram_used + ' / ' + s.ram_total + ' GB', $('#ram-sub'));
    setTemp('#cpu-temp-bar', '#cpu-temp', s.cpu_temp);
    setTemp('#gpu-temp-bar', '#gpu-temp', s.gpu_temp);
    setTemp('#hot-temp-bar', '#hot-temp', s.gpu_hotspot);
    $('#lhm-hint').classList.toggle('hidden', !!s.lhm);
    setText($('#net-down'), fmtRate(s.net_down));
    setText($('#net-up'), fmtRate(s.net_up));
    netDown.push(s.net_down || 0); netUp.push(s.net_up || 0);
    if (netDown.length > 60) { netDown.shift(); netUp.shift(); }
    drawSpark();
    setText($('#win-title'), s.window || '');
    setText($('#sys-info'), 'Ligado há ' + fmtUptime(s.uptime || 0) + ' · ' +
      (s.lhm ? 'LHM ok' + (s.gpu_name ? ' · ' + s.gpu_name : '') : 'LibreHardwareMonitor offline'));
  }

  // ---------------------------------------------------------------- áudio
  var masterSliders = [];
  masterSliders.push(makeSlider($('#master-slider'), {
    onInput: function (v) { sendQuiet('audio.master.set', v); },
    onLocal: function (v) { setText($('#master-pct'), Math.round(v * 100) + '%'); syncMasterSliders(v); }
  }));
  function syncMasterSliders(v) {
    masterSliders.forEach(function (s) { if (!s.dragging) s.set(v); });
  }

  var lastDevSig = '', lastMixSig = '', mixPage = 0, mixSliders = {};
  function shortName(name) {
    var m = /^(.*?)\s*\((.*)\)\s*$/.exec(name || '');
    return m ? { main: m[1], sub: m[2] } : { main: name, sub: '' };
  }

  function renderAudio(a) {
    if (!a) return;
    // Volume mestre
    masterSliders.forEach(function (s) { s.set(a.master, true); s.el.classList.toggle('muted', a.muted); });
    if (!masterSliders[0].dragging) setText($('#master-pct'), Math.round(a.master * 100) + '%');
    setText($('#master-mute'), a.muted ? '🔇' : '🔊');
    $('#master-mute').classList.toggle('on', a.muted);

    // Dispositivos de saída (só redesenha se a lista/padrão mudou)
    var sig = a.device + '|' + a.devices.map(function (d) { return d.id; }).join(',');
    if (sig !== lastDevSig) {
      lastDevSig = sig;
      $('#devices').innerHTML = a.devices.slice(0, 6).map(function (d) {
        var n = shortName(d.name);
        return '<button class="btn' + (d.id === a.device ? ' on' : '') + '" data-action="audio.device.set" data-v="' + esc(d.id) + '">' +
          '<b class="ellipsis">' + (d.id === a.device ? '✓ ' : '') + esc(n.main) + '</b>' +
          (n.sub ? '<small class="ellipsis">' + esc(n.sub) + '</small>' : '') + '</button>';
      }).join('') || '<div class="muted small">Nenhuma saída ativa</div>';
    }
    renderMixer(a);
  }

  function perPage() { return window.matchMedia('(orientation: portrait)').matches ? 6 : 8; }
  function hue(str) { var h = 0; for (var i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) % 360; return h; }

  function renderMixer(a) {
    var channels = [{ key: '__master__', name: 'Mestre', volume: a.master, muted: a.muted }].concat(a.apps);
    var per = perPage();
    var pages = Math.max(1, Math.ceil(channels.length / per));
    mixPage = clamp(mixPage, 0, pages - 1);
    var slice = channels.slice(mixPage * per, mixPage * per + per);
    var sig = mixPage + '|' + slice.map(function (c) { return c.key; }).join(',');
    setText($('#mix-page'), (mixPage + 1) + '/' + pages);

    if (sig !== lastMixSig) {
      lastMixSig = sig;
      var box = $('#mixer');
      box.innerHTML = slice.map(function (c) {
        var isMaster = c.key === '__master__';
        var letter = isMaster ? '🔊' : esc((c.name || '?').charAt(0).toUpperCase());
        return '<div class="strip' + (isMaster ? ' master' : '') + '" data-k="' + esc(c.key) + '">' +
          '<div class="avatar" style="background:hsla(' + hue(c.key) + ',70%,55%,.25)">' + letter + '</div>' +
          '<div class="name ellipsis">' + esc(c.name) + '</div>' +
          '<div class="slider v"><div class="fill"></div></div>' +
          '<div class="pct">--%</div>' +
          (isMaster ? '<button class="btn" data-action="audio.master.mute">🔇</button>'
                    : '<button class="btn" data-action="audio.app.mute" data-key="' + esc(c.key) + '">🔇</button>') +
          '</div>';
      }).join('');
      // remove sliders antigos do master da lista de sincronização
      masterSliders = masterSliders.slice(0, 1);
      mixSliders = {};
      $$('.strip', box).forEach(function (strip) {
        var key = strip.dataset.k, pct = $('.pct', strip);
        var sl = makeSlider($('.slider', strip), {
          vertical: true,
          onLocal: function (v) {
            setText(pct, Math.round(v * 100) + '%');
            if (key === '__master__') { syncMasterSliders(v); setText($('#master-pct'), Math.round(v * 100) + '%'); }
          },
          onInput: function (v) {
            if (key === '__master__') sendQuiet('audio.master.set', v);
            else sendQuiet('audio.app.set', { key: key, volume: v });
          }
        });
        sl.pct = pct; sl.mute = $('.btn', strip);
        mixSliders[key] = sl;
        if (key === '__master__') masterSliders.push(sl);
      });
    }
    slice.forEach(function (c) {
      var sl = mixSliders[c.key];
      if (!sl) return;
      sl.set(c.volume, true);
      if (!sl.dragging && Date.now() >= sl.lockUntil) setText(sl.pct, Math.round(c.volume * 100) + '%');
      sl.el.classList.toggle('muted', c.muted);
      sl.mute.classList.toggle('on', c.muted);
    });
  }
  clickHandlers['mix-prev'] = function () { mixPage--; lastMixSig = ''; if (lastState) renderMixer(lastState.audio); };
  clickHandlers['mix-next'] = function () { mixPage++; lastMixSig = ''; if (lastState) renderMixer(lastState.audio); };

  // ---------------------------------------------------------------- mídia
  var artKey = null;
  var artImg = $('#np-art');
  artImg.onload = function () { artImg.parentNode.classList.add('has-art'); };
  artImg.onerror = function () { artImg.parentNode.classList.remove('has-art'); };

  function renderMedia(m) {
    if (!m || (!m.title && !m.artist)) {
      setText($('#np-app'), 'Nada tocando');
      setText($('#np-title'), '—'); setText($('#np-artist'), ' ');
      setText($('#np-play'), '▶');
      setTransform($('#np-bar'), 'scaleX(0)');
      setText($('#np-pos'), '0:00'); setText($('#np-dur'), '0:00');
      if (artKey !== null) { artKey = null; artImg.parentNode.classList.remove('has-art'); }
      return;
    }
    setText($('#np-app'), (m.app || 'Mídia') + (m.status === 'paused' ? ' · pausado' : ''));
    setText($('#np-title'), m.title || '—');
    setText($('#np-artist'), m.artist || ' ');
    setText($('#np-play'), m.status === 'playing' ? '⏸' : '▶');
    var dur = m.duration || 0;
    setTransform($('#np-bar'), 'scaleX(' + (dur > 0 ? clamp(m.position / dur, 0, 1) : 0) + ')');
    setText($('#np-pos'), fmtTime(m.position));
    setText($('#np-dur'), dur > 0 ? fmtTime(dur) : '--:--');
    if (m.art !== artKey) {
      artKey = m.art;
      if (m.art) artImg.src = '/media/art?k=' + m.art + (token ? '&' + tokenQuery() : '');
      else artImg.parentNode.classList.remove('has-art');
    }
  }

  // ---------------------------------------------------------------- estado
  var lastState = null;
  function renderState(st) {
    lastState = st;
    renderSys(st.sys);
    renderAudio(st.audio);
    renderMedia(st.media);
    renderLol(window.__ccLolOverride || st.lol);  // override só para depuração/simulação
    renderDiscord(st.discord);
    renderClaude(st.claude);
  }

  // ---------------------------------------------------------------- launcher
  var launcherPages = [], launcherPage = 0;
  var PER_LAUNCH_PAGE = 18;
  function renderLauncher(idx) {
    launcherPage = clamp(idx || 0, 0, Math.max(0, launcherPages.length - 1));
    store('cc_lpage', String(launcherPage));
    $('#launch-tabs').innerHTML = launcherPages.map(function (p, i) {
      return '<button class="btn' + (i === launcherPage ? ' on' : '') + '" data-lpage="' + i + '">' + esc(p.name) + '</button>';
    }).join('');
    var page = launcherPages[launcherPage];
    var btns = page ? page.buttons.slice(0, PER_LAUNCH_PAGE) : [];
    $('#launch-grid').innerHTML = btns.map(function (b) {
      var hold = b.confirm ? ' hold warn' : '';
      var style = b.color ? ' style="background:' + esc(b.color) + '"' : '';
      return '<button class="btn tile' + hold + '"' + style + ' data-action="launcher.run" data-v="' + esc(b.id) + '">' +
        '<i>' + esc(b.icon || '⬛') + '</i><span>' + esc(b.label || b.id) + '</span>' +
        (b.confirm ? '<b class="hold-fill"></b>' : '') + '</button>';
    }).join('') || '<div class="muted">Configure botões em config.json → "launcher"</div>';
  }

  // ---------------------------------------------------------------- tela (DDC/CI)
  var monitorsLoaded = false;
  function loadMonitors() {
    monitorsLoaded = true;
    $('#monitors').innerHTML = '<div class="muted small">Lendo monitores via DDC/CI…</div>';
    send('display.list').then(function (list) {
      if (!list || !list.length) { $('#monitors').innerHTML = '<div class="muted small">Nenhum monitor DDC/CI</div>'; return; }
      $('#monitors').innerHTML = list.map(function (m) {
        return '<div class="mon-row"><div class="small"><span>☀️ ' + esc(m.name) + '</span><b class="mon-val">' +
          (m.brightness == null ? 'sem DDC' : m.brightness + '%') + '</b></div>' +
          '<div class="slider h" data-i="' + m.index + '"><div class="fill"></div></div></div>';
      }).join('');
      $$('.mon-row').forEach(function (row, i) {
        var m = list[i], val = $('.mon-val', row);
        var sl = makeSlider($('.slider', row), {
          throttle: 250,  // DDC é lento; não inunde o barramento I2C
          onLocal: function (v) { setText(val, Math.round(v * 100) + '%'); },
          onInput: function (v) { sendQuiet('display.set', { index: m.index, value: Math.round(v * 100) }); }
        });
        sl.set((m.brightness || 0) / 100);
      });
    }).catch(function (e) {
      monitorsLoaded = false;
      $('#monitors').innerHTML = '<div class="muted small">Erro: ' + esc(e.message) + '</div>';
    });
  }
  clickHandlers['ddc-refresh'] = loadMonitors;

  // ---------------------------------------------------------------- dimmer noturno
  var nightCfg = null, dimManual = store('cc_dim') === '1', wakeUntil = 0;
  var dimmer = $('#dimmer');
  function minutes(hhmm) { var p = String(hhmm || '0:0').split(':'); return (+p[0]) * 60 + (+p[1] || 0); }
  function inNight() {
    if (!nightCfg || !nightCfg.enabled) return false;
    var d = new Date(), now = d.getHours() * 60 + d.getMinutes();
    var a = minutes(nightCfg.start), b = minutes(nightCfg.end);
    return a <= b ? (now >= a && now < b) : (now >= a || now < b);
  }
  function updateDim() {
    var want = (dimManual || inNight()) && Date.now() > wakeUntil;
    var op = dimManual ? 0.85 : (nightCfg && nightCfg.opacity) || 0.75;
    dimmer.style.opacity = want ? op : 0;
    dimmer.classList.toggle('on', want);
    $('#dim-toggle').classList.toggle('on', dimManual);
  }
  dimmer.addEventListener('pointerdown', function (e) {
    e.preventDefault(); e.stopPropagation();
    wakeUntil = Date.now() + 60000;  // toque acorda por 60 s
    updateDim();
  });
  clickHandlers['dim-toggle'] = function () {
    dimManual = !dimManual; store('cc_dim', dimManual ? '1' : '0');
    wakeUntil = dimManual ? Date.now() + 1500 : 0;
    updateDim();
    toast(dimManual ? 'Tela vai escurecer — toque para acordar' : 'Escurecimento desligado');
  };

  // ---------------------------------------------------------------- config / relógio
  function applyConfig(c) {
    setText($('#host'), c.host || '');
    launcherPages = c.launcher || [];
    renderLauncher(+(store('cc_lpage') || 0));
    nightCfg = c.night_dim || null;
    updateDim();
    if (currentTab === 'system' && !monitorsLoaded) loadMonitors();
  }

  function tickClock() {
    var d = new Date();
    setText($('#clock'), ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2));
    try {
      setText($('#date'), d.toLocaleDateString('pt-BR', { weekday: 'short', day: '2-digit', month: 'short' }));
    } catch (e) { setText($('#date'), d.toDateString()); }
  }
  setInterval(function () { tickClock(); updateDim(); }, 1000);

  // ---------------------------------------------------------------- notificações
  var notifs = [], unread = 0, actx = null;
  var KIND_ICON = { lol: '⚔️', claude: '✳️', discord: '🎧', sys: '⚙️' };
  // O iOS só libera áudio depois de um toque do usuário: destrava no primeiro toque.
  function unlockAudio() {
    if (actx) { if (actx.state === 'suspended' && actx.resume) actx.resume(); return; }
    try {
      var C = window.AudioContext || window.webkitAudioContext;
      if (!C) return;
      actx = new C();
      var b = actx.createBuffer(1, 1, 22050), src = actx.createBufferSource();
      src.buffer = b; src.connect(actx.destination); src.start(0);
    } catch (e) { actx = null; }
  }
  document.addEventListener('pointerdown', unlockAudio, true);
  function beep(level) {
    if (!actx) return;
    var notes = level === 'alert' ? [880, 1175, 880, 1175, 1568] : [660, 990];
    var t0 = actx.currentTime + 0.02;
    notes.forEach(function (f, i) {
      var o = actx.createOscillator(), g = actx.createGain(), t = t0 + i * 0.16;
      o.type = 'sine'; o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.35, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.15);
      o.connect(g); g.connect(actx.destination);
      o.start(t); o.stop(t + 0.16);
    });
  }
  function flashScreen() {
    var f = $('#flash'); f.classList.add('on');
    setTimeout(function () { f.classList.remove('on'); }, 400);
  }
  function timeAgo(ts) {
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return 'agora';
    if (s < 3600) return Math.floor(s / 60) + ' min';
    if (s < 86400) return Math.floor(s / 3600) + ' h';
    return Math.floor(s / 86400) + ' d';
  }
  function renderNotifs() {
    $('#notif-list').innerHTML = notifs.slice(0, 8).map(function (n) {
      return '<div class="ntf ' + esc(n.level) + '"><i>' + (KIND_ICON[n.kind] || '🔔') + '</i>' +
        '<div class="ntf-txt"><b class="ellipsis">' + esc(n.title) + '</b>' +
        (n.body ? '<small class="ellipsis">' + esc(n.body) + '</small>' : '') + '</div>' +
        '<time>' + timeAgo(n.ts) + '</time></div>';
    }).join('') || '<div class="muted small">Nada por aqui</div>';
    var c = $('#bell-count');
    c.textContent = unread > 9 ? '9+' : String(unread);
    c.classList.toggle('hidden', unread === 0);
  }
  function onNotify(n) {
    notifs.unshift(n);
    if (notifs.length > 50) notifs.pop();
    if ($('#notif-panel').classList.contains('hidden')) unread++;
    renderNotifs();
    toast(n.title + (n.body ? ' — ' + n.body : ''));
    if (n.sound) beep(n.level);
    if (n.level === 'alert') flashScreen();
  }
  clickHandlers['bell'] = function () {
    var p = $('#notif-panel'), open = p.classList.contains('hidden');
    p.classList.toggle('hidden', !open);
    if (open) { unread = 0; renderNotifs(); }
  };
  clickHandlers['notif-clear'] = function () { sendQuiet('notify.clear'); };
  document.addEventListener('pointerdown', function (e) {
    var p = $('#notif-panel');
    if (!p.classList.contains('hidden') && !e.target.closest('#notif-panel, #bell')) p.classList.add('hidden');
  }, true);

  // ---------------------------------------------------------------- LoL
  var LOL_PHASES = {
    closed: 'Cliente fechado', None: 'Cliente aberto', Lobby: 'No lobby', Matchmaking: 'Na fila',
    ReadyCheck: 'Partida encontrada!', ChampSelect: 'Seleção de campeões', GameStart: 'Carregando partida',
    InProgress: 'Em partida', Reconnect: 'Reconectar à partida', WaitingForStats: 'Fim de jogo',
    PreEndOfGame: 'Fim de jogo', EndOfGame: 'Fim de jogo'
  };
  var lolChampId = null, lolAuto = false;
  var lolImg = $('#lol-champ-img');
  lolImg.onload = function () { lolImg.parentNode.classList.add('has-img'); };
  lolImg.onerror = function () { lolImg.parentNode.classList.remove('has-img'); };
  function renderLol(l) {
    if (!l) return;
    setText($('#lol-phase'), LOL_PHASES[l.phase] || l.phase || '—');
    var sub = ' ';
    if (l.queue) sub = 'Na fila há ' + fmtTime(l.queue.time) + (l.queue.estimated ? ' · estimado ' + fmtTime(l.queue.estimated) : '');
    else if (l.phase === 'closed') sub = 'Abra o cliente para usar';
    else if (l.select) {
      var pos = LOL_POS[(l.select.position || '').toLowerCase()] || (l.select.mode === 'ARAM' ? 'ARAM' : 'sem posição');
      sub = 'Você: ' + (l.select.champion || 'escolhendo…') + (l.select.locked ? ' ✔' : '') + ' · ' + pos;
    }
    setText($('#lol-sub'), sub);
    renderBuild(l);
    lolAuto = !!l.auto_accept;
    $('#lol-auto').classList.toggle('on', lolAuto);
    setText($('#lol-auto-state'), lolAuto ? 'sim' : 'não');
    var rc = l.ready_check;
    $('#lol-accept-btn').classList.toggle('hidden', !(rc && rc.response === 'None'));
    var modal = !!(rc && rc.response === 'None');
    $('#ready-modal').classList.toggle('hidden', !modal);
    if (modal) setText($('#ready-timer'), Math.max(0, Math.ceil(rc.remaining)));

    var g = l.game;
    var view = lolViewFor(l);
    $('#lol-v-game').classList.toggle('on', view === 'game');
    $('#lol-v-build').classList.toggle('on', view === 'build');
    $('#lol-build').classList.toggle('hidden', view !== 'build');
    $('#lol-live').classList.toggle('hidden', view !== 'game' || !g);
    $('#lol-nogame').classList.toggle('hidden', view !== 'game' || !!g);
    if (!g) { setText($('#lol-time'), '--:--'); lvClock.on = false; return; }
    setText($('#lol-time'), fmtTime(g.time));
    renderLive(g);
    setText($('#lol-champ'), g.champion || '—');
    setText($('#lol-level'), g.level);
    setText($('#lol-gold'), Math.floor(g.gold));
    setText($('#lol-k'), g.kills); setText($('#lol-d'), g.deaths); setText($('#lol-a'), g.assists);
    setText($('#lol-kda-ratio'), 'KDA ' + ((g.kills + g.assists) / Math.max(1, g.deaths)).toFixed(1));
    setText($('#lol-cs'), g.cs);
    setText($('#lol-csm'), g.time > 60 ? (g.cs / (g.time / 60)).toFixed(1) : '0.0');
    setText($('#lol-ward'), Math.round(g.ward_score || 0));
    var teamKills = g.my_team === 'ORDER' ? g.blue_kills : g.red_kills;
    setText($('#lol-kp'), teamKills > 0 ? Math.round((g.kills + g.assists) / teamKills * 100) + '%' : '0%');
    setText($('#lol-blue'), g.blue_kills); setText($('#lol-red'), g.red_kills);
    var tot = g.blue_kills + g.red_kills;
    setTransform($('#lol-score-bar'), 'scaleX(' + (tot ? g.blue_kills / tot : 0.5) + ')');
    if (g.champion_id !== lolChampId) {
      lolChampId = g.champion_id;
      if (g.champion_id) lolImg.src = '/lol/champion-icon/' + g.champion_id;
      else lolImg.parentNode.classList.remove('has-img');
    }
  }
  // ----- Build (seleção de campeões / partida) -----
  var LOL_POS = { top: 'Topo', jungle: 'Selva', middle: 'Meio', mid: 'Meio', bottom: 'Atirador', utility: 'Suporte' };
  var lolViewPick = null, lolViewPhase = null, lbSig = '';
  // Automático: seleção de campeões -> Build; partida -> Partida. Os chips mudam até a próxima fase.
  function lolViewFor(l) {
    if (l.phase !== lolViewPhase) { lolViewPhase = l.phase; lolViewPick = null; }
    if (lolViewPick) return lolViewPick;
    return l.phase === 'InProgress' ? 'game' : (l.build ? 'build' : 'game');
  }
  clickHandlers['lol-v-game'] = function () { lolViewPick = 'game'; if (lastState) renderLol(lastState.lol); };
  clickHandlers['lol-v-build'] = function () { lolViewPick = 'build'; if (lastState) renderLol(lastState.lol); };
  function assetUrl(p) { return p ? '/lol/asset?p=' + encodeURIComponent(p) : ''; }
  function icon(x, cls) {
    return '<img class="' + (cls || '') + '" src="' + esc(assetUrl(x.icon)) + '" alt="' + esc(x.name) + '">';
  }
  function icons(list, cls, sep) {
    return (list || []).map(function (x) { return icon(x, cls); }).join(sep ? '<i class="lb-sep">›</i>' : '');
  }
  function pct(v) { return v == null ? '—' : (v * 100).toFixed(1).replace('.', ',') + '%'; }

  // ----- Análise ao vivo (g.live, vem de server/modules/lol_live.py) -----
  var LV_TABS = ['sum', 'lane', 'teams', 'obj', 'feed'];
  var lvTab = 'sum', lvLastG = null, lvSig = {};
  var lvClock = { on: false, t: 0, at: 0 };   // relógio de jogo interpolado entre ticks do servidor
  var ROLE_PT = { fighter: 'Lutador', tank: 'Tanque', mage: 'Mago', assassin: 'Assassino', marksman: 'Atirador', support: 'Suporte' };
  var POS_PT = { top: 'Topo', jungle: 'Selva', mid: 'Meio', adc: 'Atirador', support: 'Suporte' };
  LV_TABS.forEach(function (k) {
    clickHandlers['lv-c-' + k] = function () { lvTab = k; if (lvLastG) renderLive(lvLastG); };
  });
  function lvNow() { return lvClock.t + (Date.now() - lvClock.at) / 1000; }
  function signed(v, unit, dec) { v = dec ? v.toFixed(dec) : Math.round(v); return (v > 0 ? '+' : '') + v + (unit || ''); }
  function cls(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : ''; }
  function kfmt(v) { return Math.abs(v) >= 1000 ? (v / 1000).toFixed(1).replace('.', ',') + 'k' : String(Math.round(v)); }
  function cd(at, zero) {   // contagem regressiva em tempo de jogo, atualizada por lvTick
    var left = at - lvNow();
    return '<b class="lv-cd" data-at="' + at + '" data-zero="' + esc(zero || '') + '">' +
      (left > 0 ? fmtTime(left) : esc(zero || '')) + '</b>';
  }
  function lvTick() {
    if (!lvClock.on) return;
    $$('.lv-cd').forEach(function (el) {
      var left = parseFloat(el.dataset.at) - lvNow();
      setText(el, left > 0 ? fmtTime(left) : (el.dataset.zero || ''));
    });
  }
  setInterval(lvTick, 1000);

  // Gráfico em SVG inline: séries [{ys, kind}] sobre o eixo xs. zero=true desenha a linha do zero.
  function lvChart(svg, xs, series, zero) {
    if (!xs || xs.length < 2) { svg.innerHTML = ''; return; }
    var lo = Infinity, hi = -Infinity;
    series.forEach(function (s) { s.ys.forEach(function (y) { lo = Math.min(lo, y); hi = Math.max(hi, y); }); });
    if (zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    if (hi - lo < 1) { hi += 1; lo -= 1; }
    var x0 = xs[0], x1 = xs[xs.length - 1] || 1, W = 300, H = 56;
    var X = function (x) { return ((x - x0) / Math.max(1, x1 - x0) * W).toFixed(1); };
    var Y = function (y) { return (H - 3 - (y - lo) / (hi - lo) * (H - 6)).toFixed(1); };
    var out = zero ? '<line class="zero" x1="0" x2="' + W + '" y1="' + Y(0) + '" y2="' + Y(0) + '"/>' : '';
    series.forEach(function (s) {
      out += '<polyline class="' + s.kind + '" points="' + s.ys.map(function (y, i) { return X(xs[i]) + ',' + Y(y); }).join(' ') + '"/>';
    });
    svg.innerHTML = out;
  }

  function renderLive(g) {
    var L = g.live;
    lvLastG = g;
    lvClock.t = g.time; lvClock.at = Date.now(); lvClock.on = true;
    var tabs = $('#lol-live');
    LV_TABS.forEach(function (k) {
      $('#lv-c-' + k).classList.toggle('on', k === lvTab);
      $('#lv-' + k).classList.toggle('hidden', k !== lvTab);
    });
    tabs.classList.toggle('no-live', !L);
    if (!L) {   // servidor sem análise (versão antiga em execução ou erro): o Resumo básico continua
      var msg = 'Análise ao vivo indisponível. Reinicie o servidor no PC (veja logs/server.log se persistir).';
      if (lvTab === 'sum') setText($('#lv-pace'), msg);
      else $('#lv-' + lvTab).innerHTML = '<div class="lol-nogame muted">' + esc(msg) + '</div>';
      lvSig[lvTab] = null;
      return;
    }
    var part = { sum: lvSum, lane: lvLane, teams: lvTeams, obj: lvObj, feed: lvFeed }[lvTab];
    var sig = lvTab === 'sum' ? '' : JSON.stringify(lvTab === 'lane' ? L.lane : lvTab === 'teams' ? L.teams :
      lvTab === 'obj' ? L.objectives : L.events);
    if (lvTab !== 'sum' && lvSig[lvTab] === sig) return;
    lvSig[lvTab] = sig;
    part(L);
    lvTick();
  }

  function lvSum(L) {
    var m = L.me, p = L.pace;
    var dead = $('#lv-dead');
    dead.classList.toggle('hidden', !m.dead);
    if (m.dead) setText(dead, 'Você está morto · volta em ' + m.respawn + 's');
    var bits = ['CS/min <b>' + (p.cs_per_min || 0).toFixed(1) + '</b>'];
    if (p.benchmark) bits[0] += ' <span class="muted">(meta ' + p.benchmark.toFixed(1) + (p.cs_delta != null ?
      ', <span class="' + cls(p.cs_delta) + '">' + signed(p.cs_delta, ' CS') + '</span>' : '') + ')</span>';
    bits.push('CS@10 <b>' + (p.cs10 == null ? '—' : p.cs10) + '</b>');
    bits.push('CS@15 <b>' + (p.cs15 == null ? '—' : p.cs15) + '</b>');
    if (p.gold_per_min != null) bits.push('~<b>' + p.gold_per_min + '</b> ouro/min');
    $('#lv-pace').innerHTML = bits.join(' · ');
    var n = m.next_item, el = $('#lv-next');
    el.classList.toggle('hidden', !n && !m.idle_gold.active);
    el.classList.toggle('warn', !!m.idle_gold.active);
    var msg = m.idle_gold.active ? '<b>' + m.gold + ' de ouro parado</b> · ' : '';
    if (n) msg += 'Próximo: ' + bgIcon(n.icon ? assetUrl(n.icon) : '', 'lv-ic', n.name) + ' <b>' + esc(n.name) + '</b>' + (n.missing == null ? '' :
      n.missing > 0 ? ' · faltam ~' + n.missing + ' de ouro' : ' · <span class="pos">já dá para comprar</span>');
    else if (m.idle_gold.active) msg += 'volte à base';
    el.innerHTML = msg;
    var s = L.series, bench = p.benchmark;
    var csSeries = [{ ys: s.cs, kind: 'cs' }];
    if (bench) csSeries.push({ ys: s.t.map(function (t) { return bench * t / 60; }), kind: 'ref' });
    lvChart($('#lv-chart-cs'), s.t, csSeries, false);
    lvChart($('#lv-chart-val'), s.t, [{ ys: s.value_diff, kind: 'cs' }], true);
  }

  // Ícones da análise ao vivo usam background-image (não <img>): a falha de um ícone não dispara o
  // handler global de erros da página nem o faixa vermelha, e não refaz requisição a cada re-render.
  function bgIcon(url, klass, title) {
    return '<i class="' + klass + '" title="' + esc(title || '') + '"' +
      (url ? ' style="background-image:url(&quot;' + esc(url) + '&quot;)"' : '') + '></i>';
  }
  function plIcon(p) {
    return bgIcon(p.champion_id ? '/lol/champion-icon/' + p.champion_id : '', 'lv-pic', p.name);
  }
  function itemsRow(p) {
    return '<span class="lv-items">' + (p.items || []).map(function (i) {
      return bgIcon(i.icon ? assetUrl(i.icon) : '', 'lv-ic', i.name);
    }).join('') + '</span>';
  }

  function lvLane(L) {
    var l = L.lane, el = $('#lv-lane');
    if (!l) { el.innerHTML = '<div class="lol-nogame muted">Sem oponente de rota neste modo</div>'; return; }
    var me = l.me, op = l.opponent, d = l.diff;
    var row = function (label, a, diff, b, dec) {
      return '<div class="lv-rowx"><span class="lv-a">' + a + '</span><span class="lv-d ' + cls(diff) + '">' +
        esc(label) + '<br>' + signed(diff, '', dec) + '</span><span class="lv-b">' + b + '</span></div>';
    };
    var kda = function (p) { return p.k + '/' + p.d + '/' + p.a; };
    el.innerHTML = '<div class="lv-rowx lv-heads"><span class="lv-a">' + plIcon(me) + '<b>' + esc(me.name) + '</b></span>' +
      '<span class="lv-d muted">' + esc(POS_PT[me.position] || '') + '</span>' +
      '<span class="lv-b"><b>' + esc(op.name) + '</b>' + plIcon(op) + '</span></div>' +
      row('Nível', me.level, d.level, op.level) + row('CS', me.cs, d.cs, op.cs) +
      row('KDA', kda(me), d.kda, kda(op), 1) +
      row('Itens (est.)', kfmt(me.items_value), d.items, kfmt(op.items_value)) +
      '<div class="lv-rowx"><span class="lv-a">' + itemsRow(me) + '</span><span class="lv-d"></span><span class="lv-b">' +
      itemsRow(op) + '</span></div>';
  }

  function teamRows(t) {
    return t.players.map(function (p) {
      return '<div class="lv-pl' + (p.me ? ' me' : '') + (p.dead ? ' dead' : '') + '">' + plIcon(p) +
        '<b class="lv-nm ellipsis">' + esc(p.name) + '</b><span class="lv-lv">' + p.level + '</span>' +
        '<span class="lv-kda">' + p.k + '/' + p.d + '/' + p.a + '</span><span class="lv-cs">' + p.cs + '</span>' +
        '<span class="lv-val">' + kfmt(p.items_value) + '</span></div>';
    }).join('');
  }
  function compLine(c) {
    var ks = Object.keys(c || {});
    return ks.length ? ks.map(function (k) { return (ROLE_PT[k] || k) + ' ' + c[k]; }).join(' · ') : '—';
  }
  function lvTeams(L) {
    var T = L.teams, a = T.ally, e = T.enemy, th = T.threat;
    var tr = T.value_trend;
    var html = '<div class="lv-sumline"><span class="ally">Aliados <b>' + a.kills + '</b> abates · ' + a.towers + ' torres</span>' +
      '<span class="enemy">Inimigos <b>' + e.kills + '</b> abates · ' + e.towers + ' torres</span></div>' +
      '<div class="lv-line">Valor de itens (estimativa): <b class="' + cls(T.value_diff) + '">' + (T.value_diff > 0 ? '+' : '') + kfmt(T.value_diff) +
      '</b> (' + kfmt(a.items_value) + ' vs ' + kfmt(e.items_value) + ')' +
      (tr == null ? '' : ' · últimos 2 min <b class="' + cls(tr) + '">' + signed(tr) + '</b>') + '</div>' +
      '<div class="lv-th muted"><span></span><span></span><span class="lv-lv">Nv</span><span class="lv-kda">K/D/A</span><span class="lv-cs">CS</span><span class="lv-val">Itens</span></div>' +
      '<div class="lv-team ally">' + teamRows(a) + '</div><div class="lv-team enemy">' + teamRows(e) + '</div>' +
      '<div class="lv-line muted">Seu time: ' + esc(compLine(T.comp.ally)) + '<br>Inimigo: ' + esc(compLine(T.comp.enemy)) + '</div>';
    if (th) html += '<div class="lv-line">Dano inimigo (estimativa): <b>' + Math.round(th.ad_share * 100) + '% físico</b> · considere ' + esc(th.hint) + '</div>';
    $('#lv-teams').innerHTML = html;
  }

  function lvObj(L) {
    var o = L.objectives, el = $('#lv-obj');
    if (!o) { el.innerHTML = '<div class="lol-nogame muted">Sem objetivos neste modo</div>'; return; }
    var dr = function (list) {
      return list.length ? list.map(function (t) { return '<span class="lv-pill">' + esc(o.dragon.names[t] || t) + '</span>'; }).join('') : '<span class="muted">—</span>';
    };
    var soul = o.dragon.soul ? ' · <b class="' + (o.dragon.soul === 'ally' ? 'pos' : 'neg') + '">alma ' +
      (o.dragon.soul === 'ally' ? 'sua' : 'inimiga') + '</b>' : '';
    var buff = o.baron.buff ? '<div class="lv-line ' + (o.baron.buff.side === 'ally' ? 'pos' : 'neg') + '">Buff do Barão ' +
      (o.baron.buff.side === 'ally' ? 'aliado' : 'inimigo') + ': ' + cd(o.baron.buff.until, '') + '</div>' : '';
    var inh = o.inhibs.map(function (i) {
      return '<div class="lv-line ' + (i.side === 'ally' ? 'neg' : 'pos') + '">Inibidor ' + (i.side === 'ally' ? 'aliado caído' : 'inimigo caído') +
        ' · volta em ' + cd(i.back_at, '') + '</div>';
    }).join('');
    var pair = function (label, a, b) {
      return '<div class="lv-rowx"><span class="lv-a ally">' + a + '</span><span class="lv-d muted">' + label + '</span><span class="lv-b enemy">' + b + '</span></div>';
    };
    el.innerHTML = '<div class="lv-line">' + (o.dragon.next_is_elder ? 'Dragão Ancião' : 'Dragão') + ': ' +
      cd(o.dragon.next_at, 'disponível') + soul + '</div>' +
      pair('Dragões', dr(o.dragon.ally), dr(o.dragon.enemy)) +
      '<div class="lv-line">Barão: ' + cd(o.baron.next_at, 'disponível') + '</div>' + buff +
      pair('Barões', o.baron.count.ally, o.baron.count.enemy) +
      pair('Arautos', o.herald.ally, o.herald.enemy) + pair('Vastilarvas', o.grubs.ally, o.grubs.enemy) +
      pair('Torres', o.towers.ally, o.towers.enemy) + inh +
      '<div class="lv-line muted small">Tempos de respawn são aproximados e dependem do patch.</div>';
  }

  function lvFeed(L) {
    var ev = L.events;
    $('#lv-feed').innerHTML = ev.length ? ev.map(function (e) {
      return '<div class="lv-ev ' + esc(e.side) + '"><span class="muted">' + fmtTime(e.t) + '</span> ' + esc(e.text) + '</div>';
    }).join('') : '<div class="lol-nogame muted">Nada aconteceu ainda</div>';
  }

  function renderBuild(l) {
    var b = l.build, d = b && b.data;
    $('#lb-body').classList.toggle('hidden', !d);
    $('#lb-empty').classList.toggle('hidden', !!d);
    if (!d) {
      setText($('#lb-empty'), !b ? 'Escolha um campeão' : (b.loading ? 'Buscando build…' : (b.error || 'Build indisponível')));
      return;
    }
    var sig = d.champion_id + '|' + d.position;
    if (sig === lbSig) return;   // só redesenha quando muda o campeão/posição
    lbSig = sig;
    $('#lb-champ-img').src = assetUrl(d.champion_icon);
    setText($('#lb-title'), d.champion + ' · ' + d.position_label);
    setText($('#lb-stats'), pct(d.win_rate) + ' vitórias · ' + pct(d.pick_rate) + ' escolha' +
      (d.play ? ' · ' + Math.round(d.play / 1000) + 'k partidas' : '') + (d.tier ? ' · tier ' + d.tier : ''));
    var r = d.runes;
    $('#lb-runes').innerHTML = icon(r.primary_style, 'style') + icon(r.primary[0] || {}, 'key') +
      icons(r.primary.slice(1)) + '<i class="lb-gap"></i>' + icon(r.sub_style, 'style') + icons(r.secondary) +
      '<i class="lb-gap"></i>' + icons(r.shards, 'shard');
    $('#lb-spells').innerHTML = icons(d.spells);
    $('#lb-start').innerHTML = icons(d.starter);
    $('#lb-core').innerHTML = icons(d.core, '', true);
    $('#lb-boots').innerHTML = icons(d.boots);
    $('#lb-situ').innerHTML = icons(d.situational);
    $('#lb-skills').innerHTML = '<b class="lb-prio">' + esc((d.skill_priority || []).join(' › ')) + '</b>' +
      (d.skill_order || []).map(function (k) { return '<span class="lb-sk ' + esc(k) + '">' + esc(k) + '</span>'; }).join('');
    setText($('#lb-src'), 'Fonte: ' + d.source + ' · build mais popular · toque num ícone para ver o nome');
  }
  // Toque num ícone da build mostra o nome (itens, runas, feitiços).
  $('#lol-build').addEventListener('click', function (e) {
    if (e.target.tagName === 'IMG' && e.target.alt) toast(e.target.alt);
  });

  clickHandlers['lol-auto'] = function () {
    send('lol.auto_accept', !lolAuto).then(function (v) {
      toast(v ? 'Aceitar automático ligado' : 'Aceitar automático desligado');
    }).catch(function (e) { toast(e.message, true); });
  };

  // ---------------------------------------------------------------- Discord
  var dcSig = '', dcSliders = {};
  function renderDiscord(d) {
    if (!d) return;
    var chan = '—', sub = ' ';
    if (!d.configured) { chan = 'Não configurado'; sub = 'Preencha "discord" no config.json (veja o README)'; }
    else if (!d.connected) { chan = 'Discord fechado'; sub = 'Abra o Discord no PC'; }
    else if (!d.authorized) { chan = 'Autorizar'; sub = d.error || 'Aprove o pedido na janela do Discord'; }
    else if (d.channel) { chan = d.channel.name; sub = d.channel.guild ? d.channel.guild : 'Chamada'; }
    else { chan = 'Fora de call'; sub = 'Toque em Entrar no grupo'; }
    setText($('#dc-channel'), chan);
    setText($('#dc-sub'), sub);
    var me = d.self || {};
    $('#dc-mute').classList.toggle('on', !!me.mute);
    $('#dc-deaf').classList.toggle('on', !!me.deaf);
    setText($('#dc-mute span'), me.mute ? 'Mutado' : 'Mute');
    setText($('#dc-deaf span'), me.deaf ? 'Surdo' : 'Ensurdecer');

    var members = (d.channel && d.channel.members) || [];
    setText($('#dc-count'), members.length);
    var sig = members.map(function (m) { return m.id; }).join(',');
    if (sig !== dcSig) {
      dcSig = sig; dcSliders = {};
      var box = $('#dc-members');
      box.innerHTML = members.slice(0, 8).map(function (m) {
        var av = m.avatar ? ' style="background-image:url(' + esc(m.avatar) + ')"' : '';
        return '<div class="dc-m" data-u="' + esc(m.id) + '">' +
          '<div class="dc-av"' + av + '>' + (m.avatar ? '' : esc((m.name || '?').charAt(0).toUpperCase())) + '</div>' +
          '<div class="dc-name ellipsis">' + esc(m.name) + '</div><div class="dc-flags"></div>' +
          (m.is_self ? '<div class="muted small">você</div>' : '<div class="slider h"><div class="fill"></div></div>') + '</div>';
      }).join('') || '<div class="muted">Ninguém na call</div>';
      $$('.dc-m', box).forEach(function (el) {
        var uid = el.dataset.u, sl = $('.slider', el);
        if (!sl) return;
        dcSliders[uid] = makeSlider(sl, {
          throttle: 120,
          onInput: function (v) { sendQuiet('discord.user_volume', { id: uid, volume: Math.round(v * 200) }); }
        });
      });
    }
    members.forEach(function (m) {
      var el = $('.dc-m[data-u="' + m.id + '"]');
      if (!el) return;
      el.classList.toggle('speaking', !!m.speaking);
      setText($('.dc-flags', el), (m.mute ? '🔇 ' : '') + (m.deaf ? '🎧✕' : '') + (m.volume != null && !m.is_self ? ' ' + m.volume + '%' : ''));
      if (dcSliders[m.id] && m.volume != null) dcSliders[m.id].set(m.volume / 200, true);
    });
  }

  // ---------------------------------------------------------------- Claude
  var clPermSig = '', clSessSig = '', clLaunchSig = '', clRemote = false;
  clickHandlers['cl-remote'] = function () {
    send('claude.remote', !clRemote).then(function (v) {
      toast(v ? 'Pedidos de permissão virão para o iPad' : 'Pedidos de permissão voltam para o PC');
    }).catch(function (e) { toast(e.message, true); });
  };
  function fmtTok(n) {
    if (n == null) return '—';
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return Math.round(n / 1e3) + 'k';
    return String(n);
  }
  var CL_STATUS = { working: 'trabalhando', waiting: 'esperando você', done: 'concluído' };
  function renderClaude(c) {
    if (!c) return;
    setText($('#cl-hooks'), c.hooks ? 'hooks ativos' : 'hooks não instalados');
    clRemote = !!c.remote;
    $('#cl-remote').classList.toggle('on', clRemote);
    setText($('#cl-remote-state'), clRemote ? 'sim' : 'não');
    var perms = c.permissions || [];
    var badge = $('#claude-badge');
    badge.textContent = String(perms.length);
    badge.classList.toggle('hidden', perms.length === 0);
    var psig = perms.map(function (p) { return p.id; }).join(',');
    if (psig !== clPermSig) {
      clPermSig = psig;
      $('#cl-perms').innerHTML = perms.slice(0, 2).map(function (p) {
        return '<div class="cl-perm"><div class="cl-perm-head">🔐 ' + esc(p.project) + ' quer usar <b>' + esc(p.tool) + '</b></div>' +
          '<pre>' + esc(p.summary) + '</pre><div class="cl-perm-btns">' +
          '<button class="btn allow" data-action="claude.permission.decide" data-v="' + esc(p.id) + ':allow">Permitir</button>' +
          '<button class="btn deny" data-action="claude.permission.decide" data-v="' + esc(p.id) + ':deny">Negar</button></div></div>';
      }).join('');
    }
    var sessions = (c.sessions || []).slice(0, perms.length ? 3 : 6);
    var ssig = JSON.stringify(sessions.map(function (s) { return [s.id, s.status, s.message, Math.floor(s.ts / 60)]; }));
    if (ssig !== clSessSig) {
      clSessSig = ssig;
      $('#cl-sessions').innerHTML = sessions.map(function (s) {
        return '<div class="cl-s"><div class="cl-s-txt"><b class="ellipsis">' + esc(s.project) + '</b>' +
          '<small class="ellipsis">' + esc(s.message || '') + '</small></div>' +
          '<span class="pill ' + esc(s.status) + '">' + esc(CL_STATUS[s.status] || s.status) + '</span>' +
          '<small class="muted">' + timeAgo(s.ts) + '</small></div>';
      }).join('') || '<div class="muted">' + (c.hooks ? 'Nenhuma sessão ainda' : 'Instale os hooks: python scripts\\install_claude_hooks.py') + '</div>';
    }
    var u = c.usage || {};
    setText($('#cl-today'), fmtTok(u.today));
    setText($('#cl-week'), fmtTok(u.week));
    setText($('#cl-top'), (u.top || []).map(function (t) { return t.project + ': ' + fmtTok(t.tokens); }).join('  ·  ') || ' ');
    var launch = c.launch || [];
    var lsig = launch.map(function (x) { return x.id + x.label; }).join('|');
    if (lsig !== clLaunchSig) {
      clLaunchSig = lsig;
      $('#cl-launch').innerHTML = launch.map(function (x) {
        return '<button class="btn" data-action="claude.launch" data-v="' + esc(x.id) + '"><span class="ellipsis">' + esc(x.label) + '</span></button>';
      }).join('');
    }
  }

  // ---------------------------------------------------------------- diagnóstico
  // Foto do layout enviada ao servidor (logs/client-debug.log) para depurar o
  // Safari do iPad sem precisar de Mac/Web Inspector.
  function rect(sel) {
    var el = $(sel);
    if (!el) return null;
    var r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
      display: cs.display, op: cs.opacity, vis: cs.visibility };
  }
  function supports(p, v) { try { return CSS.supports(p, v); } catch (e) { return 'n/a'; } }
  function diag(kind, extra) {
    if (!window.__ccReport) return;
    window.__ccReport(kind, {
      extra: extra || null,
      ws: ws ? ws.readyState : null,
      tab: currentTab,
      body: rect('body'), app: rect('#app'), topbar: rect('#topbar'), telemetry: rect('#telemetry'),
      pages: rect('#pages'), activePage: rect('.page.active'), tabbar: rect('#tabbar'),
      overlay: rect('#overlay'), dimmer: rect('#dimmer'), tokenModal: rect('#token-modal'),
      gridRows: getComputedStyle($('#app')).gridTemplateRows,
      css: { inset: supports('inset', '0'), grid: supports('display', 'grid'), gap: supports('gap', '1px'),
        backdrop: supports('-webkit-backdrop-filter', 'blur(2px)'), env: supports('padding-top', 'env(safe-area-inset-top)') }
    });
  }
  window.__ccDiag = diag;
  window.__ccRenderLol = function (l) { renderLol(l); };  // depuração: simular estados do LoL
  setTimeout(function () { diag('layout-3s'); }, 3000);

  // Rede de segurança para WebKit antigo: se a linha "1fr" do grid principal
  // vier com 0 px, calcula a sobra em pixels e fixa as linhas no style.
  var appEl = $('#app');
  function setPageHeight() {
    // Altura real da área das abas -> --page-h (o CSS calcula tudo em px a partir dela).
    document.documentElement.style.setProperty('--page-h', $('#pages').clientHeight + 'px');
  }
  function fixGridRows() {
    appEl.style.gridTemplateRows = '';           // volta ao CSS e mede de novo
    if ($('#pages').offsetHeight > 40) { setPageHeight(); return; }  // CSS funcionou
    var cs = getComputedStyle(appEl);
    var rows = cs.gridTemplateRows.split(' ').map(parseFloat);
    var gap = parseFloat(cs.rowGap || cs.gridRowGap) || 0;
    var fixed = 0;
    for (var i = 0; i < rows.length; i++) if (i !== 2) fixed += rows[i];
    var free = appEl.clientHeight - fixed - gap * (rows.length - 1);
    rows[2] = Math.max(0, Math.floor(free));
    appEl.style.gridTemplateRows = rows.map(function (r) { return r + 'px'; }).join(' ');
    setPageHeight();
    if (window.__ccReport) window.__ccReport('grid-fallback', { rows: appEl.style.gridTemplateRows });
  }
  window.__ccFixGrid = fixGridRows;
  fixGridRows();
  window.addEventListener('resize', function () { setTimeout(fixGridRows, 100); });
  window.addEventListener('orientationchange', function () { setTimeout(fixGridRows, 300); });

  // ---------------------------------------------------------------- start
  tickClock();
  sizeSpark();
  switchTab(store('cc_tab') || 'media');
  updateDim();
  connect();
})();
