import json
from pathlib import Path

import pytest
from mcp import Client

from ponte_llm import gemini

MODELOS = [
    "gemini-2.5-flash",
    "gemini-4-carbon-low",
    "gemini-4-carbon-medium",
    "gemini-4-carbon-high",
    "gemini-4-hydrogen-medium",
    "gemini-4-hydrogen-xhigh",
]


def envelope(resposta="Resposta do Gemini", status="SUCCESS") -> str:
    return "log qualquer\n" + json.dumps(
        {"status": status, "response": resposta, "duration_seconds": 3.2, "usage": {"total_tokens": 42}}
    )


class AgyFalso:
    """Substitui a execução do agy e guarda os argumentos de cada chamada."""

    def __init__(self, saida: str = "", erro: str = "", codigo: int = 0):
        self.saida, self.erro, self.codigo = saida or envelope(), erro, codigo
        self.chamadas: list[list[str]] = []
        self.pastas: list[str | None] = []
        self.pedido_em_arquivo: str | None = None

    async def __call__(self, args, cwd=None, tempo=0):
        self.chamadas.append(args)
        self.pastas.append(cwd)
        if args[1:] == ["models"]:
            return 0, "\n".join(MODELOS), ""
        if "--add-dir" in args:  # o arquivo do pedido longo precisa existir durante a chamada
            pasta = Path(args[args.index("--add-dir") + 1])
            arquivo = pasta / "pedido.md"
            if arquivo.exists():
                self.pedido_em_arquivo = arquivo.read_text(encoding="utf-8")
        return self.codigo, self.saida, self.erro


@pytest.fixture
def agy(monkeypatch, tmp_path):
    def montar(**kwargs) -> AgyFalso:
        falso = AgyFalso(**kwargs)
        monkeypatch.setattr(gemini, "_agy", lambda: "agy")
        monkeypatch.setattr(gemini, "_executar", falso)
        monkeypatch.setattr(gemini, "_cache_modelos", None)
        monkeypatch.setattr(gemini, "PASTA_TEMP", str(tmp_path))
        return falso

    return montar


@pytest.mark.parametrize(
    ("modelo", "esforco", "esperado"),
    [
        ("carbon", "high", "gemini-4-carbon-high"),
        ("carbon", "xhigh", "gemini-4-carbon-high"),  # não existe xhigh: cai para o mais alto abaixo
        ("hydrogen", "low", "gemini-4-hydrogen-medium"),  # nada abaixo: usa o mais baixo que existe
        ("hydrogen", "", "gemini-4-hydrogen-medium"),
        ("gemini-4-carbon-low", "high", "gemini-4-carbon-low"),  # slug exato vence o esforço
        ("desconhecido", "high", "desconhecido"),
        ("carbon", "ultra", "gemini-4-carbon-medium"),  # esforço inválido conta como medium
        ("flash", "high", "gemini-2.5-flash"),  # modelo sem variantes de esforço
    ],
)
def test_escolher_slug(modelo, esforco, esperado):
    assert gemini.escolher_slug(modelo, esforco, MODELOS) == esperado


def test_escolher_slug_sem_lista_devolve_o_pedido():
    assert gemini.escolher_slug("carbon", "high", []) == "carbon"


def test_extrair_modelos_ignora_texto_e_repetidos():
    saida = "Modelos disponíveis:\n  gemini-4-carbon-high (padrão)\n  gemini-4-carbon-high\n  outra-coisa\n"
    assert gemini.extrair_modelos(saida) == ["gemini-4-carbon-high"]


def test_interpretar_sucesso_tem_rodape():
    resposta, _ = gemini.interpretar_resposta(0, envelope("Oi"), "", "slug-x")
    assert resposta.startswith("Oi\n\n---\n[Gemini · modelo slug-x · 3.2s · 42 tokens]")


def test_interpretar_resposta_boa_que_cita_login_nao_e_descartada():
    resposta, _ = gemini.interpretar_resposta(0, envelope("Erro 'authentication required' no log"), "", "s")
    assert resposta is not None and resposta.startswith("Erro 'authentication required'")


def test_interpretar_json_em_varias_linhas():
    saida = json.dumps({"status": "SUCCESS", "response": "Oi"}, indent=2)
    resposta, _ = gemini.interpretar_resposta(0, saida, "", "s")
    assert resposta is not None and resposta.startswith("Oi")


def test_interpretar_sucesso_vazio_e_falha():
    resposta, detalhe = gemini.interpretar_resposta(0, envelope(""), "", "s")
    assert resposta is None and "status SUCCESS" in detalhe


def test_interpretar_sem_login():
    resposta, detalhe = gemini.interpretar_resposta(1, "", "Error: Authentication required", "s")
    assert resposta is None and detalhe == gemini.SEM_LOGIN


def test_interpretar_saida_quebrada():
    resposta, detalhe = gemini.interpretar_resposta(2, "não é json", "explodiu", "s")
    assert resposta is None and "código 2" in detalhe and "explodiu" in detalhe


async def test_perguntar_monta_comando_seguro(agy):
    falso = agy()
    async with Client(gemini.servidor) as cliente:
        resultado = await cliente.call_tool(
            "gemini_perguntar", {"pergunta": "vale a pena?", "modelo": "carbon", "esforco": "high"}
        )
    assert resultado.content[0].text.startswith("Resposta do Gemini")
    args = falso.chamadas[-1]
    assert args[args.index("--model") + 1] == "gemini-4-carbon-high"
    assert args[args.index("--mode") + 1] == "plan"
    assert "--sandbox" in args
    assert "--dangerously-skip-permissions" not in args
    assert args[2].startswith(gemini.ENQUADRAMENTO)


async def test_pedido_longo_vai_por_arquivo_e_e_apagado(agy):
    falso = agy()
    pedido = "x" * (gemini.LIMITE_PROMPT_DIRETO + 1)
    await gemini.gemini_perguntar(pedido)
    args = falso.chamadas[-1]
    assert len(args[2]) < gemini.LIMITE_PROMPT_DIRETO  # o texto não foi na linha de comando
    assert falso.pedido_em_arquivo == pedido
    pasta = Path(args[args.index("--add-dir") + 1])
    assert not pasta.exists()  # limpo depois da chamada


async def test_analisar_so_libera_pastas_de_fora(agy, tmp_path):
    falso = agy()
    projeto, fora = tmp_path / "projeto", tmp_path / "fora"
    (projeto / "src").mkdir(parents=True)
    fora.mkdir()
    dentro, externo = projeto / "src" / "a.py", fora / "log.txt"
    dentro.write_text("print(1)\n", encoding="utf-8")
    externo.write_text("erro\n", encoding="utf-8")

    await gemini.gemini_analisar(str(projeto), "revise", arquivos=[str(dentro), str(externo)])

    args = falso.chamadas[-1]
    liberadas = [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"]
    assert liberadas == [str(fora.resolve())]
    assert str(dentro.resolve()) in args[2]
    assert falso.pastas[-1] == str(projeto)


async def test_pergunta_roda_numa_pasta_temporaria_vazia(agy):
    falso = agy()
    await gemini.gemini_perguntar("oi")
    pasta = falso.pastas[-1]
    assert pasta and Path(pasta).name.startswith("ponte-gemini-")
    assert not Path(pasta).exists()  # apagada depois da chamada


@pytest.mark.parametrize("modelo", ["--dangerously-skip-permissions", "-x", "carbon high", "../agy"])
async def test_nome_de_modelo_perigoso_e_recusado(agy, modelo):
    falso = agy()
    resposta = await gemini.gemini_perguntar("oi", modelo=modelo)
    assert resposta.startswith("Nome de modelo inválido")
    assert falso.chamadas == []  # nem chegou a chamar o agy


def test_agy_path_errado_nao_cai_para_outro(monkeypatch, tmp_path):
    monkeypatch.setenv("AGY_PATH", str(tmp_path / "nao-existe.exe"))
    assert gemini._agy() is None


async def test_executar_programa_inexistente_vira_mensagem(tmp_path):
    codigo, _, erro = await gemini._executar([str(tmp_path / "nao-existe.exe")])
    assert codigo == -1 and erro.startswith("Não consegui executar")


async def test_analisar_pasta_inexistente(agy, tmp_path):
    agy()
    assert (await gemini.gemini_analisar(str(tmp_path / "nada"), "revise")).startswith("Pasta não encontrada")


async def test_sem_agy(monkeypatch):
    monkeypatch.setattr(gemini, "_agy", lambda: None)
    assert await gemini.gemini_pesquisar("algo") == gemini.NAO_INSTALADO


async def test_ferramentas_expostas():
    async with Client(gemini.servidor) as cliente:
        nomes = {f.name for f in (await cliente.list_tools()).tools}
    assert nomes == {"gemini_perguntar", "gemini_analisar", "gemini_pesquisar", "gemini_modelos"}
