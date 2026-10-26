"""GIS Comercial · gera os dados de um município (mesmo método dos municípios do CONIAPE).

    python gerar_municipio.py <código IBGE> [--saida arq.json] [--cache pasta]

1. Setores censitários 2022 do IBGE (arquivo da UF) -> manchas urbanas (CD_SIT 1, 2 ou 3), por distrito
2. Vias do OpenStreetMap (Overpass) transitáveis por veículo, só dentro das manchas
3. Recorte das vias nas manchas e medição em UTM (SIRGAS 2000, fuso do município)
4. JSON no formato que o mapa lê, com a procedência de cada fonte (para a verificação de atualização)
"""
import argparse, json, math, sys, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import requests
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon, box, mapping, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep

VERSAO = 1
UA = {"User-Agent": "GIS-Comercial/1.0 (TECGEO; geoprocessamento)"}
IBGE_SETORES = ("https://geoftp.ibge.gov.br/organizacao_do_territorio/malhas_territoriais/"
                "malhas_de_setores_censitarios__divisoes_intramunicipais/censo_2022/setores/gpkg/UF")
IBGE_API = "https://servicodados.ibge.gov.br/api"
# servidores públicos do Overpass oscilam (o principal chegou a ficar fora do ar): tenta vários, em rodízio
OVERPASS = ("https://overpass.openstreetmap.fr/api/interpreter", "https://z.overpass-api.de/api/interpreter",
            "https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter")
# vias que um veículo percorre (mesmo conjunto usado nos 39 municípios revisados)
TIPOS = ("motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link|"
         "tertiary|tertiary_link|unclassified|residential|living_street|service|road|track")
TAGS = ("highway", "name", "service", "access", "motor_vehicle", "vehicle")
TILE = 0.05          # graus (~5,5 km): cada consulta ao Overpass cobre um quadrado deste tamanho (áreas urbanas)
TILE_RURAL = 0.1     # graus (~11 km): quadrados maiores nas áreas rurais, onde as vias são mais esparsas
MAX_TILES_RURAIS = 150  # município enorme (ex.: Amazônia): não gera vias rurais, para não sobrecarregar o Overpass
MIN_MANCHA_M2 = 200  # descarta migalhas de polígono


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def agora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------------------------------------------------ IBGE
LISTA_LOCAL = Path(__file__).resolve().parent.parent / "dados" / "municipios_ibge.json"   # [código, nome, UF]


def _get_ibge(url, tentativas=4):
    """GET na API do IBGE com novas tentativas (o servidor oscila e às vezes nem aceita a conexão)."""
    for t in range(tentativas):
        try:
            r = requests.get(url, headers=UA, timeout=(20, 60))
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if t == tentativas - 1:
                raise
            log(f"  IBGE não respondeu ({type(e).__name__}); nova tentativa em {10 * (t + 1)} s")
            time.sleep(10 * (t + 1))


def meta_ibge(cd):
    try:
        j = _get_ibge(f"{IBGE_API}/v1/localidades/municipios/{cd}").json()
    except requests.RequestException:
        # API fora do ar: nome e UF vêm da lista do repositório; as regiões ficam em branco
        for c, nome, uf in json.loads(LISTA_LOCAL.read_text(encoding="utf-8")):
            if str(c) == str(cd):
                log("  API de localidades do IBGE indisponível: usando nome e UF da lista local")
                return {"nome": nome, "uf": uf, "rgi": "", "rgint": ""}
        raise
    if not j:
        raise ValueError(f"Município {cd} não existe no IBGE")
    j = j[0] if isinstance(j, list) else j
    ri = j.get("regiao-imediata") or {}
    rint = ri.get("regiao-intermediaria") or {}
    uf = (rint.get("UF") or {}).get("sigla") or j["microrregiao"]["mesorregiao"]["UF"]["sigla"]
    return {"nome": j["nome"], "uf": uf, "rgi": ri.get("nome", ""), "rgint": rint.get("nome", "")}


def area_oficial(cd):
    """Área territorial oficial (km²) pela API de agregados do IBGE; None se a API não responder."""
    try:
        r = _get_ibge(f"{IBGE_API}/v3/agregados/1301/periodos/-1/variaveis/615?localidades=N6[{cd}]", tentativas=3)
        s = r.json()[0]["resultados"][0]["series"][0]["serie"]
        return float(list(s.values())[-1])
    except Exception:
        return None


def baixar_setores(uf, cache):
    """Arquivo de setores da UF (reaproveita o do cache se tiver o mesmo tamanho e data)."""
    url = f"{IBGE_SETORES}/{uf}/{uf}_setores_CD2022.gpkg"
    h = requests.head(url, headers=UA, timeout=60, allow_redirects=True)
    h.raise_for_status()
    fonte = {"url": url, "last_modified": h.headers.get("Last-Modified"),
             "content_length": int(h.headers["Content-Length"]), "etag": h.headers.get("ETag")}
    dest = Path(cache) / f"{uf}_setores_CD2022.gpkg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta = dest.with_suffix(".meta.json")
    if dest.exists() and dest.stat().st_size == fonte["content_length"] and meta.exists() \
            and json.loads(meta.read_text()).get("last_modified") == fonte["last_modified"]:
        log(f"setores {uf}: usando o arquivo em cache ({dest.stat().st_size / 1e6:.0f} MB)")
        return dest, fonte
    log(f"setores {uf}: baixando {fonte['content_length'] / 1e6:.0f} MB")
    for t in range(6):
        try:
            ja = dest.stat().st_size if dest.exists() else 0
            if ja == fonte["content_length"]:
                break                      # já completo (ex.: baixado antes, sem o registro de data)
            if ja > fonte["content_length"]:
                dest.unlink(); ja = 0
            cab = dict(UA, Range=f"bytes={ja}-") if ja else UA
            with requests.get(url, headers=cab, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(dest, "ab" if ja else "wb") as f:
                    for bloco in r.iter_content(1 << 20):
                        f.write(bloco)
            if dest.stat().st_size == fonte["content_length"]:
                break
        except requests.RequestException as e:
            log("  falhou:", e, "- tentando de novo"); time.sleep(5 * (t + 1))
    else:
        raise RuntimeError("Não consegui baixar os setores do IBGE")
    meta.write_text(json.dumps(fonte))
    return dest, fonte


# ------------------------------------------------------------------ geometria
def epsg_utm(lon):
    zona = min(25, max(18, int((lon + 180) // 6) + 1))   # SIRGAS 2000 / UTM 18S..25S = EPSG 31978..31985
    return 31960 + zona


def arredondar(coords, nd):
    if isinstance(coords[0], (int, float)):
        return [round(coords[0], nd), round(coords[1], nd)]
    return [arredondar(c, nd) for c in coords]


def poligonos(g):
    return [g] if isinstance(g, Polygon) else [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]


def partes_linha(g):
    if g.is_empty:
        return []
    if isinstance(g, LineString):
        return [g]
    if isinstance(g, MultiLineString) or hasattr(g, "geoms"):
        return [p for x in g.geoms for p in partes_linha(x)]
    return []


def manchas_urbanas(urb_utm):
    """Uma mancha por componente contínuo de setores urbanos de cada distrito, da maior para a menor."""
    saida = []
    for (_cd, nome), sub in urb_utm.groupby(["CD_DIST", "NM_DIST"], sort=True):
        u = unary_union(list(sub.geometry)).buffer(1).buffer(-1)   # fecha frestas de ~1 m entre setores
        partes = sorted([p for p in poligonos(u) if p.area >= MIN_MANCHA_M2], key=lambda p: -p.area)
        for i, p in enumerate(partes, 1):
            saida.append({"nome": nome if len(partes) == 1 else f"{nome} ({i})", "geom": p})
    return saida


# ------------------------------------------------------------------ OpenStreetMap
def overpass(consulta, tentativas=5):
    ultimo = None
    for t in range(tentativas):
        for ep in OVERPASS:
            try:
                r = requests.post(ep, data={"data": consulta}, headers=UA, timeout=(15, 200))   # (conexão, leitura)
                if r.status_code == 200:
                    return r.json(), ep
                ultimo = f"HTTP {r.status_code} em {ep}"
            except (requests.RequestException, ValueError) as e:
                ultimo = f"{type(e).__name__} em {ep}"
        log("  overpass:", ultimo, "- aguardando"); time.sleep(10 * (t + 1))
    raise RuntimeError(f"Overpass indisponível ({ultimo})")


def tiles_de(geoms_wgs, passo):
    """Quadrados de `passo` graus que tocam alguma das geometrias (não consulta o vazio entre manchas distantes)."""
    achados = set()
    for g in geoms_wgs:
        for p in poligonos(g):
            pp = prep(p)
            w, s, e, n = p.bounds
            for i in range(math.floor(w / passo), math.floor(e / passo) + 1):
                for j in range(math.floor(s / passo), math.floor(n / passo) + 1):
                    if pp.intersects(box(i * passo, j * passo, (i + 1) * passo, (j + 1) * passo)):
                        achados.add((i, j))
    return sorted(achados)


def baixar_vias(manchas_wgs, rural_wgs=None):
    """Vias OSM transitáveis dos quadrados que tocam as manchas urbanas e (se informado) a área rural."""
    trabalho = [(TILE, t) for t in tiles_de(manchas_wgs, TILE)]
    if rural_wgs:
        trabalho += [(TILE_RURAL, t) for t in tiles_de(rural_wgs, TILE_RURAL)]
    log(f"vias OSM: {len(trabalho)} consulta(s) ao Overpass")
    vias, bases, ep_usado = {}, [], None
    for k, (passo, (i, j)) in enumerate(trabalho, 1):
        s, w, n, e = j * passo, i * passo, (j + 1) * passo, (i + 1) * passo
        q = f'[out:json][timeout:180];way["highway"~"^({TIPOS})$"]({s:.5f},{w:.5f},{n:.5f},{e:.5f});out tags geom;'
        dados, ep_usado = overpass(q)
        bases.append((dados.get("osm3s") or {}).get("timestamp_osm_base"))
        for el in dados.get("elements", []):
            if el.get("geometry"):
                vias[el["id"]] = el
        log(f"  consulta {k}/{len(trabalho)}: {len(dados.get('elements', []))} vias")
        time.sleep(1.2)
    bases = [b for b in bases if b]
    fonte = {"base": min(bases) if bases else None, "consultado_em": agora(), "endpoint": ep_usado,
             "tiles": len(trabalho), "vias_total": len(vias)}
    if manchas_wgs:     # área onde a conferência de atualização procura mudanças (só a urbana)
        fonte["bbox"] = [round(min(m.bounds[0] for m in manchas_wgs), 5), round(min(m.bounds[1] for m in manchas_wgs), 5),
                         round(max(m.bounds[2] for m in manchas_wgs), 5), round(max(m.bounds[3] for m in manchas_wgs), 5)]
    return vias, fonte


def motivo_exclusao(t):
    if t.get("access") in ("private", "no"):
        return "acesso privado/proibido"
    if t.get("motor_vehicle") in ("no", "private") or t.get("vehicle") in ("no", "private"):
        return "proibido a veículos"
    if t.get("service") in ("driveway", "parking_aisle", "drive-through"):
        return f"service={t['service']}"
    return None


def _limpar(g):
    """Polígono válido, sem migalhas."""
    g = g.buffer(0)
    ps = [p for p in poligonos(g) if p.area >= MIN_MANCHA_M2]
    return unary_union(ps) if ps else Polygon()


# ------------------------------------------------------------------ geração
def gerar(cd, cache, saida=None, exportar_em=None, reutilizar=None):
    """Gera o município. exportar_em: pasta onde gravar SHP/KML/KMZ. reutilizar: pacote já publicado; mantém a parte
    urbana (números e vias) e só acrescenta a área rural, as vias rurais e os arquivos."""
    from exportar import exportar as exportar_arquivos
    cd = str(cd).zfill(7)
    meta = meta_ibge(cd)
    log(f"{meta['nome']} - {meta['uf']} ({cd})" + ("  [reaproveitando a parte urbana]" if reutilizar else ""))
    gpkg, fonte_setores = baixar_setores(meta["uf"], cache)

    setores = gpd.read_file(gpkg, where=f"CD_MUN = '{cd}'", engine="pyogrio")
    if setores.empty:
        raise ValueError("Nenhum setor censitário encontrado para este município")
    lon_c = float(setores.geometry.union_all().centroid.x)
    epsg = epsg_utm(lon_c)
    if reutilizar and reutilizar.get("crs_medicao"):
        epsg = int(str(reutilizar["crs_medicao"]).split(":")[1])
    s_utm = setores.to_crs(epsg)
    to_wgs = Transformer.from_crs(epsg, 4326, always_xy=True).transform
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True).transform

    urb = s_utm[s_utm["CD_SIT"].isin(["1", "2", "3"])]
    rur = s_utm[~s_utm["CD_SIT"].isin(["1", "2", "3"])]
    territorio = unary_union(list(s_utm.geometry)).buffer(1).buffer(-1)
    if reutilizar:      # as manchas publicadas (as mesmas da tela)
        manchas = [{"nome": u["n"], "geom": transform(to_utm, shape(u["g"]))} for u in reutilizar["urb"]]
    else:
        manchas = manchas_urbanas(urb)
    log(f"{len(urb)} setores urbanos -> {len(manchas)} mancha(s); {len(rur)} rurais")
    if not manchas:
        raise ValueError("O município não tem setores urbanos (CD_SIT 1, 2 ou 3)")

    polys_utm = [m["geom"] for m in manchas]
    polys_wgs = [transform(to_wgs, p) for p in polys_utm]
    uniao_utm = unary_union(polys_utm)
    arvore = STRtree(polys_utm)
    rural_utm = _limpar(territorio.difference(uniao_utm))
    rural_wgs = [transform(to_wgs, rural_utm)] if not rural_utm.is_empty else []
    vias_rurais_geradas = True
    if rural_wgs:
        n_rurais = len(tiles_de(rural_wgs, TILE_RURAL))
        if n_rurais > MAX_TILES_RURAIS:
            log(f"área rural grande demais ({n_rurais} consultas): as vias rurais não serão geradas")
            rural_wgs, vias_rurais_geradas = [], False

    vias_osm, fonte_osm = baixar_vias([] if reutilizar else polys_wgs, rural_wgs)

    nomes, idx_nome, tipos, idx_tipo = [], {}, [], {}
    segs = []                                  # [índice do nome, km, [x,y,x,y...], índice do tipo]
    reg_urb, reg_rur = [], []
    km_mancha, n_mancha = defaultdict(float), defaultdict(int)
    km_tipo, excl, excl_rur = defaultdict(float), defaultdict(float), defaultdict(float)
    cols = {"CD_MUN": cd, "NM_MUN": meta["nome"]}

    if reutilizar:      # vias urbanas já publicadas: viram camada sem recalcular
        nomes_pub = reutilizar.get("viaNames", [])
        tipos_pub = reutilizar.get("tipos") or []
        for ni, km, flat, *resto in reutilizar["vias"]:
            linha = transform(to_utm, LineString(list(zip(flat[0::2], flat[1::2]))))
            ponto = linha.interpolate(.5, normalized=True)
            ks = [k for k in arvore.query(ponto) if polys_utm[k].contains(ponto)] or [int(arvore.nearest(ponto))]
            nome_m = manchas[ks[0]]["nome"]
            tipo = tipos_pub[resto[0]] if resto and resto[0] < len(tipos_pub) else ""
            km_real = linha.length / 1000      # mede pela geometria: o km guardado nos pacotes antigos repetia o da via inteira nas partes
            reg_urb.append({**cols, "NOME": nomes_pub[ni], "TIPO": tipo, "COMP_KM": round(km_real, 3), "MANCHA": nome_m, "geometry": linha})
            km_mancha[nome_m] += km_real
        segs = reutilizar["vias"]

    rural_prep = prep(rural_utm) if not rural_utm.is_empty else None
    for el in vias_osm.values():
        tags = el.get("tags") or {}
        linha = transform(to_utm, LineString([(p["lon"], p["lat"]) for p in el["geometry"]]))
        motivo = motivo_exclusao(tags)
        nome = tags.get("name") or tags.get("highway") or ""
        tipo = tags.get("highway", "")
        if not reutilizar:                                      # urbanas (cálculo completo)
            for k in arvore.query(linha, predicate="intersects"):
                for parte in partes_linha(linha.intersection(polys_utm[k])):
                    km = parte.length / 1000
                    if km <= 0:
                        continue
                    if motivo:
                        excl[motivo] += km
                        continue
                    if nome not in idx_nome:
                        idx_nome[nome] = len(nomes); nomes.append(nome)
                    if tipo not in idx_tipo:
                        idx_tipo[tipo] = len(tipos); tipos.append(tipo)
                    flat = []
                    for x, y in transform(to_wgs, parte).coords:
                        flat += [round(x, 5), round(y, 5)]
                    segs.append([idx_nome[nome], round(km, 3), flat, idx_tipo[tipo]])
                    reg_urb.append({**cols, "NOME": nome, "TIPO": tipo, "COMP_KM": round(km, 3), "MANCHA": manchas[k]["nome"], "geometry": parte})
                    km_mancha[manchas[k]["nome"]] += km; n_mancha[manchas[k]["nome"]] += 1
                    km_tipo[tipo] += km
        if rural_prep and rural_prep.intersects(linha):         # rurais
            for parte in partes_linha(linha.intersection(rural_utm)):
                km = parte.length / 1000
                if km <= 0:
                    continue
                if motivo:
                    excl_rur[motivo] += km
                    continue
                reg_rur.append({**cols, "NOME": nome, "TIPO": tipo, "COMP_KM": round(km, 3), "geometry": parte})

    area_terr = territorio.area / 1e6
    area_rural_geo = rural_utm.area / 1e6 if not rural_utm.is_empty else 0.0
    km_rural = sum(r["COMP_KM"] for r in reg_rur)
    crs = f"EPSG:{epsg}"
    if reutilizar:
        pacote = json.loads(json.dumps(reutilizar))
        pacote["mun"].update({"viasRur": round(km_rural, 2) if vias_rurais_geradas else None, "areaRur": round(area_rural_geo, 2)})
        pacote.setdefault("fontes", {})["osm_rural"] = {k: fonte_osm[k] for k in ("base", "consultado_em", "endpoint", "tiles")}
        pacote["excluidas_rurais_km"] = {k: round(v, 2) for k, v in excl_rur.items()}
        pacote["rurais_geradas"] = vias_rurais_geradas
        vias_km = pacote["mun"]["vias"]; area_urb = pacote["mun"]["urb"]
    else:
        vias_km = sum(km_mancha.values())
        area_urb = uniao_utm.area / 1e6
        area_rur = sum(g.area for g in rur.geometry) / 1e6
        a_ibge = area_oficial(cd) or float(setores["AREA_KM2"].sum())
        contorno = transform(to_wgs, territorio.simplify(40))
        pacote = {
            "versao": VERSAO, "gerado_em": agora(),
            "mun": {
                "c": cd, "n": meta["nome"], "uf": meta["uf"], "rgi": meta["rgi"], "rgint": meta["rgint"],
                "co": False, "origem": "pesquisa",
                "aI": round(a_ibge, 3), "aU": round(area_terr, 3), "urb": round(area_urb, 2), "rur": round(area_rur, 2),
                "vias": round(vias_km, 2), "perc": round(area_urb / a_ibge * 100, 2),
                "dens": round(vias_km / area_urb, 2) if area_urb else None,
                "set": int(len(urb)), "dist": len(manchas), "pdf": None,
                "viasRur": round(km_rural, 2) if vias_rurais_geradas else None, "areaRur": round(area_rural_geo, 2),
                "fonte": "IBGE setores 2022 + vias OSM transitáveis (gerado pelo GIS Comercial)",
                "g": {"type": contorno.geom_type, "coordinates": arredondar(mapping(contorno)["coordinates"], 4)},
            },
            "urb": [{"c": cd, "n": m["nome"], "a": round(m["geom"].area / 1e6, 4),
                     "g": {"type": "Polygon", "coordinates": arredondar(mapping(transform(to_wgs, m["geom"]))["coordinates"], 5)}}
                    for m in manchas],
            "viaNames": nomes, "tipos": tipos, "vias": segs,
            "distritos": [{"CD_MUN": cd, "municipio": meta["nome"], "distrito": m["nome"],
                           "area_km2": round(m["geom"].area / 1e6, 2), "vias_km": round(km_mancha[m["nome"]], 2)}
                          for m in manchas],
            "por_tipo_km": {k: round(v, 2) for k, v in sorted(km_tipo.items(), key=lambda x: -x[1])},
            "excluidas_km": {k: round(v, 2) for k, v in excl.items()},
            "excluidas_rurais_km": {k: round(v, 2) for k, v in excl_rur.items()}, "rurais_geradas": vias_rurais_geradas,
            "crs_medicao": crs,
            "fontes": {"ibge_setores": dict(fonte_setores, baixado_em=agora()), "osm": fonte_osm,
                       "criterio": "Vias transitáveis por veículo (OSM), recortadas nas manchas urbanas IBGE 2022 (CD_SIT 1-3)"},
        }
    log(f"urbana {area_urb:.2f} km2 | vias urbanas {vias_km:.2f} km | rural {area_rural_geo:.2f} km2 | vias rurais {km_rural:.2f} km")

    if exportar_em:
        camadas = {
            "limite_municipal": gpd.GeoDataFrame([{**cols, "UF": meta["uf"], "AREA_KM2": round(area_terr, 3), "geometry": territorio}], crs=crs),
            "area_urbana": gpd.GeoDataFrame([{**cols, "MANCHA": m["nome"], "AREA_KM2": round(m["geom"].area / 1e6, 4),
                                              "VIAS_KM": round(km_mancha.get(m["nome"], 0.0), 2), "geometry": m["geom"]} for m in manchas], crs=crs),
            "area_rural": gpd.GeoDataFrame([{**cols, "AREA_KM2": round(area_rural_geo, 3), "geometry": rural_utm}], crs=crs) if not rural_utm.is_empty else None,
            "vias_urbanas": gpd.GeoDataFrame(reg_urb, crs=crs) if reg_urb else None,
            "vias_rurais": gpd.GeoDataFrame(reg_rur, crs=crs) if reg_rur else None,
        }
        pacote["_exportacao"] = exportar_arquivos(dict(meta, cd=cd), epsg, camadas, pacote["fontes"], exportar_em)
        log("arquivos gerados:", {k: (v.name if hasattr(v, "name") else v) for k, v in pacote["_exportacao"].items() if k != "camadas"})
    if saida:
        Path(saida).parent.mkdir(parents=True, exist_ok=True)
        gravavel = {k: v for k, v in pacote.items() if not k.startswith("_")}
        Path(saida).write_text(json.dumps(gravavel, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        log(f"gravado: {saida} ({Path(saida).stat().st_size / 1e3:.0f} KB)")
    return pacote


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("codigo")
    ap.add_argument("--saida")
    ap.add_argument("--exportar", help="pasta onde gravar SHP, KML e KMZ")
    ap.add_argument("--reutilizar", help="pacote .json já publicado: mantém a parte urbana e só acrescenta rural e arquivos")
    ap.add_argument("--cache", default=str(Path(__file__).resolve().parent / ".cache"))
    a = ap.parse_args()
    antigo = json.loads(Path(a.reutilizar).read_text(encoding="utf-8")) if a.reutilizar else None
    r = gerar(a.codigo, a.cache, a.saida, a.exportar, antigo)
    m = r["mun"]
    print(json.dumps({k: m.get(k) for k in ("n", "uf", "aI", "urb", "vias", "dens", "dist", "areaRur", "viasRur")}, ensure_ascii=False))
    for d in r["distritos"]:
        print(f"  {d['distrito']:<28} {d['area_km2']:>7} km2 {d['vias_km']:>9} km")
    if "_exportacao" in r:
        print("camadas:", json.dumps(r["_exportacao"]["camadas"], ensure_ascii=False))
