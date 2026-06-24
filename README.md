# monitor-trt19-trabalhista — engine

Engine (Python **stdlib**, sem dependências — usar `python3`/`/usr/bin/python3`) do
**Monitor das Turmas Trabalhistas do TRT-19** (1ª e 2ª Turma, Justiça do Trabalho de
Alagoas). Mede **o que cada Turma está julgando**, semana a semana, e posta a tabela
de percentuais num canal Slack. Usado pela skill local `monitor-trt19-trabalhista` e
pela routine de nuvem (claude.ai) que roda semanalmente (Sonnet 4.6, headless).

## Fonte

API pública **`no-auth`** do **FALCÃO** (Jurisprudência Unificada da Justiça do
Trabalho, `jurisprudencia.jt.jus.br`) — JSON, sem login, sessão auto-emitida.
Coleta por janela de **data de juntada/disponibilização**; ementa (embutida no
inteiro teor) e `referenciaLegislativa` vêm inline. Sem PDF, sem navegador.

## Eixos de análise

- **Turma** — `orgaoJulgador = Primeira Turma | Segunda Turma`.
- **Classe processual** — metadado CNJ (ROT, RORSum, AP, AIAP, AIRO, MS, ED…). Soma 100% por Turma.
- **Matéria trabalhista** — léxico determinístico (`taxonomia.json`) lido na ementa + referências
  legais. **Multivalorada** (um acórdão junta várias) → reportada por **incidência** (não soma 100%):
  verbas rescisórias, horas extras/jornada, vínculo, justa causa, adicionais, terceirização/resp.
  subsidiária, dano moral, FGTS, estabilidade, execução, etc.
- **Subtemas** — recorte fino por matéria (ex.: Adicionais → insalubridade/periculosidade/noturno).
- **Incidentes processuais** — eixo transversal multivalorado (gratuidade pós-reforma, honorários
  sucumbenciais/periciais, prescrição, deserção/preparo, cerceamento, nulidade, execução/penhora,
  IDPJ, competência, ED, revelia, litigância, limitação à inicial).
- **Tendência** — Δ p.p. contra o registro datado anterior.

## Subcomandos

```
python3 monitor_trt19.py coletar     --inicio AAAA-MM-DD --fim AAAA-MM-DD --out coleta_bruta.csv
python3 monitor_trt19.py classificar --inp coleta_bruta.csv --out classificado.csv
python3 monitor_trt19.py agregar     --inp classificado.csv --saida-dir <pasta> --base-dir <mãe> \
                                     --rotulo AAAA-MM-DD --inicio AAAA-MM-DD --fim AAAA-MM-DD
python3 monitor_trt19.py notificar   --saida-dir <pasta> --config config.local.json
python3 monitor_trt19.py filtrar     --inp classificado.csv [--turma|--classe|--materia|--subtema|--incidente|--texto ...] [--com-ementa]
python3 monitor_trt19.py publicar    --saida-dir <pasta> --rotulo AAAA-MM-DD --repo-dir <clone> [--push]
```

Rodar `python3 monitor_trt19.py <subcomando> --help` para as opções.

## Registros semanais e drill-down via Slack (`@Claude`)

Cada rodada é arquivada em **`registros/<AAAA-MM-DD>/`** (`classificado.csv` + `resumo.json` +
`resumo.md` + `slack.txt`). É a **fonte do drill-down**: respondendo à mensagem semanal no canal
`#trt19-turmas`, o usuário menciona **`@Claude`** ("traz os números e as ementas dos processos
sobre vínculo de emprego") e uma sessão do **Claude Code na nuvem** clona este repo, roda
`filtrar` sobre o snapshot da semana e devolve os acórdãos com **número + matéria(s) + ementa +
link**. O playbook que a sessão segue está em **`CLAUDE.md`** (carregado automaticamente).

O `publicar` copia o registro datado para `registros/<rótulo>/` e, com `--push`, commita e envia —
é o que mantém o drill-down do Slack alimentado a cada rodada (local ou routine de nuvem).

## Configuração

Copie `config.example.json` para `config.local.json` (**não versionado** — contém o webhook
secreto do Slack). O payload do `notificar` precisa do campo `channel` (já enviado).

## Limites da fonte (tratados pelo engine)

`size=5` obrigatório; paginação travada na página 40 (200 docs/query) → fatiamento adaptativo
por data e depois por classe; rate limit ~50 req/janela (pausa automática). Detalhes da fonte
e do domínio na skill local e em `= Monitor TRT-19/FONTE_FALCAO.md`.
