"""GIS Comercial · processa a fila de pedidos (roda na automação do GitHub a cada 5 minutos).

Variáveis: SUPABASE_URL, SUPABASE_SERVICE_KEY (segredos do repositório; nunca vão para o navegador).
Para cada pedido em 'fila': reserva, gera o município (gerar_municipio.py), grava o JSON no Storage,
atualiza gc_municipios e marca o pedido como 'pronto' (ou 'erro', com a mensagem).
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from gerar_municipio import gerar, log
from verificar_fontes import verificar
from drive import Drive, url_arquivo, url_pasta

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
CACHE = os.environ.get("GC_CACHE", str(Path(__file__).resolve().parent / ".cache"))
MAX_PEDIDOS = int(os.environ.get("GC_MAX_PEDIDOS", "6"))
ORCAMENTO_MIN = int(os.environ.get("GC_ORCAMENTO_MIN", "40"))    # para antes de estourar o limite do job
MAX_TENTATIVAS = 3
LISTA_PDF = os.environ.get("GC_LISTA_PDF", str(Path(__file__).resolve().parent / "relatorios_pendentes.txt"))   # municípios que ganharam dados novos
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
# Google Drive (segredo GOOGLE_OAUTH): guarda os arquivos GIS e a planilha geral. Sem ele, os arquivos vão para o Storage.
DRIVE = Drive(os.environ["GOOGLE_OAUTH"]) if os.environ.get("GOOGLE_OAUTH") else None


def agora():
    return datetime.now(timezone.utc)


def rest(metodo, caminho, **kw):
    cab = dict(H, **kw.pop("headers", {}))
    r = requests.request(metodo, f"{URL}/rest/v1/{caminho}", headers=cab, timeout=60, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{metodo} {caminho}: HTTP {r.status_code} {r.text[:200]}")
    return r.json() if r.text else None


def recuperar_travados():
    """Pedido 'processando' há mais de 40 min = execução anterior caiu: volta para a fila (ou erro)."""
    # formato com Z: o "+00:00" do isoformat() vira espaço na URL e o PostgREST recusa
    limite = (agora() - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for p in rest("GET", f"gc_pedidos?status=eq.processando&iniciado_em=lt.{limite}&select=id,tentativas") or []:
        novo = {"status": "fila"} if p["tentativas"] < MAX_TENTATIVAS else \
            {"status": "erro", "mensagem": "A geração não terminou depois de várias tentativas.", "concluido_em": agora().isoformat()}
        rest("PATCH", f"gc_pedidos?id=eq.{p['id']}", json=novo)
        log("pedido travado recolocado:", p["id"], novo["status"])


FALHARAM_AGORA = set()   # pedidos que falharam nesta rodada: a nova tentativa espera alguns minutos


def reservar():
    # pedidos de visitantes (gerar/atualizar) passam na frente da exportação em massa e das conferências
    fora = f"&id=not.in.({','.join(FALHARAM_AGORA)})" if FALHARAM_AGORA else ""
    fila = rest("GET", f"gc_pedidos?status=eq.fila&tipo=in.(gerar,atualizar){fora}&order=criado_em.asc&limit=1&select=id,cd_mun,tipo,tentativas") \
        or rest("GET", f"gc_pedidos?status=eq.fila{fora}&order=criado_em.asc&limit=1&select=id,cd_mun,tipo,tentativas")
    if not fila:
        return None
    p = fila[0]
    ok = rest("PATCH", f"gc_pedidos?id=eq.{p['id']}&status=eq.fila",
              json={"status": "processando", "iniciado_em": agora().isoformat(), "tentativas": p["tentativas"] + 1},
              headers={"Prefer": "return=representation", "Content-Type": "application/json"})
    return p if ok else None      # lista vazia = outra execução reservou primeiro


# o KML não é guardado (ocupa 3 a 4 vezes mais que o KMZ, com o mesmo conteúdo): o mapa o extrai do KMZ na hora do download
TIPOS_ARQUIVO = {"shp": "application/zip", "kmz": "application/vnd.google-earth.kmz"}


def subir(caminho, dados, content_type):
    r = requests.post(f"{URL}/storage/v1/object/gc-municipios/{caminho}", data=dados, timeout=300,
                      headers=dict(H, **{"Content-Type": content_type, "x-upsert": "true", "Cache-Control": "max-age=300"}))
    if r.status_code >= 400:
        raise RuntimeError(f"Storage: HTTP {r.status_code} {r.text[:200]}")


def subir_arquivos(cd, pacote):
    """Arquivos GIS do município: no Drive (se configurado) ou no Storage (arquivos/<código>/…). Devolve o registro do catálogo."""
    exp, m = pacote["_exportacao"], pacote["mun"]
    registro = {"camadas": exp["camadas"], "gerado_em": agora().isoformat(),
                "tamanhos": {k: Path(exp[k]).stat().st_size for k in ("shp", "kmz")}}
    if DRIVE:
        registro["drive"] = DRIVE.guardar_municipio(m["uf"], m["n"], cd, {k: Path(exp[k]) for k in ("shp", "kmz")})
        log(f"  enviado ao Drive: {m['n']} ({sum(registro['tamanhos'].values()) / 1e6:.1f} MB)")
        return registro
    for chave, ct in TIPOS_ARQUIVO.items():
        f = Path(exp[chave])
        subir(f"arquivos/{cd}/{f.name}", f.read_bytes(), ct)
        registro[chave] = f"arquivos/{cd}/{f.name}"
        log(f"  enviado {f.name} ({f.stat().st_size / 1e6:.1f} MB)")
    return registro


def baixar_pacote(cd):
    r = requests.get(f"{URL}/storage/v1/object/public/gc-municipios/{cd}.json", timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Pacote do município indisponível (HTTP {r.status_code})")
    return r.json()


def guardar_json(cd, pacote):
    limpo = {k: v for k, v in pacote.items() if not k.startswith("_")}
    subir(f"{cd}.json", json.dumps(limpo, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json")


def guardar_exportacao(cd, pacote):
    """Pedido 'exportar': guarda os arquivos e o pacote enriquecido (área e vias rurais) sem mexer no resto do município."""
    arquivos = subir_arquivos(cd, pacote)
    guardar_json(cd, pacote)
    rest("PATCH", f"gc_municipios?cd_mun=eq.{cd}", json={"arquivos": arquivos, "atualizado_em": agora().isoformat()},
         headers={"Content-Type": "application/json"})


def guardar(cd, pacote):
    arquivos = subir_arquivos(cd, pacote) if pacote.get("_exportacao") else None
    guardar_json(cd, pacote)
    m = pacote["mun"]
    anterior = rest("GET", f"gc_municipios?cd_mun=eq.{cd}&select=origem") or []
    linha = {
        "cd_mun": cd, "nome": m["n"], "uf": m["uf"], "rgi": m["rgi"], "rgint": m["rgint"],
        "origem": anterior[0]["origem"] if anterior else "pesquisa",
        "resumo": {k: m[k] for k in ("aI", "aU", "urb", "rur", "vias", "perc", "dens", "set", "dist")},
        "contorno": m["g"],
        "fontes": pacote["fontes"], "arquivo": f"{cd}.json", "versao": pacote["versao"],
        "gerado_em": pacote["gerado_em"], "verificado_em": None, "verificacao": None, "atualizado_em": agora().isoformat(),
        "arquivos": arquivos,
    }
    rest("POST", "gc_municipios?on_conflict=cd_mun", json=linha,
         headers={"Prefer": "resolution=merge-duplicates", "Content-Type": "application/json"})


def atualizar_planilha():
    """Planilha geral no Drive: uma linha por município da base."""
    linhas = rest("GET", "gc_municipios?select=cd_mun,nome,uf,origem,resumo,arquivos,gerado_em&order=uf.asc,nome.asc") or []
    cab = ["Município", "UF", "Código IBGE", "Área do município (km²)", "Área urbana (km²)", "Área rural (km²)", "Vias urbanas (km)",
           "Vias rurais (km)", "Densidade de vias urbanas (km/km²)", "Gerado em", "Origem", "Pasta no Drive", "Shapefile", "KMZ", "Relatório PDF"]
    saida = [cab]
    for m in linhas:
        r, a = m.get("resumo") or {}, m.get("arquivos") or {}
        cam, d = a.get("camadas") or {}, a.get("drive") or {}
        link = lambda rot, i: f'=HYPERLINK("{url_arquivo(i)}","{rot}")' if i else ""
        saida.append([m["nome"], m["uf"], m["cd_mun"], r.get("aI"), r.get("urb"), (cam.get("area_rural") or {}).get("km2"), r.get("vias"),
                      (cam.get("vias_rurais") or {}).get("km"), r.get("dens"), (m.get("gerado_em") or "")[:10], m.get("origem"),
                      f'=HYPERLINK("{url_pasta(d["pasta"])}","abrir")' if d.get("pasta") else "",
                      link("baixar", d.get("shp")), link("baixar", d.get("kmz")), link("baixar", d.get("pdf"))])
    DRIVE.atualizar_planilha(saida)
    log(f"planilha atualizada ({len(linhas)} municípios)")


def mensagem_amigavel(e, ultima):
    """Erro técnico -> frase para quem pediu (o detalhe técnico fica no log da automação)."""
    t = str(e)
    if "servicodados.ibge.gov.br" in t or "geoftp.ibge.gov.br" in t:
        causa = "O servidor do IBGE não respondeu."
    elif "overpass" in t.lower():
        causa = "O servidor do OpenStreetMap não respondeu."
    elif "Drive " in t or "googleapis" in t:
        causa = "O Google Drive não respondeu ao guardar os arquivos."
    else:
        return t[:300]
    return causa + (" Tente de novo mais tarde." if ultima else " O pedido será tentado de novo automaticamente em alguns minutos.")


def conferir(cd):
    """Pedido 'verificar': confere IBGE e OSM e guarda o resultado no município (a função só lê daqui)."""
    linhas = rest("GET", f"gc_municipios?cd_mun=eq.{cd}&select=fontes,gerado_em") or []
    if not linhas:
        raise RuntimeError("Município não está na base")
    v = verificar(linhas[0]["fontes"], linhas[0]["gerado_em"])
    rest("PATCH", f"gc_municipios?cd_mun=eq.{cd}", json={"verificacao": v, "verificado_em": v["verificado_em"]},
         headers={"Content-Type": "application/json"})
    log(f"conferido {cd}: {v['resumo']}")


def main():
    inicio = time.time()
    recuperar_travados()
    feitos = 0
    esperas = [120, 300]           # novas tentativas dos que falharam, ainda nesta execução
    while feitos < MAX_PEDIDOS and (time.time() - inicio) / 60 < ORCAMENTO_MIN:
        p = reservar()
        if not p:
            if not (FALHARAM_AGORA and esperas) or (time.time() - inicio) / 60 + esperas[0] / 60 > ORCAMENTO_MIN:
                break
            espera = esperas.pop(0)
            log(f"{len(FALHARAM_AGORA)} pedido(s) falharam; nova tentativa em {espera // 60} min")
            time.sleep(espera)
            FALHARAM_AGORA.clear()
            continue
        cd = p["cd_mun"]
        log(f"=== pedido {p['id']}: {p['tipo']} {cd}")
        try:
            if p["tipo"] == "verificar":
                conferir(cd)
            elif p["tipo"] == "exportar":      # município já na base: mantém os números urbanos e acrescenta rural e arquivos
                with tempfile.TemporaryDirectory() as tmp:
                    pacote = gerar(cd, CACHE, exportar_em=tmp, reutilizar=baixar_pacote(cd))
                    guardar_exportacao(cd, pacote)
                log(f"arquivos prontos: {pacote['mun']['n']} - rural {pacote['mun'].get('areaRur')} km2, vias rurais {pacote['mun'].get('viasRur')} km")
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    pacote = gerar(cd, CACHE, exportar_em=tmp)
                    guardar(cd, pacote)
                log(f"pronto: {pacote['mun']['n']} - {pacote['mun']['urb']} km2 urbanos, {pacote['mun']['vias']} km de vias")
            if p["tipo"] != "verificar":       # dados novos ou atualizados: o relatório PDF do município precisa ser refeito
                with open(LISTA_PDF, "a") as f:
                    f.write(cd + "\n")
            rest("PATCH", f"gc_pedidos?id=eq.{p['id']}", json={
                "status": "pronto", "concluido_em": agora().isoformat(), "mensagem": None},
                 headers={"Content-Type": "application/json"})
        except Exception as e:     # noqa: BLE001 - qualquer falha vira mensagem para o usuário
            log("ERRO:", e)
            FALHARAM_AGORA.add(p["id"])
            ultima = p["tentativas"] + 1 >= MAX_TENTATIVAS
            rest("PATCH", f"gc_pedidos?id=eq.{p['id']}", json={
                "status": "erro" if ultima else "fila",
                "mensagem": mensagem_amigavel(e, ultima), "concluido_em": agora().isoformat()},
                 headers={"Content-Type": "application/json"})
        feitos += 1
    if DRIVE and feitos:
        try:
            if not DRIVE.raiz_publica():
                log("AVISO: a pasta 'GIS Comercial' no Drive não está com 'Qualquer pessoa com o link'; os downloads do mapa vão pedir login.")
            atualizar_planilha()
        except Exception as e:     # noqa: BLE001 - a planilha não pode derrubar o processamento dos pedidos
            log("planilha não atualizada:", e)
    log(f"{feitos} pedido(s) processado(s)")


if __name__ == "__main__":
    sys.exit(main())
