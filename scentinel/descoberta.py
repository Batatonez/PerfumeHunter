"""Busca automática: o "canal de promos".

Três jeitos de achar oferta, todos passando pelo mesmo motor dos alertas
(níveis, anti-spam, sanidade):

1. VARREDURA (o principal, estilo canal de promos): de hora em hora percorre as ofertas
   de perfume das lojas (Mercado Livre por vários termos; lojas VTEX por maior desconto)
   e posta QUALQUER perfume em promo boa, os maiores descontos primeiro.
2. CATÁLOGO: busca dirigida dos perfumes do catalogo.toml (referência de preço melhor).
3. AO VIVO: o /locate pesquisa nas lojas na hora.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta

from scentinel import busca, catalogo, db, promo
from scentinel.caca import pct_do_post
from scentinel.coleta import Leitura, Resultado
from scentinel.fontes import ResultadoBusca, fonte_busca_item
from scentinel.formato import formatar_reais, pct_inteiro

log = logging.getLogger("scentinel")

ERROS_PRA_PAUSAR = 3  # erros seguidos numa loja -> pausa 24 h (ela provavelmente não tem essa busca)
RELER_MESMO_PRECO_APOS = timedelta(hours=6)  # preço igual: só registra de novo depois disso
NAO_REPOSTAR_POR = timedelta(days=7)  # mesmo anúncio a preço igual ou maior que o já postado: não reposta
PRECO_SUSPEITO = 0.35  # perfume do catálogo a menos de 35% do normal: quase certo que não é o produto
TEMPO_MAX_AO_VIVO_S = 60  # /locate: loja que não respondeu nisso fica de fora da resposta
PAUSA_LIMITE_MAX_HORAS = 2  # pausa por limite de velocidade (429) nunca passa disso
INTERVALO_ALERTA_USUARIO = timedelta(hours=3)  # cada /alerta é pesquisado no máximo a cada 3 h
ALERTA_REARMA_APOS = 2  # pesquisas seguidas acima do preço pra um /alerta poder avisar de novo


_NAO_E_PERFUME = re.compile(
    r"\b(vinho|cerveja|whisky|vodka|gin|licor|cachaca|espumante|copo|caneca|garrafa|squeeze|termica|"
    r"tinta|leite|suco|refrigerante|detergente|amaciante|desinfetante|alcool|cafe|cha|azeite|molho)\b"
)


def _passageiro(erro):
    """Erro de rede ou da loja fora do ar (não é a busca que não existe)."""
    texto = (erro or "").lower()
    return any(x in texto for x in ("conexão", "timeout", "renovar", "http 5"))


def _como_refazer(fonte, achado, padrao):
    """Pra checagem de sanidade: relê só esse produto quando a loja sabe (ML: 1 chamada);
    senão refaz a busca inteira."""
    if getattr(fonte, "sabe_reler", None) and fonte.sabe_reler(achado):
        return lambda: fonte.reler(achado)
    return padrao


@dataclass
class Candidato:
    achado: object
    ml: float
    item: object  # ItemCatalogo ou None
    chave: str
    fonte: object
    refazer: object = None  # função que refaz a busca (sanidade)


@dataclass
class ResultadoAoVivo:
    consulta: str
    ofertas: list = field(default_factory=list)  # [(Achado, ml, pct_vs_de)] do mais barato pro mais caro
    lojas_ok: list = field(default_factory=list)
    lojas_falha: list = field(default_factory=list)
    detalhes: list = field(default_factory=list)  # [(loja, o que ela respondeu)] pra explicar quando não acha
    links: list = field(default_factory=list)  # [(loja, link da busca no site)]: um toque abre tudo, até o que o bot não vê
    preco_bom: float | None = None  # do mais barato: abaixo disso é preço bom (None = o bot ainda não sabe)


class Descoberta:
    def __init__(self, cacador, itens, fontes, enviar_aviso=None):
        self.cacador = cacador
        self.con = cacador.con
        self.cfg = cacador.cfg.descoberta
        self.itens = list(itens)
        self.fontes = list(fontes)
        self.enviar_aviso = enviar_aviso
        self._fonte_pausada_ate = {}  # fonte.id -> datetime
        self._fonte_falhas = {}  # bloqueios seguidos
        self._fonte_erros = {}  # erros seguidos (loja fora do ar, endpoint que não existe...)
        self._fonte_avisada = {}  # último bloqueio avisado no canal (avisa de novo só se mudar)
        self._fonte_motivo = {}  # por que está pausada (pro /status e pro /locate)
        self._fonte_pausa_curta = {}  # pausada só por limite de velocidade (o /locate ainda tenta)
        self._fonte_ultimo_ok = {}  # última busca que funcionou
        self._fonte_ultimo_erro = {}  # último erro (mesmo sem pausar)
        self._ultima_varredura = None
        self.trava_varredura = asyncio.Lock()  # uma varredura por vez (loop e /varrer)
        cacador.catalogo = {i.id: i for i in self.itens}
        for fonte in self.fontes:  # a fonte pode economizar (ex.: ML pula a categoria feminina)
            if hasattr(fonte, "publico"):
                fonte.publico = self.cfg.publico

    def adicionar_itens(self, novos):
        """Catálogo automático: itens novos entram na hora (próxima rodada do catálogo já busca eles)."""
        ids = {i.id for i in self.itens}
        for item in novos:
            if item.id not in ids:
                self.itens.append(item)
                self.cacador.catalogo[item.id] = item
                ids.add(item.id)

    def remover_item(self, item_id):
        self.itens = [i for i in self.itens if i.id != item_id]
        self.cacador.catalogo.pop(item_id, None)
        db.limpar_alvo_do_item(self.con, item_id)  # o "preço bom" dele não fica valendo nos anúncios

    # ------------------------------------------------------------
    # lojas pausadas, bloqueio e erro
    # ------------------------------------------------------------

    def pausada(self, fonte, agora=None):
        pausa = self._fonte_pausada_ate.get(fonte.id)
        return bool(pausa and pausa > (agora or self.cacador.relogio()))

    def pausada_pro_locate(self, fonte, agora=None):
        """Pausa por limite de velocidade não impede uma pesquisa pedida por alguém (é uma só); bloqueio de
        verdade (login, recusa) sim."""
        return self.pausada(fonte, agora) and not self._fonte_pausa_curta.get(fonte.id)

    def estado_fontes(self, agora=None):
        """Pro /status: cada loja, se está pausada (até quando e por quê), última busca boa e último erro."""
        agora = agora or self.cacador.relogio()
        return [{
            "nome": f.nome,
            "pausada_ate": self._fonte_pausada_ate.get(f.id) if self.pausada(f, agora) else None,
            "motivo": self._fonte_motivo.get(f.id),
            "ultimo_ok": self._fonte_ultimo_ok.get(f.id),
            "ultimo_erro": self._fonte_ultimo_erro.get(f.id),
        } for f in self.fontes]

    def despausar(self):
        """O dono mandou tentar de novo agora (/despausar, /varrer): zera a pausa e a contagem que dobra ela."""
        self._fonte_pausada_ate.clear()
        self._fonte_erros.clear()
        self._fonte_falhas.clear()
        self._fonte_motivo.clear()

    async def _tratar_resultado(self, fonte, res, agora):
        """Cuida de bloqueio/erro. Devolve (ok, linha_de_log)."""
        if res.bloqueado:
            falhas = self._fonte_falhas.get(fonte.id, 0) + 1
            self._fonte_falhas[fonte.id] = falhas
            base = res.pausa_horas or self.cacador.cfg.backoff_horas
            # limite de velocidade: no máximo 2 h (com o ML de única loja, 48 h seria o canal parado 2 dias)
            teto = PAUSA_LIMITE_MAX_HORAS if res.pausa_horas else self.cacador.cfg.backoff_max_horas
            horas = min(base * 2 ** (falhas - 1), teto)
            self._fonte_pausada_ate[fonte.id] = agora + timedelta(hours=horas)
            self._fonte_motivo[fonte.id] = self._fonte_ultimo_erro[fonte.id] = res.erro
            self._fonte_pausa_curta[fonte.id] = bool(res.pausa_horas)
            # avisa uma vez por tipo de bloqueio (ex.: depois de um 429, um "login venceu" ainda avisa);
            # limite de velocidade (pausa curta) fica só no log
            if self.enviar_aviso and not res.pausa_horas and self._fonte_avisada.get(fonte.id) != res.erro:
                self._fonte_avisada[fonte.id] = res.erro
                await self.enviar_aviso(
                    f"🚫 **{fonte.nome}** bloqueou a busca ({res.erro}). Pausei essa loja por {horas:g} h, sem forçar."
                )
            return False, f"🚫 {fonte.nome}: {res.erro} (pausada {horas:g} h)"
        self._fonte_falhas.pop(fonte.id, None)
        if res.erro:
            erros = self._fonte_erros.get(fonte.id, 0) + 1
            self._fonte_erros[fonte.id] = erros
            self._fonte_ultimo_erro[fonte.id] = res.erro
            if erros >= ERROS_PRA_PAUSAR:
                # rede caiu / loja fora do ar: volta logo; erro que se repete (busca que não existe): 24 h
                horas = 1 if _passageiro(res.erro) else 24
                self._fonte_pausada_ate[fonte.id] = agora + timedelta(hours=horas)
                self._fonte_motivo[fonte.id] = f"{erros} erros seguidos: {res.erro}"
                self._fonte_erros[fonte.id] = 0
                return False, f"⏸️ {fonte.nome}: {erros} erros seguidos ({res.erro}), pausada por {horas} h"
            return False, f"❌ {fonte.nome}: {res.erro}"
        self._fonte_erros.pop(fonte.id, None)
        self._fonte_avisada.pop(fonte.id, None)
        self._fonte_motivo.pop(fonte.id, None)
        self._fonte_ultimo_ok[fonte.id] = agora
        return True, None

    # ------------------------------------------------------------
    # filtro: só posta o melhor e sem enxurrada
    # ------------------------------------------------------------

    async def _filtro(self, anuncio, oferta):
        agora = self.cacador.relogio()
        ja = db.ultimo_alerta(self.con, anuncio["id"], agora - NAO_REPOSTAR_POR)
        if ja and oferta.preco >= ja["preco"] - 0.01:
            return "já postado a esse preço"
        if db.alertas_desde(self.con, agora - timedelta(hours=1)) >= self.cfg.max_alertas_por_hora:
            return "limite de posts por hora"
        if self.cfg.so_menor_preco and anuncio.get("catalogo_id") and anuncio.get("ml"):
            outros = db.ofertas_recentes_do_item(
                self.con, anuncio["catalogo_id"], anuncio["ml"], agora - timedelta(hours=24), exceto_id=anuncio["id"]
            )
            mais_barato = min(outros, key=lambda o: o["preco"], default=None)
            if mais_barato and mais_barato["preco"] < oferta.preco:
                return f"mais barato em {mais_barato['loja']} ({formatar_reais(mais_barato['preco'])})"
        return None

    # ------------------------------------------------------------
    # achado -> anúncio
    # ------------------------------------------------------------

    def _perfume_do_item(self, item):
        nome = item.nome_exibicao
        nome_norm = busca.normalizar(nome)
        perfume = db.perfume_por_nome_norm(self.con, nome_norm)
        if perfume and perfume.get("marca") and not busca.marca_bate(item.marca, perfume["marca"]):
            # mesmo nome, outra marca (L'Homme da YSL x da Prada): cada um na sua linha, com a marca no nome
            nome = f"{item.marca} {nome}"
            nome_norm = busca.normalizar(nome)
            perfume = db.perfume_por_nome_norm(self.con, nome_norm)
        if perfume:
            return perfume
        return db.criar_perfume(
            self.con, nome, nome_norm, marca=item.marca, concentracao=item.concentracao,
            apelidos=";".join(item.busca),
        )

    def _perfume_generico(self, achado):
        nome = catalogo.nome_limpo(achado.titulo)
        nome_norm = busca.normalizar(nome)
        if not nome_norm:
            return None
        perfume = db.perfume_por_nome_norm(self.con, nome_norm)
        if perfume:
            return perfume
        return db.criar_perfume(
            self.con, nome, nome_norm, marca=achado.marca, concentracao=busca.detectar_concentracao(achado.titulo),
        )

    # ------------------------------------------------------------
    # público do canal (ex.: só masculino)
    # ------------------------------------------------------------

    def item_no_publico(self, item):
        return self.cfg.publico != "masculino" or item.publico != "fem"

    def no_publico(self, achado, item=None, titulo=None):
        """O canal posta esse perfume? Com publico = "masculino": masculino e unissex entram;
        feminino (pelo catálogo, pelo que a loja diz ou pelo título) fica de fora. Sem informação, entra.
        Item unissex ainda confere o anúncio: o "Ana Abiyedh Rouge Feminino" não entra pelo item unissex."""
        if self.cfg.publico != "masculino":
            return True
        if item and not self.item_no_publico(item):
            return False
        if item and item.publico == "masc":
            return True  # o casar já barrou título feminino
        genero = getattr(achado, "genero", None) if achado else None
        return (genero or catalogo.genero_do_texto(titulo or (achado.titulo if achado else ""))) != "f"

    def preparar(self, achado, fonte, so_item=None):
        """Achado -> Candidato (ou None se não é perfume, é lixo, não tem tamanho, não é loja confiável...).
        so_item: busca do catálogo, só aceita esse item. Não mexe no banco (roda numa thread)."""
        if not achado.url or not achado.preco or not achado.disponivel:
            return None
        if self.cfg.so_oficial and not (achado.oficial or achado.bem_avaliado):
            return None
        if so_item is not None and not self.item_no_publico(so_item):
            return None
        if so_item is not None:
            ok, _ = catalogo.casar(so_item, achado.titulo, achado.marca, self.itens)
            item = so_item if ok else None
            if not item:
                return None
        else:
            if not catalogo.e_perfume(achado.titulo):
                return None
            item = catalogo.identificar(achado.titulo, self.itens, achado.marca)
        # o tamanho do SKU vale mais que o do título (o título do produto pode citar outro tamanho)
        ml = achado.ml or catalogo.extrair_ml(achado.variacao or "") or catalogo.extrair_ml(achado.titulo)
        if not ml or ml < self.cfg.ml_min or achado.preco < self.cfg.preco_min:
            return None
        if not self.no_publico(achado, item):  # antes de soltar o item: Good Girl 30ml continua feminino
            return None
        if item and item.ml and ml not in item.ml:
            if so_item is not None:
                return None
            item = None  # tamanho fora do catálogo: trata como perfume qualquer
        if item:
            ref = item.referencia_para(ml)
            if ref and achado.preco < ref * PRECO_SUSPEITO:
                return None  # barato demais pra ser o original: contratipo/miniatura/erro
        # chave pelo título (e não pelo id do ML): assim o mesmo perfume no ML e na Época se encontram
        chave = item.id if item else catalogo.chave_generica(achado.titulo, ml, achado.marca)
        if not chave:
            return None
        return Candidato(achado, ml, item, chave, fonte)

    def preparar_lote(self, achados, fonte, so_item=None):
        return [c for c in (self.preparar(a, fonte, so_item) for a in achados) if c]

    def _vale_rastrear(self, c):
        """Na varredura, o que vale guardar. Com a vitrine ligada, guarda tudo que pode ir pro canal: é o
        estoque dela e o histórico que depois mostra quando o preço caiu de verdade. Sem vitrine, só o que é
        do catálogo, tem desconto anunciado ou já é conhecido (senão o banco enche de perfume a preço cheio)."""
        if c.item or self.cfg.vitrine_minutos:
            return True
        a = c.achado
        if a.preco_de and a.preco_de >= a.preco * (1 + self.cfg.desconto_min_rastrear / 100):
            return True
        return db.anuncio_por_url(self.con, a.url) is not None

    def salvar(self, c):
        a = c.achado
        perfume = self._perfume_do_item(c.item) if c.item else self._perfume_generico(a)
        if not perfume:
            return None
        alvo = c.item.preco_bom_para(c.ml) if c.item else None
        return db.salvar_anuncio_descoberto(
            self.con, perfume["id"], a.loja, a.url, c.ml, alvo, c.chave, c.fonte.id, a.titulo, a.oficial, a.imagem,
        )

    def _leitura(self, c):
        a = c.achado
        return Leitura(a.preco, f"busca:{c.fonte.id}", not a.disponivel, a.preco_de, a.preco_pix)

    def _novidade(self, anuncio, leitura, agora):
        """Preço igual ao último lido há pouco: nada novo, não precisa registrar."""
        ultima = db.ultima_leitura(self.con, anuncio["id"])
        if not ultima:
            return True
        if ultima["preco"] != leitura.preco or bool(ultima["esgotado"]) != leitura.esgotado:
            return True
        return agora - db.de_texto(ultima["quando"]) >= RELER_MESMO_PRECO_APOS

    def _rebuscar(self, c):
        async def rebuscar():
            if not c.refazer:
                return Resultado(erro="sem como refazer")
            r = await self.cacador.na_vez(c.refazer)
            for a in r.achados:
                if a.url == c.achado.url:
                    return Resultado(leitura=Leitura(a.preco, f"busca:{c.fonte.id}", not a.disponivel,
                                                     a.preco_de, a.preco_pix))
            return Resultado(erro=r.erro or "sumiu da busca")
        return rebuscar

    async def _processar(self, candidatos, agora, avisar=True):
        """Salva, avalia e passa pelo motor de alertas. Maior desconto primeiro.
        Do mesmo produto e tamanho no lote, só o mais barato pode ser postado (os outros só são gravados)."""
        fila, vistos, menor_do_lote = [], set(), {}
        for c in candidatos:
            if c.achado.url in vistos:
                continue
            vistos.add(c.achado.url)
            anuncio = self.salvar(c)
            if not anuncio:
                continue  # anúncio cadastrado à mão com /add: a busca automática não mexe
            leitura = self._leitura(c)
            avaliacao, _ = self.cacador._avaliar(anuncio, leitura, agora)
            fila.append((avaliacao.pct, anuncio, leitura, c))
            grupo = (c.chave, c.ml)
            menor_do_lote[grupo] = min(menor_do_lote.get(grupo, c.achado.preco), c.achado.preco)
        fila.sort(key=lambda t: -t[0])
        linhas = []
        for _pct, anuncio, leitura, c in fila:
            pode_postar = avisar and c.achado.preco <= menor_do_lote[(c.chave, c.ml)]
            async with self.cacador.lock:
                anuncio = db.anuncio_por_id(self.con, anuncio["id"])  # estado fresco, dentro da trava
                if not anuncio:
                    continue
                chk = await self.cacador.processar_leitura(
                    anuncio, leitura, agora, avisar=pode_postar, rebaixar=self._rebuscar(c), filtro=self._filtro,
                    registrar=self._novidade(anuncio, leitura, agora),
                )
            linhas.append(chk.linha)
        return linhas

    # ------------------------------------------------------------
    # 1) VARREDURA
    # ------------------------------------------------------------

    def varredura_vencida(self, agora):
        if not self.cfg.varredura:
            return False
        if self._ultima_varredura is None:
            return True
        return agora - self._ultima_varredura >= timedelta(minutes=self.cfg.varredura_minutos)

    async def varredura(self, forcar=False):
        if self.trava_varredura.locked():
            return []
        async with self.trava_varredura:
            return await self._varredura(forcar)

    def _marcar_esgotados(self, achados, fonte, agora):
        """Anúncio conhecido que voltou sem estoque: grava 'esgotado' (sai da referência de mercado)."""
        for a in achados:
            if a.disponivel or not a.url:
                continue
            anuncio = db.anuncio_por_url(self.con, a.url)
            if anuncio and (anuncio.get("origem") or "manual") == "descoberta":
                ultima = db.ultima_leitura(self.con, anuncio["id"])
                if ultima and ultima["esgotado"]:
                    continue
                preco = a.preco or (ultima["preco"] if ultima else None)
                if preco:
                    db.registrar_preco(self.con, anuncio["id"], Leitura(preco, f"busca:{fonte.id}", True), agora)

    async def _varredura(self, forcar=False):
        agora = self.cacador.relogio()
        if not (forcar or self.varredura_vencida(agora)):
            return []
        self._ultima_varredura = agora
        candidatos, linhas = [], []
        for fonte in self.fontes:
            if self.pausada(fonte) or not hasattr(fonte, "tarefas_varredura"):
                continue
            for rotulo, tarefa in fonte.tarefas_varredura():
                if self.pausada(fonte):
                    break
                try:
                    res = await self.cacador.na_vez(tarefa)
                except Exception:
                    log.exception("erro na varredura %s", rotulo)
                    continue
                if res.pulado:  # não falou com a loja: não conta nem como sucesso nem como erro
                    continue
                ok, linha = await self._tratar_resultado(fonte, res, self.cacador.relogio())
                if not ok:
                    linhas.append(linha)
                    log.info(linha)
                    continue
                self._marcar_esgotados(res.achados, fonte, self.cacador.relogio())
                novos = 0
                # casar título com catálogo é pesado: roda fora do loop do Discord
                for c in await asyncio.to_thread(self.preparar_lote, res.achados, fonte):
                    if self._vale_rastrear(c):
                        c.refazer = _como_refazer(fonte, c.achado, tarefa)
                        candidatos.append(c)
                        novos += 1
                log.info("🧹 %s: %d produto(s), %d perfume(s) em oferta", rotulo, len(res.achados), novos)
        if candidatos:
            log.info("🧹 Varredura: avaliando %d oferta(s)", len(candidatos))
        linhas += await self._processar(candidatos, self.cacador.relogio())
        for linha in linhas:
            if linha.startswith(("🔥", "⏭️")):
                log.info(linha)
        return linhas

    # ------------------------------------------------------------
    # 2) CATÁLOGO
    # ------------------------------------------------------------

    def pendentes(self, agora):
        """Pares (item, fonte) vencidos, os mais atrasados primeiro."""
        intervalo = timedelta(hours=self.cfg.intervalo_horas)
        fila = []
        for fonte in self.fontes:
            if self.pausada(fonte, agora):
                continue
            for item in self.itens:
                if not fonte_busca_item(fonte, item) or not self.item_no_publico(item):
                    continue
                ultima = db.ultima_busca(self.con, item.id, fonte.id)
                if ultima is None or agora - ultima >= intervalo:
                    fila.append((ultima.timestamp() if ultima else 0, item.id, item, fonte))
        fila.sort(key=lambda t: (t[0], t[1]))
        return [(item, fonte) for _, _, item, fonte in fila]

    def selecionar(self, item, achados, fonte=None):
        """Achados que são mesmo esse item. Devolve [Candidato] (no máximo max_por_busca)."""
        escolhidos, vistos = [], set()
        for c in self.preparar_lote(achados, fonte, so_item=item):
            a = c.achado
            if a.url in vistos:
                continue
            vistos.add(a.url)
            escolhidos.append(c)
            if len(escolhidos) >= self.cfg.max_por_busca:
                break
        return escolhidos

    async def buscar_item(self, item, fonte):
        """Busca um item do catálogo numa fonte e processa o que achar. Devolve linhas de log."""
        termo = item.termos_busca[0]
        res = await self.cacador.na_vez(fonte.buscar, termo)
        agora = self.cacador.relogio()
        ok, linha = await self._tratar_resultado(fonte, res, agora)
        if not ok:
            db.registrar_busca(self.con, item.id, fonte.id, agora, erro=res.erro)
            return [linha.replace(f"{fonte.nome}:", f"{item.nome_exibicao} @ {fonte.nome}:", 1)]
        escolhidos = self.selecionar(item, res.achados, fonte)
        db.registrar_busca(self.con, item.id, fonte.id, agora, achados=len(escolhidos))
        if not escolhidos:
            return [f"· {item.nome_exibicao} @ {fonte.nome}: nada ({len(res.achados)} resultados)"]
        for c in escolhidos:
            c.refazer = _como_refazer(fonte, c.achado, lambda f=fonte, t=termo: f.buscar(t))
        return await self._processar(escolhidos, agora)

    async def rodada(self, max_buscas=None, max_segundos=None):
        """Buscas do catálogo vencidas (com limite de tempo, pra varredura não esperar demais)."""
        if not self.cfg.ativo or not self.fontes or not self.itens:
            return []
        pendentes = self.pendentes(self.cacador.relogio())
        if max_buscas:
            pendentes = pendentes[:max_buscas]
        if pendentes:
            log.info("🔎 Catálogo: %d busca(s) pendente(s)", len(pendentes))
        inicio = time.monotonic()
        linhas = []
        for item, fonte in pendentes:
            if max_segundos and time.monotonic() - inicio > max_segundos:
                break
            if self.pausada(fonte):
                continue
            try:
                novas = await self.buscar_item(item, fonte)
            except Exception:
                log.exception("erro buscando %s em %s", item.id, fonte.id)
                continue
            for linha in novas:
                log.info(linha)
            linhas += novas
        return linhas

    # ------------------------------------------------------------
    # 3) AO VIVO (/locate de qualquer perfume)
    # ------------------------------------------------------------

    def _casa_consulta(self, achado, palavras, marca_norm):
        texto = busca.normalizar(busca.separar_numeros(f"{achado.titulo} {achado.marca or ''}"))
        tokens = texto.split()
        if catalogo.e_lixo(achado.titulo):
            return False  # kit, decant, amostra...
        if not catalogo.e_perfume(achado.titulo):
            # anúncio comum do site às vezes não diz "perfume" ("Queening Mind Games 100ml"): vale se tiver
            # tamanho de frasco e não for bebida/utensílio (o Funko "God of Fire" não tem ml e fica de fora)
            ml = catalogo.extrair_ml(achado.titulo)
            if not ml or ml > 200 or _NAO_E_PERFUME.search(busca.normalizar(achado.titulo)):
                return False
        if marca_norm and not catalogo._marca_presente(marca_norm, tokens, achado.marca):
            return False
        return all(catalogo._tem_palavra(p, tokens) for p in palavras)

    async def buscar_ao_vivo(self, nome, marca=None, na_fila=False):
        """Pesquisa nas lojas agora. Uma requisição por loja (em paralelo: nenhuma loja leva mais de uma).
        Não grava nada. na_fila=True (alerta pessoal, em segundo plano): espera a vez como a varredura,
        uma loja por vez, e respeita qualquer pausa."""
        consulta = " ".join(x for x in (nome, marca) if x).strip()
        resultado = ResultadoAoVivo(consulta)
        resultado.links = [(f.nome, f.link_busca(consulta)) for f in self.fontes if hasattr(f, "link_busca")]
        fontes = [f for f in self.fontes if not (self.pausada(f) if na_fila else self.pausada_pro_locate(f))]
        if not consulta or not fontes:
            return resultado
        if na_fila:
            respostas = []
            for f in fontes:
                try:
                    respostas.append(await self.cacador.na_vez(getattr(f, "buscar_ao_vivo", f.buscar), consulta))
                except Exception as e:  # vira "loja falhou", igual no /locate
                    respostas.append(e)
        else:
            respostas = await asyncio.gather(
                *(asyncio.wait_for(asyncio.to_thread(getattr(f, "buscar_ao_vivo", f.buscar), consulta),
                                   TEMPO_MAX_AO_VIVO_S) for f in fontes),
                return_exceptions=True,
            )
            self.cacador._ultimo_request = time.monotonic()
        agora = self.cacador.relogio()

        conc = busca.detectar_concentracao(consulta)
        # "torino21" e "Torino 21" são o mesmo: separa letra de número dos dois lados
        palavras = [p for p in busca.normalizar(busca.separar_numeros(nome)).split()
                    if p not in catalogo._palavras_conc()]
        marca_norm = busca.normalizar(marca) if marca else None
        ofertas = []
        for fonte, res in zip(fontes, respostas):
            if isinstance(res, Exception):
                if isinstance(res, TimeoutError):
                    log.info("/locate: %s não respondeu em %d s", fonte.nome, TEMPO_MAX_AO_VIVO_S)
                else:
                    log.warning("/locate: erro em %s: %r", fonte.nome, res)
                resultado.lojas_falha.append(fonte.nome)
                continue
            ok, _ = await self._tratar_resultado(fonte, res, agora)
            if not ok and not res.achados:
                resultado.lojas_falha.append(fonte.nome)
                resultado.detalhes.append((fonte.nome, res.detalhe or (res.erro or "erro").removeprefix("api: ")))
                continue
            # bloqueou mas o site achou algo: a pausa já foi aplicada, e o que veio ainda aparece
            resultado.lojas_ok.append(fonte.nome)
            antes = len(ofertas)
            for a in res.achados:
                if not a.disponivel or not self._casa_consulta(a, palavras, marca_norm):
                    continue
                if conc and busca.detectar_concentracao(a.titulo) not in (None, conc):
                    continue
                ml = a.ml or catalogo.extrair_ml(a.titulo, a.variacao or "")
                pct = (a.preco_de - a.preco) / a.preco_de * 100 if a.preco_de and a.preco_de > a.preco else 0
                ofertas.append((a, ml, pct))
            if len(ofertas) == antes:  # essa loja não trouxe nada que sirva: guarda o porquê
                outros = sum(1 for a in res.achados if a.disponivel)
                motivo = res.detalhe or ("nenhum resultado" if not res.achados else "")
                if outros:
                    motivo += ("; " if motivo else "") + f"{outros} anúncio(s) com outro nome/tamanho"
                resultado.detalhes.append((fonte.nome, motivo))
        log.info("/locate %r: %d oferta(s) de %s; casaram %d", consulta,
                 sum(len(r.achados) for r in respostas if isinstance(r, ResultadoBusca)),
                 ", ".join(resultado.lojas_ok) or "nenhuma loja", len(ofertas))
        vistos, unicas = set(), []
        for o in sorted(ofertas, key=lambda o: o[0].preco):
            if o[0].url not in vistos:
                vistos.add(o[0].url)
                unicas.append(o)
        resultado.ofertas = unicas  # o /locate só mostra: não grava nada (pesquisa sempre do zero)
        if unicas:
            resultado.preco_bom = self.preco_bom_do_achado(unicas[0][0], unicas[0][1])
        return resultado

    def preco_bom_do_achado(self, achado, ml):
        """Preço bom de algo achado ao vivo (/locate, /alerta), com o que o bot já sabe, sem pesquisar:
        o histórico desse anúncio (se a varredura já viu ele) ou o "preço bom" do catálogo."""
        anuncio = db.anuncio_por_url(self.con, achado.url)
        if anuncio and (anuncio.get("origem") or "manual") == "descoberta" and (not ml or anuncio["ml"] == ml):
            oferta = self.cacador.oferta_atual(anuncio)
            if oferta and oferta.preco_bom:
                return oferta.preco_bom
        item = catalogo.identificar(achado.titulo, self.itens, achado.marca) if ml else None
        if item and (not item.ml or ml in item.ml):
            return item.preco_bom_para(ml)
        return None

    # ------------------------------------------------------------
    # 🔔 ALERTA PESSOAL (/alerta): "me marca quando o Sauvage ficar abaixo de R$ 500"
    # ------------------------------------------------------------

    @staticmethod
    def melhor_do_alerta(res, ml=None):
        """A oferta mais barata do /locate que serve pro alerta (do tamanho pedido, se pediu)."""
        for achado, ml_oferta, pct in res.ofertas:
            if ml and not (ml_oferta and abs(ml_oferta - ml) < 0.5):
                continue
            return achado, ml_oferta, pct
        return None

    async def avaliar_alerta(self, alerta, melhor, enviar, agora):
        """Decide se avisa. Avisa quando chega no preço; de novo só se baixar mais. Se o preço ficar acima
        do pedido em 2 pesquisas seguidas (~6 h), a próxima queda avisa outra vez: uma pesquisa só acima
        pode ser o anúncio barato que não veio naquela resposta do ML. Devolve True se avisou."""
        if not self._mesmo_alerta(alerta):
            return False  # removido (ou o número foi reaproveitado por um alerta novo) durante a pesquisa
        avisado, acima = alerta["avisado_preco"], alerta.get("acima_seguidas") or 0
        avisou = False
        if melhor and melhor[0].preco <= alerta["preco_max"]:
            acima = 0
            if avisado is None or melhor[0].preco < avisado - 0.01:
                if await enviar(alerta, *melhor):
                    avisado, avisou = melhor[0].preco, True
        elif melhor:
            acima += 1
            if acima >= ALERTA_REARMA_APOS:
                avisado = None
        db.marcar_alerta_usuario(self.con, alerta["id"], agora, avisado, acima, criado_em=alerta["criado_em"])
        return avisou

    def _mesmo_alerta(self, alerta):
        """O alerta ainda existe e é o mesmo? (o SQLite reaproveita o número de um alerta apagado: remover e
        criar de novo enquanto a pesquisa roda faria o aviso sair com o preço velho)"""
        atual = db.alerta_usuario(self.con, alerta["id"])
        campos = ("usuario_id", "consulta", "preco_max", "ml", "criado_em")
        return bool(atual) and all(atual[k] == alerta[k] for k in campos)

    async def checar_alertas_usuarios(self, enviar, max_buscas=20):
        """Pesquisa os alertas mais atrasados (cada um no máximo a cada INTERVALO_ALERTA_USUARIO).
        enviar(alerta, achado, ml, pct) -> bool. Devolve quantos avisou."""
        agora = self.cacador.relogio()
        avisos = 0
        for alerta in db.alertas_usuario_vencidos(self.con, agora - INTERVALO_ALERTA_USUARIO, max_buscas):
            if all(self.pausada(f) for f in self.fontes):
                break
            res = await self.buscar_ao_vivo(alerta["consulta"], na_fila=True)
            if not res.lojas_ok:
                continue  # loja fora do ar: tenta de novo na próxima rodada
            if await self.avaliar_alerta(alerta, self.melhor_do_alerta(res, alerta["ml"]), enviar,
                                         self.cacador.relogio()):
                avisos += 1
        return avisos

    # ------------------------------------------------------------
    # 🏆 Top do dia
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # 4) VITRINE: um post a cada X min, mesmo sem promo nova
    # ------------------------------------------------------------

    def ranking_vitrine(self, agora=None):
        """Ofertas conhecidas, da melhor pra pior: (novas, repetidas). Ordem: promo (nível) > maior desconto >
        perfume do catálogo > menor R$/ml. Repetida = foi pro canal nas últimas `vitrine_sem_repetir_horas`
        (ela ou o mesmo perfume); essas vêm da que saiu há mais tempo pra mais recente.
        Cada item: (oferta, chave do perfume, último post ou None)."""
        agora = agora or self.cacador.relogio()
        postados = db.ultimos_posts_desde(self.con, agora - timedelta(hours=self.cfg.vitrine_sem_repetir_horas))
        ultimo_do_anuncio = {p["anuncio_id"]: db.de_texto(p["quando"]) for p in postados}
        ultimo_do_perfume = {}
        for p in postados:
            chave = p["catalogo_id"] or p["perfume_id"]
            quando = db.de_texto(p["quando"])
            ultimo_do_perfume[chave] = max(quando, ultimo_do_perfume.get(chave, quando))
        novas, repetidas = [], []
        for anuncio in db.anuncios_descobertos_recentes(self.con, agora - timedelta(hours=24)):
            item = self.cacador.catalogo.get(anuncio.get("catalogo_id"))
            if not self.no_publico(None, item, titulo=anuncio.get("titulo") or anuncio.get("perfume_nome") or ""):
                continue
            oferta = self.cacador.oferta_atual(anuncio)
            if not oferta or oferta.esgotado or not oferta.preco:
                continue
            av = oferta.avaliacao
            pct = av.pct if av.referencia and av.pct else 0
            nota = (0 if av.nivel else 1, -pct, 0 if item else 1, oferta.por_ml or oferta.preco)
            chave = anuncio.get("catalogo_id") or anuncio["perfume_id"]
            ultimo = ultimo_do_anuncio.get(anuncio["id"]) or ultimo_do_perfume.get(chave)
            if ultimo:
                repetidas.append(((ultimo, nota), (oferta, chave, ultimo)))
            else:
                novas.append((nota, (oferta, chave, None)))
        return ([x for _, x in sorted(novas, key=lambda t: t[0])],
                [x for _, x in sorted(repetidas, key=lambda t: t[0])])

    def escolher_vitrine(self, agora=None):
        """A melhor oferta que ainda não foi pro canal (nem o mesmo perfume). Se tudo já foi, repete a que
        saiu há mais tempo, desde que faz pelo menos 12 h."""
        agora = agora or self.cacador.relogio()
        novas, repetidas = self.ranking_vitrine(agora)
        if novas:
            return novas[0][0]
        for oferta, _, ultimo in repetidas:
            if agora - ultimo >= timedelta(hours=12):
                return oferta
        return None

    async def mandar_varias(self, quantidade, enviar, pausa=None, filtro=None):
        """/send N: posta até N ofertas agora (novas primeiro; acabando, as que já saíram, da mais antiga pra
        mais nova). Nunca o mesmo perfume duas vezes no mesmo lote. filtro(oferta) -> bool escolhe quais
        (estilo, preço, desconto). Devolve quantas foram."""
        agora = self.cacador.relogio()
        novas, repetidas = self.ranking_vitrine(agora)
        enviadas, perfumes = 0, set()
        for oferta, chave, _ in novas + repetidas:
            if enviadas >= quantidade:
                break
            if chave in perfumes or (filtro and not filtro(oferta)):
                continue
            if enviadas:
                await (pausa or asyncio.sleep)(1.5)  # o Discord limita ~5 mensagens a cada 5 s por canal
            if not await enviar(oferta):
                break
            perfumes.add(chave)
            db.registrar_alerta(self.con, oferta.anuncio_id, "vitrine", None, oferta.preco, self.cacador.relogio(),
                                pct_do_post(oferta))
            enviadas += 1
        log.info("🛍️ /send: %d de %d oferta(s) (%d novas, %d já postadas conhecidas)",
                 enviadas, quantidade, len(novas), len(repetidas))
        return enviadas

    @staticmethod
    def filtro_oferta(estilo=None, preco_max=None, desconto_min=None):
        """Filtro do /send. estilo: 'designer', 'nicho', 'arabe', 'nacional' ou 'outros' (marca que o bot
        não conhece). preco_max em R$. desconto_min: pelo menos X% OFF. Tudo vazio: qualquer oferta."""
        def passa(oferta):
            if estilo and (oferta.estilo or catalogo.OUTROS) != estilo:
                return False
            if preco_max and oferta.preco > preco_max:
                return False
            if desconto_min:
                av = oferta.avaliacao
                if not (av.referencia and pct_inteiro(av.pct) >= desconto_min):
                    return False
            return True
        return passa

    async def postar_vitrine(self, enviar):
        """Posta a oferta da vitrine (enviar(oferta) -> bool). Pula se já saiu post no canal agora há pouco."""
        if not self.cfg.vitrine_minutos:
            return None
        agora = self.cacador.relogio()
        if db.alertas_desde(self.con, agora - timedelta(minutes=self.cfg.vitrine_minutos * 0.9)):
            return None  # acabou de sair promo: a vitrine espera a próxima vez
        oferta = self.escolher_vitrine(agora)
        if not oferta:
            log.info("🛍️ Vitrine: nada pra mostrar agora (%d oferta(s) conhecida(s) nas últimas 24 h)",
                     len(db.anuncios_descobertos_recentes(self.con, agora - timedelta(hours=24))))
            return None
        if not await enviar(oferta):
            return None
        # nível fica vazio: post da vitrine não conta no limite diário de @everyone
        db.registrar_alerta(self.con, oferta.anuncio_id, "vitrine", None, oferta.preco, agora, pct_do_post(oferta))
        log.info("🛍️ Vitrine: %s %s", oferta.titulo, formatar_reais(oferta.preco))
        return oferta

    def resumo_vitrine(self, agora=None):
        agora = agora or self.cacador.relogio()
        return {
            "minutos": self.cfg.vitrine_minutos,
            "conhecidas": len(db.anuncios_descobertos_recentes(self.con, agora - timedelta(hours=24))),
            "ultimo_post": db.ultimo_post(self.con),
        }

    def top_semana(self, n=None, agora=None):
        """🏆 Top da semana: os maiores % OFF que foram pro canal nos últimos 7 dias, um por perfume.
        Cada item: {"anuncio", "pct", "preco", "quando", "agora": Oferta atual ou None}."""
        n = n or self.cfg.top_n
        agora = agora or self.cacador.relogio()
        itens, perfumes = [], set()
        for post in db.posts_com_desconto_desde(self.con, agora - timedelta(days=7), pct_min=5):
            chave = post["catalogo_id"] or post["perfume_id"]
            if chave in perfumes:
                continue
            anuncio = db.anuncio_por_id(self.con, post["anuncio_id"])
            if not anuncio:
                continue
            perfumes.add(chave)
            itens.append({"anuncio": anuncio, "pct": post["pct"], "preco": post["preco"],
                          "quando": db.de_texto(post["quando"]), "agora": self.cacador.oferta_atual(anuncio)})
            if len(itens) >= n:
                break
        return itens

    def top(self, n=None, horas=24):
        """Melhores ofertas atuais (uma por perfume), maior desconto primeiro."""
        n = n or self.cfg.top_n
        agora = self.cacador.relogio()
        melhores = {}
        for anuncio in db.anuncios_descobertos_recentes(self.con, agora - timedelta(hours=horas)):
            item = self.cacador.catalogo.get(anuncio.get("catalogo_id"))
            if not self.no_publico(None, item, titulo=anuncio.get("titulo") or anuncio.get("perfume_nome") or ""):
                continue
            oferta = self.cacador.oferta_atual(anuncio)
            if not oferta or oferta.esgotado or not oferta.avaliacao.nivel:
                continue
            chave = anuncio.get("catalogo_id") or anuncio["perfume_id"]
            atual = melhores.get(chave)
            if atual is None or promo.chave_ranking(oferta.avaliacao.pct, oferta.preco) < promo.chave_ranking(
                atual.avaliacao.pct, atual.preco
            ):
                melhores[chave] = oferta
        return sorted(melhores.values(), key=lambda o: promo.chave_ranking(o.avaliacao.pct, o.preco))[:n]
