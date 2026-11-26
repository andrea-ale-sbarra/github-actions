# Testray routine scripts

Before changing any script here, read the "Changing the scripts" section of `README.md` and follow it: every change explains its context and reasons, split between comments in the code and the commit message, and is checked on real builds (dry run or a read-only script) before it is committed.

The rest of `README.md` covers setup, flags (`run_local.sh` runs in DRY_RUN unless `--live` is passed) and the file layout.

Commit each change to the scripts at the end of the session that made it, with its verification written in the message while it is fresh. Changes left uncommitted pile up across sessions: on 2026-11-26 five changes from 23-25 November were still in the working tree, mixed in the same files as the day's work, and had to be split by hand; for some, how they had been checked could no longer be told.
