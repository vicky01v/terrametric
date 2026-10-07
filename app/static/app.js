(() => {
  const $ = (id) => document.getElementById(id);
  const state = { file: null, page: 1, pageSize: 100, pages: 1, search: '', type: 'all', layer: null, toastTimer: null };
  const map = window.L ? L.map('map', { zoomControl: true, scrollWheelZoom: false }).setView([20, 0], 2) : null;
  if (map) L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);

  function toast(message) {
    const el = $('toast'); el.textContent = message; el.classList.add('show');
    clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => el.classList.remove('show'), 3600);
  }
  function formatNumber(value, maximum = 2) {
    return new Intl.NumberFormat(undefined, { maximumFractionDigits: maximum }).format(value || 0);
  }
  function prettyName(value) { return String(value || '').replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase()); }
  function activeProperties(properties) {
    if (!properties || !Object.keys(properties).length) return '—';
    const entries = Object.entries(properties).filter(([, v]) => v !== null && v !== '').slice(0, 3);
    return entries.map(([key, value]) => `${prettyName(key)}: ${String(value).slice(0, 46)}`).join(' · ') || '—';
  }
  function geomClass(type) {
    if (/Line/i.test(type)) return 'line';
    if (/Point/i.test(type)) return 'point';
    return '';
  }
  function displayMeasurement(feature) {
    const m = feature.measurement;
    if (!m) return `<span title="${escapeHtml(feature.measurement_note || 'No measurement')}">— <small>${escapeHtml(feature.measurement_note || 'not measured')}</small></span>`;
    if (m.kind === 'area') return `${formatNumber(m.hectares, 3)} <small>ha</small>`;
    if (m.kind === 'length') return `${formatNumber(m.kilometres, 3)} <small>km</small>`;
    return '—';
  }
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
  }

  async function refreshRecent() {
    try {
      const response = await fetch('/api/files/?limit=30');
      if (!response.ok) throw new Error();
      const data = await response.json();
      $('api-status').textContent = 'CONNECTED';
      $('upload-count').textContent = data.files.length;
      const list = $('recent-list');
      if (!data.files.length) list.innerHTML = '<div class="empty-recent">Your processed files<br/>will appear here.</div>';
      else list.innerHTML = data.files.map(file => `<div class="recent-file ${state.file?.id === file.id ? 'selected' : ''}" data-id="${escapeHtml(file.id)}"><span class="rf-icon">${file.filename.toLowerCase().endsWith('.kml') ? 'KML' : 'SHP'}</span><span><b>${escapeHtml(file.filename)}</b><small>${formatNumber(file.feature_count, 0)} features · ${new Date(file.created_at).toLocaleDateString()}</small></span></div>`).join('');
    } catch { $('api-status').textContent = 'OFFLINE'; }
  }

  async function selectFile(id) {
    try {
      const response = await fetch(`/api/files/${encodeURIComponent(id)}/`);
      if (!response.ok) throw new Error('This dataset could not be loaded.');
      const file = await response.json();
      state.file = file; state.page = 1;
      $('file-status').textContent = file.status; $('file-status').classList.add('ready');
      $('detail-filename').textContent = file.filename; $('detail-filename').title = file.filename;
      $('detail-date').textContent = new Date(file.created_at).toLocaleString();
      $('detail-source-crs').textContent = file.crs; $('source-crs').textContent = file.crs;
      $('detail-area-crs').textContent = file.measurement_crs.area; $('detail-length-crs').textContent = file.measurement_crs.length;
      $('measurement-crs').textContent = file.measurement_crs.area === file.measurement_crs.length ? file.measurement_crs.area : 'area + length';
      $('feature-count').textContent = formatNumber(file.feature_count, 0);
      $('geometry-counts').textContent = Object.entries(file.geometry_counts).map(([k,v]) => `${formatNumber(v,0)} ${k}`).join(' · ');
      $('map-label').textContent = `${file.feature_count} FEATURES · ${file.crs}`;
      $('download-button').disabled = false;
      const warnings = file.warnings || [];
      if (warnings.length) showNotice(warnings.join(' '), false); else showNotice('Dataset processed successfully. Coordinates were projected before area and distance calculations.', true);
      await Promise.all([loadMeasurements(), loadMap()]);
      await refreshRecent();
      if (map) setTimeout(() => map.invalidateSize(), 60);
    } catch (error) { toast(error.message || 'Unable to load dataset.'); }
  }

  function showNotice(message, success) {
    const box = $('notice'); box.textContent = message; box.className = `notice show${success ? ' success' : ''}`;
  }

  async function loadMeasurements() {
    if (!state.file) return;
    const params = new URLSearchParams({ page: state.page, page_size: state.pageSize });
    if (state.type !== 'all') params.set('geometry_type', state.type);
    if (state.search) params.set('search', state.search);
    const response = await fetch(`${state.file.measurements_url}?${params}`);
    if (!response.ok) throw new Error('Could not load measurements.');
    const result = await response.json();
    const t = result.totals;
    $('total-area').innerHTML = `${formatNumber(t.area_hectares, 3)} <small>ha</small>`;
    $('area-count').textContent = `${formatNumber(t.area_features, 0)} measured ${t.area_features === 1 ? 'polygon' : 'polygons'}`;
    $('total-length').innerHTML = `${formatNumber(t.length_km, 3)} <small>km</small>`;
    $('length-count').textContent = `${formatNumber(t.length_features, 0)} measured ${t.length_features === 1 ? 'line' : 'lines'}`;
    $('table-total').textContent = formatNumber(result.pagination.total, 0);
    state.pages = Math.max(result.pagination.pages, 1);
    $('page-display').textContent = `${state.page} / ${state.pages}`;
    $('prev-page').disabled = state.page <= 1; $('next-page').disabled = state.page >= state.pages;
    const start = result.pagination.total ? (state.page - 1) * state.pageSize + 1 : 0;
    const end = Math.min(state.page * state.pageSize, result.pagination.total);
    $('table-summary').textContent = `Showing ${start}–${end} of ${formatNumber(result.pagination.total, 0)} features`;
    const body = $('feature-rows');
    if (!result.features.length) {
      body.innerHTML = `<tr class="table-placeholder"><td colspan="5"><span class="placeholder-mark">⌕</span>No features match these filters</td></tr>`;
      return;
    }
    body.innerHTML = result.features.map(f => `<tr><td><span class="feature-id">#${String(f.id).padStart(3, '0')}</span></td><td><span class="geometry-pill ${geomClass(f.geometry_type)}"><i></i>${escapeHtml(f.geometry_type)}</span></td><td title="${escapeHtml(activeProperties(f.properties))}">${escapeHtml(activeProperties(f.properties))}</td><td class="measure-cell">${displayMeasurement(f)}</td><td><span class="feature-id">${escapeHtml(f.crs)}</span></td></tr>`).join('');
  }

  async function loadMap() {
    if (!state.file || !map) return;
    const response = await fetch(state.file.geojson_url);
    if (!response.ok) return;
    const geojson = await response.json();
    if (state.layer) map.removeLayer(state.layer);
    state.layer = L.geoJSON(geojson, {
      style: feature => {
        const type = feature.properties?._geometry_type || '';
        return { color: /Line/i.test(type) ? '#d8874f' : '#37865a', weight: /Line/i.test(type) ? 3 : 2, fillColor: '#65aa78', fillOpacity: .2 };
      },
      pointToLayer: (_feature, latlng) => L.circleMarker(latlng, { radius: 5, color: '#567c9b', fillColor: '#7395b1', fillOpacity: .9, weight: 1.5 }),
      onEachFeature: (feature, layer) => {
        const p = feature.properties || {};
        const type = p._geometry_type || 'Feature';
        const m = p._measurement;
        const measure = m?.kind === 'area' ? `${formatNumber(m.hectares, 3)} ha` : m?.kind === 'length' ? `${formatNumber(m.kilometres, 3)} km` : 'Not measured';
        layer.bindPopup(`<b>${escapeHtml(type)}</b><br/>${escapeHtml(measure)}<br/><span style="color:#879188">${escapeHtml(activeProperties(p))}</span>`);
      }
    }).addTo(map);
    const bounds = state.layer.getBounds();
    if (bounds.isValid()) map.fitBounds(bounds.pad(.12), { maxZoom: 15 });
    $('map-empty').style.display = 'none';
  }

  function handleFiles(files) {
    if (!files?.length) return;
    const file = files[0];
    if (!/\.(kml|zip)$/i.test(file.name)) { toast('Choose a .kml file or a .zip Shapefile archive.'); return; }
    upload(file);
  }
  async function upload(file) {
    const zone = $('drop-zone'); const progress = $('upload-progress');
    $('notice').className = 'notice'; progress.classList.add('show'); $('progress-name').textContent = file.name;
    $('progress-bar').style.width = '7%'; $('progress-pct').textContent = '7%';
    let pct = 7;
    const timer = setInterval(() => { pct = Math.min(pct + Math.max(1, Math.round((92 - pct) * .12)), 92); $('progress-bar').style.width = `${pct}%`; $('progress-pct').textContent = `${pct}%`; }, 220);
    zone.classList.add('busy');
    try {
      const form = new FormData(); form.append('file', file);
      const response = await fetch('/api/files/', { method: 'POST', body: form });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'Upload failed.');
      $('progress-bar').style.width = '100%'; $('progress-pct').textContent = '100%';
      clearInterval(timer);
      await selectFile(result.id);
      toast(`${result.filename} processed · ${formatNumber(result.feature_count, 0)} features`);
    } catch (error) { clearInterval(timer); showNotice(error.message || 'Upload failed.', false); toast(error.message || 'Upload failed.'); }
    finally { setTimeout(() => progress.classList.remove('show'), 650); zone.classList.remove('busy'); }
  }

  $('browse-button').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', event => { handleFiles(event.target.files); event.target.value = ''; });
  const drop = $('drop-zone');
  drop.addEventListener('dragover', event => { event.preventDefault(); drop.classList.add('dragging'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('dragging'));
  drop.addEventListener('drop', event => { event.preventDefault(); drop.classList.remove('dragging'); handleFiles(event.dataTransfer.files); });
  $('recent-list').addEventListener('click', event => { const item = event.target.closest('.recent-file'); if (item) selectFile(item.dataset.id); });
  $('refresh-files').addEventListener('click', refreshRecent);
  $('download-button').addEventListener('click', () => { if (state.file) window.location.href = `${state.file.geojson_url}?download=true`; });
  $('prev-page').addEventListener('click', () => { if (state.page > 1) { state.page--; loadMeasurements(); } });
  $('next-page').addEventListener('click', () => { if (state.page < state.pages) { state.page++; loadMeasurements(); } });
  $('type-filter').addEventListener('change', event => { state.type = event.target.value; state.page = 1; loadMeasurements(); });
  let searchTimer;
  $('search-input').addEventListener('input', event => { clearTimeout(searchTimer); state.search = event.target.value.trim(); searchTimer = setTimeout(() => { state.page = 1; loadMeasurements(); }, 250); });

  refreshRecent();
})();
