#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
ENV_EXAMPLE="$SCRIPT_DIR/.env.example"

# --- Load credentials from .env ---
if [[ ! -f "$ENV_FILE" ]]; then
    echo "❌ File .env non trovato in $SCRIPT_DIR"
    echo "   Esegui: cp .env.example .env"
    echo "   Poi apri il file .env e inserisci le tue credenziali."
    echo "   (Il file .env è gitignorato, non verrà mai committato.)"
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Verifica che tutte le variabili richieste siano state impostate
REQUIRED_VARS=(JIRA_API_USER JIRA_API_TOKEN TESTRAY_CLIENT_ID TESTRAY_CLIENT_SECRET)
MISSING=()
for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${!var}" ]]; then
        MISSING+=("$var")
    fi
done
if (( ${#MISSING[@]} > 0 )); then
    echo "❌ Variabili mancanti in .env: ${MISSING[*]}"
    echo "   Controlla $ENV_FILE e confrontalo con $ENV_EXAMPLE"
    exit 1
fi

# --- Parsing flag ---
export DRY_RUN=true
export RESUME=false
SUBCOMMAND=""
BUILD_IDS=()

while (( $# > 0 )); do
    case "$1" in
        --live) export DRY_RUN=false ;;
        --resume) export RESUME=true ;;
        --priorities|--summary|--diagnose|--inspect|--check-pr-failures|--poshi-burndown) SUBCOMMAND="$1" ;;
        --routine) shift; export ROUTINE="$1" ;;
        --routine=*) export ROUTINE="${1#--routine=}" ;;
        --build) shift; SUBCOMMAND="--build"; BUILD_IDS+=("$1") ;;
        --build=*) SUBCOMMAND="--build"; BUILD_IDS+=("${1#--build=}") ;;
        *)
            echo "❌ Argomento non riconosciuto: $1"
            echo "   Usa: --live | --resume | --routine <name|id> | --priorities | --summary | --inspect | --diagnose | --check-pr-failures | --poshi-burndown | --build <id>"
            exit 1
            ;;
    esac
    shift
done

# --- Sottocomandi ausiliari ---
if [[ "$SUBCOMMAND" == "--priorities" ]]; then
    echo "🔎 Elenco priorità Jira per LPD..."
    cd "$SCRIPT_DIR"
    uv run discover_jira_priorities.py
    exit 0
fi
if [[ "$SUBCOMMAND" == "--summary" ]]; then
    echo "📋 Generazione messaggio Slack per l'ultima run..."
    [[ -n "$ROUTINE" ]] && echo "   (filtrato sulla routine: $ROUTINE)"
    cd "$SCRIPT_DIR"
    uv run print_slack_summary.py
    exit 0
fi
if [[ "$SUBCOMMAND" == "--diagnose" ]]; then
    echo "🔬 Diagnostica: ticket Commerce chiusi oggi e SHA citati..."
    cd "$SCRIPT_DIR"
    uv run diagnose_closed_commerce_tickets.py
    exit 0
fi
if [[ "$SUBCOMMAND" == "--inspect" ]]; then
    echo "🔎 Ispezione build per le routine selezionate..."
    [[ -n "$ROUTINE" ]] && echo "   (filtrato sulla routine: $ROUTINE)"
    cd "$SCRIPT_DIR"
    uv run inspect_builds.py
    exit 0
fi
if [[ "$SUBCOMMAND" == "--check-pr-failures" ]]; then
    echo "🔍 PR review check: controllo dei ticket mergiati contro Acceptance..."
    [[ -n "$ROUTINE" ]] && echo "   (filtrato sulla routine: $ROUTINE)"
    if [[ -z "$GITHUB_TOKEN" ]]; then
        echo "ℹ️  GITHUB_TOKEN non impostato: le chiamate GitHub useranno il rate limit non autenticato (60 req/h)."
    fi
    cd "$SCRIPT_DIR"
    uv run check_pr_failures.py
    exit 0
fi
if [[ "$SUBCOMMAND" == "--poshi-burndown" ]]; then
    echo "📉 Conteggio Poshi rimasti da convertire (scan dei file sorgente)..."
    cd "$SCRIPT_DIR"
    uv run poshi_burndown.py
    exit 0
fi
if [[ "$SUBCOMMAND" == "--build" ]]; then
    if (( ${#BUILD_IDS[@]} == 0 )); then
        echo "❌ --build richiede almeno un id (es. --build 470911677)"
        exit 1
    fi
    echo "🔎 Ispezione build specifici: ${BUILD_IDS[*]}"
    cd "$SCRIPT_DIR"
    uv run inspect_builds.py "${BUILD_IDS[@]}"
    exit 0
fi

if [[ "$DRY_RUN" == "false" ]]; then
    echo "⚠️ ATTENZIONE: Modalità LIVE attiva. Verranno apportate modifiche a Jira e Testray."
else
    echo "ℹ️ Modalità DRY RUN attiva (nessuna modifica verrà effettuata)."
    echo "   Usa './run_local.sh --live' per eseguire le modifiche reali."
    echo "   Usa './run_local.sh --priorities' per scoprire i nomi priorità validi su LPD."
fi
if [[ "$RESUME" == "true" ]]; then
    echo "🔄 Modalità RESUME attiva: verrà ripreso il task in analisi più recente invece della build più nuova."
fi
if [[ -n "$ROUTINE" ]]; then
    echo "🎯 Routine selezionata: $ROUTINE (le altre verranno saltate)."
else
    echo "ℹ️ Nessun filtro routine: verranno analizzate tutte (Commerce, User Management)."
    echo "   Usa './run_local.sh --routine user_management' per limitare a una sola."
fi

echo "🚀 Avvio analisi in corso..."
echo ""

cd "$SCRIPT_DIR"
uv run analyze_testray_results.py
