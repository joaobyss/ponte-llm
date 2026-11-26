# ponte-llm

Dois servidores [MCP](https://modelcontextprotocol.io) que deixam o Claude Code (ou qualquer agente
que fale MCP) passar trabalho para outros modelos:

- `ponte-local` manda para um LLM rodando na sua máquina (llama.cpp, LM Studio, Ollama, vLLM). Sai
  de graça e não gasta o contexto do agente principal.
- `ponte-gemini` manda para o Gemini, pelo Antigravity CLI (`agy`), só com permissão de leitura. Uso
  para segunda opinião, pesquisa na web e para ler repositórios grandes.

> **English:** two MCP servers that let Claude Code (or any MCP client) hand work off to a local
> LLM (any OpenAI-compatible server) or to Google's Gemini through the Antigravity CLI, read-only.
> Install with `uvx --from git+https://github.com/joaobyss/ponte-llm ponte-local` (or `ponte-gemini`).
> Docs below are in Portuguese.

## Por que eu fiz

Rodo modelo de linguagem no meu PC desde 2023, e o Claude Code vivia gastando contexto com coisa que
o modelo local resolvia. Um log de 20 mil linhas, por exemplo: o Claude só precisa do resumo. Com a
`ponte-local`, o modelo local lê o arquivo em partes e só o resumo volta.

O Gemini entrou por outro motivo. Um modelo que não viu a conversa revisa o código com outros olhos e
pega o que o primeiro deixou passar.

```
Claude Code ──MCP──▶ ponte-local  ──HTTP──▶ llama-server / LM Studio / Ollama
            └─MCP──▶ ponte-gemini ──CLI───▶ agy -p --mode plan --sandbox
```

## Status

| Parte | Estado |
|---|---|
| `ponte-local` | ✅ testes passando com um servidor simulado |
| `ponte-gemini` | ✅ testes passando com um `agy` simulado, e testado com o `agy` de verdade |
| CI | ✅ Linux, Windows e macOS, Python 3.11 e 3.13 |
| Demonstração com modelo local de verdade | ⏳ falta gravar |
| PyPI | ⏳ ainda não publicado, instale pelo GitHub |

## Ferramentas

| Servidor | Ferramenta | O que faz |
|---|---|---|
| `ponte-local` | `local_perguntar` | Pergunta direta ao modelo local: tradução, rascunho, código repetitivo. |
| | `local_resumir_arquivo` | Lê um arquivo grande em partes e devolve só o resumo, com o `foco` que você pedir. |
| | `local_modelos` | Lista os modelos que o servidor local oferece. |
| `ponte-gemini` | `gemini_perguntar` | Segunda opinião sobre um plano, uma decisão ou um trecho de código. |
| | `gemini_analisar` | Abre uma pasta só para leitura: revisão, caça a bugs, PDFs, imagens, vídeos. |
| | `gemini_pesquisar` | Pesquisa na web com o Google e devolve um resumo com os links. |
| | `gemini_modelos` | Lista os modelos que a sua conta pode usar. |

## Instalação

Precisa de Python 3.11+ e do [uv](https://docs.astral.sh/uv/). Os comandos abaixo instalam direto
do GitHub. O `uvx` cria um ambiente isolado, então nada fica instalado no seu Python.

### Modelo local

1. Suba um servidor compatível com a API da OpenAI. Por exemplo:
   - llama.cpp: `llama-server -m modelo.gguf --port 8080`
   - LM Studio: aba *Developer* → *Start server* (porta 1234)
   - Ollama: já responde em `http://127.0.0.1:11434/v1`
2. Registre no Claude Code:

   ```bash
   claude mcp add ponte-local -e PONTE_LOCAL_URL=http://127.0.0.1:8080 -- uvx --from git+https://github.com/joaobyss/ponte-llm ponte-local
   ```

### Gemini

1. Instale o Antigravity CLI pelo site oficial e rode `agy` uma vez num terminal para fazer login.
2. Registre no Claude Code:

   ```bash
   claude mcp add ponte-gemini -- uvx --from git+https://github.com/joaobyss/ponte-llm ponte-gemini
   ```

### Claude Desktop (ou outro cliente que usa arquivo JSON)

```json
{
  "mcpServers": {
    "ponte-local": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/joaobyss/ponte-llm", "ponte-local"],
      "env": { "PONTE_LOCAL_URL": "http://127.0.0.1:8080" }
    },
    "ponte-gemini": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/joaobyss/ponte-llm", "ponte-gemini"]
    }
  }
}
```

## Configuração

É tudo por variável de ambiente, e nenhuma é obrigatória.

### `ponte-local`

| Variável | Padrão | Para quê |
|---|---|---|
| `PONTE_LOCAL_URL` | `http://127.0.0.1:8080` | Endereço do servidor, com ou sem `/v1` no fim. |
| `PONTE_LOCAL_MODELO` | primeiro de `/v1/models` | Qual modelo usar. |
| `PONTE_LOCAL_API_KEY` | vazio | Se o servidor pedir chave. |
| `PONTE_LOCAL_TEMPERATURA` | `0.7` | Temperatura. |
| `PONTE_LOCAL_SEM_PENSAR` | vazio | Com `1`, desliga o raciocínio de modelos como o Qwen3 (`enable_thinking=false`). |
| `PONTE_LOCAL_TEMPO` | `900` | Tempo limite por chamada, em segundos. |
| `PONTE_LOCAL_PARTE` | `60000` | Caracteres por parte ao resumir (~15k tokens). Diminua se o contexto do modelo for pequeno. |
| `PONTE_LOCAL_LIMITE` | `3000000` | Tamanho máximo do arquivo a resumir, em caracteres. |

### `ponte-gemini`

| Variável | Padrão | Para quê |
|---|---|---|
| `AGY_PATH` | procura no PATH | Caminho do `agy`. Se estiver definido e errado, dá erro em vez de usar outro `agy`. |
| `PONTE_GEMINI_MODELO` | `hydrogen` | Modelo de `gemini_perguntar` e `gemini_pesquisar`. |
| `PONTE_GEMINI_MODELO_FORTE` | `carbon` | Modelo de `gemini_analisar`. |
| `PONTE_GEMINI_MODO` | `plan` | Modo do `agy`. Cuidado: os outros modos podem gravar arquivos. |
| `PONTE_GEMINI_TEMPO` | `900` | Tempo limite por chamada, em segundos. |
| `PONTE_GEMINI_TMP` | pasta temporária do sistema | Onde criar a pasta temporária de cada chamada. |

## Como funciona por dentro

O Gemini não edita nada. Ele roda com `--mode plan --sandbox`, e o pedido vai com uma instrução de
responder direto. Quem aplica qualquer mudança é o agente principal, depois de conferir. O nome do
modelo e o do modo só aceitam letras, números, ponto e hífen, então ninguém consegue enfiar uma
opção como `--dangerously-skip-permissions` no comando. Tem teste para isso.

`gemini_perguntar` e `gemini_pesquisar` rodam numa pasta temporária vazia, e não na pasta onde o
servidor foi aberto. Assim o Gemini não vê seus arquivos quando você só fez uma pergunta.
`gemini_analisar` libera a pasta que você pediu e, se algum item de `arquivos` estiver fora dela, a
pasta desse arquivo também.

Pedido com mais de 12 mil caracteres vai por arquivo, porque a linha de comando do Windows tem
limite de tamanho. O arquivo fica na pasta temporária da chamada, que é apagada no fim.

Você pode pedir o modelo pelo apelido e pelo esforço: `carbon` + `high` vira `gemini-4-carbon-high`.
Se aquele esforço não existir para o modelo, a ponte usa o mais alto abaixo dele (ou o mais baixo que
existir, se não houver nenhum abaixo).

O resumo da `ponte-local` acontece em rodadas. Cada parte do arquivo vira um resumo; depois os
resumos são juntados em grupos que cabem numa parte, até sobrar um só. Nenhuma chamada manda mais
que `PONTE_LOCAL_PARTE` caracteres de conteúdo para o modelo, e é por isso que funciona com modelos
de 32k a 64k tokens de contexto.

Modelos como o Qwen3 escrevem um bloco `<think>` antes da resposta. A ponte tira esse bloco. Se o
modelo gastar todos os tokens pensando e não chegar à resposta, a ferramenta avisa, em vez de
devolver o raciocínio cortado no meio.

Quando algo dá errado (servidor desligado, `agy` sem login, modelo recusado), a ferramenta devolve
uma mensagem dizendo o que fazer. O agente lê isso e consegue seguir sem o ajudante.

## Limites

- O modelo local é menor que o agente principal. Confira no arquivo original o que for crítico,
  como números de linha, nomes e valores.
- A `ponte-gemini` depende do JSON que o `agy` imprime com `--output-format json`. Testei no agy 1.5;
  uma versão nova pode mudar esse formato.
- Nenhuma das duas pontes limita quais arquivos podem ser lidos. Elas leem o que o agente pedir, com
  as permissões do seu usuário.
- Cuidado com injeção de prompt. Se você mandar o Gemini analisar um repositório de outra pessoa, um
  texto escondido lá dentro pode tentar dar ordens a ele. O modo `plan` impede edição e comandos,
  mas a busca na web continua ligada. Então não analise código desconhecido numa pasta que tenha
  segredos, como um `.env`.
- O texto que você manda vai direto para o `agy`, e um comando de barra (`/algo`) no começo de uma
  linha pode ser interpretado por ele. A opção que desligaria isso também desliga o modo `plan`.
- Se `PONTE_LOCAL_URL` apontar para outra máquina, o conteúdo dos arquivos resumidos vai pela rede
  até ela. Fora da sua rede local, use `https`.
- No Windows, quando o tempo limite estoura, só o processo do `agy` é encerrado. Processos que ele
  tenha aberto podem continuar rodando até terminar sozinhos.

## Desenvolvimento

```bash
uv sync
uv run pytest
uv run ruff check .
```

Os testes rodam sem GPU, sem rede e sem `agy`. O servidor local é simulado com
`httpx.MockTransport`, e o `agy` é trocado por uma função que devolve saídas prontas.

## Licença

MIT
