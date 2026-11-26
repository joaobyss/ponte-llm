import importlib
import json

import httpx
import pytest
from mcp import Client

from ponte_llm import local


class ServidorFalso:
    """Imita um servidor compatível com a API da OpenAI e guarda os pedidos recebidos."""

    def __init__(self, respostas: list[str] | None = None, modelos: list[str] | None = None, status: int = 200):
        self.respostas = list(respostas or ["ok"])
        self.modelos = modelos if modelos is not None else ["modelo-a", "modelo-b"]
        self.status = status
        self.pedidos: list[dict] = []

    def __call__(self, pedido: httpx.Request) -> httpx.Response:
        if pedido.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in self.modelos]})
        corpo = json.loads(pedido.content)
        self.pedidos.append(corpo)
        if self.status != 200:
            return httpx.Response(self.status, text="falhou")
        texto = self.respostas.pop(0) if len(self.respostas) > 1 else self.respostas[0]
        return httpx.Response(200, json={"choices": [{"message": {"content": texto}}]})


@pytest.fixture
def servidor(monkeypatch):
    def montar(**kwargs) -> ServidorFalso:
        falso = ServidorFalso(**kwargs)
        monkeypatch.setattr(local, "_TRANSPORTE", httpx.MockTransport(falso))
        monkeypatch.setattr(local, "_modelo_descoberto", None)
        monkeypatch.setattr(local, "MODELO", "")
        return falso

    return montar


def texto_de(resultado) -> str:
    return resultado.content[0].text


def test_dividir_respeita_linhas_e_tamanho():
    texto = "".join(f"linha {i:03d}\n" for i in range(100))  # 10 caracteres por linha
    partes = local.dividir(texto, tamanho=95)
    assert "".join(partes) == texto
    assert all(len(p) <= 95 for p in partes)
    assert all(p.endswith("\n") for p in partes)


def test_dividir_fatia_linha_gigante_sem_perder_nada():
    texto = "a" * 500 + "\nfim\n"
    partes = local.dividir(texto, tamanho=100)
    assert all(len(p) <= 100 for p in partes)
    assert "".join(partes) == texto


def test_limpar_remove_pensamento_so_do_comeco():
    assert local._limpar("<think>hmm\nvários passos</think>\n\nResposta.") == "Resposta."
    assert local._limpar("Use <think>x</think> no prompt.") == "Use <think>x</think> no prompt."


def test_limpar_pensamento_sem_fim_vira_erro():
    with pytest.raises(local.ErroLocal, match="raciocinando"):
        local._limpar("<think>pensando pensando... e acabou o limite")


def test_url_com_v1_nao_duplica_e_env_vazia_nao_derruba(monkeypatch):
    monkeypatch.setenv("PONTE_LOCAL_URL", "http://127.0.0.1:11434/v1/")
    monkeypatch.setenv("PONTE_LOCAL_PARTE", "")
    try:
        importlib.reload(local)
        assert local.BASE_URL == "http://127.0.0.1:11434"
        assert local.TAMANHO_PARTE == 60_000
    finally:
        monkeypatch.delenv("PONTE_LOCAL_URL")
        monkeypatch.delenv("PONTE_LOCAL_PARTE")
        importlib.reload(local)


async def test_perguntar_usa_primeiro_modelo_e_contexto(servidor):
    falso = servidor(respostas=["<think>x</think>Olá!"])
    async with Client(local.servidor) as cliente:
        resultado = await cliente.call_tool("local_perguntar", {"pergunta": "oi", "contexto": "apoio"})
    assert texto_de(resultado) == "Olá!"
    pedido = falso.pedidos[0]
    assert pedido["model"] == "modelo-a"
    assert [m["content"] for m in pedido["messages"]][1:] == ["Contexto:\napoio", "oi"]
    assert "chat_template_kwargs" not in pedido


async def test_sem_pensar_liga_chat_template_kwargs(servidor, monkeypatch):
    falso = servidor()
    monkeypatch.setattr(local, "SEM_PENSAR", True)
    await local.local_perguntar("oi")
    assert falso.pedidos[0]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_servidor_desligado_vira_mensagem(monkeypatch):
    def recusa(pedido):
        raise httpx.ConnectError("recusado", request=pedido)

    monkeypatch.setattr(local, "_TRANSPORTE", httpx.MockTransport(recusa))
    monkeypatch.setattr(local, "_modelo_descoberto", None)
    assert await local.local_perguntar("oi") == local.DESLIGADO


async def test_erro_http_vira_mensagem(servidor):
    servidor(status=500)
    assert (await local.local_perguntar("oi")).startswith("Erro do servidor local (500)")


async def test_resumir_arquivo_em_partes(servidor, monkeypatch, tmp_path):
    monkeypatch.setattr(local, "TAMANHO_PARTE", 50)
    arquivo = tmp_path / "log.txt"
    arquivo.write_text("".join(f"evento {i}\n" for i in range(20)), encoding="utf-8")
    falso = servidor(respostas=["r1", "r2", "r3", "r4", "FINAL"])

    resposta = await local.local_resumir_arquivo(str(arquivo), foco="erros")

    # 190 caracteres em partes de 50 = 4 partes. Os 4 resumos juntos não cabem numa parte, então a
    # junção acontece em rodadas: 2 grupos e depois a junção final.
    assert len(falso.pedidos) == 7
    assert resposta.endswith("FINAL")
    assert "4 parte(s)" in resposta
    for pedido in falso.pedidos[4:]:
        enviado = pedido["messages"][1]["content"].split("\n\n", 1)[1]
        assert len(enviado) <= 50  # nenhuma junção manda mais que uma parte ao modelo
    assert "Foque em: erros" in falso.pedidos[0]["messages"][1]["content"]


async def test_resumir_arquivo_inexistente(servidor, tmp_path):
    servidor()
    assert (await local.local_resumir_arquivo(str(tmp_path / "nao.txt"))).startswith("Arquivo não encontrado")


async def test_resposta_vazia_vira_mensagem(servidor):
    servidor(respostas=[""])
    assert await local.local_perguntar("oi") == "O modelo local devolveu uma resposta vazia."


async def test_conexao_que_cai_no_meio_vira_mensagem(monkeypatch):
    def cai(pedido):
        raise httpx.ReadError("caiu", request=pedido)

    monkeypatch.setattr(local, "_TRANSPORTE", httpx.MockTransport(cai))
    monkeypatch.setattr(local, "_modelo_descoberto", None)
    assert (await local.local_perguntar("oi")).startswith("A conexão com o servidor local caiu")


async def test_modelos_lista_e_marca_o_em_uso(servidor):
    servidor(modelos=["qwen", "llama"])
    assert await local.local_modelos() == "Em uso: qwen\n- qwen\n- llama"


async def test_ferramentas_expostas():
    async with Client(local.servidor) as cliente:
        nomes = {f.name for f in (await cliente.list_tools()).tools}
    assert nomes == {"local_perguntar", "local_resumir_arquivo", "local_modelos"}
