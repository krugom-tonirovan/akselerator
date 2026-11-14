export function mountLegacyApp(host) {
  const mapEl = host.querySelector('#map-viewport');
  const tileLayer = host.querySelector('#tile-layer');
  const overlay = host.querySelector('#map-overlay');
  const parcelLayer = host.querySelector('#parcel-layer');
  const zoneLayer = host.querySelector('#zone-layer');
  const recommendationLayer = host.querySelector('#recommendation-layer');
  const drawingLayer = host.querySelector('#drawing-layer');
  const $ = (id) => host.querySelector(`#${id}`);

  const REGION = { lat: 46.3727, lon: 44.3301, zoom: 13 };
  const TILE_LAYERS = {
    streets: {
      url: (z, x, y) => `https://tile.openstreetmap.org/${z}/${x}/${y}.png`,
      attribution: '© OpenStreetMap contributors',
    },
    satellite: {
      url: (z, x, y) => `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${z}/${y}/${x}`,
      attribution: 'Source: Esri, Maxar, Earthstar Geographics, GIS User Community',
    },
  };
  const DIR_NAMES = ['С', 'СВ', 'В', 'ЮВ', 'Ю', 'ЮЗ', 'З', 'СЗ'];
  const state = {
    center: { lat: REGION.lat, lon: REGION.lon },
    zoom: REGION.zoom,
    baseLayer: 'streets',
    parcel: null,
    options: [],
    activeOption: 0,
    result: null,
    availableBelts: [],
    selectedBeltIds: [],
    recommendedBeltIds: [],
    drawing: false,
    drawingKind: null,
    drawPoints: [],
    drawHistory: [],
    selectedVertex: -1,
    drawGesture: null,
    draggingVertex: false,
    drawPath: null,
    panning: false,
    panStart: null,
    tileGeneration: 0,
    loadedTiles: 0,
    failedTiles: 0,
    fallbackTimer: null,
    modelReady: false,
    gisStatus: null,
    referenceScenario: null,
  };

  function worldSize(zoom = state.zoom) { return 256 * (2 ** zoom); }
  function lonLatToWorld(lon, lat, zoom = state.zoom) {
    const size = worldSize(zoom);
    const clamped = Math.max(-85.05112878, Math.min(85.05112878, lat));
    const sin = Math.sin(clamped * Math.PI / 180);
    return {
      x: (lon + 180) / 360 * size,
      y: (0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size,
    };
  }
  function worldToLonLat(x, y, zoom = state.zoom) {
    const size = worldSize(zoom);
    const lon = x / size * 360 - 180;
    const n = Math.PI - 2 * Math.PI * y / size;
    const lat = 180 / Math.PI * Math.atan(Math.sinh(n));
    return { lat, lon };
  }
  function screenToGeo(x, y) {
    const rect = mapEl.getBoundingClientRect();
    const center = lonLatToWorld(state.center.lon, state.center.lat);
    return worldToLonLat(center.x + x - rect.width / 2, center.y + y - rect.height / 2);
  }
  function eventToScreen(event) {
    const rect = mapEl.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }
  function geoToScreen(lon, lat) {
    const rect = mapEl.getBoundingClientRect();
    const p = lonLatToWorld(lon, lat);
    const c = lonLatToWorld(state.center.lon, state.center.lat);
    return { x: rect.width / 2 + p.x - c.x, y: rect.height / 2 + p.y - c.y };
  }
  function renderTiles() {
    const rect = mapEl.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const generation = ++state.tileGeneration;
    state.loadedTiles = 0;
    state.failedTiles = 0;
    $('map-fallback').hidden = true;
    window.clearTimeout(state.fallbackTimer);
    const center = lonLatToWorld(state.center.lon, state.center.lat);
    const left = center.x - rect.width / 2;
    const top = center.y - rect.height / 2;
    const firstX = Math.floor(left / 256);
    const lastX = Math.floor((left + rect.width) / 256);
    const firstY = Math.floor(top / 256);
    const lastY = Math.floor((top + rect.height) / 256);
    const totalTiles = (lastX - firstX + 1) * (lastY - firstY + 1);
    const maxTile = 2 ** state.zoom;
    const layer = TILE_LAYERS[state.baseLayer];
    const fragment = document.createDocumentFragment();

    for (let x = firstX; x <= lastX; x += 1) {
      for (let y = firstY; y <= lastY; y += 1) {
        if (y < 0 || y >= maxTile) continue;
        const wrappedX = ((x % maxTile) + maxTile) % maxTile;
        const image = document.createElement('img');
        image.className = 'map-tile';
        image.alt = '';
        image.draggable = false;
        image.decoding = 'async';
        image.style.left = `${x * 256 - left}px`;
        image.style.top = `${y * 256 - top}px`;
        image.onload = () => {
          if (generation !== state.tileGeneration) return;
          state.loadedTiles += 1;
          $('map-fallback').hidden = true;
        };
        image.onerror = () => {
          if (generation !== state.tileGeneration) return;
          state.failedTiles += 1;
          image.remove();
          if (state.failedTiles >= totalTiles && state.loadedTiles === 0) $('map-fallback').hidden = false;
        };
        image.src = layer.url(state.zoom, wrappedX, y);
        fragment.appendChild(image);
      }
    }
    tileLayer.replaceChildren(fragment);
    $('map-attribution').textContent = layer.attribution;
    state.fallbackTimer = window.setTimeout(() => {
      if (generation === state.tileGeneration && state.loadedTiles === 0) $('map-fallback').hidden = false;
    }, 4500);
    $('zoom-level').textContent = state.zoom;
    $('footer-zoom').textContent = zoomScale(state.zoom);
    updateScale();
    renderOverlay();
    updateCoordinates();
  }

  function zoomScale(zoom) {
    const denominator = Math.round(10000000 / (2 ** zoom));
    return `1:${denominator.toLocaleString('ru-RU')}`;
  }
  function updateScale() {
    const metersPerPixel = 156543.03392 * Math.cos(state.center.lat * Math.PI / 180) / (2 ** state.zoom);
    const targets = [10000, 5000, 2000, 1000, 500, 200, 100, 50, 20, 10];
    const meters = targets.find((target) => target / metersPerPixel < 68) || 10;
    const width = meters / metersPerPixel;
    host.querySelector('.scale-bar span').style.width = `${Math.max(24, Math.min(76, width))}px`;
    $('scale-label').textContent = meters >= 1000 ? `${meters / 1000} км` : `${meters} м`;
  }
  function updateCoordinates() {
    $('map-coordinates').textContent = `${Math.abs(state.center.lat).toFixed(2)}° ${state.center.lat >= 0 ? 'с. ш.' : 'ю. ш.'}   ${Math.abs(state.center.lon).toFixed(2)}° ${state.center.lon >= 0 ? 'в. д.' : 'з. д.'}`;
  }
  function svgNode(tag, attrs = {}) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  }
  function ringPath(points) {
    return points.map((point, index) => {
      const [lon, lat] = point;
      const p = geoToScreen(lon, lat);
      return `${index ? 'L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`;
    }).join(' ') + ' Z';
  }
  function renderOverlay() {
    const rect = mapEl.getBoundingClientRect();
    overlay.setAttribute('viewBox', `0 0 ${rect.width} ${rect.height}`);
    parcelLayer.replaceChildren();
    zoneLayer.replaceChildren();
    recommendationLayer.replaceChildren();
    const visibleParcel = state.drawing ? state.drawPoints : state.parcel;
    if (visibleParcel?.length >= 3) {
      const geoRing = visibleParcel.map(({ lon, lat }) => [lon, lat]);
      const path = ringPath(geoRing);
      $('parcel-clip-path').setAttribute('d', path);
      parcelLayer.appendChild(svgNode('path', { d: path, class: 'parcel-halo' }));
      parcelLayer.appendChild(svgNode('path', { d: path, class: 'parcel-shape' }));
      const showHandles = state.drawing;
      const shownPoints = showHandles ? visibleParcel : geoRing.filter((_, i) => i % Math.max(1, Math.ceil(geoRing.length / 22)) === 0);
      shownPoints.forEach((point, i) => {
        const [lon, lat] = Array.isArray(point) ? point : [point.lon, point.lat];
        const p = geoToScreen(lon, lat);
        const index = showHandles ? i : -1;
        const selected = index >= 0 && index === state.selectedVertex;
        parcelLayer.appendChild(svgNode('circle', {
          cx: p.x, cy: p.y, r: showHandles ? (selected ? 7 : 5) : 3,
          class: `parcel-vertex${showHandles ? ' is-editable' : ''}${selected ? ' is-selected' : ''}`,
          'data-vertex-index': index,
        }));
      });
    } else {
      $('parcel-clip-path').removeAttribute('d');
    }
    if (state.drawing && state.drawPoints.length < 3) {
      state.drawPoints.forEach(({ lon, lat }, index) => {
        const p = geoToScreen(lon, lat);
        parcelLayer.appendChild(svgNode('circle', {
          cx: p.x, cy: p.y, r: index === state.selectedVertex ? 7 : 5,
          class: `parcel-vertex is-editable${index === state.selectedVertex ? ' is-selected' : ''}`,
          'data-vertex-index': index,
        }));
      });
    }
    const option = state.drawing ? null : state.options[state.activeOption];
    if (option?.zones) {
      option.zones.forEach((zone) => zoneLayer.appendChild(svgNode("path", { d: ringPath(zone), class: "zone-shade" })));
      (option.zone_directions || []).forEach((direction, index) => {
        const zone = option.zones[index];
        if (!zone || zone.length < 4) return;
        const nearA = geoToScreen(zone[0][0], zone[0][1]);
        const nearB = geoToScreen(zone[1][0], zone[1][1]);
        const farA = geoToScreen(zone[3][0], zone[3][1]);
        const farB = geoToScreen(zone[2][0], zone[2][1]);
        const start = { x: (nearA.x + nearB.x) / 2, y: (nearA.y + nearB.y) / 2 };
        const end = { x: (farA.x + farB.x) / 2, y: (farA.y + farB.y) / 2 };
        zoneLayer.appendChild(svgNode("path", { d: `M${start.x},${start.y} L${end.x},${end.y}`, class: "zone-direction-arrow", "marker-end": "url(#zone-arrow-head)" }));
        const label = svgNode("text", { x: (start.x + end.x) / 2, y: (start.y + end.y) / 2 - 5, class: "zone-direction-label" });
        label.textContent = `${direction.from_sector} \u2192 ${direction.to_sector}`;
        zoneLayer.appendChild(label);
      });
    }
    if (option?.lines) {
      option.lines.forEach((line) => {
        const path = line.map(([lon, lat], index) => {
          const p = geoToScreen(lon, lat);
          return `${index ? 'L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`;
        }).join(' ');
        recommendationLayer.appendChild(svgNode('path', { d: path, class: 'recommendation-line is-provisional' }));
        recommendationLayer.appendChild(svgNode('path', { d: path, class: 'recommendation-core is-provisional' }));
        line.forEach(([lon, lat]) => {
          const p = geoToScreen(lon, lat);
          recommendationLayer.appendChild(svgNode('circle', { cx: p.x, cy: p.y, r: 3, class: 'recommendation-end is-provisional' }));
        });
      });
    }
    if (state.drawing && state.drawPoints.length > 0) {
      const screenPoints = state.drawPoints.map(({ lon, lat }) => geoToScreen(lon, lat));
      const d = screenPoints.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ');
      drawingLayer.replaceChildren(svgNode('path', {
        d: state.drawPoints.length >= 3 ? `${d} Z` : d,
        class: 'drawing-path',
      }));
    } else {
      drawingLayer.replaceChildren();
    }
  }

  function areaHectares(points) {
    if (!points || points.length < 3) return 0;
    const lat0 = points.reduce((sum, point) => sum + point.lat, 0) / points.length * Math.PI / 180;
    const scaleX = Math.cos(lat0) * 111320;
    const scaleY = 110574;
    let sum = 0;
    for (let i = 0; i < points.length; i += 1) {
      const next = points[(i + 1) % points.length];
      sum += points[i].lon * scaleX * next.lat * scaleY - next.lon * scaleX * points[i].lat * scaleY;
    }
    return Math.abs(sum / 2 / 10000);
  }
  function parcelCenter(points) {
    return {
      lat: points.reduce((sum, point) => sum + point.lat, 0) / points.length,
      lon: points.reduce((sum, point) => sum + point.lon, 0) / points.length,
    };
  }
  function setParcel(points, options = {}) {
    if (!points || points.length < 3) return;
    state.parcel = points;
    state.options = [];
    state.result = null;
    state.availableBelts = [];
    state.selectedBeltIds = [];
    state.recommendedBeltIds = [];
    const area = areaHectares(points);
    $('parcel-state').textContent = options.label || 'Поле на карте';
    $('parcel-meta').textContent = `${points.length} точек · контур выбран`;
    $('parcel-area').textContent = `${area < 10 ? area.toFixed(2) : area.toFixed(1)} га`;
    $('clear-parcel').hidden = false;
    $('calculate-button').disabled = !state.modelReady;
    $('map-toast').hidden = false;
    $('map-legend').hidden = true;
    $('map-foot-status').textContent = 'Контур выбран · можно рассчитать';
    $('results-empty').hidden = false;
    $('results-content').hidden = true;
    updateDrawControls();
    const center = parcelCenter(points);
    if (!options.preserveView) state.center = center;
    if (options.zoom) state.zoom = options.zoom;
    renderTiles();
  }
  function clearParcel() {
    state.parcel = null;
    state.options = [];
    state.result = null;
    state.availableBelts = [];
    state.selectedBeltIds = [];
    state.recommendedBeltIds = [];
    $('parcel-state').textContent = 'Участок не выбран';
    $('parcel-meta').textContent = 'Обведите поле свободной линией';
    $('parcel-area').textContent = '— га';
    $('clear-parcel').hidden = true;
    $('calculate-button').disabled = true;
    $('map-toast').hidden = true;
    $('map-legend').hidden = true;
    $('map-foot-status').textContent = 'Карта готова к выбору участка';
    $('results-empty').hidden = false;
    $('results-content').hidden = true;
    if (state.drawing) cancelLasso();
    else updateDrawControls();
    renderOverlay();
  }
  function simplifyDraw(points, limit = 120) {
    if (points.length <= limit) return points;
    const step = Math.ceil(points.length / limit);
    const reduced = points.filter((_, index) => index % step === 0);
    const last = points[points.length - 1];
    if (reduced[reduced.length - 1] !== last) reduced.push(last);
    return reduced;
  }
  function updateDrawControls() {
    const toolbox = $('lasso-toolbox');
    if (toolbox) toolbox.hidden = !state.drawing;
    const canFinish = state.drawPoints.length >= 3;
    $('finish-drawing').disabled = !canFinish;
    $('undo-draw').disabled = !state.drawHistory.length && !state.drawPoints.length;
    $('delete-selected-vertex').disabled = !state.drawing || state.selectedVertex < 0 || state.drawPoints.length <= 3;
    $('lasso-label').textContent = state.drawing
      ? (state.drawingKind === 'edit' ? '\u0413\u043e\u0442\u043e\u0432\u043e \u043a \u0440\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u044e' : '\u0420\u0438\u0441\u043e\u0432\u0430\u043d\u0438\u0435\u2026')
      : (state.parcel ? '\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0433\u0440\u0430\u043d\u0438\u0446\u0443' : '\u041e\u0431\u0432\u0435\u0441\u0442\u0438 \u0443\u0447\u0430\u0441\u0442\u043e\u043a');
    $('lasso-button').classList.toggle('is-active', state.drawing);
    mapEl.classList.toggle('is-drawing', state.drawing);
    mapEl.classList.toggle('is-editing-parcel', state.drawingKind === 'edit');
  }
  function pushDrawHistory() {
    state.drawHistory.push(state.drawPoints.map(({ lat, lon }) => ({ lat, lon })));
    if (state.drawHistory.length > 30) state.drawHistory.shift();
    updateDrawControls();
  }
  function startLasso() {
    if (state.drawing) {
      cancelLasso();
      return;
    }
    state.drawing = true;
    state.drawingKind = state.parcel?.length ? 'edit' : 'new';
    state.drawPoints = state.parcel?.length
      ? state.parcel.map(({ lat, lon }) => ({ lat, lon }))
      : [];
    state.drawHistory = [];
    state.selectedVertex = -1;
    state.drawGesture = null;
    state.draggingVertex = false;
    updateDrawControls();
    $('lasso-hint').textContent = state.drawingKind === 'edit'
      ? '\u041f\u0435\u0440\u0435\u0442\u0430\u0441\u043a\u0438\u0432\u0430\u0439\u0442\u0435 \u0432\u0435\u0440\u0448\u0438\u043d\u044b \u0433\u0440\u0430\u043d\u0438\u0446\u044b. \u041d\u0430\u0436\u043c\u0438\u0442\u0435 \u043d\u0430 \u0441\u0442\u043e\u0440\u043e\u043d\u0443, \u0447\u0442\u043e\u0431\u044b \u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0442\u043e\u0447\u043a\u0443; \u0432\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0442\u043e\u0447\u043a\u0443 \u0438 \u0443\u0434\u0430\u043b\u0438\u0442\u0435 \u0435\u0451. Enter \u2014 \u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c, Esc \u2014 \u043e\u0442\u043c\u0435\u043d\u0438\u0442\u044c.'
      : '\u041a\u043b\u0438\u043a\u0430\u0439\u0442\u0435 \u043f\u043e \u043a\u0430\u0440\u0442\u0435, \u0447\u0442\u043e\u0431\u044b \u0441\u0442\u0430\u0432\u0438\u0442\u044c \u0432\u0435\u0440\u0448\u0438\u043d\u044b, \u0438\u043b\u0438 \u0432\u0435\u0434\u0438\u0442\u0435 \u043a\u0443\u0440\u0441\u043e\u0440 \u0441 \u0437\u0430\u0436\u0430\u0442\u043e\u0439 \u043a\u043d\u043e\u043f\u043a\u043e\u0439. \u041a\u043b\u0438\u043a\u043d\u0438\u0442\u0435 \u0440\u044f\u0434\u043e\u043c \u0441 \u043f\u0435\u0440\u0432\u043e\u0439 \u0442\u043e\u0447\u043a\u043e\u0439 \u0438\u043b\u0438 \u043d\u0430\u0436\u043c\u0438\u0442\u0435 Enter, \u0447\u0442\u043e\u0431\u044b \u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c.';
    $('map-foot-status').textContent = state.drawingKind === 'edit'
      ? '\u041f\u0435\u0440\u0435\u0442\u0430\u0441\u043a\u0438\u0432\u0430\u0439\u0442\u0435 \u0432\u0435\u0440\u0448\u0438\u043d\u044b; \u043d\u0430\u0436\u043c\u0438\u0442\u0435 \u043d\u0430 \u0441\u0442\u043e\u0440\u043e\u043d\u0443, \u0447\u0442\u043e\u0431\u044b \u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0442\u043e\u0447\u043a\u0443.'
      : '\u041a\u043b\u0438\u043a\u0430\u0439\u0442\u0435, \u0447\u0442\u043e\u0431\u044b \u0441\u0442\u0430\u0432\u0438\u0442\u044c \u0442\u043e\u0447\u043a\u0438, \u0438\u043b\u0438 \u0432\u0435\u0434\u0438\u0442\u0435 \u043a\u0443\u0440\u0441\u043e\u0440 \u0441 \u0437\u0430\u0436\u0430\u0442\u043e\u0439 \u043a\u043d\u043e\u043f\u043a\u043e\u0439.';
    $('map-viewport').focus({ preventScroll: true });
    renderOverlay();
  }
  function finishLasso() {
    if (!state.drawing) return;
    if (state.drawPoints.length < 3) {
      $('map-foot-status').textContent = '\u041d\u0443\u0436\u043d\u043e \u043d\u0435 \u043c\u0435\u043d\u0435\u0435 \u0442\u0440\u0451\u0445 \u0442\u043e\u0447\u0435\u043a, \u0447\u0442\u043e\u0431\u044b \u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c \u0443\u0447\u0430\u0441\u0442\u043e\u043a.';
      return;
    }
    const points = simplifyDraw(state.drawPoints.map(({ lat, lon }) => ({ lat, lon })));
    state.drawing = false;
    state.drawingKind = null;
    state.drawPoints = [];
    state.drawHistory = [];
    state.selectedVertex = -1;
    state.drawGesture = null;
    state.draggingVertex = false;
    updateDrawControls();
    setParcel(points, { preserveView: true });
    renderOverlay();
  }
  function cancelLasso() {
    state.drawing = false;
    state.drawingKind = null;
    state.drawPoints = [];
    state.drawHistory = [];
    state.selectedVertex = -1;
    state.drawGesture = null;
    state.draggingVertex = false;
    updateDrawControls();
    $('map-foot-status').textContent = state.parcel
      ? '\u041a\u043e\u043d\u0442\u0443\u0440 \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d. \u041c\u043e\u0436\u043d\u043e \u0440\u0430\u0441\u0441\u0447\u0438\u0442\u0430\u0442\u044c \u0440\u0430\u0437\u043c\u0435\u0449\u0435\u043d\u0438\u0435.'
      : '\u041a\u0430\u0440\u0442\u0430 \u0433\u043e\u0442\u043e\u0432\u0430 \u043a \u0432\u044b\u0431\u043e\u0440\u0443 \u0443\u0447\u0430\u0441\u0442\u043a\u0430.';
    renderOverlay();
  }
  function undoDraw() {
    if (!state.drawing) return;
    if (state.drawHistory.length) state.drawPoints = state.drawHistory.pop();
    else if (state.drawPoints.length) state.drawPoints.pop();
    state.selectedVertex = -1;
    updateDrawControls();
    renderOverlay();
  }
  function deleteSelectedVertex() {
    if (!state.drawing || state.selectedVertex < 0 || state.drawPoints.length <= 3) return;
    pushDrawHistory();
    state.drawPoints.splice(state.selectedVertex, 1);
    state.selectedVertex = -1;
    updateDrawControls();
    renderOverlay();
  }
  function nearestVertex(screen, tolerance = 13) {
    let best = { index: -1, distance: tolerance };
    state.drawPoints.forEach(({ lon, lat }, index) => {
      const p = geoToScreen(lon, lat);
      const distance = Math.hypot(p.x - screen.x, p.y - screen.y);
      if (distance <= best.distance) best = { index, distance };
    });
    return best.index;
  }
  function nearestEdge(screen, tolerance = 12) {
    if (state.drawPoints.length < 3) return null;
    let best = null;
    for (let i = 0; i < state.drawPoints.length; i += 1) {
      const aGeo = state.drawPoints[i];
      const bGeo = state.drawPoints[(i + 1) % state.drawPoints.length];
      const a = geoToScreen(aGeo.lon, aGeo.lat);
      const b = geoToScreen(bGeo.lon, bGeo.lat);
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const length2 = dx * dx + dy * dy || 1;
      const t = Math.max(0, Math.min(1, ((screen.x - a.x) * dx + (screen.y - a.y) * dy) / length2));
      const x = a.x + t * dx;
      const y = a.y + t * dy;
      const distance = Math.hypot(screen.x - x, screen.y - y);
      if (distance <= tolerance && (!best || distance < best.distance)) best = { index: i + 1, distance, point: screenToGeo(x, y) };
    }
    return best;
  }

  function updateWindPresentation(from = state.referenceScenario?.wind_from ?? 135) {
    from = Number(from);
    const to = (from + 180) % 360;
    const sourceName = DIR_NAMES[Math.round(from / 45) % 8];
    const destName = DIR_NAMES[Math.round(to / 45) % 8];
    $('wind-map-label').textContent = `${sourceName} → ${destName}`;
    $('wind-map-icon').style.transform = `rotate(${to - 90}deg)`;
  }
  function formatNumber(value, digits = 1) {
    return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: digits }).format(Number(value));
  }
  function renderScenarioCard(scenario) {
    if (!scenario) return;
    const automatic = !scenario.source || scenario.source.includes('архив');
    const monthNames = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
    const from = DIR_NAMES[Math.round(Number(scenario.wind_from) / 45) % 8];
    const to = DIR_NAMES[Math.round((Number(scenario.wind_from) + 180) / 45) % 8];
    $('scenario-badge').textContent = automatic
      ? `АРХИВ · ${scenario.event_count || 82} ЭПИЗОДА · ${scenario.period || '2010–2024'}`
      : 'ЗАДАННЫЙ СЦЕНАРИЙ';
    $('scenario-title').textContent = automatic ? 'Характерные условия пыльной бури' : 'Условия расчёта';
    $('scenario-summary').textContent = `${from} → ${to} · ветер ${formatNumber(scenario.wind_speed)} м/с · порывы ${formatNumber(scenario.wind_gust)} м/с · ${monthNames[Number(scenario.month) - 1] || 'сезон'}`;
    $('scenario-basis').textContent = automatic
      ? `Медианы эпизодов: ${formatNumber(scenario.temperature_c)} °C, влажность ${formatNumber(scenario.humidity, 0)}%, видимость ${formatNumber(scenario.visibility)} км. Это архивный сценарий, не прогноз на сегодня.`
      : 'Этот сценарий задан вручную.';
  }
  function readScenario() {
    return {
      design: {
        height: Number($('height').value),
        porosity: Number($('porosity').value),
        strips: $('strips').value,
      },
      polygon: state.parcel.map(({ lon, lat }) => [lon, lat]),
    };
  }
  function renderProfile(height) {
    const ratios = Array.from({ length: 21 }, (_, step) => {
      const distance = step;
      const porosity = Number($('porosity').value);
      return porosity + (1 - porosity) * (1 - Math.exp(-0.15 * distance));
    });
    const coords = ratios.map((ratio, index) => ({ x: 22 + index * 13.5, y: 84 - ratio * 72 }));
    const d = coords.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');
    $('profile-line').setAttribute('d', d);
    $('profile-area').setAttribute('d', `${d} L292,84 L22,84 Z`);
    $('profile-dot').setAttribute('cx', coords[0].x);
    $('profile-dot').setAttribute('cy', coords[0].y);
    $('profile-height').textContent = `H = ${height} м`;
  }
  function renderOptions(options) {
    const list = $('option-list');
    list.replaceChildren();
    options.forEach((option, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `option-button${index === state.activeOption ? ' selected' : ''}`;
      const number = document.createElement('span');
      number.className = 'option-number';
      number.textContent = String(option.strips).padStart(2, '0');
      const details = document.createElement('span');
      details.className = 'option-details';
      const title = document.createElement('strong');
      title.textContent = option.recommended
        ? (state.result?.selection_mode === 'manual' ? '\u0412\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0439 \u0440\u0430\u0441\u0447\u0451\u0442\u043d\u044b\u0439 \u0432\u0430\u0440\u0438\u0430\u043d\u0442' : '\u0420\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0430\u0446\u0438\u044f \u043c\u043e\u0434\u0435\u043b\u0438')
        : `\u0412\u0430\u0440\u0438\u0430\u043d\u0442: ${stripLabel(option.strips)}`;
      const description = document.createElement('small');
      const sides = (option.belt_sectors || []).map((sectors) => sectors.join('/')).join(', ');
      description.textContent = `${formatLength(option.length_m)} \u043f\u043e\u0441\u0430\u0434\u043a\u0438 \u00b7 \u0440\u0430\u0441\u0447\u0451\u0442\u043d\u0430\u044f \u0437\u0430\u0449\u0438\u0442\u0430 ${Math.round(option.protected_share * 100)}% \u00b7 ${sides || '\u043d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u044b'}`;
      const score = document.createElement('span');
      score.className = 'option-score';
      score.textContent = `${Math.round(option.risk * 100)}`;
      details.append(title, description);
      button.append(number, details, score);
      button.addEventListener('click', () => {
        state.activeOption = index;
        state.selectedBeltIds = [...(option.belt_ids || [])];
        renderOptionSelection();
        renderOverlay();
      });
      list.appendChild(button);
    });
  }
  function renderBeltControls(belts = state.availableBelts, selectedIds = state.selectedBeltIds) {
    const list = $('belt-toggle-list');
    if (!list) return;
    list.replaceChildren();
    const selected = new Set(selectedIds || []);
    const recommended = new Set(state.recommendedBeltIds || []);
    belts.forEach((belt) => {
      const row = document.createElement('label');
      row.className = 'belt-toggle';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = String(belt.id);
      checkbox.checked = selected.has(belt.id);
      checkbox.setAttribute('aria-label', `\u041f\u043e\u043b\u043e\u0441\u0430 ${belt.id + 1}: ${formatLength(belt.length_m)}`);
      checkbox.addEventListener('change', () => {
        const ids = [...list.querySelectorAll('input:checked')].map((input) => Number(input.value));
        calculate(ids);
      });
      const copy = document.createElement('span');
      copy.className = 'belt-toggle-copy';
      const title = document.createElement('strong');
      title.textContent = `\u041f\u043e\u043b\u043e\u0441\u0430 ${belt.id + 1} \u00b7 ${formatLength(belt.length_m)}`;
      const detail = document.createElement('small');
      const sectors = (belt.wind_sector_counts || []).map((item) => `${item.sector_name}: ${item.count}`).join(", ") || (belt.wind_sectors || []).join(", ");
      detail.textContent = `${sectors ? `\u041d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f \u0438 \u0447\u0438\u0441\u043b\u043e \u0441\u0442\u0440\u043e\u043a: ${sectors}` : '\u041d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e'}${recommended.has(belt.id) ? ' \u00b7 \u0432 \u0430\u0432\u0442\u043e\u0432\u0430\u0440\u0438\u0430\u043d\u0442\u0435' : ''}`;
      copy.append(title, detail);
      row.append(checkbox, copy);
      list.appendChild(row);
    });
    if (!belts.length) {
      const emptyNote = document.createElement('small');
      emptyNote.className = 'belt-empty-note';
      emptyNote.textContent = '\u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0445 \u043b\u0438\u043d\u0438\u0439 \u043f\u043e\u0441\u0430\u0434\u043a\u0438';
      list.appendChild(emptyNote);
    }
    list.classList.toggle('is-empty', belts.length === 0);
  }
  function renderOptionSelection() {
    host.querySelectorAll('.option-button').forEach((button, index) => button.classList.toggle('selected', index === state.activeOption));
    const option = state.options[state.activeOption];
    if (!option) return;
    $('risk-after').textContent = String(Math.round(option.risk * 100));
    $('risk-reduction').textContent = `${Math.round(option.reduction_pct)}%`;
    $('recommendation-summary').textContent = `${stripLabel(option.strips)} \u00b7 ${formatLength(option.length_m)} \u00b7 \u0441\u043d\u0430\u0440\u0443\u0436\u0438 \u043f\u043e\u043b\u044f`;
    const offsets = (option.offsets_m || []).map(formatLength).join(', ');
    const selectedSectors = [...new Set((option.belt_sectors || []).flat())].join(', ');
    $('placement-reason-text').textContent = `${state.result?.orientation?.placement_reason || '\u041b\u0438\u043d\u0438\u0438 \u0440\u0430\u0441\u043f\u043e\u043b\u043e\u0436\u0435\u043d\u044b \u0432\u0434\u043e\u043b\u044c \u0432\u043d\u0435\u0448\u043d\u0435\u0439 \u0433\u0440\u0430\u043d\u0438\u0446\u044b \u043f\u043e\u043b\u044f.'}${selectedSectors ? ` \u041e\u0440\u0438\u0435\u043d\u0442\u0430\u0446\u0438\u044f \u0443\u0447\u0438\u0442\u044b\u0432\u0430\u0435\u0442 \u0432\u0435\u0442\u0440\u044b: ${selectedSectors}.` : ''}${offsets ? ` \u041e\u0442\u0441\u0442\u0443\u043f\u044b: ${offsets}.` : ''}`;
    $('placement-wind-angle').textContent = `\u041b\u0438\u043d\u0438\u044f \u043f\u0440\u043e\u0445\u043e\u0434\u0438\u0442 \u0432\u0434\u043e\u043b\u044c \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0435\u0433\u043e \u0443\u0447\u0430\u0441\u0442\u043a\u0430 \u0432\u043d\u0435\u0448\u043d\u0435\u0439 \u0433\u0440\u0430\u043d\u0438\u0446\u044b \u043f\u043e\u043b\u044f, \u0430\u0437\u0438\u043c\u0443\u0442 ${Math.round(option.axis_azimuth_deg)}\u00b0.`;
    const recommended = state.options.find((item) => item.recommended);
    const comparisonNote = state.result?.selection_mode === 'auto' && !option.recommended && recommended
      ? `\u0421\u0435\u0439\u0447\u0430\u0441 \u043f\u043e\u043a\u0430\u0437\u0430\u043d \u0432\u0430\u0440\u0438\u0430\u043d\u0442 \u0441 ${stripLabel(option.strips)}. \u041c\u043e\u0434\u0435\u043b\u044c \u0440\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0443\u0435\u0442 ${stripLabel(recommended.strips)}. `
      : '';
    $('selection-reason-text').textContent = `${comparisonNote}${state.result?.selection_reason || '\u041c\u043e\u0436\u043d\u043e \u0432\u044b\u0431\u0440\u0430\u0442\u044c \u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e \u0438 \u0441\u043e\u0441\u0442\u0430\u0432 \u043b\u0435\u0441\u043e\u043f\u043e\u043b\u043e\u0441.'}`;
    $('map-foot-status').textContent = `\u041f\u043e\u043a\u0430\u0437\u0430\u043d \u0432\u0430\u0440\u0438\u0430\u043d\u0442 \u00b7 ${stripLabel(option.strips)}`;
    renderGisAssessment(option);
    renderBeltControls();
  }
  function renderGisAssessment(option) {
    const summary = $('gis-assessment-summary');
    const details = $('gis-assessment-details');
    const assessment = option.gis_assessment;
    const lines = option.gis_lines || [];
    details.replaceChildren();
    const addRow = (title, value) => {
      if (!value) return;
      const row = document.createElement('div');
      row.className = 'gis-assessment-row';
      const heading = document.createElement('strong');
      heading.textContent = title;
      const text = document.createElement('span');
      text.textContent = value;
      row.append(heading, text);
      details.appendChild(row);
    };
    const layerStatus = state.gisStatus?.layers;
    const constraintStatus = state.gisStatus?.constraint_layers;
    const layerSummary = layerStatus
      ? `${Object.entries(layerStatus).map(([name, available]) => `${name}: ${available ? 'файл есть' : 'файл отсутствует'}`).join('; ')}. Ограничения: ${constraintStatus ? Object.keys(constraintStatus).filter((name) => !constraintStatus[name]).join(', ') : 'нет статуса'}.`
      : 'Сервер не передал сведения о ГИС-слоях. Проверьте /api/health.';
    if (!assessment) {
      summary.textContent = 'Ответ API не содержит ГИС-оценку. Вероятно, запущен старый сервер: перезапустите server.py из папки проекта и пересчитайте участок.';
      addRow('Состояние ГИС-слоёв на сервере', layerSummary);
      return;
    }
    if (!assessment.available) {
      summary.textContent = assessment.reason || 'Ни один доступный ГИС-слой не покрыл предложенные линии.';
      addRow('Состояние ГИС-слоёв на сервере', layerSummary);
      lines.forEach((line, index) => {
        if (line.warnings?.length) addRow(`Полоса ${index + 1}`, line.warnings.join(' '));
      });
      return;
    }
    const coveredLines = lines.filter((line) => Number(line.landcover_samples) > 0).length;
    const blocked = Number(assessment.blocked_belts || 0);
    summary.textContent = coveredLines
      ? `По карте земного покрова: конфликтные линии — ${blocked} из ${coveredLines}. Линия помечается конфликтной при любом попадании проверенных точек на застройку или постоянную воду.`
      : 'Карта земного покрова не дала покрытия для этих линий; конфликт по застройке и воде не оценён.';
    const fmt = (value, digits = 1) => Number.isFinite(Number(value)) ? Number(value).toLocaleString('ru-RU', { maximumFractionDigits: digits }) : null;
    const layers = assessment.layers_used?.join(', ');
    if (layers) addRow('Использованные слои', layers);
    if (assessment.mean_slope_deg != null) {
      const slope = `Средний уклон ${fmt(assessment.mean_slope_deg)}°, максимум ${fmt(assessment.max_slope_deg)}°`;
      const drainage = assessment.downslope_azimuth_deg == null ? '' : `; ориентировочное направление вниз по склону ${Math.round(assessment.downslope_azimuth_deg)}°`;
      addRow('Рельеф вдоль линий (DEM, 30 м)', `${slope}${drainage}.`);
    }
    const cover = Object.entries(assessment.landcover_shares_pct || {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3)
      .map(([name, share]) => `${name} ${fmt(share)}%`)
      .join('; ');
    if (cover) addRow('Земной покров вдоль посадки', `${cover}${assessment.hard_conflict_pct == null ? '' : `; застройка и вода вместе ${fmt(assessment.hard_conflict_pct)}%`}.`);
    if (assessment.cropland_pct > 0) addRow('Проверка прав на землю', `По карте пашня занимает ${fmt(assessment.cropland_pct)}% коридора посадки. Кадастровые границы и собственник не определены.`);
    const soilNames = {
      clay_pct_0_5cm: 'глина, 0–5 см',
      sand_pct_0_5cm: 'песок, 0–5 см',
      ph_h2o_0_5cm: 'pH, 0–5 см',
      organic_carbon_g_kg_0_5cm: 'органический углерод, 0–5 см',
      clay_pct_15_30cm: 'глина, 15–30 см',
      sand_pct_15_30cm: 'песок, 15–30 см',
      ph_h2o_15_30cm: 'pH, 15–30 см',
      organic_carbon_g_kg_15_30cm: 'органический углерод, 15–30 см',
    };
    const soil = Object.entries(assessment.soil || {}).map(([key, value]) => {
      const unit = key.startsWith('clay_') || key.startsWith('sand_') ? '%' : key.startsWith('organic_') ? ' г/кг' : '';
      return `${soilNames[key] || key}: ${fmt(value)}${unit}`;
    }).join('; ');
    if (soil) addRow('Почвенный обзор (SoilGrids, около 250 м)', soil);
    lines.forEach((line, index) => {
      const facts = [];
      if (line.mean_slope_deg != null) facts.push(`уклон ${fmt(line.mean_slope_deg)}°`);
      if (line.dominant_landcover) facts.push(`покров: ${line.dominant_landcover}`);
      if (line.hard_conflict_pct != null) facts.push(`застройка/вода: ${fmt(line.hard_conflict_pct)}%`);
      if (line.cropland_pct > 0) facts.push(`пашня: ${fmt(line.cropland_pct)}%, права не проверены`);
      if (line.blocked) facts.push('конфликтная линия');
      if (line.warnings?.length) facts.push(line.warnings.join(' '));
      addRow(`Полоса ${index + 1}`, facts.join('; ') || 'Нет данных по этой линии.');
    });
  }
  function stripLabel(count) {
    if (count === 0) return '\u0411\u0435\u0437 \u043b\u0435\u0441\u043e\u043f\u043e\u043b\u043e\u0441';
    const lastTwo = count % 100;
    const last = count % 10;
    const noun = lastTwo >= 11 && lastTwo <= 14 ? '\u043f\u043e\u043b\u043e\u0441' : last === 1 ? '\u043f\u043e\u043b\u043e\u0441\u0430' : last >= 2 && last <= 4 ? '\u043f\u043e\u043b\u043e\u0441\u044b' : '\u043f\u043e\u043b\u043e\u0441';
    return `${count} ${noun}`;
  }
  function formatLength(meters) {
    return meters >= 1000 ? `${(meters / 1000).toFixed(1)} \u043a\u043c` : `${Math.round(meters)} \u043c`;
  }
  async function calculate(selectedBeltIds = null) {
    if (!state.parcel) return;
    if (!Array.isArray(selectedBeltIds)) selectedBeltIds = null;
    const button = $('calculate-button');
    button.disabled = true;
    $('belt-toggle-list')?.classList.add('is-loading');
    $('map-loading').hidden = false;
    $('map-foot-status').textContent = 'Расчёт сценариев на сетке выбранного поля';
    try {
      const scenario = readScenario();
      if (selectedBeltIds !== null) scenario.design.selected_belts = selectedBeltIds;
      const response = await fetch('/api/simulate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(scenario),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Сервер не смог выполнить расчёт.');
      state.result = data;
      state.options = data.options;
      state.availableBelts = data.available_belts || [];
      state.selectedBeltIds = data.selected_belt_ids || [];
      state.recommendedBeltIds = data.recommended_belt_ids || [];
      state.activeOption = Math.max(0, data.options.findIndex((option) => option.recommended));
      $('risk-before').textContent = String(Math.round(data.baseline_risk * 100));
      renderScenarioCard(data.scenario);
      $('result-scenario-summary').textContent = $('scenario-summary').textContent;
      const directionSummary = (data.direction_counts || []).map((item) => `${item.sector_name}: ${item.count}`).join(", ");
      $('result-scenario-basis').textContent = `${data.scenario.basis || '\u041c\u0435\u0434\u0438\u0430\u043d\u044b \u043f\u043e \u0440\u0430\u0437\u043c\u0435\u0447\u0435\u043d\u043d\u044b\u043c \u043d\u0430\u0431\u043b\u044e\u0434\u0435\u043d\u0438\u044f\u043c.'}${directionSummary ? ` \u0412\u0435\u0442\u0440\u043e\u0432\u044b\u0435 \u0441\u0435\u043a\u0442\u043e\u0440\u044b \u0438 \u0447\u0438\u0441\u043b\u043e \u0441\u0442\u0440\u043e\u043a: ${directionSummary}.` : ''} \u0421\u0442\u0440\u0435\u043b\u043a\u0430 \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442 \u0441\u0430\u043c\u043e\u0435 \u0447\u0430\u0441\u0442\u043e\u0435 \u043d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435; \u043c\u043e\u0434\u0435\u043b\u044c \u0443\u0447\u0438\u0442\u044b\u0432\u0430\u0435\u0442 \u0432\u0441\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043d\u044b\u0435.`;
      $('selection-reason-text').textContent = data.selection_reason;
      updateWindPresentation(data.wind_from);
      $('results-empty').hidden = true;
      $('results-content').hidden = false;
      $('map-legend').hidden = false;
      $('map-toast').hidden = true;
      $('result-disclaimer').textContent = data.station_distance_km > 100
        ? `Участок находится примерно в ${Math.round(data.station_distance_km)} км от станции Элиста. Погодная модель не калибрована за пределами региона; результат является экстраполяцией. ГИС-слои дают предварительную оценку, а не кадастровую проверку.`
        : 'Индекс сравнивает варианты в историческом сценарии Элисты; это не вероятность события и не прогноз на сегодня. ГИС-слои не подтверждают права на землю; SoilGrids около 250 м не заменяет анализ почвы.';
      renderOptions(data.options);
      renderOptionSelection();
      renderProfile(Number($('height').value));
      renderOverlay();
      $('map-foot-status').textContent = `Расчёт готов · площадь ${data.area_ha.toFixed(data.area_ha < 10 ? 2 : 1)} га`;
      if (window.innerWidth <= 1030 && window.innerWidth > 680) $('results-rail').classList.add('show');
    } catch (error) {
      renderBeltControls();
      showDialog('Не удалось рассчитать', `<p>${escapeHtml(error.message)}</p><p>Проверьте, что участок обведён и локальный сервер модели Элисты запущен.</p>`);
    } finally {
      $('belt-toggle-list')?.classList.remove('is-loading');
      button.disabled = !state.parcel || !state.modelReady;
      $('map-loading').hidden = true;
    }
  }
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
  }
  function showDialog(title, html) {
    $('dialog-title').textContent = title;
    $('dialog-body').innerHTML = html;
    $('info-dialog').showModal();
  }
  function exportResult() {
    if (!state.result || !state.parcel) return;
    const option = state.options[state.activeOption];
    const features = [{
      type: 'Feature',
      properties: { type: 'selected_field', area_ha: state.result.area_ha },
      geometry: { type: 'Polygon', coordinates: [[...state.parcel.map(({ lon, lat }) => [lon, lat]), [state.parcel[0].lon, state.parcel[0].lat]]] },
    }];
    option.lines.forEach((line, index) => features.push({
      type: 'Feature',
      properties: { type: 'recommended_forest_belt', location: 'outside_field_boundary', option: option.strips, segment: index + 1, setback_m: option.offsets_m?.[index] ?? state.result.setback_m, height_m: Number($('height').value), porosity: Number($('porosity').value), model_index: option.risk, gis_assessment: option.gis_lines?.[index] || null },
      geometry: { type: 'LineString', coordinates: line },
    }));
    const blob = new Blob([JSON.stringify({ type: 'FeatureCollection', name: 'forest_belt_plan', features }, null, 2)], { type: 'application/geo+json' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'plan-lesopolos.geojson';
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }
  function downloadReport() {
    if (!state.result || !state.options.length) return;
    const result = state.result;
    const option = state.options[state.activeOption];
    const scenario = result.scenario;
    const orientation = result.orientation;
    const selectedSectors = [...new Set((option.belt_sectors || []).flat())].join(', ');
    const placementReason = `${orientation.placement_reason}${selectedSectors ? ` Для этого варианта выбраны стороны, обращённые к ветрам из секторов: ${selectedSectors}.` : ''}`;
    const monthNames = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
    const weatherRows = [
      ['Ветер', `${DIR_NAMES[Math.round(scenario.wind_from / 45) % 8]} → ${DIR_NAMES[Math.round((scenario.wind_from + 180) / 45) % 8]}, ${formatNumber(scenario.wind_speed)} м/с; порывы ${formatNumber(scenario.wind_gust)} м/с`],
      ['Температура и влажность', `${formatNumber(scenario.temperature_c)} °C; ${formatNumber(scenario.humidity, 0)}%`],
      ['Видимость и месяц', `${formatNumber(scenario.visibility)} км; ${monthNames[Number(scenario.month) - 1] || scenario.month}`],
      ['Источник сценария', `${scenario.event_count || 82} эпизода пыльных бурь, ${scenario.period || '2010–2024'}, станция Элиста (ВМО 34861)`],
      ['Направления ветра в архиве', (result.direction_counts || []).map((item) => `${item.sector_name}: ${item.count}`).join('; ') || 'заданный сценарий'],
    ];
    const rows = weatherRows.map(([label, value]) => `<tr><th>${escapeHtml(label)}</th><td>${escapeHtml(value)}</td></tr>`).join('');
    const gis = option.gis_assessment || {};
    const gisSoilNames = { clay_pct_0_5cm: 'Глина, 0–5 см', sand_pct_0_5cm: 'Песок, 0–5 см', ph_h2o_0_5cm: 'pH, 0–5 см', organic_carbon_g_kg_0_5cm: 'Органический углерод, 0–5 см', clay_pct_15_30cm: 'Глина, 15–30 см', sand_pct_15_30cm: 'Песок, 15–30 см', ph_h2o_15_30cm: 'pH, 15–30 см', organic_carbon_g_kg_15_30cm: 'Органический углерод, 15–30 см' };
    const gisSoil = Object.entries(gis.soil || {}).map(([key, value]) => `${gisSoilNames[key] || key}: ${formatNumber(value)}${key.startsWith('clay_') || key.startsWith('sand_') ? '%' : key.startsWith('organic_') ? ' г/кг' : ''}`).join('; ') || 'Нет покрывающих точек SoilGrids.';
    const gisCover = Object.entries(gis.landcover_shares_pct || {}).map(([name, share]) => `${name}: ${formatNumber(share)}%`).join('; ') || 'Покров не определён.';
    const gisRows = gis.available ? [
      ['Слои', (gis.layers_used || []).join(', ') || '—'],
      ['Рельеф по DEM', gis.mean_slope_deg == null ? 'Нет оценки уклона.' : `Средний уклон ${formatNumber(gis.mean_slope_deg)}°, максимум ${formatNumber(gis.max_slope_deg)}°; ориентировочное направление вниз по склону ${gis.downslope_azimuth_deg == null ? 'не определено' : `${Math.round(gis.downslope_azimuth_deg)}°`}.`],
      ['Земной покров', `${gisCover}${gis.hard_conflict_pct == null ? '' : `; застройка и постоянная вода вместе ${formatNumber(gis.hard_conflict_pct)}%`}`],
      ['Почвенный обзор SoilGrids (~250 м)', gisSoil],
      ['Конфликтные линии', `${gis.blocked_belts || 0}; конфликт — любое попадание проверенных точек на застройку или постоянную воду.`],
    ] : [['Статус ГИС', gis.reason || 'Открытые слои не покрыли предложенные линии.']];
    const gisHtml = `<h2>Предварительная проверка места по ГИС-слоям</h2><table>${gisRows.map(([label, value]) => `<tr><th>${escapeHtml(label)}</th><td>${escapeHtml(value)}</td></tr>`).join('')}</table><p class="muted">Уклон рассчитывается и приводится здесь для оценки, но пока не меняет рекомендованное размещение. Земной покров отдельно учитывается при исключении линий, где найден пиксель застройки или воды. Класс пашни показывает сельскохозяйственный покров, а не владельца участка. Кадастровые границы, дорожные полосы и зоны аэропортов не загружены: эту схему нельзя считать разрешённой к посадке без местной проверки. SoilGrids не заменяет почвенный анализ. Направление вниз по склону приближённо оценено по региональному DEM, без полноценного гидрологического моделирования.</p>`;
    const offsets = (option.offsets_m || []).map(formatLength).join(', ');
    const comparisonRows = result.options.map((item) => `<tr><td>${stripLabel(item.strips)}${item.recommended ? ' · рекомендуется' : ''}</td><td>${Math.round(item.risk * 100)}/100</td><td>${Math.round(item.reduction_pct)}%</td><td>${formatNumber(item.length_m)} м</td><td>${Math.round(item.protected_share * 100)}%</td></tr>`).join('');
    const report = `<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Отчёт · лесополоса · Элиста</title><style>body{font:16px/1.55 Arial,sans-serif;max-width:820px;margin:40px auto;padding:0 22px;color:#28372d}h1,h2{line-height:1.2}h1{font-size:28px}h2{font-size:19px;margin-top:30px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid #dfe6dc;vertical-align:top}th{width:32%;color:#536653}.callout{padding:14px 16px;background:#f1f6ed;border:1px solid #dce9d7;border-radius:9px}.muted{color:#68776b;font-size:13px}@media print{body{margin:15mm auto;padding:0}}</style><h1>Расчёт размещения защитной лесополосы</h1><p class="muted">Пилотная область: Элиста, Республика Калмыкия · сформировано ${escapeHtml(new Date().toLocaleString('ru-RU'))}</p><h2>Участок и погодный сценарий</h2><table><tr><th>Площадь поля</th><td>${formatNumber(result.area_ha)} га</td></tr>${rows}</table><h2>Рекомендуемое размещение</h2><div class="callout"><strong>${escapeHtml(placementReason)}</strong><br>Отступ от внешней границы: ${offsets || '—'}; азимут одной из полос: ${Math.round(option.axis_azimuth_deg)}°.</div><p>Выбран вариант с ${stripLabel(option.strips)}. Длина посадки: ${formatNumber(option.length_m)} м. Доля расчётных точек в зоне ветровой защиты: ${Math.round(option.protected_share * 100)}%. Расчётная дальность аэродинамической тени — до 20 высот насаждения.</p><p>${escapeHtml(result.selection_reason)}</p>${gisHtml}<h2>Сравнение числа полос</h2><table><tr><th>Вариант</th><th>Индекс</th><th>Снижение</th><th>Длина посадки</th><th>Защищённая площадь</th></tr>${comparisonRows}</table><h2>Сравнение сценарного индекса</h2><p>Без полос: ${Math.round(result.baseline_risk * 100)} из 100. С выбранным вариантом: ${Math.round(option.risk * 100)} из 100. Изменение индекса: ${Math.round(option.reduction_pct)}%.</p><p class="muted">Это относительный индекс Random Forest, а не калиброванная вероятность пыльной бури. Сценарий основан на исторических данных и не является оперативным прогнозом. Модель обучена на погодных наблюдениях одной станции; аэродинамический профиль лесополос требует полевой калибровки. Уклон и почвенные оценки показываются справочно и пока не меняют рекомендованное размещение; карта земного покрова отдельно помогает выявить линии через застройку или постоянную воду. Грунтовые воды и права на землю здесь не проверяются. SoilGrids (около 250 м) не заменяет анализ почвы на участке. Суховеи отдельно не моделируются из-за отсутствия разметки.</p><p class="muted">Источники метеоданных: meteo.ru и RP5, станция Элиста, ВМО 34861. Пыльные бури размечены по кодам 30–35. Справочная методика посадки: Д. К. Сучков, 2019, DOI <a href="https://doi.org/10.34736/FNC.2019.106.3.002">10.34736/FNC.2019.106.3.002</a>.</p></html>`;
    const blob = new Blob([report], { type: 'text/html;charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'otchet-lesopolosa-elista.html';
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  mapEl.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || event.target.closest('button,select')) return;
    mapEl.setPointerCapture(event.pointerId);
    const screen = eventToScreen(event);
    if (state.drawing && state.drawingKind === 'new') {
      event.preventDefault();
      event.stopPropagation();
      state.drawGesture = {
        type: 'new',
        start: screen,
        startGeo: screenToGeo(screen.x, screen.y),
        points: [screen],
        moved: false,
      };
      return;
    }
    if (state.drawing && state.drawingKind === 'edit') {
      event.preventDefault();
      event.stopPropagation();
      const vertex = nearestVertex(screen);
      if (vertex >= 0) {
        state.selectedVertex = vertex;
        state.dragGesture = { type: 'vertex', index: vertex, start: screen, moved: false, historySaved: false };
        state.draggingVertex = true;
        updateDrawControls();
        renderOverlay();
        return;
      }
      const edge = nearestEdge(screen);
      if (edge) {
        pushDrawHistory();
        state.drawPoints.splice(edge.index, 0, edge.point);
        state.selectedVertex = edge.index;
        state.dragGesture = { type: 'vertex', index: edge.index, start: screen, moved: true, historySaved: true };
        state.draggingVertex = true;
        updateDrawControls();
        renderOverlay();
        return;
      }
    }
    state.panning = true;
    state.panStart = { x: event.clientX, y: event.clientY, center: lonLatToWorld(state.center.lon, state.center.lat) };
    mapEl.classList.add('is-panning');
  });
  mapEl.addEventListener('pointermove', (event) => {
    const screen = eventToScreen(event);
    if (state.drawGesture?.type === 'new' && event.buttons === 1) {
      const gesture = state.drawGesture;
      if (Math.hypot(screen.x - gesture.start.x, screen.y - gesture.start.y) >= 4) gesture.moved = true;
      const last = gesture.points[gesture.points.length - 1];
      if (gesture.moved && Math.hypot(screen.x - last.x, screen.y - last.y) >= 3) {
        gesture.points.push(screen);
        const preview = gesture.points.map((p) => ({ ...screenToGeo(p.x, p.y), x: p.x, y: p.y }));
        drawingLayer.replaceChildren(svgNode('path', {
          d: preview.map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' '),
          class: 'drawing-path',
        }));
      }
      return;
    }
    if (state.draggingVertex && state.dragGesture?.type === 'vertex' && event.buttons === 1) {
      const gesture = state.dragGesture;
      if (Math.hypot(screen.x - gesture.start.x, screen.y - gesture.start.y) >= 2) gesture.moved = true;
      if (gesture.moved) {
        if (!gesture.historySaved) {
          pushDrawHistory();
          gesture.historySaved = true;
        }
        const geo = screenToGeo(screen.x, screen.y);
        state.drawPoints[gesture.index] = geo;
        renderOverlay();
      }
      return;
    }
    if (state.panning && state.panStart) {
      const dx = event.clientX - state.panStart.x;
      const dy = event.clientY - state.panStart.y;
      const shift = `translate(${dx}px, ${dy}px)`;
      tileLayer.style.transform = shift;
      overlay.style.transform = shift;
    }
  });
  mapEl.addEventListener('pointerup', (event) => {
    let closeAfterAdd = false;
    if (state.drawGesture?.type === 'new') {
      const gesture = state.drawGesture;
      const screen = eventToScreen(event);
      if (gesture.moved) {
        pushDrawHistory();
        const freehand = simplifyDraw(gesture.points.map((p) => screenToGeo(p.x, p.y)));
        state.drawPoints.push(...freehand.filter((point, i) => i > 0 || !state.drawPoints.length || Math.hypot(point.lon - state.drawPoints[state.drawPoints.length - 1].lon, point.lat - state.drawPoints[state.drawPoints.length - 1].lat) > 1e-8));
      } else {
        const first = state.drawPoints[0];
        if (state.drawPoints.length >= 3 && first) {
          const p = geoToScreen(first.lon, first.lat);
          closeAfterAdd = Math.hypot(p.x - screen.x, p.y - screen.y) <= 14;
        }
        if (!closeAfterAdd) {
          pushDrawHistory();
          state.drawPoints.push(gesture.startGeo);
        }
      }
      state.drawGesture = null;
      updateDrawControls();
      renderOverlay();
      if (closeAfterAdd) finishLasso();
    }
    if (state.draggingVertex) {
      state.draggingVertex = false;
      state.dragGesture = null;
      updateDrawControls();
      renderOverlay();
    }
    let movedMap = false;
    if (state.panning && state.panStart) {
      const dx = event.clientX - state.panStart.x;
      const dy = event.clientY - state.panStart.y;
      movedMap = Math.hypot(dx, dy) > 1;
      state.center = worldToLonLat(state.panStart.center.x - dx, state.panStart.center.y - dy);
    }
    state.panning = false;
    state.panStart = null;
    mapEl.classList.remove('is-panning');
    tileLayer.style.transform = '';
    overlay.style.transform = '';
    if (movedMap) renderTiles();
  });
  mapEl.addEventListener('pointercancel', () => {
    state.drawGesture = null;
    state.dragGesture = null;
    state.draggingVertex = false;
    state.panning = false;
    state.panStart = null;
    mapEl.classList.remove('is-panning');
    tileLayer.style.transform = '';
    overlay.style.transform = '';
    updateDrawControls();
    renderOverlay();
  });
  mapEl.addEventListener('wheel', (event) => {
    if (state.drawing) return;
    event.preventDefault();
    const oldZoom = state.zoom;
    const newZoom = Math.max(4, Math.min(18, oldZoom + (event.deltaY < 0 ? 1 : -1)));
    if (newZoom === oldZoom) return;
    const screen = eventToScreen(event);
    const before = screenToGeo(screen.x, screen.y);
    state.zoom = newZoom;
    const afterPixel = lonLatToWorld(before.lon, before.lat);
    const center = worldToLonLat(afterPixel.x - (screen.x - mapEl.clientWidth / 2), afterPixel.y - (screen.y - mapEl.clientHeight / 2));
    state.center = center;
    renderTiles();
  }, { passive: false });

  $('lasso-button').addEventListener('click', startLasso);
  $('finish-drawing').addEventListener('click', finishLasso);
  $('undo-draw').addEventListener('click', undoDraw);
  $('delete-selected-vertex').addEventListener('click', deleteSelectedVertex);
  $('demo-button').addEventListener('click', () => {
    const example = [
      { lat: 46.3717, lon: 44.3191 }, { lat: 46.3741, lon: 44.3228 },
      { lat: 46.3711, lon: 44.3272 }, { lat: 46.3675, lon: 44.3240 },
      { lat: 46.3685, lon: 44.3194 },
    ];
    setParcel(example, { label: 'Пример поля', zoom: 15 });
  });
  $('clear-parcel').addEventListener('click', clearParcel);
  $('calculate-button').addEventListener('click', () => calculate());
  function markScenarioChanged() {
    if (!state.result) return;
    state.result = null;
    state.options = [];
    state.activeOption = 0;
    $('results-content').hidden = true;
    $('results-empty').hidden = false;
    $('map-legend').hidden = true;
    $('map-foot-status').textContent = 'Параметры изменены · повторите расчёт';
    renderOverlay();
  }
  host.querySelectorAll('.sidebar input, .sidebar select').forEach((input) => {
    input.addEventListener('input', markScenarioChanged);
    input.addEventListener('change', markScenarioChanged);
  });
  $('export-button').addEventListener('click', exportResult);
  $('report-button').addEventListener('click', downloadReport);
  $('dismiss-toast').addEventListener('click', () => { $('map-toast').hidden = true; });
  $('zoom-in').addEventListener('click', () => { state.zoom = Math.min(18, state.zoom + 1); renderTiles(); });
  $('zoom-out').addEventListener('click', () => { state.zoom = Math.max(4, state.zoom - 1); renderTiles(); });
  $('locate-button').addEventListener('click', () => { state.center = { lat: REGION.lat, lon: REGION.lon }; state.zoom = REGION.zoom; renderTiles(); });
  $('north-button').addEventListener('click', () => { state.center = { ...state.center }; renderTiles(); });
  host.querySelectorAll('.layer-button').forEach((button) => button.addEventListener('click', () => {
    host.querySelectorAll('.layer-button').forEach((candidate) => candidate.classList.toggle('active', candidate === button));
    state.baseLayer = button.dataset.layer;
    renderTiles();
  }));
  $('about-button').addEventListener('click', () => $('info-dialog').showModal());
  $('source-info').addEventListener('click', () => showDialog('Источники метеоданных', '<p>Используются две пользовательские выгрузки для станции Элиста, ВМО 34861: таблица meteo.ru за 2010–2024 годы и почасовые метеополя RP5. Сроки наблюдений сопоставлены по дате и времени.</p><p>Положительный класс — только коды 30–35 из поля «Погода в срок» meteo.ru: 82 срока. Код 9 намеренно не включён. Для обучения доступны 42 684 строки после сопоставления и исключения неполных основных признаков. Влажность, температура и видимость взяты из RP5; ветер и код явления — из meteo.ru. Осадки не включены в модель из-за большого числа пропусков.</p><p>В дипломе укажите meteo.ru, rp5.ru и индекс станции ВМО 34861.</p>'));
  const methodText = '<p>Random Forest обучен на погодных наблюдениях Элисты. Если пользователь не задаёт погоду, система использует типичные значения из 82 отмеченных эпизодов пыльных бурь за 2010–2024 годы: медианы погодных параметров и самые частые месяц и сектор ветра. Это архивный сценарий, не прогноз на сегодня.</p><p>Алгоритм строит прямые внешние линии по главным осям контура, без учёта кадастров и транспортных объектов. Карта земного покрова помечает линию конфликтной при любом попадании её проверенных точек на отображённую застройку или постоянную воду; это картографическая проверка, она может не распознать узкую дорогу или аэропорт.</p><p>Методика Сучкова (2019) рекомендует учитывать тип местности и приводит ориентир около 200 м между полосами в степи. Это справочная рекомендация для систем полос на сельхозугодьях, а не жёсткое правило для внешних отрезков вокруг одного участка. <a href="https://doi.org/10.34736/FNC.2019.106.3.002" target="_blank" rel="noopener">Открыть статью</a>.</p><p>В приложении отдельно показаны уклон и приближённое направление вниз по склону по Copernicus DEM GLO-30, классы земного покрова по ESA WorldCover и почвенные оценки SoilGrids (~250 м). Уклон рассчитывается, но пока не меняет рекомендованное размещение; почвенные значения также справочные. Земной покров отдельно помогает выявлять линии через застройку и постоянную воду. Класс пашни не определяет её собственника. Кадастровые границы, дорожные полосы и зоны аэропортов не загружены, поэтому линию нельзя считать разрешённой к посадке без проверки. Ни один слой не выбирает автоматически древесную породу. DSM и глобальные карты не заменяют местную съёмку, анализ проб и сведения о грунтовых водах. Расчёт направления вниз по склону не является гидрологическим моделированием. Профиль ветровой тени упрощён, а его зона действия ограничена 20H. Индекс не является калиброванной вероятностью или оперативным прогнозом. Суховеи пока не моделируются: отдельной разметки для них нет.</p>';
  $('method-link').addEventListener('click', () => showDialog('Метод расчёта', methodText));
  $('result-info').addEventListener('click', () => showDialog('Как читать индекс', methodText));
  $('info-dialog').addEventListener('click', (event) => { if (event.target === $('info-dialog')) $('info-dialog').close(); });
  window.addEventListener('keydown', (event) => {
    const typing = event.target instanceof HTMLElement && event.target.matches('input,select,textarea,[contenteditable="true"]');
    if (state.drawing && event.key === 'Escape') { event.preventDefault(); cancelLasso(); return; }
    if (state.drawing && !typing && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); undoDraw(); return; }
    if (state.drawing && !typing && (event.key === 'Delete' || event.key === 'Backspace')) { event.preventDefault(); deleteSelectedVertex(); return; }
    if (state.drawing && !typing && event.key === 'Enter') { event.preventDefault(); finishLasso(); return; }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'l') { event.preventDefault(); startLasso(); }
  });
  window.addEventListener('resize', renderTiles);

  const mapResizeObserver = new ResizeObserver(() => renderTiles());
  mapResizeObserver.observe(mapEl);

  updateWindPresentation();
  renderTiles();
  fetch('/api/health').then((response) => response.json()).then((data) => {
    state.modelReady = Boolean(data.ok);
    state.gisStatus = data.gis || null;
    state.referenceScenario = data.reference_scenario || null;
    renderScenarioCard(state.referenceScenario);
    if (state.referenceScenario) updateWindPresentation(state.referenceScenario.wind_from);
    const chip = $('model-chip');
    const gisTitle = state.gisStatus
      ? `Растровые ГИС: ${state.gisStatus.ready ? 'файлы готовы' : state.gisStatus.available ? 'файлы доступны частично' : 'файлы недоступны'}; кадастр, дороги и зоны аэропортов не загружены`
      : 'ГИС-статус сервером не передан';
    chip.title = data.ok ? `${data.model} · ${data.trees} деревьев · область: ${data.region} · ${gisTitle}` : `${data.message} · ${gisTitle}`;
    chip.innerHTML = data.ok
      ? `<span class="chip-glyph">✳</span> Random Forest <span class="chip-sep">·</span> ${data.trees} деревьев`
      : '<span class="chip-glyph">✳</span> Элиста <span class="chip-sep">·</span> модель недоступна';
    $('calculate-label').textContent = data.ok ? 'Рассчитать размещение' : 'Модель Элисты не готова';
    $('calculate-button').disabled = !state.parcel || !state.modelReady;
  }).catch(() => {
    $('model-chip').title = 'Не удалось получить статус локальной модели Элисты.';
    $('calculate-label').textContent = 'Сервер модели недоступен';
  });
}
