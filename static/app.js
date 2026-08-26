const form = document.querySelector('#card-form');
const attributeList = document.querySelector('#attribute-list');
const template = document.querySelector('#attribute-template');

function addAttribute(values = {}) {
  const node = template.content.cloneNode(true);
  const row = node.querySelector('.attribute-row');
  row.querySelector('.attribute-key').value = values.key || '';
  row.querySelector('.attribute-value').value = values.value || '';
  row.querySelector('.attribute-unit').value = values.unit || '';
  row.querySelector('button').addEventListener('click', () => row.remove());
  attributeList.append(node);
}

function attributes() {
  return [...document.querySelectorAll('.attribute-row')]
    .map(row => ({
      key: row.querySelector('.attribute-key').value.trim(),
      value: row.querySelector('.attribute-value').value.trim(),
      unit: row.querySelector('.attribute-unit').value.trim()
    }))
    .filter(item => item.key || item.value || item.unit);
}

function readFile(file) {
  if (!file) return Promise.resolve('');
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('Не удалось прочитать изображение.'));
    reader.readAsDataURL(file);
  });
}

function statusLabel(status) {
  return {violation: 'Нарушение', clean: 'Нарушений не найдено', requires_review: 'Требуется проверка'}[status] || 'Требуется проверка';
}

function renderResult(result) {
  const panel = document.querySelector('#result-panel');
  panel.hidden = false;
  panel.dataset.status = result.status;
  let title = statusLabel(result.status);
  const violationCount = (result.violations || []).length;
  if (result.status === 'violation' && violationCount > 1) {
    title += ` (обнаружено ошибок: ${violationCount})`;
  }
  document.querySelector('#result-title').textContent = title;
  document.querySelector('#mode').textContent = result.mode === 'rules+llm' ? 'Правила + LLM' : 'Только правила';
  const list = document.querySelector('#violations');
  list.replaceChildren();
  for (const violation of result.violations || []) {
    const item = document.createElement('article');
    item.className = 'violation';
    const text = document.createElement('p'); text.textContent = violation.text;
    const evidence = document.createElement('small'); evidence.textContent = violation.evidence || violation.source || '';
    item.append(text, evidence);
    list.append(item);
  }
  document.querySelector('#review-reason').textContent = result.review_reason || '';
  const sources = document.querySelector('#sources-list');
  sources.replaceChildren();
  for (const item of result.source_context || []) {
    const li = document.createElement('li'); li.textContent = item.source; sources.append(li);
  }
}

async function loadStatus() {
  const response = await fetch('/api/status');
  const data = await response.json();
  document.querySelector('#source-status').innerHTML = `<strong>${data.sources} источника · ${data.fragments} фрагментов</strong><br>${data.rules} формализованных правила${data.llm_configured ? ' · LLM подключена' : ' · LLM пока не настроена'}`;
}

async function loadHistory() {
  const response = await fetch('/api/history');
  const history = await response.json();
  const root = document.querySelector('#history');
  root.replaceChildren();
  if (!history.length) { root.textContent = 'Проверок ещё не было.'; return; }
  const list = document.createElement('div'); list.className = 'history-list';
  for (const item of history) {
    const row = document.createElement('div'); row.className = `history-row ${item.status}`;
    row.innerHTML = `<strong>${statusLabel(item.status)}</strong><span>${item.card.title || 'Без названия'}</span><time>${new Date(item.created_at).toLocaleString('ru-RU')}</time>`;
    list.append(row);
  }
  root.append(list);
}

document.querySelector('#add-attribute').addEventListener('click', () => addAttribute());
document.querySelector('#refresh-history').addEventListener('click', loadHistory);

form.addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.querySelector('#submit-button');
  button.disabled = true; button.textContent = 'Проверяю…';
  try {
    const data = new FormData(form);
    const card = Object.fromEntries([...data.entries()].filter(([key]) => key !== 'image'));
    card.attributes = attributes();
    card.image_data_url = await readFile(data.get('image'));
    const response = await fetch('/api/check', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(card)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Не удалось выполнить проверку.');
    renderResult(result);
    await loadHistory();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false; button.textContent = 'Проверить карточку';
  }
});

addAttribute();
loadStatus().catch(console.error);
loadHistory().catch(console.error);
