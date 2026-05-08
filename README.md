# Github Actions for liferay-headless

## Workflows
### [Sync liferay-portal fork](https://github.com/liferay-headless/github-actions/blob/master/.github/workflows/sync-liferay-portal.yml)

This is a cron job that runs hourly to sync [liferay-headless/liferay-portal](https://github.com/liferay-headless/liferay-portal) to [liferay/liferay-portal](https://github.com/liferay/liferay-portal).

### [Analyze TestRay results](https://github.com/liferay-headless/github-actions/blob/master/.github/workflows/analyze-testray-results.yml)

This analyzes the latest TestRay run and keeps Jira tickets in sync for each failure.

## Esecuzione locale (`scripts/python/`)

Tutti gli script Python si lanciano tramite `./run_local.sh`, che carica le credenziali da `.env` (copia da `.env.example` la prima volta) e dispaccia il sotto-comando giusto.

Senza flag esegue l'analizzatore principale (`analyze_testray_results.py`) in **DRY_RUN** su tutte le routine selezionabili (Commerce, User Management): nessuna scrittura su Jira o Testray, solo log di cosa farebbe.

### Flag principali

| Flag | Effetto |
|---|---|
| `--live` | Disattiva il DRY_RUN: applica davvero le modifiche a Jira e Testray |
| `--resume` | Riprende il task di analisi più recente invece di partire dalla build più nuova |
| `--routine <name\|id>` | Limita l'analisi a una sola routine (es. `--routine user_management`). Se omesso, gira su tutte |

### Sotto-comandi diagnostici

Sostituiscono l'analizzatore principale, non lo affiancano.

| Flag | Script eseguito | Scopo |
|---|---|---|
| `--summary` | `print_slack_summary.py` | Ristampa il recap Slack dell'ultima build DONE di ogni routine, senza rifare l'analisi |
| `--diagnose` | `diagnose_closed_commerce_tickets.py` | Per ogni ticket con label `commerce_routine_tasks` chiuso oggi, estrae lo SHA citato nel commento di chiusura e lo incrocia con le ultime 20 build Commerce e UM. Serve a distinguere chiusure legittime (SHA esclusivo Commerce) da chiusure errate (SHA esclusivo UM) |
| `--inspect` | `inspect_builds.py` | Elenca le ultime 20 build di ogni routine selezionata, con `id`, `importStatus`, `dateCreated`, `dueDate`, `gitHash`. Espone i campi che la UI di Testray non mostra ma che il picker usa |
| `--build <id> [<id> ...]` | `inspect_builds.py <ids>` | Ispeziona build specifiche per id. Utile quando una build appare nella UI di Testray ma non viene restituita dall'API della routine |
| `--priorities` | `discover_jira_priorities.py` | Lista le priorità Jira definite nell'istanza e quelle ammesse su LPD/Task. Da usare quando si aggiorna `_PRIORITY_LADDER` in `utils/testray_helpers.py` |

### Esempi

```bash
# Dry run completo (default)
./run_local.sh

# Esecuzione reale, solo User Management
./run_local.sh --live --routine user_management

# Riparti dal task in analisi più recente, in dry run
./run_local.sh --resume

# Solo il messaggio Slack dell'ultima run, senza rianalizzare
./run_local.sh --summary

# Verifica se chiusure di oggi su Commerce sono legittime
./run_local.sh --diagnose

# Ispeziona le build più recenti di Commerce
./run_local.sh --inspect --routine commerce

# Ispeziona due build specifiche per id
./run_local.sh --build 470911677 --build 470910001
```
