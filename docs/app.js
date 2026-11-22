"use strict";

/**
 * Клиентская часть конвертера: разбор координат, GeoJSON/НСПД, карта и экспорт.
 * Всё считается в браузере; сервер используется только по желанию для НСПД.
 */

const EARTH_RADIUS = 6378137.0;
const MAX_MERCATOR = 20037508.342789244;
const MAX_MERCATOR_Y = 20048966.11005013;

const SAMPLE_COORDS = `52.28697, 104.28066
52.28740, 104.28190
52.28650, 104.28220
52.28620, 104.28090`;

const NUMBER_FORMAT = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
const AREA_FORMAT = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });

const state = {
  feature: null,
  layer: null,
  addMode: false,
  tab: "coords",
};

const $ = (id) => document.getElementById(id);

function setStatus(message, kind = "") {
  const element = $("status");
  if (!element) return;
  element.textContent = message;
  element.className = "status" + (kind ? " " + kind : "");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeXml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function round7(value) {
  return Number(value.toFixed(7));
}

function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

// ---------------------------------------------------------------------------
// Карта
// ---------------------------------------------------------------------------

const map = window.L
  ? window.L.map("map", { zoomControl: true, worldCopyJump: true }).setView([61.5, 95], 3)
  : null;

const baseLayers = {};

if (map) {
  baseLayers.osm = window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  });
  baseLayers.sat = window.L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    {
      maxZoom: 19,
      attribution: "Tiles &copy; Esri",
    }
  );
  baseLayers.topo = window.L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
    maxZoom: 17,
    attribution: '&copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
  });
  baseLayers.osm.addTo(map);
  window.L.control.scale({ imperial: false, position: "bottomright" }).addTo(map);
}

function switchBasemap(name) {
  if (!map) return;
  Object.entries(baseLayers).forEach(([key, layer]) => {
    if (key !== name && map.hasLayer(layer)) map.removeLayer(layer);
  });
  if (baseLayers[name] && !map.hasLayer(baseLayers[name])) baseLayers[name].addTo(map);
}

// ---------------------------------------------------------------------------
// Координаты и системы координат
// ---------------------------------------------------------------------------

function toWgs84(x, y, crs = "auto") {
  if (!isFiniteNumber(x) || !isFiniteNumber(y)) {
    throw new Error("Координаты должны быть числами");
  }
  const mode =
    crs === "auto" ? (Math.abs(x) > 180 || Math.abs(y) > 90 ? "epsg3857" : "wgs84") : crs;

  if (mode === "epsg3857") {
    if (Math.abs(x) > MAX_MERCATOR + 1e-6 || Math.abs(y) > MAX_MERCATOR_Y + 1e-6) {
      throw new Error("Координаты вне диапазона EPSG:3857. Проверьте систему координат.");
    }
    const lon = (x / EARTH_RADIUS) * (180 / Math.PI);
    const lat = (2 * Math.atan(Math.exp(y / EARTH_RADIUS)) - Math.PI / 2) * (180 / Math.PI);
    return [round7(lon), round7(lat)];
  }

  if (mode !== "wgs84") throw new Error("Неизвестная система координат: " + mode);
  if (Math.abs(x) > 180 || Math.abs(y) > 90) {
    throw new Error("Координаты вне диапазона WGS84: долгота ±180, широта ±90.");
  }
  return [round7(x), round7(y)];
}

function wgs84ToMercator(lon, lat) {
  const x = (lon * Math.PI * EARTH_RADIUS) / 180;
  const y =
    Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) * EARTH_RADIUS;
  return [x, y];
}

function forEachPosition(geometry, callback) {
  if (!geometry || typeof geometry !== "object") return;
  const type = geometry.type;
  const coords = geometry.coordinates;
  if (type === "Point") {
    callback(coords);
  } else if (type === "MultiPoint" || type === "LineString") {
    (coords || []).forEach(callback);
  } else if (type === "MultiLineString" || type === "Polygon") {
    (coords || []).forEach((part) => (part || []).forEach(callback));
  } else if (type === "MultiPolygon") {
    (coords || []).forEach((polygon) =>
      (polygon || []).forEach((ring) => (ring || []).forEach(callback))
    );
  } else if (type === "GeometryCollection") {
    (geometry.geometries || []).forEach((child) => forEachPosition(child, callback));
  }
}

function detectCrs(geometry) {
  let mercator = false;
  forEachPosition(geometry, (position) => {
    if (!Array.isArray(position)) return;
    if (Math.abs(Number(position[0])) > 180 || Math.abs(Number(position[1])) > 90) {
      mercator = true;
    }
  });
  return mercator ? "epsg3857" : "wgs84";
}

function normalizeGeometry(geometry, crs = "auto") {
  if (!geometry || typeof geometry !== "object" || !geometry.type) {
    throw new Error("В данных нет геометрии GeoJSON");
  }
  const resolved = crs === "auto" ? detectCrs(geometry) : crs;

  const convert = (position) => {
    if (!Array.isArray(position) || position.length < 2) {
      throw new Error("Некорректная точка в геометрии");
    }
    const [lon, lat] = toWgs84(
      Number(position[0]),
      Number(position[1]),
      resolved
    );
    const result = [lon, lat];
    if (position.length > 2 && isFiniteNumber(Number(position[2]))) {
      result.push(Number(position[2]));
    }
    return result;
  };

  const type = geometry.type;
  const coords = geometry.coordinates;
  if (type === "Point") return { type, coordinates: convert(coords) };
  if (type === "MultiPoint" || type === "LineString") {
    return { type, coordinates: (coords || []).map(convert) };
  }
  if (type === "MultiLineString" || type === "Polygon") {
    return { type, coordinates: (coords || []).map((part) => (part || []).map(convert)) };
  }
  if (type === "MultiPolygon") {
    return {
      type,
      coordinates: (coords || []).map((polygon) => (polygon || []).map((ring) => (ring || []).map(convert))),
    };
  }
  if (type === "GeometryCollection") {
    return {
      type,
      geometries: (geometry.geometries || []).map((child) => normalizeGeometry(child, resolved)),
    };
  }
  throw new Error("Неподдерживаемый тип геометрии: " + type);
}

function parseNumber(token) {
  const value = Number(String(token).replace(",", "."));
  if (!Number.isFinite(value)) throw new Error("Не удалось разобрать число: " + token);
  return value;
}

function parseGroup(tokens) {
  let hemisphere = null;
  const parts = tokens.slice();
  if (parts.length && /^[NSEW]$/i.test(parts[parts.length - 1])) {
    hemisphere = parts.pop().toUpperCase();
  }
  if (!parts.length || parts.length > 3) {
    throw new Error("Ожидались градусы, минуты и секунды");
  }
  const degrees = parseNumber(parts[0]);
  const minutes = parts[1] !== undefined ? parseNumber(parts[1]) : 0;
  const seconds = parts[2] !== undefined ? parseNumber(parts[2]) : 0;
  if (minutes < 0 || minutes >= 60) throw new Error("Минуты должны быть в диапазоне 0–59");
  if (seconds < 0 || seconds >= 60) throw new Error("Секунды должны быть в диапазоне 0–59");
  let value = Math.abs(degrees) + minutes / 60 + seconds / 3600;
  if (hemisphere === "S" || hemisphere === "W") value = -value;
  else if (!hemisphere && degrees < 0) value = -value;
  return { value, hemisphere };
}

function parseCoordinateLine(line, lineNumber) {
  const tokens = line.match(/-?\d+(?:[.,]\d+)?|[NSEW]/gi);
  if (!tokens) throw new Error(`Строка ${lineNumber}: не найдены координаты`);

  const groups = [];
  let current = [];
  tokens.forEach((token) => {
    if (/^[NSEW]$/i.test(token)) {
      current.push(token);
      groups.push(current);
      current = [];
    } else {
      current.push(token);
    }
  });
  if (current.length) groups.push(current);

  if (groups.length === 2) {
    const first = parseGroup(groups[0]);
    const second = parseGroup(groups[1]);
    return { a: first.value, b: second.value, hemiA: first.hemisphere, hemiB: second.hemisphere };
  }
  if (groups.length === 1) {
    const numbers = groups[0];
    if (numbers.length === 2) {
      return { a: parseNumber(numbers[0]), b: parseNumber(numbers[1]), hemiA: null, hemiB: null };
    }
    if (numbers.length === 4) {
      const first = parseGroup(numbers.slice(0, 2));
      const second = parseGroup(numbers.slice(2));
      return { a: first.value, b: second.value, hemiA: null, hemiB: null };
    }
    throw new Error(`Строка ${lineNumber}: ожидались две координаты`);
  }
  throw new Error(`Строка ${lineNumber}: найдено больше двух координат`);
}

function resolvePair(pair, order, lineNumber) {
  const { a, b, hemiA, hemiB } = pair;

  if (order === "epsg3857") {
    if (hemiA || hemiB) throw new Error(`Строка ${lineNumber}: градусы-минуты-секунды несовместимы с EPSG:3857`);
    return toWgs84(a, b, "epsg3857");
  }

  let lat;
  let lon;
  if (
    (hemiA === "N" || hemiA === "S") &&
    (hemiB === "E" || hemiB === "W")
  ) {
    lat = a;
    lon = b;
  } else if (
    (hemiA === "E" || hemiA === "W") &&
    (hemiB === "N" || hemiB === "S")
  ) {
    lon = a;
    lat = b;
  } else if (hemiA === "N" || hemiA === "S") {
    lat = a;
    lon = b;
  } else if (hemiB === "N" || hemiB === "S") {
    lon = a;
    lat = b;
  } else if (order === "latlon") {
    lat = a;
    lon = b;
  } else if (order === "lonlat") {
    lon = a;
    lat = b;
  } else if (Math.abs(a) <= 90 && Math.abs(b) <= 180) {
    lat = a;
    lon = b;
  } else if (Math.abs(a) <= 180 && Math.abs(b) <= 90) {
    lon = a;
    lat = b;
  } else {
    throw new Error(`Строка ${lineNumber}: не удалось определить порядок координат`);
  }

  if (Math.abs(lon) > 180 || Math.abs(lat) > 90) {
    throw new Error(`Строка ${lineNumber}: координаты вне диапазона WGS84`);
  }
  return [round7(lon), round7(lat)];
}

function parseCoords(text, order) {
  const points = [];
  let lineNumber = 0;
  text.split(/\r?\n/).forEach((rawLine) => {
    lineNumber += 1;
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) return;
    const pair = parseCoordinateLine(line, lineNumber);
    points.push(resolvePair(pair, order, lineNumber));
  });
  return points;
}

function buildGeometry(points, geometryType) {
  if (geometryType === "Polygon") {
    if (points.length < 3) throw new Error("Для полигона нужно минимум 3 точки");
    const ring = points.map((point) => point.slice());
    const first = ring[0];
    const last = ring[ring.length - 1];
    if (first[0] !== last[0] || first[1] !== last[1]) ring.push(first.slice());
    return { type: "Polygon", coordinates: [ring] };
  }
  if (geometryType === "LineString") {
    if (points.length < 2) throw new Error("Для линии нужно минимум 2 точки");
    return { type: "LineString", coordinates: points };
  }
  if (points.length < 1) throw new Error("Добавьте хотя бы одну точку");
  return { type: "MultiPoint", coordinates: points };
}

// ---------------------------------------------------------------------------
// Метрики
// ---------------------------------------------------------------------------

function radians(value) {
  return (value * Math.PI) / 180;
}

function haversine(lon1, lat1, lon2, lat2) {
  const phi1 = radians(lat1);
  const phi2 = radians(lat2);
  const dPhi = phi2 - phi1;
  const dLambda = radians(lon2 - lon1);
  const a =
    Math.sin(dPhi / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dLambda / 2) ** 2;
  return 2 * EARTH_RADIUS * Math.asin(Math.min(1, Math.sqrt(a)));
}

function ringArea(ring) {
  if (!ring || ring.length < 3) return 0;
  let total = 0;
  for (let index = 0; index < ring.length; index += 1) {
    const [lon1, lat1] = ring[index];
    const [lon2, lat2] = ring[(index + 1) % ring.length];
    total += radians(lon2 - lon1) * (2 + Math.sin(radians(lat1)) + Math.sin(radians(lat2)));
  }
  return Math.abs((total * EARTH_RADIUS * EARTH_RADIUS) / 2);
}

function lineLength(points) {
  let total = 0;
  for (let index = 0; index < points.length - 1; index += 1) {
    total += haversine(points[index][0], points[index][1], points[index + 1][0], points[index + 1][1]);
  }
  return total;
}

function geometryMetrics(geometry) {
  const metrics = { points: 0, area: 0, perimeter: 0 };
  if (!geometry) return metrics;
  forEachPosition(geometry, () => {
    metrics.points += 1;
  });

  const type = geometry.type;
  const coords = geometry.coordinates;
  if (type === "LineString") {
    metrics.perimeter = lineLength(coords || []);
  } else if (type === "MultiLineString") {
    metrics.perimeter = (coords || []).reduce((sum, line) => sum + lineLength(line || []), 0);
  } else if (type === "Polygon") {
    (coords || []).forEach((ring) => {
      metrics.perimeter += lineLength(ring || []);
    });
    if (coords && coords[0]) {
      metrics.area = ringArea(coords[0]);
      coords.slice(1).forEach((hole) => {
        metrics.area -= ringArea(hole);
      });
      metrics.area = Math.max(0, metrics.area);
    }
  } else if (type === "MultiPolygon") {
    (coords || []).forEach((polygon) => {
      if (!polygon || !polygon.length) return;
      metrics.area += ringArea(polygon[0]);
      polygon.slice(1).forEach((hole) => {
        metrics.area -= ringArea(hole);
      });
      polygon.forEach((ring) => {
        metrics.perimeter += lineLength(ring || []);
      });
    });
    metrics.area = Math.max(0, metrics.area);
  } else if (type === "GeometryCollection") {
    (geometry.geometries || []).forEach((child) => {
      const childMetrics = geometryMetrics(child);
      metrics.area += childMetrics.area;
      metrics.perimeter += childMetrics.perimeter;
    });
  }
  return metrics;
}

function formatArea(area) {
  if (!area || area <= 0) return "—";
  if (area < 1000) return `${NUMBER_FORMAT.format(area)} м²`;
  if (area < 100000) return `${NUMBER_FORMAT.format(area)} м² (${AREA_FORMAT.format(area / 100)} сот.)`;
  return `${NUMBER_FORMAT.format(area)} м² (${AREA_FORMAT.format(area / 10000)} га)`;
}

function formatDistance(distance) {
  if (!distance || distance <= 0) return "—";
  return `${NUMBER_FORMAT.format(distance)} м`;
}

function updateStats(geometry) {
  const metrics = geometryMetrics(geometry);
  $("statPoints").textContent = metrics.points || "—";
  $("statArea").textContent = formatArea(metrics.area);
  $("statPerimeter").textContent =
    geometry.type === "MultiPoint" || geometry.type === "Point"
      ? "—"
      : formatDistance(metrics.perimeter);

  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;
  forEachPosition(geometry, (position) => {
    const lon = Number(position[0]);
    const lat = Number(position[1]);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) return;
    minLon = Math.min(minLon, lon);
    maxLon = Math.max(maxLon, lon);
    minLat = Math.min(minLat, lat);
    maxLat = Math.max(maxLat, lat);
  });
  $("statBbox").textContent =
    minLon === Infinity
      ? "—"
      : `${minLat.toFixed(5)}…${maxLat.toFixed(5)} с.ш.; ${minLon.toFixed(5)}…${maxLon.toFixed(5)} в.д.`;
}

// ---------------------------------------------------------------------------
// Отрисовка и загрузка объекта
// ---------------------------------------------------------------------------

function popupContent(feature) {
  const properties = feature.properties || {};
  const geometry = feature.geometry;
  const name = properties.name || properties.cad_num || "Объект";
  const address = properties.address || properties.description || "";
  let html = `<strong>${escapeHtml(name)}</strong>`;
  if (address) html += `<div>${escapeHtml(address)}</div>`;
  if (geometry && geometry.type !== "MultiPoint" && geometry.type !== "Point") {
    const metrics = geometryMetrics(geometry);
    if (metrics.area > 0) html += `<div>Площадь: ${escapeHtml(formatArea(metrics.area))}</div>`;
    if (metrics.perimeter > 0) {
      html += `<div>Периметр: ${escapeHtml(formatDistance(metrics.perimeter))}</div>`;
    }
  }
  return html;
}

function renderFeature(feature, { fit = true } = {}) {
  if (!map) return;
  if (state.layer) {
    map.removeLayer(state.layer);
    state.layer = null;
  }
  state.layer = window.L.geoJSON(feature, {
    style: {
      color: "#ea580c",
      weight: 3,
      fillColor: "#f97316",
      fillOpacity: 0.22,
    },
    pointToLayer: (_feature, latlng) =>
      window.L.circleMarker(latlng, {
        radius: 6,
        color: "#ea580c",
        weight: 2,
        fillColor: "#ffffff",
        fillOpacity: 1,
      }),
    onEachFeature: (item, layer) => {
      layer.bindPopup(popupContent(item));
      layer.on("click", (event) => {
        if (!state.addMode) return;
        layer.closePopup();
        window.L.DomEvent.stopPropagation(event);
        appendPoint(event.latlng);
      });
    },
  }).addTo(map);

  if (fit) {
    const bounds = state.layer.getBounds();
    if (bounds && bounds.isValid()) {
      map.fitBounds(bounds, { padding: [28, 28], maxZoom: 18 });
    }
  }
}

function setFeature(feature, { fit = true, status = "", kind = "ok" } = {}) {
  if (!feature || !feature.geometry) {
    throw new Error("В объекте нет геометрии");
  }
  const normalized = normalizeGeometry(feature.geometry, "auto");
  const normalizedFeature = {
    type: "Feature",
    properties: Object.assign(
      {},
      feature.properties || {},
      { name: (feature.properties && feature.properties.name) || $("objName").value.trim() || "Объект" }
    ),
    geometry: normalized,
  };
  state.feature = normalizedFeature;
  renderFeature(normalizedFeature, { fit });
  updateStats(normalized);
  setDownloadsEnabled(true);
  if (status) setStatus(status, kind);
}

function setDownloadsEnabled(enabled) {
  ["downloadKml", "downloadKmz", "downloadGeo", "copyKml"].forEach((id) => {
    const button = $(id);
    if (button) button.disabled = !enabled;
  });
}

function safeFilename(name) {
  const cleaned = String(name || "export")
    .replace(/[^\p{L}\p{N}._-]+/gu, "_")
    .replace(/^[._]+|[._]+$/g, "")
    .slice(0, 80);
  return cleaned || "export";
}

// ---------------------------------------------------------------------------
// KML / KMZ / GeoJSON
// ---------------------------------------------------------------------------

function positionToKml(position) {
  if (!Array.isArray(position) || position.length < 2) {
    throw new Error("Некорректная точка");
  }
  return `${Number(position[0]).toFixed(7)},${Number(position[1]).toFixed(7)},0`;
}

function ringToKml(ring) {
  const points = (ring || []).map((point) => point.slice());
  if (!points.length) throw new Error("Пустое кольцо полигона");
  const first = points[0];
  const last = points[points.length - 1];
  if (first[0] !== last[0] || first[1] !== last[1]) points.push(first.slice());
  return points.map(positionToKml).join(" ");
}

function lineToKml(points) {
  if (!points || points.length < 2) throw new Error("Для линии нужно минимум 2 точки");
  return points.map(positionToKml).join(" ");
}

function polygonToKml(rings) {
  if (!rings || !rings.length) throw new Error("Полигон без координат");
  let xml = `<Polygon><outerBoundaryIs><LinearRing><coordinates>${ringToKml(
    rings[0]
  )}</coordinates></LinearRing></outerBoundaryIs>`;
  rings.slice(1).forEach((hole) => {
    xml += `<innerBoundaryIs><LinearRing><coordinates>${ringToKml(
      hole
    )}</coordinates></LinearRing></innerBoundaryIs>`;
  });
  return xml + "</Polygon>";
}

function geometryToKml(geometry) {
  if (!geometry) throw new Error("Нет геометрии");
  const type = geometry.type;
  const coords = geometry.coordinates;
  if (type === "Point") {
    return `<Point><coordinates>${positionToKml(coords)}</coordinates></Point>`;
  }
  if (type === "MultiPoint") {
    return `<MultiGeometry>${coords
      .map((point) => `<Point><coordinates>${positionToKml(point)}</coordinates></Point>`)
      .join("")}</MultiGeometry>`;
  }
  if (type === "LineString") {
    return `<LineString><tessellate>1</tessellate><coordinates>${lineToKml(
      coords
    )}</coordinates></LineString>`;
  }
  if (type === "MultiLineString") {
    return `<MultiGeometry>${coords
      .map(
        (line) =>
          `<LineString><tessellate>1</tessellate><coordinates>${lineToKml(
            line
          )}</coordinates></LineString>`
      )
      .join("")}</MultiGeometry>`;
  }
  if (type === "Polygon") return polygonToKml(coords);
  if (type === "MultiPolygon") {
    return `<MultiGeometry>${coords.map(polygonToKml).join("")}</MultiGeometry>`;
  }
  if (type === "GeometryCollection") {
    return `<MultiGeometry>${(geometry.geometries || [])
      .map(geometryToKml)
      .join("")}</MultiGeometry>`;
  }
  throw new Error("Неподдерживаемый тип геометрии: " + type);
}

function extendedData(properties) {
  const entries = Object.entries(properties || {}).filter(
    ([key, value]) =>
      ["cad_num", "address", "category", "area_value", "cost_value"].includes(key) &&
      value !== undefined &&
      value !== null &&
      value !== ""
  );
  if (!entries.length) return "";
  return `<ExtendedData>${entries
    .map(
      ([key, value]) =>
        `<Data name="${escapeXml(key)}"><value>${escapeXml(value)}</value></Data>`
    )
    .join("")}</ExtendedData>`;
}

function buildKml(feature) {
  const properties = feature.properties || {};
  const name = properties.name || "Экспорт";
  const description = properties.description || "";
  const placemark =
    `<Placemark><name>${escapeXml(name)}</name>` +
    (description ? `<description>${escapeXml(description)}</description>` : "") +
    `<styleUrl>#cadastrStyle</styleUrl>` +
    extendedData(properties) +
    geometryToKml(feature.geometry) +
    `</Placemark>`;

  return `<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>${escapeXml(name)}</name>
    <Style id="cadastrStyle">
      <LineStyle><color>ffeb6325</color><width>2.5</width></LineStyle>
      <PolyStyle><color>4deb6325</color></PolyStyle>
    </Style>
    ${placemark}
  </Document>
</kml>
`;
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function download(fmt) {
  if (!state.feature) {
    setStatus("Сначала загрузите объект или координаты.", "err");
    return;
  }
  try {
    const name = safeFilename(state.feature.properties.name);
    if (fmt === "geojson") {
      const payload = {
        type: "Feature",
        properties: state.feature.properties,
        geometry: state.feature.geometry,
      };
      triggerDownload(
        new Blob([JSON.stringify(payload, null, 2)], { type: "application/geo+json;charset=utf-8" }),
        `${name}.geojson`
      );
      setStatus(`✔ GeoJSON сохранён: ${name}.geojson`, "ok");
      return;
    }

    const kml = buildKml(state.feature);
    if (fmt === "kmz") {
      if (typeof window.JSZip === "undefined") {
        throw new Error("Библиотека JSZip не загрузилась — KMZ недоступен. Скачайте KML.");
      }
      const zip = new window.JSZip();
      zip.file("doc.kml", kml);
      const blob = await zip.generateAsync({
        type: "blob",
        compression: "DEFLATE",
        compressionOptions: { level: 6 },
        mimeType: "application/vnd.google-earth.kmz",
      });
      triggerDownload(blob, `${name}.kmz`);
      setStatus(`✔ KMZ сохранён: ${name}.kmz`, "ok");
    } else {
      triggerDownload(
        new Blob([kml], { type: "application/vnd.google-earth.kml+xml;charset=utf-8" }),
        `${name}.kml`
      );
      setStatus(`✔ KML сохранён: ${name}.kml`, "ok");
    }
  } catch (error) {
    setStatus("Ошибка экспорта: " + error.message, "err");
  }
}

async function copyKml() {
  if (!state.feature) return;
  try {
    const kml = buildKml(state.feature);
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(kml);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = kml;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
    }
    setStatus("✔ KML скопирован в буфер обмена.", "ok");
  } catch (error) {
    setStatus("Не удалось скопировать KML: " + error.message, "err");
  }
}

// ---------------------------------------------------------------------------
// Вкладка «Из координат»
// ---------------------------------------------------------------------------

function updateCoordsHint() {
  const lines = $("coordsInput")
    .value.split(/\r?\n/)
    .filter((line) => line.trim() && !line.trim().startsWith("#")).length;
  $("coordsHint").textContent = `Строк: ${lines}. Можно перетащить файл .txt / .csv / .geojson в эту панель.`;
}

function previewCoords({ silent = false } = {}) {
  try {
    const text = $("coordsInput").value;
    if (!text.trim()) throw new Error("Введите координаты");
    const order = $("coordOrder").value;
    const points = parseCoords(text, order);
    if (!points.length) throw new Error("Введите хотя бы одну пару координат");
    const geometryType = $("geomType").value;
    const geometry = buildGeometry(points, geometryType);
    const name = $("objName").value.trim() || "Объект";
    const description = `Точек: ${points.length}`;
    setFeature(
      {
        type: "Feature",
        properties: { name, description },
        geometry,
      },
      { fit: true }
    );
    setStatus(`✔ Точек: ${points.length}. Объект готов к экспорту.`, "ok");
  } catch (error) {
    if (!silent) setStatus("Ошибка: " + error.message, "err");
  }
}

// ---------------------------------------------------------------------------
// Вкладка «Из НСПД»
// ---------------------------------------------------------------------------

function findNspdFeature(payload) {
  if (!payload || typeof payload !== "object") return null;
  if (Array.isArray(payload)) {
    for (const item of payload) {
      const feature = findNspdFeature(item);
      if (feature) return feature;
    }
    return null;
  }
  if (payload.type === "Feature" && payload.geometry) return payload;
  if (Array.isArray(payload.features)) {
    const withGeometry = payload.features.find((feature) => feature && feature.geometry);
    if (withGeometry) return withGeometry;
  }
  for (const key of ["data", "result", "results", "items"]) {
    if (payload[key]) {
      const feature = findNspdFeature(payload[key]);
      if (feature) return feature;
    }
  }
  if (payload.geometry && payload.geometry.type) return payload;
  return null;
}

function mergeGeometries(features) {
  const geometries = features
    .map((feature) => (feature && feature.geometry ? feature.geometry : feature))
    .filter((geometry) => geometry && geometry.type);
  if (!geometries.length) return null;
  if (geometries.length === 1) return geometries[0];
  return { type: "GeometryCollection", geometries };
}

function geometryFromGeoJson(payload) {
  if (!payload || typeof payload !== "object") return null;
  if (payload.type === "FeatureCollection" && Array.isArray(payload.features)) {
    return mergeGeometries(payload.features);
  }
  if (payload.type === "Feature") return payload.geometry || null;
  if (payload.type && payload.coordinates !== undefined) return payload;
  if (payload.type === "GeometryCollection") return payload;
  const feature = findNspdFeature(payload);
  return feature && feature.geometry ? feature.geometry : null;
}

function nspdProperties(payload, feature) {
  const props = (feature && feature.properties) || {};
  const options = props.options || {};
  const cadnum = options.cad_num || props.cad_num || $("cadnum").value.trim();
  const address =
    options.readable_address || props.readable_address || props.address || "Адрес не указан";
  return { name: cadnum || "Объект НСПД", address, cadnum };
}

function parseNspdJson() {
  try {
    const text = $("nspdJson").value.trim();
    if (!text) throw new Error("Вставьте JSON из НСПД или файл GeoJSON");
    let payload;
    try {
      payload = JSON.parse(text);
    } catch (error) {
      throw new Error("Это не валидный JSON: " + error.message);
    }

    const feature = findNspdFeature(payload);
    if (!feature || !feature.geometry) throw new Error("В JSON не найдена геометрия объекта");
    const isNspd = Boolean(payload && payload.data);
    const properties = isNspd
      ? nspdProperties(payload, feature)
      : {
          name:
            (feature.properties && (feature.properties.name || feature.properties.cad_num)) ||
            "Импортированный объект",
          address: (feature.properties && feature.properties.address) || "",
          cadnum: (feature.properties && feature.properties.cad_num) || "",
        };
    const geometry = normalizeGeometry(feature.geometry, "auto");
    $("objName").value = properties.name;
    setFeature(
      {
        type: "Feature",
        properties: {
          name: properties.name,
          description: `Кадастровый номер: ${properties.name}\nАдрес: ${properties.address}`,
          address: properties.address,
          cad_num: properties.cadnum,
        },
        geometry,
      },
      { fit: true }
    );
    setStatus(`✔ Объект распознан: ${properties.address}`, "ok");
  } catch (error) {
    setStatus("Ошибка разбора JSON: " + error.message, "err");
  }
}

function openNspdJson() {
  const cadnum = $("cadnum").value.trim();
  if (!cadnum) {
    setStatus("Введите кадастровый номер", "err");
    return;
  }
  const thematicId = $("thematicId").value;
  const url = `https://nspd.gov.ru/api/geoportal/v2/search/geoportal?query=${encodeURIComponent(
    cadnum
  )}&thematicSearchId=${thematicId}`;
  window.open(url, "_blank", "noopener");
}

async function fetchViaBackend() {
  const cadnum = $("cadnum").value.trim();
  if (!cadnum) {
    setStatus("Введите кадастровый номер", "err");
    return;
  }
  const button = $("backendBtn");
  button.disabled = true;
  setStatus("Запрос к НСПД через сервер…");
  try {
    const url = `/api/nspd?cadnum=${encodeURIComponent(cadnum)}&thematic_id=${encodeURIComponent(
      $("thematicId").value
    )}`;
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    let body = null;
    try {
      body = await response.json();
    } catch (error) {
      body = null;
    }
    if (!response.ok) {
      throw new Error((body && body.detail) || `Сервер вернул HTTP ${response.status}`);
    }
    if (!body || !body.geometry) throw new Error("В ответе сервера нет геометрии");
    $("objName").value = body.cadnum || cadnum;
    setFeature(
      {
        type: "Feature",
        properties: {
          name: body.cadnum || cadnum,
          address: body.address,
          cad_num: body.cadnum || cadnum,
          description: `Кадастровый номер: ${body.cadnum || cadnum}\nАдрес: ${body.address || ""}`,
        },
        geometry: body.geometry,
      },
      { fit: true }
    );
    setStatus(`✔ Загружено из НСПД: ${body.address || body.cadnum}`, "ok");
  } catch (error) {
    setStatus("Ошибка НСПД: " + error.message, "err");
  } finally {
    button.disabled = false;
  }
}

async function checkBackend() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 2500);
  try {
    const response = await fetch("/api/health", {
      signal: controller.signal,
      headers: { Accept: "application/json" },
    });
    if (!response.ok) return;
    const body = await response.json();
    if (body && body.status === "ok") {
      $("backendBlock").classList.remove("hidden");
    }
  } catch (error) {
    // Статический хостинг без бэкенда — это нормально.
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// Файлы и drag & drop
// ---------------------------------------------------------------------------

async function handleFile(file) {
  if (!file) return;
  try {
    const text = await file.text();
    const name = file.name || "import";
    const isGeoJson = /\.(geojson|json)$/i.test(name) || /^\s*[{[]/.test(text);

    if (isGeoJson) {
      let payload;
      try {
        payload = JSON.parse(text);
      } catch (error) {
        throw new Error("Файл не является валидным JSON/GeoJSON");
      }
      const geometry = geometryFromGeoJson(payload);
      if (!geometry) throw new Error("В файле не найдена геометрия");
      const feature = findNspdFeature(payload);
      const isNspd = Boolean(payload && payload.data);
      const properties = isNspd && feature
        ? nspdProperties(payload, feature)
        : {
            name:
              (feature && feature.properties && (feature.properties.name || feature.properties.cad_num)) ||
              name.replace(/\.(geojson|json)$/i, ""),
            address: "",
          };
      const normalized = normalizeGeometry(geometry, "auto");
      $("objName").value = properties.name || name;
      setFeature(
        {
          type: "Feature",
          properties: {
            name: properties.name || name,
            address: properties.address || "",
            cad_num: properties.cadnum || "",
            description: properties.address ? `Адрес: ${properties.address}` : "",
          },
          geometry: normalized,
        },
        { fit: true }
      );
      setStatus(`✔ Импортирован файл ${name}`, "ok");
      return;
    }

    switchTab("coords");
    $("coordsInput").value = text;
    updateCoordsHint();
    previewCoords();
  } catch (error) {
    setStatus("Ошибка импорта: " + error.message, "err");
  }
}

function setupDragAndDrop() {
  const panel = $("panel");
  ["dragenter", "dragover"].forEach((eventName) => {
    panel.addEventListener(eventName, (event) => {
      event.preventDefault();
      panel.classList.add("drag-over");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    panel.addEventListener(eventName, (event) => {
      event.preventDefault();
      panel.classList.remove("drag-over");
    });
  });
  panel.addEventListener("drop", (event) => {
    const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
    handleFile(file);
  });
}

// ---------------------------------------------------------------------------
// Вкладки, тема, карта
// ---------------------------------------------------------------------------

function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.tab === tab));
  });
  $("panelCoords").classList.toggle("hidden", tab !== "coords");
  $("panelNspd").classList.toggle("hidden", tab !== "nspd");
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("cadastr-theme", theme);
  } catch (error) {
    // Игнорируем недоступный localStorage.
  }
  const button = $("themeToggle");
  if (button) {
    const isDark = theme === "dark";
    button.textContent = isDark ? "☀" : "☾";
    button.setAttribute("aria-label", isDark ? "Включить светлую тему" : "Включить тёмную тему");
  }
}

function currentTheme() {
  if (document.documentElement.dataset.theme) return document.documentElement.dataset.theme;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function toggleTheme() {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
}

function setAddMode(enabled) {
  state.addMode = enabled;
  $("addPointBtn").setAttribute("aria-pressed", String(enabled));
  $("mapHint").hidden = !enabled;
  if (map) map.getContainer().style.cursor = enabled ? "crosshair" : "";
}

function appendPoint(latlng) {
  if (state.tab !== "coords") switchTab("coords");
  const order = $("coordOrder").value;
  let line;
  if (order === "lonlat") {
    line = `${latlng.lng.toFixed(7)}, ${latlng.lat.toFixed(7)}`;
  } else if (order === "epsg3857") {
    const [x, y] = wgs84ToMercator(latlng.lng, latlng.lat);
    line = `${x.toFixed(2)}, ${y.toFixed(2)}`;
  } else {
    line = `${latlng.lat.toFixed(7)}, ${latlng.lng.toFixed(7)}`;
  }
  const textarea = $("coordsInput");
  textarea.value = textarea.value.trim() ? `${textarea.value.trim()}\n${line}` : line;
  updateCoordsHint();
  setStatus(`Точка добавлена: ${line}. Нажмите «Показать на карте».`, "ok");
}

// ---------------------------------------------------------------------------
// Инициализация
// ---------------------------------------------------------------------------

function init() {
  if (!map) {
    setStatus("Не удалось загрузить Leaflet — проверьте доступ к CDN или интернет.", "err");
    return;
  }

  $("tabCoords").addEventListener("click", () => switchTab("coords"));
  $("tabNspd").addEventListener("click", () => switchTab("nspd"));
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        switchTab(button.dataset.tab);
      }
    });
  });

  $("previewBtn").addEventListener("click", () => previewCoords());
  $("clearBtn").addEventListener("click", () => {
    $("coordsInput").value = "";
    updateCoordsHint();
    setStatus("Поле очищено.");
    setDownloadsEnabled(false);
    state.feature = null;
    if (state.layer) {
      map.removeLayer(state.layer);
      state.layer = null;
    }
    ["statPoints", "statArea", "statPerimeter", "statBbox"].forEach((id) => {
      $(id).textContent = "—";
    });
  });
  $("sampleBtn").addEventListener("click", () => {
    $("coordsInput").value = SAMPLE_COORDS;
    updateCoordsHint();
    previewCoords();
  });
  $("coordsInput").addEventListener("input", updateCoordsHint);
  $("coordsInput").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      previewCoords();
    }
  });

  $("downloadKml").addEventListener("click", () => download("kml"));
  $("downloadKmz").addEventListener("click", () => download("kmz"));
  $("downloadGeo").addEventListener("click", () => download("geojson"));
  $("copyKml").addEventListener("click", copyKml);

  $("openJsonBtn").addEventListener("click", openNspdJson);
  $("parseJsonBtn").addEventListener("click", parseNspdJson);
  $("nspdFile").addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    handleFile(file);
    event.target.value = "";
  });
  $("backendBtn").addEventListener("click", fetchViaBackend);

  $("themeToggle").addEventListener("click", toggleTheme);
  applyTheme(currentTheme());

  $("basemapSelect").addEventListener("change", (event) => switchBasemap(event.target.value));
  $("addPointBtn").addEventListener("click", () => setAddMode(!state.addMode));
  $("fitBtn").addEventListener("click", () => {
    if (state.layer) {
      const bounds = state.layer.getBounds();
      if (bounds && bounds.isValid()) map.fitBounds(bounds, { padding: [28, 28], maxZoom: 18 });
    }
  });

  map.on("click", (event) => {
    if (state.addMode) appendPoint(event.latlng);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.addMode) setAddMode(false);
  });
  window.addEventListener("resize", () => map.invalidateSize());

  setupDragAndDrop();
  checkBackend();
  updateCoordsHint();

  // Демонстрационный объект, чтобы карта не была пустой при первом открытии.
  $("coordsInput").value = SAMPLE_COORDS;
  updateCoordsHint();
  previewCoords({ silent: true });
  if (state.feature) {
    setStatus("Пример загружен. Замените координаты своими или откройте вкладку «Из НСПД».", "ok");
  } else {
    setStatus("Готов к работе. Данные обрабатываются в браузере.");
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
