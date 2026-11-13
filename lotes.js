'use strict';
const $ = (id) => document.getElementById(id);
const api = '/api/lotes';
const RESUME_KEY = 'iptu-upload-pendente';
const ACCEPTED = /\.(pdf|zip|jpe?g|png|webp|html?)$/i;
// Rótulos de exibição. Os códigos internos (enquadra, em_analise…) ficam inalterados na exportação.
const RESULT = {
  enquadra: 'Aceito', enquadra_conferir_cadastro: 'Aceito · conferir cadastro', nao_enquadra: 'Não aceito',
  em_analise: 'Revisão manual', fora_do_fluxo: 'Fora do fluxo', processando: 'Processando', erro: 'Erro de leitura',
};
const DECISION = {deferido: 'Aceito', indeferido: 'Não aceito', pendente: 'Revisão manual',
  fora_do_fluxo: 'Fora do fluxo', processando: 'Processando'};
const DECISION_ICON = {deferido: ['✓', 'i-good'], indeferido: ['✕', 'i-critical'], pendente: ['!', 'i-warning'],
  fora_do_fluxo: ['→', 'i-neutral'], processando: ['…', 'i-neutral']};
const AI_TO_DECISION = {enquadra: 'deferido', enquadra_conferir_cadastro: 'deferido', nao_enquadra: 'indeferido',
  em_analise: 'pendente', fora_do_fluxo: 'fora_do_fluxo', processando: 'processando'};
const MARK = {atendido: '✓', impedimento: '✕', pendente: '!', cadastral: '○', aviso: 'i', fora_do_fluxo: '→'};
const MARK_CLASS = {atendido: 'i-good', impedimento: 'i-critical', pendente: 'i-warning', cadastral: 'i-neutral',
  aviso: 'i-neutral', fora_do_fluxo: 'i-neutral'};
const LABEL = {
  modalidade: 'Modalidade', identidade: 'Identificação', beneficio: 'Benefício', renda: 'Renda familiar',
  residencia: 'Residência', 'edícula': 'Edícula e uso', imovel: 'Vínculo com o imóvel', unicidade: 'Imóvel único',
  mobiliario: 'Atividade econômica', prazo: 'Protocolo e prazo', exercicio: 'Exercício',
  dupla_leitura: 'Dupla leitura do requerimento',
  processo_sei: 'Processo SEI', leitura: 'Leitura', processamento: 'Processamento', requerimento: 'Requerimento',
};
let currentBatch = '';   // filtro de lote da tabela (cabeçalho)
let trackedBatch = '';   // lote cujo progresso aparece na área de envio
let currentProcess = '';
let detailBatch = '';
let detailData = null;
let uploading = false;

function badge(decision, text) {
  const [icon, cls] = DECISION_ICON[decision] || ['·', 'i-neutral'];
  return el('span', {class: 'badge b-' + decision}, el('span', {class: 'icon ' + cls, 'aria-hidden': 'true'}, icon),
    text || DECISION[decision] || decision);
}

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.status || 'HTTP ' + response.status);
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child != null));
  return node;
}

function money(cents) {
  return cents == null ? '' : (cents / 100).toLocaleString('pt-BR', {style: 'currency', currency: 'BRL'});
}

/* ---------- Etapas ---------- */
function step(name, text, value) {
  $('job').classList.add('on');
  const order = ['preparar', 'enviar', 'ler', 'pronto'];
  for (const span of document.querySelectorAll('.steps span')) {
    const index = order.indexOf(span.dataset.step);
    span.className = index < order.indexOf(name) ? 'done' : index === order.indexOf(name) ? 'now' : '';
  }
  if (name === 'pronto') document.querySelector('[data-step="pronto"]').className = 'done';
  $('job-text').textContent = text;
  if (value != null) $('progress-bar').style.width = Math.round(100 * Math.min(1, Math.max(0, value))) + '%';
}

/* ---------- Pasta → lista de arquivos ---------- */
function readEntries(reader) {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

async function walk(entry, prefix, out) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    out.push({path: prefix + entry.name, file});
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    for (let batch = await readEntries(reader); batch.length; batch = await readEntries(reader)) {
      for (const child of batch) await walk(child, prefix + entry.name + '/', out);
    }
  }
}

function usable(items) {
  return items.filter(({path}) => {
    const parts = path.split('/');
    const name = parts[parts.length - 1];
    // Documentos do SEI exportados sem extensão (despachos em HTML) também entram.
    return (ACCEPTED.test(name) || !name.includes('.')) && !parts.some((part) => part.startsWith('.') || part === '__MACOSX');
  });
}

/* ---------- ZIP sem compressão, montado sob demanda (ZIP64 quando necessário) ---------- */
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

async function crc32(file, onBytes) {
  let crc = 0xFFFFFFFF;
  const reader = file.stream().getReader();
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    for (let i = 0; i < value.length; i++) crc = CRC_TABLE[(crc ^ value[i]) & 0xFF] ^ (crc >>> 8);
    onBytes(value.length);
  }
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

function dosTime(date) {
  return {
    time: (date.getHours() << 11) | (date.getMinutes() << 5) | (date.getSeconds() >> 1),
    date: ((Math.max(1980, date.getFullYear()) - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate(),
  };
}

function bytes(size, fill) {
  const buffer = new ArrayBuffer(size);
  fill(new DataView(buffer));
  return new Uint8Array(buffer);
}

async function buildZip(items, onProgress) {
  const encoder = new TextEncoder();
  const parts = [];
  const central = [];
  const total = items.reduce((sum, item) => sum + item.file.size, 0);
  let offset = 0;
  let hashed = 0;
  for (const {path, file} of items) {
    if (file.size >= 0xFFFFFFFF) throw new Error('arquivo maior que 4 GB: ' + path);
    const name = encoder.encode(path);
    const crc = await crc32(file, (n) => { hashed += n; onProgress(hashed / Math.max(total, 1)); });
    const {time, date} = dosTime(new Date(file.lastModified || Date.now()));
    parts.push(bytes(30, (v) => {
      v.setUint32(0, 0x04034b50, true); v.setUint16(4, 45, true); v.setUint16(6, 0x0800, true);
      v.setUint16(8, 0, true); v.setUint16(10, time, true); v.setUint16(12, date, true);
      v.setUint32(14, crc, true); v.setUint32(18, file.size, true); v.setUint32(22, file.size, true);
      v.setUint16(26, name.length, true); v.setUint16(28, 0, true);
    }), name, file);
    const big = offset >= 0xFFFFFFFF;
    const extra = big ? bytes(12, (v) => {
      v.setUint16(0, 0x0001, true); v.setUint16(2, 8, true); v.setBigUint64(4, BigInt(offset), true);
    }) : new Uint8Array(0);
    central.push(bytes(46, (v) => {
      v.setUint32(0, 0x02014b50, true); v.setUint16(4, 45, true); v.setUint16(6, 45, true);
      v.setUint16(8, 0x0800, true); v.setUint16(10, 0, true); v.setUint16(12, time, true);
      v.setUint16(14, date, true); v.setUint32(16, crc, true); v.setUint32(20, file.size, true);
      v.setUint32(24, file.size, true); v.setUint16(28, name.length, true); v.setUint16(30, extra.length, true);
      v.setUint32(42, big ? 0xFFFFFFFF : offset, true);
    }), name, extra);
    offset += 30 + name.length + file.size;
  }
  const centralSize = central.reduce((sum, part) => sum + part.length, 0);
  const zip64 = offset >= 0xFFFFFFFF || items.length >= 0xFFFF;
  const tail = [];
  if (zip64) {
    tail.push(bytes(56, (v) => {
      v.setUint32(0, 0x06064b50, true); v.setBigUint64(4, 44n, true); v.setUint16(12, 45, true);
      v.setUint16(14, 45, true); v.setBigUint64(24, BigInt(items.length), true);
      v.setBigUint64(32, BigInt(items.length), true); v.setBigUint64(40, BigInt(centralSize), true);
      v.setBigUint64(48, BigInt(offset), true);
    }), bytes(20, (v) => {
      v.setUint32(0, 0x07064b50, true); v.setBigUint64(8, BigInt(offset + centralSize), true);
      v.setUint32(16, 1, true);
    }));
  }
  tail.push(bytes(22, (v) => {
    v.setUint32(0, 0x06054b50, true);
    v.setUint16(8, zip64 ? 0xFFFF : items.length, true); v.setUint16(10, zip64 ? 0xFFFF : items.length, true);
    v.setUint32(12, centralSize, true); v.setUint32(16, zip64 ? 0xFFFFFFFF : offset, true);
  }));
  return new Blob([...parts, ...central, ...tail], {type: 'application/zip'});
}

/* ---------- Envio retomável ---------- */
const MAX_PART = 4 * 1024 * 1024;   // parte máxima
const MIN_PART = 256 * 1024;        // parte mínima em conexão instável
const UPLOAD_STALL = 30 * 1000;     // bytes parados no meio do envio: problema de rede
const REPLY_WAIT = 120 * 1000;      // parte inteira enviada, aguardando o servidor gravar
const RETRIES = 12;                 // falhas seguidas antes de desistir (zera a cada parte aceita)

function sendPart(id, offset, data, onProgress, onWait) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let last = performance.now();
    let sent = false;  // corpo inteiro já saiu do navegador
    let reason = '';
    const watch = setInterval(() => {
      const idle = performance.now() - last;
      if (idle > 3000) onWait(Math.round(idle / 1000), sent);
      if (idle > (sent ? REPLY_WAIT : UPLOAD_STALL)) {
        reason = sent ? 'servidor sem resposta por ' + REPLY_WAIT / 1000 + ' s'
                      : 'envio parado por ' + UPLOAD_STALL / 1000 + ' s';
        xhr.abort();
      }
    }, 1000);
    const done = (fn, value) => { clearInterval(watch); fn(value); };
    xhr.open('POST', api + '/' + id + '/parte');
    xhr.setRequestHeader('Upload-Offset', String(offset));
    xhr.upload.onprogress = (event) => { last = performance.now(); onProgress(event.loaded); };
    xhr.upload.onload = () => { sent = true; last = performance.now(); };
    xhr.onabort = () => done(reject, Object.assign(new Error(reason || 'cancelado'), {network: !sent}));
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* resposta não JSON (proxy) */ }
      if (xhr.status === 200 && body) done(resolve, body);
      else done(reject, Object.assign(new Error(body?.status || 'HTTP ' + xhr.status), {status: xhr.status, network: xhr.status >= 500 || xhr.status === 413 || xhr.status === 408}));
    };
    xhr.onerror = () => done(reject, Object.assign(new Error('conexão interrompida'), {network: true}));
    xhr.send(data);
  });
}

function duration(seconds) {
  if (!isFinite(seconds)) return '';
  return seconds < 90 ? Math.round(seconds) + ' s' : Math.round(seconds / 60) + ' min';
}

async function send(blob, name, resumeKey) {
  step('enviar', 'Conectando ao servidor…', 0);
  const saved = JSON.parse(localStorage.getItem(RESUME_KEY) || 'null');
  let lot = null;
  if (saved?.key === resumeKey) lot = await request(api + '/' + saved.id).catch(() => null);
  if (!lot || lot.status !== 'recebendo') {
    lot = await request(api, {method: 'POST', body: JSON.stringify({nome: name, bytes: blob.size})});
    localStorage.setItem(RESUME_KEY, JSON.stringify({key: resumeKey, id: lot.id}));
  }
  const mb = (bytes) => (bytes / 1048576).toLocaleString('pt-BR', {maximumFractionDigits: 1}) + ' MB';
  const size = (bytes) => bytes >= 1048576 ? mb(bytes) : Math.round(bytes / 1024) + ' KB';
  const started = performance.now();
  const startOffset = lot.recebido || 0;
  let offset = startOffset;
  let part = MAX_PART;
  let ceiling = MAX_PART;  // limite aprendido (ex.: proxy que recusa corpo grande com 413)
  let failures = 0;
  let streak = 0;
  const note = () => (part < MAX_PART ? (ceiling < MAX_PART ? ' · limite do servidor, partes de '
    : ' · conexão instável, partes de ') + size(part) : '')
    + (failures ? ' · tentativa ' + (failures + 1) + ' de ' + (RETRIES + 1) : '');
  const show = (sent) => {
    const elapsed = (performance.now() - started) / 1000;
    const speed = (sent - startOffset) / Math.max(elapsed, 0.001);
    const left = speed > 0 ? (blob.size - sent) / speed : Infinity;
    step('enviar', 'Enviando ' + mb(sent) + ' de ' + mb(blob.size) + ' · ' + Math.round(100 * sent / blob.size) + '%'
      + (elapsed > 2 && speed > 0 ? ' · ' + mb(speed) + '/s · faltam ' + duration(left) : '') + note(), sent / blob.size);
  };
  show(offset);
  while (offset < blob.size) {
    try {
      const result = await sendPart(lot.id, offset, blob.slice(offset, offset + part), (loaded) => show(offset + loaded),
        (seconds, sent) => step('enviar', 'Enviando ' + mb(offset) + ' de ' + mb(blob.size) + ' · '
          + (sent ? 'servidor gravando a parte há ' : 'envio parado há ') + seconds + ' s' + note()));
      offset = result.recebido;
      failures = 0;
      // Conexão estável de novo: volta a aumentar as partes aos poucos.
      if (++streak >= 3 && part < ceiling) { part = Math.min(ceiling, part * 2); streak = 0; }
      show(offset);
    } catch (error) {
      failures += 1;
      streak = 0;
      if (error.network !== false) part = Math.max(MIN_PART, Math.floor(part / 2));
      if (error.status === 413) ceiling = part;
      if (failures > RETRIES) {
        throw new Error(error.message + ' (parte em ' + mb(offset) + ', ' + RETRIES + ' falhas seguidas). '
          + 'Selecione a mesma pasta para continuar de onde parou');
      }
      const wait = Math.min(30, 2 * failures);
      step('enviar', 'Falha no envio (' + error.message + '). Tentando de novo em ' + wait + ' s' + note());
      await new Promise((resolve) => setTimeout(resolve, wait * 1000));
      // O servidor informa até onde recebeu; retoma desse ponto (cobre a parte que chegou sem resposta).
      const current = await request(api + '/' + lot.id).catch(() => null);
      if (current?.status === 'recebendo') offset = current.recebido;
      else if (current) break;  // já completo
    }
  }
  step('enviar', 'Envio concluído. Iniciando a leitura…', 1);
  await request(api + '/' + lot.id + '/processar', {method: 'POST'});
  localStorage.removeItem(RESUME_KEY);
  return lot.id;
}

async function start(items, label) {
  if (uploading) return;
  items = usable(items);
  if (!items.length) {
    step('preparar', 'Nenhum PDF, ZIP ou foto encontrado nessa pasta.', 0);
    return;
  }
  uploading = true;
  $('pick-folder').disabled = true;
  try {
    let blob, name;
    if (items.length === 1 && !items[0].path.includes('/')) {
      blob = items[0].file;
      name = items[0].file.name;
    } else {
      step('preparar', 'Preparando ' + items.length + ' arquivo(s) de "' + label + '"…', 0);
      blob = await buildZip(items, (fraction) =>
        step('preparar', 'Preparando ' + items.length + ' arquivo(s) · ' + Math.round(100 * fraction) + '%', fraction));
      name = label + '.zip';
    }
    const key = JSON.stringify([name, blob.size, items.length, Math.max(...items.map((i) => i.file.lastModified || 0))]);
    const id = await send(blob, name, key);
    step('ler', 'Lendo os documentos no servidor…', 0);
    await refreshBatches();
    trackedBatch = id;
    refreshLots();
    refreshTable();
  } catch (error) {
    step('enviar', 'Falha: ' + error.message + '. Selecione a mesma pasta para retomar o envio.');
  } finally {
    uploading = false;
    $('pick-folder').disabled = false;
  }
}

$('pick-folder').onclick = () => $('folder-input').click();
$('pick-file').onclick = () => $('file-input').click();
$('folder-input').onchange = (event) => {
  const files = [...event.target.files];
  event.target.value = '';
  const label = files[0]?.webkitRelativePath.split('/')[0] || 'pasta';
  start(files.map((file) => ({path: file.webkitRelativePath || file.name, file})), label);
};
$('file-input').onchange = (event) => {
  const file = event.target.files[0];
  event.target.value = '';
  if (file) start([{path: file.name, file}], file.name);
};
const drop = $('drop');
drop.ondragover = (event) => { event.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = async (event) => {
  event.preventDefault();
  drop.classList.remove('over');
  const entries = [...event.dataTransfer.items].map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  const items = [];
  for (const entry of entries) await walk(entry, '', items);
  const label = entries.length === 1 ? entries[0].name.replace(/\.[^.]+$/, '') : 'processos';
  start(items, label);
};

/* ---------- Painel: indicadores e gráficos ---------- */
const SVGNS = 'http://www.w3.org/2000/svg';
const STATUS_FILL = {aceitos: 'var(--good)', nao_aceitos: 'var(--critical)', revisao_manual: 'var(--warning)',
  fora_do_fluxo: 'var(--neutral)', processando: 'var(--line-strong)'};
const STATUS_NAME = {aceitos: 'Aceito', nao_aceitos: 'Não aceito', revisao_manual: 'Revisão manual', fora_do_fluxo: 'Fora do fluxo',
  processando: 'Em leitura'};

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if ((key === 'fill' || key === 'stroke') && String(value).startsWith('var(')) node.style[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.filter(Boolean));
  return node;
}
function number(value) { return value == null ? '—' : value.toLocaleString('pt-BR'); }
function compact(value) {
  if (value == null) return '—';
  const fmt = (v) => v.toLocaleString('pt-BR', {maximumFractionDigits: 1});
  return value >= 1e9 ? fmt(value / 1e9) + ' bi' : value >= 1e6 ? fmt(value / 1e6) + ' mi'
    : value >= 1e3 ? fmt(value / 1e3) + ' mil' : number(value);
}
function seconds(ms) {
  if (ms == null) return '—';
  const s = ms / 1000;
  return s < 90 ? s.toLocaleString('pt-BR', {maximumFractionDigits: 1}) + ' s'
    : (s / 60).toLocaleString('pt-BR', {maximumFractionDigits: 1}) + ' min';
}
function niceMax(value) {
  if (value <= 4) return 4;
  const step = Math.pow(10, Math.floor(Math.log10(value)));
  return Math.ceil(value / step) * step;
}
function tooltip(container) {
  let tip = container.querySelector('.tooltip');
  if (!tip) { tip = el('div', {class: 'tooltip', role: 'status'}); container.append(tip); }
  return tip;
}

function lotLabel(lot) {
  const name = (lot.nome || '').replace(/\.zip$/i, '');
  return name.length > 12 ? name.slice(0, 11) + '…' : name;
}
function lotTitle(lot) {
  return (lot.nome || '') + (lot.criado_em ? ' · enviado em ' + formatDate(lot.criado_em) : '');
}

function drawVolume(lots) {
  const box = $('chart-volume');
  const rows = lots;
  const keys = ['aceitos', 'nao_aceitos', 'revisao_manual', 'fora_do_fluxo', 'processando'];
  const totals = rows.map((row) => keys.reduce((sum, key) => sum + row[key], 0));
  if (!totals.some(Boolean)) {
    box.replaceChildren(el('div', {class: 'chart-empty'}, 'Nenhum envio analisado ainda.'));
    $('table-volume').replaceChildren();
    return;
  }
  const W = 640, H = 190, left = 34, bottom = 22, top = 8, plotH = H - bottom - top;
  const max = niceMax(Math.max(...totals));
  const band = (W - left) / rows.length, barW = Math.max(3, Math.min(24, band * 0.6));
  const chart = svg('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Processos analisados por dia, por decisão'});
  for (const tick of [0, max / 2, max]) {
    const y = top + plotH - (tick / max) * plotH;
    chart.append(svg('line', {x1: left, x2: W, y1: y, y2: y, stroke: 'var(--line)', 'stroke-width': 1}),
      svg('text', {x: left - 6, y: y + 4, 'text-anchor': 'end', 'font-size': 11, 'font-family': 'IBM Plex Mono, monospace', fill: 'var(--ink-3)'}, number(Math.round(tick))));
  }
  const tip = tooltip(box);
  rows.forEach((row, index) => {
    const x = left + index * band + (band - barW) / 2;
    let y = top + plotH;
    const present = keys.filter((key) => row[key] > 0);
    present.forEach((key, position) => {
      const h = (row[key] / max) * plotH;
      const gap = position > 0 ? 2 : 0;  // 2px de superfície entre segmentos
      const isTop = position === present.length - 1;
      const height = Math.max(0, h - gap);
      y -= h;
      const r = isTop ? Math.min(4, height) : 0;
      // segmento com cantos de 4px apenas no topo da coluna
      const d = `M${x},${y + gap + height} V${y + gap + r} Q${x},${y + gap} ${x + r},${y + gap} H${x + barW - r}`
        + ` Q${x + barW},${y + gap} ${x + barW},${y + gap + r} V${y + gap + height} Z`;
      chart.append(svg('path', {d, fill: STATUS_FILL[key]}));
    });
    if (totals[index] && (index === rows.length - 1 || index === totals.indexOf(Math.max(...totals)))) {
      chart.append(svg('text', {x: x + barW / 2, y: top + plotH - (totals[index] / max) * plotH - 5, 'text-anchor': 'middle',
        'font-size': 11, 'font-weight': 600, fill: 'var(--ink)'}, number(totals[index])));
    }
    const step = Math.ceil(rows.length / 7);
    const labeled = rows.length <= 8 || index === rows.length - 1 || (index % step === 0 && index < rows.length - step);
    // com muitos envios, rótulos curtos (número do envio na ordem) para caber no eixo
    if (labeled && rows.length > 16) {
      chart.append(svg('text', {x: x + barW / 2, y: H - 6, 'text-anchor': 'middle', 'font-size': 11,
        'font-family': 'IBM Plex Mono, monospace', fill: 'var(--ink-3)'}, '#' + (index + 1)));
    }
    if (labeled && rows.length <= 16) {
      chart.append(svg('text', {x: x + barW / 2, y: H - 6, 'text-anchor': 'middle', 'font-size': 11, 'font-family': 'IBM Plex Mono, monospace', fill: 'var(--ink-3)'}, lotLabel(row)));
    }
    const hit = svg('rect', {x: left + index * band, y: top, width: band, height: plotH, fill: 'transparent'});
    hit.addEventListener('mouseenter', () => {
      tip.replaceChildren(el('b', {}, '#' + (index + 1) + ' · ' + lotTitle(row)), el('div', {}, number(totals[index]) + ' processos'),
        ...keys.filter((key) => row[key]).map((key) => el('div', {}, STATUS_NAME[key] + ': ' + number(row[key]))));
      tip.style.left = ((x + barW / 2) / W * 100) + '%';
      tip.style.top = ((top + plotH - (totals[index] / max) * plotH) / H * 190) + 'px';
      tip.style.display = totals[index] ? 'block' : 'none';
    });
    hit.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
    chart.append(hit);
  });
  box.replaceChildren(chart, tip);
  $('table-volume').replaceChildren(el('table', {},
    el('thead', {}, el('tr', {}, ...['Envio', 'Aceitos', 'Não aceitos', 'Revisão manual', 'Fora do fluxo', 'Em leitura'].map((h) => el('th', {}, h)))),
    el('tbody', {}, ...rows.filter((row, i) => totals[i]).map((row) => el('tr', {}, el('td', {}, lotTitle(row)),
      ...keys.map((key) => el('td', {}, number(row[key]))))))));
}

function drawLine(boxId, tableId, lots, spec) {
  // Uma série por envio, na ordem de envio (tempo de IA por cidadão ou tokens).
  const box = $(boxId);
  const days = lots;
  const points = lots.map((lot, index) => ({lot, index, ms: spec.value(lot)})).filter((p) => p.ms != null);
  const seconds = spec.format;
  if (!points.length) {
    box.replaceChildren(el('div', {class: 'chart-empty'}, spec.empty));
    $(tableId).replaceChildren();
    return;
  }
  const W = 480, H = 190, left = 44, bottom = 22, top = 12, right = 12, plotH = H - bottom - top;
  const max = spec.axisMax(Math.max(...points.map((p) => p.ms)));
  const xAt = (index) => days.length === 1 ? left + (W - left - right) / 2
    : left + (index / Math.max(1, days.length - 1)) * (W - left - right);
  const yAt = (ms) => top + plotH - (ms / max) * plotH;
  const chart = svg('svg', {viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': spec.label});
  for (const tick of [0, max / 2, max]) {
    chart.append(svg('line', {x1: left, x2: W - right, y1: yAt(tick), y2: yAt(tick), stroke: 'var(--line)', 'stroke-width': 1}),
      svg('text', {x: left - 6, y: yAt(tick) + 4, 'text-anchor': 'end', 'font-size': 11, 'font-family': 'IBM Plex Mono, monospace', fill: 'var(--ink-3)'}, seconds(tick)));
  }
  chart.append(svg('path', {d: points.map((p, i) => (i ? 'L' : 'M') + xAt(p.index) + ',' + yAt(p.ms)).join(' '),
    fill: 'none', stroke: 'var(--accent)', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'}));
  const cross = svg('line', {y1: top, y2: top + plotH, stroke: 'var(--line-strong)', 'stroke-width': 1, visibility: 'hidden'});
  chart.append(cross);
  for (const p of points) {
    chart.append(svg('circle', {cx: xAt(p.index), cy: yAt(p.ms), r: days.length > 30 ? 3 : 4, fill: 'var(--accent)', stroke: 'var(--surface)', 'stroke-width': 2}));
  }
  const last = points[points.length - 1];
  chart.append(svg('text', {x: xAt(last.index), y: yAt(last.ms) - 9, 'text-anchor': 'end', 'font-size': 11, 'font-weight': 600,
    fill: 'var(--ink)'}, seconds(last.ms)));
  const shortLabel = (lot) => days.length > 16 ? '#' + (days.indexOf(lot) + 1) : lotLabel(lot);
  for (const index of [...new Set([0, Math.floor((days.length - 1) / 2), days.length - 1])]) {
    chart.append(svg('text', {x: xAt(index), y: H - 6, 'text-anchor': days.length === 1 ? 'middle' : index ? (index === days.length - 1 ? 'end' : 'middle') : 'start',
      'font-size': 11, 'font-family': 'IBM Plex Mono, monospace', fill: 'var(--ink-3)'}, shortLabel(days[index])));
  }
  const tip = tooltip(box);
  const overlay = svg('rect', {x: left, y: top, width: W - left - right, height: plotH, fill: 'transparent'});
  overlay.addEventListener('mousemove', (event) => {
    const rect = overlay.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    const span = Math.max(1, days.length - 1);
    const nearest = points.reduce((best, p) => Math.abs(p.index / span - ratio) < Math.abs(best.index / span - ratio) ? p : best);
    cross.setAttribute('x1', xAt(nearest.index)); cross.setAttribute('x2', xAt(nearest.index));
    cross.setAttribute('visibility', 'visible');
    tip.replaceChildren(el('b', {}, '#' + (nearest.index + 1) + ' · ' + lotTitle(nearest.lot)), ...spec.tooltip(nearest.lot));
    tip.style.left = (xAt(nearest.index) / W * 100) + '%';
    tip.style.top = (yAt(nearest.ms) / H * 190) + 'px';
    tip.style.display = 'block';
  });
  overlay.addEventListener('mouseleave', () => { tip.style.display = 'none'; cross.setAttribute('visibility', 'hidden'); });
  chart.append(overlay);
  box.replaceChildren(chart, tip);
  $(tableId).replaceChildren(el('table', {}, el('thead', {}, el('tr', {}, el('th', {}, 'Envio'),
    ...spec.columns.map(([title]) => el('th', {}, title)))),
    el('tbody', {}, ...points.map((p) => el('tr', {}, el('td', {}, lotTitle(p.lot)),
      ...spec.columns.map(([, cell]) => el('td', {}, cell(p.lot))))))));
}

async function refreshDashboard() {
  const d = await request('/api/painel?' + new URLSearchParams(currentBatch ? {lote: currentBatch} : {}));
  const pct = (value) => d.processados ? Math.round(100 * value / d.processados) + '%' : '';
  const recalc = d.recalculando;
  $('recalc').hidden = !recalc;
  if (recalc) {
    $('recalc').replaceChildren(el('span', {}, 'Atualizando os pareceres com as regras novas: ' + number(recalc.feitos)
      + ' de ' + number(recalc.total) + ' processos. Números e lista se ajustam ao terminar.'),
      el('div', {class: 'bar'}, el('i', {style: 'width:' + Math.round(100 * recalc.feitos / Math.max(1, recalc.total)) + '%'})));
  }
  $('k-processados').textContent = number(d.processados);
  $('k-processados-sub').textContent = (d.processando ? number(d.processando) + ' em leitura · ' : '')
    + number(d.com_revisao_humana) + ' com revisão humana';
  $('k-aceitos').textContent = number(d.aceitos);
  $('k-aceitos-sub').textContent = [pct(d.aceitos), number(d.aceitos_sem_ressalvas) + ' sem ressalvas',
    d.aceitos_conferir_cadastro ? number(d.aceitos_conferir_cadastro) + ' conferir cadastro' : '',
    d.aceitos_com_revisao ? number(d.aceitos_com_revisao) + ' por revisão humana' : ''].filter(Boolean).join(' · ');
  $('k-nao').textContent = number(d.nao_aceitos);
  $('k-nao-sub').textContent = pct(d.nao_aceitos);
  $('k-revisao').textContent = number(d.revisao_manual);
  $('k-revisao-sub').textContent = [pct(d.revisao_manual), d.fora_do_fluxo ? number(d.fora_do_fluxo) + ' fora do fluxo' : '']
    .filter(Boolean).join(' · ');
  const tokens = (d.tokens_entrada || 0) + (d.tokens_saida || 0);
  $('k-tokens').textContent = compact(tokens);
  $('k-tokens-sub').textContent = compact(d.tokens_entrada || 0) + ' lidos · ' + compact(d.tokens_saida || 0) + ' gerados'
    + (d.tokens_por_processo ? ' · ' + compact(d.tokens_por_processo) + ' por cidadão' : '');
  $('k-tempo').textContent = seconds(d.tempo_medio_ms);
  $('k-tempo-sub').textContent = 'soma das páginas, lidas em paralelo' + (d.ritmo_processos_hora
    ? ' · ritmo real ' + number(d.ritmo_processos_hora) + ' processos/hora' : '');
  const hours = d.paginas * (d.minutos_manuais_por_pagina || 1) / 60;
  $('k-paginas').textContent = number(d.paginas);
  $('k-paginas-sub').textContent = '≈ ' + number(Math.round(hours)) + ' h de leitura manual (est. '
    + number(d.minutos_manuais_por_pagina || 1) + ' min/página)';
  drawVolume(d.envios || []);
  drawLine('chart-tempo', 'table-tempo', d.envios || [], {
    value: (lot) => lot.tempo_medio_ms, format: seconds, label: 'Tempo de IA por cidadão em cada envio',
    axisMax: (value) => niceMax(value / 1000) * 1000, empty: 'Sem medições de tempo ainda.',
    tooltip: (lot) => [el('div', {}, 'Tempo de IA por cidadão: ' + seconds(lot.tempo_medio_ms))],
    columns: [['Tempo de IA por cidadão', (lot) => seconds(lot.tempo_medio_ms)]]});
  drawLine('chart-tokens', 'table-tokens', d.envios || [], {
    value: (lot) => lot.tokens, format: compact, label: 'Tokens de IA usados em cada envio', axisMax: niceMax,
    empty: 'Sem registro de tokens ainda (conta a partir dos envios lidos na versão atual).',
    tooltip: (lot) => [el('div', {}, number(lot.tokens) + ' tokens'),
      lot.tokens_por_processo ? el('div', {}, compact(lot.tokens_por_processo) + ' por cidadão') : null].filter(Boolean),
    columns: [['Tokens', (lot) => number(lot.tokens)], ['Por cidadão', (lot) => number(lot.tokens_por_processo)]]});
}

/* ---------- Lotes enviados ---------- */
const LOT_STATE = {recebendo: 'Enviando', na_fila: 'Na fila', processando: 'Lendo documentos', concluido: 'Concluído', falha: 'Falha'};
const LOT_REASON = {zip_invalido: 'arquivo ZIP inválido', espaco_insuficiente: 'sem espaço em disco no servidor',
  limite_de_entradas: 'arquivos demais no ZIP', limite_de_extracao: 'tamanho extraído acima do limite',
  falha_de_importacao: 'erro ao abrir os arquivos'};

async function refreshBatches() {
  const lots = await request(api);
  const select = $('batches');
  if (document.activeElement === select) return;  // não fecha o seletor enquanto o usuário escolhe
  select.replaceChildren(el('option', {value: ''}, 'Todos os lotes'),
    ...lots.map((lot) => el('option', {value: lot.id}, lot.nome + ' · ' +
      new Date((lot.criado || 0) * 1000).toLocaleString('pt-BR', {dateStyle: 'short', timeStyle: 'short'}))));
  select.value = lots.some((lot) => lot.id === currentBatch) ? currentBatch : '';
  currentBatch = select.value;
}

async function refreshLots() {
  const data = await request('/api/lotes-estado');
  $('lots-config').textContent = 'até ' + data.lotes_simultaneos + ' lotes e ' + data.paginas_em_paralelo + ' páginas em paralelo';
  if (!data.lotes.length) { $('lots').replaceChildren(el('p', {class: 'muted small'}, 'Nenhum lote enviado.')); return; }
  const mb = (bytes) => (bytes / 1048576).toLocaleString('pt-BR', {maximumFractionDigits: 0}) + ' MB';
  $('lots').replaceChildren(...data.lotes.slice(0, 30).map((lot) => {
    const progress = lot.progresso;
    let fraction = 0, detail = '';
    if (lot.status === 'recebendo') { fraction = lot.recebido / Math.max(1, lot.bytes); detail = mb(lot.recebido) + ' de ' + mb(lot.bytes); }
    else if (lot.etapa === 'separando') { detail = 'separando os processos…'; }
    else if (lot.status === 'processando' && progress?.total_paginas) {
      fraction = progress.paginas_concluidas / progress.total_paginas;
      detail = number(progress.paginas_concluidas) + ' de ' + number(progress.total_paginas) + ' páginas';
      const idle = progress.atualizado_em ? (Date.now() / 1000 - progress.atualizado_em) / 60 : null;
      if (idle != null && idle >= 1) {
        detail += ' · última página há ' + Math.floor(idle) + ' min';
        if (idle >= 3) detail += ' ⚠ leitura parada: veja o Diagnóstico e o servidor de IA';
      }
    } else if (lot.status === 'concluido') { fraction = 1; detail = lot.duracao_ms ? 'em ' + seconds(lot.duracao_ms) : ''; }
    else if (lot.status === 'falha') { detail = LOT_REASON[lot.motivo] || lot.motivo || ''; }
    const stateClass = {concluido: 'b-deferido', falha: 'b-indeferido', recebendo: 'b-pendente'}[lot.status] || 'b-processando';
    return el('div', {class: 'lot'},
      el('div', {}, el('div', {class: 'name', title: lot.nome}, lot.nome),
        el('div', {class: 'meta'}, [mb(lot.bytes), lot.processos != null ? number(lot.processos) + ' processos' : null,
          new Date((lot.criado || 0) * 1000).toLocaleString('pt-BR', {dateStyle: 'short', timeStyle: 'short'})].filter(Boolean).join(' · '))),
      el('div', {class: 'state'}, el('span', {class: 'badge ' + stateClass}, LOT_STATE[lot.status] || lot.status),
        el('div', {class: 'meta'}, detail)),
      lot.status === 'concluido' ? null : el('div', {class: 'bar'}, el('i', {style: 'width:' + Math.round(100 * fraction) + '%'})),
      lot.status === 'recebendo' ? null : el('div', {class: 'lot-actions'},
        el('button', {type: 'button', class: 'link', onclick: () => showDiagnostics(lot.id)}, 'Diagnóstico'),
        el('button', {type: 'button', class: 'link', onclick: () => { currentBatch = lot.id; $('batches').value = lot.id;
          view.pagina = 1; refreshAll(); document.querySelector('#rows').scrollIntoView({behavior: 'smooth', block: 'start'}); }},
          'Ver processos'),
        el('button', {type: 'button', class: 'link', onclick: () => rerunLot(lot.id)}, 'Reanalisar')));
  }));
}

async function refreshTracked() {
  if (!trackedBatch) return;
  const lot = await request(api + '/' + trackedBatch);
  const progress = lot.progresso;
  if (lot.status === 'na_fila') step('ler', 'Na fila · ' + lot.processos.length + ' processo(s) separados', 0);
  else if (lot.status === 'processando') {
    const done = lot.processos.filter((row) => row.concluido).length;
    step('ler', (progress ? number(progress.paginas_concluidas) + ' de ' + number(progress.total_paginas) + ' páginas · ' : '')
      + done + ' de ' + lot.processos.length + ' processo(s) com parecer',
      progress?.total_paginas ? progress.paginas_concluidas / progress.total_paginas : 0);
  } else if (lot.status === 'falha') step('ler', 'Falha: ' + (LOT_REASON[lot.motivo] || lot.motivo || 'erro') + '.');
  else if (lot.status === 'concluido') { step('pronto', lot.processos.length + ' processo(s) analisado(s).', 1); trackedBatch = ''; }
}

async function refreshLotActions() {
  const actions = ['reanalisar', 'avaliar', 'diagnostico'];
  if (!currentBatch) { for (const id of actions) $(id).hidden = true; return; }
  const lot = await request(api + '/' + currentBatch);
  $('reanalisar').hidden = lot.status === 'recebendo';
  $('avaliar').hidden = lot.status !== 'concluido';
  $('diagnostico').hidden = lot.status === 'recebendo';
}

/* ---------- Processos: busca, filtros, ordenação e paginação ---------- */
const view = {q: '', filtro: '', ordem: 'atualizado', direcao: 'desc', pagina: 1, por_pagina: 50};
const FILTROS = [['', 'Todos', 'todos'], ['aceitos', 'Aceitos'], ['nao_aceitos', 'Não aceitos'],
  ['revisao_manual', 'Revisão manual'], ['fora_do_fluxo', 'Fora do fluxo'], ['processando', 'Processando'],
  ['revisao_humana', 'Com revisão humana'], ['duplicados', 'Possíveis duplicidades']];

function query(extra = {}) {
  const params = new URLSearchParams({q: view.q, filtro: view.filtro, ordem: view.ordem, direcao: view.direcao, ...extra});
  if (currentBatch) params.set('lote', currentBatch);
  return params.toString();
}
function formatDate(value) {
  return value ? new Date(value * 1000).toLocaleString('pt-BR', {dateStyle: 'short', timeStyle: 'short'}) : '';
}

async function refreshTable() {
  const data = await request('/api/processos?' + query({pagina: view.pagina, por_pagina: view.por_pagina}));
  if (view.pagina > data.paginas) { view.pagina = data.paginas; return refreshTable(); }
  const totals = data.totais || {};
  $('counts').replaceChildren(...FILTROS.map(([key, label, total]) => el('button', {
    type: 'button', class: 'chip' + (view.filtro === key ? ' on' : ''), 'aria-pressed': String(view.filtro === key),
    onclick: () => { view.filtro = key; view.pagina = 1; refreshTable(); }},
    label, el('span', {class: 'n'}, number(totals[total || key] || 0)))));
  $('csv').href = '/api/processos/exportar.csv?' + query();
  $('json-all').href = '/api/processos/exportar?' + query();
  for (const th of document.querySelectorAll('th[data-sort]')) th.dataset.dir = th.dataset.sort === view.ordem ? view.direcao : '';
  const first = data.total ? (data.pagina - 1) * data.por_pagina + 1 : 0;
  const last = Math.min(data.total, data.pagina * data.por_pagina);
  $('list-count').textContent = number(data.total) + ' processo(s)' + (view.q || view.filtro ? ' no filtro' : '');
  $('page-info').textContent = data.total ? `${number(first)}–${number(last)} de ${number(data.total)} · página ${data.pagina} de ${data.paginas}` : '';
  $('prev').disabled = data.pagina <= 1;
  $('next').disabled = data.pagina >= data.paginas;
  const body = $('rows');
  if (!data.processos.length) {
    body.replaceChildren(el('tr', {}, el('td', {class: 'empty', colspan: '6'},
      view.q || view.filtro ? 'Nenhum processo encontrado com esses filtros.' : 'Nenhum processo ainda. Envie uma pasta para começar.')));
    return;
  }
  body.replaceChildren(...data.processos.map((row) => {
    const decision = row.resultado === 'processando' ? 'processando' : (row.decisao || AI_TO_DECISION[row.resultado]);
    const human = row.origem_decisao === 'revisao_humana';
    const changed = human && row.decisao && AI_TO_DECISION[row.resultado] && row.decisao !== AI_TO_DECISION[row.resultado];
    const dup = row.duplicidades?.length
      ? el('span', {class: 'tag', style: 'color:var(--warning-ink);border-color:#f3d48a;background:var(--warning-bg)',
          title: row.duplicidades.map((d) => (d.sei || d.processo) + ': ' + d.descricao).join('\n')}, 'Possível duplicidade')
      : null;
    return el('tr', {onclick: () => openProcess(row.lote_id, row.processo_id)},
      el('td', {}, row.sei || row.processo, el('div', {class: 'sub'}, row.lote_nome || '')),
      el('td', {}, row.requerente || '—', row.cpf ? el('div', {class: 'sub'}, row.cpf) : null),
      el('td', {}, badge(decision), el('span', {class: 'tag' + (human ? ' human' : ''), title: human && row.revisado_por
        ? 'Revisado por ' + row.revisado_por : 'Decisão sem intervenção humana'}, human ? 'Revisão humana' : 'IA'), dup,
        changed ? el('div', {class: 'sub'}, 'IA sugeriu: ' + DECISION[AI_TO_DECISION[row.resultado]]) : null,
        row.renda_centavos != null ? el('div', {class: 'sub'}, 'Renda ' + money(row.renda_centavos)) : null),
      el('td', {class: 'sub'}, row.sintese ? el('div', {style: 'color:var(--ink)'}, row.sintese) : null,
        decision === 'deferido' ? null : (row.motivos || [])[0] || ''),
      el('td', {class: 'sub'}, formatDate(row.atualizado_em), row.versao > 1 ? el('div', {}, 'versão ' + row.versao) : null),
      el('td', {class: 'actions'},
        el('button', {type: 'button', class: 'primary', onclick: (event) => { event.stopPropagation(); openProcess(row.lote_id, row.processo_id); }}, 'Analisar'),
        el('button', {type: 'button', title: 'Ler de novo todas as páginas deste processo',
          onclick: (event) => { event.stopPropagation(); rerunProcess(row.lote_id, row.processo_id); }}, 'Reanalisar'),
        el('button', {type: 'button', class: 'danger', onclick: (event) => { event.stopPropagation(); deleteProcess(row); }}, 'Excluir')));
  }));
}

async function rerunProcess(lote, processo) {
  if (!confirm('Ler de novo todas as páginas deste processo? A conferência e a decisão registradas são mantidas.')) return false;
  try {
    const lot = await request(api + '/' + lote + '/processos/' + processo + '/reanalisar', {method: 'POST'});
    trackedBatch = lote;
    step('ler', lot.status === 'processando' ? 'Reanálise agendada: começa quando a leitura atual terminar.' : 'Reanalisando o processo…', 0);
    refreshTable();
    return lot;
  } catch (error) { alert('Não foi possível reanalisar: ' + error.message); return false; }
}

async function deleteProcess(row) {
  const name = (row.sei || row.processo) + (row.requerente ? ' — ' + row.requerente : '');
  if (!confirm('Excluir o processo ' + name + '?\n\nOs documentos originais, as leituras e o histórico de pareceres '
    + 'deste processo serão apagados. Esta ação não pode ser desfeita.')) return;
  const typed = prompt('Para confirmar a exclusão de ' + name + ', digite EXCLUIR:');
  if (typed === null) return;
  if (typed.trim() !== 'EXCLUIR') { alert('Exclusão cancelada: o texto digitado não confere.'); return; }
  try {
    await request(api + '/' + row.lote_id + '/processos/' + row.processo_id + '/excluir',
      {method: 'POST', body: JSON.stringify({confirmacao: 'EXCLUIR'})});
    if ($('detail').classList.contains('on')) $('close').click();
    await refreshBatches();
    refreshAll();
  } catch (error) {
    alert(error.message === 'lote_em_processamento'
      ? 'O lote deste processo ainda está em leitura. Tente de novo quando terminar.'
      : 'Não foi possível excluir: ' + error.message);
  }
}

let searchTimer;
$('search').oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { view.q = $('search').value; view.pagina = 1; refreshTable(); }, 300);
};
for (const th of document.querySelectorAll('th[data-sort]')) {
  th.onclick = () => {
    view.direcao = view.ordem === th.dataset.sort ? (view.direcao === 'desc' ? 'asc' : 'desc')
      : (th.dataset.sort === 'atualizado' ? 'desc' : 'asc');
    view.ordem = th.dataset.sort;
    view.pagina = 1;
    refreshTable();
  };
}
$('prev').onclick = () => { view.pagina = Math.max(1, view.pagina - 1); refreshTable(); };
$('next').onclick = () => { view.pagina += 1; refreshTable(); };
$('page-size').onchange = () => { view.por_pagina = Number($('page-size').value); view.pagina = 1; refreshTable(); };
$('batches').onchange = () => { currentBatch = $('batches').value; view.pagina = 1; refreshAll(); };
async function rerunLot(id) {
  if (!confirm('Reler as páginas com falha ou lidas por versão anterior em todos os processos deste lote?')) return;
  try {
    const lot = await request(api + '/' + id + '/reanalisar', {method: 'POST'});
    trackedBatch = id;
    step('ler', lot.status === 'processando' ? 'Reanálise agendada: começa quando a leitura atual terminar.' : 'Reanalisando…', 0);
    refreshLots();
  } catch (error) { alert('Não foi possível reanalisar: ' + error.message); }
}
$('reanalisar').onclick = () => rerunLot(currentBatch);

function refreshAll() {
  return Promise.all([refreshDashboard(), refreshLots(), refreshTable(), refreshLotActions(), refreshTracked()]);
}

/* ---------- Detalhe do processo ---------- */
function show(fileId, page = 1) {
  if (!fileId) return;
  const base = api + '/' + detailBatch + '/pdf/' + fileId;
  $('files').value = fileId;
  $('viewer').src = base + '/' + page;
  $('page-label').textContent = 'página ' + page;
  $('original').href = base;
  $('original').hidden = false;
}

function pageButtons(sources) {
  const seen = new Set();
  const list = sources.filter((source) => {
    const key = source.arquivo + ':' + source.pagina;
    if (!source.arquivo || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  if (!list.length) return null;
  return el('span', {class: 'pages'}, ...list.slice(0, 8).map((source) =>
    el('button', {type: 'button', onclick: () => show(source.arquivo, source.pagina)}, 'p. ' + source.pagina)));
}

function renderDecision(detail) {
  const report = detail.parecer;
  const ai = detail.status === 'em_revisao' ? AI_TO_DECISION[report.resultado] : 'processando';
  const human = detail.revisao?.decisao;
  $('d-ia').replaceChildren(badge(ai, ai === 'processando' ? 'Processando' : RESULT[report.resultado]));
  const final = human ? human.resultado : ai;
  $('d-final').replaceChildren(badge(final),
    el('div', {class: 'small muted', style: 'margin-top:.3rem'}, human
      ? 'Revisão humana' + (human.usuario ? ' por ' + human.usuario : '') + ' em ' + formatDate(human.registrada_em)
      : (detail.revisao ? 'Conferência manual registrada; decisão da IA mantida' : '100% IA — sem revisão humana')));
  const form = $('decision-form');
  form.reset();
  if (human) {
    form.elements.namedItem('justificativa').value = human.justificativa || '';
    for (const input of form.querySelectorAll('input[name=resultado]')) input.checked = input.value === human.resultado;
  }
  for (const control of form.elements) control.disabled = detail.status !== 'em_revisao';
  $('decision-status').textContent = detail.status !== 'em_revisao' ? 'Disponível quando a leitura terminar.' : '';
}

async function openProcess(lote, id) {
  detailBatch = lote;
  currentProcess = id;
  const path = api + '/' + lote + '/processos/' + id;
  const detail = await request(path);
  detailData = detail;
  const report = detail.parecer;
  $('detail').classList.add('on');
  $('d-title').textContent = report.resumo.requerente || detail.processo;
  $('d-sub').textContent = [detail.numero_processo_sei || detail.processo,
    report.resumo.inscricao_imobiliaria ? 'inscrição ' + report.resumo.inscricao_imobiliaria : '',
    report.resumo.sintese || ''].filter(Boolean).join(' · ');
  $('d-sub').dataset.sei = detail.numero_processo_sei || detail.processo;
  const final = detail.revisao?.decisao?.resultado || (detail.status === 'em_revisao' ? AI_TO_DECISION[report.resultado] : 'processando');
  $('d-badge').replaceChildren(badge(final));
  $('d-badge').className = '';
  renderDecision(detail);
  $('criteria').replaceChildren(...report.criterios.map((row) => el('li', {},
    el('span', {class: 'icon ' + (MARK_CLASS[row.status] || 'i-neutral'), title: row.status, 'aria-hidden': 'true'}, MARK[row.status] || '·'),
    el('div', {}, el('strong', {}, LABEL[row.criterio] || row.criterio),
      el('span', {class: 'm'}, row.motivo), pageButtons(row.evidencias)))));
  $('opinion').textContent = report.parecer_texto;
  $('json').href = path + '/exportar';
  const files = detail.arquivos.filter((doc) => doc.id);
  $('files').replaceChildren(...files.map((doc) => el('option', {value: doc.id}, doc.origem)));
  $('page-list').replaceChildren(...detail.paginas.map((row) => el('li', {},
    el('button', {class: 'link', type: 'button', onclick: () => show(row.arquivo, row.pagina)},
      row.origem.split('/').pop() + ' · p. ' + row.pagina),
    ' — ' + (row.tipo || row.status) + (row.continuacao_requerimento ? ' (continuação)' : ''))));
  const first = detail.requerimentos[0] || detail.paginas[0];
  if (first) show(first.arquivo, first.pagina); else if (files[0]) show(files[0].id);
  const form = $('notes');
  form.reset();
  form.hidden = detail.status !== 'em_revisao';
  for (const [field, value] of Object.entries(detail.revisao?.confirmacoes || {})) {
    const input = form.elements.namedItem(field);
    if (input) input.value = value;
  }
  const amount = detail.revisao?.renda_bruta_familiar_centavos;
  if (amount != null) form.elements.renda.value = (amount / 100).toFixed(2).replace('.', ',');
  form.elements.fonte_renda.value = detail.revisao?.fonte_renda || '';
  form.elements.nota.value = detail.revisao?.nota || '';
  $('note-status').textContent = detail.revisao ? 'Conferência registrada.' : '';
}

function currentReview(extra) {
  const review = detailData?.revisao || {};
  return {nota: review.nota || '', confirmacoes: review.confirmacoes || {}, fonte_renda: review.fonte_renda || '',
    renda_bruta_familiar_centavos: review.renda_bruta_familiar_centavos ?? null, decisao: review.decisao
      ? {resultado: review.decisao.resultado, justificativa: review.decisao.justificativa} : null, ...extra};
}

async function saveReview(body, statusId) {
  await request(api + '/' + detailBatch + '/processos/' + currentProcess + '/revisao', {method: 'POST', body: JSON.stringify(body)});
  await openProcess(detailBatch, currentProcess);
  $(statusId).textContent = 'Salvo.';
  refreshAll();
}

$('decision-form').onsubmit = async (event) => {
  event.preventDefault();
  const form = new FormData($('decision-form'));
  const resultado = form.get('resultado');
  const justificativa = (form.get('justificativa') || '').trim();
  if (!resultado) { $('decision-status').textContent = 'Escolha Deferir ou Indeferir.'; return; }
  if (justificativa.length < 10) { $('decision-status').textContent = 'Escreva a justificativa (mínimo de 10 caracteres).'; return; }
  const ai = AI_TO_DECISION[detailData.parecer.resultado];
  if (resultado !== ai && !confirm('A decisão "' + DECISION[resultado] + '" é diferente da sugestão da IA ("' + DECISION[ai]
    + '"). Confirmar a decisão humana?')) return;
  try {
    const review = currentReview({decisao: {resultado, justificativa}});
    review.nota = review.nota || justificativa;
    await saveReview(review, 'decision-status');
  } catch (error) { $('decision-status').textContent = 'Não foi possível salvar: ' + error.message; }
};

$('files').onchange = () => show($('files').value);
$('close').onclick = () => { $('detail').classList.remove('on'); currentProcess = ''; detailData = null; };
$('detail').onclick = (event) => { if (event.target === $('detail')) $('close').click(); };
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && $('detail').classList.contains('on')) $('close').click(); });
$('copy').onclick = async () => {
  await navigator.clipboard.writeText($('opinion').textContent);
  $('copy').textContent = 'Copiado';
  setTimeout(() => { $('copy').textContent = 'Copiar'; }, 1500);
};
$('rerun-process').onclick = async () => {
  const lot = await rerunProcess(detailBatch, currentProcess);
  if (lot) $('d-badge').replaceChildren(badge('processando', lot.status === 'processando' ? 'Reanálise agendada' : 'Reanalisando'));
};
$('delete-process').onclick = () => deleteProcess({lote_id: detailBatch, processo_id: currentProcess,
  sei: $('d-sub').dataset.sei, requerente: $('d-title').textContent});
$('notes').onsubmit = async (event) => {
  event.preventDefault();
  try {
    const {renda, fonte_renda, nota, ...fields} = Object.fromEntries(new FormData($('notes')));
    const parts = /^(\d+)(?:[,.](\d{1,2}))?$/.exec(renda.trim().replace(/\.(?=\d{3}\b)/g, ''));
    if (renda.trim() && !parts) throw new Error('Informe a renda em reais, por exemplo 3242,00.');
    const cents = parts ? Number(parts[1]) * 100 + Number((parts[2] || '').padEnd(2, '0')) : null;
    const confirmacoes = Object.fromEntries(Object.entries(fields).filter(([, value]) => value.trim()));
    await saveReview(currentReview({nota, confirmacoes, fonte_renda, renda_bruta_familiar_centavos: cents}), 'note-status');
  } catch (error) {
    $('note-status').textContent = error.message;
  }
};

refreshBatches().then(refreshAll).catch((error) => { $('footer-status').textContent = 'Falha: ' + error.message; });
setInterval(() => {
  if (uploading) return;
  Promise.all([refreshBatches(), refreshLots(), refreshTracked(), refreshDashboard(), refreshLotActions(),
    document.activeElement !== $('search') ? refreshTable() : null]).catch(() => {});
}, 5000);

/* ---------- Comparação com gabarito ---------- */
$('avaliar').onclick = () => $('gabarito').click();
$('gabarito').onchange = async (event) => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  try {
    const report = await request(api + '/' + currentBatch + '/avaliar', {method: 'POST', body: await file.text()});
    const r = report.resumo;
    const pct = (value) => value == null ? '—' : value.toLocaleString('pt-BR') + '%';
    $('eval-body').replaceChildren(
      el('p', {}, 'Resultado correto em ' + r.resultado_ok + ' de ' + r.processos + ' processos (' + pct(r.acerto_resultado_pct) +
        ') · renda ' + pct(r.acerto_renda_pct) + ' · campos ' + pct(r.acerto_campos_pct) + '.'),
      el('table', {}, el('thead', {}, el('tr', {}, ...['Processo', 'Esperado', 'Obtido', 'Divergências'].map((h) => el('th', {}, h)))),
        el('tbody', {}, ...report.processos.map((row) => el('tr', {},
          el('td', {}, row.processo),
          el('td', {}, RESULT[row.esperado] || row.esperado || ''),
          el('td', {}, el('span', {class: 'badge r-' + (row.obtido === row.esperado ? 'enquadra' : 'nao_enquadra')},
            RESULT[row.obtido] || row.obtido)),
          el('td', {class: 'why'}, [
            row.renda_esperada != null && row.renda_esperada !== row.renda_obtida
              ? 'renda ' + money(row.renda_obtida) + ' (esperado ' + money(row.renda_esperada) + ')' : '',
            ...(row.campos_divergentes || []).map((c) => c.campo + ': ' + c.obtido + ' (esperado ' + c.esperado + ')'),
            row.obtido !== row.esperado ? row.motivo : '',
          ].filter(Boolean).join(' · ')))))));
    document.querySelector('#eval-box h3').textContent = 'Assertividade do lote';
    $('eval-box').showModal();
  } catch (error) {
    alert('Gabarito inválido: ' + error.message);
  }
};

/* ---------- Diagnóstico do lote ---------- */
const SITUACAO = {
  extraido: 'lidas com sucesso', esquema_invalido: 'resposta fora do formato', json_invalido: 'resposta não é JSON',
  resposta_truncada: 'resposta cortada (limite de tokens)', resposta_sem_conteudo: 'resposta vazia',
  falha_de_leitura: 'erro interno na página', pagina_inacessivel: 'página não renderizou',
  erro_http_429: 'modelo ocupado (429)', erro_http_500: 'erro no LiteLLM (500)', erro_http_502: 'LiteLLM indisponível (502)',
  erro_http_503: 'LiteLLM indisponível (503)', erro_http_504: 'LiteLLM sem resposta (504)',
};
const ETAPA = {renderizacao_ms: 'renderização', texto_pdf_ms: 'texto do PDF', litellm_ms: 'modelo (todas as leituras da página)'};
$('diagnostico').onclick = () => showDiagnostics(currentBatch);
async function showDiagnostics(id) {
  try {
    const d = await request(api + '/' + id + '/diagnostico');
    const seconds = (ms) => (ms / 1000).toLocaleString('pt-BR', {maximumFractionDigits: 1}) + ' s';
    const list = (entries, labels) => el('ul', {}, ...entries.map(([key, value]) =>
      el('li', {}, value + ' · ' + (labels[key] || key))));
    const c = d.configuracao;
    $('eval-body').replaceChildren(
      d.tokens ? el('p', {class: 'small muted'}, 'IA: ' + number(d.tokens.chamadas) + ' chamadas registradas · média de '
        + number(d.tokens.entrada_por_chamada) + ' tokens lidos e ' + number(d.tokens.saida_por_chamada) + ' gerados por chamada') : null,
      el('p', {}, d.paginas + ' páginas · ' + d.novas_tentativas + ' novas tentativas · ' + d.divergencias +
        ' divergências na dupla leitura · ' + d.campos_descartados + ' campos descartados'),
      el('strong', {}, 'Páginas por situação'), list(Object.entries(d.por_situacao).sort((a, b) => b[1] - a[1]), SITUACAO),
      el('strong', {}, 'Leitura detalhada'), list(Object.entries(d.leitura_detalhada), {dispensada: 'dispensada (1ª leitura completa)'}),
      el('strong', {}, 'Tempo médio por página'), list(Object.entries(d.tempo_medio_ms).map(([k, v]) => [k, seconds(v)]), ETAPA),
      d.exemplos_de_falha.length ? el('strong', {}, 'Exemplos de falha') : null,
      d.exemplos_de_falha.length ? el('ul', {}, ...d.exemplos_de_falha.map((f) =>
        el('li', {}, f.processo + ' · ' + f.arquivo.split('/').pop() + ' p. ' + f.pagina + ' · ' + (SITUACAO[f.situacao] || f.situacao)))) : null,
      el('p', {class: 'why'}, 'Modelos: ' + c.modelo + ' / ' + (c.modelo_detalhado || c.modelo) + ' / ' +
        (c.modelo_verificacao || 'mesmo') + ' · ' + c.paginas_em_paralelo + ' páginas em paralelo · ' +
        c.cpus + ' CPU(s) · limite ' + c.limite_tempo_s + ' s por chamada'),
      el('button', {type: 'button', onclick: () => navigator.clipboard.writeText(JSON.stringify(d, null, 2))},
        'Copiar diagnóstico'));
    document.querySelector('#eval-box h3').textContent = 'Diagnóstico do lote';
    $('eval-box').showModal();
  } catch (error) { alert('Falha ao gerar o diagnóstico: ' + error.message); }
};

/* ---------- Teste de capacidade da IA ---------- */
async function showCapacity(state) {
  const rows = state.resultados || [];
  const rec = state.recomendacao;
  const minutes = (pages) => { const m = 2535 / pages; return m < 1 ? 'menos de 1 min' : '≈ ' + number(Math.round(m)) + ' min'; };
  $('eval-body').replaceChildren(...[
    el('p', {class: 'small muted'}, 'Mede a 1ª leitura com uma página real (' + (state.amostra || '…') + ') no modelo ' + (state.modelo || '')
      + '. Rode com o servidor de IA livre: lotes em leitura ao mesmo tempo diminuem o resultado.'),
    el('table', {}, el('thead', {}, el('tr', {}, ...['Simultâneas', 'Páginas/min', 'Tempo médio', 'Pior caso (p95)', 'Tokens gerados', 'Erros'].map((h) => el('th', {}, h)))),
      el('tbody', {}, ...rows.map((row) => el('tr', {}, el('td', {}, number(row.simultaneas)), el('td', {}, number(row.paginas_por_minuto)),
        el('td', {}, row.tempo_medio_s != null ? row.tempo_medio_s.toLocaleString('pt-BR') + ' s' : '—'),
        el('td', {}, row.tempo_p95_s != null ? row.tempo_p95_s.toLocaleString('pt-BR') + ' s' : '—'),
        el('td', {}, row.tokens_saida_medio != null ? number(row.tokens_saida_medio) : '—'),
        el('td', {}, Object.entries(row.erros || {}).map(([k, v]) => v + ' ' + k).join(', ') || '0'))))),
    state.status === 'executando' ? el('p', {}, 'Medindo com ' + (state.nivel_atual || '…') + ' chamadas simultâneas…') : null,
    state.status === 'falha' ? el('p', {}, 'Falha no teste: ' + state.motivo) : null,
    rows.some((row) => row.tokens_saida_medio > 250) ? el('p', {class: 'small'}, '⚠ A classificação deveria gerar menos de '
      + '150 tokens. Valores bem maiores indicam que o modelo está "raciocinando" antes de responder: desligar esse modo no '
      + 'servidor de IA (ou usar um modelo sem raciocínio) reduz bastante o tempo de cada página.') : null,
    state.status === 'concluido' ? el('p', {}, rec
      ? 'Recomendação: IPTU_PAGE_WORKERS=' + rec.IPTU_PAGE_WORKERS + ' (' + number(rec.paginas_por_minuto)
        + ' páginas/min; ' + minutes(rec.paginas_por_minuto) + ' para a 1ª leitura de um lote de 2.535 páginas).'
      : 'Nenhum nível rodou sem erros: o servidor de IA está saturado ou com limite de chamadas. Verifique o LiteLLM e o vLLM.') : null,
  ].filter(Boolean));
  document.querySelector('#eval-box h3').textContent = 'Capacidade do servidor de IA';
  if (!$('eval-box').open) $('eval-box').showModal();
  if (state.status === 'executando') setTimeout(async () => {
    if ($('eval-box').open) showCapacity(await request('/api/capacidade'));
  }, 2000);
}
$('capacity').onclick = async () => {
  if (!confirm('O teste envia até 688 chamadas ao servidor de IA (8 a 128 simultâneas) e leva alguns minutos. '
    + 'Lotes em leitura vão disputar o servidor. Continuar?')) return;
  try {
    const response = await fetch('/api/capacidade', {method: 'POST'});
    showCapacity(await response.json());
  } catch (error) { alert('Não foi possível iniciar o teste: ' + error.message); }
};

/* ---------- Administração: apagar tudo ---------- */
$('reset-all').onclick = async () => {
  if (!confirm('APAGAR TUDO?\n\nTodos os lotes enviados, documentos, leituras, pareceres, decisões e o histórico serão '
    + 'apagados definitivamente. Leituras em andamento serão interrompidas. Esta ação não pode ser desfeita.')) return;
  const typed = prompt('Para confirmar, digite APAGAR TUDO:');
  if (typed === null) return;
  if (typed.trim() !== 'APAGAR TUDO') { alert('Cancelado: o texto digitado não confere.'); return; }
  try {
    const result = await request('/api/sistema/zerar', {method: 'POST', body: JSON.stringify({confirmacao: 'APAGAR TUDO'})});
    localStorage.removeItem(RESUME_KEY);
    currentBatch = ''; trackedBatch = '';
    $('job').classList.remove('on');
    alert(number(result.processos_removidos) + ' processo(s) removido(s) do histórico. Os arquivos dos lotes são apagados '
      + 'assim que as leituras em andamento pararem (alguns segundos a poucos minutos).');
    await refreshBatches();
    refreshAll();
  } catch (error) { alert('Não foi possível apagar: ' + error.message); }
};
