(function () {
  'use strict';
  var REPO = 'https://github.com/ianlpereira/ipad-pc-control';
  var $ = function (s) { return document.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(document.querySelectorAll(s)); };

  // ---------------------------------------------------------------- galeria
  var CAPTIONS = {
    media: 'Música atual com capa e controles, volume mestre e troca de saída de áudio com um toque. CPU, GPU, RAM, temperaturas e rede ficam sempre no topo.',
    mixer: 'Um fader para cada app tocando áudio, com mute individual. O volume mestre fica sempre na primeira coluna.',
    'lol-teams': 'Durante a partida: os dois times com nível, K/D/A, CS e valor de itens. Também há visões de rota, objetivos e eventos.',
    'lol-build': 'Build mais popular do seu campeão: runas (importa com um toque), feitiços, itens e ordem de habilidades.',
    fla: 'Placar ao vivo do seu time, com contagem para o próximo jogo e últimos resultados quando não tem partida.',
    discord: 'Canal de voz do grupo: entrar com um toque, quem está falando acende, mute e volume de cada amigo.',
    claude: 'Sessões do Claude Code, pedidos de permissão com Permitir/Negar e uso de tokens do dia e da semana.',
    launcher: 'Páginas de atalhos: apps, pastas, sites, scripts, teclas e macros, configurados no PC.',
    system: 'Bloquear, suspender, desligar (segure para confirmar), brilho dos monitores e escurecer o iPad à noite.'
  };
  var shot = $('#shot');
  Object.keys(CAPTIONS).forEach(function (k) { new Image().src = 'assets/shots/' + k + '.webp'; });  // pré-carrega
  $$('#shot-tabs button').forEach(function (b) {
    b.addEventListener('click', function () {
      $$('#shot-tabs button').forEach(function (x) { x.classList.toggle('on', x === b); });
      var k = b.dataset.shot;
      shot.classList.add('fade');
      setTimeout(function () {
        shot.src = 'assets/shots/' + k + '.webp';
        shot.alt = 'Painel do PC Control Center: ' + b.textContent;
        $('#shot-caption').textContent = CAPTIONS[k];
        shot.classList.remove('fade');
      }, 120);
    });
  });

  // ---------------------------------------------------------------- times (ids do ESPN)
  // [nome, id no ESPN, cor]. Atualizar: site.api.espn.com/apis/site/v2/sports/soccer/bra.1/teams (e bra.2)
  var TEAMS = {
    a: [['Athletico-PR', '3458', '#d80518'], ['Atlético-MG', '7632', '#e5e7eb'], ['Bahia', '9967', '#2563eb'],
      ['Botafogo', '6086', '#e5e7eb'], ['Bragantino', '6079', '#e5e7eb'], ['Chapecoense', '9318', '#22a34a'],
      ['Corinthians', '874', '#e5e7eb'], ['Coritiba', '3456', '#22a34a'], ['Cruzeiro', '2022', '#0093ec'],
      ['Flamengo', '819', '#e11d2a'], ['Fluminense', '3445', '#9b1c31'], ['Grêmio', '6273', '#0093ec'],
      ['Internacional', '1936', '#e11d2a'], ['Mirassol', '9169', '#fedc00'], ['Palmeiras', '2029', '#22a34a'],
      ['Remo', '4936', '#3b6fb6'], ['Santos', '2674', '#e5e7eb'], ['São Paulo', '2026', '#e11d2a'],
      ['Vasco', '3454', '#e5e7eb'], ['Vitória', '3457', '#e11d2a']],
    b: [['América-MG', '6154', '#22a34a'], ['Athletic', '20851', '#e5e7eb'], ['Atlético-GO', '10357', '#ef4444'],
      ['Avaí', '9966', '#0093ec'], ['Botafogo-SP', '10281', '#e5e7eb'], ['CRB', '9970', '#e11d2a'],
      ['Ceará', '9969', '#e5e7eb'], ['Criciúma', '9971', '#f8e71c'], ['Cuiabá', '17313', '#16a34a'],
      ['Fortaleza', '6272', '#2563eb'], ['Goiás', '3395', '#22a34a'], ['Juventude', '6270', '#22a34a'],
      ['Londrina', '17333', '#0093ec'], ['Náutico', '7633', '#e11d2a'], ['Novorizontino', '18127', '#facc15'],
      ['Operário-PR', '18187', '#e5e7eb'], ['Ponte Preta', '3459', '#e5e7eb'], ['São Bernardo', '11268', '#facc15'],
      ['Sport', '7635', '#e11d2a'], ['Vila Nova', '9973', '#e11d2a']]
  };
  var team = { name: 'Flamengo', id: '819' }, league = 'a';
  function renderTeams() {
    var box = $('#team-chips');
    box.classList.toggle('hidden', league === 'other');
    $('#other-team').classList.toggle('hidden', league !== 'other');
    if (league === 'other') return;
    box.innerHTML = TEAMS[league].map(function (t) {
      var on = t[1] === team.id;
      return '<button type="button" role="radio" aria-checked="' + on + '" class="team' + (on ? ' on' : '') +
        '" data-id="' + t[1] + '" data-name="' + t[0] + '"><i style="background:' + t[2] + '"></i>' + t[0] + '</button>';
    }).join('');
  }
  $('#team-chips').addEventListener('click', function (e) {
    var b = e.target.closest('button.team');
    if (!b) return;
    team = { name: b.dataset.name, id: b.dataset.id };
    renderTeams(); render();
  });
  $$('.league-tabs button').forEach(function (b) {
    b.addEventListener('click', function () {
      league = b.dataset.league;
      $$('.league-tabs button').forEach(function (x) { x.classList.toggle('on', x === b); });
      if (league === 'other') {
        $('#team-label').value = $('#team-label').value || team.name;
        $('#team-id').value = $('#team-id').value || team.id;
      }
      renderTeams(); render();
    });
  });

  // ---------------------------------------------------------------- configurador
  var outMode = 'cmd';
  function choices() {
    var feats = {};
    $$('#feat-chips input').forEach(function (i) { feats[i.value] = i.checked; });
    var other = league === 'other';
    return {
      feats: feats,
      team: other ? ($('#team-label').value || 'Meu time').trim() : team.name,
      teamId: other ? (($('#team-id').value || '').replace(/\D/g, '') || '819') : team.id,
      token: $('#opt-token').checked,
      autostart: $('#opt-autostart').checked,
      lhm: $('#opt-lhm').checked
    };
  }
  var NAMES = { lol: 'League of Legends', football: 'futebol', discord: 'Discord', claude: 'Claude Code' };

  function yesNo(v) { return v ? 'sim' : 'não'; }

  function commands(c) {
    var L = [];
    L.push('# 1. Python 3.13 (pule se já tiver o Python 3.11+ instalado)');
    L.push('winget install -e --id Python.Python.3.13');
    L.push('');
    L.push('# 2. Baixe o projeto (ou use Code > Download ZIP no GitHub)');
    L.push('cd $HOME\\Documents');
    L.push('git clone ' + REPO + '.git');
    L.push('cd ipad-pc-control');
    if (c.lhm) {
      L.push('');
      L.push('# 3. (opcional) LibreHardwareMonitor para temperaturas e GPU');
      L.push('winget install -e --id LibreHardwareMonitor.LibreHardwareMonitor');
    }
    L.push('');
    L.push('# ' + (c.lhm ? '4' : '3') + '. Instala, abre o assistente e inicia o servidor');
    L.push('.\\start.bat');
    L.push('');
    L.push('# Respostas para o assistente:');
    L.push('#   Exigir token de acesso? ' + yesNo(c.token));
    Object.keys(NAMES).forEach(function (k) { L.push('#   Ativar a aba ' + (k === 'football' ? 'de futebol' : NAMES[k]) + '? ' + yesNo(c.feats[k])); });
    if (c.feats.football) L.push('#   Time: ' + c.team + ' (id ' + c.teamId + ' no ESPN; escolha na lista ou digite o id)');
    L.push('#   Liberar a porta no firewall? sim');
    L.push('#   Iniciar junto com o Windows? ' + yesNo(c.autostart));
    return L.join('\n');
  }

  function prompt(c) {
    var on = Object.keys(NAMES).filter(function (k) { return c.feats[k]; }).map(function (k) { return NAMES[k]; });
    var L = [];
    L.push('Quero instalar o PC Control Center no meu PC com Windows para usar o meu iPad como painel de controle.');
    L.push('Aja como um técnico paciente: conduza UMA ETAPA CURTA POR VEZ, em português simples, e espere minha resposta antes do próximo passo.');
    L.push('');
    L.push('Minhas escolhas:');
    L.push('- Repositório: ' + REPO);
    L.push('- Abas extras: ' + (on.length ? on.join(', ') : 'nenhuma (só Mídia, Mixer, Atalhos, Janelas e Sistema)'));
    if (c.feats.football) L.push('- Time de futebol: ' + c.team + ' (id do ESPN: ' + c.teamId + ')');
    if (c.feats.discord) L.push('- Discord: me ajude a criar o app no portal de desenvolvedores e a pegar o ID do canal de voz do grupo');
    if (c.feats.claude) L.push('- Claude Code: instalar os hooks de alerta (com backup do settings.json)');
    L.push('- Exigir token de acesso: ' + yesNo(c.token));
    L.push('- Temperaturas e GPU com LibreHardwareMonitor: ' + yesNo(c.lhm));
    L.push('- Iniciar junto com o Windows: ' + yesNo(c.autostart));
    L.push('');
    L.push('Como começar:');
    L.push('1. Confira se tenho Python 3.11 a 3.13 e git; se faltar, instale (winget) e me avise.');
    L.push('2. Clone o repositório em Documentos e leia o README.md.');
    L.push('3. Me peça para rodar o start.bat numa janela própria (o assistente é interativo) e me diga o que responder em cada pergunta, de acordo com as escolhas acima.');
    L.push('4. No fim, confira se a rede está como Privada e me explique como abrir a URL no Safari do iPad e adicionar à Tela de Início.');
    return L.join('\n');
  }

  function render() {
    var c = choices();
    $('#q-team').classList.toggle('off', !c.feats.football);
    var text = outMode === 'cmd' ? commands(c) : prompt(c);
    var pre = $('#out');
    if (outMode === 'cmd') {
      pre.innerHTML = text.split('\n').map(function (line) {
        var safe = line.replace(/&/g, '&amp;').replace(/</g, '&lt;');
        return line.charAt(0) === '#' ? '<span class="c">' + safe + '</span>' : safe;
      }).join('\n');
    } else {
      pre.textContent = text;
    }
    pre.dataset.raw = text;
    $('#out-title').textContent = outMode === 'cmd' ? 'Cole no PowerShell, uma linha por vez' : 'Cole no Claude Code';
    $('#out-hint').textContent = outMode === 'cmd'
      ? 'Na primeira execução o start.bat instala as dependências (1 a 3 minutos) e abre o assistente. Enter aceita o valor sugerido.'
      : 'Cole no Claude Code (terminal ou app) e aperte Enter. Ele te guia um passo curto por vez.';
  }

  $$('.config input').forEach(function (i) { i.addEventListener('input', render); i.addEventListener('change', render); });
  $$('.out-tabs button').forEach(function (b) {
    b.addEventListener('click', function () {
      outMode = b.dataset.out;
      $$('.out-tabs button').forEach(function (x) { x.classList.toggle('on', x === b); });
      render();
    });
  });
  $('#copy').addEventListener('click', function () {
    var btn = this, text = $('#out').dataset.raw;
    function done() { btn.textContent = 'Copiado!'; setTimeout(function () { btn.textContent = 'Copiar'; }, 1600); }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done);
    } else {
      var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); done(); } catch (e) { /* sem suporte */ }
      document.body.removeChild(ta);
    }
  });
  renderTeams();
  render();
})();
