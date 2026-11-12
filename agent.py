"""Agente de promoções: a cada execução busca ofertas no Promobit para os itens pendentes da planilha.

A busca não usa IA. O Claude só é chamado quando aparecem ofertas novas, para filtrar as relevantes,
avaliar o histórico de preços e escrever a mensagem. Cada oferta vai numa mensagem própria, com print.
"""
import csv
import html
import io
import json
import os
import re
import sys
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import anthropic

SHEET_CSV_URL = os.environ["SHEET_CSV_URL"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
# Quem marcar nas super promoções: "id:Nome,@username"
_mencoes = [m.strip() for m in os.environ.get("MENCOES", "").split(",") if m.strip()]
MENCOES = [m.split(":", 1) for m in _mencoes if ":" in m]
USERNAMES = [m for m in _mencoes if m.startswith("@")]
# Produtos vigiados direto na loja: [{"nome": "...", "url": "https://www.amazon.com.br/dp/ASIN", "max": 1600}]
MONITORES = json.loads(os.environ.get("MONITORES") or "[]")

ESTADO = "estado.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
      "Accept-Language": "pt-BR,pt;q=0.9"}
MAX_IDADE = timedelta(days=3)

SYSTEM = """Você filtra ofertas do Promobit para um casal montando a casa nova no Brasil.
Recebe a lista de itens que eles querem (com marca/modelo preferido, prioridade e preço máximo opcional)
e ofertas candidatas, cada uma com um resumo do histórico de preços já visto para aquele item
(menor, mediana, quantas vezes apareceu, últimos registros) e o desconto real contra a mediana.

Para cada candidata decida:
- enviar: true só se o produto é de fato o item procurado (ex.: "Hack" é rack de sala, não hacking),
  respeita marca/modelo quando informado, respeita o preço máximo, e o preço é bom comparado ao histórico
  e ao preço antigo. Descarte descontos falsos (preço "antigo" inflado) e acessórios/peças.
- super: true só para ofertas excepcionais: menor preço do histórico, ou desconto real acima de ~30%
  em item de prioridade Alta/Média.
- mensagem: texto puro em português, no máximo 600 caracteres, neste formato:
🔥 Item - Produto resumido
💰 Agora: R$ X na Loja (antes R$ Y, -Z%)
📉 Histórico: preços anteriores com datas, ou "primeira vez que vejo esse produto"
✅ Veredito: uma frase dizendo se vale comprar agora e por quê
Use só os números recebidos; nunca invente preços. Se enviar for false, mensagem pode ser vazia."""

SCHEMA = {
    "type": "object",
    "properties": {"ofertas": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "enviar": {"type": "boolean"},
            "super": {"type": "boolean"},
            "mensagem": {"type": "string"},
        },
        "required": ["id", "enviar", "super", "mensagem"],
        "additionalProperties": False,
    }}},
    "required": ["ofertas"],
    "additionalProperties": False,
}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read().decode("utf-8")


def load_items():
    rows = csv.DictReader(io.StringIO(get(SHEET_CSV_URL)))
    return [r for r in rows if r.get("Comprado", "").strip().upper() not in ("TRUE", "VERDADEIRO")]


def queries(item):
    nome = re.sub(r"\(.*?\)", "", item["Item"]).strip()
    marcas = [re.sub(r"\(.*?\)", "", m).strip() for m in item.get("Modelo / Marca", "").split("/")]
    marcas = [m for m in marcas if m]
    if not marcas:
        return [nome]
    # modelo completo (texto longo) já é a busca; marca curta vira "item marca"
    return [m if len(m) > 25 else f"{nome} {m}" for m in marcas]


def normalizar(texto):
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", sem_acento.lower()).strip()


PALAVRAS_VAZIAS = {"de", "da", "do", "e", "com", "para", "ate", "a", "o"}


def palavras(item):
    texto = normalizar(item["Item"] + " " + item.get("Modelo / Marca", ""))
    return {p for p in texto.split() if len(p) >= 2 and p not in PALAVRAS_VAZIAS}


def parece_o_item(titulo, item):
    """Pré-filtro grátis: o título precisa citar o item ou a marca, senão nem gasta token com ele."""
    return bool(palavras(item) & set(normalizar(titulo).split()))


def impressao(o):
    """Mesma oferta repostada (outro ID, mesmo produto e preço) tem a mesma impressão digital."""
    return f"{normalizar(o['offer_title'])[:60]}|{round(o['offer_price'])}"


def resumo(historico, preco):
    """Estatística pronta para a IA: menos tokens e nada de conta de cabeça."""
    precos = sorted(p for _, p, _ in historico if p)
    if not precos:
        return {"vezes_visto": 0}
    mediana = precos[len(precos) // 2]
    return {
        "vezes_visto": len(precos),
        "menor": precos[0],
        "mediana": mediana,
        "desconto_vs_mediana_pct": round((1 - preco / mediana) * 100),
        "e_o_menor_ja_visto": preco <= precos[0],
        "ultimos": [f"{d} R$ {p} {t[:40]}" for d, p, t in sorted(historico)[-5:]],
    }


def search(q):
    try:
        data = json.loads(get("https://api.promobit.com.br/search?q=" + urllib.parse.quote(q)))
    except Exception as e:  # uma busca ruim não derruba as outras
        print(f"busca '{q}' falhou: {e}")
        return [], []
    return data.get("active_offers") or [], data.get("finished_offers") or []


def published(o):
    return datetime.strptime(o["offer_published"], "%Y-%m-%dT%H:%M:%S%z")


def avaliar(itens, candidatas):
    client = anthropic.Anthropic()
    prompt = json.dumps({"itens": itens, "candidatas": candidatas}, ensure_ascii=False)
    response = client.beta.messages.create(
        model="claude-opus-5-5",
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude recusou: {response.stop_details}")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["ofertas"]


BLOQUEIOS = ("403 error", "request could not be satisfied", "access denied", "acesso negado", "captcha",
             "digite os caracteres", "nao sou um robo", "are you a robot", "pagina indisponivel", "unusual traffic")


def bloqueio_na_pagina(status, texto):
    """A loja barrou o navegador automático? Então o print não presta (sairia uma tela de erro)."""
    return status >= 400 or any(b in normalizar(texto) for b in BLOQUEIOS)


def print_oferta(url):
    """Print da página da oferta, como se alguém entrasse e tirasse um screenshot. None se a loja bloquear."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")  # Chrome já vem instalado no runner do GitHub
            page = browser.new_page(viewport={"width": 430, "height": 820}, device_scale_factor=2, locale="pt-BR")
            resposta = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)  # deixa redirecionar, e imagens e preço aparecerem
            texto = page.title() + " " + page.inner_text("body")[:3000]
            bloqueado = bloqueio_na_pagina(resposta.status if resposta else 200, texto)
            png = None if bloqueado else page.screenshot()
            browser.close()
            if bloqueado:
                print("print bloqueado pela loja, usando foto do produto")
            return png
    except Exception as e:  # ponytail: sem print, cai para a foto do produto
        print(f"print falhou ({e}), usando foto do produto")
        return None


def telegram(method, fields, files=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    if not files:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    else:  # multipart para enviar o print
        boundary = "radar-ofertas"
        body = b""
        for k, v in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, (name, content) in files.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\nContent-Type: image/png\r\n\r\n'.encode()
            body += content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    for tentativa in range(3):
        try:
            return urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code != 429 or tentativa == 2:
                raise
            espera = json.loads(e.read()).get("parameters", {}).get("retry_after", 5)
            time.sleep(espera)  # Telegram pediu calma: espera e tenta de novo


def grafico(historico):
    """Link de um gráfico do histórico de preços (QuickChart gera a imagem, sem dependência nenhuma)."""
    if len(historico) < 2:
        return None
    pontos = sorted(historico)[-30:]
    config = {
        "type": "line",
        "data": {"labels": [d[5:] for d, _, _ in pontos],
                 "datasets": [{"label": "Preço (R$)", "data": [p for _, p, _ in pontos], "fill": False}]},
    }
    return "https://quickchart.io/chart?c=" + urllib.parse.quote(json.dumps(config, separators=(",", ":")))


def marcar_todos():
    marcas = " ".join(f'<a href="tg://user?id={uid}">{html.escape(nome)}</a>' for uid, nome in MENCOES)
    return marcas + "".join(f" {m}" for m in USERNAMES)


def enviar(oferta, mensagem, super_promo, historico):
    link = f"https://www.promobit.com.br/oferta/{oferta['offer_slug']}/"
    aprovado, laudo = testar_link(link)
    if not aprovado:
        raise RuntimeError(f"link reprovado no teste: {laudo}")
    texto = html.escape(mensagem)
    if oferta.get("offer_coupon"):
        texto += f"\n🎟️ Cupom: <code>{html.escape(oferta['offer_coupon'])}</code>"
    texto += f"\n🔒 Link testado: {html.escape(laudo)}"
    if super_promo:
        texto = f"🚨🚨 SUPER PROMOÇÃO 🚨🚨 {marcar_todos()}\n\n" + texto
    botoes = [{"text": "🛒 Ver oferta", "url": link}]
    if grafico(historico):
        botoes.append({"text": "📈 Histórico de preços", "url": grafico(historico)})
    fields = {"chat_id": TELEGRAM_CHAT_ID, "caption": texto[:1024], "parse_mode": "HTML",
              "reply_markup": json.dumps({"inline_keyboard": [botoes]})}
    enviar_com_imagem(fields, link, "https://i.promobit.com.br/400" + oferta["offer_photo"])


def preco_amazon(pagina):
    """Preço atual de uma página de produto da Amazon, ou None se indisponível."""
    m = re.search(r'"priceAmount":([\d.]+)', pagina)
    return float(m.group(1)) if m else None


def reais(valor):
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# Só alerta de lojas grandes e conhecidas (nome como aparece no Zoom/Buscapé/Promobit, sem acento e minúsculo).
LOJAS_CONFIAVEIS = {
    "amazon", "magazine luiza", "magalu", "fast shop", "casas bahia", "ponto", "extra",
    "carrefour", "americanas", "submarino", "shoptime", "mercado livre", "leroy merlin", "kabum",
    "midea", "loja midea", "shopee",
}

# Um link só vai para o grupo se apontar para um destes domínios (ou subdomínio deles).
DOMINIOS_CONFIAVEIS = {
    "zoom.com.br", "buscape.com.br", "promobit.com.br", "amazon.com.br", "magazineluiza.com.br", "magalu.com",
    "fastshop.com.br", "casasbahia.com.br", "pontofrio.com.br", "extra.com.br", "carrefour.com.br",
    "americanas.com.br", "submarino.com.br", "shoptime.com.br", "mercadolivre.com.br", "leroymerlin.com.br",
    "kabum.com.br", "midea.com.br", "midea.com", "shopee.com.br",
}


def confiavel(loja):
    return normalizar(loja or "") in LOJAS_CONFIAVEIS


def dominio_confiavel(host):
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in DOMINIOS_CONFIAVEIS)


def bloqueado_cloudflare(host):
    """Consulta o DNS de segurança da Cloudflare (1.1.1.2): domínio de malware/phishing volta como 0.0.0.0.

    Devolve True (bloqueado), False (limpo) ou None (não deu para consultar).
    """
    try:
        req = urllib.request.Request(
            "https://security.cloudflare-dns.com/dns-query?type=A&name=" + urllib.parse.quote(host),
            headers={"accept": "application/dns-json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            resposta = json.loads(r.read())
    except Exception as e:
        print(f"cloudflare não respondeu para {host}: {e}")
        return None
    censurado = any("Censored" in c for c in resposta.get("Comment", []))
    return censurado or any(a.get("data") == "0.0.0.0" for a in resposta.get("Answer", []))


def testar_link(url):
    """Testador de link: só passa HTTPS, em domínio conhecido, sem alerta da Cloudflare e com a página no ar.

    Devolve (aprovado, laudo). O laudo vai na mensagem do grupo.
    """
    partes = urllib.parse.urlparse(url)
    if partes.scheme != "https":
        return False, "link sem HTTPS"
    if not dominio_confiavel(partes.hostname):
        return False, f"domínio desconhecido ({partes.hostname})"
    seguranca = bloqueado_cloudflare(partes.hostname)
    if seguranca:
        return False, f"Cloudflare marca {partes.hostname} como malicioso"
    laudo = "HTTPS · domínio conhecido · " + ("Cloudflare sem alerta" if seguranca is False else "Cloudflare fora do ar")
    for tentativa in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                destino = urllib.parse.urlparse(r.geturl())
                if destino.scheme != "https" or not dominio_confiavel(destino.hostname):
                    return False, f"link redireciona para fora ({destino.hostname})"
                return True, laudo + " · página no ar"
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503):  # loja barrando robô: o domínio é bom, mas não deu para abrir a página
                return True, laudo + " · loja não deixou conferir a página"
            if tentativa:
                return False, f"página fora do ar (erro {e.code})"
        except Exception as e:
            if tentativa:
                return False, f"página não abriu ({e})"
        time.sleep(3)


def get_com_retry(url, nome):
    for tentativa in range(4):  # lojas às vezes respondem 503 para robôs: tenta de novo
        try:
            return get(url)
        except Exception as e:
            print(f"monitor '{nome}' tentativa {tentativa + 1} falhou: {e}")
            time.sleep(5 * (tentativa + 1))
    return None


def serve(titulo, alvo):
    """A oferta não pode ter nada da lista de exclusão (cor branca, 220V, kit...)."""
    t = normalizar(titulo)
    return not any(normalizar(p) in t for p in alvo.get("excluir", []))


def ofertas_amazon(alvo):
    """Preço direto nas páginas de produto da Amazon."""
    encontradas = []
    for url in alvo.get("amazon", []):
        pagina = get_com_retry(url, alvo["nome"])
        preco = preco_amazon(pagina) if pagina else None
        if preco is None:
            continue
        foto = re.search(r'data-old-hires="([^"]+)"', pagina)
        encontradas.append({"loja": "Amazon", "preco": preco, "link": url, "foto": foto and foto.group(1)})
    return encontradas


def ofertas_comparadores(alvo):
    """Ofertas de cada código de modelo no Zoom; se o Zoom falhar, tenta o Buscapé (mesmo formato)."""
    encontradas = []
    for modelo in alvo.get("modelos", []):
        for site in ("https://www.zoom.com.br", "https://www.buscape.com.br"):
            pagina = get_com_retry(f"{site}/search?q=" + urllib.parse.quote(modelo), alvo["nome"])
            m = pagina and re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', pagina, re.S)
            try:
                hits = json.loads(m.group(1))["props"]["initialReduxState"]["hits"]["hits"] if m else []
            except (KeyError, ValueError):
                hits = []
            if hits:
                break
        for h in hits:
            loja = h.get("merchantName") or (h.get("bestOffer") or {}).get("merchantName")
            # só anúncio de loja ("offer"): a página agrupada do comparador ("product") mistura as cores
            # e mostra o preço da mais barata, que pode ser a branca
            # "agrupada": true no alvo aceita a página agrupada, para produto que só existe em uma cor
            tipos = ("offer", "product") if alvo.get("agrupada") else ("offer",)
            nome = normalizar(h.get("name", ""))
            # ponytail: cada palavra da busca precisa estar no título (substring); "excluir" corta o resto
            if (h.get("type") not in tipos or not all(p in nome for p in normalizar(modelo).split())
                    or not serve(h["name"], alvo) or not confiavel(loja)):
                continue
            encontradas.append({"loja": loja, "preco": float(h["price"]), "titulo": h["name"],
                                "link": site + h["url"],
                                "foto": re.sub(r"_\d+_\d+\.", "_600_600.", h.get("image") or "") or None})
    return encontradas


def ofertas_promobit(alvo):
    """Promoções recentes postadas no Promobit (pega cupom e oferta relâmpago que comparador não mostra)."""
    cfg = alvo.get("promobit")
    if not cfg:
        return []
    agora = datetime.now(timezone.utc)
    encontradas = []
    consultas = [cfg["q"]] if isinstance(cfg["q"], str) else cfg["q"]
    ativas = {o["offer_id"]: o for q in consultas for o in search(q)[0]}  # várias buscas, sem repetir oferta
    for o in ativas.values():
        t = normalizar(o["offer_title"])
        if (agora - published(o) > MAX_IDADE or not all(normalizar(p) in t for p in cfg.get("todos", []))
                or not any(normalizar(p) in t for p in cfg.get("algum", [""]))
                or not serve(o["offer_title"], alvo) or not confiavel(o["store_name"])):
            continue
        encontradas.append({"loja": o["store_name"].strip(), "preco": float(o["offer_price"]), "titulo": o["offer_title"],
                            "link": f"https://www.promobit.com.br/oferta/{o['offer_slug']}/",
                            "foto": "https://i.promobit.com.br/400" + o["offer_photo"], "cupom": o.get("offer_coupon")})
    return encontradas


def enviar_com_imagem(fields, link, foto):
    """Print da página; se a loja bloquear o print, foto do produto; sem foto, só o texto."""
    imagem = print_oferta(link)
    if not imagem and foto:
        try:  # baixa a foto aqui: o Telegram não consegue buscar imagem de alguns sites
            with urllib.request.urlopen(urllib.request.Request(foto, headers=UA), timeout=30) as r:
                imagem = r.read()
        except Exception as e:
            print(f"foto do produto não baixou ({e}), enviando só o texto")
    if imagem:
        telegram("sendPhoto", fields, {"photo": ("oferta.png", imagem)})
    else:
        telegram("sendMessage", {k: v for k, v in fields.items() if k != "caption"} | {"text": fields["caption"]})


def alertar(alvo, oferta, hist):
    aprovado, laudo = testar_link(oferta["link"])
    if not aprovado:
        raise RuntimeError(f"link reprovado no teste: {laudo}")
    precos = [p for _, p, _ in hist]
    texto = (f"🚨🎯 PREÇO ALVO ATINGIDO 🎯🚨 {marcar_todos()}\n\n"
             f"🔥 {html.escape(oferta.get('titulo') or alvo['nome'])}\n"
             f"💰 Agora: <b>{reais(oferta['preco'])}</b> na {html.escape(oferta['loja'])} "
             f"(alvo: abaixo de {reais(alvo['max'])})\n"
             f"📉 Histórico: menor {reais(min(precos))} · maior {reais(max(precos))} · {len(precos)} preços registrados\n")
    if oferta.get("cupom"):
        texto += f"🎟️ Cupom: <code>{html.escape(oferta['cupom'])}</code>\n"
    texto += f"🔒 Link testado: {html.escape(laudo)}\n✅ Loja conhecida e abaixo do preço que vocês querem pagar. Corre!"
    botoes = [{"text": f"🛒 Comprar na {oferta['loja']}", "url": oferta["link"]}]
    if grafico(hist):
        botoes.append({"text": "📈 Histórico de preços", "url": grafico(hist)})
    fields = {"chat_id": TELEGRAM_CHAT_ID, "caption": texto[:1024], "parse_mode": "HTML",
              "reply_markup": json.dumps({"inline_keyboard": [botoes]})}
    enviar_com_imagem(fields, oferta["link"], oferta.get("foto"))


def monitorar(estado):
    """Vigia produtos específicos em várias fontes e avisa (marcando todos) quando uma loja conhecida fica
    abaixo do alvo. Sem IA.

    Cada alvo: {"nome", "max", "excluir": [...], "modelos": [códigos no Zoom/Buscapé],
                "promobit": {"q", "todos": [...], "algum": [...]}, "amazon": [urls]}
    """
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for alvo in MONITORES:
        info = estado.setdefault("monitores", {}).setdefault(alvo["nome"], {"alertado": {}, "historico": []})
        ofertas, fontes = [], []
        for fonte in (ofertas_comparadores, ofertas_promobit, ofertas_amazon):
            try:  # uma fonte fora do ar não derruba as outras
                achadas = fonte(alvo)
            except Exception as e:
                print(f"monitor '{alvo['nome']}': {fonte.__name__} falhou: {e}")
                achadas = []
            ofertas += achadas
            fontes.append(f"{fonte.__name__.removeprefix('ofertas_')}={len(achadas)}")
        if not ofertas:
            print(f"monitor {alvo['nome']}: nenhuma oferta em loja conhecida agora ({', '.join(fontes)})")
            continue
        melhor = min(ofertas, key=lambda o: o["preco"])
        print(f"monitor {alvo['nome']}: menor {reais(melhor['preco'])} na {melhor['loja']} "
              f"(alvo {reais(alvo['max'])}; {', '.join(fontes)})")
        hist = info["historico"]
        if not hist or hist[-1][0] != hoje or hist[-1][1] != melhor["preco"]:
            hist.append([hoje, melhor["preco"], alvo["nome"]])
            info["historico"] = hist[-100:]
        por_loja = {}
        for o in ofertas:  # vários anúncios na mesma loja: vale o mais barato
            if o["loja"] not in por_loja or o["preco"] < por_loja[o["loja"]]["preco"]:
                por_loja[o["loja"]] = o
        for loja in list(info["alertado"]):
            if loja not in por_loja or por_loja[loja]["preco"] >= alvo["max"]:
                del info["alertado"][loja]  # subiu ou sumiu: a próxima queda avisa de novo
        for loja, oferta in por_loja.items():
            ja_avisado = info["alertado"].get(loja)
            if oferta["preco"] >= alvo["max"] or (ja_avisado is not None and oferta["preco"] >= ja_avisado - 1):
                continue  # acima do alvo, ou já avisamos esse preço nessa loja
            try:
                alertar(alvo, oferta, hist)
                info["alertado"][loja] = oferta["preco"]
            except Exception as e:
                print(f"alerta de '{alvo['nome']}' ({loja}) falhou: {e}")


def rodar_lista(estado, primeira_vez):
    """Promoções do Promobit para os itens pendentes da planilha, avaliadas pelo Claude."""
    vistos = set(estado["vistos"])
    enviados = set(estado.get("enviados", []))
    agora = datetime.now(timezone.utc)
    itens = load_items()
    candidatas, ofertas = [], {}

    for item in itens:
        nome = item["Item"]
        hist = estado["historico"].setdefault(nome, [])
        for q in queries(item):
            ativas, encerradas = search(q)
            for o in ativas + encerradas:  # histórico grátis: tudo que o Promobit já mostrou desse item
                registro = [o["offer_published"][:10], o["offer_price"], o["offer_title"][:80]]
                if registro not in hist:
                    hist.append(registro)
            for o in ativas:
                if (o["offer_id"] in vistos or o["offer_id"] in ofertas or agora - published(o) > MAX_IDADE
                        or impressao(o) in enviados or not parece_o_item(o["offer_title"], item)):
                    vistos.add(o["offer_id"])
                    continue
                ofertas[o["offer_id"]] = o
                o["_item"] = nome
                candidatas.append({
                    "id": o["offer_id"], "item": nome, "titulo": o["offer_title"], "loja": o["store_name"],
                    "preco": o["offer_price"], "preco_antigo": o["offer_old_price"] or None,
                    "cupom": o["offer_coupon"], "historico": resumo(hist, o["offer_price"]),
                })
        estado["historico"][nome] = sorted(hist)[-50:]

    print(f"{len(itens)} itens pendentes, {len(candidatas)} ofertas novas")
    if candidatas and not primeira_vez:  # sem estado salvo: só registra o que já existe, não reenvia
        for r in avaliar(itens, candidatas):
            if r["enviar"] and r["id"] in ofertas:
                if impressao(ofertas[r["id"]]) in enviados:
                    continue  # repost do mesmo produto na mesma execução
                try:
                    oferta = ofertas[r["id"]]
                    enviar(oferta, r["mensagem"], r["super"], estado["historico"][oferta["_item"]])
                    enviados.add(impressao(ofertas[r["id"]]))
                except Exception as e:  # uma oferta com problema não impede as outras
                    print(f"envio da oferta {r['id']} falhou: {e}")
    vistos.update(ofertas)  # avaliadas uma vez só, para não gastar token de novo
    estado["vistos"] = sorted(vistos)[-3000:]
    estado["enviados"] = sorted(enviados)[-1000:]


FALHAS_ATE_AVISAR = 3  # ~30 min seguidos de erro antes de incomodar o grupo


def avisar_saude(estado, erros):
    """Avisa no grupo quando o radar fica quebrado por várias rodadas, e de novo quando volta."""
    if erros:
        estado["falhas"] = estado.get("falhas", 0) + 1
        if estado["falhas"] == FALHAS_ATE_AVISAR:
            detalhe = html.escape(" | ".join(erros))[:600]
            telegram("sendMessage", {"chat_id": TELEGRAM_CHAT_ID,
                                     "text": f"⚠️ O radar está com problema há {FALHAS_ATE_AVISAR} rodadas seguidas:\n{detalhe}"})
    else:
        if estado.get("falhas", 0) >= FALHAS_ATE_AVISAR:
            telegram("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": "✅ O radar voltou ao normal."})
        estado["falhas"] = 0


def main():
    try:
        estado = json.load(open(ESTADO, encoding="utf-8"))
        primeira_vez = False
    except (OSError, ValueError):  # sem estado, ou arquivo corrompido: recomeça do zero
        estado, primeira_vez = {}, True
    estado.setdefault("vistos", [])
    estado.setdefault("historico", {})

    erros = []
    for nome, etapa in (("lista de compras", lambda: rodar_lista(estado, primeira_vez)),
                        ("monitor de preço alvo", lambda: monitorar(estado))):
        try:  # uma etapa quebrada não impede a outra
            etapa()
        except Exception as e:
            traceback.print_exc()
            erros.append(f"{nome}: {e}")
    try:
        avisar_saude(estado, erros)
    except Exception as e:
        print(f"aviso de saúde falhou: {e}")
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)
    if erros:
        sys.exit(1)


def _teste():
    item = {"Item": "Máquina de Lavar", "Modelo / Marca": "Samsung / LG / Midea"}
    assert parece_o_item("Lavadora LG Vc5 12kg Branca", item)
    assert not parece_o_item("SELEÇÃO CASA & ELETRO: Geladeira", {"Item": "Televisão", "Modelo / Marca": ""})
    assert queries({"Item": "Air Fryer", "Modelo / Marca": "Mondial / WAP"}) == ["Air Fryer Mondial", "Air Fryer WAP"]
    a = {"offer_title": "Air Fryer Mondial 4L", "offer_price": 224.0}
    assert impressao(a) == impressao({"offer_title": "AIR FRYER MONDIAL 4l", "offer_price": 224.3})
    assert grafico([["2026-10-01", 100, "x"]]) is None
    assert grafico([["2026-10-01", 100, "x"], ["2026-10-02", 90, "x"]]).startswith("https://quickchart.io/")
    r = resumo([["2026-10-01", 100, "a"], ["2026-10-02", 200, "b"], ["2026-10-03", 300, "c"]], 100)
    assert r["mediana"] == 200 and r["desconto_vs_mediana_pct"] == 50 and r["e_o_menor_ja_visto"]
    assert preco_amazon('x "priceAmount":1899.00,"y"') == 1899.0 and preco_amazon("sem preço") is None
    assert reais(1599.9) == "R$ 1.599,90"
    assert confiavel("Magazine Luiza") and confiavel("Midea ") and not confiavel("Webcontinental")
    assert confiavel("Shopee") and dominio_confiavel("shopee.com.br")
    assert dominio_confiavel("www.amazon.com.br") and not dominio_confiavel("amazon.com.br.golpe.xyz")
    assert testar_link("http://www.amazon.com.br/dp/X")[0] is False  # sem HTTPS
    assert testar_link("https://amaz0n-ofertas.xyz/dp/X")[0] is False  # domínio desconhecido
    alvo = {"excluir": ["branc", "220", "kit"]}
    assert serve("Lava Louças 8 Serviços Preta Touch Plus Midea 127v", alvo)
    assert not serve("Lava Louças 8 Serviços Branca Touch Plus Midea", alvo)
    assert not serve("Lava Louças 8 Serviços Cinza Touch Plus Midea 220V", alvo)
    assert bloqueio_na_pagina(403, "") and bloqueio_na_pagina(200, "403 ERROR The request could not be satisfied")
    assert not bloqueio_na_pagina(200, "Robô Aspirador Xiaomi por R$ 999")
    print("testes ok")


if __name__ == "__main__":
    _teste() if "--teste" in sys.argv else main()
