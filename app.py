"""Simulacro independiente del tablero del Clúster Salud con archivos históricos F01/F02.

Las métricas visibles proceden de datos declarados salvo las visualizaciones
de prueba, que se identifican explícitamente como simuladas en la interfaz.
"""

from __future__ import annotations

import base64
import hashlib
import html as html_lib
import io
import json
import math
import os
import re
import sys
import textwrap
import unicodedata
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

# The desktop preview keeps its document libraries in a shared runtime.  Add
# that location only after the data stack is loaded so its NumPy build cannot
# shadow the application's own installation.
_shared_packages = Path(r"C:\Users\claud\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\Lib\site-packages")
if _shared_packages.exists():
    sys.path.append(str(_shared_packages))

from report import build_report, bar_chart, composition_bar


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
# Paleta de marca única (provista por la usuaria, uso exclusivo en todo el
# tablero): Azul oscuro, Azul principal, Azul cielo, Azul claro, Naranja,
# Gris. Para series con más de 5-6 categorías se generan tintes/sombras de
# estos mismos 6 tonos (helpers tint/shade) en vez de introducir colores
# nuevos, para no salirse de la paleta.
INK, PAPER = "#20252B", "#FFFFFF"  # neutros tipográficos (texto/fondo de página), no forman parte de la paleta de datos


def _blend(hex_a: str, hex_b: str, t: float) -> str:
    a = tuple(int(hex_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(hex_b[i:i + 2], 16) for i in (1, 3, 5))
    m = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return "#{:02X}{:02X}{:02X}".format(*m)


def tint(hex_color: str, t: float = 0.35) -> str:
    return _blend(hex_color, "#FFFFFF", t)


def shade(hex_color: str, t: float = 0.35) -> str:
    return _blend(hex_color, "#000000", t)


def value_gradient_colors(values, stops: list) -> list:
    # Reemplaza a color_continuous_scale para treemaps: Plotly/Kaleido
    # dibuja un rectángulo de fondo oscuro (no configurable via root_color,
    # marker_line ni paper/plot_bgcolor) cuando un treemap usa `color=`
    # numérico con coloraxis. Precalculando aquí el color exacto de cada
    # bloque y pasándolo como color_discrete_map se logra el mismo
    # degradado sin activar ese renderizado con fondo oscuro.
    vmin, vmax = min(values), max(values)
    span = (vmax - vmin) or 1
    out = []
    for v in values:
        t = (v - vmin) / span
        for i in range(len(stops) - 1):
            p0, c0 = stops[i]; p1, c1 = stops[i + 1]
            if t <= p1 or i == len(stops) - 2:
                local_t = 0 if p1 == p0 else min(max((t - p0) / (p1 - p0), 0), 1)
                out.append(_blend(c0, c1, local_t))
                break
    return out


# Los 6 tonos exactos que dio la usuaria, suavizados un 15% (mezclados hacia
# blanco) a pedido suyo para que no se vean tan "brillantes" sobre el fondo
# blanco de la hoja — equivale visualmente a un 15% de transparencia sobre
# ese fondo blanco.
_BRAND_NAVY, _BRAND_BLUE, _BRAND_SKY, _BRAND_PALE, _BRAND_ORANGE, _BRAND_GRAY = "#0B3C5D", "#0067A5", "#2D9CDB", "#EAF4FA", "#F26A21", "#8FA4B3"
NAVY, BLUE, SKY, PALE, ORANGE, GRAY = (tint(c, .07) for c in (_BRAND_NAVY, _BRAND_BLUE, _BRAND_SKY, _BRAND_PALE, _BRAND_ORANGE, _BRAND_GRAY))
# Alias históricos que quedan mapeados dentro de la paleta de 6 colores:
RED, YELLOW, TEAL, MUTED = ORANGE, SKY, SKY, GRAY
# Las series de sexo/edad también usan exclusivamente la paleta acordada.
MUJERES_RED = ORANGE
FOCUS_COLORS = {"Establecimiento de salud": ORANGE, "Salud pública": SKY}


# Paleta categórica extendida: los 5 tonos base (máxima distinción) seguidos
# de tintes/sombras de los mismos, para gráficas con varias categorías sin
# salir de la paleta de marca.
PALETTE_EXT = [
    NAVY, ORANGE, SKY, GRAY, BLUE,
    shade(NAVY, .35), tint(ORANGE, .35), tint(SKY, .35), shade(GRAY, .3), tint(BLUE, .35),
    shade(ORANGE, .35), tint(NAVY, .35), shade(SKY, .35), tint(GRAY, .35),
]
# Colores fijos por estado, tomados de la paleta institucional. El azul claro
# queda para fondos: en barras sobre blanco tendría poco contraste.
TERRITORY_COLORS = [_BRAND_NAVY, _BRAND_ORANGE, _BRAND_SKY, _BRAND_GRAY, _BRAND_BLUE]
STATE_COLORS = {
    "La Guaira": _BRAND_NAVY,
    "Miranda": _BRAND_ORANGE,
    "Distrito Capital": _BRAND_SKY,
    "Bolívar": _BRAND_GRAY,
}
TERRITORY_LEGEND_SHORT = {
    "Farmacia y dispensación de medicamentos": "Farmacia y medicamentos",
    "Gestión y manejo de residuos hospitalarios": "Gestión de residuos hospitalarios",
    "Sistema de Alerta Temprana y Vigilancia Epidemiológica": "Alerta temprana y vigilancia",
}
CALENDAR_YEAR = 2026
# Catálogo real de "Tipo de lugar de la intervención" (XLSForm F01/F02,
# lista `intervention_place_type`). El histórico no trae este dato; el
# simulacro asigna valores provisionales en memoria, sin alterar el CSV.
PUBLIC_PLACE_TYPES = [
    "Comunidad",
    "Alojamiento temporal / Albergue",
    "Establecimiento de salud",
    "Centro comunitario",
    "Escuela / Institución educativa",
    "Espacio público",
    "Brigada móvil / Punto móvil de atención",
    "Centro de protección",
    "Otro (especificar)",
]
# Catálogo real de la pregunta 4.1 del F02 ("Seleccione las áreas o
# servicios del establecimiento de salud que recibieron apoyo durante este
# período") — la comparten F01 (bloque 5.1, dato real en areas.csv) y F02,
# pero el histórico de reportes periódicos (resultados.csv) nunca capturó
# esta pregunta por período. Autorizado explícitamente por la usuaria:
# simular estos datos de forma determinística para este simulacro,
# mientras no exista un histórico real de reportes F02 con esta pregunta.
FACILITY_AREA_CATALOG = [
    "Agua, saneamiento e higiene",
    "Atención materna y obstétrica",
    "Atención neonatal",
    "Cirugía",
    "Consulta externa",
    "Emergencias",
    "Farmacia y dispensación de medicamentos",
    "Gestión y manejo de residuos hospitalarios",
    "Hospitalización",
    "Imagenología",
    "Laboratorio clínico",
    "Medicina interna",
    "Odontología",
    "Pediatría",
    "Rehabilitación",
    "Salud mental y apoyo psicosocial",
    "Salud sexual y reproductiva",
    "Vacunación",
    "Otro",
]
# Catálogo real de la pregunta 4.2 del F02 ("Seleccione el tipo o los tipos
# de apoyo proporcionados durante este período") — tomado directamente del
# formulario vigente (captura de pantalla provista por la usuaria), no del
# histórico de apoyos.csv: ese histórico solo trae 5 de estas 12 categorías
# (las que alguna organización ya registró alguna vez), pero el simulacro de
# esta pregunta a nivel de reporte periódico debe poder mostrar cualquiera
# de las 12 opciones reales del formulario, no solo el subconjunto que ya
# tiene datos de registro.
FACILITY_SUPPORT_TYPE_CATALOG = [
    "Acciones en agua, saneamiento e higiene (WASH)",
    "Acompañamiento técnico y capacitación",
    "Adecuaciones e infraestructura",
    "Despliegue de personal y atención médica directa",
    "Dotación de equipos médicos y biomédicos",
    "Dotación de insumos",
    "Donación de medicamentos",
    "Gestión de residuos de establecimientos de salud",
    "Salud Mental y Apoyo Psicosocial (SMAPS)",
    "Sistemas de información y vigilancia epidemiológica",
    "Soporte logístico, energía y cadena de frío",
    "Otro (especifique)",
]

def simulated_f02_facility_selections(report_ids):
    """Respuestas de demostración reproducibles, nunca datos declarados en F02."""
    area_weights = [7, 6, 3, 2, 9, 5, 5, 3, 3, 2, 4, 4, 2, 6, 2, 5, 3, 7, 1]
    support_weights = [7, 8, 4, 3, 4, 7, 6, 3, 4, 2, 3, 1]
    rows = []
    for report_id in sorted(set(report_ids), key=str):
        for kind, catalog, weights, maximum in (
            ("area", FACILITY_AREA_CATALOG, area_weights, 3),
            ("apoyo", FACILITY_SUPPORT_TYPE_CATALOG, support_weights, 2),
        ):
            pool = [name for name, weight in zip(catalog, weights) for _ in range(weight)]
            seed = hashlib.sha256(f"demo-f02-v1:{report_id}:{kind}".encode()).digest()
            count = 1 + seed[0] % maximum
            chosen = []
            for position in range(1, len(seed)):
                candidate = pool[seed[position] % len(pool)]
                if candidate not in chosen:
                    chosen.append(candidate)
                if len(chosen) == count:
                    break
            rows.extend({"id_reporte": report_id, "tipo": kind, "categoria": name}
                        for name in chosen)
    return pd.DataFrame(rows, columns=["id_reporte", "tipo", "categoria"])

# Equivalencias explícitas entre las cinco modalidades del archivo F01
# histórico y los rótulos del formulario vigente. No se asignan registros
# a categorías nuevas que el histórico no contiene.
FACILITY_SUPPORT_CURRENT_NAMES = {
    "Acciones en agua, saneamiento e higiene": "Acciones en agua, saneamiento e higiene (WASH)",
    "Acompañamiento técnico": "Acompañamiento técnico y capacitación",
}

st.set_page_config(page_title="Tablero de la Respuesta en Salud", page_icon="✚", layout="wide")


@st.cache_data
def load_municipio_geojson() -> dict:
    """Límites municipales reales de Venezuela (OCHA/HDX vía el repositorio anterior del tablero),
    para mapas de polígonos en vez de puntos. Se agrega "join_key" = "Estado | Municipio" a cada
    feature porque el nombre de municipio solo no es único (p. ej. "Libertador" existe en varios
    estados) — así se puede unir sin ambigüedad con los datos reales de estado+municipio."""
    with open(DATA / "geografia" / "limites_municipales.geojson", encoding="utf-8") as f:
        geo = json.load(f)
    for feature in geo["features"]:
        props = feature["properties"]
        props["join_key"] = f"{props['adm1_name']} | {props['adm2_name']}"
    return geo


@st.cache_data
def load_estado_geojson() -> dict:
    """Límites estadales reales de Venezuela, mismo origen que load_municipio_geojson().
    "join_key" = nombre del estado (ya es único, no hace falta componerlo)."""
    with open(DATA / "geografia" / "limites_estadales.geojson", encoding="utf-8") as f:
        geo = json.load(f)
    for feature in geo["features"]:
        feature["properties"]["join_key"] = feature["properties"]["adm1_name"]
    return geo


@st.cache_data
def load_parroquia_geojson(keys: tuple[str, ...]) -> dict:
    """Límites reales de las parroquias F01, guardados para uso sin red."""
    with open(DATA / "geografia" / "limites_parroquiales_f01.geojson", encoding="utf-8") as source:
        stored = json.load(source)
    geo = {"type": "FeatureCollection", "features": []}
    for feature in stored["features"]:
        props = feature["properties"]
        state_name = "La Guaira" if normalize_name(props.get("estado")) == "vargas" else props.get("estado", "")
        props["join_key"] = " | ".join(normalize_name(part) for part in (
            state_name, props.get("municipio", ""), props.get("parroquia", "")
        ))
        if props["join_key"] in keys:
            geo["features"].append(feature)
    return geo


def geojson_lat_lon_bounds(geo: dict, join_keys: set) -> tuple[float, float, float, float] | None:
    """Caja delimitadora (lat_lo, lat_hi, lon_lo, lon_hi) de los polígonos cuyo
    join_key está en join_keys — para encuadrar el mapa según los datos
    realmente presentes (p. ej. un solo estado costero chico como La Guaira)
    en vez de un centro/zoom fijo pensado para el país completo."""
    lats: list[float] = []; lons: list[float] = []

    def _walk(coords):
        if isinstance(coords[0], (int, float)):
            lons.append(coords[0]); lats.append(coords[1])
        else:
            for c in coords:
                _walk(c)

    for feature in geo["features"]:
        if feature["properties"]["join_key"] in join_keys:
            _walk(feature["geometry"]["coordinates"])
    if not lats:
        return None
    return min(lats), max(lats), min(lons), max(lons)


def leaflet_partner_map(geo: dict, areas: pd.DataFrame, level: str,
                        markers: pd.DataFrame | None = None,
                        background_geo: dict | None = None,
                        outline_geo: dict | None = None,
                        metric: str = "socios", metric_label: str = "Socios",
                        zoom_to_data: bool = False, marker_only: bool = False,
                        original_palette: bool = False) -> None:
    """Mapa territorial con la presentación Leaflet del tablero original."""
    columns = ["join_key", "nombre", "socios", "puntos"]
    if metric not in columns:
        columns.append(metric)
    values = areas[columns].to_dict("records")
    marker_columns = ["latitud", "longitud", "lugar", "organizacion", "estado", "municipio", "parroquia"]
    if markers is not None and "reportes_punto" in markers:
        marker_columns.append("reportes_punto")
    if markers is not None and "estado_reporte" in markers:
        marker_columns.append("estado_reporte")
    points = [] if markers is None else markers[marker_columns].fillna("").to_dict("records")
    maximum = max([int(row[metric]) for row in values] or [0])
    html_template = """
<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
<style>
html,body{margin:0;background:#f7f9fb;font-family:Arial,sans-serif}
#map{height:548px;width:100%;border-radius:13px;overflow:hidden}
.leaflet-control-zoom a{font-size:23px!important;width:32px!important;height:32px!important;line-height:30px!important}
.legend{background:rgba(255,255,255,.96);padding:13px 15px;border-radius:11px;
box-shadow:0 3px 12px #50607335;color:#29445f;min-width:120px;font-size:12px}
.legend b{font-size:13px}.legend .zero{display:flex;align-items:center;gap:6px;margin-top:9px}
.legend .zero i{height:14px;width:14px;background:__ZERO__;display:inline-block;border-radius:2px}
.legend .ramp{height:12px;border-radius:8px;margin:8px 0 5px;
background:linear-gradient(90deg,__PALE__,__SKY__,__BLUE__,__NAVY__)}
.legend .ends{display:flex;justify-content:space-between;color:#75879b}
.leaflet-popup-content{line-height:1.55;font-size:13px}
.facility-dot{width:13px;height:13px;background:__NAVY__;border:2px solid white;border-radius:50%;box-shadow:0 1px 5px #29445f55}
.facility-dot.unreported{background:#d64545}
.facility-cluster{display:flex;align-items:center;justify-content:center;border-radius:50%;background:__SKY__;border:3px solid white;color:#fff;font-weight:700;box-shadow:0 2px 8px #29445f66}
.facility-cluster.unreported{background:#d64545}
.status-legend{background:white;padding:10px 13px;border-radius:9px;box-shadow:0 2px 8px #29445f44;font-size:12px;color:#29445f}
.status-legend span{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:5px}
</style></head><body><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script>
const geo=__GEO__, background=__BG__, outlines=__OUTLINES__, rows=__ROWS__, markers=__MARKERS__, maxCount=__MAX__, level=__LEVEL__, metric=__METRIC__, metricLabel=__METRIC_LABEL__, autoFocus=__AUTO_FOCUS__, markerOnly=__MARKER_ONLY__;
const lookup=new Map(rows.map(r=>[r.join_key,r]));
const map=L.map('map',{scrollWheelZoom:true,zoomControl:true,zoomSnap:.1,minZoom:3,maxZoom:12});
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',
  {attribution:'Tiles © Esri — Esri, DeLorme, NAVTEQ',maxZoom:18}).addTo(map);
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function tone(n){if(markerOnly||!n)return '__ZERO__';let t=n/Math.max(1,maxCount);
 if(t<.2)return '__PALE__';if(t<.45)return '__SKY__';if(t<.75)return '__BLUE__';return '__NAVY__';}
if(background){L.geoJSON(background,{style:{color:'#fff',weight:1.1,fillColor:'__ZERO__',fillOpacity:.75}}).addTo(map);}
const areaLayer=L.geoJSON(geo,{style:f=>{let r=lookup.get(f.properties.join_key);
 return {color:'#fff',weight:1.15,fillColor:tone(r?.[metric]||0),fillOpacity:.92};},
 onEachFeature:(f,l)=>{let r=lookup.get(f.properties.join_key);
 let name=r?.nombre||f.properties.adm1_name||f.properties.adm2_name||'Territorio';
 l.bindPopup('<b>'+esc(name)+'</b><br>'+esc(level)+': '+esc(name)+
   '<br>'+esc(metricLabel)+': '+(r?.[metric]||0)+
   (metric==='puntos'?'':'<br>Puntos: '+(r?.puntos||0))+
   (metric==='socios'?'':'<br>Socios: '+(r?.socios||0)));
 l.on('mouseover',()=>l.setStyle({color:'__ORANGE__',weight:2.4}));
 l.on('mouseout',()=>l.setStyle({color:'#fff',weight:1.15}));}
}).addTo(map);
if(outlines){L.geoJSON(outlines,{interactive:false,style:{color:'#20252b',weight:2.1,fillOpacity:0,opacity:.95}}).addTo(map);}
const clusterLayer=markerOnly&&L.markerClusterGroup?L.markerClusterGroup({showCoverageOnHover:false,
 iconCreateFunction:c=>{const allUnreported=c.getAllChildMarkers().every(m=>m.options.reportStatus==='Sin reporte');
 return L.divIcon({html:'<div class="facility-cluster'+(allUnreported?' unreported':'')+'" style="width:36px;height:36px">'+c.getChildCount()+'</div>',className:'',iconSize:[42,42]});}}):null;
for(const p of markers){
 const point=markerOnly?L.marker([Number(p.latitud),Number(p.longitud)],{reportStatus:p.estado_reporte||'',icon:L.divIcon({html:'<div class="facility-dot'+(p.estado_reporte==='Sin reporte'?' unreported':'')+'"></div>',className:'',iconSize:[17,17]})}):
   L.circleMarker([Number(p.latitud),Number(p.longitud)],{radius:6.5,color:'#fff',weight:1.4,fillColor:'__NAVY__',fillOpacity:.95});
 point.bindPopup('<b>'+esc(p.lugar)+'</b><br>Socio: '+esc(p.organizacion)+
 '<br>Parroquia: '+esc(p.parroquia||'No reportada')+'<br>Municipio: '+esc(p.municipio)+
 '<br>Estado: '+esc(p.estado)+(p.estado_reporte?'<br>Estado del reporte: '+esc(p.estado_reporte):'')+
 (p.reportes_punto?'<br>Reportes: '+esc(p.reportes_punto):''));
 if(clusterLayer)clusterLayer.addLayer(point);else point.addTo(map);
}
if(clusterLayer)clusterLayer.addTo(map);
let activeBounds=L.latLngBounds([]);
if(markerOnly && markers.length){
 for(const p of markers)activeBounds.extend([Number(p.latitud),Number(p.longitud)]);
}else if(autoFocus){
 areaLayer.eachLayer(layer=>{
   const row=lookup.get(layer.feature?.properties?.join_key);
   if(row && Number(row.puntos)>0 && layer.getBounds)activeBounds.extend(layer.getBounds());
 });
}
if(autoFocus && activeBounds.isValid()){
 map.fitBounds(activeBounds,{padding:[32,32],maxZoom:9});
}else{
 map.fitBounds([[-1.6,-75.5],[13.3,-58.4]],{padding:[8,8]});
}
const legend=L.control({position:'bottomleft'});
legend.onAdd=()=>{const d=L.DomUtil.create('div','legend');
 d.innerHTML='<b>'+esc(metricLabel)+'</b><div class="zero"><i></i>0</div><div class="ramp"></div>'+
 '<div class="ends"><span>1</span><span>'+maxCount+'</span></div>';
 return d;};if(!markerOnly)legend.addTo(map);
if(markerOnly && markers.some(p=>p.estado_reporte)){
 const statusLegend=L.control({position:'bottomleft'});
 statusLegend.onAdd=()=>{const d=L.DomUtil.create('div','status-legend');
 d.innerHTML='<div><span style="background:__NAVY__"></span>Con reporte F02</div><div style="margin-top:5px"><span style="background:#d64545"></span>Sin reporte F02</div>';return d;};
 statusLegend.addTo(map);
}
</script></body></html>"""
    page=(html_template.replace("__GEO__",json.dumps(geo,ensure_ascii=False).replace("</","<\\/"))
                       .replace("__BG__",json.dumps(background_geo,ensure_ascii=False).replace("</","<\\/") if background_geo else "null")
                       .replace("__OUTLINES__",json.dumps(outline_geo,ensure_ascii=False).replace("</","<\\/") if outline_geo else "null")
                       .replace("__ROWS__",json.dumps(values,ensure_ascii=False))
                       .replace("__MARKERS__",json.dumps(points,ensure_ascii=False))
                       .replace("__MAX__",str(maximum))
                       .replace("__LEVEL__",json.dumps(level,ensure_ascii=False))
                       .replace("__METRIC__",json.dumps(metric))
                       .replace("__METRIC_LABEL__",json.dumps(metric_label,ensure_ascii=False))
                       .replace("__AUTO_FOCUS__","true" if zoom_to_data else "false")
                       .replace("__MARKER_ONLY__","true" if marker_only else "false")
                       .replace("__ZERO__","#e1e5eb" if original_palette else _BRAND_GRAY)
                       .replace("__PALE__","#d5e5f0" if original_palette else _BRAND_PALE)
                       .replace("__SKY__","#a8c9df" if original_palette else _BRAND_SKY)
                       .replace("__BLUE__","#648fb9" if original_palette else _BRAND_BLUE)
                       .replace("__NAVY__","#35679e" if original_palette else _BRAND_NAVY)
                       .replace("__ORANGE__",_BRAND_ORANGE))
    components.html(page,height=560,scrolling=False)


@st.cache_data
def load_data() -> dict[str, pd.DataFrame]:
    files = {
        "facilities": DATA / "establecimientos" / "establecimientos.csv",
        "facility_areas": DATA / "establecimientos" / "areas.csv",
        "supports": DATA / "establecimientos" / "apoyos.csv",
        "places": DATA / "cobertura" / "puntos_atencion.csv",
        "offered": DATA / "cobertura" / "acciones_ofertadas.csv",
        "reports": DATA / "acciones" / "reportes.csv",
        "results": DATA / "acciones" / "resultados.csv",
    }
    out = {k: pd.read_csv(v, encoding="utf-8-sig") for k, v in files.items()}
    for frame in out.values():
        for col in ("organizacion", "estado", "municipio"):
            if col in frame:
                frame[col] = frame[col].fillna("No indicado").astype(str)
                if col == "organizacion":
                    frame[col] = frame[col].map(canonical_org)
    for key in ("facilities", "reports", "results"):
        if "fecha_reporte" in out[key]:
            out[key]["fecha_reporte"] = pd.to_datetime(out[key]["fecha_reporte"], errors="coerce")
    for col in ("desde", "hasta"):
        out["places"][col] = pd.to_datetime(out["places"][col], errors="coerce")
    return out


def clean_service(value: object) -> str:
    text = str(value or "No indicado").strip()
    return re.sub(r"^\d+(?:\.\d+)*\s*", "", text)


def normalize_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return re.sub(r"[^a-z0-9]+", " ", "".join(c for c in text if not unicodedata.combining(c))).strip()


# El histórico trae la misma organización escrita de varias formas (mayúsculas,
# acentos, nombre corto vs. razón social completa), lo que inflaba los conteos
# de "socios" al contarlas como organizaciones distintas. Se normalizan a un
# único nombre canónico antes de cualquier conteo o gráfica.
ORG_CANONICAL = {
    "medicos sin fronteras": "Médicos Sin Fronteras",
    "oim": "OIM - Organización Internacional para las Migraciones",
    "oim organizacion internacional para las migraciones": "OIM - Organización Internacional para las Migraciones",
    "accion solidaria": "Acción Solidaria",
    "fundacion vive mas": "Fundación Vive Más (FVM)",
    "fundacion vive mas fvm": "Fundación Vive Más (FVM)",
    "fundaensalud": "FUNDAENSALUD (Fundación Educando en Salud)",
    "fundaensalud fundacion educando en salud": "FUNDAENSALUD (Fundación Educando en Salud)",
    "fondo de naciones unidas para la infancia": "UNICEF",
}


def canonical_org(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    return ORG_CANONICAL.get(normalize_name(text), text)


def classify_health_facility(name: object, reported_type: object) -> str:
    if pd.notna(reported_type) and str(reported_type).strip() and str(reported_type).lower() != "nan":
        historical_type=str(reported_type).strip()
        return {
            "Consultorio Médico Popular":"Consultorio Popular",
            "Hospital":"Hospital",
            "Otro":"Otro",
        }.get(historical_type,"Tipo histórico sin equivalencia")
    text = normalize_name(name)
    if "hospital" in text or "materno" in text: return "Hospital"
    if "consultorio" in text: return "Consultorio Popular"
    return "Tipo no reportado"


def area_from_indicator(code: object) -> str:
    m = re.match(r"(?:3|4)\.(\d+)", str(code))
    lookup = {
        "1": "Salud mental y apoyo psicosocial", "2": "Enfermedades no transmisibles",
        "3": "Enfermedades transmisibles, VIH e ITS", "4": "Salud materna y perinatal",
        "5": "Salud sexual y reproductiva", "6": "Violencia sexual", "7": "Salud nutricional",
        "8": "Inmunización", "9": "Lesiones y traumas", "10": "Discapacidad",
        "11": "Acciones institucionales", "12": "Atención primaria",
        "13": "Vigilancia epidemiológica", "14": "Acciones comunitarias", "15": "Gestión de casos",
    }
    return lookup.get(m.group(1), "Otra área") if m else "Otra área"


@st.cache_data
def prepare() -> dict[str, pd.DataFrame]:
    t = load_data()
    required_place_columns={"parroquia","donantes","tipo_organizacion","modalidad_implementacion","desde","hasta"}
    if not required_place_columns.issubset(t["places"].columns):
        t["places"]=pd.read_csv(DATA / "cobertura" / "puntos_atencion.csv",encoding="utf-8-sig")
        for col in ("desde","hasta"): t["places"][col]=pd.to_datetime(t["places"][col],errors="coerce")
        for col in ("organizacion","estado","municipio","parroquia"): t["places"][col]=t["places"][col].fillna("No reportada" if col=="parroquia" else "No indicado").astype(str)
        t["places"]["organizacion"]=t["places"]["organizacion"].map(canonical_org)

    # Vocabulario alineado con los formularios F01/F02 vigentes (XLSForm
    # F01_guia_enlace_simple_20260914 / F02_lista_organizaciones_completa_20260915).
    # El histórico usa categorías de los formularios viejos ("Nacional") y
    # tiene un error real donde una modalidad mixta queda como el texto de
    # ambas opciones concatenado; se remapea a las opciones reales vigentes.
    t["places"]["tipo_organizacion"] = t["places"]["tipo_organizacion"].replace(
        {"Nacional": "ONG Nacional", "Internacional": "ONG Internacional"}
    )

    t["places"]["modalidad_implementacion"] = t["places"]["modalidad_implementacion"].replace(
        {"Implementador directo Socio implementador": "Ambas"}
    )
    # La visualización utiliza las opciones exactas del F01 vigente. Cuando
    # el histórico no declara el tipo de lugar, se rellena SOLO en memoria
    # con un valor de prueba reproducible; una respuesta real siempre gana.
    if "tipo_lugar" not in t["places"]:
        t["places"]["tipo_lugar"] = pd.NA
    missing_place_type = (
        t["places"]["tipo_lugar"].isna() |
        t["places"]["tipo_lugar"].astype(str).str.strip().str.casefold().isin(
            {"", "nan", "no reportado", "no reportada"}
        )
    )
    t["places"]["tipo_lugar_simulado"] = missing_place_type
    missing_ids = sorted(t["places"].loc[missing_place_type, "id_servicio"].astype(str).unique())
    simulated_types = {
        point_id: PUBLIC_PLACE_TYPES[position % len(PUBLIC_PLACE_TYPES)]
        for position, point_id in enumerate(missing_ids)
    }
    t["places"].loc[missing_place_type, "tipo_lugar"] = (
        t["places"].loc[missing_place_type, "id_servicio"].astype(str).map(simulated_types)
    )
    # El F01 histórico tampoco incluye importes. Para probar la sección de
    # inversión, se genera un importe reproducible por punto únicamente en
    # memoria. Si un futuro F01 trae una cifra real, esta prevalece.
    declared_investment = pd.to_numeric(
        t["places"].get("inversion_usd",pd.Series(index=t["places"].index,dtype="float64")),
        errors="coerce",
    )
    def _test_investment(point_id: object) -> int:
        digest=hashlib.sha256(str(point_id).encode("utf-8")).hexdigest()
        return 5000+(int(digest[:8],16)%26)*1000
    t["places"]["inversion_simulada"] = declared_investment.isna()
    t["places"]["inversion_visual_usd"] = declared_investment.fillna(
        t["places"]["id_servicio"].map(_test_investment)
    )

    fac = t["facilities"].copy()
    fac["foco"] = "Establecimiento de salud"
    fac["lugar"] = fac["nombre_establecimiento"]
    fac["id_punto"] = "FAC-" + fac["id_establecimiento"].astype(str)
    fac["tipo_punto"] = fac.apply(lambda row: classify_health_facility(row["nombre_establecimiento"],row.get("tipo_establecimiento")),axis=1)
    fac["parroquia"] = "No reportada"

    rep = t["reports"].copy()
    facility_names = {normalize_name(v) for v in fac["nombre_establecimiento"].dropna()}
    health_terms = ("hospital", "ambulatorio", "consultorio", "clinica", "cdi ", "cpt ", "materno infantil", "maternidad")
    rep["foco"] = rep["nombre_sitio"].map(
        lambda v: "Establecimiento de salud"
        if normalize_name(v) in facility_names or any(term in f"{normalize_name(v)} " for term in health_terms)
        else "Salud pública"
    )
    facility_type_lookup = (
        fac.dropna(subset=["nombre_establecimiento"]).assign(_key=lambda x:x.nombre_establecimiento.map(normalize_name))
        .drop_duplicates("_key").set_index("_key")["tipo_establecimiento"].to_dict()
    )
    place_parish_lookup = (
        t["places"].dropna(subset=["nombre_sitio"]).assign(_key=lambda x:x.nombre_sitio.map(normalize_name))
        .drop_duplicates("_key").set_index("_key")["parroquia"].to_dict()
    )
    rep["tipo_punto"] = rep.apply(
        lambda row: classify_health_facility(
            row["nombre_sitio"], facility_type_lookup.get(normalize_name(row["nombre_sitio"]))
        )
        if row["foco"] == "Establecimiento de salud"
        else None,
        axis=1,
    )
    # F02 histórico no contiene tipo de lugar: conservar la ausencia, sin
    # asignar categorías que nadie declaró.
    rep.loc[rep["foco"].eq("Salud pública"), "tipo_punto"] = "No reportado"
    rep["lugar"] = rep["nombre_sitio"]
    rep["parroquia"] = rep["nombre_sitio"].map(lambda v: place_parish_lookup.get(normalize_name(v),"No reportada"))
    rep["parroquia"] = rep["parroquia"].fillna("No reportada")
    rep["id_punto"] = rep["organizacion"].astype(str) + " | " + rep["nombre_sitio"].astype(str)
    public_points = rep[rep.foco.eq("Salud pública")].sort_values("fecha_reporte").drop_duplicates("id_punto", keep="last")

    points = pd.concat([
        fac[["id_punto", "organizacion", "estado", "municipio", "parroquia", "lugar", "latitud", "longitud", "foco", "tipo_punto", "fecha_reporte"]],
        public_points[["id_punto", "organizacion", "estado", "municipio", "parroquia", "lugar", "latitud", "longitud", "foco", "tipo_punto", "fecha_reporte"]],
    ], ignore_index=True)
    points["latitud"] = pd.to_numeric(points["latitud"], errors="coerce")
    points["longitud"] = pd.to_numeric(points["longitud"], errors="coerce")
    # Los archivos históricos no contienen inversión; no fabricar montos.
    points["inversion_usd"] = pd.NA

    results = t["results"].copy()
    # Verificado contra el XLSForm real y vigente del F02
    # (F02_lista_organizaciones_completa_20260915.xlsx, hoja "survey"): la
    # sección 5 del formulario va de 5.1 a 5.15 y NO existe un bloque 5.11 —
    # sin embargo el histórico de resultados.csv sí trae un bloque "3.11.x"
    # ("profesionales de salud apoyados", "instituciones dotadas con
    # insumos", etc.) que no corresponde a ninguna pregunta real del
    # formulario actual. También hay dos códigos sueltos dentro de 5.8
    # Inmunización (3.8.2 "instituciones apoyadas en inmunización" y 3.8.3
    # "personas del talento humano apoyadas en inmunización") que tampoco
    # existen ahí — el bloque real 5.8 tiene un solo indicador (5.8.1). Se
    # excluyen estos códigos huérfanos para que ninguna gráfica los
    # presente como si fueran un indicador real del F02 vigente.
    _non_form_codes = {"3.8.2", "3.8.3"}
    results = results[~results["indicador_codigo"].isin(_non_form_codes) & ~results["indicador_codigo"].str.startswith("3.11.")]
    results["area"] = results["indicador_codigo"].map(area_from_indicator)
    results = results.merge(rep[["id_reporte", "organizacion", "estado", "municipio", "lugar", "fecha_reporte", "foco"]], on="id_reporte", how="left", suffixes=("", "_rep"))

    facility_services = t["facility_areas"].merge(fac[["id_establecimiento", "organizacion", "estado", "municipio", "lugar", "tipo_punto", "fecha_reporte"]], on="id_establecimiento", how="left")
    facility_services["servicio"] = facility_services["area_servicio"].map(clean_service)
    # Bloque 5.2 del F01 ("Seleccione el tipo o los tipos de apoyo que serán
    # proporcionados"): es una pregunta real distinta de 5.1 (áreas/servicios
    # que reciben apoyo) — datos reales de apoyos.csv, nunca antes usados en
    # el tablero.
    facility_supports = t["supports"].merge(fac[["id_establecimiento", "organizacion", "estado", "municipio", "lugar", "tipo_punto"]], on="id_establecimiento", how="left")
    facility_supports["tipo_apoyo"] = (facility_supports["tipo_apoyo"].map(clean_service)
                                      .replace(FACILITY_SUPPORT_CURRENT_NAMES))
    offered = t["offered"].merge(t["places"][["id_servicio", "organizacion", "estado", "municipio", "parroquia", "nombre_sitio", "desde", "hasta"]], on="id_servicio", how="left")
    offered["servicio"] = offered["servicio"].map(clean_service)
    offered["lugar"] = offered["nombre_sitio"]
    # Un registro por servicio/acción y lugar: es oferta declarada, no
    # cantidad de personas atendidas ni resultados periódicos del F02.
    facility_coverage = facility_services[["id_establecimiento", "organizacion", "estado", "municipio", "servicio", "fecha_reporte"]].copy()
    facility_coverage = facility_coverage.rename(columns={"id_establecimiento": "id_punto", "servicio": "tipo_accion"})
    facility_coverage["id_punto"] = "FAC-" + facility_coverage["id_punto"].astype(str)
    facility_coverage["foco"] = "Establecimiento de salud"
    public_coverage = offered[["organizacion", "estado", "municipio", "parroquia", "nombre_sitio", "servicio", "desde", "hasta"]].copy()
    public_coverage["id_punto"] = public_coverage["organizacion"].astype(str) + " | " + public_coverage["nombre_sitio"].astype(str)
    public_coverage = public_coverage.rename(columns={"servicio": "tipo_accion"})
    public_coverage["foco"] = "Salud pública"
    coverage_actions = pd.concat([facility_coverage, public_coverage], ignore_index=True)
    coverage_actions = coverage_actions.dropna(subset=["tipo_accion", "id_punto"])
    coverage_actions = coverage_actions[coverage_actions.tipo_accion.astype(str).str.strip().ne("")]

    donor_base = t["places"].copy()
    if "donantes" not in donor_base.columns:
        donor_base = pd.read_csv(DATA / "cobertura" / "puntos_atencion.csv", encoding="utf-8-sig")
    donor_sources = donor_base[["id_servicio","organizacion","estado","municipio","donantes"]].copy()
    donor_sources["donantes"] = donor_sources["donantes"].fillna("").astype(str).str.strip().replace({"":"Sin fuente reportada","nan":"Sin fuente reportada"})
    donor_sources["donantes"] = donor_sources["donantes"].str.split(r"\s*[,;]\s*",regex=True)
    donor_sources = donor_sources.explode("donantes",ignore_index=True)

    # Calendario ilustrativo de brigadas: las fechas posibles proceden de la
    # vigencia y los días de operación declarados en F01. Para evitar que
    # cada lunes, martes, etc. repita el mismo total semanal, se programa
    # un subconjunto determinista de esos puntos en cada fecha posible.
    # Son fechas simuladas, no actividades confirmadas por el formulario.
    year_start = pd.Timestamp(f"{CALENDAR_YEAR}-01-01")
    year_end = pd.Timestamp(f"{CALENDAR_YEAR}-12-31")
    weekday_cols = ["dia_lunes", "dia_martes", "dia_miercoles", "dia_jueves", "dia_viernes", "dia_sabado", "dia_domingo"]
    service_labels = {
        "serv_atencion_primaria": "Atención primaria",
        "serv_salud_mental_psicosocial": "Salud mental y apoyo psicosocial",
        "serv_enfermedades_no_transmisibles": "Enfermedades no transmisibles",
        "serv_enfermedades_transmisibles": "Enfermedades transmisibles, VIH e ITS",
        "serv_salud_materna_perinatal": "Salud materna y perinatal",
        "serv_prevencion_atencion_vih": "Prevención y atención VIH",
        "serv_salud_sexual_reproductiva": "Salud sexual y reproductiva",
        "serv_violencia_sexual": "Violencia sexual",
        "serv_salud_nutricional": "Salud nutricional",
        "serv_vacunacion": "Vacunación",
        "serv_alerta_temprana_vigilancia_epidemiologica": "Vigilancia epidemiológica",
        "serv_lesiones_traumas": "Lesiones y traumas",
        "serv_acciones_institucionales": "Acciones institucionales",
        "serv_acciones_comunitarias": "Acciones comunitarias",
        "serv_gestion_casos": "Gestión de casos",
    }

    vigencia_real = t["places"].copy()
    vigencia_real["desde"] = pd.to_datetime(vigencia_real["desde"], errors="coerce")
    vigencia_real["hasta"] = pd.to_datetime(vigencia_real["hasta"], errors="coerce")
    vigencia_real = vigencia_real.dropna(subset=["desde"])
    # Sin fecha de cierre declarada no se puede afirmar vigencia diaria.
    vigencia_real = vigencia_real.dropna(subset=["hasta"])
    vigencia_real["hasta_efectiva"] = vigencia_real["hasta"].clip(upper=year_end)

    calendar_rows = []
    for row in vigencia_real.itertuples():
        active_weekdays = {i for i, col in enumerate(weekday_cols) if getattr(row, col, 0) == 1}
        if not active_weekdays:
            continue
        activity = next((label for col, label in service_labels.items() if getattr(row, col, 0) == 1), "Brigada de salud pública")
        jornada = "Tarde" if getattr(row, "horario_tarde", 0) == 1 and getattr(row, "horario_manana", 0) != 1 else "Mañana"
        start = max(row.desde, year_start)
        end = min(row.hasta_efectiva, year_end)
        if start > end:
            continue
        for day in pd.date_range(start, end):
            if day.weekday() in active_weekdays:
                day_seed=hashlib.blake2s(day.strftime("%Y-%m-%d").encode(),digest_size=2).digest()
                point_seed=hashlib.blake2s(f"{row.id_servicio}|{day:%Y-%m-%d}".encode(),digest_size=2).digest()
                daily_share=.20+.75*(int.from_bytes(day_seed,"big")/65535)
                if int.from_bytes(point_seed,"big")/65535 > daily_share:
                    continue
                calendar_rows.append({
                    "id_punto": row.id_servicio, "organizacion": row.organizacion, "estado": row.estado,
                    "municipio": row.municipio, "parroquia": row.parroquia, "lugar": row.nombre_sitio,
                    "actividad": activity, "jornada": jornada, "fecha": day, "foco": "Salud pública",
                })
    sample = pd.DataFrame(calendar_rows, columns=["id_punto","organizacion","estado","municipio","parroquia","lugar","actividad","jornada","fecha","foco"])
    return {**t, "reports_prepared": rep, "points": points, "public_points": public_points, "results_joined": results,
            "facility_services_joined": facility_services, "facility_supports_joined": facility_supports, "offered_joined": offered, "coverage_actions": coverage_actions, "donor_sources": donor_sources,
            "calendar": sample}


def img_data(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii") if path.exists() else ""


def num(value: float | int) -> str:
    return f"{value:,.0f}".replace(",", ".")


def support_ranking_table(ranking: pd.DataFrame, *, max_height: int = 700) -> None:
    """Tabla legible con barras rojas; evita el color fijo del ProgressColumn de Streamlit."""
    frame = ranking[["Organización", "Establecimiento", "Tipología", "Apoyos recibidos"]].copy()
    maximum = max(1, int(frame["Apoyos recibidos"].max()))
    rows = []
    for row in frame.itertuples(index=False, name=None):
        org, establishment, typology, supports = row
        width = max(0, min(100, int(round(int(supports) / maximum * 100))))
        rows.append(
            "<tr>"
            f"<td>{html_lib.escape(str(org))}</td>"
            f"<td>{html_lib.escape(str(establishment))}</td>"
            f"<td>{html_lib.escape(str(typology))}</td>"
            "<td><div class='support-meter'>"
            f"<span style='width:{width}%'></span><b>{int(supports)}</b>"
            "</div></td></tr>"
        )
    height = min(max_height, 45 * len(frame) + 48)
    st.markdown(
        f"""
        <div class="support-table-wrap" style="max-height:{height}px">
          <table class="support-table">
            <thead><tr><th>Organización</th><th>Establecimiento</th><th>Tipología</th><th>Apoyos recibidos</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def complete_dashboard_export() -> None:
    """Abre la impresión del navegador para guardar toda la vista activa como PDF."""
    with st.container(key="print_export"):
        components.html(
            f"""
            <style>
              html,body{{margin:0;background:transparent;font-family:Arial,sans-serif}}
              button{{width:100%;border:0;border-radius:8px;padding:12px 16px;background:{ORANGE};color:white;
                      font-size:15px;font-weight:700;cursor:pointer}}
              button:hover{{background:{shade(ORANGE,.12)}}}
            </style>
            <button onclick="window.parent.focus(); window.parent.print();">Descargar infografía</button>
            """,
            height=48,
        )


def add_map_marker_outline(fig: go.Figure, frame: pd.DataFrame, size_col: str) -> None:
    # px.scatter_map / go.Scattermap no admiten marker.line (el círculo de
    # MapLibre no soporta borde): se simula un contorno fino agregando una
    # capa negra debajo, con el mismo sizeref/sizemode/sizemin que ya calculó
    # Plotly Express, apenas más grande en área (~1.2x → ~9% más de radio)
    # para que se vea como un anillo delgado y no un halo grueso.
    if fig.data and frame.size:
        ref_marker = fig.data[0].marker
        fig.add_trace(go.Scattermap(
            lat=frame["latitud"], lon=frame["longitud"],
            marker=dict(size=frame[size_col] * 1.2, sizeref=ref_marker.sizeref, sizemode=ref_marker.sizemode,
                        sizemin=ref_marker.sizemin, color="black", opacity=0.9),
            hoverinfo="skip", showlegend=False,
        ))
        fig.data = (fig.data[-1],) + fig.data[:-1]


def plot(fig: go.Figure, height: int = 420, key: str | None = None, margin: dict | None = None, is_map: bool = False) -> None:
    if not getattr(fig.layout.title, "text", None):
        fig.update_layout(title={"text": ""})
    # Ajuste centralizado de legibilidad: fuente más grande y margen derecho
    # suficiente para que las etiquetas "outside" de las barras no se
    # recorten contra el borde de la figura (cliponaxis=False evita que
    # Plotly las corte incluso cuando quedan cerca del máximo del eje).
    fig.update_traces(selector=dict(type="bar"), cliponaxis=False)
    # Las etiquetas de cada treemap conservan el tamaño específico de su
    # sección; sobrescribirlo aquí las reducía incluso después de ajustarlas.
    fig.update_traces(selector=dict(type="pie"), textfont=dict(size=13))
    fig.update_layout(height=height, margin=margin or (dict(l=0, r=0, t=0, b=44) if is_map else dict(l=16, r=54, t=18, b=18)), paper_bgcolor=PAPER,
                      plot_bgcolor=PAPER, font=dict(family="Arial", color=INK, size=13),
                      title_font=dict(family="Arial", color=NAVY, size=19), legend_title_text="")
    if is_map:
        # Leyenda horizontal debajo del mapa (no a la derecha): así el mapa
        # ocupa todo el ancho de la figura y el modebar (cámara/+/-/home/
        # pantalla completa), que Plotly siempre ubica arriba a la derecha
        # de TODA la figura, queda superpuesto sobre el mapa en sí — antes,
        # con la leyenda a la derecha, el modebar terminaba flotando sobre
        # esa columna en blanco en vez de sobre el mapa.
        fig.update_layout(legend=dict(font=dict(size=16), orientation="h", yanchor="top", y=-0.03, xanchor="left", x=0, tracegroupgap=28))
    else:
        fig.update_layout(legend=dict(font=dict(size=12)))
    fig.update_xaxes(gridcolor=tint(GRAY,.7), zeroline=False, automargin=True, tickfont=dict(size=12))
    fig.update_yaxes(gridcolor=tint(GRAY,.7), zeroline=False, automargin=True, tickfont=dict(size=13))
    if is_map:
        # La rueda del mouse permite acercar y alejar cuando el cursor está
        # sobre el mapa. Los íconos por defecto de Plotly son gris claro y casi no se notan
        # sobre el mapa — se fuerza un fondo blanco sólido y color oscuro
        # para que se vean claramente encima de las tejas del mapa.
        fig.update_layout(modebar=dict(bgcolor="rgba(255,255,255,0.95)", color=NAVY, activecolor=RED, orientation="h"))
        map_config = {
            "displayModeBar": True, "scrollZoom": True, "displaylogo": False,
            "modeBarButtonsToRemove": ["pan2d", "lasso2d", "select2d", "resetViewMapbox"],
        }
        st.plotly_chart(fig, width="stretch", config=map_config, key=key)
    else:
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=key)


def tile_name(name: str) -> str:
    # Conservar el nombre completo: los saltos de línea permiten leerlo
    # también en los bloques pequeños sin recurrir a siglas nuevas.
    return "<br>".join(textwrap.wrap(name, width=21, break_long_words=False))


def set_org_tile_labels(fig, min_size: int = 12, max_size: int = 18) -> None:
    # Mismo criterio que set_value_tile_labels: escala continua por raíz
    # cuadrada (no saltos discretos) acotada entre un mínimo y un máximo, así
    # ningún bloque queda con letras gigantes ni microscópicas. "name" ya
    # viene con saltos de línea de tile_name(), así que no se envuelve de
    # nuevo aquí.
    values=list(fig.data[0].values)
    vmin,vmax=min(values),max(values)
    span=(vmax-vmin) or 1
    labels=[]
    for name,value in zip(fig.data[0].labels,values):
        frac=((value-vmin)/span)**0.5
        size=round(min_size+frac*(max_size-min_size))
        labels.append(f'<span style="font-size:{size}px">{name}<br>{value:g}</span>')
    fig.update_traces(text=labels,texttemplate="%{text}",textfont_size=max_size,textposition="middle center",root_color="white")


def set_value_tile_labels(fig, unit: str, min_size: int = 13, max_size: int = 18, wrap_width: int = 15, value_first: bool = False) -> None:
    # Tamaño de letra proporcional al valor (raíz cuadrada, como el área del
    # bloque), acotado entre un mínimo y un máximo (rango angosto a
    # propósito: nada de letras gigantes en bloques grandes ni miscroscópicas
    # en los chicos — look ejecutivo, no "de escuela"). Los nombres largos se
    # envuelven en varias líneas ANTES de calcular/mostrar el texto (en vez
    # de dejar que Plotly los encoja para que quepan en una sola línea): así
    # dos bloques del mismo tamaño (mismo valor) siempre quedan con el mismo
    # tamaño de letra, sin importar si el nombre es corto o largo.
    values=list(fig.data[0].values)
    vmin,vmax=min(values),max(values)
    span=(vmax-vmin) or 1
    labels=[]
    for name,value in zip(fig.data[0].labels,values):
        frac=((value-vmin)/span)**0.5
        size=round(min_size+frac*(max_size-min_size))
        wrapped_name="<br>".join(textwrap.wrap(str(name),width=wrap_width,break_long_words=False)) or str(name)
        value_line=f"{value:,.0f} {unit}".strip()
        labels.append(f'<span style="font-size:{size}px">'+(f"<b>{value_line}</b><br>{wrapped_name}" if value_first else f"{wrapped_name}<br>{value_line}")+"</span>")
    fig.update_traces(text=labels,texttemplate="%{text}",textfont_size=max_size,textposition="middle center",
                       marker_line_color="white",marker_line_width=2,root_color="white")


def integer_colorbar_ticks(cmax: int, target_steps: int = 5) -> tuple[list[int], list[str]]:
    """Genera ticks enteros y parejos para una barra de color (0, 5, 10, 15…), en vez
    de forzar el máximo exacto como último tick suelto (p. ej. …,10,12,13)."""
    cmax = max(int(cmax), 1)
    raw_step = cmax / target_steps
    magnitude = 10 ** math.floor(math.log10(raw_step)) if raw_step >= 1 else 1
    step = next((c * magnitude for c in (1, 2, 5, 10) if c * magnitude >= raw_step), 10 * magnitude)
    step = max(1, int(step))
    top = (cmax // step + 1) * step
    vals = list(range(0, int(top) + 1, step))
    return vals, [str(v) for v in vals]


def metric(label: str, value: object, note: str) -> str:
    return f'<div class="metric"><strong>{value}</strong><span>{label}</span><small>{note}</small></div>'


def section_band(title: str, question: str = "") -> None:
    question_html = f"<span>{question}</span>" if question else ""
    st.markdown(f'<div class="section-band">{title}{question_html}</div>', unsafe_allow_html=True)


d = prepare()
fac, reports, results, points = d["facilities"], d["reports_prepared"], d["results_joined"], d["points"]

st.markdown(f"""
<style>
  .stApp{{background:{PAPER};color:{INK}}}.block-container{{max-width:1620px;padding-top:1rem;padding-bottom:3.5rem;padding-left:2rem;padding-right:2rem}}
  [data-testid="stSidebar"]{{background:{tint(GRAY,.88)};border-right:1px solid {tint(GRAY,.7)}}}
  [data-testid="stSidebar"] [data-testid="stSidebarUserContent"]{{padding-top:.55rem;padding-bottom:.4rem}}
  [data-testid="stSidebar"] [data-testid="stVerticalBlock"]{{gap:.45rem}}
  [data-testid="stSidebar"] h3{{font-size:1rem!important;margin:0 0 .1rem!important}}
  [data-testid="stSidebar"] [data-testid="stSelectbox"] label p,
  [data-testid="stSidebar"] [data-testid="stSlider"] label p{{font-size:.8rem!important;line-height:1.2!important}}
  [data-testid="stSidebar"] [data-testid="stSelectbox"] .react-aria-ComboBox [role="group"],
  [data-testid="stSidebar"] [data-baseweb="select"]{{min-height:40px!important;height:40px!important}}
  .logo-crop{{max-width:1660px;margin:0 auto 20px;padding:16px 22px;background:white;border:1px solid {tint(GRAY,.78)};border-radius:14px;box-shadow:0 3px 12px {NAVY}12}}
  .logo-crop img{{width:100%;height:auto;display:block;filter:contrast(1.02);}}
  .logo-strip-extra{{max-width:1660px;margin:0 auto 24px;padding:14px 22px;background:white;border:1px solid {tint(GRAY,.78)};border-radius:14px;box-shadow:0 3px 12px {NAVY}12;display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:16px 28px}}
  .logo-strip-extra img{{height:44px;width:auto;max-width:200px;object-fit:contain;filter:contrast(1.02)}}
  .hero{{background:linear-gradient(120deg,{NAVY},{BLUE});color:white;border-radius:16px;padding:16px 26px;margin:0 0 18px;box-shadow:0 8px 18px {NAVY}22}}
  .hero b{{font-size:.68rem;letter-spacing:.15em}}.hero h1{{font-size:1.65rem;margin:.28rem 0 .18rem;color:white}}.hero p{{font-size:.88rem;margin:0;color:{PALE}}}
  .hero small{{display:block;margin-top:.22rem;color:{tint(SKY,.72)};font-size:.76rem}}
  .module-head{{border-left:6px solid {ORANGE};background:white;padding:14px 18px;border-radius:8px;margin:18px 0}}
  .module-head h2{{margin:0;color:{NAVY};font-size:1.55rem}}.module-head p{{margin:4px 0 0;color:{MUTED}}}
  .section-band{{display:flex;align-items:center;flex-wrap:wrap;gap:.35rem .65rem;background:{PALE};border:1px solid {tint(SKY,.7)};border-left:5px solid {NAVY};color:{NAVY};border-radius:9px;padding:.6rem .85rem;margin:.9rem 0 .65rem;font-weight:800;font-size:.92rem;letter-spacing:.02em}}
  .section-band span{{color:{MUTED};font-weight:500;font-style:italic;font-size:.88rem;letter-spacing:0}}
  .metric-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:0;margin:16px 0 26px;background:white;border:1px solid #d8e1eb;border-radius:14px;box-shadow:0 8px 22px #1934520b;overflow:hidden;font-family:"Source Sans 3","Source Sans Pro",sans-serif}}
  .metric{{background:white;border:0;border-right:1px solid #e0e7ee;padding:14px 16px;min-height:94px;text-align:left;display:flex;flex-direction:column;min-width:0}}
  .metric:last-child{{border-right:0}}
  .metric span{{display:flex;align-items:center;justify-content:flex-start;min-height:0;text-transform:uppercase;font-size:.7rem;letter-spacing:.035em;color:#586777;font-weight:750;line-height:1.2}}
  .metric strong{{display:block;font-size:1.85rem;line-height:1.1;color:#4e70a8;font-weight:750;margin:0 0 8px;overflow-wrap:anywhere}}.metric small{{display:block;color:#728091;font-size:.75rem;line-height:1.25;margin-top:2px}}
  .facility-metrics .metric{{padding:12px 13px;min-height:94px}}
  .facility-metrics .metric strong{{font-size:1.65rem;line-height:1.1}}
  .facility-metrics .metric span{{font-size:.66rem;letter-spacing:.035em;line-height:1.2}}
  .facility-metrics .metric small{{font-size:.72rem;line-height:1.25}}
  .report-overview{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:0;margin:14px 0;background:white;border:1px solid #d8e1eb;border-radius:14px;box-shadow:0 8px 22px #1934520b;overflow:hidden;font-family:"Source Sans 3","Source Sans Pro",sans-serif}}
  .report-overview.report-people{{grid-template-columns:repeat(6,minmax(0,1fr))}}
  .report-overview .metric{{border:0;border-right:1px solid #e0e7ee;border-radius:0;min-height:94px;padding:12px 14px;text-align:left;font-family:inherit}}
  .report-overview .metric:last-child{{border-right:0}}
  .report-overview .metric strong{{color:#4e70a8;font-size:1.85rem;line-height:1.1;font-weight:750;margin:0 0 8px;white-space:nowrap}}
  .report-overview .metric span{{justify-content:flex-start;min-height:0;font-size:.7rem;line-height:1.2;letter-spacing:.035em;color:#586777;font-weight:750}}
  .report-overview .metric small{{color:#728091;font-size:.75rem;line-height:1.25}}
  .st-key-f02_action_tabs [role="radiogroup"]{{flex-wrap:wrap!important;gap:6px!important}}
  .st-key-f02_action_tabs [data-testid="stRadioOption"]{{margin-right:0!important}}
  .pilot{{background:{tint(ORANGE,.88)};border:1px solid {tint(ORANGE,.45)};border-radius:8px;padding:10px 13px;color:{shade(ORANGE,.55)};font-size:.85rem}}
  .excel-dl{{display:inline-flex;align-items:center;gap:6px;text-decoration:none;color:{NAVY};background:white;border:1px solid {tint(GRAY,.68)};border-radius:8px;padding:5px 11px;font-weight:700;font-size:.83rem;line-height:1.6}}
  .excel-dl:hover{{border-color:{BLUE};color:{BLUE}}}
  .excel-dl svg{{flex:none}}
  .sidebar-index-label{{color:{MUTED};font-size:.68rem;font-weight:800;text-transform:uppercase;letter-spacing:.045em;margin:12px 0 7px;white-space:nowrap}}
  .sidebar-index{{display:flex;flex-direction:column;gap:4px;margin:0 0 12px}}
  .sidebar-index a{{display:block;text-decoration:none;color:{NAVY};background:white;border:1px solid {tint(GRAY,.68)};border-radius:7px;padding:7px 10px;font-weight:650;font-size:.78rem;line-height:1.3}}
  .sidebar-index a:hover{{border-color:{BLUE};color:{BLUE}}}
  .sidebar-index a.active,.sidebar-index a[aria-current="location"]{{background:{NAVY};border-color:{NAVY};color:white}}
  .sidebar-daily{{background:white;border:1px solid {tint(GRAY,.68)};border-left:3px solid {ORANGE};border-radius:8px;padding:8px 10px;margin:5px 0 9px;font-size:.77rem;color:{NAVY}}}
  .sidebar-daily strong{{display:block;font-size:1.2rem;line-height:1.2}}
  .sidebar-daily small{{display:block;color:{MUTED};font-size:.7rem;line-height:1.3}}
  .ext-links{{display:flex;gap:8px;flex-wrap:wrap;margin:2px 0 20px}}
  .ext-links a{{text-decoration:none;color:{NAVY};background:white;border:1px solid {tint(GRAY,.68)};border-radius:999px;padding:7px 12px;font-weight:700;font-size:.82rem}}
  .ext-links a::after{{content:" ↗";color:{BLUE}}}
  .sidebar-nav{{display:flex;gap:7px;margin:10px 0 20px}}
  .sidebar-nav a{{flex:1;text-decoration:none;text-align:center;color:{NAVY};background:white;border:1px solid {tint(GRAY,.68)};border-radius:8px;padding:8px 6px;font-weight:700;font-size:.8rem}}
  .sidebar-nav a:hover{{border-color:{BLUE};color:{BLUE}}}
  [data-testid="stSidebar"] :is(.st-key-sidebar_update_f01,.st-key-sidebar_update_f02,.st-key-sidebar_calendar) a{{min-height:36px!important;padding:8px 6px!important;border:0!important;border-radius:8px!important;background:{BLUE}!important;color:white!important;font-size:.8rem!important;font-weight:700!important;text-decoration:none!important}}
  [data-testid="stSidebar"] .st-key-sidebar_calendar a{{background:{ORANGE}!important}}
  [data-testid="stSidebar"] :is(.st-key-sidebar_update_f01,.st-key-sidebar_update_f02,.st-key-sidebar_calendar) a:hover{{background:{NAVY}!important}}
  /* Controles de filtros: contraste uniforme en F01, F02 y la barra lateral. */
  .stApp [data-testid="stSelectbox"] .react-aria-ComboBox [role="group"],.stApp [data-testid="stMultiSelect"] [role="group"],.stApp [data-baseweb="select"]{{background:{PALE}!important;border:1px solid {NAVY}!important;border-radius:8px!important;box-shadow:none!important}}
  .stApp [data-testid="stSelectbox"] .react-aria-ComboBox [role="group"]:focus-within,.stApp [data-testid="stMultiSelect"] [role="group"]:focus-within,.stApp [data-baseweb="select"]:focus-within{{border-color:{BLUE}!important;box-shadow:0 0 0 2px {SKY}55!important}}
  .stApp [data-testid="stSelectbox"] .react-aria-ComboBox input,.stApp [data-testid="stSelectbox"] .react-aria-ComboBox button{{background:transparent!important;color:{NAVY}!important}}
  .stApp [data-testid="stSelectbox"] .react-aria-ComboBox button svg,.stApp [data-baseweb="select"] svg{{color:{NAVY}!important;fill:{NAVY}!important;width:20px!important;height:20px!important}}
  .stApp [data-testid="stDateInput"] [data-baseweb="input"],.stApp [data-testid="stTextInput"] [data-baseweb="input"],.stApp [data-testid="stNumberInput"] [data-baseweb="input"]{{background:{PALE}!important;border:1px solid {NAVY}!important;border-radius:8px!important}}
  .stApp [data-testid="stDateInput"] input,.stApp [data-testid="stTextInput"] input,.stApp [data-testid="stNumberInput"] input{{background:transparent!important;color:{INK}!important}}
  .stApp [data-testid="stRadioOption"]{{background:{PALE}!important;border:1px solid {GRAY}!important;border-radius:8px!important;padding:6px 11px!important;margin-right:6px!important;cursor:pointer}}
  .stApp [data-testid="stRadioOption"][data-selected="true"]{{background:{tint(SKY,.72)}!important;border-color:{BLUE}!important;box-shadow:none!important}}
  .st-key-calendar_view_mode [data-testid="stRadioOption"]{{background:{tint(ORANGE,.9)}!important;border-color:{tint(ORANGE,.35)}!important}}
  .st-key-calendar_view_mode [data-testid="stRadioOption"][data-selected="true"]{{background:{tint(ORANGE,.55)}!important;border-color:{ORANGE}!important}}
  .stApp [data-testid="stSegmentedControl"] button{{border:1px solid {NAVY}!important;background:{PALE}!important;color:{NAVY}!important;font-weight:700!important}}
  .stApp [data-testid="stSegmentedControl"] button[aria-pressed="true"]{{background:{BLUE}!important;color:white!important}}
  .st-key-territory_facility_service_f01,.st-key-territory_public_action_f01{{max-width:340px}}
  .st-key-territory_facility_service_f01 [data-baseweb="select"] svg,.st-key-territory_public_action_f01 [data-baseweb="select"] svg{{color:{BLUE}!important;fill:{BLUE}!important;width:22px!important;height:22px!important;stroke-width:3}}
  .st-key-territory_facility_partner_f01 label p,.st-key-territory_public_partner_f01 label p,.st-key-territory_facility_service_f01 label p,.st-key-territory_public_action_f01 label p{{font-weight:700!important}}
  .st-key-territory_facility_partner_f01 [data-baseweb="select"] div,.st-key-territory_public_partner_f01 [data-baseweb="select"] div,.st-key-territory_facility_service_f01 [data-baseweb="select"] div,.st-key-territory_public_action_f01 [data-baseweb="select"] div{{font-weight:700!important}}
  .section-kicker{{color:{RED};text-transform:uppercase;letter-spacing:.11em;font-size:.7rem;font-weight:800;margin-top:22px}}
  .section-rule{{border-top:1px solid {tint(GRAY,.55)};margin:24px 0 8px}}
  [data-testid="stRadio"] label p{{color:{INK}!important;font-weight:700}}
  h2,h3,h4{{color:#252b33}}
  .block-container h2{{font-size:1.35rem!important;line-height:1.3;font-weight:650}}
  .block-container h3{{font-size:1.12rem!important;line-height:1.25;font-weight:650}}
  .block-container h4{{font-size:.98rem!important;line-height:1.3;font-weight:650}}
  /* Acerca cada figura a su encabezado, sin comprimir los controles ni las tarjetas. */
  .block-container [data-testid="stPlotlyChart"]{{margin-top:-.45rem}}
  .block-container h3,.block-container h4{{margin-bottom:.12rem!important}}
  .block-container .module-head h2{{font-size:1.25rem!important}}
  .stTabs [data-baseweb="tab-list"]{{gap:1.2rem;border-bottom:1px solid {GRAY}}}
  .stTabs [aria-selected="true"]{{border-bottom:4px solid {RED}}}
  .support-table-wrap{{overflow:auto;border:1px solid {tint(GRAY,.72)};border-radius:10px;background:white}}
  .support-table{{width:100%;border-collapse:collapse;font-size:.88rem}}
  .support-table th{{position:sticky;top:0;z-index:1;background:{tint(GRAY,.9)};color:{MUTED};text-align:left;padding:11px 12px;border-bottom:1px solid {tint(GRAY,.68)};font-weight:600}}
  .support-table td{{padding:10px 12px;border-bottom:1px solid {tint(GRAY,.78)};vertical-align:middle}}
  .support-table th:nth-child(1),.support-table td:nth-child(1){{width:16%}}
  .support-table th:nth-child(2),.support-table td:nth-child(2){{width:34%}}
  .support-table th:nth-child(3),.support-table td:nth-child(3){{width:28%}}
  .support-table th:nth-child(4),.support-table td:nth-child(4){{width:22%}}
  .support-meter{{display:grid;grid-template-columns:1fr 32px;gap:9px;align-items:center;position:relative}}
  .support-meter::before{{content:"";grid-column:1;grid-row:1;height:10px;background:{tint(GRAY,.82)};border-radius:999px}}
  .support-meter span{{grid-column:1;grid-row:1;height:10px;background:{RED};border-radius:999px;min-width:0}}
  .support-meter b{{grid-column:2;font-weight:500;color:{INK};text-align:right}}
  @media print{{
    [data-testid="stSidebar"],header,[data-testid="stToolbar"],.stRadio,.stDownloadButton,.st-key-calendar_nav_btn,.st-key-print_export{{display:none!important}}
    .block-container{{max-width:none!important;padding:0!important}}
    .hero{{box-shadow:none}}
    [data-testid="stPlotlyChart"],.support-table-wrap,.metric-row,[data-testid="stDataFrame"]{{break-inside:avoid;page-break-inside:avoid}}
    .support-table-wrap{{max-height:none!important;overflow:visible!important}}
    .print-break{{break-before:page}}
  }}
  @media(max-width:1100px){{.report-overview,.report-overview.report-people{{grid-template-columns:repeat(3,minmax(0,1fr))}}.report-overview .metric{{border-bottom:1px solid #e0e7ee}}}}
  @media(max-width:900px){{.metric-row{{grid-template-columns:repeat(2,1fr)!important}}.metric-row .metric:nth-child(2n){{border-right:0}}.metric-row .metric{{border-bottom:1px solid #e0e7ee}}}}
  @media(max-width:650px){{.report-overview,.report-overview.report-people{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
</style>
""", unsafe_allow_html=True)

st.markdown('<div id="top"></div>', unsafe_allow_html=True)
logo = img_data(ROOT / "logos_formularios_hd.png")
if logo:
    st.markdown(f'<div class="logo-crop"><img src="data:image/png;base64,{logo}" alt="Logotipos de las organizaciones participantes"></div>', unsafe_allow_html=True)
# Socios nuevos: cualquier archivo de imagen que se coloque en esta carpeta
# aparece aquí automáticamente en el próximo reinicio, sin tocar código ni
# el banner compuesto de arriba.
_logos_dir = ROOT / "logos_socios"
_mime_by_ext = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".svg": "image/svg+xml"}
_extra_logo_files = sorted(p for p in _logos_dir.glob("*") if p.suffix.lower() in _mime_by_ext) if _logos_dir.exists() else []
_extra_logo_imgs = [f'<img src="data:{_mime_by_ext[p.suffix.lower()]};base64,{img_data(p)}" alt="{p.stem}">' for p in _extra_logo_files if img_data(p)]
if _extra_logo_imgs:
    st.markdown(f'<div class="logo-strip-extra">{"".join(_extra_logo_imgs)}</div>', unsafe_allow_html=True)
report_dates = pd.concat([
    pd.to_datetime(fac["fecha_reporte"], errors="coerce"),
    pd.to_datetime(reports["fecha_reporte"], errors="coerce"),
]).dropna()
date_floor, date_ceiling = (
    (report_dates.min().date(), report_dates.max().date())
    if not report_dates.empty
    else (pd.Timestamp.today().date(), pd.Timestamp.today().date())
)
hero_today_label = pd.Timestamp.today().strftime("%d/%m/%Y")
st.markdown(f'<div class="hero"><b>OPS/OMS · CLÚSTER DE SALUD · VENEZUELA</b><h1>Tablero de la Respuesta en Salud</h1><p>Presencia operativa, programación de actividades y resultados reportados</p><small>Fecha de consulta: {hero_today_label} · Periodo de reportes: {date_floor.strftime("%d/%m/%Y")} – {date_ceiling.strftime("%d/%m/%Y")}</small></div>', unsafe_allow_html=True)

F01_MODULE="Registro de oferta · Clúster de salud"
F01_FACILITY_MODULE="Capacidad de apoyo a establecimientos de salud"
F01_MODULE_OPTIONS=[F01_MODULE,F01_FACILITY_MODULE]
F02_ACTIONS_MODULE="Reporte de acciones en salud pública"
F02_FACILITY_MODULE="Reportes de acciones implementadas"
F02_MODULE_OPTIONS=[F02_ACTIONS_MODULE,F02_FACILITY_MODULE]
if "active_module" not in st.session_state:
    requested_view=st.query_params.get("vista")
    st.session_state.active_module = (F02_ACTIONS_MODULE if requested_view=="reportes"
                                      else F02_FACILITY_MODULE if requested_view=="reportes-establecimientos"
                                      else F01_FACILITY_MODULE if requested_view=="establecimientos"
                                      else F01_FACILITY_MODULE if requested_view=="capacidad"
                                      else F01_MODULE)
elif st.session_state.active_module=="Reportes periódicos de acciones (F02)":
    st.session_state.active_module=F02_ACTIONS_MODULE
elif st.session_state.active_module=="2 · Capacidad de apoyo a establecimientos":
    st.session_state.active_module=F01_FACILITY_MODULE
elif st.session_state.active_module=="Apoyo a establecimientos de salud":
    st.session_state.active_module=F01_FACILITY_MODULE
elif st.session_state.active_module=="Acciones en establecimientos de salud":
    st.session_state.active_module=F02_FACILITY_MODULE
elif st.session_state.active_module=="Reportes de apoyo a establecimientos de salud":
    st.session_state.active_module=F02_FACILITY_MODULE
elif st.session_state.active_module not in F01_MODULE_OPTIONS+F02_MODULE_OPTIONS:
    st.session_state.active_module=F01_MODULE

def _go_to_f01_module() -> None:
    st.session_state.active_module = F01_MODULE
    st.query_params["vista"] = "cluster"

def _go_to_f01_facility_module() -> None:
    st.session_state.active_module = F01_FACILITY_MODULE
    st.query_params["vista"] = "capacidad"

def _go_to_f02_actions_module() -> None:
    st.session_state.active_module = F02_ACTIONS_MODULE
    st.query_params["vista"] = "reportes"

def _go_to_f02_facility_module() -> None:
    st.session_state.active_module = F02_FACILITY_MODULE
    st.query_params["vista"] = "reportes-establecimientos"

with st.container(key="nav_routes"):
    st.markdown('<div class="nav-route-label"><span>01</span> Salud pública</div>', unsafe_allow_html=True)
    nav_1,arrow_1,nav_1_report=st.columns([1,0.09,1],vertical_alignment="center")
    with nav_1:
        st.button(F01_MODULE,on_click=_go_to_f01_module,key="f01_module_btn",width="stretch")
    with arrow_1:
        st.markdown('<div class="nav-flow-arrow" aria-hidden="true">→</div>',unsafe_allow_html=True)
    with nav_1_report:
        st.button(F02_ACTIONS_MODULE,on_click=_go_to_f02_actions_module,key="f02_actions_btn",width="stretch")
    st.markdown('<div class="nav-route-label nav-route-label-second"><span>02</span> Establecimientos de salud</div>', unsafe_allow_html=True)
    nav_2,arrow_2,nav_2_report=st.columns([1,0.09,1],vertical_alignment="center")
    with nav_2:
        st.button(F01_FACILITY_MODULE,on_click=_go_to_f01_facility_module,key="f01_facility_btn",width="stretch")
    with arrow_2:
        st.markdown('<div class="nav-flow-arrow" aria-hidden="true">→</div>',unsafe_allow_html=True)
    with nav_2_report:
        st.button(F02_FACILITY_MODULE,on_click=_go_to_f02_facility_module,key="f02_facility_btn",width="stretch")
module = st.session_state.active_module
st.markdown(f"""<style>
.st-key-nav_routes{{padding:0;margin:5px 0 3px}}
.st-key-nav_routes [data-testid="stVerticalBlock"]{{gap:.12rem}}
.nav-route-label{{color:{NAVY};font-size:.72rem;font-weight:750;letter-spacing:.035em;margin:0 0 3px}}
.nav-route-label-second{{margin-top:7px}}
.nav-route-label span{{display:inline-flex;align-items:center;justify-content:center;min-width:23px;height:23px;margin-right:6px;border-radius:5px;background:{tint(SKY,.78)};color:{NAVY};font-size:.69rem}}
.nav-flow-arrow{{font-size:1.25rem;font-weight:750;color:{BLUE};text-align:center;margin-top:-2px}}
.st-key-f01_module_btn button,.st-key-f01_facility_btn button,.st-key-f02_actions_btn button,.st-key-f02_facility_btn button{{background:{PALE}!important;color:{NAVY}!important;border:1px solid {tint(GRAY,.38)}!important;border-radius:7px!important;font-size:.875rem!important;font-weight:650!important;line-height:1.3!important;min-height:40px;box-shadow:none!important}}
.st-key-f01_module_btn button:hover,.st-key-f01_facility_btn button:hover,.st-key-f02_actions_btn button:hover,.st-key-f02_facility_btn button:hover{{border-color:{BLUE}!important;background:{tint(SKY,.84)}!important}}
.st-key-f01_module_btn button:focus-visible,.st-key-f01_facility_btn button:focus-visible,.st-key-f02_actions_btn button:focus-visible,.st-key-f02_facility_btn button:focus-visible{{outline:2px solid {BLUE}!important;outline-offset:2px}}
.st-key-f01_module_btn button{{background:{tint(SKY,.71) if module==F01_MODULE else PALE}!important;border-color:{BLUE if module==F01_MODULE else tint(GRAY,.38)}!important}}
.st-key-f01_facility_btn button{{background:{tint(SKY,.71) if module==F01_FACILITY_MODULE else PALE}!important;border-color:{BLUE if module==F01_FACILITY_MODULE else tint(GRAY,.38)}!important}}
.st-key-f02_actions_btn button{{background:{tint(SKY,.71) if module==F02_ACTIONS_MODULE else PALE}!important;border-color:{BLUE if module==F02_ACTIONS_MODULE else tint(GRAY,.38)}!important}}
.st-key-f02_facility_btn button{{background:{tint(SKY,.71) if module==F02_FACILITY_MODULE else PALE}!important;border-color:{BLUE if module==F02_FACILITY_MODULE else tint(GRAY,.38)}!important}}
</style>""",unsafe_allow_html=True)

states = sorted(set(points["estado"].dropna()) | set(reports["estado"].dropna()))
municipios = sorted(set(points["municipio"].dropna()) | set(reports["municipio"].dropna()))
orgs = sorted(set(points["organizacion"].dropna()) | set(reports["organizacion"].dropna()))
filters_panel = st.sidebar.expander("Filtros", expanded=False)
# Orden: jerarquía geográfica, socio y rango de fecha.
state = filters_panel.selectbox("Estado", ["Todos"] + states)
municipio_filter = filters_panel.selectbox("Municipio", ["Todos"] + municipios)
def render_calendar(section_figures: dict[str, list[go.Figure]]) -> None:
    st.markdown(f'<div id="calendario-f01" class="section-kicker" style="color:{ORANGE}">02 · Calendario de brigadas</div>',unsafe_allow_html=True)
    cal=filt(d["calendar"],False); cal=cal[cal.foco.eq("Salud pública")]
    st.markdown(f'<h2 style="color:{ORANGE}">Calendario de brigadas de salud pública · {CALENDAR_YEAR}</h2>',unsafe_allow_html=True)
    st.caption("Programación ilustrativa simulada a partir de la vigencia y los días declarados en F01. Las fechas y cantidades de brigadas no son actividades confirmadas.")
    # Fuera de un expander a propósito: el clic para seleccionar un día
    # (on_select) dejaba de funcionar cuando este calendario quedaba anidado
    # dentro de un st.expander.
    cal_f=filt(cal,False)
    if cal_f.empty:
        st.info("No hay brigadas vigentes para los filtros seleccionados (solo se muestran puntos con fecha de inicio real declarada en el F01).")
    else:
        # Mantener una escala común entre las vistas: seis actividades no
        # deben verse tan intensas como veintidós al cambiar de mes.
        calendar_color_max=max(int(cal_f.groupby("fecha").size().max()),1)
        month_abbr_es={1:"Ene",2:"Feb",3:"Mar",4:"Abr",5:"May",6:"Jun",7:"Jul",8:"Ago",9:"Sep",10:"Oct",11:"Nov",12:"Dic"}
        weekday_abbr_es=["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"]
    
        def _fmt_date_es(value: pd.Timestamp) -> str:
            return f"{value.day:02d} {month_abbr_es[value.month]} {value.year}"
    
        def _shift_pick(key: str, options: list, delta: int) -> None:
            current=st.session_state.get(key, options[0])
            idx=options.index(current) if current in options else 0
            st.session_state[key]=options[min(max(idx+delta, 0), len(options)-1)]
    
        def _calendar_heatmap(grid_df: pd.DataFrame, x_col: str, y_col: str, x_order: list, y_order: list, cmax: int, height: int, key: str, show_text: bool = True, hide_yaxis: bool = False):
            z=grid_df.pivot(index=y_col, columns=x_col, values="actividades").reindex(index=y_order, columns=x_order)
            iso=grid_df.pivot(index=y_col, columns=x_col, values="fecha_iso").reindex(index=y_order, columns=x_order)
            tickvals,ticktext=integer_colorbar_ticks(cmax)
            xi=list(range(len(x_order))); yi=list(range(len(y_order)))
            # go.Heatmap no dispara eventos de selección de clic en
            # Streamlit (on_select solo funciona con scatter/bar/histogram),
            # por eso "seleccionar una fecha" nunca abría el detalle. Se
            # dibuja la cuadrícula con marcadores cuadrados de go.Scatter en
            # su lugar: mismo aspecto visual (color por intensidad), pero sí
            # es clicable.
            cell_px=max(24,min(68,int(700/max(len(x_order),1)),int((height-80)/max(len(y_order),1))))
            cell_xs,cell_ys,cell_colors,cell_cds,cell_counts=[],[],[],[],[]
            for r in range(len(y_order)):
                for c in range(len(x_order)):
                    zv=z.values[r][c]
                    if pd.isna(zv):
                        continue
                    cell_xs.append(c); cell_ys.append(r); cell_colors.append(zv)
                    cell_cds.append(iso.values[r][c]); cell_counts.append(str(int(zv)))
            fig=go.Figure()
            # Franja de fin de semana (sáb/dom) sombreada detrás de la
            # cuadrícula, como en un calendario real — un detalle sutil que
            # ayuda a ubicarse de un vistazo, no solo por el encabezado.
            weekend_cols=[i for i,t in enumerate(x_order) if t in ("Sáb","Dom")]
            for wc in weekend_cols:
                fig.add_shape(type="rect", x0=wc-0.5, x1=wc+0.5, y0=-0.6, y1=len(y_order)-0.4,
                              fillcolor=tint(GRAY,.75), opacity=0.5, line_width=0, layer="below")
            # Escala continua común: 0 queda casi blanco y los valores
            # intermedios no se saturan antes del máximo del período.
            fig.add_trace(go.Scatter(
                x=cell_xs, y=cell_ys, mode="markers",
                marker=dict(symbol="square", size=cell_px, color=cell_colors, cmin=0, cmax=cmax,
                            colorscale=[[0, tint(ORANGE, .94)], [0.25, tint(ORANGE, .72)], [0.5, tint(ORANGE, .38)], [0.75, ORANGE], [1, shade(ORANGE, .24)]], line=dict(width=3, color=PAPER),
                            colorbar=dict(title=dict(text="Actividades",font=dict(size=13,color=MUTED)), thickness=16, tickfont=dict(size=13), tickmode="array", tickvals=tickvals, ticktext=ticktext, outlinewidth=0)),
                customdata=cell_cds, text=cell_counts,
                hovertemplate="%{customdata}<br>Actividades: %{text}<extra></extra>",
                showlegend=False,
            ))
            if show_text:
                day=grid_df.pivot(index=y_col, columns=x_col, values="dia").reindex(index=y_order, columns=x_order)
                xs,day_ys,count_ys,cds,day_text,count_text=[],[],[],[],[],[]
                for r in range(len(y_order)):
                    for c in range(len(x_order)):
                        dv=day.values[r][c]
                        if pd.isna(dv):
                            continue
                        cv=z.values[r][c]
                        xs.append(c); day_ys.append(r+0.34); count_ys.append(r-0.08)
                        cds.append(iso.values[r][c])
                        day_text.append(f"<b>{int(dv)}</b>")
                        count_text.append(str(int(cv)) if pd.notna(cv) and cv>0 else "")
                fig.add_trace(go.Scatter(x=xs, y=day_ys, mode="text", text=day_text,
                                          textfont=dict(size=10, color="#7D2638"), customdata=cds,
                                          hoverinfo="skip", showlegend=False))
                fig.add_trace(go.Scatter(x=xs, y=count_ys, mode="text", text=count_text,
                                          textfont=dict(size=24, color=INK), customdata=cds,
                                          hoverinfo="skip", showlegend=False))
            fig.update_xaxes(tickmode="array", tickvals=xi, ticktext=[f"<b>{t}</b>" for t in x_order], side="top", title=None, showgrid=False, zeroline=False, tickfont=dict(size=13,color=MUTED), range=[-0.6,len(x_order)-0.4])
            fig.update_yaxes(tickmode="array", tickvals=yi, ticktext=[f"<b>{t}</b>" for t in y_order], showgrid=False, zeroline=False, tickfont=dict(size=13,color=MUTED), range=[len(y_order)-0.4,-0.6])
            if hide_yaxis:
                fig.update_yaxes(visible=False)
            fig.update_layout(height=height, margin=dict(l=16,r=16,t=48,b=8), paper_bgcolor=PAPER, plot_bgcolor=PAPER, font=dict(family="Arial", color=INK, size=13))
            event=st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=key, on_select="rerun", selection_mode="points")
            return fig, event
    
        def _calendar_export_button(scope_df: pd.DataFrame, label: str) -> None:
            export_cols=[c for c in ["fecha","organizacion","estado","municipio","parroquia","lugar","actividad","jornada"] if c in scope_df.columns]
            export_df=scope_df[export_cols].sort_values("fecha").copy()
            export_df["fecha"]=pd.to_datetime(export_df["fecha"]).dt.strftime("%d/%m/%Y")
            export_df=export_df.rename(columns={"fecha":"Fecha","organizacion":"Organización","estado":"Estado","municipio":"Municipio","parroquia":"Parroquia","lugar":"Lugar","actividad":"Actividad","jornada":"Jornada"})
            excel_buffer=io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                export_df.to_excel(writer, index=False, sheet_name="Actividades")
            excel_b64=base64.b64encode(excel_buffer.getvalue()).decode("ascii")
            _, right_col=st.columns([3,2])
            with right_col:
                st.markdown(
                    '<div style="text-align:right">'
                    f'<a class="excel-dl" href="data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,{excel_b64}" download="{label}.xlsx">'
                    '<svg width="15" height="15" viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">'
                    '<rect width="16" height="16" rx="3" fill="#1D6F42"/>'
                    '<path d="M4.5 4.5L11.5 11.5M11.5 4.5L4.5 11.5" stroke="white" stroke-width="1.6" stroke-linecap="round"/>'
                    '</svg>Descargar el calendario</a></div>',
                    unsafe_allow_html=True,
                )
    
        calendar_view=st.radio("Vista del calendario",["Anual","Mensual","Semanal","Diario"],horizontal=True,key="calendar_view_mode",index=1)
        selected_date=None
        selection_points=[]
    
        if calendar_view=="Anual":
            _calendar_export_button(cal_f, f"calendario_brigadas_{CALENDAR_YEAR}_anual")
            grid=cal_f.copy(); grid["month_period"]=grid.fecha.dt.to_period("M"); grid["dia"]=grid.fecha.dt.day
            months=sorted(grid["month_period"].unique()) or list(pd.period_range(f"{CALENDAR_YEAR}-01", f"{CALENDAR_YEAR}-12", freq="M"))
            cell_rows=[]
            for m in months:
                month_label=month_abbr_es[m.month]
                for dday in range(1, m.days_in_month+1):
                    date_val=pd.Timestamp(year=m.year, month=m.month, day=dday)
                    count=int(grid.loc[grid.month_period.eq(m) & grid.dia.eq(dday)].shape[0])
                    cell_rows.append({"mes":month_label,"dia":dday,"fecha_iso":date_val.strftime("%Y-%m-%d"),"actividades":count})
            grid_df=pd.DataFrame(cell_rows)
            month_order=[month_abbr_es[m.month] for m in months]
            day_order=list(range(1,32))
            cmax=calendar_color_max
            fig,event=_calendar_heatmap(grid_df,"dia","mes",day_order,month_order,cmax,min(480,90+30*len(months)),"calendar_grid_annual",show_text=False)
            section_figures.setdefault("Calendarios", []).append(fig)
            st.caption(f"Cada fila es un mes y cada columna un día del mes; el color indica cuántos puntos tienen una brigada vigente ese día (pase el mouse para ver el número exacto). Haga clic en un día para ver el detalle.")
            selection_points=event.selection.points if event and event.selection else []
            if selection_points:
                selected_date=pd.Timestamp(selection_points[0]["customdata"])
            active_days=sorted(grid_df.loc[grid_df.actividades.gt(0), "fecha_iso"].unique())
            day_labels={iso: _fmt_date_es(pd.Timestamp(iso)) for iso in active_days}
            picked_iso=st.selectbox("O elija una fecha con actividad",options=["— Seleccionar —", *active_days],format_func=lambda iso: day_labels.get(iso, iso),key="calendar_date_picker_annual")
            if selected_date is None and picked_iso != "— Seleccionar —":
                selected_date=pd.Timestamp(picked_iso)
    
        elif calendar_view=="Mensual":
            all_months=list(pd.period_range(f"{CALENDAR_YEAR}-01", f"{CALENDAR_YEAR}-12", freq="M"))
            months_with_activity=set(cal_f.fecha.dt.to_period("M").unique())
            month_labels={m: f"{month_abbr_es[m.month]} {m.year}" for m in all_months}
            # Por defecto se abre en el mes actual (no en el primero con
            # actividad): es el calendario de "ahora", no un archivo histórico.
            today_month=pd.Timestamp.today().to_period("M")
            default_month_index=next((i for i,m in enumerate(all_months) if m==today_month), next((i for i,m in enumerate(all_months) if m in months_with_activity), 0))
            nav_l,nav_mid,nav_r=st.columns([1,6,1])
            with nav_l: st.button("◀",key="cal_month_prev",on_click=_shift_pick,args=("calendar_month_picker",all_months,-1))
            with nav_r: st.button("▶",key="cal_month_next",on_click=_shift_pick,args=("calendar_month_picker",all_months,1))
            with nav_mid:
                picked_month=st.selectbox(
                    "Mes",options=all_months,
                    format_func=lambda m: month_labels[m]+(" · con actividad" if m in months_with_activity else ""),
                    index=default_month_index,key="calendar_month_picker",
                )
            days_in_month=picked_month.days_in_month
            first_weekday=pd.Timestamp(year=picked_month.year,month=picked_month.month,day=1).weekday()
            month_rows=cal_f[cal_f.fecha.dt.to_period("M").eq(picked_month)]
            _calendar_export_button(month_rows, f"calendario_brigadas_{picked_month.strftime('%Y_%m')}")
            month_counts=month_rows.groupby(month_rows.fecha.dt.day).size()
            cell_rows=[]
            for dday in range(1, days_in_month+1):
                date_val=pd.Timestamp(year=picked_month.year,month=picked_month.month,day=dday)
                week_of_month=(dday-1+first_weekday)//7
                count=int(month_counts.get(dday,0))
                cell_rows.append({"dia":dday,"weekday":weekday_abbr_es[date_val.weekday()],"semana":f"Semana {week_of_month+1}","semana_idx":week_of_month,"fecha_iso":date_val.strftime("%Y-%m-%d"),"actividades":count})
            grid_df=pd.DataFrame(cell_rows)
            n_weeks=int(grid_df["semana_idx"].max())+1
            week_order=[f"Semana {i+1}" for i in range(n_weeks)]
            cmax=calendar_color_max
            fig,event=_calendar_heatmap(grid_df,"weekday","semana",weekday_abbr_es,week_order,cmax,min(420,130+56*n_weeks),"calendar_grid",hide_yaxis=True)
            section_figures.setdefault("Calendarios", []).append(fig)
            st.caption("El número grande es la cantidad de puntos con una brigada vigente ese día (el chico, el día del mes). Haga clic en un día para ver el detalle automáticamente.")
            selection_points=event.selection.points if event and event.selection else []
            if selection_points:
                selected_date=pd.Timestamp(selection_points[0]["customdata"])
    
            active_days=sorted(grid_df.loc[grid_df.actividades.gt(0), "fecha_iso"].unique())
            day_labels={iso: _fmt_date_es(pd.Timestamp(iso)) for iso in active_days}
            picked_iso=st.selectbox(
                "O elija una fecha con actividad de este mes",
                options=["— Seleccionar —", *active_days],
                format_func=lambda iso: day_labels.get(iso, iso),
                key="calendar_date_picker_month",
            )
            if selected_date is None and picked_iso != "— Seleccionar —":
                selected_date=pd.Timestamp(picked_iso)
    
        elif calendar_view=="Semanal":
            weeks=list(pd.period_range(start=f"{CALENDAR_YEAR}-01-01", end=f"{CALENDAR_YEAR}-12-31", freq="W-SUN"))
            week_labels={w: f"{w.start_time.strftime('%d/%m')} – {w.end_time.strftime('%d/%m')}" for w in weeks}
            cal_week_period=cal_f.fecha.dt.to_period("W-SUN")
            weeks_with_activity=set(cal_week_period.unique())
            # Por defecto, la semana actual (no la primera con actividad).
            today_week=pd.Timestamp.today().to_period("W-SUN")
            default_index=next((i for i,w in enumerate(weeks) if w==today_week), next((i for i,w in enumerate(weeks) if w in weeks_with_activity), 0))
            nav_l,nav_mid,nav_r=st.columns([1,6,1])
            with nav_l: st.button("◀",key="cal_week_prev",on_click=_shift_pick,args=("calendar_week_picker",weeks,-1))
            with nav_r: st.button("▶",key="cal_week_next",on_click=_shift_pick,args=("calendar_week_picker",weeks,1))
            with nav_mid:
                picked_week=st.selectbox(
                    "Semana",options=weeks,
                    format_func=lambda w: week_labels[w]+(" · con actividad" if w in weeks_with_activity else ""),
                    index=default_index,key="calendar_week_picker",
                )
            week_days=pd.date_range(picked_week.start_time.normalize(), picked_week.end_time.normalize())
            week_rows=cal_f[cal_week_period.eq(picked_week)]
            _calendar_export_button(week_rows, f"calendario_brigadas_semana_{picked_week.start_time.strftime('%Y%m%d')}")
            week_counts=week_rows.groupby(week_rows.fecha.dt.date).size()
            week_df=pd.DataFrame({"fecha": week_days})
            week_df["dia"]=week_df.fecha.dt.day
            week_df["dia_semana"]=[weekday_abbr_es[dt.weekday()] for dt in week_df.fecha]
            week_df["actividades"]=[int(week_counts.get(dt.date(),0)) for dt in week_df.fecha]
            week_df["fecha_iso"]=week_df.fecha.dt.strftime("%Y-%m-%d")
            week_df["fila"]="Actividades"
            cmax=calendar_color_max
            fig,event=_calendar_heatmap(week_df,"dia_semana","fila",weekday_abbr_es,["Actividades"],cmax,240,"calendar_week_grid",hide_yaxis=True)
            section_figures.setdefault("Calendarios", []).append(fig)
            st.caption(f"Semana del {week_labels[picked_week]}. El número grande es la cantidad de puntos con una brigada vigente ese día (el chico, el día del mes). Haga clic en un día para ver el detalle.")
            selection_points=event.selection.points if event and event.selection else []
            default_day_index=int(week_df["actividades"].to_numpy().argmax()) if not week_df.empty else 0
            picked_day=st.selectbox("O elija un día de la semana",options=list(week_df.fecha),format_func=_fmt_date_es,index=default_day_index,key="calendar_day_picker_week")
            if selection_points:
                selected_date=pd.Timestamp(selection_points[0]["customdata"])
            else:
                selected_date=picked_day
    
        else:  # Diario
            all_days=list(pd.date_range(f"{CALENDAR_YEAR}-01-01", f"{CALENDAR_YEAR}-12-31"))
            active_days_ts=sorted(cal_f.fecha.unique())
            # Por defecto, el día de hoy (no el primero con actividad).
            today_norm=pd.Timestamp.today().normalize()
            if today_norm in all_days:
                default_index=all_days.index(today_norm)
            elif active_days_ts:
                default_index=all_days.index(pd.Timestamp(active_days_ts[0]))
            else:
                default_index=0
            nav_l,nav_mid,nav_r=st.columns([1,6,1])
            with nav_l: st.button("◀",key="cal_day_prev",on_click=_shift_pick,args=("calendar_day_picker",all_days,-1))
            with nav_r: st.button("▶",key="cal_day_next",on_click=_shift_pick,args=("calendar_day_picker",all_days,1))
            with nav_mid:
                selected_date=st.selectbox("Seleccione un día",options=all_days,format_func=_fmt_date_es,index=default_index,key="calendar_day_picker")
            _calendar_export_button(cal_f[cal_f.fecha.eq(selected_date)], f"calendario_brigadas_{selected_date.strftime('%Y_%m_%d')}")
    
        if selected_date is not None:
            detail=cal_f[cal_f.fecha.eq(selected_date)]
            if calendar_view=="Diario":
                day_cards=[
                    ("Actividades",num(len(detail)),""),
                    ("Organizaciones",num(detail.organizacion.nunique()),""),
                    ("Lugares",num(detail.lugar.nunique()),""),
                    ("Estados",num(detail.estado.nunique()),""),
                ]
                st.markdown('<div class="metric-row" style="grid-template-columns:repeat(4,1fr)">'+''.join(metric(*x) for x in day_cards)+'</div>',unsafe_allow_html=True)
            st.markdown(f"#### Actividades del {_fmt_date_es(selected_date)}")
            if detail.empty:
                st.info("No hay actividades registradas para esta fecha con los filtros actuales.")
            else:
                st.dataframe(
                    detail[["organizacion","estado","municipio","lugar","actividad","jornada"]].rename(columns={"organizacion":"Organización","estado":"Estado","municipio":"Municipio","lugar":"Lugar","actividad":"Actividad","jornada":"Jornada"}),
                    hide_index=True, width="stretch",
                )
        else:
            st.info("Haga clic en un día del calendario para ver el detalle de organizaciones y actividades planificadas.")
    
    
    
focus = "Establecimiento de salud" if module in (F01_FACILITY_MODULE,F02_FACILITY_MODULE) else "Salud pública"
partner = filters_panel.selectbox("Socio", ["Todos"] + orgs, key="sidebar_socio")
default_start = max(date_floor, date_ceiling - timedelta(days=30))
date_start, date_end = filters_panel.slider(
    "Rango de fecha del reporte",
    min_value=date_floor,
    max_value=date_ceiling,
    value=(default_start, date_ceiling),
    format="DD/MM/YYYY",
)
today = pd.Timestamp.today().normalize()
def _today_count(frame: pd.DataFrame, id_column: str) -> int:
    rows = frame[pd.to_datetime(frame["fecha_reporte"], errors="coerce").dt.normalize().eq(today)]
    if state != "Todos": rows = rows[rows.estado.eq(state)]
    if municipio_filter != "Todos": rows = rows[rows.municipio.eq(municipio_filter)]
    if partner != "Todos": rows = rows[rows.organizacion.eq(partner)]
    return int(rows[id_column].nunique())

today_f01 = _today_count(fac, "id_establecimiento")
today_f02 = _today_count(reports, "id_reporte")
st.sidebar.markdown(
    f'<div class="sidebar-daily"><strong>{today_f01 + today_f02}</strong> registros hoy · {today.strftime("%d/%m/%Y")}'
    f'<small>F01 establecimientos: {today_f01} · F02 reportes: {today_f02}</small></div>',
    unsafe_allow_html=True,
)

def render_sidebar_index(links: list[tuple[str, str]]) -> None:
    items = "".join(f'<a href="#{anchor}" data-section="{anchor}">{label}</a>' for anchor, label in links)
    index_title={
        F01_MODULE:"Registro de oferta",
        F02_ACTIONS_MODULE:"Reporte de acciones",
        F01_FACILITY_MODULE:"Capacidad de apoyo",
        F02_FACILITY_MODULE:"Acciones implementadas",
    }.get(module,module)
    st.sidebar.markdown(
        f'<div class="sidebar-index-label">Índice · {index_title}</div><nav class="sidebar-index" aria-label="Secciones del tablero">{items}</nav>',
        unsafe_allow_html=True,
    )

nav_buttons = '<div class="sidebar-nav"><a href="#top">Inicio</a></div>'
st.sidebar.markdown(nav_buttons, unsafe_allow_html=True)
st.sidebar.link_button("Actualizar datos F01 ↗", "https://ee.kobotoolbox.org/x/gjyvWedn", key="sidebar_update_f01", width="stretch")
st.sidebar.link_button("Actualizar datos F02 ↗", "https://ee.kobotoolbox.org/x/pQqj0V1d", key="sidebar_update_f02", width="stretch")
st.sidebar.link_button("Calendario de brigadas ↓", "?vista=cluster#calendario-f01", key="sidebar_calendar", width="stretch")

def filt(frame: pd.DataFrame, has_focus: bool = False) -> pd.DataFrame:
    out=frame.copy()
    if state != "Todos" and "estado" in out: out=out[out["estado"].eq(state)]
    if municipio_filter != "Todos" and "municipio" in out: out=out[out["municipio"].eq(municipio_filter)]
    if partner != "Todos" and "organizacion" in out: out=out[out["organizacion"].eq(partner)]
    if has_focus and focus != "Ambos" and "foco" in out: out=out[out["foco"].eq(focus)]
    if "fecha_reporte" in out:
        report_day = pd.to_datetime(out["fecha_reporte"], errors="coerce")
        out = out[report_day.isna() | ((report_day >= pd.Timestamp(date_start)) & (report_day <= pd.Timestamp(date_end)))]
    return out


if module in F01_MODULE_OPTIONS:
    is_facility_capacity_view=module==F01_FACILITY_MODULE
    section_figures: dict[str, list[go.Figure]] = {}
    p=filt(points,True)
    # Universo de socios: el registro de organizaciones (con modalidad,
    # donantes y personal) es la fuente correcta para "Socios", no el listado
    # de puntos de intervención (que solo cubre presencia física). Se calcula
    # una sola vez aquí para que la tarjeta resumen y la sección "Socios" usen
    # siempre el mismo número. "Vigente" ahora se evalúa contra el rango de
    # la barra de fecha (no contra "hoy" fijo): así el número de socios se
    # mueve junto con el resto de las gráficas al angostar o ampliar el
    # rango, en vez de quedar fijo mientras todo lo demás sí cambia.
    window_start,window_end=pd.Timestamp(date_start),pd.Timestamp(date_end)
    staffing=filt(d["places"].copy()); staffing=staffing[(staffing.desde.isna()|staffing.desde.le(window_end))&(staffing.hasta.isna()|staffing.hasta.ge(window_start))]; all_staff_cols=[c for c in staffing.columns if c.startswith("personal_")]
    active_places=staffing
    f01_report_dates=pd.concat([pd.to_datetime(d["facilities"].fecha_reporte,errors="coerce"),pd.to_datetime(d["reports_prepared"].fecha_reporte,errors="coerce")]).dropna()
    first_f01=f01_report_dates.min().strftime("%d/%m/%Y"); last_f01=f01_report_dates.max().strftime("%d/%m/%Y")
    section_band("SOCIOS Y APOYOS DEL CLÚSTER SALUD" if module==F01_MODULE else "CAPACIDAD DE APOYO A ESTABLECIMIENTOS DE SALUD · F01", "Registro de la oferta de acciones de salud pública de los socios." if module==F01_MODULE else "Portafolio de capacidades declaradas en F01. En la vista de reportes se muestra solo como contexto; no equivale a acciones realizadas.")
    panorama_link="01 · Panorama de la oferta vigente" if module==F01_MODULE else "01 · Panorama del apoyo institucional"
    section_links = [("panorama-f01", panorama_link)]
    if module==F01_MODULE:
        section_links.append(("calendario-f01", "02 · Calendario de brigadas"))
    section_links.append(("territorio-f01", "03 · Alcance territorial" if module==F01_MODULE else "02 · Alcance territorial"))
    section_links.append(("socios-f01", "04 · Socios y lugares de intervención" if module==F01_MODULE else "03 · Socios"))
    if is_facility_capacity_view:
        section_links.append(("apoyos-establecimientos-f01", "04 · Apoyo a establecimientos de salud"))
    if module!=F02_FACILITY_MODULE:
        section_links.append(("inversion-f01", "05 · Inversión"))
    section_links.append(("imprimir-infografia-del-tablero", "Descargar infografía ↓"))
    render_sidebar_index(section_links)
    professional_total=pd.to_numeric(staffing[all_staff_cols].stack(),errors="coerce").sum()
    if module==F01_MODULE:
        parish_count=active_places.loc[active_places.parroquia.ne("No reportada"),"parroquia"].nunique()
        cards=[
            ("Estados",num(active_places.estado.nunique()),"con presencia reportada"),
            ("Municipios",num(active_places.municipio.nunique()),"con oferta activa"),
            ("Parroquias",num(parish_count),"con ubicación reportada"),
            ("Socios",num(active_places.organizacion.nunique()),"organizaciones activas"),
            ("Puntos",num(active_places.id_servicio.nunique()),"puntos de atención"),
            ("Total profesionales reportados",num(professional_total),"en los puntos vigentes"),
            ("Inversión simulada",f"USD {num(active_places.inversion_visual_usd.sum())}","datos de prueba, no declarados"),
        ]
    else:
        facility_registry=p[p.foco.eq("Establecimiento de salud")].copy()
        facility_ids=set(facility_registry.id_punto.astype(str).str.replace("FAC-","",regex=False))
        facility_areas=filt(d["facility_services_joined"])
        facility_areas=facility_areas[facility_areas.id_establecimiento.astype(str).isin(facility_ids)]
        facility_supports=filt(d["facility_supports_joined"])
        facility_supports=facility_supports[facility_supports.id_establecimiento.astype(str).isin(facility_ids)]
        reported_parishes=facility_registry.parroquia.dropna()
        reported_parishes=reported_parishes[~reported_parishes.isin(["No reportada","No reportado",""])]
        cards=[
            ("Establecimientos",num(facility_registry.id_punto.nunique()),"registros únicos F01"),
            ("Organizaciones",num(facility_registry.organizacion.nunique()),"instituciones que reportan"),
            ("Estados",num(facility_registry.estado.nunique()),"con establecimientos"),
            ("Municipios",num(facility_registry.municipio.nunique()),"con establecimientos"),
            ("Parroquias",num(reported_parishes.nunique()) if not reported_parishes.empty else "—","sin dato declarado" if reported_parishes.empty else "con ubicación declarada"),
            ("Tipos de establecimiento",num(facility_registry.tipo_punto.nunique()),"categorías registradas"),
            ("Áreas o servicios",num(facility_areas.servicio.nunique()),"que reciben apoyo"),
            ("Tipos de apoyo",num(facility_supports.tipo_apoyo.nunique()),"modalidades utilizadas"),
        ]
    st.markdown('<div id="panorama-f01" class="section-kicker">01 · Panorama de la oferta vigente</div>' if module==F01_MODULE else '<div id="panorama-f01" class="section-kicker">01 · Panorama del apoyo institucional</div>',unsafe_allow_html=True)
    metric_class="metric-row facility-metrics" if is_facility_capacity_view else "metric-row"
    st.markdown(f'<div class="{metric_class}" style="grid-template-columns:repeat({len(cards)},1fr)">'+''.join(metric(*x) for x in cards)+'</div>',unsafe_allow_html=True)
    if is_facility_capacity_view:
        support_totals=(facility_supports.groupby("tipo_apoyo").id_establecimiento.nunique()
                        .sort_values(ascending=False).head(5))
        if not support_totals.empty:
            support_cards=[(name,num(count),"establecimientos") for name,count in support_totals.items()]
            st.markdown(f'<div class="metric-row facility-metrics" style="grid-template-columns:repeat({len(support_cards)},1fr)">'+
                        ''.join(metric(*item) for item in support_cards)+'</div>',unsafe_allow_html=True)
        st.caption("Los tipos de apoyo son de selección múltiple. Un establecimiento puede aparecer en varias categorías.")
    coverage=filt(d["coverage_actions"],True)
    public_rows=coverage.foco.eq("Salud pública")
    active_public=coverage.desde.isna() | coverage.desde.le(window_end)
    active_public &= coverage.hasta.isna() | coverage.hasta.ge(window_start)
    coverage=coverage[~public_rows | active_public].copy()
    selected_coverage=coverage
    if module==F01_MODULE:
        render_calendar(section_figures)
    territory_number="03" if module==F01_MODULE else "02"
    st.markdown(f'<div id="territorio-f01" class="section-kicker">{territory_number} · Alcance territorial</div>',unsafe_allow_html=True)
    if selected_coverage.empty:
        st.info("No hay tipos de acción registrados para los filtros seleccionados.")
    else:
        st.caption("Distribución de los registros del foco seleccionado. Compare la oferta por estado, municipio o parroquia." if module==F01_MODULE else "Distribución de los registros del foco seleccionado. Consulte los totales por estado o municipio, o seleccione Parroquia para ver los puntos georreferenciados.")
        territorial=selected_coverage.copy()
        territorial_foci = (
            [("Salud pública","Áreas temáticas en las que la organización realiza acciones de salud pública")]
            if module==F01_MODULE else
            [("Establecimiento de salud","Áreas o servicios del establecimiento de salud que reciben apoyo")]
        )
        for focus_name,heading in territorial_foci:
            focus_points=territorial[territorial.foco.eq(focus_name)].copy()
            is_facility=focus_name=="Establecimiento de salud"
            focus_key="facility" if is_facility else "public"
            if not is_facility:
                st.markdown("<h2>Mapa de cobertura de socios</h2>",unsafe_allow_html=True)
            if not is_facility and not st.session_state.get("public_state_legend_default_applied"):
                st.session_state["territory_public_level_f01"]="Estado"
                st.session_state["public_state_legend_default_applied"]=True
            if is_facility:
                st.markdown("### Establecimientos con y sin reporte periódico" if module==F02_FACILITY_MODULE else "### Puntos de apoyo registrados")
            map_level=("Estado" if is_facility else st.radio(
                "Nivel del análisis territorial",
                ["Estado","Municipio","Parroquia"],horizontal=True,
                index=0,key=f"territory_{focus_key}_level_f01",
            ))
            territory_level="Municipio" if map_level=="Parroquia" else map_level
            partner_choice="Todos"
            if is_facility:
                partner_choice=st.selectbox(
                    "Socio",["Todos"]+sorted(focus_points.organizacion.dropna().unique().tolist()),
                    key=f"territory_{focus_key}_partner_f01",
                )
            if territory_level=="Estado":
                focus_points["territorio"]=focus_points.estado
                municipality_colors={}
            else:
                focus_points["territorio"]=focus_points.municipio+" ("+focus_points.estado+")"
                municipality_colors={name:PALETTE_EXT[i % len(PALETTE_EXT)]
                                     for i,name in enumerate(sorted(focus_points.territorio.dropna().unique()))}
            if partner_choice!="Todos":
                focus_points=focus_points[focus_points.organizacion.eq(partner_choice)].copy()
            focus_points=focus_points.drop_duplicates(["territorio","tipo_accion","id_punto","organizacion"])
            subset=focus_points
            all_label="Todos los servicios" if is_facility else "Todas las acciones"
            choice=all_label
            if is_facility:
                choice=st.selectbox(
                    "Explorar servicios ofertados" if is_facility else "Explorar acciones de salud pública",
                    [all_label]+sorted(subset.tipo_accion.dropna().unique()),
                    key="territory_facility_service_f01" if is_facility else "territory_public_action_f01",
                    format_func=(lambda name: "Otro" if is_facility and str(name).startswith("Otro:") else name),
                )
            if choice!=all_label:
                subset=subset[subset.tipo_accion.eq(choice)]
            if subset.empty:
                st.info("No hay registros de este foco con los filtros seleccionados.")
                continue
            # Cada fila es un servicio o una acción. El denominador usa los
            # puntos del foco con Socio aplicado, pero no cambia al alternar
            # Estado/Municipio ni al seleccionar un único servicio o acción.
            base_total=focus_points.id_punto.nunique()
            with st.container(border=True) if is_facility else st.container():
                if is_facility and module==F02_FACILITY_MODULE:
                    report_rows=filt(reports,True)
                    facility_pool=filt(points,True)
                    facility_pool=facility_pool[facility_pool.foco.eq("Establecimiento de salud")].copy().drop_duplicates("id_punto")
                    facility_pool=facility_pool[facility_pool.id_punto.isin(set(subset.id_punto))]
                    facility_pool["_match_key"]=list(zip(facility_pool.organizacion.map(normalize_name),facility_pool.lugar.map(normalize_name)))
                    report_keys=set(zip(report_rows.organizacion.map(normalize_name),report_rows.nombre_sitio.map(normalize_name)))
                    facility_pool["estado_reporte"]=facility_pool._match_key.map(lambda key:"Con reporte" if key in report_keys else "Sin reporte")
                    facility_pool["reportes_punto"]=facility_pool._match_key.map(
                        report_rows.assign(_match_key=list(zip(report_rows.organizacion.map(normalize_name),report_rows.nombre_sitio.map(normalize_name))))
                        .groupby("_match_key").id_reporte.nunique().to_dict()).fillna(0).astype(int)
                    mapped_facilities=facility_pool.dropna(subset=["latitud","longitud"]).copy()
                    mapped_facilities=mapped_facilities[mapped_facilities.latitud.between(-2,14)&mapped_facilities.longitud.between(-76,-58)]
                    if mapped_facilities.empty:
                        st.info("No hay coordenadas válidas de establecimientos registrados para esta selección.")
                    else:
                        geo=load_estado_geojson()
                        areas=pd.DataFrame([{"join_key":feature["properties"]["join_key"],
                                             "nombre":feature["properties"]["adm1_name"],"socios":0,"puntos":0}
                                            for feature in geo["features"]])
                        leaflet_partner_map(geo,areas,"Estado",markers=mapped_facilities,
                                            metric="puntos",metric_label="Establecimientos",
                                            zoom_to_data=True,marker_only=True)
                        reported_count=int(mapped_facilities.estado_reporte.eq("Con reporte").sum())
                        st.caption(f"{reported_count} establecimientos con reporte F02 y {len(mapped_facilities)-reported_count} sin reporte en el período filtrado. Azul: con reporte; rojo: sin reporte. La comparación utiliza organización y nombre del establecimiento.")
                elif map_level=="Parroquia":
                    if not is_facility:
                        parish_source=subset.dropna(subset=["parroquia"]).copy()
                        parish_source=parish_source[parish_source.parroquia.astype(str).str.strip().ne("")]
                        parish_source=parish_source[parish_source.parroquia.ne("No reportada")]
                        if parish_source.empty:
                            st.info("Esta selección no contiene parroquias declaradas en F01.")
                        else:
                            parish_source["join_key"]=parish_source.apply(
                                lambda row:" | ".join(normalize_name(part) for part in
                                    (row.estado,row.municipio,row.parroquia)),axis=1)
                            parish_counts=parish_source.groupby("join_key",as_index=False).agg(
                                socios=("organizacion","nunique"),puntos=("id_punto","nunique"))
                            try:
                                parish_geo=load_parroquia_geojson(tuple(sorted(parish_counts.join_key)))
                            except (OSError,ValueError) as exc:
                                st.warning(f"No se pudieron cargar los límites parroquiales: {exc}")
                            else:
                                parish_areas=pd.DataFrame([
                                    {"join_key":feature["properties"]["join_key"],
                                     "nombre":feature["properties"].get("parroquia", "Parroquia")}
                                    for feature in parish_geo["features"]])
                                if parish_areas.empty:
                                    st.info("No se encontraron límites para las parroquias declaradas.")
                                else:
                                    parish_areas=parish_areas.merge(parish_counts,on="join_key",how="left")
                                    parish_areas[["socios","puntos"]]=parish_areas[["socios","puntos"]].fillna(0).astype(int)
                                    leaflet_partner_map(
                                        parish_geo,parish_areas,"Parroquia",
                                        background_geo=load_estado_geojson(),
                                        zoom_to_data=False,original_palette=True,
                                    )
                                    st.caption("Parroquias declaradas en F01, representadas con límites administrativos reales (Venezuela360). El formulario no aporta coordenadas individuales de los puntos.")
                    selected_ids=set(subset.id_punto)
                    mapped_points=p[p.id_punto.isin(selected_ids) & p.foco.eq(focus_name)].copy()
                    mapped_points=mapped_points.dropna(subset=["latitud","longitud"])
                    mapped_points=mapped_points[
                        mapped_points.latitud.between(-2,14) & mapped_points.longitud.between(-76,-58)
                    ].drop_duplicates("id_punto")
                    if mapped_points.empty:
                        st.info("No hay coordenadas válidas para los puntos de esta selección.")
                    else:
                        mapped_points["parroquia_visible"]=mapped_points.parroquia.fillna("No reportada")
                        if is_facility:
                            state_geo=load_estado_geojson()
                            state_rows=pd.DataFrame([
                                {"join_key":feature["properties"]["join_key"],
                                 "nombre":feature["properties"]["adm1_name"],"socios":0,"puntos":0}
                                for feature in state_geo["features"]])
                            leaflet_partner_map(state_geo,state_rows,"Estado",
                                                markers=mapped_points,metric="puntos",
                                                metric_label="Establecimientos",zoom_to_data=True,marker_only=True)
                            st.caption(f"Se muestran {mapped_points.id_punto.nunique()} puntos georreferenciados de la selección. Cuando el registro histórico no identifica la parroquia, el punto aparece en su coordenada y se indica «No reportada» al pasar el cursor.")
                else:
                    if territory_level=="Estado":
                        geo=load_estado_geojson()
                        map_rows=(subset.groupby("estado",as_index=False)
                                  .agg(puntos=("id_punto","nunique"),
                                       socios=("organizacion","nunique")))
                        map_rows=map_rows.rename(columns={"estado":"join_key"})
                        geo_rows=pd.DataFrame([
                            {"join_key":feature["properties"]["join_key"],
                             "nombre":feature["properties"]["adm1_name"]}
                            for feature in geo["features"]
                        ])
                    else:
                        geo=load_municipio_geojson()
                        map_subset=subset.assign(join_key=subset.estado+" | "+subset.municipio)
                        map_rows=(map_subset.groupby("join_key",as_index=False)
                                  .agg(puntos=("id_punto","nunique"),
                                       socios=("organizacion","nunique")))
                        geo_rows=pd.DataFrame([
                            {"join_key":feature["properties"]["join_key"],
                             "nombre":feature["properties"]["adm2_name"]+" ("+feature["properties"]["adm1_name"]+")"}
                            for feature in geo["features"]
                        ])
                    if geo_rows.empty:
                        st.info("No hay límites geográficos disponibles para esta selección.")
                        continue
                    if is_facility:
                        state_geo=load_estado_geojson()
                        state_rows=pd.DataFrame([
                            {"join_key":feature["properties"]["join_key"],
                             "nombre":feature["properties"]["adm1_name"],"socios":0,"puntos":0}
                            for feature in state_geo["features"]])
                        selection_markers=p[p.id_punto.isin(set(subset.id_punto)) & p.foco.eq(focus_name)].copy()
                        selection_markers=selection_markers.dropna(subset=["latitud","longitud"])
                        selection_markers=selection_markers[
                            selection_markers.latitud.between(-2,14) & selection_markers.longitud.between(-76,-58)
                        ].drop_duplicates("id_punto")
                        if selection_markers.empty:
                            st.info("No hay coordenadas válidas para los establecimientos de esta selección.")
                        else:
                            leaflet_partner_map(state_geo,state_rows,"Estado",markers=selection_markers,
                                                metric="puntos",metric_label="Establecimientos",
                                                zoom_to_data=True,marker_only=True)
                            st.caption(f"Se muestran {selection_markers.id_punto.nunique()} establecimientos georreferenciados de la selección, con el mismo formato del mapa inicial.")
                    else:
                        keyed=(subset.assign(join_key=subset.estado if territory_level=="Estado"
                                             else subset.estado+" | "+subset.municipio))
                        partner_counts=keyed.groupby("join_key",as_index=False).agg(
                            socios=("organizacion","nunique"),puntos=("id_punto","nunique"))
                        partner_map=geo_rows.merge(partner_counts,on="join_key",how="left")
                        partner_map[["socios","puntos"]]=partner_map[["socios","puntos"]].fillna(0).astype(int)
                        leaflet_partner_map(geo,partner_map,territory_level,
                                            zoom_to_data=False,original_palette=True)
                        st.caption("Mapa de cobertura de socios con el mismo formato del tablero original; los territorios sin registros permanecen en gris.")
            if not is_facility:
                # Explorador F01: cada tarjeta y figura se recalcula desde los puntos
                # que declararon el servicio, nunca desde los resultados periódicos F02.
                service_options={
                    "Atención Primaria (APS)":"serv_atencion_primaria",
                    "Salud mental y apoyo psicosocial":"serv_salud_mental_psicosocial",
                    "Enfermedades no transmisibles":"serv_enfermedades_no_transmisibles",
                    "Enfermedades transmisibles, VIH e ITS":"serv_enfermedades_transmisibles",
                    "Salud materna y perinatal":"serv_salud_materna_perinatal",
                    "Prevención y atención VIH":"serv_prevencion_atencion_vih",
                    "Salud sexual y reproductiva":"serv_salud_sexual_reproductiva",
                    "Violencia sexual":"serv_violencia_sexual",
                    "Salud nutricional":"serv_salud_nutricional",
                    "Vacunación":"serv_vacunacion",
                    "Vigilancia epidemiológica":"serv_alerta_temprana_vigilancia_epidemiologica",
                    "Lesiones y traumas":"serv_lesiones_traumas",
                    "Acciones institucionales":"serv_acciones_institucionales",
                    "Acciones comunitarias":"serv_acciones_comunitarias",
                    "Gestión de casos":"serv_gestion_casos",
                }
                service_counts=pd.DataFrame([
                    {"servicio":label,"puntos":active_places.loc[
                        pd.to_numeric(active_places[column],errors="coerce").fillna(0).gt(0),
                        "id_servicio",
                    ].nunique()}
                    for label,column in service_options.items() if column in active_places
                ])
                service_counts=service_counts[service_counts.puntos.gt(0)].sort_values("puntos",ascending=False)
                st.markdown("### Servicios ofertados")
                if service_counts.empty:
                    st.info("No hay servicios ofertados para los filtros seleccionados.")
                else:
                    service_colors=dict(zip(
                        service_counts.servicio,
                        [PALETTE_EXT[i % len(PALETTE_EXT)] for i in range(len(service_counts))],
                    ))
                    service_fig=px.treemap(
                        service_counts,path=["servicio"],values="puntos",color="servicio",
                        color_discrete_map=service_colors,
                    )
                    set_value_tile_labels(service_fig,"puntos",wrap_width=19)
                    service_fig.update_traces(hovertemplate="<b>%{label}</b><br>%{value:,.0f} puntos<extra></extra>")
                    plot(service_fig,510,"public_f01_services_tree",margin=dict(l=8,r=8,t=8,b=8))
                    section_figures.setdefault("Explora un servicio",[]).append(service_fig)
                    st.caption("El tamaño de cada bloque indica cuántos puntos F01 ofrecen ese servicio. Un punto puede ofrecer varios servicios.")
                st.markdown('<div id="explora-servicio-f01" class="section-kicker">Explorar la oferta por servicio</div>',unsafe_allow_html=True)
                st.markdown("### 2. Explora un servicio")
                service_name=st.selectbox(
                    "Seleccionar un servicio",["Todos los servicios",*service_options],
                    key="public_f01_service",
                )
                service_selected=service_name!="Todos los servicios"
                service_places=(
                    active_places[
                        pd.to_numeric(active_places[service_options[service_name]],errors="coerce").fillna(0).gt(0)
                    ] if service_selected else active_places
                ).drop_duplicates("id_servicio").copy()
                public_universe=active_places.id_servicio.nunique()
                service_staff_cols=[c for c in service_places if c.startswith("personal_")]
                service_professionals=int(pd.to_numeric(service_places[service_staff_cols].stack(),errors="coerce").sum()) if service_staff_cols else 0
                service_cards=[
                    ("Socios",num(service_places.organizacion.nunique()),"que ofrecen el servicio"),
                    ("Puntos",f"{num(len(service_places))} de {num(public_universe)}","ofrecen el servicio"),
                    ("Estados",num(service_places.estado.nunique()),"con presencia"),
                    ("Municipios",num(service_places.municipio.nunique()),"con oferta"),
                    ("Profesionales reportados",num(service_professionals),"en los puntos oferentes"),
                    ("Inversión registrada","Sin datos","no declarada por servicio"),
                ]
                st.markdown('<div class="metric-row" style="grid-template-columns:repeat(6,1fr)">'+''.join(metric(*row) for row in service_cards)+'</div>',unsafe_allow_html=True)
                st.caption("Los profesionales corresponden al personal total de los puntos que ofrecen el servicio; F01 no permite atribuirlos exclusivamente a ese servicio. Los archivos históricos no registran inversión por servicio, por lo que no se estima un monto.")
                st.markdown("#### Socios por estado y municipio")
                state_col,municipality_col=st.columns(2)
                state_options=["Todos los estados",*sorted(service_places.estado.dropna().unique())]
                selected_state=state_col.selectbox("Estado",state_options,key="public_f01_service_state")
                state_service_places=(service_places if selected_state=="Todos los estados" else
                                      service_places[service_places.estado.eq(selected_state)])
                municipality_options=["Todos los municipios",*sorted(state_service_places.municipio.dropna().unique())]
                selected_municipality=municipality_col.selectbox(
                    "Municipio",municipality_options,key="public_f01_service_municipality",
                )
                territorial_service_places=(state_service_places if selected_municipality=="Todos los municipios" else
                                            state_service_places[state_service_places.municipio.eq(selected_municipality)])
                if territorial_service_places.empty:
                    st.info("No hay socios para esta selección.")
                else:
                    territory_counts=(territorial_service_places.groupby(["estado","municipio"],as_index=False)
                                      .agg(socios=("organizacion","nunique"),
                                           organizaciones=("organizacion",lambda names:", ".join(sorted(set(names.dropna()))))))
                    territory_counts["territorio"]=territory_counts.municipio+" · "+territory_counts.estado
                    territory_counts=territory_counts.sort_values(["socios","territorio"],ascending=[True,True])
                    st.markdown('<div class="metric-row" style="grid-template-columns:repeat(2,1fr)">'+
                                metric("Socios",num(territorial_service_places.organizacion.nunique()),"en la selección")+
                                metric("Municipios",num(len(territory_counts)),"con presencia")+'</div>',
                                unsafe_allow_html=True)
                    territory_fig=px.bar(territory_counts,x="socios",y="territorio",orientation="h",
                                         text="socios",custom_data=["organizaciones"],
                                         color_discrete_sequence=["#4e70a8"])
                    territory_fig.update_traces(hovertemplate="<b>%{y}</b><br>Socios: %{x}<br>%{customdata[0]}<extra></extra>")
                    territory_fig.update_layout(xaxis_title="Cantidad de socios",yaxis_title=None,
                                                showlegend=False)
                    plot(territory_fig,max(240,48*len(territory_counts)+95),
                         "public_f01_service_territory_partners",margin=dict(l=8,r=28,t=8,b=42))
                    st.caption("Cada socio se cuenta una vez por municipio; el total de la tarjeta cuenta socios únicos en la selección.")
                st.markdown("#### Socios presentes por territorio")
                for service_state, state_places in service_places.groupby("estado",sort=True):
                    state_orgs=state_places.organizacion.nunique()
                    with st.expander(f"{service_state} · {state_orgs} {'socio' if state_orgs==1 else 'socios'}"):
                        municipality=st.selectbox(
                            "Municipio",sorted(state_places.municipio.dropna().unique()),
                            key=f"service_municipality_{normalize_name(service_state)}",
                        )
                        municipality_places=state_places[state_places.municipio.eq(municipality)]
                        st.markdown("**Socios:** "+", ".join(sorted(municipality_places.organizacion.dropna().unique())))
                        parish_rows=municipality_places[
                            municipality_places.parroquia.notna() &
                            municipality_places.parroquia.ne("No reportada")
                        ]
                        if not parish_rows.empty:
                            parish_summary=parish_rows.groupby("parroquia",as_index=False).agg(
                                socios=("organizacion",lambda names:", ".join(sorted(set(names.dropna())))),
                                puntos=("id_servicio","nunique"),
                            ).rename(columns={"parroquia":"Parroquia","socios":"Socios","puntos":"Puntos"})
                            st.dataframe(parish_summary,hide_index=True,width="stretch")
                # F01 registra la oferta a nivel de servicio, no el detalle
                # de acciones/indicadores que corresponde a los reportes F02.
                action_scope=service_places
                st.markdown("### Personal disponible por perfil")
                staff_cols=[c for c in action_scope.columns if c.startswith("personal_")]
                if action_scope.empty or not staff_cols:
                    st.info("No hay personal reportado para el servicio seleccionado.")
                else:
                    totals=pd.to_numeric(action_scope[staff_cols].stack(),errors="coerce").unstack().sum().sort_values(ascending=False).head(12).sort_values().reset_index()
                    totals.columns=["perfil","personas"]
                    totals=totals[totals.personas.gt(0)]
                    totals["perfil"]=totals.perfil.str.replace("personal_","",regex=False).str.replace("_"," ").str.title()
                    totals["perfil"]=totals["perfil"].replace({"Medico":"Médicos","Enfermero":"Enfermeros","Administrativos":"Administrativos","Psicologo":"Psicólogos","Pediatria":"Pediatría","Ginecologia":"Ginecología","Odontologos":"Odontólogos","Psiquiatra":"Psiquiatras"})
                    if totals.empty:
                        st.info("No hay personal reportado para el servicio seleccionado.")
                    else:
                        totals["_tilecolor"]=value_gradient_colors(totals.personas,[[0,tint(NAVY,.88)],[.55,tint(NAVY,.42)],[1,NAVY]])
                        fig=px.treemap(totals,path=["perfil"],values="personas",color="perfil",color_discrete_map=dict(zip(totals.perfil,totals._tilecolor)))
                        set_value_tile_labels(fig,"",value_first=True)
                        fig.update_traces(hovertemplate="<b>%{label}</b><br>%{value:,.0f}<extra></extra>")
                        plot(fig,500,"staffing_tree",margin=dict(l=8,r=8,t=8,b=8))
                        section_figures.setdefault("Personal",[]).append(fig)
            if map_level=="Parroquia" and is_facility:
                st.caption("El gráfico de barras permanece agrupado por municipio; la opción Parroquia cambia solo el mapa a puntos georreferenciados.")
            if is_facility:
                st.markdown("<h2>Presencia territorial - Capacidad</h2>",unsafe_allow_html=True)
                st.markdown(f"### {heading}")
            stacked=subset.groupby(["tipo_accion","territorio"],as_index=False).agg(puntos=("id_punto","nunique"))
            action_totals=(subset.groupby("tipo_accion").id_punto.nunique()
                           .sort_values(ascending=False))
            action_order=action_totals.index.tolist()
            if is_facility:
                # Conservar el detalle original en los datos; simplificar solo
                # la etiqueta visible y situar «Otro» después de Odontología.
                other_actions=[name for name in action_order if str(name).startswith("Otro:")]
                action_order=[name for name in action_order if name not in other_actions]
                if other_actions:
                    insert_at=action_order.index("Odontología")+1 if "Odontología" in action_order else len(action_order)
                    action_order[insert_at:insert_at]=other_actions
            display_name=lambda name: "Otro" if is_facility and str(name).startswith("Otro:") else name
            display_order=[display_name(name) for name in action_order]
            territory_order=(focus_points.groupby("territorio").id_punto.nunique()
                             .sort_values(ascending=False).index.tolist())
            chart_state_colors={**STATE_COLORS,"Sin estado":_BRAND_GRAY,"Bolívar":_BRAND_ORANGE}
            territory_colors={name:(chart_state_colors.get(name,TERRITORY_COLORS[i % len(TERRITORY_COLORS)])
                                    if territory_level=="Estado" else municipality_colors[name])
                              for i,name in enumerate(territory_order)}
            stacked["porcentaje"]=stacked.puntos/base_total*100
            stacked["total_accion"]=stacked.tipo_accion.map(action_totals)
            stacked["porcentaje_total"]=stacked.total_accion/base_total*100
            stacked["etiqueta"]=stacked.porcentaje.map(lambda value:f"{value:.1f}%" if value>=3 else "")
            stacked["accion_visible"]=stacked.tipo_accion.map(display_name)
            fig=px.bar(stacked,x="porcentaje",y="accion_visible",color="territorio",orientation="h",barmode="stack",
                       text="etiqueta",custom_data=["puntos","total_accion","porcentaje_total"],
                       color_discrete_map=territory_colors,
                       category_orders={"accion_visible":display_order,"territorio":territory_order},
                       labels={"porcentaje":"Porcentaje del total de puntos","accion_visible":"","territorio":territory_level})
            for trace in fig.data:
                trace.hovertemplate=("<b>%{y}</b><br>"+territory_level+": "+html_lib.escape(trace.name)+"<br>"
                                     "Puntos en "+("el estado" if territory_level=="Estado" else "el municipio")+": %{customdata[0]}<br>"
                                     "Porcentaje del total: %{x:.1f}%<br>"
                                     "Total de la acción: %{customdata[1]} de "+str(base_total)+" puntos "
                                     "(%{customdata[2]:.1f}%)<extra></extra>")
            fig.update_traces(textposition="inside",insidetextanchor="middle",textfont=dict(size=11))
            total_percent=action_totals/base_total*100
            label_gap=2.0
            fig.add_trace(go.Scatter(x=(total_percent.reindex(action_order)+label_gap).tolist(),y=display_order,mode="text",
                                     text=[f"{total_percent[name]:.1f}%" for name in action_order],
                                     textposition="middle right",textfont=dict(size=12,color=NAVY),
                                     showlegend=False,hoverinfo="skip"))
            fig.update_xaxes(ticksuffix="%",range=[0,max(20,float(total_percent.max())+14)])
            fig.update_layout(legend=dict(orientation="h",yanchor="bottom",y=1.02,x=0,
                                          entrywidth=230,entrywidthmode="pixels",
                                          font=dict(size=12),itemsizing="constant"),bargap=.28)
            if territory_level=="Estado":
                fig.add_annotation(text="Estado",xref="paper",yref="paper",x=0,y=1.15,
                                   showarrow=False,font=dict(size=12,color=MUTED),xanchor="left")
            fig.add_annotation(text=f"Base: {num(base_total)} puntos",xref="paper",yref="paper",
                               x=1,y=1.15,showarrow=False,font=dict(size=12,color=MUTED),xanchor="right")
            if is_facility:
                plot(fig,max(570,46*len(action_order)+270),f"territory_{focus_key}_f01",
                     margin=dict(l=8,r=50,t=105,b=55))
                section_figures.setdefault("Alcance territorial por acción",[]).append(fig)
            if is_facility:
                st.markdown("### Modalidades de apoyo")
                selected_supports=filt(d["facility_supports_joined"])
                selected_supports=selected_supports[
                    selected_supports.id_establecimiento.astype(str).isin(
                        subset.id_punto.astype(str).str.replace("FAC-","",regex=False)
                    )
                ]
                ftsup=(selected_supports.groupby("tipo_apoyo",as_index=False)
                       .agg(establecimientos=("id_establecimiento","nunique"))
                       .sort_values("establecimientos",ascending=False))
                if ftsup.empty:
                    st.info("No hay tipos de apoyo registrados para los filtros seleccionados.")
                else:
                    ftsup["% de la selección"]=(100*ftsup.establecimientos/max(base_total,1)).round(1)
                    support_chart=ftsup.sort_values("establecimientos",ascending=False).copy()
                    support_palette=[ORANGE,tint(ORANGE,.30),tint(ORANGE,.47),
                                     tint(ORANGE,.68),tint(ORANGE,.79)]
                    support_chart["_color"]=[support_palette[i%len(support_palette)]
                                              for i in range(len(support_chart))]
                    support_fig=px.treemap(
                        support_chart,path=["tipo_apoyo"],values="establecimientos",
                        color="tipo_apoyo",color_discrete_map=dict(zip(support_chart.tipo_apoyo,support_chart._color)))
                    set_value_tile_labels(support_fig,"establecimientos",min_size=14,max_size=17,wrap_width=22)
                    support_fig.update_traces(
                        hovertemplate="<b>%{label}</b><br>%{value:,.0f} establecimientos<extra></extra>")
                    support_fig.update_layout(showlegend=False)
                    plot(support_fig,340,"facility_support_modalities_f01",margin=dict(l=4,r=4,t=4,b=4))
                    section_figures.setdefault("Modalidades de apoyo",[]).append(support_fig)
                    unregistered_modalities=[name for name in FACILITY_SUPPORT_TYPE_CATALOG
                                             if name not in set(ftsup.tipo_apoyo)]
                    if unregistered_modalities:
                        with st.expander(f"Ver {len(unregistered_modalities)} modalidades del formulario vigente sin registros en esta selección"):
                            st.markdown("\n".join(f"- {name}" for name in unregistered_modalities))
                    selected_modality=st.selectbox("Explorar una modalidad de apoyo",
                        ["Todas las modalidades",*FACILITY_SUPPORT_TYPE_CATALOG],
                        key="facility_support_modality_f01",
                        format_func=lambda name:name if name=="Todas las modalidades" or name in set(ftsup.tipo_apoyo)
                            else f"{name} · sin registros")
                    detail=selected_supports if selected_modality=="Todas las modalidades" else selected_supports[selected_supports.tipo_apoyo.eq(selected_modality)]
                    detail_by_state=(detail.groupby("estado",as_index=False).id_establecimiento.nunique()
                                     .rename(columns={"estado":"Estado","id_establecimiento":"Establecimientos"})
                                     .sort_values("Establecimientos",ascending=False))
                    if detail_by_state.empty:
                        st.info("No hay establecimientos con esta modalidad en la selección actual.")
                    else:
                        st.dataframe(detail_by_state,hide_index=True,width="stretch")
                    st.caption("Modalidades declaradas en F01 para los establecimientos de la selección. Un establecimiento puede registrar varias. El F02 histórico no desglosa modalidades por período.")
        if is_facility_capacity_view:
            st.caption("Cada barra muestra el porcentaje de puntos que ofrece el servicio o acción. El mapa de establecimientos muestra los puntos por territorio o sus ubicaciones georreferenciadas cuando están disponibles.")

    if module==F01_MODULE:
        st.markdown('<div id="socios-f01" class="section-kicker">04 · Socios y lugares de intervención</div>',unsafe_allow_html=True)

    socios_source=active_places if module==F01_MODULE else p
    socios_point_col="id_servicio" if module==F01_MODULE else "id_punto"
    if is_facility_capacity_view:
        st.markdown('<div id="socios-f01" class="section-kicker">03 · Socios</div>',unsafe_allow_html=True)
    st.markdown(f'<h2>Organizaciones y su alcance · {num(socios_source.organizacion.nunique())} socios · {num(socios_source[socios_point_col].nunique())} puntos</h2>',unsafe_allow_html=True)
    if module==F01_MODULE:
        def _socios_share_card(category: str, heading: str, key: str, colors: dict[str,str]) -> None:
            # Una organización cuenta una sola vez por categoría; las barras
            # muestran la proporción del total de socios clasificados.
            rows=(active_places[["organizacion",category]].dropna()
                  .drop_duplicates(["organizacion",category])
                  .groupby(category).organizacion.nunique().sort_values(ascending=False))
            with st.container(border=True):
                st.markdown(f"### {heading}")
                if rows.empty or rows.sum()==0:
                    st.info("No hay datos para la selección actual.")
                    return
                fig=go.Figure()
                for i,(name,count) in enumerate(rows.items()):
                    percentage=100*count/rows.sum()
                    fig.add_trace(go.Bar(
                        x=[percentage],y=["Socios"],orientation="h",name=str(name),
                        marker_color=colors.get(str(name),PALETTE_EXT[i%len(PALETTE_EXT)]),
                        text=[f"{percentage:.0f}%" if percentage>=7 else ""],
                        textposition="inside",insidetextanchor="middle",
                        customdata=[[int(count)]],
                        hovertemplate="<b>%{fullData.name}</b><br>%{customdata[0]} socios · %{x:.1f}%<extra></extra>",
                    ))
                fig.update_layout(
                    barmode="stack",showlegend=True,uniformtext_minsize=11,uniformtext_mode="hide",
                    legend=dict(orientation="h",x=0,y=-.35,xanchor="left",font=dict(size=10)),
                )
                fig.update_xaxes(range=[0,100],ticksuffix="%",dtick=20,showgrid=False,zeroline=False)
                fig.update_yaxes(showticklabels=False,showgrid=False)
                plot(fig,230,key,margin=dict(l=0,r=0,t=5,b=85))
                section_figures.setdefault("Presencia de socios",[]).append(fig)

        composition_col,implementation_col=st.columns(2,gap="medium")
        with composition_col:
            _socios_share_card("tipo_organizacion","Composición de socios","partner_composition",
                               {"ONG Internacional":NAVY,"ONG Nacional":ORANGE,"Agencia de Naciones Unidas":SKY,"Observador":GRAY})
        with implementation_col:
            _socios_share_card("modalidad_implementacion","Modalidad de implementación","implementation_mode",
                               {"Ambas":SKY,"Socio implementador":ORANGE,"Implementador directo":NAVY})

        mode_col,rank_col=st.columns([0.88,1.12],gap="medium")
        with mode_col:
            public_places=active_places.assign(
                tipo_visible=active_places.tipo_lugar.fillna("No reportado")
            )
            place_counts=public_places.groupby("tipo_visible").id_servicio.nunique().reset_index(name="puntos")
            simulated_count=int(public_places.loc[public_places.tipo_lugar_simulado,"id_servicio"].nunique())
            st.markdown("### Tipo de lugar de intervención" + (" · datos de prueba" if simulated_count else ""))
            if place_counts.empty:
                st.info("No hay lugares de intervención para los filtros seleccionados.")
            else:
                fig=px.pie(place_counts,names="tipo_visible",values="puntos",hole=.58,
                           color_discrete_sequence=PALETTE_EXT[:len(place_counts)])
                fig.update_traces(textinfo="percent",textposition="inside",textfont=dict(size=11),
                                  marker_line_color=PAPER,marker_line_width=2,
                                  hovertemplate="<b>%{label}</b><br>%{value} puntos · %{percent}<extra></extra>")
                fig.update_layout(showlegend=True,legend=dict(orientation="h",x=0,y=-.1,font=dict(size=10)),
                                  annotations=[dict(text=f"<b>{public_places.id_servicio.nunique()}</b><br>puntos",x=.5,y=.5,
                                                    showarrow=False,font=dict(size=17,color=NAVY))])
                plot(fig,470,"public_type_donut",margin=dict(l=8,r=8,t=6,b=132))
                section_figures.setdefault("Presencia de socios",[]).append(fig)
            if simulated_count:
                st.caption(f"Datos de prueba: {simulated_count} puntos sin tipo de lugar en el F01 histórico recibieron una categoría provisional. No representan respuestas reales.")
            mode_labels={"modalidad_intramural":"Atención intramural","modalidad_extramural":"Atención extramural","modalidad_movil":"Unidad Móvil","modalidad_sede_propia":"Sede Propia","modalidad_pago_atenciones":"Pago por atenciones","modalidad_oficina":"Oficina","modalidad_otra":"Otra"}
            mode_values=pd.DataFrame({"modalidad":list(mode_labels.values()),"selecciones":[pd.to_numeric(active_places.get(col,0),errors="coerce").fillna(0).sum() if col in active_places else 0 for col in mode_labels]})
            mode_total=int(mode_values.selecciones.sum()); mode_values["porcentaje"]=mode_values.selecciones/max(mode_total,1)*100; mode_values=mode_values[mode_values.selecciones.gt(0)].sort_values("porcentaje",ascending=False)
            with st.expander("Ver modalidades de atención registradas"):
                st.caption("Porcentaje de selecciones; un punto puede marcar más de una modalidad.")
                st.dataframe(mode_values.rename(columns={"modalidad":"Modalidad","selecciones":"Selecciones","porcentaje":"Porcentaje"}),
                             hide_index=True,width="stretch")
        with rank_col:
            partner_points=active_places.groupby("organizacion").id_servicio.nunique().sort_values(ascending=False)
            st.markdown(f"### Principales socios por cantidad de puntos de intervención · {len(partner_points)} socios")
            if partner_points.empty:
                st.info("No hay socios para los filtros seleccionados.")
            else:
                top_partner_points=partner_points.head(10).sort_values()
                chart_names=["<br>".join(textwrap.wrap(str(name),width=24,break_long_words=False))
                             for name in top_partner_points.index]
                fig=px.bar(x=top_partner_points.values,y=chart_names,orientation="h",
                           text=top_partner_points.values,color_discrete_sequence=[NAVY],
                           labels={"x":"Puntos de intervención","y":""})
                fig.update_traces(textposition="outside",textfont=dict(size=11,color=NAVY),
                                  marker_line_width=0,hovertemplate="%{y}<br>%{x} puntos<extra></extra>")
                fig.update_layout(showlegend=False,bargap=.42)
                fig.update_xaxes(showgrid=True,gridcolor=tint(GRAY,.8),zeroline=False,dtick=2)
                fig.update_yaxes(showgrid=False,automargin=True,tickfont=dict(size=11,color=INK))
                plot(fig,420,"top_orgs_overall_bar",margin=dict(l=2,r=12,t=8,b=42))
                section_figures.setdefault("Presencia de socios",[]).append(fig)
                with st.expander("Ver todos los socios y sus puntos"):
                    st.dataframe(partner_points.rename("Puntos").rename_axis("Socio").reset_index(),
                                 hide_index=True,width="stretch")
            if not partner_points.empty:
                st.caption(f"Se muestran {min(10,len(partner_points))} de {len(partner_points)} socios, ordenados por puntos de intervención.")

    # Vocabulario alineado al F01 real (grupo "Servicios y apoyos de la
    # organización": pregunta 5.1 "Seleccione las áreas o servicios del
    # establecimiento de salud que reciben apoyo" y 5.2 "tipo(s) de apoyo que
    # serán proporcionados"). El formulario nunca usa la palabra "oferta"
    # para esto — usa "apoyo".
    if is_facility_capacity_view:
        st.markdown("### Principales socios por cantidad de puntos")
        facility_points=p[p.foco.eq("Establecimiento de salud")]
        facility_types=facility_points.groupby("tipo_punto").id_punto.nunique().reset_index(name="puntos")
        facility_orgs=facility_points.groupby("organizacion").id_punto.nunique().reset_index(name="puntos")

        # La dona ocupa menos ancho para que el cuadro de organizaciones (lo
        # más importante de esta comparación) tenga más espacio.
        with st.container(border=True):
            facility_left,facility_right=st.columns([1.6,3.4],gap="small")
            with facility_left:
                st.markdown("#### Tipo de establecimiento de salud")
                if facility_types.empty: st.info("No hay establecimientos para los filtros seleccionados.")
                else:
                    fig=px.pie(facility_types,names="tipo_punto",values="puntos",hole=.52,
                               color_discrete_sequence=PALETTE_EXT[:7],
                               labels={"tipo_punto":"Tipo de establecimiento","puntos":"Establecimientos"})
                    fig.update_traces(textinfo="percent",textposition="inside",marker_line_color=PAPER,marker_line_width=3)
                    # Leyenda vertical (no horizontal): con la columna angosta, una
                    # leyenda en fila se salía del recuadro; en columna se lee bien
                    # sin importar el ancho disponible.
                    fig.update_layout(annotations=[dict(text=f"<b>{num(facility_types.puntos.sum())}</b><br>establecimientos",x=.5,y=.5,showarrow=False,font=dict(size=15,color=INK))],
                                      legend=dict(orientation="v",yanchor="top",y=-.05,xanchor="center",x=.5,font=dict(size=11)))
                    plot(fig,560,"facility_type_donut",margin=dict(l=12,r=12,t=10,b=160))
                    section_figures.setdefault("Presencia de socios", []).append(fig)
                    if "Tipo histórico sin equivalencia" in set(facility_types.tipo_punto):
                        st.caption("Los registros históricos de clínica, ambulatorio y CDI no permiten asignar con certeza una opción del formulario vigente; se agrupan como «Tipo histórico sin equivalencia».")
            with facility_right:
                st.markdown("#### Organizaciones que apoyan establecimientos de salud")
                if facility_orgs.empty: st.info("No hay organizaciones para los filtros seleccionados.")
                else:
                    facility_orgs["nombre_corto"]=facility_orgs.organizacion.map(tile_name)
                    fig=px.treemap(facility_orgs,path=["nombre_corto"],values="puntos",color="organizacion",custom_data=["organizacion"],
                                   color_discrete_sequence=PALETTE_EXT[:9],
                                   labels={"organizacion":"Organización","puntos":"Establecimientos"})
                    fig.update_traces(hovertemplate="%{customdata[0]}<br>%{value} establecimientos<extra></extra>",
                                      marker_line_color=PAPER,marker_line_width=3)
                    set_org_tile_labels(fig)
                    fig.update_layout(showlegend=False)
                    plot(fig,700,"facility_orgs_treemap",margin=dict(l=4,r=4,t=10,b=12))
                    section_figures.setdefault("Presencia de socios", []).append(fig)
                    st.caption("El tamaño de cada bloque representa el número de establecimientos apoyados.")

        st.markdown('<div id="apoyos-establecimientos-f01" class="section-kicker">04 · Apoyos a establecimientos de salud</div><h2>Servicios y apoyos que las organizaciones brindan</h2>',unsafe_allow_html=True)

        st.markdown("### Paquete de apoyo por establecimiento")
        facility_details=facility_points.drop_duplicates("id_punto")[["id_punto","estado","lugar","organizacion","tipo_punto"]].copy()
        facility_details["id_establecimiento"]=facility_details.id_punto.astype(str).str.replace("FAC-","",regex=False)
        historical_types=(d["facilities"].assign(
            id_establecimiento=lambda frame:frame.id_establecimiento.astype(str))
            .set_index("id_establecimiento")["tipo_establecimiento"])
        facility_details["Tipo original F01"]=facility_details.id_establecimiento.map(historical_types)
        area_names=(facility_areas.groupby("id_establecimiento").servicio
                    .agg(lambda values:", ".join(sorted(set(values.dropna().astype(str))))))
        support_names=(facility_supports.groupby("id_establecimiento").tipo_apoyo
                       .agg(lambda values:", ".join(sorted(set(values.dropna().astype(str))))))
        facility_details["Áreas o servicios"]=facility_details.id_establecimiento.map(area_names).fillna("No reportado")
        facility_details["Modalidades de apoyo"]=facility_details.id_establecimiento.map(support_names).fillna("No reportado")
        facility_details=facility_details.rename(columns={"estado":"Estado","lugar":"Establecimiento","organizacion":"Organización","tipo_punto":"Tipología"})
        for state, state_rows in facility_details.groupby("Estado",sort=True):
            with st.expander(f"{state} · {len(state_rows)} establecimiento(s)"):
                st.dataframe(state_rows[["Establecimiento","Organización","Tipología","Tipo original F01","Áreas o servicios","Modalidades de apoyo"]],
                             hide_index=True,width="stretch")

    if module!=F02_FACILITY_MODULE:
        st.markdown('<div id="inversion-f01" class="section-kicker">05 · Recursos movilizados</div><h2>Inversión</h2>',unsafe_allow_html=True)
    facility_investment=pd.DataFrame({"inversion_simulada_usd":pd.Series(dtype="float64")})
    if module==F01_MODULE:
        simulated_investment_points=int(active_places.loc[active_places.inversion_simulada,"id_servicio"].nunique())
        st.warning(f"SIMULACIÓN · {simulated_investment_points} de {active_places.id_servicio.nunique()} puntos tienen importes de prueba. El F01 histórico no declara montos de inversión; estas cifras no son gasto, financiamiento ni compromisos reales.")
        investment_summary=active_places.groupby("estado",as_index=False).agg(
            inversion_usd=("inversion_visual_usd","sum"),puntos=("id_servicio","nunique")
        ).sort_values("inversion_usd",ascending=False)
        if investment_summary.empty:
            st.info("No hay puntos en la selección actual.")
        else:
            st.markdown('<div class="metric-row" style="grid-template-columns:repeat(2,1fr)">'+''.join(metric(*row) for row in [
                ("Total de prueba",f"USD {num(investment_summary.inversion_usd.sum())}","suma de importes simulados"),
                ("Puntos incluidos",num(investment_summary.puntos.sum()),"con importe de prueba"),
            ])+'</div>',unsafe_allow_html=True)
            st.markdown("### Inversión simulada por estado")
            fig=px.bar(investment_summary,x="inversion_usd",y="estado",orientation="h",
                       text=investment_summary.inversion_usd.map(lambda value:f"USD {num(value)}"),
                       category_orders={"estado":investment_summary.estado.tolist()},
                       color_discrete_sequence=[NAVY],labels={"inversion_usd":"USD simulados","estado":""})
            fig.update_traces(textposition="outside",textfont=dict(size=12,color=NAVY),marker_line_width=0)
            fig.update_layout(showlegend=False,bargap=.45)
            fig.update_xaxes(showgrid=True,gridcolor=tint(GRAY,.8),zeroline=False)
            fig.update_yaxes(showgrid=False,automargin=True)
            plot(fig,max(290,50*len(investment_summary)+85),"f01_investment_by_state",margin=dict(l=8,r=115,t=12,b=42))
            section_figures.setdefault("Inversión simulada",[]).append(fig)
    elif module==F01_FACILITY_MODULE:
        facility_investment=facility_registry.drop_duplicates("id_punto").copy()
        def _facility_test_investment(point_id: object) -> int:
            digest=hashlib.sha256(str(point_id).encode("utf-8")).hexdigest()
            return 5000+(int(digest[:8],16)%26)*1000
        facility_investment["inversion_simulada_usd"]=facility_investment.id_punto.map(_facility_test_investment)
        st.warning("SIMULACIÓN · El registro F01 de establecimientos no declara montos de inversión. Las cifras siguientes son datos de prueba reproducibles, no gasto ni financiamiento real.")
        if facility_investment.empty:
            st.info("No hay establecimientos en la selección actual.")
        else:
            total_simulado=int(facility_investment.inversion_simulada_usd.sum())
            st.markdown('<div class="metric-row" style="grid-template-columns:repeat(2,1fr)">'+''.join(metric(*row) for row in [
                ("Inversión agregada de prueba",f"USD {num(total_simulado)}","monto simulado"),
                ("Establecimientos incluidos",num(facility_investment.id_punto.nunique()),"con importe de prueba"),
            ])+'</div>',unsafe_allow_html=True)
            investment_by_state=(facility_investment.groupby("estado",as_index=False)
                                 .inversion_simulada_usd.sum().sort_values("inversion_simulada_usd",ascending=False))
            fig=px.bar(investment_by_state,x="inversion_simulada_usd",y="estado",orientation="h",
                       text=investment_by_state.inversion_simulada_usd.map(lambda value:f"USD {num(value)}"),
                       category_orders={"estado":investment_by_state.estado.tolist()},
                       color_discrete_sequence=[NAVY],labels={"inversion_simulada_usd":"USD simulados","estado":""})
            fig.update_traces(textposition="outside",marker_line_width=0)
            fig.update_layout(showlegend=False,bargap=.45)
            plot(fig,max(290,50*len(investment_by_state)+85),"f01_facility_investment_by_state",margin=dict(l=8,r=115,t=12,b=42))
            section_figures.setdefault("Inversión simulada",[]).append(fig)
    st.markdown("### Enlaces de interés")
    st.markdown(
        '<div class="ext-links">'
        '<a href="https://healthcluster.who.int/countries-and-regions/venezuela-(bolivarian-republic-of)" target="_blank">Clúster de Salud · Venezuela (Health Cluster)</a>'
        '<a href="https://www.paho.org/en/paho-response-2026-venezuela-earthquakes" target="_blank">OPS/OMS · Respuesta a los terremotos en Venezuela 2026</a>'
        '<a href="https://www.paho.org/es" target="_blank">OPS/OMS · Sitio oficial</a>'
        '</div>',
        unsafe_allow_html=True,
    )

    report_palette={"navy":NAVY,"muted":MUTED,"ink":INK,"blue":BLUE,"border":tint(GRAY,.75)}
    export_points=active_places if module==F01_MODULE else p
    export_id="id_servicio" if module==F01_MODULE else "id_punto"
    top_states=export_points.groupby("estado")[export_id].nunique().nlargest(12).sort_values()
    top_munis=export_points.groupby("municipio")[export_id].nunique().nlargest(12).sort_values()
    top_orgs=export_points.groupby("organizacion")[export_id].nunique().nlargest(12).sort_values()
    point_distribution=pd.DataFrame({"foco_formulario":["Salud pública" if module==F01_MODULE else "Establecimiento de salud"],
                                     "puntos":[export_points[export_id].nunique()]})
    fs=filt(d["facility_services_joined"])
    fsup=filt(d["facility_supports_joined"])
    donor=filt(d["donor_sources"])
    top_services=fs["servicio"].value_counts().nlargest(12).sort_values()
    top_supports=fsup["tipo_apoyo"].value_counts().nlargest(12).sort_values()
    report_staff=pd.to_numeric(staffing[all_staff_cols].stack(),errors="coerce").unstack().sum().sort_values(ascending=False).head(12).sort_values()
    report_staff.index=report_staff.index.str.replace("personal_","",regex=False).str.replace("_"," ").str.title()
    top_donors=donor.groupby("donantes").id_servicio.nunique().nlargest(12).sort_values()
    report_bytes=build_report(
        title="Tablero de la Respuesta en Salud",
        subtitle="Socios y apoyos del Clúster Salud — Mapeo de socios, servicios y apoyos",
        scope_text="Universo vigente según los filtros activos. La inversión indicada es simulada y no representa montos declarados." if module==F01_MODULE else "Universo vigente según los filtros de estado, municipio, organización, foco y rango de fecha activos.",
        as_of_text=f"Generado el {pd.Timestamp.today().strftime('%d/%m/%Y')}",
        kpis=[
            ("Estados",num(export_points.estado.nunique())),
            ("Municipios",num(export_points.municipio.nunique())),
            ("Socios",num(export_points.organizacion.nunique())),
            ("Puntos de intervención",num(export_points[export_id].nunique())),
            ("Profesionales",num(professional_total)),
            ("Inversión simulada (USD)",
             num(active_places.inversion_visual_usd.sum()) if module==F01_MODULE else num(facility_investment.inversion_simulada_usd.sum())),
        ],
        sections=[
            {
                "title": "Panorama territorial",
                "rows": [[
                    ("Top estados por puntos de intervención",bar_chart(top_states.index.tolist(),top_states.tolist(),BLUE,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=190,width=350,label_width=110),12.7),
                    ("Top municipios por puntos de intervención",bar_chart(top_munis.index.tolist(),top_munis.tolist(),BLUE,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=190,width=350,label_width=120),12.7),
                ]],
                "caption": "Cuenta puntos de intervención distintos (F01) dentro del alcance de los filtros activos.",
            },
            {
                "title": "Socios y distribución por foco",
                "rows": [[
                    ("Top socios por puntos de intervención",bar_chart(top_orgs.index.tolist(),top_orgs.tolist(),NAVY,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=190,width=350,label_width=140),12.7),
                    ("Distribución por foco de intervención",composition_bar(point_distribution.foco_formulario.tolist(),point_distribution.puntos.tolist(),[RED,YELLOW],MUTED,width=350,height=115),12.7),
                ]],
                "caption": "El foco distingue acciones en el establecimiento de salud de acciones de salud pública.",
            },
            {
                "title": "Servicios y tipos de apoyo registrados",
                "rows": [[
                    ("Áreas o servicios que reciben apoyo",bar_chart(top_services.index.tolist(),top_services.tolist(),RED,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=220,width=350,label_width=145),12.7),
                    ("Tipos de apoyo proporcionado",bar_chart(top_supports.index.tolist(),top_supports.tolist(),ORANGE,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=220,width=350,label_width=145),12.7),
                ]],
                "caption": "Cuenta puntos registrados en F01 que declaran cada servicio o tipo de apoyo.",
            },
            {
                "title": "Capacidad operativa",
                "rows": [[
                    ("Personal disponible por perfil",bar_chart(report_staff.index.tolist(),report_staff.tolist(),RED,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=230,width=735,label_width=170),26.0),
                ]],
                "caption": "Suma del personal disponible reportado en los puntos incluidos por los filtros activos.",
            },
            {
                "title": "Fuentes que respaldan la oferta",
                "rows": [[
                    ("Puntos respaldados por fuente",bar_chart(top_donors.index.tolist(),top_donors.tolist(),TEAL,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=230,width=735,label_width=180),26.0),
                ]],
                "caption": "Cuenta vínculos fuente–punto reportados; no representa montos ni atribuye financiamiento a un servicio específico.",
            },
        ],
        palette=report_palette,
    )
    if module!=F02_FACILITY_MODULE:
        complete_dashboard_export()
if module in F02_MODULE_OPTIONS:
    section_figures: dict[str, list[go.Figure]] = {}
    r=filt(reports,True); ids=set(r.id_reporte); res=results[results.id_reporte.isin(ids)].copy(); active=r.sort_values("fecha_reporte").drop_duplicates(["organizacion","nombre_sitio"],keep="last")
    is_actions_view=module==F02_ACTIONS_MODULE
    section_band("REPORTE DE ACCIONES" if is_actions_view else "ACCIONES IMPLEMENTADAS · F02", "¿Qué acciones de salud pública reportan los socios, dónde se realizan y qué resultados registran?" if is_actions_view else "Actividades efectivamente realizadas y reportadas por fecha; distintas de la capacidad declarada en F01.")
    if is_actions_view:
        action_tabs={
            "General":None,
            "Salud mental y apoyo psicosocial":"Salud mental y apoyo psicosocial",
            "Enfermedades no transmisibles":"Enfermedades no transmisibles",
            "Transmisibles, VIH e ITS":"Enfermedades transmisibles, VIH e ITS",
            "Salud materna y perinatal":"Salud materna y perinatal",
            "Salud sexual y reproductiva":"Salud sexual y reproductiva",
            "Violencia sexual":"Violencia sexual",
            "Salud nutricional":"Salud nutricional",
            "Vacunación":"Inmunización",
            "Lesiones y traumas":"Lesiones y traumas",
            "Personas con discapacidad":"Discapacidad",
            "Institucional":"Acciones institucionales",
        }
        available_areas=set(res.area.dropna())
        for area in sorted(available_areas-set(action_tabs.values())):
            action_tabs[area]=area
        selected_action_tab=st.radio(
            "Pestaña de acción", list(action_tabs), horizontal=True,
            key="f02_action_tabs",
        )
        selected_reported_service=action_tabs[selected_action_tab]
        if selected_reported_service is not None:
            selected_result_ids=set(res.loc[
                res.foco.eq("Salud pública") & res.area.eq(selected_reported_service),"id_reporte"
            ])
            r=r[r.id_reporte.isin(selected_result_ids)].copy()
            res=res[res.id_reporte.isin(selected_result_ids) & res.area.eq(selected_reported_service)].copy()
            active=r.sort_values("fecha_reporte").drop_duplicates(["organizacion","nombre_sitio"],keep="last")
    if is_actions_view:
        section_index=[("panorama-f02","Panorama general"),("mapa-f02","01 · Mapa"),("focos-f02","02 · Resumen de los reportes"),("reportes-acciones-f02","03 · Reportes de acciones de salud pública"),("reportantes-f02","04 · Socios que reportaron")]
    else:
        section_index=[("mapa-establecimientos-f02","Mapa de reportes"),("resultados-f02","01 · Acciones registradas"),("focos-f02","02 · Resumen de reportes"),("reportes-establecimientos-f02","03 · Resultados reportados"),("reportantes-f02","04 · Socios que reportaron")]
    render_sidebar_index(section_index+[("imprimir-infografia-del-tablero","Descargar infografía ↓")])
    # Los indicadores de personas son sumas F02, no individuos únicos.
    person_results=res.loc[res.foco.eq("Salud pública") & res.unidad.astype(str).str.casefold().eq("personas")]
    people_reached=pd.to_numeric(person_results.total,errors="coerce").sum()
    if is_actions_view:
        cards=[
            ("Reportes",num(r.id_reporte.nunique()),"formularios únicos F02"),
            ("Organizaciones",num(r.organizacion.nunique()),"socios que reportaron"),
            ("Estados",num(r.estado.nunique()),"con actividad"),
            ("Sitios",num(r[["organizacion","nombre_sitio"]].drop_duplicates().shape[0]),"lugares de intervención"),
            ("Acciones activas",num(res.area.nunique()),"áreas con resultados"),
        ]
        st.markdown('<div id="panorama-f02"></div><h3>Panorama general de la respuesta reportada</h3>',unsafe_allow_html=True)
        st.markdown('<div class="report-overview">'+''.join(metric(*x) for x in cards)+'</div>',unsafe_allow_html=True)
        demographic_cards=[
            ("Personas reportadas",num(people_reached),"suma de indicadores"),
            ("Mujeres",num(pd.to_numeric(person_results.mujeres,errors="coerce").sum()),"suma operativa"),
            ("Hombres",num(pd.to_numeric(person_results.hombres,errors="coerce").sum()),"suma operativa"),
            ("Niñas",num(pd.to_numeric(person_results.ninas,errors="coerce").sum()),"menores de 18 años"),
            ("Niños",num(pd.to_numeric(person_results.ninos,errors="coerce").sum()),"menores de 18 años"),
            ("Personas con discapacidad",num(pd.to_numeric(person_results.discapacidad,errors="coerce").sum()),"subconjunto transversal"),
        ]
        st.markdown('<div class="report-overview report-people">'+''.join(metric(*x) for x in demographic_cards)+'</div>',unsafe_allow_html=True)
        st.caption("Las cifras son sumas de indicadores y no representan personas únicas. La discapacidad es un subconjunto transversal y no debe añadirse a los demás totales. Las pestañas filtran los reportes vinculados a cada área; un formulario puede aparecer en más de una.")
    else:
        cards=[
            ("Reportes periódicos",num(r.id_reporte.nunique()),""),
            ("Estados",num(r.estado.nunique()),""),
            ("Municipios",num(r.municipio.nunique()),""),
            ("Parroquias",num(r.loc[r.parroquia.ne("No reportada"),"parroquia"].nunique()),""),
        ]
        st.markdown('<h3>Panorama general de reportes periódicos</h3>',unsafe_allow_html=True)
        st.markdown(f'<div class="metric-row" style="grid-template-columns:repeat({len(cards)},1fr)">'+''.join(metric(*x) for x in cards)+'</div>',unsafe_allow_html=True)
    if not is_actions_view:
        st.markdown('<div id="mapa-establecimientos-f02" class="section-kicker">Reportes F02 · Ubicación</div><h2>Establecimientos con y sin reporte periódico</h2>',unsafe_allow_html=True)
        # F01 define exclusivamente el universo de establecimientos; el
        # estado azul/rojo y la cantidad de reportes vienen solo de F02.
        facility_pool=points[points.foco.eq("Establecimiento de salud")].drop_duplicates("id_punto").copy()
        if state!="Todos":
            facility_pool=facility_pool[facility_pool.estado.eq(state)]
        if municipio_filter!="Todos":
            facility_pool=facility_pool[facility_pool.municipio.eq(municipio_filter)]
        if partner!="Todos":
            facility_pool=facility_pool[facility_pool.organizacion.eq(partner)]
        facility_pool["_match_key"]=list(zip(
            facility_pool.organizacion.map(normalize_name),facility_pool.lugar.map(normalize_name)))
        report_keys=(r.assign(_match_key=list(zip(
            r.organizacion.map(normalize_name),r.nombre_sitio.map(normalize_name))))
            .groupby("_match_key").id_reporte.nunique().to_dict())
        facility_pool["reportes_punto"]=facility_pool._match_key.map(report_keys).fillna(0).astype(int)
        facility_pool["estado_reporte"]=facility_pool.reportes_punto.map(
            lambda count:"Con reporte" if count>0 else "Sin reporte")
        report_markers=facility_pool.dropna(subset=["latitud","longitud"]).copy()
        report_markers=report_markers[report_markers.latitud.between(-2,14)&report_markers.longitud.between(-76,-58)]
        if report_markers.empty:
            st.info("Los establecimientos registrados de esta selección no contienen coordenadas válidas para el mapa.")
        else:
            geo=load_estado_geojson()
            areas=pd.DataFrame([{"join_key":feature["properties"]["join_key"],
                                 "nombre":feature["properties"]["adm1_name"],"socios":0,"puntos":0}
                                for feature in geo["features"]])
            with st.container(border=True):
                leaflet_partner_map(geo,areas,"Estado",markers=report_markers,
                                    metric="puntos",metric_label="Reportes F02",
                                    zoom_to_data=True,marker_only=True)
            with_report=int(report_markers.estado_reporte.eq("Con reporte").sum())
            without_report=len(report_markers)-with_report
            missing_coordinates=len(facility_pool)-len(report_markers)
            st.caption(f"{with_report} establecimientos con reporte F02 (azul) y {without_report} sin reporte F02 (rojo) en el período seleccionado. {missing_coordinates} establecimientos registrados sin coordenadas válidas no aparecen en el mapa. F01 define el universo de establecimientos, no las acciones realizadas.")
        st.markdown('<div id="resultados-f02" class="section-kicker">01 · Acciones registradas en F02 · Simulacro</div>',unsafe_allow_html=True)
        st.markdown("## Áreas y modalidades de apoyo reportadas (simulación)")
        st.info("Datos simulados para visualizar cómo se verían las respuestas a las preguntas 4.1 y 4.2 del F02. No son acciones declaradas en los formularios recibidos.")
        demo_selections = simulated_f02_facility_selections(r.id_reporte)
        demo_cols = st.columns(2)
        for col, kind, title, color in (
            (demo_cols[0], "area", "Áreas o servicios con más reportes", NAVY),
            (demo_cols[1], "apoyo", "Modalidades de apoyo con más reportes", ORANGE),
        ):
            with col:
                st.markdown(f"### {title}")
                counts = (demo_selections[demo_selections.tipo.eq(kind)]
                          .groupby("categoria", as_index=False).id_reporte.nunique()
                          .rename(columns={"id_reporte": "reportes"})
                          .sort_values(["reportes", "categoria"], ascending=[False, True]))
                if counts.empty:
                    st.info("No hay reportes F02 para los filtros seleccionados.")
                else:
                    shown = counts.head(10).iloc[::-1]
                    fig = px.bar(shown, x="reportes", y="categoria", orientation="h",
                                 text="reportes", color_discrete_sequence=[color],
                                 labels={"reportes": "Reportes simulados", "categoria": ""})
                    fig.update_traces(textposition="outside", marker_line_width=0)
                    fig.update_layout(showlegend=False, bargap=.34,
                                      xaxis=dict(dtick=1, rangemode="tozero"))
                    plot(fig, max(360, 37 * len(shown) + 85),
                         f"f02_simulated_{kind}", margin=dict(l=8, r=48, t=10, b=45))
                    section_figures.setdefault("Simulación F02", []).append(fig)
                    if len(counts) > 10:
                        with st.expander(f"Ver las otras {len(counts) - 10} categorías simuladas"):
                            st.dataframe(counts.iloc[10:].rename(columns={"categoria": "Categoría", "reportes": "Reportes simulados"}),
                                         hide_index=True, use_container_width=True)
        st.caption(f"Base de la simulación: {r.id_reporte.nunique()} reportes F02 dentro de los filtros activos. Un reporte puede seleccionar varias áreas y varias modalidades; por eso las categorías no suman el número de reportes.")
    if is_actions_view:
        st.markdown('<div id="mapa-f02" class="section-kicker">01 · Alcance territorial de los reportes</div><h2>Distribución territorial y georreferenciación</h2>',unsafe_allow_html=True)
        map_controls=st.columns(2)
        with map_controls[0]:
            map_view=st.radio("Vista del mapa",["Cobertura","Georreferenciación"],horizontal=True,key="f02_map_view")
        with map_controls[1]:
            map_level=st.radio("Nivel del análisis territorial",["Estado","Municipio","Parroquia"],index=1,horizontal=True,key="f02_territory_level")
        point_reports=r.groupby(["organizacion","nombre_sitio"]).id_reporte.nunique().reset_index(name="reportes_punto")
        mapped=active.dropna(subset=["latitud","longitud"]).merge(point_reports,on=["organizacion","nombre_sitio"],how="left")
        mapped=mapped[mapped.latitud.between(-2,14)&mapped.longitud.between(-76,-58)].copy()
        mapped["lugar"]=mapped.nombre_sitio
        state_geo=load_estado_geojson()
        if map_view=="Georreferenciación":
            if mapped.empty:
                st.info("Los sitios F02 de esta selección no tienen coordenadas válidas para georreferenciación.")
            else:
                blank_areas=pd.DataFrame([{"join_key":feature["properties"]["join_key"],
                                           "nombre":feature["properties"]["adm1_name"],
                                           "socios":0,"puntos":0} for feature in state_geo["features"]])
                leaflet_partner_map(state_geo,blank_areas,"Estado",markers=mapped,
                                    outline_geo=state_geo,metric="puntos",metric_label="Sitios con reporte",
                                    zoom_to_data=True,marker_only=True)
            st.caption(f"{len(mapped)} sitios con coordenadas F02 visibles. Los reportes sin coordenadas siguen incluidos en los indicadores, pero no se colocan como puntos.")
        else:
            if map_level=="Estado":
                geo=state_geo
                scoped=r.assign(join_key=r.estado)
                areas=pd.DataFrame([{"join_key":feature["properties"]["join_key"],
                                     "nombre":feature["properties"]["adm1_name"]} for feature in geo["features"]])
            elif map_level=="Municipio":
                geo=load_municipio_geojson()
                scoped=r.assign(join_key=r.estado+" | "+r.municipio)
                areas=pd.DataFrame([{"join_key":feature["properties"]["join_key"],
                                     "nombre":feature["properties"]["adm2_name"]+" ("+feature["properties"]["adm1_name"]+")"}
                                    for feature in geo["features"]])
            else:
                parish_rows=r.loc[r.parroquia.ne("No reportada")].copy()
                parish_rows["join_key"]=parish_rows.apply(
                    lambda row:" | ".join(normalize_name(part) for part in
                        (row.estado,row.municipio,row.parroquia)),axis=1)
                geo=load_parroquia_geojson(tuple(sorted(parish_rows.join_key.unique())))
                scoped=parish_rows
                areas=pd.DataFrame([{"join_key":feature["properties"]["join_key"],
                                     "nombre":feature["properties"].get("parroquia","Parroquia")}
                                    for feature in geo["features"]])
            if areas.empty:
                st.info("No hay límites territoriales disponibles para las parroquias declaradas en estos reportes.")
            else:
                counts=scoped.groupby("join_key",as_index=False).agg(
                    reportes=("id_reporte","nunique"),socios=("organizacion","nunique"),
                    puntos=("nombre_sitio","nunique"))
                areas=areas.merge(counts,on="join_key",how="left")
                areas[["reportes","socios","puntos"]]=areas[["reportes","socios","puntos"]].fillna(0).astype(int)
                leaflet_partner_map(geo,areas,map_level,
                                    background_geo=state_geo if map_level=="Parroquia" else None,
                                    outline_geo=state_geo,metric="socios",metric_label="Organizaciones",
                                    zoom_to_data=True)
            st.caption("Cobertura de organizaciones con reportes F02: azul más oscuro indica más socios que reportaron en el territorio; las zonas sin reportes quedan en gris. Use Georreferenciación para ver los sitios con coordenadas.")
    st.markdown(f'<div id="focos-f02" class="section-rule"></div><div class="section-kicker">02 · Resumen de los reportes</div><h2>Reportes y puntos activos por foco de intervención</h2>',unsafe_allow_html=True)
    r_foco=r.assign(foco_formulario=r.foco.map({"Establecimiento de salud":"Acciones en el establecimiento de salud","Salud pública":"Acciones de salud pública"}))
    active_foco=active.assign(foco_formulario=active.foco.map({"Establecimiento de salud":"Acciones en el establecimiento de salud","Salud pública":"Acciones de salud pública"}))
    focus_reports=r_foco.groupby("foco_formulario").id_reporte.nunique().reset_index(name="reportes")
    focus_points=active_foco.groupby("foco_formulario").nombre_sitio.nunique().reset_index(name="puntos")
    focus_orgs=active_foco.groupby("foco_formulario").organizacion.nunique().reset_index(name="socios")
    focus_summary=focus_reports.merge(focus_points,on="foco_formulario",how="outer").merge(focus_orgs,on="foco_formulario",how="outer").fillna(0)
    total_focus_reports=max(float(focus_summary.reportes.sum()),1)
    # Antes se mostraba un número grande (reportes) y, chiquito debajo, otro
    # número distinto (puntos) sin explicar la diferencia — se leía como un
    # error ("aparece 30 y luego 26"). Ahora cada cifra tiene su propia
    # etiqueta, y se agrega cuántas organizaciones (socios) hay en cada foco.
    for focus_name in ["Acciones de salud pública" if is_actions_view else "Acciones en el establecimiento de salud"]:
        row=focus_summary[focus_summary.foco_formulario.eq(focus_name)]
        reports_value=int(row.reportes.iloc[0]) if not row.empty else 0
        points_value=int(row.puntos.iloc[0]) if not row.empty else 0
        share=reports_value/total_focus_reports*100
        st.markdown(f"#### {focus_name}")
        focus_block=[
            ("Reportes periódicos",num(reports_value),f"{share:.1f}% de los reportes"),
            ("Puntos con reporte",num(points_value),"un mismo punto puede reportar más de una vez"),
        ]
        st.markdown('<div class="metric-row" style="grid-template-columns:repeat(2,1fr)">'+''.join(metric(*x) for x in focus_block)+'</div>',unsafe_allow_html=True)
    def _render_focus_reports(mapped_name: str, raw_name: str, color: str, suffix: str, tipo_titulo: str, bucket: str, include_population: bool = False) -> None:
        # Todo lo que corresponde a este foco vive junto en una sola sección
        # (tendencia, rankings, tipos de punto, resultados por unidad y, para
        # salud pública, población alcanzada) — antes estaba repartido en
        # varias secciones separadas que mezclaban ambos focos por columnas.
        subset=r_foco[r_foco.foco_formulario.eq(mapped_name)]
        if subset.empty:
            st.info("No hay reportes para este foco con los filtros seleccionados.")
            return
        if include_population:
            org_rank=(subset.groupby("organizacion").id_reporte.nunique()
                      .nlargest(10).sort_values(ascending=False).reset_index(name="reportes"))
            state_rank=(subset.groupby("estado").id_reporte.nunique()
                        .nlargest(10).sort_values(ascending=False).reset_index(name="reportes"))
            muni_rank=(subset.groupby(["estado","municipio"]).id_reporte.nunique()
                       .nlargest(10).sort_values(ascending=False).reset_index(name="reportes"))
            muni_rank["territorio"]=muni_rank.municipio+" · "+muni_rank.estado
            st.markdown("#### Organizaciones con más reportes")
            fig=px.bar(org_rank,x="reportes",y="organizacion",orientation="h",text="reportes",
                       color_discrete_sequence=[BLUE],category_orders={"organizacion":org_rank.organizacion.tolist()},
                       labels={"organizacion":"","reportes":"Reportes"})
            fig.update_traces(textposition="outside")
            plot(fig,440,"f02_public_org_reports",margin=dict(l=4,r=35,t=10,b=20))
            section_figures.setdefault(bucket,[]).append(fig)
            second_left,second_right=st.columns(2)
            with second_left:
                st.markdown("#### Estados con más reportes")
                fig=px.bar(state_rank,x="reportes",y="estado",orientation="h",text="reportes",
                           color_discrete_sequence=[NAVY],category_orders={"estado":state_rank.estado.tolist()},
                           labels={"estado":"","reportes":"Reportes"})
                fig.update_traces(textposition="outside")
                plot(fig,390,"f02_public_state_reports",margin=dict(l=4,r=35,t=10,b=20))
                section_figures.setdefault(bucket,[]).append(fig)
            with second_right:
                st.markdown("#### Municipios con más reportes")
                fig=px.bar(muni_rank,x="reportes",y="territorio",orientation="h",text="reportes",
                           color_discrete_sequence=[SKY],category_orders={"territorio":muni_rank.territorio.tolist()},
                           labels={"territorio":"","reportes":"Reportes"})
                fig.update_traces(textposition="outside")
                plot(fig,390,"f02_public_muni_reports",margin=dict(l=4,r=35,t=10,b=20))
                section_figures.setdefault(bucket,[]).append(fig)
        # El período de reportes real (barra de fecha) suele cubrir pocas
        # semanas, no varios meses: agrupar por mes colapsaba todo en un
        # solo punto y la "evolución" se veía vacía. Se agrupa por semana
        # cuando el rango cubierto es corto (<60 días) y por mes cuando es
        # más largo, para que la tendencia siempre tenga varios puntos.
        span_days=(subset.fecha_reporte.max()-subset.fecha_reporte.min()).days
        if span_days<60:
            period,period_label="W","Semana"
            trend=subset.assign(bucket=subset.fecha_reporte.dt.to_period(period).apply(lambda p:p.start_time.strftime("%d/%m"))).groupby("bucket").id_reporte.nunique().reset_index(name="reportes")
            trend=trend.rename(columns={"bucket":period_label})
        else:
            period,period_label="M","Mes"
            trend=subset.assign(bucket=subset.fecha_reporte.dt.to_period(period).astype(str)).groupby("bucket").id_reporte.nunique().reset_index(name="reportes")
            trend=trend.rename(columns={"bucket":period_label})
        fig=px.area(trend,x=period_label,y="reportes",markers=True,color_discrete_sequence=[color],labels={period_label:period_label,"reportes":"Reportes enviados"})
        # type="category": si un foco solo tiene reportes en 1-2 periodos,
        # Plotly interpretaba el eje como fecha continua y generaba ticks
        # con hora/segundos en vez de mostrar el periodo limpio.
        fig.update_xaxes(type="category")
        plot(fig,320,f"f02_monthly_{suffix}")
        section_figures.setdefault(bucket, []).append(fig)

        # Tipo de punto reportante antes del detalle de resultados.
        tipo_full=active[active.foco.eq(raw_name)].groupby("tipo_punto").nombre_sitio.nunique().reset_index(name="establecimientos")
        type_palette=[color]+[c for c in PALETTE_EXT if c!=color][:8]
        type_order=sorted(tipo_full.tipo_punto.unique())
        type_color_map=dict(zip(type_order,type_palette))
        if not include_population:
          with st.container(border=True):
            st.markdown(f"#### {tipo_titulo}")
            if tipo_full.empty:
                st.info("No hay establecimientos con reportes para los filtros seleccionados.")
            else:
                total_establishments=int(tipo_full.establecimientos.sum())
                tipo_full["porcentaje"]=100*tipo_full.establecimientos/max(total_establishments,1)
                fig=px.pie(tipo_full,names="tipo_punto",values="establecimientos",hole=.58,
                           color="tipo_punto",color_discrete_map=type_color_map,
                           labels={"tipo_punto":"Tipo de establecimiento","establecimientos":"Establecimientos"})
                fig.update_traces(
                    text=tipo_full.porcentaje.map(lambda value:f"{value:.1f}%" if value>=8 else ""),
                    texttemplate="%{text}",textposition="inside",
                    marker_line_color=PAPER,marker_line_width=3,
                    hovertemplate="<b>%{label}</b><br>%{value} establecimientos · %{percent}<extra></extra>")
                fig.update_layout(
                    annotations=[dict(text=f"<b>{num(total_establishments)}</b><br>establecimientos",
                                      x=.5,y=.5,showarrow=False,font=dict(size=15,color=NAVY))],
                    legend=dict(orientation="v",yanchor="middle",y=.5,xanchor="left",x=1.02,font=dict(size=12)),
                    uniformtext_minsize=11,uniformtext_mode="hide")
                plot(fig,390,f"f02_types_{suffix}",margin=dict(l=20,r=260,t=10,b=15))
                section_figures.setdefault(bucket, []).append(fig)
        # Orden pedido: primero las áreas reportadas (4.1-style, "qué se
        # reportó"), y luego el tipo de apoyo institucional reportado sobre
        # esas áreas (3.11.x, "qué apoyo se dio") — antes iba al revés.
        # Título distinto según el foco: "Salud pública" muestra volumen real
        # con unidad de medida (resultados.csv); "Establecimiento de salud"
        # muestra el simulacro de la pregunta 4.1 real del formulario, con
        # el texto exacto de esa pregunta en vez del título genérico de
        # "unidad de medida" (que no aplica a esta pregunta).
        if raw_name=="Salud pública":
            st.markdown("### Volumen reportado por área y unidad de medida")
            area_subset=res[res.foco.eq(raw_name)]
            areas_by_unit=area_subset.groupby(["area","unidad"],as_index=False).total.sum()
            if areas_by_unit.empty:
                st.info("No hay resultados para este foco con los filtros seleccionados.")
            else:
                # Menos áreas (6 en vez de 8) para que cada bloque quede más
                # grande y legible; antes las más chicas eran franjas casi
                # ilegibles.
                top_areas=areas_by_unit.groupby("area").total.sum().nlargest(6).index
                # Un solo nivel (estilo OEC): un bloque por área. La jerarquía
                # área→unidad se descartó porque Plotly reserva una franja de
                # encabezado propia para cada área-padre y la repite dentro
                # del bloque hijo cuando solo hay una unidad (nombre
                # duplicado), y las unidades minoritarias quedaban en franjas
                # demasiado angostas para su texto (se salía del cuadro). El
                # total por área ya suma sus unidades (se aclara en el
                # caption que no deben interpretarse como la misma unidad).
                areas=areas_by_unit[areas_by_unit.area.isin(top_areas)].groupby("area",as_index=False).total.sum()
                fig=px.treemap(areas,path=["area"],values="total",color="area",
                               color_discrete_sequence=PALETTE_EXT[:8])
                vals=list(fig.data[0].values)
                vmin,vmax=min(vals),max(vals); span=(vmax-vmin) or 1
                grand_total=sum(vals) or 1
                min_size,max_size=13,20
                texts=[]
                for name,val in zip(fig.data[0].labels,vals):
                    pct=val/grand_total*100
                    size=round(min_size+((val-vmin)/span)**0.5*(max_size-min_size))
                    wrapped_name="<br>".join(textwrap.wrap(str(name),width=16,break_long_words=False)) or str(name)
                    texts.append(f'<span style="font-size:{size}px">{wrapped_name}<br>{val:,.0f}<br>{pct:.1f}%</span>')
                fig.update_traces(text=texts,texttemplate="%{text}",textfont_size=max_size,textposition="middle center",
                                   marker_line_color="white",marker_line_width=2,root_color="white")
                plot(fig,480,f"f02_areas_{suffix}",margin=dict(l=16,r=16,t=16,b=16))
                section_figures.setdefault(bucket, []).append(fig)
                st.caption("Cada bloque muestra su total y el porcentaje del total general. Unidad de medida por serie: personas, casos, procedimientos, kits, profesionales o instituciones (no se repite en cada bloque; no deben sumarse entre sí como si fueran la misma unidad).")

            # Segunda visualización: detalle por indicador real dentro de un
            # área — cada área del F02 real (5.1, 5.2…) agrupa varios
            # indicadores específicos (5.1.1, 5.1.2…), que el cuadro de
            # arriba no puede mostrar sin amontonarse. Se elige un área y se
            # ve su desglose real por indicador (dato 100% de resultados.csv,
            # no simulado).
            st.markdown("#### Detalle por indicador dentro de un área")
            area_options=sorted(area_subset.area.unique()) if not area_subset.empty else []
            if not area_options:
                st.info("No hay áreas para mostrar el detalle.")
            else:
                selected_area=st.selectbox("Seleccione un área temática para ver el detalle",area_options,key=f"area_detail_{suffix}")
                detail=area_subset[area_subset.area.eq(selected_area)].groupby(["indicador","unidad"],as_index=False).agg(
                    total=("total","sum"),mujeres=("mujeres","sum"),hombres=("hombres","sum"),
                    ninas=("ninas","sum"),ninos=("ninos","sum"),
                )
                if detail.empty:
                    st.info("No hay indicadores para esta área con los filtros seleccionados.")
                else:
                    detail["etiqueta"]=detail.indicador.str.replace(r"^Número de ","",regex=True).str.slice(0,70)
                    order=detail.sort_values("total",ascending=False).etiqueta.tolist()
                    # Barras apiladas por sexo/edad cuando el indicador es de
                    # unidad "personas" y trae desagregación real; el resto
                    # (sin desagregar, o en otra unidad — casos,
                    # procedimientos, kits...) se muestra como un solo
                    # segmento con la etiqueta de su unidad real, para no
                    # inventar un desglose por género que el dato no tiene.
                    is_personas=detail.unidad.str.lower().eq("personas")
                    gender_cols=["mujeres","hombres","ninas","ninos"]
                    detail.loc[~is_personas,gender_cols]=0
                    resto=(detail.total-detail[gender_cols].sum(axis=1)).clip(lower=0)
                    detail["resto"]=resto.where(is_personas,detail.total)
                    detail["resto_etiqueta"]=detail.unidad.str.capitalize().where(~is_personas,"Sin desagregar por sexo/edad")
                    rows=[]
                    for _,row in detail.iterrows():
                        for col,label in zip(gender_cols,["Mujeres","Hombres","Niñas","Niños"]):
                            if row[col]>0:
                                rows.append({"etiqueta":row.etiqueta,"grupo":label,"total":row[col]})
                        if row.resto>0:
                            rows.append({"etiqueta":row.etiqueta,"grupo":row.resto_etiqueta,"total":row.resto})
                    melted=pd.DataFrame(rows)
                    # Segmentos muy angostos (barra chica) no muestran el
                    # número adentro — a ese ancho el texto queda cortado o
                    # superpuesto con el del segmento vecino; mejor omitirlo
                    # que mostrarlo ilegible (el valor real sigue disponible
                    # al pasar el cursor).
                    chart_max=melted.groupby("etiqueta").total.sum().max() or 1
                    melted["texto"]=melted.total.map(lambda v: f"{v:,.0f}" if v/chart_max>=0.025 else "")
                    base_colors={"Mujeres":MUJERES_RED,"Hombres":BLUE,"Niñas":tint(MUJERES_RED,.45),"Niños":SKY}
                    extra_groups=sorted(set(melted.grupo)-set(base_colors))
                    extra_palette=[GRAY,tint(NAVY,.55),shade(GRAY,.2),tint(GRAY,.55)]
                    detail_colors={**base_colors,**{g:extra_palette[i%len(extra_palette)] for i,g in enumerate(extra_groups)}}
                    group_order=["Mujeres","Hombres","Niñas","Niños"]+extra_groups
                    fig=px.bar(melted,x="total",y="etiqueta",color="grupo",orientation="h",text="texto",barmode="stack",
                               category_orders={"etiqueta":order,"grupo":group_order},
                               color_discrete_map=detail_colors,
                               hover_data={"total":True,"texto":False},
                               labels={"total":"Total reportado","etiqueta":"","grupo":""})
                    fig.update_traces(textposition="inside",insidetextanchor="middle",textfont=dict(color="white",size=11))
                    fig.update_layout(legend=dict(orientation="h",yanchor="bottom",y=1.02,xanchor="right",x=1))
                    plot(fig,max(360,34*len(detail)+90),f"f02_area_detail_{suffix}",margin=dict(l=4,r=54,t=44,b=18))
                    section_figures.setdefault(bucket, []).append(fig)
                    st.caption(f"Indicadores reales de F02 dentro de «{selected_area}» (resultados.csv). Los indicadores de personas se dividen por sexo/edad cuando el formulario trae esa desagregación; los que no la traen, o están en otra unidad (casos, procedimientos, kits…), se muestran como un solo segmento con su unidad real.")
        if raw_name=="Establecimiento de salud":
            st.markdown("### Establecimientos con reportes F02")
            ranking=(subset.groupby(["organizacion","nombre_sitio"],as_index=False)
                     .id_reporte.nunique().rename(columns={
                         "organizacion":"Organización","nombre_sitio":"Establecimiento",
                         "id_reporte":"Reportes F02"}).sort_values("Reportes F02",ascending=False))
            st.dataframe(ranking,hide_index=True,width="stretch")

        if include_population:
            st.markdown("### Composición de las acciones realizadas por género")
            gender_labels={"mujeres":"Mujeres","hombres":"Hombres","ninas":"Niñas","ninos":"Niños"}
            gender_colors={"Mujeres":MUJERES_RED,"Hombres":BLUE,"Niñas":tint(MUJERES_RED,.45),"Niños":SKY}
            people=res[res.foco.eq(raw_name) & res.unidad.str.lower().eq("personas") & res.tiene_desagregacion.eq(1)].copy()
            if people.empty:
                st.info("No hay personas desagregadas para este foco con los filtros seleccionados.")
            else:
                gender=people.groupby("area")[["mujeres","hombres","ninas","ninos"]].sum().reset_index().melt(id_vars="area",var_name="grupo",value_name="personas")
                gender["grupo"]=gender.grupo.map(gender_labels)
                # La dona solo suma sexo/edad (mujeres, hombres, niñas,
                # niños): son categorías mutuamente excluyentes que sí suman
                # el 100% de las personas atendidas. "Discapacidad" NO se
                # agrega aquí — es una característica que puede aplicar a
                # personas YA contadas en cualquiera de esos 4 grupos, así
                # que sumarla como una quinta porción duplicaría personas en
                # el total. Se muestra aparte, como cifra independiente.
                totals_gender=gender.groupby("grupo",as_index=False).personas.sum()
                disability_total=people.discapacidad.sum()
                gp_l,gp_r=st.columns(2)
                with gp_l:
                    fig=px.pie(totals_gender,names="grupo",values="personas",hole=.6,color="grupo",color_discrete_map=gender_colors)
                    fig.update_traces(textinfo="percent+label")
                    plot(fig,420,f"gender_donut_{suffix}")
                    section_figures.setdefault(bucket, []).append(fig)
                    if disability_total>0:
                        st.caption(f"Adicionalmente, {num(disability_total)} de estas personas (ya incluidas arriba en su grupo de sexo/edad) tienen discapacidad — no se suma aparte para no duplicar el total.")
                with gp_r:
                    # Filtro en vez de selección por clic en la dona (el
                    # clic no disparaba el rerun de forma confiable): elige
                    # qué grupo mostrar en esta misma gráfica de áreas.
                    # "Discapacidad" no es una opción aquí: no es un grupo de
                    # sexo/edad, es una característica aparte (ya aclarada en
                    # el caption de la dona) — mezclarla en este mismo filtro
                    # la haría ver como un grupo más, cuando no lo es.
                    group_options=["Todos"]+totals_gender.grupo.tolist()
                    selected_group=st.selectbox("Filtrar por grupo",group_options,key=f"gender_filter_{suffix}")
                    if selected_group=="Todos":
                        top=gender.groupby("area").personas.sum().nlargest(6).index; gender_top=gender[gender.area.isin(top)]
                        fig=px.bar(gender_top,x="personas",y="area",color="grupo",orientation="h",barmode="stack",category_orders={"area":top.tolist()},color_discrete_map=gender_colors,labels={"personas":"Personas","area":"","grupo":""})
                        # Sin leyenda propia: ya está la de la dona a la
                        # izquierda con los mismos colores por grupo.
                        fig.update_layout(showlegend=False)
                        plot(fig,420,f"gender_stack_{suffix}",margin=dict(l=4,r=24,t=12,b=18))
                        section_figures.setdefault(bucket, []).append(fig)
                    else:
                        col={"Mujeres":"mujeres","Hombres":"hombres","Niñas":"ninas","Niños":"ninos"}[selected_group]
                        top_group=people.groupby("area")[col].sum().sort_values(ascending=False).head(6).sort_values().reset_index(name="personas")
                        if top_group.personas.sum()==0:
                            st.info(f"No hay personas reportadas en «{selected_group}» para estos filtros.")
                        else:
                            fig=px.bar(top_group,x="personas",y="area",orientation="h",text="personas",color_discrete_sequence=[gender_colors[selected_group]],labels={"personas":f"Personas ({selected_group.lower()})","area":""}); fig.update_traces(textposition="outside")
                            plot(fig,420,f"gender_detail_{suffix}",margin=dict(l=4,r=24,t=12,b=18))
                            section_figures.setdefault(bucket, []).append(fig)

    if is_actions_view:
        st.markdown('<div id="reportes-acciones-f02" class="section-rule"></div><div class="section-kicker">03 · Reportes de acciones de salud pública</div><h2>Evolución y alcance de los reportes de acciones de salud pública</h2>',unsafe_allow_html=True)
        _render_focus_reports("Acciones de salud pública", "Salud pública", YELLOW, "pub", "Tipos de lugar de intervención", "Reportes de acciones de salud pública", include_population=True)
    else:
        st.markdown('<div id="reportes-establecimientos-f02" class="section-rule"></div><div class="section-kicker">03 · Reportes de establecimientos de salud</div><h2>Evolución y alcance de los reportes de establecimientos de salud</h2>',unsafe_allow_html=True)
        _render_focus_reports("Acciones en el establecimiento de salud", "Establecimiento de salud", RED, "est", "Tipos de establecimiento de salud", "Reportes de establecimientos de salud")

    st.markdown(f'<div id="reportantes-f02" class="section-rule"></div><div class="section-kicker">04 · Socios que reportaron</div>',unsafe_allow_html=True)
    st.markdown("### Socios que reportaron y puntos de intervención")
    # Barras apiladas por foco (no un solo color) para poder ver, dentro del
    # total de cada organización, cuánto corresponde a establecimientos de
    # salud y cuánto a salud pública.
    org_points_by_focus=active.assign(foco_formulario=active.foco.map({"Establecimiento de salud":"Acciones en el establecimiento de salud","Salud pública":"Acciones de salud pública"})).groupby(["organizacion","foco_formulario"]).nombre_sitio.nunique().reset_index(name="puntos")
    if org_points_by_focus.empty:
        st.info("No hay organizaciones con reportes para los filtros seleccionados.")
    else:
        # Orden descendente (mayor arriba, como un ranking editorial) — antes
        # quedaba ascendente y el más chico terminaba arriba.
        org_order_full=org_points_by_focus.groupby("organizacion").puntos.sum().sort_values(ascending=False).index.tolist()

        def _f02_org_bar(names: list, key: str):
            rows=org_points_by_focus[org_points_by_focus.organizacion.isin(names)].copy()
            # Segmentos de 1-2 puntos no llevan número: a ese ancho el texto
            # queda apretado contra el borde de la barra y se ve mal.
            rows["etiqueta"]=rows.puntos.map(lambda v: str(v) if v>=3 else "")
            fig=px.bar(rows,x="puntos",y="organizacion",color="foco_formulario",orientation="h",barmode="stack",
                       category_orders={"organizacion":names},
                       color_discrete_map={"Acciones en el establecimiento de salud":RED,"Acciones de salud pública":YELLOW},
                       text="etiqueta",labels={"puntos":"Puntos con reporte","organizacion":"","foco_formulario":"Foco"})
            fig.update_traces(textposition="inside",insidetextanchor="middle",textfont=dict(color="white",size=14),constraintext="none")
            fig.update_layout(bargap=.35,legend=dict(orientation="h",yanchor="bottom",y=1.02,xanchor="right",x=1))
            plot(fig,max(420,54*len(names)+90),key,margin=dict(l=4,r=54,t=52,b=18))
            section_figures.setdefault("Resumen territorial", []).append(fig)

        top10,rest=org_order_full[:10],org_order_full[10:]
        _f02_org_bar(top10,"f02_org_points")
        if rest:
            with st.expander(f"Ver las {len(rest)} organizaciones restantes"):
                _f02_org_bar(rest,"f02_org_points_rest")
        # Verificación explícita: el número de organizaciones en esta
        # gráfica debe coincidir con la tarjeta "Organizaciones reportantes"
        # del resumen, ya que ambas cuentan sobre el mismo universo
        # filtrado (r/active).
        rest_note=f"las {len(rest)} restantes se pueden ver en el desplegable." if rest else "incluye todas las organizaciones reportantes."
        st.caption(f"Cuenta puntos distintos (no reportes); una organización con varios puntos aparece una sola vez, con sus puntos divididos entre los dos focos. Top 10 organizaciones por total de puntos, ordenadas de mayor a menor; {rest_note}")


    report_palette={"navy":NAVY,"muted":MUTED,"ink":INK,"blue":BLUE,"border":tint(GRAY,.75)}
    top_orgs_f02=org_points_by_focus.groupby("organizacion").puntos.sum().nlargest(12).sort_values()
    area_volume=res.groupby("area",as_index=False).total.sum().nlargest(12,"total").sort_values("total")
    top_states_f02=r.groupby("estado").id_reporte.nunique().nlargest(12).sort_values()
    top_munis_f02=r.groupby("municipio").id_reporte.nunique().nlargest(12).sort_values()
    reports_by_focus=focus_summary.set_index("foco_formulario")["reportes"].sort_values()
    totals_by_unit=res.groupby("unidad").total.sum().nlargest(12).sort_values()
    population_totals=res[["mujeres","hombres","ninas","ninos"]].sum().rename({"mujeres":"Mujeres","hombres":"Hombres","ninas":"Niñas","ninos":"Niños"})
    disability_by_area=res.groupby("area").discapacidad.sum().nlargest(12).sort_values()
    report_bytes=build_report(
        title="Tablero de la Respuesta en Salud",
        subtitle=f"{module} (F02)",
        scope_text="Universo de reportes vigente según los filtros de estado, municipio, organización, foco y rango de fecha activos.",
        as_of_text=f"Generado el {pd.Timestamp.today().strftime('%d/%m/%Y')}",
        kpis=[(c[0],c[1]) for c in cards],
        sections=[
            {
                "title": "Organizaciones y distribución por foco",
                "rows": [[
                    ("Top organizaciones por puntos con reporte",bar_chart(top_orgs_f02.index.tolist(),top_orgs_f02.tolist(),NAVY,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=190,width=350,label_width=140),12.7),
                    ("Distribución por foco de intervención",composition_bar(focus_points.foco_formulario.tolist(),focus_points.puntos.tolist(),[RED,YELLOW],MUTED,width=350,height=115),12.7),
                ]],
                "caption": "Cuenta puntos distintos (no reportes); una organización con varios puntos aparece una sola vez.",
            },
            {
                "title": "Volumen reportado por área temática",
                "rows": [[
                    ("Top áreas por total reportado",bar_chart(area_volume.area.tolist(),area_volume.total.tolist(),ORANGE,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=190,width=735,label_width=180),26.0),
                ]],
                "caption": "Suma de indicadores por área (personas, casos, procedimientos, kits, profesionales o instituciones); no deben sumarse entre sí como si fueran la misma unidad.",
            },
            {
                "title": "Distribución territorial de los reportes",
                "rows": [[
                    ("Estados con más reportes",bar_chart(top_states_f02.index.tolist(),top_states_f02.tolist(),RED,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=210,width=350,label_width=115),12.7),
                    ("Municipios con más reportes",bar_chart(top_munis_f02.index.tolist(),top_munis_f02.tolist(),ORANGE,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=210,width=350,label_width=125),12.7),
                ]],
                "caption": "Cuenta formularios F02 recibidos dentro del alcance de los filtros activos.",
            },
            {
                "title": "Composición de los resultados reportados",
                "rows": [[
                    ("Reportes periódicos por foco",bar_chart(reports_by_focus.index.tolist(),reports_by_focus.tolist(),RED,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=180,width=350,label_width=145),12.7),
                    ("Resultados por unidad de medida",bar_chart(totals_by_unit.index.tolist(),totals_by_unit.tolist(),BLUE,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=180,width=350,label_width=125),12.7),
                ]],
                "caption": "Las unidades se presentan separadas; no deben sumarse como si representaran una misma medida.",
            },
            {
                "title": "Población reportada",
                "rows": [[
                    ("Composición por sexo y grupo de edad",composition_bar(population_totals.index.tolist(),population_totals.tolist(),[RED,BLUE,tint(RED,.45),SKY],MUTED,width=350,height=115),12.7),
                    ("Personas con discapacidad por acción",bar_chart(disability_by_area.index.tolist(),disability_by_area.tolist(),TEAL,MUTED,tint(GRAY,.75),tint(GRAY,.85),height=210,width=350,label_width=145),12.7),
                ]],
                "caption": "Las cifras son sumas de indicadores y no representan personas únicas. La discapacidad es un subconjunto transversal.",
            },
        ],
        palette=report_palette,
    )
    complete_dashboard_export()

# El índice vive en la barra lateral; esta pequeña observación solo cambia
# el resaltado visual cuando el usuario recorre las secciones de la página.
components.html("""
<script>
(() => {
  try {
    const doc = window.parent.document;
    const links = [...doc.querySelectorAll('.sidebar-index a[data-section]')];
    if (!links.length) return;
    const sections = links.map(link => ({link, node: doc.getElementById(link.dataset.section)}))
                          .filter(item => item.node);
    if (!sections.length) return;
    let pending = false;
    const update = () => {
      pending = false;
      let current = sections[0];
      for (const section of sections) {
        if (section.node.getBoundingClientRect().top <= 145) current = section;
      }
      for (const section of sections) {
        const active = section === current;
        section.link.classList.toggle('active', active);
        if (active) section.link.setAttribute('aria-current', 'location');
        else section.link.removeAttribute('aria-current');
      }
    };
    const schedule = () => {
      if (!pending) { pending = true; window.parent.requestAnimationFrame(update); }
    };
    doc.addEventListener('scroll', schedule, true);
    window.parent.addEventListener('resize', schedule);
    update();
  } catch (error) { /* El tablero sigue siendo navegable si el navegador aísla el componente. */ }
})();
</script>
""", height=0)
