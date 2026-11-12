const get = (id) => document.getElementById(id);
const base = '/api/lotes';
const upload = get('upload');
const batchList = get('batches');
const processList = get('processes');
const fileList = get('files');
const storageKey = 'iptu-upload-pendente';
const chunk = 8 * 1024 * 1024;
let currentPdfBase = '';

async function request(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.status || 'HTTP ' + response.status);
  return data;
}

function setOptions(select, rows, label, defaultLabel) {
  const selected = select.value;
  select.replaceChildren(new Option(defaultLabel, ''), ...rows.map((row) => new Option(label(row), row.id)));
  if (rows.some((row) => row.id === selected)) select.value = selected;
}

function addText(list, value) {
  const item = document.createElement('li');
  item.textContent = value;
  list.append(item);
}

upload.addEventListener('submit', async (event) => {
  event.preventDefault();
  const file = get('upload-file').files[0];
  const button = upload.querySelector('button');
  button.disabled = true;
  try {
    const key = JSON.stringify([file.name, file.size, file.lastModified]);
    const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
    let lot = saved?.key === key ? await request(base + '/' + saved.id) : null;
    if (!lot || lot.status !== 'recebendo') {
      lot = await request(base, {method:'POST', body:JSON.stringify({nome:file.name, bytes:file.size})});
      localStorage.setItem(storageKey, JSON.stringify({key, id:lot.id}));
    }
    for (let offset = lot.recebido || 0; offset < file.size;) {
      const result = await request(base + '/' + lot.id + '/parte', {
        method:'POST', headers:{'Upload-Offset':String(offset)}, body:file.slice(offset, offset + chunk)
      });
      offset = result.recebido;
      get('progress').value = offset / file.size;
      get('upload-status').textContent = 'Enviando: ' + Math.round(100 * offset / file.size) + '%';
    }
    await request(base + '/' + lot.id + '/processar', {method:'POST'});
    localStorage.removeItem(storageKey);
    get('upload-status').textContent = 'Lote enviado. O processamento continua no servidor.';
    get('progress').value = 1;
    await refreshBatches();
    batchList.value = lot.id;
    clearProcess();
    await refreshProcesses();
  } catch (error) {
    get('upload-status').textContent = 'Falha: ' + error.message + '. Selecione o mesmo arquivo para retomar.';
  } finally {
    button.disabled = false;
  }
});

async function refreshBatches() {
  setOptions(batchList, await request(base), (row) => row.nome + ' · ' + row.status, 'Selecione um lote');
}

async function refreshProcesses() {
  const key = batchList.value;
  if (!key) return;
  const lot = await request(base + '/' + key);
  if (batchList.value !== key) return;
  get('reanalisar').hidden = !['concluido', 'falha'].includes(lot.status);
  const done = lot.processos.filter((row) => row.concluido).length;
  get('batch-status').textContent = lot.status + ' · ' + done + '/' + lot.processos.length + ' processos lidos' +
    (lot.motivo ? ' · ' + lot.motivo : '');
  setOptions(processList, lot.processos, (row) => row.processo + (row.concluido ? ' · revisão' : ''), 'Selecione um processo');
}

function showPdf(id, page = 1) {
  if (!id) return;
  fileList.value = id;
  const source = currentPdfBase + '/' + id;
  get('original').href = source;
  get('original').hidden = false;
  get('viewer').hidden = false;
  get('viewer').src = source + '/' + page;
}

function clearProcess() {
  currentPdfBase = '';
  get('viewer').removeAttribute('src');
  get('viewer').hidden = true;
  get('original').hidden = true;
  for (const id of ['issues', 'pages', 'files']) get(id).replaceChildren();
  get('notes').hidden = true;
  get('parecer').hidden = true;
  get('note-status').textContent = '';
  get('process-status').textContent = 'Selecione um processo.';
  processList.value = '';
}

async function showProcess() {
  const key = batchList.value;
  const id = processList.value;
  clearProcess();
  processList.value = id;
  if (!key || !id) return;
  const path = base + '/' + key;
  const detail = await request(path + '/processos/' + id);
  if (batchList.value !== key || processList.value !== id) return;
  currentPdfBase = path + '/pdf';
  get('process-status').textContent = (detail.numero_processo_sei || detail.processo) + ' · ' + detail.status;
  const report = detail.parecer;
  get('parecer').hidden = false;
  get('parecer-status').textContent = {
    proposta_deferimento: 'Pode receber · proposta de deferimento',
    proposta_indeferimento: 'Não pode receber · proposta de indeferimento',
    em_analise: 'Em análise · conferência necessária'
  }[report.status];
  get('parecer-json').href = path + '/processos/' + id + '/parecer';
  get('criterios').replaceChildren();
  for (const row of report.criterios) {
    const li = document.createElement('li');
    li.textContent = row.status + ' · ' + row.motivo;
    for (const source of row.evidencias) {
      if (source.arquivo) {
        const link = document.createElement('button');
        link.type = 'button';
        link.textContent = 'Ver página ' + source.pagina;
        link.onclick = () => showPdf(source.arquivo, source.pagina);
        li.append(' ', link);
      }
    }
    get('criterios').append(li);
  }
  for (const text of detail.pendencias) addText(get('issues'), text);
  for (const doc of detail.arquivos) if (doc.erro) addText(get('issues'), doc.origem + ' · ' + doc.erro);
  const files = detail.arquivos.filter((doc) => doc.id);
  setOptions(fileList, files, (row) => row.origem, 'Selecione um PDF');
  const first = detail.requerimentos[0] || detail.paginas[0];
  if (first) showPdf(first.arquivo, first.pagina);
  else if (files.length) showPdf(files[0].id);
  for (const row of detail.paginas) {
    const item = document.createElement('li');
    const open = document.createElement('button');
    open.type = 'button';
    open.textContent = 'Página ' + row.pagina;
    open.onclick = () => showPdf(row.arquivo, row.pagina);
    const data = Object.entries(row.obtido || {}).filter(([, value]) => value !== null);
    item.append(open, ' ' + row.origem + ' · ' + (row.tipo || row.status),
      ...data.map(([field, value]) => {
        const line = document.createElement('div');
        line.textContent = field + ': ' + value;
        return line;
      }));
    get('pages').append(item);
  }
  get('notes').hidden = detail.status !== 'em_revisao';
  get('notes').reset();
  get('note').value = detail.revisao?.nota || '';
  for (const [field, value] of Object.entries(detail.revisao?.confirmacoes || {})) {
    const input = get('notes').elements.namedItem(field);
    if (input) input.value = value;
  }
  const amount = detail.revisao?.renda_bruta_familiar_centavos;
  if (amount != null) get('notes').elements.renda.value = (amount / 100).toFixed(2).replace('.', ',');
  get('notes').elements.fonte_renda.value = detail.revisao?.fonte_renda || '';
  get('note-status').textContent = detail.revisao ? 'Revisão registrada.' : '';
}

get('notes').addEventListener('submit', async (event) => {
  event.preventDefault();
  const path = base + '/' + batchList.value + '/processos/' + processList.value + '/revisao';
  try {
    const form = new FormData(get('notes'));
    const {renda, fonte_renda, ...fields} = Object.fromEntries(form);
    const amount = renda.trim();
    const parts = /^(\d+)(?:[,.](\d{1,2}))?$/.exec(amount);
    if (amount && !parts) throw new Error('Informe a renda em reais, por exemplo 3242,00.');
    const cents = parts ? Number(parts[1]) * 100 + Number((parts[2] || '').padEnd(2, '0')) : null;
    const confirmacoes = Object.fromEntries(Object.entries(fields).filter(([, value]) => value.trim()));
    await request(path, {method:'POST', body:JSON.stringify({
      nota:get('note').value, confirmacoes, fonte_renda, renda_bruta_familiar_centavos:cents
    })});
    await showProcess();
  } catch (error) {
    get('note-status').textContent = error.message;
  }
});

function report(error) { get('batch-status').textContent = 'Falha: ' + error.message; }
batchList.onchange = () => { clearProcess(); refreshProcesses().catch(report); };
processList.onchange = () => showProcess().catch(report);
get('reanalisar').onclick = async () => {
  try {
    await request(base + '/' + batchList.value + '/reanalisar', {method:'POST'});
    await refreshProcesses();
    await showProcess();
  } catch (error) { report(error); }
};
get('reload').onclick = () => showProcess().catch(report);
fileList.onchange = () => showPdf(fileList.value);
refreshBatches().catch(report);
setInterval(async () => {
  try { await refreshBatches(); await refreshProcesses(); } catch (error) { report(error); }
}, 4000);
