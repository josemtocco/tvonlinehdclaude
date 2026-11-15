#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TV Online HD -> Lista M3U (otimizada para SS IPTV)
===================================================

Fonte: https://www.tvonlinehd.app.br/

O site é um SPA (Vercel) e NÃO tem paginação / botão "carregar mais": a página
inicial já entrega o catálogo completo, com todos os canais e categorias.
A cada execução são lidas duas coisas:

  1) HTML da home  -> cards de canal (nome, número, logo, categorias, id)
  2) API do site   -> /api/stream?id=<id> devolve {"n": nome, "s": link .m3u8}
                      (o link não fica mais embutido na página; é pedido
                      canal por canal, em paralelo)

Se a API parar de responder, o script ainda tenta o formato antigo
(catálogo embutido em window.TV) antes de desistir — e, desistindo, mantém a
lista anterior intacta em vez de publicar uma lista vazia.

A categoria de cada canal é a do próprio site (chips da home).

Regras de atualização (reconciliação):
  - canal que responde -> mantém na lista.
  - canal que falha    -> incrementa contador de falhas; só é REMOVIDO após
                          6 atualizações consecutivas com falha.
  - canal novo         -> adicionado automaticamente.
  - o estado fica em estado_canais.json (commitado no repositório).

Uso:
    python atualizar_m3u.py                 # gera + testa + lista final
    python atualizar_m3u.py --sem-testar    # não valida os streams
    python atualizar_m3u.py --testar-tudo   # testa até os mais lentos
    python atualizar_m3u.py --iniciar-estado # zera os contadores de falha

IP do Brasil:
  O GitHub Actions roda em servidores fora do Brasil, e alguns servidores de
  vídeo do site bloqueiam IP estrangeiro. Três formas de lidar com isso:
    1) Runner próprio no seu PC/servidor no Brasil: a conexão sai pelo SEU IP
       de verdade (a única forma de usar "o seu IP" de fato).
    2) BR_IP_CLIENTE (secret): envia o seu IP nos cabeçalhos X-Forwarded-For,
       X-Real-IP etc. Só engana servidores que confiam nesses cabeçalhos; não
       muda o IP real da conexão (bloqueio de rede/CDN não é afetado).
    3) BR_PROXY_URL (secret): proxy HTTP com saída no Brasil.
  Variáveis:
    BR_IP_CLIENTE        seu IP, enviado nos cabeçalhos (secret, opcional)
    BR_ABORTAR_FORA_DO_BRASIL  "1": aborta (sem alterar nada) se a conexão
                         real não sair do Brasil; evita contar falsas falhas
    BR_PROXY_URL         proxy HTTP (secret, opcional)
    BR_IP_ESPERADO       IP que o proxy deve ter (secret, opcional): se o IP de
                         saída for outro, a execução é abortada sem alterar nada
    BR_PROXY_ESCOPO      "tudo" (padrão: coleta + testes) ou "coleta" (testes direto)
    BR_PROXY_EXIGIR_BR   "0" para não exigir país BR no IP de saída
  Se o proxy falhar, a execução é ABORTADA e a lista/estado ficam intactos
  (assim uma queda do proxy não conta como "falha" dos canais).

Só usa a biblioteca padrão do Python (sem pip). Usa ffprobe se estiver
instalado (é o caso do GitHub Actions) para confirmar que o vídeo decodifica.
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from urllib.parse import urljoin, urlparse, quote

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

BASE = os.environ.get("TVHD_BASE", "https://www.tvonlinehd.app.br").rstrip("/")
HOME_URL = BASE + "/"
PLAYER_URL = BASE + "/player"
API_STREAM = BASE + "/api/stream"

RAIZ = os.path.dirname(os.path.abspath(__file__))
ARQ_LISTA = os.path.join(RAIZ, "tvonlinehd.m3u")
ARQ_ESTADO = os.path.join(RAIZ, "estado_canais.json")
ARQ_RELATORIO = os.path.join(RAIZ, "relatorio.json")

LIMITE_FALHAS = int(os.environ.get("LIMITE_FALHAS", "6"))  # remover após 6 updates
TIMEOUT_HTTP = float(os.environ.get("TIMEOUT_HTTP", "15"))
TIMEOUT_PROBE = float(os.environ.get("TIMEOUT_PROBE", "25"))
WORKERS_HTTP = int(os.environ.get("WORKERS_HTTP", "24"))
WORKERS_PROBE = int(os.environ.get("WORKERS_PROBE", "10"))
WORKERS_API = int(os.environ.get("WORKERS_API", "12"))  # consultas simultaneas a API
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --- Proxy / IP do Brasil (vem de Actions secrets; sem secret = conexão direta) ---
PROXY_URL = (os.environ.get("BR_PROXY_URL") or os.environ.get("PROXY_URL") or "").strip()
PROXY_ESCOPO = (os.environ.get("BR_PROXY_ESCOPO") or "tudo").strip().lower()
if PROXY_ESCOPO not in ("tudo", "coleta"):
    PROXY_ESCOPO = "tudo"
IP_ESPERADO = (os.environ.get("BR_IP_ESPERADO") or "").strip()
EXIGIR_BRASIL = (os.environ.get("BR_PROXY_EXIGIR_BR") or "1").strip() != "0"
IP_CLIENTE = (os.environ.get("BR_IP_CLIENTE") or "").strip()
ABORTAR_FORA_BR = (os.environ.get("BR_ABORTAR_FORA_DO_BRASIL") or "").strip() == "1"

# Sinônimos para juntar categorias equivalentes (o site pode renomear chips).
SINONIMOS = {
    "noticias": "Notícias",
    "notícias": "Notícias",
    "news": "Notícias",
    "esportes": "Esportes",
    "esporte": "Esportes",
    "infantil": "Infantil",
    "kids": "Infantil",
    "desenhos": "Infantil",
    "documentarios": "Documentários",
    "documentários": "Documentários",
    "documentario": "Documentários",
    "filmes e series": "Filmes e Séries",
    "filmes e séries": "Filmes e Séries",
    "filmes": "Filmes e Séries",
    "series": "Filmes e Séries",
    "séries": "Filmes e Séries",
    "abertos": "Abertos",
    "aberto": "Abertos",
    "variedades": "Variedades",
    "variedade": "Variedades",
    "portugal": "Portugal",
    "pt": "Portugal",
}

# Ordem das categorias na lista (mantém a ordem original do site como base).
ORDEM_PADRAO = [
    "Esportes",
    "Abertos",
    "Notícias",
    "Filmes e Séries",
    "Variedades",
    "Infantil",
    "Documentários",
    "Portugal",
]

LOG_LINHAS = []


def log(msg=""):
    print(msg, flush=True)
    LOG_LINHAS.append(str(msg))


# ---------------------------------------------------------------------------
# HTTP (sem dependências externas)
# ---------------------------------------------------------------------------

try:
    import urllib.request as _url
    import urllib.error as _urlerr
    import gzip as _gzip
    import zlib as _zlib
except ImportError:  # pragma: no cover
    _url = None


def http_get(url, timeout=None, referer=None, aceitar_erro=False, proxy=True):
    """GET simples com UA de browser. Retorna (status, bytes, headers).

    proxy=True usa o proxy do Brasil (se configurado); False vai direto.
    """
    timeout = timeout or TIMEOUT_HTTP
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
    }
    if referer:
        headers["Referer"] = referer
    headers.update(_headers_ip())
    req = _url.Request(url, headers=headers)
    try:
        with _opener(proxy).open(req, timeout=timeout) as resp:
            dados = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                dados = _gzip.decompress(dados)
            elif "deflate" in enc:
                try:
                    dados = _zlib.decompress(dados)
                except _zlib.error:
                    dados = _zlib.decompress(dados, -_zlib.MAX_WBITS)
            return resp.status, dados, dict(resp.headers)
    except _urlerr.HTTPError as e:
        corpo = b""
        try:
            corpo = e.read()
        except Exception:
            pass
        if aceitar_erro:
            return e.code, corpo, dict(e.headers or {})
        return e.code, corpo, dict(e.headers or {})
    except Exception as e:  # timeout, DNS, SSL...
        return 0, str(e).encode("utf-8", "ignore"), {}


def texto(url):
    status, dados, _ = http_get(url)
    if status != 200:
        raise RuntimeError(f"Falha ao baixar {url} (HTTP {status})")
    for enc in ("utf-8", "latin-1"):
        try:
            return dados.decode(enc)
        except UnicodeDecodeError:
            continue
    return dados.decode("utf-8", "ignore")


# ---------------------------------------------------------------------------
# Proxy / IP do Brasil
# ---------------------------------------------------------------------------

_OPENER_DIRETO = _url.build_opener()
_OPENER_PROXY = None
_IP_PROXY = ""

_SERVICOS_IP = (
    ("https://ipinfo.io/json", "ip", "country"),
    ("https://api.country.is/", "ip", "country"),
    ("https://ipapi.co/json/", "ip", "country_code"),
)


_NOMES_HEADERS_IP = ("X-Forwarded-For", "X-Real-IP", "Client-IP", "True-Client-IP", "CF-Connecting-IP")


def _headers_ip():
    """Cabeçalhos que dizem 'o cliente é este IP' (só valem em servidores que confiam neles)."""
    return {n: IP_CLIENTE for n in _NOMES_HEADERS_IP} if IP_CLIENTE else {}


def _opener(proxy=True):
    return _OPENER_PROXY if (proxy and _OPENER_PROXY) else _OPENER_DIRETO


def _proxy_nos_testes():
    """Os testes de manifesto/vídeo também saem pelo proxy (escopo 'tudo')."""
    return PROXY_ESCOPO != "coleta"


def mascarar_ip(ip):
    """Logs e arquivos do repositório são públicos: nunca publica o IP inteiro."""
    p = (ip or "").split(".")
    if len(p) == 4:
        return f"{p[0]}.x.x.{p[3]}"
    return (ip[:4] + "..." + ip[-2:]) if len(ip or "") > 8 else "..."


def sanear(txt):
    """Remove dados do proxy (host/usuário/senha) de qualquer texto gravado."""
    if not PROXY_URL or not txt:
        return txt
    u = urlparse(PROXY_URL)
    partes = [PROXY_URL, u.netloc, u.hostname or ""]
    for extra in (u.username, u.password):
        if extra and len(extra) >= 4:
            partes.append(extra)
    for parte in partes:
        if parte:
            txt = txt.replace(parte, "[proxy]")
    return txt


def consultar_ip(proxy=True):
    """Descobre (ip, pais) pelo qual as requisições estão saindo."""
    for url, k_ip, k_pais in _SERVICOS_IP:
        status, dados, _ = http_get(url, timeout=12, proxy=proxy)
        if status != 200:
            continue
        try:
            obj = json.loads(dados.decode("utf-8", "ignore"))
        except Exception:
            continue
        ip = str(obj.get(k_ip) or "").strip()
        pais = str(obj.get(k_pais) or "").strip().upper()
        if ip:
            return ip, pais
    return "", ""


def _montar_proxy():
    """Cria o opener do proxy. Retorna texto de erro ou None se estiver tudo certo."""
    global _OPENER_PROXY
    u = urlparse(PROXY_URL)
    if u.scheme != "http" or not u.hostname:
        return (
            "BR_PROXY_URL inválido. Use o formato http://usuario:senha@host:porta "
            "(SOCKS e https:// não são suportados; caracteres especiais na senha "
            "precisam estar codificados, ex.: @ vira %40)."
        )
    _OPENER_PROXY = _url.build_opener(
        _url.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL})
    )
    return None


def _ip_aceito(ip, pais):
    if not ip:
        return False
    if EXIGIR_BRASIL and pais != "BR":
        return False
    if IP_ESPERADO and ip != IP_ESPERADO:
        return False
    return True


def preparar_rede():
    """Configura o proxy do Brasil e confere o IP de saída.

    Levanta RuntimeError (e o main aborta sem mexer na lista) se o proxy
    não existir no Actions, não responder, não for do Brasil ou não for o IP
    esperado. Assim, problema de rede nunca vira "canal com falha".
    """
    global _IP_PROXY
    if not PROXY_URL:
        log("[0/5] Rede: conexão direta (sem proxy)")
        ip, pais = consultar_ip(proxy=False)
        if ip:
            log(f"      IP de saída real: {mascarar_ip(ip)}  país: {pais or '?'}")
        if IP_CLIENTE:
            log("      BR_IP_CLIENTE ativo: cabeçalhos X-Forwarded-For/X-Real-IP enviados "
                "(não muda o IP real da conexão)")
        if pais and pais != "BR":
            log("      [aviso] a conexão NÃO sai do Brasil: canais com bloqueio regional "
                "vão aparecer como falha.")
            if ABORTAR_FORA_BR:
                raise RuntimeError(
                    f"a conexão sai de {pais}, não do Brasil, e BR_ABORTAR_FORA_DO_BRASIL=1. "
                    "Nada foi alterado."
                )
        return

    erro = _montar_proxy()
    if erro:
        raise RuntimeError(erro)

    log("[0/5] Conferindo o IP de saída do proxy...")
    ip, pais = consultar_ip(proxy=True)
    if not ip:
        raise RuntimeError(
            "o proxy não respondeu (conferir usuário/senha, validade do plano e "
            "se ele aceita conexões de fora; proxy liberado só por IP de origem "
            "não funciona no GitHub Actions). Nada foi alterado."
        )
    _IP_PROXY = ip
    log(f"      IP de saída: {mascarar_ip(ip)}  país: {pais or '?'}  "
        f"escopo do proxy: {PROXY_ESCOPO}")
    if EXIGIR_BRASIL and pais != "BR":
        raise RuntimeError(
            f"o IP de saída do proxy não é do Brasil (país: {pais or '?'}). Nada foi alterado."
        )
    if IP_ESPERADO and ip != IP_ESPERADO:
        raise RuntimeError(
            f"o IP de saída ({mascarar_ip(ip)}) é diferente do IP esperado "
            f"({mascarar_ip(IP_ESPERADO)}). Nada foi alterado."
        )


def proxy_continua_ok():
    """Reconfere o proxy depois dos testes (queda no meio = falsas falhas)."""
    if not PROXY_URL:
        return True
    ip, pais = consultar_ip(proxy=True)
    return _ip_aceito(ip, pais)


def verificar_proxy_cli():
    """--verificar-proxy: mostra o IP COMPLETO só no terminal (não grava em log)."""
    if not PROXY_URL:
        print("Defina BR_PROXY_URL (http://usuario:senha@host:porta) e rode de novo.")
        return 1
    erro = _montar_proxy()
    if erro:
        print(erro)
        return 1
    ip, pais = consultar_ip(proxy=True)
    if not ip:
        print("O proxy não respondeu.")
        return 1
    print(f"IP de saída: {ip}   país: {pais or '?'}")
    if IP_ESPERADO:
        print("IP esperado: " + ("CONFERE" if ip == IP_ESPERADO else "DIFERENTE de BR_IP_ESPERADO"))
    return 0


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------

def limpar_entidades(s):
    if not s:
        return ""
    s = s.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    s = s.replace("&apos;", "'").replace("&lt;", "<").replace("&gt;", ">")
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    return s.strip()


def slug(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalizar_categoria(nome):
    nome = limpar_entidades(nome or "").strip()
    if not nome or slug(nome) in ("todos", "all", "todos os canais"):
        return None
    return SINONIMOS.get(slug(nome), nome)


def esc_m3u(s):
    return (s or "").replace("\r", " ").replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# Coleta: home + API do site
# ---------------------------------------------------------------------------

def extrair_window_tv(html):
    m = re.search(r"window\.TV\s*=\s*", html)
    if not m:
        return None
    dec = json.JSONDecoder()
    try:
        obj, _ = dec.raw_decode(html[m.end():])
        return obj
    except Exception as e:
        log(f"  [aviso] não foi possível decodificar window.TV: {e}")
    return None


def _links_do_window_tv(html):
    """Modo antigo: o player trazia o catálogo inteiro em window.TV."""
    tv = extrair_window_tv(html)
    if not tv or not tv.get("channels"):
        return {}
    mapa = {}
    for c in tv["channels"]:
        cid = c.get("id") or c.get("e") or ""
        link = (c.get("s") or "").strip()
        if cid and link.startswith("http"):
            mapa[cid] = link
    return mapa


def resolver_link(cid, tentativas=3):
    """Pergunta à API do site o endereço do stream do canal.

    O site novo esconde o link atrás de /api/stream?id=<id>, que devolve
    {"n": nome, "s": "https://....m3u8"}.
    """
    url = f"{API_STREAM}?id={quote(str(cid), safe='')}"
    for i in range(tentativas):
        status, dados, _ = http_get(url, referer=f"{BASE}/embed?id={cid}")
        if status == 200:
            try:
                obj = json.loads(dados.decode("utf-8", "ignore"))
            except Exception:
                obj = None
            if isinstance(obj, dict):
                link = (obj.get("s") or obj.get("url") or obj.get("stream") or "").strip()
                if link.startswith("http"):
                    return link, limpar_entidades(obj.get("n") or "")
                return "", limpar_entidades(obj.get("n") or "")
        elif status in (0, 429, 500, 502, 503, 504):
            time.sleep(1.2 * (i + 1))  # rede/limite: vale a pena insistir
        else:
            break
    return "", ""


def coletar_catalogo():
    """Lê a home (cards + chips) e resolve os links pela API do site.

    A home entrega nome, número, categorias e logo de todos os canais.
    O endereço do stream é pedido canal por canal para /api/stream.
    Se a API mudar, ainda tenta o modo antigo (window.TV no player).
    """
    log("[1/5] Baixando a pagina inicial...")
    home = None
    for tentativa in range(4):
        try:
            home = texto(HOME_URL)
            break
        except Exception as e:
            log(f"      tentativa {tentativa + 1} falhou: {e}")
            time.sleep(3 + 3 * tentativa)
    if not home:
        raise RuntimeError(
            "Nao foi possivel baixar a pagina inicial do site depois de 4 tentativas. "
            "Confira a conexao (ou rode novamente mais tarde)."
        )

    log("[2/5] Lendo as categorias do site...")
    categorias = {}
    for cid, nome in re.findall(r'data-cat="(\d+)"[^>]*>([^<]*)</button>', home):
        n = normalizar_categoria(nome)
        if n:
            categorias[int(cid)] = n
    if not categorias:
        for cid, nome in re.findall(
            r'data-cat="(\d+)"[^>]*>\s*([^<]{2,40})\s*<', home
        ):
            n = normalizar_categoria(nome)
            if n:
                categorias[int(cid)] = n
    log(f"      categorias encontradas: {', '.join(categorias.values()) or '(nenhuma)'}")

    cards = {}
    padrao_card = re.compile(r'<div class="channel-card"[^>]*>', re.S)
    aberturas = list(padrao_card.finditer(home))
    for i, m in enumerate(aberturas):
        tag = m.group(0)
        fim = aberturas[i + 1].start() if i + 1 < len(aberturas) else len(home)
        bloco = home[m.start():fim]
        attrs = dict(re.findall(r'data-(id|name|num|cats)="([^"]*)"', tag))
        cid = attrs.get("id", "")
        if not cid:
            continue
        cats = attrs.get("cats", "")
        logo = re.search(r'<img[^>]*src="([^"]+)"', bloco)
        link = re.search(r'href="([^"]*player[^"]*)"', bloco)
        num = attrs.get("num", "")
        cards[cid] = {
            "id": cid,
            "nome": limpar_entidades(attrs.get("name", "")) or cid,
            "num": num,
            "cats": [int(x) for x in cats.split(",") if x.strip().isdigit()],
            "logo": urljoin(HOME_URL, limpar_entidades(logo.group(1))) if logo else "",
            "player": urljoin(HOME_URL, limpar_entidades(link.group(1)))
            if link
            else f"{BASE}/player?id={cid}",
        }
    log(f"      cards de canal na home: {len(cards)}")
    if not cards:
        raise RuntimeError(
            "A pagina inicial abriu, mas nenhum canal foi reconhecido "
            "(o site pode ter mudado de novo). Nada foi alterado na lista."
        )

    log("[3/5] Pedindo os links de transmissao de cada canal...")
    links, nomes_api = {}, {}
    fila = list(cards.keys())

    # 1o caminho: modo antigo, caso o player ainda entregue tudo de uma vez.
    antigos = {}
    for purl in (f"{BASE}/player", PLAYER_URL, f"{BASE}/embed"):
        try:
            antigos = _links_do_window_tv(texto(purl))
        except Exception:
            antigos = {}
        if antigos:
            log(f"      catalogo antigo disponivel em {urlparse(purl).path}: {len(antigos)} links")
            break

    if antigos:
        for cid in fila:
            if cid in antigos:
                links[cid] = antigos[cid]
        fila = [c for c in fila if c not in links]
        log(f"      resolvidos pelo modo antigo: {len(links)}")

    # 2o caminho: API /api/stream canal por canal (com varios em paralelo).
    if fila:
        feito = [0]
        total = len(fila)

        def tarefa(cid):
            link, nome = resolver_link(cid)
            feito[0] += 1
            if feito[0] % 100 == 0 or feito[0] == total:
                log(f"      {feito[0]}/{total} consultados...")
            return cid, link, nome

        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS_API) as ex:
            for cid, link, nome in ex.map(tarefa, fila):
                if link:
                    links[cid] = link
                if nome:
                    nomes_api[cid] = nome

    log(f"      canais com link resolvido: {len(links)} de {len(cards)}")

    canais = []
    for cid, card in cards.items():
        link = links.get(cid, "")
        nome = card["nome"] or nomes_api.get(cid, "") or cid
        ids_cat = [i for i in card["cats"] if i in categorias]
        if not ids_cat:
            ids_cat = []  # canal sem categoria no site -> fica em "Outros"
        nomes_cat = []
        for i in ids_cat:
            n = categorias.get(i)
            if n and n not in nomes_cat:
                nomes_cat.append(n)
        logo = card["logo"]
        if re.search(r"/(logo\.png)$", urlparse(logo).path or ""):
            logo = ""  # logo generica do site nao ajuda no SS IPTV
        canais.append(
            {
                "id": cid,
                "nome": nome,
                "logo": logo,
                "link": link,
                "categorias": nomes_cat,
                "player": card["player"],
                "num": card["num"],
                "ordem_cat": ids_cat,
            }
        )

    # ordena pela numeracao do site quando existir
    def chave(c):
        n = (c.get("num") or "").strip()
        return (0, int(n)) if n.isdigit() else (1, 0)

    canais.sort(key=chave)
    log(f"      canais no catalogo: {len(canais)}")
    log(f"      canais com link direto .m3u8: {sum(1 for c in canais if c['link'])}")
    if not any(c["link"] for c in canais):
        raise RuntimeError(
            "Nenhum link de transmissao foi obtido (a API do site nao respondeu). "
            "A lista anterior foi preservada."
        )
    return canais, categorias


# ---------------------------------------------------------------------------
# Teste de reprodução
# ---------------------------------------------------------------------------

def _ffprobe(caminho):
    return shutil.which(caminho)


def testar_http(url):
    """Etapa 1: o manifesto responde e é HLS de verdade?"""
    if not url:
        return False, "sem link"
    if not url.lower().startswith(("http://", "https://")):
        return False, "formato não suportado"
    status, dados, headers = http_get(url, timeout=TIMEOUT_HTTP, referer=HOME_URL, aceitar_erro=True, proxy=_proxy_nos_testes())
    if status in (301, 302, 303, 307, 308):
        destino = headers.get("Location") or headers.get("location")
        if destino:
            status, dados, headers = http_get(urljoin(url, destino), timeout=TIMEOUT_HTTP, referer=HOME_URL, aceitar_erro=True, proxy=_proxy_nos_testes())
    if status != 200:
        return False, f"HTTP {status or 'sem resposta'}"
    amostra = dados[:4000].decode("utf-8", "ignore")
    if "#EXTM3U" not in amostra:
        return False, "não é manifesto HLS"
    if "#EXT-X-STREAM-INF" in amostra:
        return True, "HLS (master)"
    if "#EXTINF" in amostra or "#EXT-X-TARGETDURATION" in amostra:
        return True, "HLS (ao vivo)"
    return True, "HLS (vazio)"


def testar_video(url):
    """Etapa 2: o vídeo decodifica? (usa ffprobe quando disponível)"""
    if not url:
        return False, "sem link"
    if _ffprobe("ffprobe"):
        cmd = [
            "ffprobe", "-v", "error", "-rw_timeout", "10000000",
            "-analyzeduration", "5000000", "-probesize", "2000000",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height",
            "-of", "csv=p=0", url,
        ]
        if IP_CLIENTE:
            cmd[1:1] = ["-headers", "".join(f"{n}: {IP_CLIENTE}\r\n" for n in _NOMES_HEADERS_IP)]
        env = None
        if PROXY_URL and _OPENER_PROXY and _proxy_nos_testes():
            env = dict(os.environ, http_proxy=PROXY_URL)
            env.pop("no_proxy", None)
            env.pop("NO_PROXY", None)
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_PROBE, env=env)
            saida = (p.stdout or "").strip().splitlines()
            if saida and saida[0].strip(","):
                return True, saida[0].strip()
            return False, sanear((p.stderr or "sem vídeo").strip().splitlines()[-1][:70]) if p.stderr else "sem vídeo"
        except subprocess.TimeoutExpired:
            return False, "timeout do ffprobe"
        except Exception as e:
            return False, sanear(f"erro ffprobe: {e}")
    return testar_http(url)


def testar_canal(canal):
    url = canal.get("link") or ""
    ok, motivo = testar_http(url)
    if ok and canal.get("probe", True):
        ok2, motivo2 = testar_video(url)
        if not ok2:
            return False, motivo2
        return True, motivo2
    return ok, motivo


# ---------------------------------------------------------------------------
# Estado / reconciliação
# ---------------------------------------------------------------------------

def hoje():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def carregar_estado():
    if os.path.exists(ARQ_ESTADO):
        try:
            with open(ARQ_ESTADO, encoding="utf-8") as f:
                st = json.load(f)
            st.setdefault("canais", {})
            return st
        except Exception as e:
            log(f"  [aviso] estado ilegível ({e}); começando do zero.")
    return {"versao": 1, "atualizacoes": 0, "limite_falhas": LIMITE_FALHAS, "canais": {}}


def salvar_json(caminho, obj):
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(tmp, caminho)


# ---------------------------------------------------------------------------
# Geração da M3U
# ---------------------------------------------------------------------------

def agrupar(canais, ordem_site):
    """Agrupa por categoria (uma entrada por categoria; a 1ª da ordem do site)."""
    grupos = {}
    for c in canais:
        cats = c.get("categorias") or []
        # "Todos" não é categoria: usa a mais específica (menor id > 0)
        especificas = [x for x in cats if slug(x) != "todos"]
        cat = (especificas or cats or ["Outros"])[0]
        grupos.setdefault(cat, []).append(c)

    ordem = [x for x in (ordem_site + ORDEM_PADRAO) if x in grupos]
    ordem = list(dict.fromkeys(ordem))
    for cat in grupos:
        if cat not in ordem and cat != "Outros":
            ordem.append(cat)
    if "Outros" in grupos:
        ordem.append("Outros")
    return [(cat, grupos[cat]) for cat in ordem]


def gerar_m3u(canais, ordem_site):
    linhas = ["#EXTM3U"]
    total = 0
    for cat, itens in agrupar(canais, ordem_site):
        for c in sorted(
            itens,
            key=lambda x: (
                min(x.get("ordem_cat") or [99]),
                slug(x["nome"]),
            ),
        ):
            nome = esc_m3u(c["nome"])
            logo = esc_m3u(c.get("logo") or "")
            attrs = f' tvg-id="{esc_m3u(c["id"])}" tvg-name="{nome}"'
            if logo:
                attrs += f' tvg-logo="{logo}"'
            attrs += f' group-title="{esc_m3u(cat)}"'
            linhas.append(f"#EXTINF:-1{attrs},{nome}")
            linhas.append(c["link"])
            total += 1
    return "\n".join(linhas) + "\n", total


def gerar_m3u_completa(canais, ordem_site):
    """Versão sem filtro (inclui canais temporariamente com falha)."""
    return gerar_m3u([c for c in canais if c.get("link")], ordem_site)


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Gera a M3U do TV Online HD")
    ap.add_argument("--sem-testar", action="store_true", help="não valida os streams")
    ap.add_argument("--testar-tudo", action="store_true", help="valida o vídeo de todos (mais lento)")
    ap.add_argument("--iniciar-estado", action="store_true", help="zera contadores de falha")
    ap.add_argument("--limite-falhas", type=int, default=None, help="padrão 6")
    ap.add_argument("--verificar-proxy", action="store_true",
                    help="mostra o IP de saída do BR_PROXY_URL e sai")
    args = ap.parse_args()

    if args.verificar_proxy:
        return verificar_proxy_cli()

    global LIMITE_FALHAS
    if args.limite_falhas:
        LIMITE_FALHAS = args.limite_falhas

    inicio = time.time()
    log("=" * 70)
    log(f" TV Online HD -> M3U  |  {dt.datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    log("=" * 70)

    try:
        preparar_rede()
        canais, categorias_por_id = coletar_catalogo()
    except Exception as e:
        log(f"\n[ERRO] Não foi possível ler o catálogo: {sanear(str(e))}")
        log("A lista anterior (se existir) foi preservada.")
        return 2

    estado = carregar_estado()
    if args.iniciar_estado:
        log("\n[opção] --iniciar-estado: contadores de falha zerados.")
        estado = {"versao": 1, "atualizacoes": 0, "limite_falhas": LIMITE_FALHAS, "canais": {}}

    estado["atualizacoes"] = int(estado.get("atualizacoes", 0)) + 1
    estado["limite_falhas"] = LIMITE_FALHAS
    log(f"\n[4/5] Validando transmissões (atualizacao #{estado['atualizacoes']})...")

    resultados = {}
    if args.sem_testar:
        log("      (validação desativada por --sem-testar)")
        for c in canais:
            resultados[c["id"]] = (bool(c["link"]), "não testado")
    else:
        para_testar = [c for c in canais if c["link"]]
        sem_link = [c for c in canais if not c["link"]]
        for c in sem_link:
            resultados[c["id"]] = (False, "site não informa link direto")

        ff = bool(_ffprobe("ffprobe"))
        log(f"      ffprobe disponível: {'sim' if ff else 'não (só checagem HTTP)'}")
        feitos = 0
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS_HTTP) as ex:
            futuros = {ex.submit(testar_canal, c): c for c in para_testar}
            for fut in concurrent.futures.as_completed(futuros):
                c = futuros[fut]
                try:
                    ok, motivo = fut.result()
                except Exception as e:
                    ok, motivo = False, f"erro: {e}"
                resultados[c["id"]] = (ok, motivo)
                feitos += 1
                if feitos % 50 == 0 or feitos == len(para_testar):
                    log(f"      {feitos}/{len(para_testar)} testados ({time.time()-t0:.0f}s)")

    # Proxy caiu (ou trocou de IP) no meio dos testes? Então as "falhas" seriam
    # falsas: aborta sem mexer em lista nem estado.
    if PROXY_URL and not proxy_continua_ok():
        log("\n[ERRO] O proxy do Brasil deixou de responder (ou trocou de IP) durante os testes.")
        log("Resultados descartados; lista e estado anteriores preservados.")
        return 2

    # ---- reconciliação ----
    st_canais = estado["canais"]
    ativos, falhando, removidos, novos = [], [], [], []
    marcas = hoje()

    for c in canais:
        ok, motivo = resultados.get(c["id"], (False, "não testado"))
        reg = st_canais.get(c["id"])
        if reg is None:
            reg = {"nome": c["nome"], "falhas": 0, "desde": marcas, "ultimo_ok": ""}
            st_canais[c["id"]] = reg
            if ok:
                novos.append(c)
        reg["nome"] = c["nome"]
        reg["link"] = c["link"]
        reg["categorias"] = c["categorias"]
        reg["ultimo_teste"] = marcas
        if not c["link"]:
            reg["ultimo_motivo"] = motivo
            continue
        if ok:
            if reg.get("falhas"):
                # registra a sequencia interrompida (canal instavel, voltou a tocar)
                hist = reg.setdefault("historico", [])
                hist.append({"ate": marcas, "falhas": int(reg["falhas"])})
                del hist[:-3]
            reg["falhas"] = 0
            reg["ultimo_ok"] = marcas
            reg["ultimo_motivo"] = motivo
            c["sem_falhas"] = True
            ativos.append(c)
        else:
            reg["falhas"] = int(reg.get("falhas", 0)) + 1
            reg["ultimo_motivo"] = motivo
            c["falhas"] = reg["falhas"]
            if reg["falhas"] >= LIMITE_FALHAS:
                removidos.append(c)
            else:
                falhando.append(c)

    # canais que saíram do site
    ids_site = {c["id"] for c in canais}
    fora_do_site = [i for i in st_canais if i not in ids_site]
    for i in fora_do_site:
        st_canais.pop(i, None)

    # ---- lista final ----
    final = ativos + falhando
    ordem_ids = sorted({i for c in canais for i in (c.get("ordem_cat") or [])})
    ordem_site = [categorias_por_id[i] for i in ordem_ids if i in categorias_por_id]
    for c in canais:
        for cat in c["categorias"]:
            if cat not in ordem_site:
                ordem_site.append(cat)

    conteudo, total = gerar_m3u(final, ordem_site)
    with open(ARQ_LISTA, "w", encoding="utf-8", newline="\n") as f:
        f.write(conteudo)

    # lista completa (para quem quiser tudo, inclusive os que estão em falha)
    completo, total_completo = gerar_m3u_completa(
        [c for c in canais if c["link"]], ordem_site
    )
    with open(os.path.join(RAIZ, "tvonlinehd_completo.m3u"), "w", encoding="utf-8", newline="\n") as f:
        f.write(completo)

    salvar_json(ARQ_ESTADO, estado)

    rel = {
        "gerado_em": marcas,
        "atualizacao": estado["atualizacoes"],
        "limite_falhas": LIMITE_FALHAS,
        "catalogo_site": len(canais),
        "com_link": sum(1 for c in canais if c["link"]),
        "na_lista": total,
        "tocando": len(ativos),
        "em_falha": len(falhando),
        "removidos": len(removidos),
        "novos": len(novos),
        "fora_do_site": len(fora_do_site),
        "por_categoria": {},
        "detalhes_falha": [
            {"nome": c["nome"], "motivo": resultados.get(c["id"], (False, ""))[1]}
            for c in falhando
        ][:40],
    }
    for cat, itens in agrupar(final, ordem_site):
        rel["por_categoria"][cat] = len(itens)
    salvar_json(ARQ_RELATORIO, rel)

    # ---- log final ----
    log("\n[5/5] Lista gerada")
    log("-" * 70)
    log(f"Canais no site .............: {len(canais)}")
    log(f"Com link direto ...........: {rel['com_link']}")
    log(f"TOCANDO (na lista) ........: {len(ativos)}")
    log(f"Em falha (mantidos) .......: {len(falhando)}  (removidos após {LIMITE_FALHAS} falhas)")
    log(f"Removidos nesta rodada ....: {len(removidos)}")
    log(f"Novos canais ..............: {len(novos)}")
    log(f"Fora do site (limpo) ......: {len(fora_do_site)}")
    log(f"Total na lista ............: {total} canais")
    log("-" * 70)
    log("Canais por categoria:")
    for cat, n in rel["por_categoria"].items():
        log(f"   {cat:<20} {n:>4}")
    if removidos:
        log("-" * 70)
        log(f"Removidos após {LIMITE_FALHAS} falhas:")
        for c in removidos[:20]:
            log(f"   - {c['nome']}")
    log("-" * 70)
    log(f"Arquivo: {os.path.basename(ARQ_LISTA)}  ({total} canais)")
    log(f"Tempo total: {time.time()-inicio:.0f}s")

    with open("log_ultima_execucao.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(LOG_LINHAS) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
