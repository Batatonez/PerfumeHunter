"""/atualizar pelo Discord: git pull + testes, sem nunca deixar código quebrado rodando."""

from scentinel.atualizar import atualizar


class Shell:
    """Finge o git/pip/pytest: cada comando devolve o que foi programado."""

    def __init__(self, antes="aaa", depois="bbb", testes_ok=True, mudou=(), pull_ok=True):
        self.heads = [antes, depois]
        self.testes_ok, self.mudou, self.pull_ok = testes_ok, set(mudou), pull_ok
        self.comandos = []

    def __call__(self, cmd, pasta, tempo_s=180):
        self.comandos.append(cmd)
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
        if cmd[:2] == ["git", "pull"]:
            return (0, "") if self.pull_ok else (1, "fatal: Not possible to fast-forward")
        if cmd[:2] == ["git", "log"]:
            return 0, "bbb Top da semana\nccc % OFF"
        if cmd[:2] == ["git", "diff"]:
            return (1 if cmd[-1] in self.mudou else 0), ""
        if "pytest" in cmd:
            return (0, "300 passed in 12s") if self.testes_ok else (1, "1 failed, 299 passed")
        return 0, ""


def test_nada_novo():
    res = atualizar(rodar=Shell(antes="aaa", depois="aaa"))
    assert res.ok and not res.reiniciar and "versão mais nova" in res.texto


def test_atualiza_testa_e_reinicia():
    sh = Shell(mudou={"requirements.txt"})
    res = atualizar(rodar=sh)
    assert res.ok and res.reiniciar
    assert "• Top da semana" in res.texto and "300 passed" in res.texto and "Dependências novas" in res.texto
    assert any("pip" in c for c in sh.comandos)


def test_testes_falharam_volta_o_codigo():
    sh = Shell(testes_ok=False)
    res = atualizar(rodar=sh)
    assert not res.ok and not res.reiniciar and "Voltei pro código de antes" in res.texto
    assert ["git", "reset", "-q", "--keep", "aaa"] in sh.comandos


def test_servico_mudou_avisa_pra_rodar_no_notebook():
    res = atualizar(rodar=Shell(mudou={"scentinel.service"}))
    assert res.reiniciar and "scentinel-update --reiniciar" in res.texto


def test_pull_falhou():
    res = atualizar(rodar=Shell(pull_ok=False))
    assert not res.ok and "git pull não deu" in res.texto


def test_comando_so_do_dono_e_reinicia():
    import asyncio
    from types import SimpleNamespace

    from scentinel import atualizar as mod
    from scentinel import config, db
    from scentinel.atualizar import ResultadoAtualizacao
    from scentinel.bot import BotScentinel, registrar_comandos

    bot = BotScentinel(config.carregar_config(), config.Segredos("x", 1, 2, 42), db.conectar(":memory:"))
    registrar_comandos(bot.tree)
    respostas, edicoes, fechou = [], [], []

    async def responder(texto, **kw):
        respostas.append(texto)

    async def defer(**kw):
        pass

    async def editar(**kw):
        edicoes.append(kw["content"])

    async def fechar():
        fechou.append(True)

    bot.close = fechar
    original = mod.atualizar
    mod.atualizar = lambda: ResultadoAtualizacao(True, reiniciar=True, linhas=["📦 Novidades:"])
    try:
        def interacao(uid):
            return SimpleNamespace(client=bot, user=SimpleNamespace(id=uid), edit_original_response=editar,
                                   response=SimpleNamespace(send_message=responder, defer=defer))

        cmd = bot.tree.get_command("atualizar")
        asyncio.run(cmd.callback(interacao(7)))
        assert "Só o dono" in respostas[-1] and not fechou
        asyncio.run(cmd.callback(interacao(42)))
        assert "Reiniciando" in edicoes[-1] and fechou and bot.reiniciar
    finally:
        mod.atualizar = original
