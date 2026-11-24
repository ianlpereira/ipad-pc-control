/* Linha do tempo do Reels. Tudo é função do tempo: window.seek(t) desenha o quadro t (segundos).
   Coordenadas de toque estão em px CSS do painel (1024x768); veja promo/data/scenes.json. */
(function () {
  'use strict';
  var DURATION = 57;
  var $ = function (id) { return document.getElementById(id); };
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function lerp(a, b, k) { return a + (b - a) * k; }
  function ease(k) { k = clamp(k, 0, 1); return k < .5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2; }
  function easeOut(k) { k = clamp(k, 0, 1); return 1 - Math.pow(1 - k, 3); }
  function prog(t, a, b) { return clamp((t - a) / (b - a), 0, 1); }

  // ------------------------------------------------------------------ trilhas
  var TAB = { media: 66, mixer: 178, lol: 290, fla: 400, discord: 512, claude: 622, launcher: 734, system: 956 };
  var TAB_Y = 735;

  // [início, imagem, duração do crossfade]
  var IMG = [[0, 'media'], [4.65, 'media-tv'],
    [9.35, 'mixer'], [10.6, 'mixer-b', .8], [12.35, 'mixer-c'],
    [14.45, 'lol-teams'], [18.85, 'lol-build'],
    [23.45, 'fla'],
    [28.25, 'discord'], [30.4, 'discord-b', .25], [31.0, 'discord', .25], [31.6, 'discord-b', .25],
    [32.2, 'discord', .25], [32.8, 'discord-b', .25],
    [33.65, 'claude'], [35.95, 'claude-b'],
    [39.45, 'launcher'],
    [44.45, 'system']];

  // câmera dentro da tela: [t, zoom, centro x, centro y]
  var CAM = [[0, 1, 512, 384],
    [6.0, 1, 512, 384], [6.8, 1.45, 420, 112], [8.6, 1.45, 420, 112], [9.2, 1, 512, 384],
    [9.6, 1, 512, 384], [10.2, 1.45, 300, 420], [13.3, 1.45, 300, 420], [13.9, 1, 512, 384],
    [14.8, 1, 512, 384], [15.5, 1.25, 650, 400], [17.8, 1.25, 650, 400], [18.4, 1, 512, 384],
    [19.2, 1, 512, 384], [19.8, 1.35, 640, 330], [22.6, 1.35, 640, 330], [23.2, 1, 512, 384],
    [23.8, 1, 512, 384], [24.5, 1.55, 260, 360], [27.2, 1.55, 260, 360], [27.8, 1, 512, 384],
    [30.2, 1, 512, 384], [30.8, 1.4, 680, 380], [32.9, 1.4, 680, 380], [33.4, 1, 512, 384],
    [34.0, 1, 512, 384], [34.7, 1.6, 300, 330], [36.6, 1.6, 300, 330], [37.2, 1, 512, 384],
    [44.8, 1, 512, 384], [45.4, 1.45, 300, 420], [47.8, 1.45, 300, 420], [48.3, 1, 512, 384]];

  // [t, kicker, título (HTML)]; título null esconde
  var CAP = [[0, '', 'Seu iPad parado<br>na gaveta?', 92],
    [1.6, 'PC CONTROL CENTER', 'Ele vira o <span class="grad">painel do seu PC</span>'],
    [3.2, 'MÍDIA', 'Música, volume e <span class="grad">saída de áudio</span>'],
    [6.2, 'DESEMPENHO', 'CPU, GPU e <span class="grad">temperaturas</span> ao vivo'],
    [9.4, 'MIXER', 'Volume de <span class="grad">cada app</span>, separado'],
    [14.5, 'LEAGUE OF LEGENDS', 'Placar da partida <span class="grad">em tempo real</span>'],
    [18.9, 'LEAGUE OF LEGENDS', 'Build e runas <span class="grad">com 1 toque</span>'],
    [23.5, 'FUTEBOL', 'Seu time: <span class="grad">placar ao vivo</span> e próximo jogo'],
    [28.3, 'DISCORD', 'Entre no canal de voz <span class="grad">com 1 toque</span>'],
    [30.6, 'DISCORD', 'Veja <span class="grad">quem está falando</span>'],
    [33.7, 'CLAUDE CODE', 'Aprove o Claude Code <span class="grad">do sofá</span>'],
    [37.2, 'CLAUDE CODE', 'Sessões e <span class="grad">uso de tokens</span>'],
    [39.5, 'ATALHOS', 'Apps, sites e macros <span class="grad">num toque</span>'],
    [44.5, 'SISTEMA', 'Bloquear, desligar e <span class="grad">brilho</span>'],
    [46.0, 'SISTEMA', 'Segure para <span class="grad">confirmar</span>'],
    [48.8, 'GRÁTIS E CÓDIGO ABERTO', 'Instala com <span class="grad">um arquivo</span>'],
    [52.5, '', null]];

  // toques: tap | drag (até x2,y2 em d s) | hold (d s)
  var TAPS = [
    { t: 4.6, x: 673, y: 536 },
    { t: 9.3, x: TAB.mixer, y: TAB_Y },
    { t: 10.6, x: 202, y: 421, type: 'drag', x2: 202, y2: 347, d: .8 },
    { t: 12.3, x: 451, y: 597 },
    { t: 14.4, x: TAB.lol, y: TAB_Y },
    { t: 18.8, x: 438, y: 94 },
    { t: 20.6, x: 928, y: 147 },
    { t: 23.4, x: TAB.fla, y: TAB_Y },
    { t: 28.2, x: TAB.discord, y: TAB_Y },
    { t: 29.4, x: 165, y: 476 },
    { t: 33.6, x: TAB.claude, y: TAB_Y },
    { t: 35.9, x: 167, y: 361 },
    { t: 39.4, x: TAB.launcher, y: TAB_Y },
    { t: 40.7, x: 427, y: 349 }, { t: 41.6, x: 765, y: 349 }, { t: 42.5, x: 934, y: 349 }, { t: 43.3, x: 258, y: 485 },
    { t: 44.4, x: TAB.system, y: TAB_Y },
    { t: 45.8, x: 463, y: 574, type: 'hold', d: 1.4 }];

  var TERM = [[49.0, 'PS&gt; <span class="p">.\\start.bat</span>', true],
    [49.7, '<span class="d">Instalando dependências...</span> <span class="ok">ok</span>'],
    [50.0, ''], [50.0, 'Assistente de configuração'],
    [50.3, '  1/6  Rede ................. <span class="ok">ok</span>'],
    [50.55, '  2/6  Telemetria ........... <span class="ok">ok</span>'],
    [50.8, '  3/6  Abas opcionais ....... <span class="ok">ok</span>'],
    [51.05, '  4/6  Atalhos .............. <span class="ok">ok</span>'],
    [51.3, '  5/6  Integração Windows ... <span class="ok">ok</span>'],
    [51.55, '  6/6  Pronto!'], [51.8, ''],
    [51.8, 'Abra no Safari do iPad:'], [52.0, '  <span class="p">http://192.168.0.10:8765</span>']];

  // ------------------------------------------------------------------ desenho
  var imgA = $('imgA'), imgB = $('imgB'), cam = $('cam'), fx = $('fx'), dev = $('device');
  var cap = $('cap'), kicker = $('kicker'), title = $('title'), toast = $('toast');
  var term = $('term'), termText = $('term-text'), cta = $('cta');

  function setSrc(img, name) {
    if (img.dataset.name !== name) { img.src = 'data/' + name + '.png'; img.dataset.name = name; }
  }

  function drawImages(t) {
    var i = 0;
    while (i + 1 < IMG.length && IMG[i + 1][0] <= t) i++;
    var cur = IMG[i], prev = IMG[Math.max(0, i - 1)];
    var k = i === 0 ? 1 : prog(t, cur[0], cur[0] + (cur[2] || .22));
    setSrc(imgA, prev[1]); setSrc(imgB, cur[1]);
    imgB.style.opacity = k;
  }

  function drawCamera(t) {
    var i = 0;
    while (i + 1 < CAM.length && CAM[i + 1][0] <= t) i++;
    var a = CAM[i], b = CAM[Math.min(i + 1, CAM.length - 1)];
    var k = b[0] > a[0] ? ease(prog(t, a[0], b[0])) : 0;
    var s = lerp(a[1], b[1], k), cx = lerp(a[2], b[2], k), cy = lerp(a[3], b[3], k);
    var tx = clamp(512 - cx * s, 1024 - 1024 * s, 0), ty = clamp(384 - cy * s, 768 - 768 * s, 0);
    // a tela tem 960x720: escala 0.9375 do painel de 1024x768
    cam.style.transform = 'scale(0.9375) translate(' + tx + 'px,' + ty + 'px) scale(' + s + ')';
  }

  function drawCaption(t) {
    var i = -1;
    while (i + 1 < CAP.length && CAP[i + 1][0] <= t) i++;
    if (i < 0 || CAP[i][2] === null) { cap.style.opacity = 0; return; }
    var c = CAP[i], next = CAP[i + 1];
    var kin = easeOut(prog(t, c[0], c[0] + .38));
    var kout = next ? ease(prog(t, next[0] - .22, next[0])) : 0;
    if (kicker.innerHTML !== c[1]) kicker.innerHTML = c[1];
    if (title.dataset.v !== c[2]) { title.innerHTML = c[2]; title.dataset.v = c[2]; title.style.fontSize = (c[3] || 64) + 'px'; }
    cap.style.opacity = kin * (1 - kout);
    cap.style.transform = 'translateY(' + ((1 - kin) * 36 - kout * 20) + 'px)';
  }

  function finger(x, y, alpha, scale) {
    return '<div class="finger" style="left:' + x + 'px;top:' + y + 'px;opacity:' + alpha +
      ';transform:scale(' + scale + ')"></div>';
  }
  function ripple(x, y, k) {
    if (k <= 0 || k >= 1) return '';
    var r = lerp(20, 80, easeOut(k));
    return '<div class="ripple" style="left:' + (x - r) + 'px;top:' + (y - r) + 'px;width:' + 2 * r + 'px;height:' +
      2 * r + 'px;opacity:' + (1 - k) + '"></div>';
  }

  function drawTaps(t) {
    var html = '';
    TAPS.forEach(function (p) {
      var d = p.d || 0, start = p.t - .35, end = p.t + d + .45;
      if (t < start || t > end) return;
      var appear = easeOut(prog(t, start, p.t - .1)), leave = prog(t, p.t + d + .15, end);
      var alpha = appear * (1 - leave);
      var press = t >= p.t - .08 && t <= p.t + d + .12 ? .86 : 1;
      var x = p.x, y = p.y;
      if (p.type === 'drag') {
        var k = ease(prog(t, p.t, p.t + d));
        x = lerp(p.x, p.x2, k); y = lerp(p.y, p.y2, k);
      }
      if (p.type === 'hold' && t >= p.t) {
        var hk = prog(t, p.t, p.t + d);
        html += '<div class="ring" style="left:' + x + 'px;top:' + y + 'px;opacity:' + (1 - leave) +
          ';background:conic-gradient(#f43f5e ' + (hk * 360) + 'deg, rgba(255,255,255,.15) 0);' +
          '-webkit-mask:radial-gradient(circle, transparent 40px, #000 41px)"></div>';
      }
      html += finger(x, y, alpha * .92, press);
      html += ripple(p.type === 'drag' ? p.x2 : x, p.type === 'drag' ? p.y2 : y, prog(t, p.t + d, p.t + d + .5));
    });
    fx.innerHTML = html;
  }

  function drawDevice(t) {
    var kin = easeOut(prog(t, .9, 2.5));
    var kout = ease(prog(t, 48.3, 48.9));
    var y = (1 - kin) * 1250 + kout * 380;
    var rx = (1 - kin) * 28;
    var s = lerp(.86, 1, kin) - kout * .12;
    dev.style.transform = 'perspective(1800px) translateY(' + y + 'px) rotateX(' + rx + 'deg) scale(' + s + ')';
    dev.style.opacity = (kin > 0 ? 1 : 0) * (1 - kout);
  }

  function drawToast(t) {
    var a = easeOut(prog(t, 20.8, 21.1)) * (1 - prog(t, 22.2, 22.5));
    toast.style.opacity = a;
    toast.style.transform = 'translateX(-50%) translateY(' + (1 - a) * 30 + 'px)';
  }

  function drawTerm(t) {
    var a = easeOut(prog(t, 48.7, 49.1)) * (1 - ease(prog(t, 52.3, 52.7)));
    term.style.opacity = a;
    term.style.transform = 'translateY(' + (1 - a) * 60 + 'px)';
    var out = [];
    TERM.forEach(function (l) {
      if (t < l[0]) return;
      if (l[2]) {  // digitado
        var plain = 'PS> .\\start.bat', n = Math.floor(prog(t, l[0], l[0] + .55) * plain.length);
        var typed = plain.slice(0, n).replace('>', '&gt;');
        out.push(n >= plain.length ? l[1] : typed.replace(/^PS&gt; (.*)$/, 'PS&gt; <span class="p">$1</span>') +
          '<span class="p">▌</span>');
      } else out.push(l[1]);
    });
    termText.innerHTML = out.join('\n');
  }

  function drawCta(t) {
    var a = easeOut(prog(t, 52.8, 53.5));
    cta.style.opacity = a;
    cta.style.transform = 'translateY(' + (1 - a) * 50 + 'px) scale(' + lerp(.96, 1, a) + ')';
  }

  var STEPS = [['Mídia', 3.2], ['Mixer', 9.3], ['LoL', 14.4], ['Futebol', 23.4], ['Discord', 28.2], ['Claude', 33.6],
    ['Atalhos', 39.4], ['Sistema', 44.4], [null, 48.4]];
  var steps = $('steps');
  steps.innerHTML = STEPS.slice(0, -1).map(function (s) { return '<span>' + s[0] + '</span>'; }).join('');
  function drawSteps(t) {
    var a = easeOut(prog(t, 3.0, 3.5)) * (1 - ease(prog(t, 48.2, 48.6)));
    steps.style.opacity = a;
    Array.prototype.forEach.call(steps.children, function (el, i) {
      var on = t >= STEPS[i][1] && t < STEPS[i + 1][1];
      el.className = on ? 'on' : t >= STEPS[i + 1][1] ? 'done' : '';
    });
  }

  window.seek = function (t) {
    drawSteps(t); drawImages(t); drawCamera(t); drawCaption(t); drawTaps(t); drawDevice(t); drawToast(t); drawTerm(t); drawCta(t);
  };
  window.DURATION = DURATION;

  // pré-carrega todas as imagens; render.py espera window.ready
  var names = {}; IMG.forEach(function (e) { names[e[1]] = 1; });
  window.ready = Promise.all(Object.keys(names).map(function (n) {
    var im = new Image(); im.src = 'data/' + n + '.png';
    return im.decode();
  }).concat([document.fonts.ready, document.querySelector('#cta img').decode()]));

  if (/debug/.test(location.search)) {  // zonas cobertas pela interface do Instagram
    document.getElementById('stage').insertAdjacentHTML('beforeend',
      '<div class="safe" style="top:0;height:270px"></div><div class="safe" style="bottom:0;height:672px"></div>');
  }
  var m = /t=([\d.]+)/.exec(location.search);
  window.seek(m ? parseFloat(m[1]) : 0);
})();
