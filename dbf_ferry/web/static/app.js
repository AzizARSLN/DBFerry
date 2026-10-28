/* DBFerry yapilandirma ekrani - istemci mantigi */
'use strict';

const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};

const state = {
  meta: null,
  profiles: [],
  jobType: 'file_to_server',
  mode: 'replace',
  jobId: null,
  es: null,
  plan: [],
  autoScroll: true,
  editingProfile: null,
  fsSel: null,
  fsMode: 'file',
  fsTarget: 'sourceDump',
  fsCur: '',
  logLines: [],
};

/* ------------------------------------------------------------------ api */

async function api(path, body) {
  const opts = body === undefined
    ? { method: 'GET' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  let data;
  try { data = await res.json(); } catch (e) { data = { ok: false, error: 'Sunucu yaniti okunamadi' }; }
  if (!res.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

/* --------------------------------------------------------------- toast */

function toast(msg, kind) {
  const t = el('div', 'toast ' + (kind || ''), msg);
  $('toasts').appendChild(t);
  setTimeout(() => {
    t.style.transition = '.3s'; t.style.opacity = '0'; t.style.transform = 'translateY(6px)';
    setTimeout(() => t.remove(), 320);
  }, kind === 'err' ? 7000 : 4200);
}

/* ------------------------------------------------------------- konsol */

function fmtTime(ts) {
  const d = new Date((ts || Date.now() / 1000) * 1000);
  return d.toTimeString().slice(0, 8);
}

function appendEvent(ev, live) {
  const box = $('console');
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const node = el('div', 'ev ev-' + ev.level);
  node.appendChild(el('span', 't', fmtTime(ev.ts)));
  node.appendChild(el('span', 'm', escapeHtml(ev.message)));
  box.appendChild(node);
  state.logLines.push('[' + fmtTime(ev.ts) + '] ' + ev.level.toUpperCase() + ' ' + ev.message);

  if (ev.level === 'progress') {
    setProgress(Math.min(99, ev.pct || 0));
  }
  if (ev.level === 'stage' && typeof ev.step === 'number') {
    markStep(ev.step - 1);
    setProgress(Math.round((ev.step - 1) / Math.max(1, ev.total || state.plan.length) * 100));
  }
  if (state.autoScroll && (nearBottom || live)) box.scrollTop = box.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}

function setProgress(pct) { $('progBar').style.width = Math.max(0, Math.min(100, pct)) + '%'; }
function setStatus(kind, text) {
  $('statusDot').className = 'status-dot' + (kind ? ' ' + kind : '');
  $('consoleTitle').textContent = 'dbf — ' + text;
}

/* --------------------------------------------------------------- plan */

function renderPlan(steps) {
  state.plan = steps || [];
  const strip = $('planStrip');
  strip.innerHTML = '';
  state.plan.forEach((s, i) => {
    const chip = el('div', 'step-chip');
    chip.dataset.idx = i;
    chip.title = s.detail || '';
    chip.innerHTML = '<span class="n">' + (i + 1) + '</span>' + escapeHtml(s.title);
    strip.appendChild(chip);
  });
}

function markStep(idx) {
  document.querySelectorAll('.step-chip').forEach((c) => {
    const i = Number(c.dataset.idx);
    c.classList.toggle('done', i < idx);
    c.classList.toggle('run', i === idx);
  });
}

function finishSteps() {
  document.querySelectorAll('.step-chip').forEach((c) => { c.classList.remove('run'); c.classList.add('done'); });
}

/* ---------------------------------------------------------- streaming */

function streamJob(jobId) {
  if (state.es) { state.es.close(); state.es = null; }
  state.jobId = jobId;
  state.logLines = [];
  $('console').innerHTML = '';
  setProgress(0);
  setStatus('running', 'çalışıyor');
  $('btnCancel').disabled = false;
  $('btnStart').disabled = true;
  $('btnInspect').disabled = true;

  const es = new EventSource('/api/jobs/' + jobId + '/events');
  state.es = es;
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    appendEvent(ev, true);
    if (ev.level === 'result') {
      const failed = /BASARISIZ|HATA/i.test(ev.message);
      const cancelled = /IPTAL/i.test(ev.message);
      setStatus(failed ? 'fail' : (cancelled ? '' : 'done'), failed ? 'hata' : (cancelled ? 'iptal' : 'tamamlandı'));
      if (ev.message.indexOf('SONUC:') === 0) {
        if (!failed && !cancelled) { setProgress(100); finishSteps(); }
        endJob();
      }
    }
  };
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) endJob();
  };
}

function endJob() {
  if (state.es) { state.es.close(); state.es = null; }
  $('btnCancel').disabled = true;
  $('btnStart').disabled = false;
  $('btnInspect').disabled = false;
  $('btnCancel').textContent = 'Durdur';
  loadHistory();
}

/* ----------------------------------------------------------- profiller */

function profileById(id) { return state.profiles.find((p) => p.id === id); }

function profileOptions(list) {
  return '<option value="">— seçin —</option>' + list.map(
    (p) => `<option value="${p.id}">${escapeHtml(p.name)} · ${p.is_local ? 'yerel' : escapeHtml(p.host)}</option>`).join('');
}

function renderProfileSelects() {
  const wantLocal = state.jobType === 'file_to_local';
  const remotes = state.profiles.filter((p) => !p.is_local);
  const locals = state.profiles.filter((p) => p.is_local);
  const tgtPool = wantLocal ? locals : remotes;
  const s1 = $('sourceProfile'), s2 = $('targetProfile');
  const keep1 = s1.value, keep2 = s2.value;
  s1.innerHTML = profileOptions(remotes);
  s2.innerHTML = profileOptions(tgtPool);
  s1.value = profileById(keep1) && !profileById(keep1).is_local ? keep1 : (remotes[0] ? remotes[0].id : '');
  s2.value = profileById(keep2) && !!profileById(keep2).is_local === wantLocal ? keep2 : (tgtPool[0] ? tgtPool[0].id : '');
  $('profCount').textContent = state.profiles.length;
  applyDefaults();
}

function applyDefaults() {
  const t = profileById($('targetProfile').value);
  const s = profileById($('sourceProfile').value);
  if (state.jobType === 'file_to_local') {
    $('targetDb').placeholder = 'yukarıdan seçin ya da adı yazın';
  } else {
    $('targetDb').placeholder = t ? t.db_name : '(profil varsayılanı)';
  }
  if (state.jobType === 'local_to_file') {
    $('sourceDb').placeholder = 'yukarıdan seçin';
  } else {
    $('sourceDb').placeholder = s ? s.db_name : '(profil varsayılanı)';
  }
  updateRisk();
}

function renderProfileList() {
  const box = $('profileList');
  box.innerHTML = '';
  if (!state.profiles.length) {
    box.appendChild(el('div', 'empty', 'Kayıtlı profil yok.<br>Aşağıdan uzak sunucu ya da yerel PostgreSQL ekleyin.'));
    return;
  }
  state.profiles.forEach((p) => {
    const card = el('div', 'pcard');
    const addr = p.is_local
      ? `${escapeHtml(p.db_user)}@${escapeHtml(p.db_host)}:${p.db_port}`
      : `${escapeHtml(p.user)}@${escapeHtml(p.host)}:${p.port} → ${escapeHtml(p.pg_container || '-')}`;
    card.innerHTML =
      `<div class="pn">${escapeHtml(p.name)} <span class="tag">${p.is_local ? 'bu bilgisayar' : 'uzak'}</span></div>
       <div class="pa">${addr} · ${escapeHtml(p.pg_mode === 'docker' ? 'docker' : 'doğrudan')}</div>
       <div class="pt">
         <span class="tag g">DB ${escapeHtml(p.db_name)}</span>
         <span class="tag">şema ${escapeHtml(p.db_schema)}</span>
         ${p.has_password ? '<span class="tag g">şifre ✓</span>' : '<span class="tag y">şifre yok</span>'}
         ${p.is_local ? '' : (p.hostkey ? '<span class="tag g">host key ✓</span>' : '<span class="tag y">host key yok</span>')}
       </div>`;
    card.onclick = () => openProfileModal(p);
    box.appendChild(card);
  });
}

async function loadProfiles() {
  const d = await api('/api/profiles');
  state.profiles = d.profiles || [];
  renderProfileSelects();
  renderProfileList();
}

/* ------------------------------------------------- profil düzenleyici */

function syncProfileModal() {
  const isLocal = $('p_location').value === 'local';
  $('sshFields').classList.toggle('hidden', isLocal);
  $('pgBinField').classList.toggle('hidden', !isLocal);
}

function openProfileModal(p) {
  state.editingProfile = p || null;
  $('profileModalTitle').textContent = p ? 'Profili Düzenle' : 'Profil Ekle';
  const set = (id, v) => { $(id).value = v === undefined || v === null ? '' : v; };
  set('p_name', p && p.name); set('p_location', p ? p.location : 'remote');
  set('p_host', p && p.host); set('p_port', p ? p.port : 22);
  set('p_user', p ? p.user : 'root'); set('p_password', '');
  set('p_hostkey', p && p.hostkey);
  set('p_pg_mode', p ? p.pg_mode : 'docker');
  set('p_pg_container', p ? p.pg_container : '');
  set('p_pg_bin', p && p.pg_bin);
  set('p_db_name', p ? p.db_name : '');
  set('p_db_user', p ? p.db_user : '');
  set('p_db_password', '');
  set('p_db_schema', p ? p.db_schema : 'public');
  set('p_db_host', p ? p.db_host : '127.0.0.1');
  set('p_db_port', p ? p.db_port : 5432);
  set('p_stop_containers', p && p.stop_containers);
  set('p_start_containers', p && p.start_containers);
  $('btnDeleteProfile').style.display = p ? '' : 'none';
  syncProfileModal();
  $('profileModal').classList.add('open');
}

async function saveProfile() {
  const payload = {
    id: state.editingProfile ? state.editingProfile.id : '',
    name: $('p_name').value.trim(),
    location: $('p_location').value,
    host: $('p_host').value.trim(),
    port: Number($('p_port').value || 22),
    user: $('p_user').value.trim() || 'root',
    password: $('p_password').value,
    hostkey: $('p_hostkey').value.trim(),
    pg_mode: $('p_pg_mode').value,
    pg_container: $('p_pg_container').value.trim(),
    pg_bin: $('p_pg_bin').value.trim(),
    db_name: $('p_db_name').value.trim(),
    db_user: $('p_db_user').value.trim(),
    db_password: $('p_db_password').value,
    db_schema: $('p_db_schema').value.trim() || 'public',
    db_host: $('p_db_host').value.trim() || '127.0.0.1',
    db_port: Number($('p_db_port').value || 5432),
    stop_containers: $('p_stop_containers').value.trim(),
    start_containers: $('p_start_containers').value.trim(),
  };
  if (payload.location !== 'local' && !payload.host) return toast('Sunucu adresi zorunlu', 'err');
  if (!payload.db_name) return toast('Veritabanı adı zorunlu', 'err');
  if (!payload.name) payload.name = payload.location === 'local' ? 'Yerel PostgreSQL' : payload.host;
  try {
    await api('/api/profiles', payload);
    await loadProfiles();
    $('profileModal').classList.remove('open');
    toast('Profil kaydedildi: ' + payload.name, 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

/* --------------------------------------------------------- iş tanımı */

function buildSpec() {
  const spec = {
    type: state.jobType,
    mode: state.mode,
    source_profile: $('sourceProfile').value,
    source_db: $('sourceDb').value.trim(),
    source_dump: $('sourceDump').value.trim(),
    target_profile: $('targetProfile').value,
    target_db: $('targetDb').value.trim(),
    new_db_name: $('newDbName').value.trim(),
    take_safety_backup: $('optSafety').checked,
    stop_services: $('optStop').checked,
    start_services: $('optStart').checked,
    stop_source_services: $('optStopSrc').checked,
    keep_local_copy: $('optKeepLocal').checked,
    verify: $('optVerify').checked,
    expect_migration: $('expectMigration').value.trim(),
    verify_queries: $('verifyQueries').value,
    local_db_host: $('localHost').value.trim() || '127.0.0.1',
    local_db_port: Number($('localPort').value || 5432),
    local_db_user: $('localUser').value.trim() || 'postgres',
    local_db_password: $('localPassword').value,
    local_pg_mode: $('localPgMode').value,
    local_pg_container: $('localPgContainer').value.trim() || 'postgres',
    local_pg_bin: $('localPgBin').value.trim(),
    local_copy_dir: $('localCopyDir').value.trim(),
  };
  if (['file_to_server', 'file_to_local', 'local_to_file'].includes(state.jobType)) spec.source_profile = '';
  return spec;
}

function validateSpec(spec) {
  const isFromFile = spec.type === 'file_to_server' || spec.type === 'file_to_local';
  if (isFromFile && !spec.source_dump) return 'Döküm dosyası seçin';
  if (spec.type === 'local_to_file') {
    if (!spec.local_db_password) return 'Yerel PostgreSQL şifresini girin';
    if (!spec.source_db) return 'Yedeklenecek veritabanını seçin';
    return null;
  }
  if (!isFromFile && !spec.source_profile) return 'Kaynak sunucu seçin';
  if (spec.type === 'file_to_local') {
    if (!spec.local_db_password) return 'Yerel PostgreSQL şifresini girin';
    if (!spec.target_db && !spec.new_db_name) return 'Hedef veritabanını seçin ya da adını yazın';
  } else if (spec.type !== 'server_to_file' && !spec.target_profile) {
    return 'Hedef sunucu seçin';
  }
  return null;
}

function updateRisk() {
  let band = $('riskBand');
  if (!band) {
    band = el('div', 'risk'); band.id = 'riskBand';
    document.querySelector('.actionbar').before(band);
  }
  const t = profileById($('targetProfile').value);
  const s = profileById($('sourceProfile').value);
  const jobType = state.jobType;
  const isBackup = jobType === 'server_to_file' || jobType === 'local_to_file';
  const toLocal = jobType === 'file_to_local';
  const where = toLocal ? 'bu bilgisayardaki' : 'hedefteki';
  let dbName;
  if (jobType === 'local_to_file') dbName = $('sourceDb').value.trim() || '(seçilmedi)';
  else if (toLocal) dbName = $('targetDb').value.trim() || $('newDbName').value.trim() || '(seçilmedi)';
  else dbName = $('targetDb').value.trim() || (t ? t.db_name : '?');
  const msgs = [];
  let level = 'high';

  if (isBackup) {
    level = 'medium';
    msgs.push('<b>YEDEK ALMA</b> — ' + (jobType === 'local_to_file' ? 'bu bilgisayardaki' : 'kaynak sunucudaki') +
      ' <b>' + escapeHtml(dbName) + '</b> veritabanı salt okunur olarak yedeklenir; hiçbir şey silinmez.');
  } else if (state.mode === 'replace') {
    msgs.push('<b>YIKICI İŞLEM</b> — ' + where + ' <b>' + escapeHtml(dbName) + '</b> veritabanı silinip yeniden oluşturulacak.');
    if (toLocal) msgs.push('Yereldeki <b>' + escapeHtml(dbName) + '</b> veritabanı kalıcı olarak silinir.');
    if (t && (t.stop_containers || '').split(',').filter((x) => x.trim()).length) msgs.push('Durdurulacak: ' + escapeHtml(t.stop_containers));
    else msgs.push((toLocal ? 'Yerelde' : 'Hedefte') + ' durdurulacak servis tanımlı değil — yazma trafiği açıkken geri yükleme risklidir.');
    if (!s && jobType === 'server_to_server') msgs.push('Kaynak sunucu seçilmedi.');
  } else {
    level = 'medium';
    msgs.push('<b>GÜVENLİ MOD</b> — mevcut veritabanı korunur, ayrı bir veritabanına yüklenir.');
  }
  if ((toLocal || jobType === 'local_to_file') && !$('localPassword').value) {
    msgs.push('Yerel PostgreSQL şifresi girilmedi.');
  }
  if (jobType === 'local_to_file' && !$('sourceDb').value.trim()) {
    msgs.push('Yedeklenecek veritabanı seçilmedi.');
  }
  if (!isBackup) {
    if ($('optSafety').checked) msgs.push('Güvenlik yedeği alınacak.');
    else if (state.mode === 'replace') msgs.push('<b>Güvenlik yedeği kapalı</b> — geri dönüş yolu yok.');
  }

  band.className = 'risk' + (level === 'medium' ? ' medium' : '');
  band.innerHTML = msgs.map((m) => '• ' + m).join('<br>');
}

/* -------------------------------------------------------------- işlem */

async function doPlan() {
  const spec = buildSpec();
  const err = validateSpec(spec);
  if (err) return toast(err, 'err');
  try {
    const d = await api('/api/plan', { spec });
    renderPlan(d.plan);
    $('console').innerHTML = '';
    appendEvent({ ts: Date.now() / 1000, level: 'stage', message: 'PLAN ÖNİZLEME — ' + d.summary }, false);
    d.plan.forEach((s, i) => appendEvent({
      ts: Date.now() / 1000, level: 'plan', message: `  ${String(i + 1).padStart(2)}. ${s.title.padEnd(36)} ${s.detail}`
    }, false));
    setStatus('', 'plan hazır');
    toast('Plan oluşturuldu: ' + d.plan.length + ' adım', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

async function doInspect() {
  const spec = buildSpec();
  const err = validateSpec(spec);
  if (err) return toast(err, 'err');
  try {
    const d = await api('/api/inspect', { spec });
    renderPlan([{ title: 'Denetim', detail: 'salt okunur' }]);
    streamJob(d.job_id);
    toast('Ortam denetimi başladı (hiçbir değişiklik yapılmaz)', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

async function doLocalConnect() {
  const btn = $('btnLocalConnect');
  const info = $('localInfo');
  btn.disabled = true;
  info.textContent = 'Yerel PostgreSQL’e bağlanılıyor…';
  try {
    const d = await api('/api/local/dbs', {
      db_host: $('localHost').value.trim() || '127.0.0.1',
      db_port: Number($('localPort').value || 5432),
      db_user: $('localUser').value.trim() || 'postgres',
      db_password: $('localPassword').value,
      pg_mode: $('localPgMode').value,
      pg_container: $('localPgContainer').value.trim() || 'postgres',
      pg_bin: $('localPgBin').value.trim(),
      name: 'Yerel PostgreSQL',
    });
    const sel = $('localDbSel');
    sel.innerHTML = '<option value="">— veritabanı seçin —</option>' +
      (d.databases || []).map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join('');
    if (d.databases && d.databases.length === 1) {
      sel.value = d.databases[0];
      if (state.jobType === 'local_to_file') $('sourceDb').value = d.databases[0];
      else $('targetDb').value = d.databases[0];
    }
    info.textContent = `${d.databases.length} veritabanı bulundu · ${d.version} · ${d.mode}`;
    toast('Yerel PostgreSQL bağlantısı başarılı', 'ok');
    updateRisk();
  } catch (e) {
    info.textContent = 'Bağlantı kurulamadı: ' + e.message;
    toast(e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

async function doStart() {
  const spec = buildSpec();
  const err = validateSpec(spec);
  if (err) return toast(err, 'err');

  if (state.mode === 'replace' && state.jobType !== 'server_to_file' && state.jobType !== 'local_to_file') {
    const isLocal = state.jobType === 'file_to_local';
    const t = profileById($('targetProfile').value);
    const dbName = $('targetDb').value.trim()
      || (isLocal ? $('newDbName').value.trim() : (t ? t.db_name : ''));
    const typed = prompt(
      'DİKKAT: Yıkıcı işlem.\n\n' + (isLocal ? 'Yereldeki' : 'Hedefteki') + ' "' + dbName + '" veritabanı SİLİNECEK ve yerine yedek yüklenecek.\n' +
      'Onaylamak için veritabanı adını yazın:');
    if (typed === null) return;
    if (typed.trim() !== dbName) return toast('Onay eşleşmedi, işlem başlatılmadı', 'warn');
  }

  try {
    const d = await api('/api/plan', { spec });
    renderPlan(d.plan);
    const r = await api('/api/run', { spec });
    streamJob(r.job_id);
    toast('İş başlatıldı: ' + r.job_id, 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

async function doCancel() {
  if (!state.jobId) return;
  $('btnCancel').disabled = true;
  $('btnCancel').textContent = 'Durduruluyor…';
  try { await api('/api/jobs/' + state.jobId + '/cancel', {}); } catch (e) { /* yoksay */ }
}

/* ------------------------------------------------------------- geçmiş */

async function loadHistory() {
  try {
    const d = await api('/api/history');
    const box = $('historyList');
    box.innerHTML = '';
    if (!d.items.length) { box.appendChild(el('div', 'empty', 'Henüz iş çalıştırılmadı.')); return; }
    d.items.forEach((it) => {
      const when = new Date((it.started_at || 0) * 1000);
      const card = el('div', 'pcard');
      const cls = it.status === 'tamam' ? 'g' : 'y';
      card.innerHTML =
        `<div class="pn">${escapeHtml(it.summary || it.type)} <span class="tag ${cls}">${escapeHtml(it.status)}</span></div>
         <div class="pa">${when.toLocaleString('tr-TR')} · ${(it.duration || 0).toFixed(1)} sn · ${escapeHtml(it.id)}</div>`;
      box.appendChild(card);
    });
  } catch (e) { /* sessiz */ }
}

/* -------------------------------------------------- yerel dosya seçici */

function openFs(mode, targetId, startPath) {
  state.fsMode = mode;
  state.fsTarget = targetId;
  $('fsTitle').textContent = mode === 'dir' ? 'Yedek Klasörü Seç' : 'Döküm Dosyası Seç';
  $('btnFsPick').classList.toggle('hidden', mode !== 'file');
  $('btnFsPickDir').classList.toggle('hidden', mode !== 'dir');
  $('fileModal').classList.add('open');
  fsGo(startPath || '');
}

async function fsGo(path) {
  try {
    const d = await api('/api/fs?path=' + encodeURIComponent(path || ''));
    $('fsPath').value = d.path;
    state.fsSel = null;
    const list = $('fsList');
    list.innerHTML = '';
    if (d.parent) {
      const up = el('div', 'fs-item', '<span class="ic">↰</span><span>.. (üst klasör)</span>');
      up.onclick = () => fsGo(d.parent);
      list.appendChild(up);
    }
    d.dirs.forEach((dir) => {
      const n = el('div', 'fs-item', '<span class="ic">📁</span><span>' + escapeHtml(dir.name) + '</span>');
      n.onclick = () => fsGo(dir.path);
      list.appendChild(n);
    });
    d.files.forEach((f) => {
      const n = el('div', 'fs-item',
        '<span class="ic">🗄</span><span>' + escapeHtml(f.name) + '</span><span class="sz">' + f.size + '</span>');
      n.onclick = () => {
        list.querySelectorAll('.fs-item').forEach((x) => x.classList.remove('sel'));
        n.classList.add('sel');
        state.fsSel = f.path;
        $('fsHint').textContent = f.path;
      };
      if (f.name.endsWith('.dump') || f.name.endsWith('.sql') || f.name.endsWith('.backup')) n.style.opacity = '1';
      list.appendChild(n);
    });
    $('fsHint').textContent = state.fsMode === 'dir'
      ? 'Seçilecek klasör: ' + d.path
      : (d.dirs.length + ' klasör, ' + d.files.length + ' dosya');
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------------------------------------------------------- init */

function renderTabs() {
  const tabs = $('jobTabs');
  tabs.innerHTML = '';
  Object.entries(state.meta.job_types).forEach(([key, label]) => {
    const b = el('button', 'tab' + (key === state.jobType ? ' active' : ''), label.split('(')[0].trim());
    b.onclick = () => { state.jobType = key; renderTabs(); renderBlocks(); renderProfileSelects(); };
    tabs.appendChild(b);
  });
}

function renderBlocks() {
  const t = state.jobType;
  const isFile = t === 'file_to_server' || t === 'file_to_local';
  const isToFile = t === 'server_to_file';
  const isToLocal = t === 'file_to_local';
  const isLocalToFile = t === 'local_to_file';
  const hasSourceServer = !isFile && !isLocalToFile;
  const hasTarget = !isToFile && !isLocalToFile;
  $('blockSourceFile').classList.toggle('hidden', !isFile);
  $('blockSourceServer').classList.toggle('hidden', !hasSourceServer);
  $('blockSourceDb').classList.toggle('hidden', !hasSourceServer);
  document.querySelectorAll('.tgt-block').forEach((n) => n.classList.toggle('hidden', !hasTarget));
  $('blockTargetProfile').classList.toggle('hidden', !hasTarget || isToLocal);
  $('localBlock').classList.toggle('hidden', !(isToLocal || isLocalToFile));
  $('localOutBlock').classList.toggle('hidden', !isLocalToFile);
  $('blockNewDb').classList.toggle('hidden', !hasTarget || state.mode !== 'new_db');
  $('optStopSrc').parentElement.classList.toggle('dim', isFile);
  $('optStopSrc').parentElement.querySelector('span').textContent =
    isLocalToFile ? 'Yedek öncesi yerel servisleri durdur' : 'Kaynak servisleri de durdur';
  $('optKeepLocal').parentElement.classList.toggle('hidden', !isToFile);
  ['optSafety', 'optStop', 'optStart', 'optVerify'].forEach((id) => {
    $(id).parentElement.classList.toggle('hidden', isLocalToFile);
  });
  $('tgtProfileLabel').textContent = 'Hedef sunucu';
  $('tgtDbLabel').textContent = isToLocal ? 'Hedef veritabanı (yerelde)' : 'Hedef veritabanı';
  $('targetDb').placeholder = isToLocal ? 'yukarıdan seçin ya da adı yazın' : '(profil varsayılanı)';
  $('lblOptStop').textContent = isToLocal ? 'Yerel servisleri durdur' : 'Hedef servisleri durdur';
  $('lblOptStart').textContent = isToLocal ? 'İş sonrası yerel servisleri başlat' : 'İş sonrası servisleri başlat';
  $('jobHint').textContent = {
    file_to_server: 'Yerel döküm dosyası → uzak sunucu',
    file_to_local: 'Yerel döküm dosyası → bu bilgisayardaki PostgreSQL (şifre girin, veritabanlarını listeleyin)',
    server_to_server: 'Bir sunucudan canlı kopya → diğer sunucu',
    server_to_file: 'Uzak sunucudan yerel diske yedek',
    local_to_file: 'Bu bilgisayardaki PostgreSQL → yerel .dump yedeği',
  }[t];
  $('btnStart').textContent = (isToFile || isLocalToFile) ? 'Yedeği Al' : 'Süreci Başlat';
  syncLocalBlock();
  updateRisk();
}

function syncLocalBlock() {
  const local = $('localPgMode').value === 'local';
  $('localPgContainer').closest('.field').classList.toggle('hidden', local);
  $('localPgBin').closest('.field').classList.toggle('hidden', !local);
  const toFile = state.jobType === 'local_to_file';
  $('localDbLabel').textContent = toFile ? 'Yedeklenecek yerel veritabanı' : 'Yereldeki veritabanları';
}

function renderModeSeg() {
  const seg = $('modeSeg');
  seg.innerHTML = '';
  Object.entries(state.meta.restore_modes).forEach(([key, label]) => {
    const o = el('div', 'opt' + (key === state.mode ? ' active' : ''), key === 'replace' ? 'Üzerine yaz' : 'Ayrı DB');
    o.title = label;
    o.onclick = () => { state.mode = key; renderModeSeg(); renderBlocks(); };
    seg.appendChild(o);
  });
}

function bind() {
  $('btnProfiles').onclick = () => { renderProfileList(); $('profilesDrawer').classList.add('open'); };
  $('btnHistory').onclick = () => { loadHistory(); $('historyDrawer').classList.add('open'); };
  $('btnNewProfile').onclick = () => openProfileModal(null);
  $('p_location').onchange = syncProfileModal;
  $('btnSaveProfile').onclick = saveProfile;
  $('btnDeleteProfile').onclick = async () => {
    const p = state.editingProfile;
    if (!p) return;
    if (!confirm('"' + p.name + '" profili silinsin mi?')) return;
    await api('/api/profiles/delete', { id: p.id });
    await loadProfiles();
    $('profileModal').classList.remove('open');
    toast('Profil silindi', 'ok');
  };
  $('btnGetKey').onclick = async () => {
    const host = $('p_host').value.trim();
    if (!host) return toast('Önce sunucu adresini girin', 'err');
    $('btnGetKey').textContent = '…';
    try {
      const d = await api('/api/hostkey', { host, port: Number($('p_port').value || 22) });
      $('p_hostkey').value = d.hostkey;
      toast('Host key alındı: ' + d.key_type, 'ok');
    } catch (e) { toast(e.message, 'err'); }
    $('btnGetKey').textContent = 'Anahtarı Getir';
  };
  $('btnPlan').onclick = doPlan;
  $('btnInspect').onclick = doInspect;
  $('btnStart').onclick = doStart;
  $('btnCancel').onclick = doCancel;
  $('btnBrowseFile').onclick = () => openFs('file', 'sourceDump', $('sourceDump').value || '');
  $('btnBrowseDir').onclick = () => openFs('dir', 'localCopyDir',
    $('localCopyDir').value || (state.meta && state.meta.data_dir) || '');
  $('btnLocalConnect').onclick = doLocalConnect;
  $('localPgMode').onchange = syncLocalBlock;
  $('localPassword').oninput = updateRisk;
  $('targetDb').oninput = updateRisk;
  $('sourceDb').oninput = updateRisk;
  $('localCopyDir').oninput = updateRisk;
  $('localDbSel').onchange = () => {
    const v = $('localDbSel').value;
    if (v) {
      if (state.jobType === 'local_to_file') $('sourceDb').value = v;
      else $('targetDb').value = v;
    }
    updateRisk();
  };
  $('btnFsGo').onclick = () => fsGo($('fsPath').value);
  $('fsPath').onkeydown = (e) => { if (e.key === 'Enter') fsGo($('fsPath').value); };
  $('btnFsPick').onclick = () => {
    if (!state.fsSel) return toast('Bir dosya seçin', 'warn');
    $(state.fsTarget || 'sourceDump').value = state.fsSel;
    $('fileModal').classList.remove('open');
    updateRisk();
    toast('Dosya seçildi', 'ok');
  };
  $('btnFsPickDir').onclick = () => {
    const dir = $('fsPath').value.trim();
    if (!dir) return toast('Bir klasör açın', 'warn');
    $(state.fsTarget || 'localCopyDir').value = dir;
    $('fileModal').classList.remove('open');
    updateRisk();
    toast('Klasör seçildi', 'ok');
  };
  $('btnClear').onclick = () => { $('console').innerHTML = ''; };
  $('btnScroll').onclick = () => {
    state.autoScroll = !state.autoScroll;
    $('btnScroll').style.color = state.autoScroll ? '' : '#ffc247';
  };
  $('btnSaveLog').onclick = () => {
    const blob = new Blob([state.logLines.join('\n')], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'dbf-konsol-' + new Date().toISOString().replace(/[:.]/g, '-') + '.log';
    a.click();
  };
  ['sourceProfile', 'targetProfile', 'targetDb'].forEach((id) => {
    $(id).addEventListener('change', updateRisk);
    $(id).addEventListener('input', updateRisk);
  });
  ['optSafety', 'optStop', 'optVerify'].forEach((id) => $(id).addEventListener('change', updateRisk));

  document.querySelectorAll('[data-close]').forEach((n) => {
    n.onclick = () => {
      const wrap = n.closest('.drawer-wrap, .modal-wrap');
      if (wrap) wrap.classList.remove('open');
    };
  });
}

async function init() {
  try {
    state.meta = await api('/api/meta');
  } catch (e) {
    document.body.innerHTML = '<div style="padding:40px;font-family:monospace">Sunucuya bağlanılamadı: ' + e.message + '</div>';
    return;
  }
  $('version').textContent = 'v' + state.meta.version;
  $('tagline').textContent = state.meta.tagline;
  renderTabs();
  renderModeSeg();
  renderBlocks();
  bind();
  window.updateRisk = updateRisk;
  await loadProfiles();
  setStatus('', 'hazır');
  appendEvent({
    ts: Date.now() / 1000, level: 'info',
    message: 'DBFerry v' + state.meta.version + ' — profil klasörü: ' + state.meta.data_dir
  }, false);
  appendEvent({
    ts: Date.now() / 1000, level: 'info',
    message: 'Başlamak için soldan bir iş tipi seçin, sunucularınızı tanımlayın ve "Planı Göster" ile adımları inceleyin.'
  }, false);
}

init();
