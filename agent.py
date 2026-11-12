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
import time
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


def print_oferta(url):
    """Print da página da oferta, como se alguém entrasse e tirasse um screenshot."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")  # Chrome já vem instalado no runner do GitHub
            page = browser.new_page(viewport={"width": 430, "height": 820}, device_scale_factor=2, locale="pt-BR")
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)  # deixa imagens e preço aparecerem
            png = page.screenshot()
            browser.close()
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
    texto = html.escape(mensagem)
    if oferta.get("offer_coupon"):
        texto += f"\n🎟️ Cupom: <code>{html.escape(oferta['offer_coupon'])}</code>"
    if super_promo:
        texto = f"🚨🚨 SUPER PROMOÇÃO 🚨🚨 {marcar_todos()}\n\n" + texto
    botoes = [{"text": "🛒 Ver oferta", "url": link}]
    if grafico(historico):
        botoes.append({"text": "📈 Histórico de preços", "url": grafico(historico)})
    fields = {"chat_id": TELEGRAM_CHAT_ID, "caption": texto[:1024], "parse_mode": "HTML",
              "reply_markup": json.dumps({"inline_keyboard": [botoes]})}
    png = print_oferta(link)
    if png:
        telegram("sendPhoto", fields, {"photo": ("oferta.png", png)})
    else:
        telegram("sendPhoto", {**fields, "photo": "https://i.promobit.com.br/400" + oferta["offer_photo"]})


def preco_amazon(pagina):
    """Preço atual de uma página de produto da Amazon, ou None se indisponível."""
    m = re.search(r'"priceAmount":([\d.]+)', pagina)
    return float(m.group(1)) if m else None


def reais(valor):
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# Só alerta de lojas grandes e conhecidas (nome como aparece no Zoom/Buscapé, sem acento e minúsculo).
LOJAS_CONFIAVEIS = {
    "amazon", "magazine luiza", "magalu", "fast shop", "webcontinental", "casas bahia", "ponto", "extra",
    "carrefour", "americanas", "submarino", "shoptime", "mercado livre", "leroy merlin", "kabum",
    "midea", "loja midea", "polishop", "colombo", "lojas colombo", "havan", "girafa", "fujioka",
}


def confiavel(loja):
    return normalizar(loja or "") in LOJAS_CONFIAVEIS


def get_com_retry(url, nome):
    for tentativa in range(4):  # lojas às vezes respondem 503 para robôs: tenta de novo
        try:
            return get(url)
        except Exception as e:
            print(f"monitor '{nome}' tentativa {tentativa + 1} falhou: {e}")
            time.sleep(5 * (tentativa + 1))
    return None


def ofertas_amazon(alvo):
    pagina = get_com_retry(alvo["url"], alvo["nome"])
    preco = preco_amazon(pagina) if pagina else None
    if preco is None:
        return []
    foto = re.search(r'data-old-hires="([^"]+)"', pagina)
    return [{"loja": "Amazon", "preco": preco, "link": alvo["url"], "foto": foto and foto.group(1)}]


def ofertas_zoom(alvo):
    """Todas as ofertas do modelo no Zoom (agrega dezenas de lojas), já sem cor proibida e só de loja confiável."""
    pagina = get_com_retry("https://www.zoom.com.br/search?q=" + urllib.parse.quote(alvo["busca"]), alvo["nome"])
    if not pagina:
        return []
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', pagina, re.S)
    hits = json.loads(m.group(1))["props"]["initialReduxState"]["hits"]["hits"] if m else []
    proibidas = [normalizar(p) for p in alvo.get("excluir", [])]
    encontradas = []
    for h in hits:
        nome = normalizar(h.get("name", ""))
        loja = h.get("merchantName") or (h.get("bestOffer") or {}).get("merchantName")
        if normalizar(alvo["busca"]) not in nome or any(p in nome for p in proibidas) or not confiavel(loja):
            continue
        encontradas.append({"loja": loja, "preco": float(h["price"]), "titulo": h["name"],
                            "link": "https://www.zoom.com.br" + h["url"], "foto": h.get("image")})
    return encontradas


def alertar(alvo, oferta, hist):
    precos = [p for _, p, _ in hist]
    texto = (f"🚨🎯 PREÇO ALVO ATINGIDO 🎯🚨 {marcar_todos()}\n\n"
             f"🔥 {html.escape(oferta.get('titulo') or alvo['nome'])}\n"
             f"💰 Agora: <b>{reais(oferta['preco'])}</b> na {html.escape(oferta['loja'])} "
             f"(alvo: abaixo de {reais(alvo['max'])})\n"
             f"📉 Histórico: menor {reais(min(precos))} · maior {reais(max(precos))} · {len(precos)} preços registrados\n"
             f"✅ Loja confiável e abaixo do preço que vocês querem pagar. Corre!")
    botoes = [{"text": f"🛒 Comprar na {oferta['loja']}", "url": oferta["link"]}]
    if grafico(hist):
        botoes.append({"text": "📈 Histórico de preços", "url": grafico(hist)})
    fields = {"chat_id": TELEGRAM_CHAT_ID, "caption": texto[:1024], "parse_mode": "HTML",
              "reply_markup": json.dumps({"inline_keyboard": [botoes]})}
    # foto do produto primeiro: Zoom e Amazon bloqueiam navegador automático e o print sairia como "403 ERROR"
    png = None if oferta.get("foto") else print_oferta(oferta["link"])
    if oferta.get("foto"):
        telegram("sendPhoto", {**fields, "photo": oferta["foto"]})
    elif png:
        telegram("sendPhoto", fields, {"photo": ("produto.png", png)})
    else:
        telegram("sendMessage", {k: v for k, v in fields.items() if k != "caption"} | {"text": fields["caption"]})


def monitorar(estado):
    """Vigia modelos específicos e avisa (marcando todos) quando alguma loja confiável fica abaixo do alvo. Sem IA.

    Cada alvo é {"nome", "max"} mais "url" (página da Amazon) ou "busca" (código do modelo no Zoom, com "excluir"
    para cores que não servem).
    """
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for alvo in MONITORES:
        chave = alvo.get("url") or alvo["busca"]
        info = estado.setdefault("monitores", {}).setdefault(chave, {"alertado": {}, "historico": []})
        if not isinstance(info.get("alertado"), dict):
            info["alertado"] = {}  # formato antigo: um preço só
        ofertas = ofertas_amazon(alvo) if "url" in alvo else ofertas_zoom(alvo)
        if not ofertas:
            print(f"monitor {alvo['nome']}: nenhuma oferta confiável agora")
            continue
        melhor = min(o["preco"] for o in ofertas)
        print(f"monitor {alvo['nome']}: menor {reais(melhor)} em {len(ofertas)} oferta(s) (alvo {reais(alvo['max'])})")
        hist = info["historico"]
        if not hist or hist[-1][0] != hoje or hist[-1][1] != melhor:
            hist.append([hoje, melhor, alvo["nome"]])
            info["historico"] = hist[-100:]
        por_loja = {}
        for o in ofertas:  # vários anúncios na mesma loja: vale o mais barato
            if o["loja"] not in por_loja or o["preco"] < por_loja[o["loja"]]["preco"]:
                por_loja[o["loja"]] = o
        for loja, oferta in por_loja.items():
            ja_avisado = info["alertado"].get(loja)
            if oferta["preco"] >= alvo["max"]:
                info["alertado"].pop(loja, None)  # voltou a subir: o próximo mergulho avisa de novo
                continue
            if ja_avisado is not None and oferta["preco"] >= ja_avisado - 1:
                continue  # já avisamos esse preço nessa loja; só avisa de novo se cair mais
            try:
                alertar(alvo, oferta, hist)
                info["alertado"][loja] = oferta["preco"]
            except Exception as e:
                print(f"alerta de '{alvo['nome']}' ({loja}) falhou: {e}")


def main():
    primeira_vez = not os.path.exists(ESTADO)
    estado = {"vistos": [], "historico": {}} if primeira_vez else json.load(open(ESTADO, encoding="utf-8"))
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

    monitorar(estado)
    estado["vistos"] = sorted(vistos)[-3000:]
    estado["enviados"] = sorted(enviados)[-1000:]
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)




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
    assert confiavel("Magazine Luiza") and confiavel("Fast Shop") and not confiavel("Loja do Zé")
    print("testes ok")


if __name__ == "__main__":
    import sys
    _teste() if "--teste" in sys.argv else main()
