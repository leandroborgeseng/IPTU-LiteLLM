'use strict';
const $ = (id) => document.getElementById(id);
const api = '/api/lotes';
const RESUME_KEY = 'iptu-upload-pendente';
const ACCEPTED = /\.(pdf|zip|jpe?g|png|webp|html?)$/i;
const RESULT = {
  enquadra: 'Enquadra', enquadra_conferir_cadastro: 'Enquadra · conferir cadastro',
  nao_enquadra: 'Não enquadra', em_analise: 'Em análise', fora_do_fluxo: 'Fora do fluxo',
  processando: 'Processando', erro: 'Erro de leitura',
};
const MARK = {atendido: '✓', impedimento: '✕', pendente: '!', cadastral: '○', aviso: 'i', fora_do_fluxo: '→'};
const LABEL = {
  modalidade: 'Modalidade', identidade: 'Identificação', beneficio: 'Benefício', renda: 'Renda familiar',
  residencia: 'Residência', 'edícula': 'Edícula e uso', imovel: 'Vínculo com o imóvel', unicidade: 'Imóvel único',
  mobiliario: 'Atividade econômica', prazo: 'Protocolo e prazo', exercicio: 'Exercício',
  dupla_leitura: 'Dupla leitura do requerimento',
  processo_sei: 'Processo SEI', leitura: 'Leitura', processamento: 'Processamento', requerimento: 'Requerimento',
};
let currentBatch = '';
let currentProcess = '';
let uploading = false;

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
  if (value != null) $('progress').value = value;
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
    selectBatch(id);
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

/* ---------- Lotes e resultados ---------- */
async function refreshBatches() {
  const lots = await request(api);
  const select = $('batches');
  const chosen = select.value;
  select.replaceChildren(el('option', {value: ''}, '—'),
    ...lots.map((lot) => el('option', {value: lot.id}, lot.nome + ' · ' +
      new Date((lot.criado || 0) * 1000).toLocaleString('pt-BR', {dateStyle: 'short', timeStyle: 'short'}))));
  select.value = lots.some((lot) => lot.id === chosen) ? chosen : '';
  if (!currentBatch && lots[0]) selectBatch(lots[0].id);
}

function selectBatch(id) {
  currentBatch = id;
  $('batches').value = id;
  $('csv').href = api + '/' + id + '/resumo.csv';
  $('csv').download = 'pareceres-iptu.csv';
  refreshBatch();
}

async function refreshBatch() {
  if (!currentBatch) return;
  const id = currentBatch;
  const [lot, rows] = await Promise.all([request(api + '/' + id), request(api + '/' + id + '/resumo')]);
  if (id !== currentBatch) return;
  const done = rows.filter((row) => row.concluido).length;
  const progress = lot.progresso;
  if (['na_fila', 'processando'].includes(lot.status)) {
    const fraction = progress?.total_paginas ? progress.paginas_concluidas / progress.total_paginas : 0;
    step('ler', (progress ? progress.paginas_concluidas + ' de ' + progress.total_paginas + ' páginas · ' : 'Na fila · ')
      + done + ' de ' + rows.length + ' processo(s) com parecer', fraction);
  } else if (lot.status === 'falha') {
    step('ler', 'Falha no processamento: ' + (lot.motivo || 'erro') + '.');
  } else if (lot.status === 'concluido' && $('job').classList.contains('on')) {
    step('pronto', rows.length + ' processo(s) analisado(s).', 1);
  }
  $('reanalisar').hidden = !['concluido', 'falha'].includes(lot.status);
  $('avaliar').hidden = lot.status !== 'concluido';
  $('csv').hidden = !rows.length;
  const tally = {};
  for (const row of rows) tally[row.resultado] = (tally[row.resultado] || 0) + 1;
  $('counts').replaceChildren(...Object.entries(tally).map(([key, count]) =>
    el('span', {class: 'badge r-' + key}, count + ' · ' + (RESULT[key] || key))));
  const body = $('rows');
  if (!rows.length) {
    body.replaceChildren(el('tr', {}, el('td', {class: 'empty', colspan: '4'}, 'Separando os processos…')));
    return;
  }
  body.replaceChildren(...rows.map((row) => el('tr', {onclick: () => openProcess(row.id)},
    el('td', {}, row.sei || row.processo),
    el('td', {}, row.requerente || '—', row.cpf ? el('div', {class: 'why'}, row.cpf) : null),
    el('td', {}, el('span', {class: 'badge r-' + row.resultado}, RESULT[row.resultado] || row.resultado),
      row.renda_familiar_centavos != null ? el('div', {class: 'why'}, 'Renda ' + money(row.renda_familiar_centavos)) : null),
    el('td', {class: 'why'}, row.sintese ? el('div', {}, row.sintese) : null,
      ['enquadra', 'enquadra_conferir_cadastro'].includes(row.resultado) ? null : (row.motivos || [])[0] || ''))));
}

$('batches').onchange = () => { $('job').classList.remove('on'); if ($('batches').value) selectBatch($('batches').value); };
$('reanalisar').onclick = async () => {
  await request(api + '/' + currentBatch + '/reanalisar', {method: 'POST'}).catch((e) => alert(e.message));
  step('ler', 'Reanalisando…', 0);
  refreshBatch();
};
$('limpar').onclick = async () => {
  if (!confirm('Apagar todos os lotes, documentos e conferências salvas?')) return;
  await request(api + '/limpeza', {method: 'POST'});
  currentBatch = '';
  $('rows').replaceChildren(el('tr', {}, el('td', {class: 'empty', colspan: '4'}, 'Lotes apagados.')));
  $('counts').replaceChildren();
  $('job').classList.remove('on');
  localStorage.removeItem(RESUME_KEY);
  setTimeout(refreshBatches, 4000);
};

/* ---------- Detalhe do processo ---------- */
function show(fileId, page = 1) {
  if (!fileId) return;
  const base = api + '/' + currentBatch + '/pdf/' + fileId;
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

async function openProcess(id) {
  currentProcess = id;
  const path = api + '/' + currentBatch + '/processos/' + id;
  const detail = await request(path);
  const report = detail.parecer;
  $('detail').classList.add('on');
  $('d-title').textContent = report.resumo.requerente || detail.processo;
  $('d-sub').textContent = (report.resumo.sintese ? report.resumo.sintese + ' · ' : '') +
    (detail.numero_processo_sei || detail.processo) +
    (report.resumo.inscricao_imobiliaria ? ' · inscrição ' + report.resumo.inscricao_imobiliaria : '');
  const result = detail.status === 'em_revisao' ? report.resultado : 'processando';
  $('d-badge').className = 'badge r-' + result;
  $('d-badge').textContent = RESULT[result];
  $('criteria').replaceChildren(...report.criterios.map((row) => el('li', {class: row.status},
    el('span', {class: 'ic', title: row.status}, MARK[row.status] || '·'),
    el('div', {}, el('strong', {}, LABEL[row.criterio] || row.criterio),
      el('span', {class: 'm'}, row.motivo), pageButtons(row.evidencias)))));
  $('opinion').textContent = report.parecer_texto;
  $('json').href = path + '/parecer';
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

$('files').onchange = () => show($('files').value);
$('close').onclick = () => { $('detail').classList.remove('on'); currentProcess = ''; };
$('detail').onclick = (event) => { if (event.target === $('detail')) $('close').click(); };
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') $('close').click(); });
$('copy').onclick = async () => {
  await navigator.clipboard.writeText($('opinion').textContent);
  $('copy').textContent = 'Copiado';
  setTimeout(() => { $('copy').textContent = 'Copiar'; }, 1500);
};
$('notes').onsubmit = async (event) => {
  event.preventDefault();
  try {
    const {renda, fonte_renda, nota, ...fields} = Object.fromEntries(new FormData($('notes')));
    const parts = /^(\d+)(?:[,.](\d{1,2}))?$/.exec(renda.trim().replace(/\.(?=\d{3}\b)/g, ''));
    if (renda.trim() && !parts) throw new Error('Informe a renda em reais, por exemplo 3242,00.');
    const cents = parts ? Number(parts[1]) * 100 + Number((parts[2] || '').padEnd(2, '0')) : null;
    const confirmacoes = Object.fromEntries(Object.entries(fields).filter(([, value]) => value.trim()));
    await request(api + '/' + currentBatch + '/processos/' + currentProcess + '/revisao', {
      method: 'POST', body: JSON.stringify({nota, confirmacoes, fonte_renda, renda_bruta_familiar_centavos: cents}),
    });
    await openProcess(currentProcess);
    refreshBatch();
  } catch (error) {
    $('note-status').textContent = error.message;
  }
};

refreshBatches().catch((error) => { $('footer-status').textContent = 'Falha: ' + error.message; });
setInterval(() => { if (!uploading) refreshBatch().catch(() => {}); }, 4000);

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
    $('eval-box').showModal();
  } catch (error) {
    alert('Gabarito inválido: ' + error.message);
  }
};
