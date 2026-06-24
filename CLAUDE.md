# Monitor TRT-19 — engine + drill-down (instruções para o Claude)

Este repositório é o **engine do Monitor das Turmas Trabalhistas do TRT-19** (1ª e 2ª Turma,
Justiça do Trabalho de Alagoas) **mais os snapshots semanais** de cada rodada. Toda semana o
monitor coleta os acórdãos das duas Turmas no FALCÃO, classifica por **matéria trabalhista**
(incidência) + **classe** + **subtemas** + **incidentes processuais**, posta uma tabela de
percentuais no canal Slack `#trt19-turmas` e arquiva a rodada em `registros/<AAAA-MM-DD>/`.

## Para que você (Claude) é chamado aqui

Você é acionado por **`@Claude` no canal `#trt19-turmas` do Slack** (Claude Code na nuvem).
A mensagem semanal mostra só os **percentuais** — sem números de processo. O usuário (Felipe,
advogado) responde pedindo o **drill-down**: os **números dos processos** e as **ementas** de um
recorte (uma matéria, uma classe, um incidente, uma Turma, um relator, um termo). Seu trabalho é
ler o snapshot da semana neste repo e devolver esses acórdãos. Exemplos de pedido:

- "traz os números e as ementas dos processos sobre **vínculo de emprego**"
- "quais os **agravos de petição** da 2ª Turma dessa semana?"
- "lista os acórdãos com **prescrição** e me dá a ementa"
- "os processos de **horas extras** da relatora Fulana"

## Onde estão os dados

Cada rodada vive em **`registros/<AAAA-MM-DD>/`** (o rótulo é o **fim da janela**, por data de
juntada). Arquivos:

- **`classificado.csv`** — a **fonte do drill-down** (versão enxuta, sem o `inteiro_teor` cru para
  o repo ficar leve). Uma linha por acórdão, com: `numero` (CNJ), `turma`,
  `classe`/`classe_sigla`/`classe_curta`, `relator`, `data_julgamento`, `data_juntada`, `url`,
  `area` (matéria dominante), `materias` (todas, separadas por `|`), `subtema`/`subtemas`,
  `incidentes` e `ementa` (a ementa CNJ extraída do inteiro teor — o texto que você mostra). Para o
  inteiro teor completo, use o `url`.
- `resumo.json` — agregados (percentuais por Turma, destaques, incidentes, tendência Δ p.p.).
- `resumo.md` — versão legível do resumo. `slack.txt` — a mensagem que foi postada.

**Qual rodada usar:** se o usuário responde a uma mensagem semanal, use a rodada **mais recente**
(`registros/` em ordem; pegue a pasta de maior data). Se ele citar uma data/semana, use aquela.

## Como fazer o drill-down (NÃO recolete do FALCÃO)

Os dados já estão arquivados — **não rode `coletar`** para responder a um drill-down (seria lento
e pode divergir do que foi postado). Filtre o snapshot com o subcomando `filtrar`:

```bash
# PADRÃO (enxuto — número + Turma + relator + link, SEM ementa): é o que cabe e é entregue no Slack
python3 monitor_trt19.py filtrar \
  --inp registros/<AAAA-MM-DD>/classificado.csv --materia "Vínculo"

# SOB DEMANDA (ementa — só quando o usuário pedir, idealmente de processos específicos)
python3 monitor_trt19.py filtrar \
  --inp registros/<AAAA-MM-DD>/classificado.csv --materia "Vínculo" --com-ementa
```

Filtros (todos são **substring, case-insensitive**, e combináveis):

| Flag | Filtra por | Exemplos |
|---|---|---|
| `--materia` | matéria trabalhista | `"Vínculo"`, `"Horas extras"`, `"Adicionais"`, `"Execução"` |
| `--classe` | classe processual | `"Agravo de Petição"`, `"AP"`, `"Recurso Ordinário"`, `"ROT"` |
| `--turma` | Turma | `"1ª"`, `"Primeira"`, `"2ª"`, `"Segunda"` |
| `--incidente` | incidente processual | `"Gratuidade"`, `"Prescrição"`, `"Honorários"`, `"Deserção"` |
| `--subtema` | subtema lido na ementa | `"Periculosidade"`, `"Gestante"` |
| `--relator` | relator(a) | `"LUSTOSA"` |
| `--texto` | termo na ementa/inteiro teor | `"in itinere"`, `"terceirização"` |
| `--com-ementa` | **inclui o texto da ementa** na saída (use sempre que pedirem "ementas") | — |
| `--max-ementa N` | corta a ementa em N caracteres (default 1200) | — |

`--formato csv` se precisar tabular. Sem `--com-ementa` a saída traz número + classe + Turma +
matéria(s) + incidentes + relator + datas + link.

### Rótulos de matéria (`--materia`) — mapeie o pedido do usuário para um destes

`Vínculo de emprego` · `Verbas rescisórias` · `Horas extras / jornada` · `Intervalos
(intra/interjornada)` · `Adicionais (insalubridade / periculosidade / noturno)` · `Justa causa /
modalidade de rescisão` · `Equiparação / desvio e acúmulo de função` · `Salário e diferenças
salariais` · `Terceirização / responsabilidade subsidiária` · `Dano moral / existencial` ·
`Doença ocupacional / acidente de trabalho` · `FGTS` · `Estabilidade / garantia de emprego` ·
`Grupo econômico / sucessão de empregadores` · `Contribuições previdenciárias e fiscais` ·
`Execução trabalhista`.

Basta a substring distintiva (ex.: `--materia "Vínculo"`, `--materia "Terceiriz"`). A matéria é
**multivalorada**: um acórdão pode aparecer em vários filtros (a contagem por incidência não soma
100%) — isso é esperado.

## Como responder no Slack — REGRA DE OURO: enxuto primeiro, ementa sob demanda

⚠️ **O bot do Slack tem limite de tamanho.** Respostas longas (várias ementas de uma vez) **não são
entregues no canal** — ficam só na sessão, e para o usuário parece que "não respondeu". Por isso:

1. **Resposta padrão = lista ENXUTA.** Uma linha por acórdão: **número CNJ + Turma + relator +
   link** — **SEM ementa**. Cabe na mensagem e é sempre entregue. Abra dizendo o **total** e de qual
   **rodada/semana** vieram os dados.
2. **Ementa só quando o usuário pedir** — e de preferência de **processos específicos** ("me dá a
   ementa do 0000511-68"). Nunca despeje todas as ementas do recorte de uma vez.
3. **Se o recorte for grande** (mais de ~40 acórdãos, ou se a lista enxuta passar de ~3.500
   caracteres): **não tente mandar tudo numa mensagem.** Em vez disso: (a) diga o **total**, e
   (b) poste os números em **blocos** (várias respostas no thread) **ou** ofereça **refinar** (por
   Turma, classe, relator, subtema, incidente). Avise quantos ficaram de fora de cada bloco.
- Os acórdãos são **públicos** (jurisprudência publicada) — pode listar número e link à vontade.
- Link do inteiro teor: `https://jurisprudencia.jt.jus.br/jurisprudencia-nacional/pesquisa/numero/<CNJ>?abaSelecionada=acordaos`.

## Limitações honestas (não esconda do usuário)

- **Ementa dispensada (~8%):** em acórdãos do **rito sumaríssimo** a ementa é dispensada na
  origem; nesses casos a coluna `ementa` começa com "Dispensada... RITO SUMARÍSSIMO" (não há
  ementa real). O número, a classe e o **link** continuam corretos — aponte o link para o inteiro
  teor. Não invente ementa.
- **Classificação léxica:** matéria/incidentes vêm de léxico determinístico sobre a ementa +
  referências legais (não há campo de assunto/CNJ na fonte). É de alta precisão (resíduo órfão
  ~1%), mas não é infalível — se algo parecer fora do recorte, confira a ementa/inteiro teor antes
  de afirmar.
- **Nunca cite um acórdão que não esteja no `classificado.csv` da rodada.** Não traga julgados de
  fora do snapshot.

## Coleta nova (só se pedirem explicitamente uma janela não arquivada)

Se — e só se — o usuário pedir uma semana/janela que ainda **não** está em `registros/`, aí sim
rode a cadeia completa (`coletar` → `classificar` → `agregar`), avisando que leva alguns minutos
(a API do FALCÃO tem rate limit e cap de paginação, tratados pelo engine). Veja o README e o
`--help` de cada subcomando. Para drill-down do que já foi postado, **sempre** use o snapshot.

## Modelos

As **conversas de construção/manutenção** deste monitor (com o Felipe) usam sempre o **modelo mais
recente e robusto** (família Opus). A **execução do monitor** (a rodada semanal: coleta →
classificação → agregação → notificação, na routine de nuvem) roda em **Sonnet** — esqueleto
determinístico, só o resíduo de classificação chama o modelo. O drill-down via Slack é leve (ler
CSV e formatar) e não exige fixar modelo.
