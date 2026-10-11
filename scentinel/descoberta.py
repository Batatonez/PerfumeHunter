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
import time
from dataclasses import dataclass, field
from datetime import timedelta

from scentinel import busca, catalogo, db, promo
from scentinel.coleta import Leitura, Resultado
from scentinel.fontes import fonte_busca_item
from scentinel.formato import formatar_reais

log = logging.getLogger("scentinel")

ERROS_PRA_PAUSAR = 3  # erros seguidos numa loja -> pausa 24 h (ela provavelmente não tem essa busca)
RELER_MESMO_PRECO_APOS = timedelta(hours=6)  # preço igual: só registra de novo depois disso


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
        self._ultima_varredura = None
        cacador.catalogo = {i.id: i for i in self.itens}

    # ------------------------------------------------------------
    # lojas pausadas, bloqueio e erro
    # ------------------------------------------------------------

    def pausada(self, fonte, agora=None):
        pausa = self._fonte_pausada_ate.get(fonte.id)
        return bool(pausa and pausa > (agora or self.cacador.relogio()))

    async def _tratar_resultado(self, fonte, res, agora):
        """Cuida de bloqueio/erro. Devolve (ok, linha_de_log)."""
        if res.bloqueado:
            falhas = self._fonte_falhas.get(fonte.id, 0) + 1
            self._fonte_falhas[fonte.id] = falhas
            horas = min(self.cacador.cfg.backoff_horas * 2 ** (falhas - 1), self.cacador.cfg.backoff_max_horas)
            self._fonte_pausada_ate[fonte.id] = agora + timedelta(hours=horas)
            if falhas == 1 and self.enviar_aviso:
                await self.enviar_aviso(
                    f"🚫 **{fonte.nome}** bloqueou a busca ({res.erro}). Pausei essa loja por {horas:g} h, sem forçar."
                )
            return False, f"🚫 {fonte.nome}: {res.erro} (pausada {horas:g} h)"
        self._fonte_falhas.pop(fonte.id, None)
        if res.erro:
            erros = self._fonte_erros.get(fonte.id, 0) + 1
            self._fonte_erros[fonte.id] = erros
            if erros >= ERROS_PRA_PAUSAR:
                self._fonte_pausada_ate[fonte.id] = agora + timedelta(hours=24)
                self._fonte_erros[fonte.id] = 0
                return False, f"⏸️ {fonte.nome}: {erros} erros seguidos ({res.erro}), pausada por 24 h"
            return False, f"❌ {fonte.nome}: {res.erro}"
        self._fonte_erros.pop(fonte.id, None)
        return True, None

    # ------------------------------------------------------------
    # filtro: só posta o melhor e sem enxurrada
    # ------------------------------------------------------------

    async def _filtro(self, anuncio, oferta):
        agora = self.cacador.relogio()
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

    def preparar(self, achado, fonte, so_item=None):
        """Achado -> Candidato (ou None se não é perfume, é lixo, não tem tamanho...).
        so_item: busca do catálogo, só aceita esse item."""
        if not achado.url or not achado.preco or not achado.disponivel:
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
        ml = achado.ml or catalogo.extrair_ml(achado.titulo, achado.variacao or "")
        if not ml or ml < self.cfg.ml_min or achado.preco < self.cfg.preco_min:
            return None
        if item and item.ml and ml not in item.ml:
            if so_item is not None:
                return None
            item = None  # tamanho fora do catálogo: trata como perfume qualquer
        chave = item.id if item else (achado.chave or catalogo.chave_generica(achado.titulo, ml, achado.marca))
        if not chave:
            return None
        return Candidato(achado, ml, item, chave, fonte)

    def _vale_rastrear(self, c):
        """Na varredura, só guarda o que é do catálogo, tem desconto anunciado ou já é conhecido
        (senão o banco enche de perfume a preço cheio)."""
        if c.item:
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
        """Salva, avalia e passa pelo motor de alertas. Maior desconto primeiro."""
        fila, vistos = [], set()
        for c in candidatos:
            if c.achado.url in vistos:
                continue
            vistos.add(c.achado.url)
            anuncio = self.salvar(c)
            if not anuncio:
                continue
            leitura = self._leitura(c)
            if not self._novidade(anuncio, leitura, agora):
                continue
            avaliacao, _ = self.cacador._avaliar(anuncio, leitura, agora)
            fila.append((avaliacao.pct, anuncio, leitura, c))
        fila.sort(key=lambda t: -t[0])
        linhas = []
        for _pct, anuncio, leitura, c in fila:
            async with self.cacador.lock:
                chk = await self.cacador.processar_leitura(
                    anuncio, leitura, agora, avisar=avisar, rebaixar=self._rebuscar(c), filtro=self._filtro,
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
                ok, linha = await self._tratar_resultado(fonte, res, self.cacador.relogio())
                if not ok:
                    linhas.append(linha)
                    log.info(linha)
                    continue
                novos = 0
                for a in res.achados:
                    c = self.preparar(a, fonte)
                    if c and self._vale_rastrear(c):
                        c.refazer = tarefa
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
                if not fonte_busca_item(fonte, item):
                    continue
                ultima = db.ultima_busca(self.con, item.id, fonte.id)
                if ultima is None or agora - ultima >= intervalo:
                    fila.append((ultima.timestamp() if ultima else 0, item.id, item, fonte))
        fila.sort(key=lambda t: (t[0], t[1]))
        return [(item, fonte) for _, _, item, fonte in fila]

    def selecionar(self, item, achados, fonte=None):
        """Achados que são mesmo esse item. Devolve [Candidato] (no máximo max_por_busca)."""
        escolhidos, vistos = [], set()
        for a in achados:
            if a.url in vistos:
                continue
            c = self.preparar(a, fonte, so_item=item)
            if not c:
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
            c.refazer = lambda f=fonte, t=termo: f.buscar(t)
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
        texto = busca.normalizar(f"{achado.titulo} {achado.marca or ''}")
        tokens = texto.split()
        if catalogo.e_lixo(achado.titulo):
            return False
        if marca_norm and not catalogo._marca_presente(marca_norm, tokens, achado.marca):
            return False
        return all(catalogo._tem_palavra(p, tokens) for p in palavras)

    async def buscar_ao_vivo(self, nome, marca=None):
        """Pesquisa nas lojas agora. Uma requisição por loja (em paralelo: nenhuma loja leva mais de uma).
        Também guarda o que achar (histórico e /top), sem postar no canal."""
        consulta = " ".join(x for x in (nome, marca) if x).strip()
        resultado = ResultadoAoVivo(consulta)
        fontes = [f for f in self.fontes if not self.pausada(f)]
        if not consulta or not fontes:
            return resultado
        respostas = await asyncio.gather(
            *(asyncio.to_thread(f.buscar, consulta) for f in fontes), return_exceptions=True
        )
        self.cacador._ultimo_request = time.monotonic()
        agora = self.cacador.relogio()

        conc = busca.detectar_concentracao(consulta)
        palavras = [p for p in busca.normalizar(nome).split() if p not in catalogo._palavras_conc()]
        marca_norm = busca.normalizar(marca) if marca else None
        candidatos, ofertas = [], []
        for fonte, res in zip(fontes, respostas):
            if isinstance(res, Exception):
                resultado.lojas_falha.append(fonte.nome)
                continue
            ok, _ = await self._tratar_resultado(fonte, res, agora)
            if not ok:
                resultado.lojas_falha.append(fonte.nome)
                continue
            resultado.lojas_ok.append(fonte.nome)
            for a in res.achados:
                if not a.disponivel or not self._casa_consulta(a, palavras, marca_norm):
                    continue
                if conc and busca.detectar_concentracao(a.titulo) not in (None, conc):
                    continue
                ml = a.ml or catalogo.extrair_ml(a.titulo, a.variacao or "")
                pct = (a.preco_de - a.preco) / a.preco_de * 100 if a.preco_de and a.preco_de > a.preco else 0
                ofertas.append((a, ml, pct))
                c = self.preparar(a, fonte)
                if c:
                    c.refazer = lambda f=fonte, q=consulta: f.buscar(q)
                    candidatos.append(c)
        vistos, unicas = set(), []
        for o in sorted(ofertas, key=lambda o: o[0].preco):
            if o[0].url not in vistos:
                vistos.add(o[0].url)
                unicas.append(o)
        resultado.ofertas = unicas
        if candidatos:
            await self._processar(candidatos, agora, avisar=False)
        return resultado

    # ------------------------------------------------------------
    # 🏆 Top do dia
    # ------------------------------------------------------------

    def top(self, n=None, horas=24):
        """Melhores ofertas atuais (uma por perfume), maior desconto primeiro."""
        n = n or self.cfg.top_n
        agora = self.cacador.relogio()
        melhores = {}
        for anuncio in db.anuncios_descobertos_recentes(self.con, agora - timedelta(hours=horas)):
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
