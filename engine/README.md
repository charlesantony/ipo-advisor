# IPO Advisor v0.4.4 — Prospective Experiment Tracking

V0.4.4 does not change Research Model V1 or SME V2 shadow thresholds.

The purpose is to build clean forward-looking evidence.

## First checkpoint

Only canonical closing-day captures around 2:30 PM IST count.

Target:

`20 exact 2:30 PM listed observations`

The dashboard shows exact captures, listed/pending outcomes, Mainboard/SME split,
V1 performance, V2 shadow performance, a hypothetical V1+V2 union, >=20% winner
capture, and every exact IPO with its actual listing gain.

## Guardrails

- V1 stays frozen.
- V2 stays shadow-only.
- At 20 exact listed observations the status becomes `READY_FOR_MODEL_REVIEW`.
- The application does not automatically retrain, retune, replace V1, or promote V2.

## Daily behavior

The 2026 tracker continues to update daily at 6:00 PM IST with startup catch-up.
Each tracker sync also writes:

`logs/reports/*PROSPECTIVE_EXPERIMENT_2026*.json`

## Exact-data requirement

Keep the app running on IPO closing days around 2:30 PM IST.

Retrospective proxy rows do not count toward the checkpoint.

## Run

```bash
cd ipo_advisor_v0.4.4
chmod +x *.sh
./copy_v043_database.sh
./start.sh
```

Open `http://127.0.0.1:8000`, then Historical / backtest.

New log markers:

- `PROSPECTIVE_EXPERIMENT`
- `PROSPECTIVE_CHECKPOINT_REACHED`

The checkpoint means "review manually", not "change the model".
