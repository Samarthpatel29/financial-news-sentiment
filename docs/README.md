# Documentation map

Start with **HANDOFF.md** if you are going to work on the code.

| File | What it is | Read it when |
|---|---|---|
| **HANDOFF.md** | The developer guide: first 30 minutes, how the pieces fit, file map, API reference, tests, known gaps, roadmap | You are picking the project up |
| **PREDICTION_TRACK_RECORD.md** | How accurate the ratings actually are, measured from the database | You want to know if it works |
| **DEMO_WALKTHROUGH.md** | A screen-by-screen tour of the dashboard | You want to see what it does |
| **PROJECT_REPORT.md** | The full write-up: goals, method, results, conclusions | You want the formal report |
| **PROJECT_STATUS.md** | What is done, what is outstanding, and recent corrections | You want the current state in one page |
| **DIFFERENTIATION.md** | How this differs from Finviz and similar tools | You are wondering why it exists |
| **FUNDAMENTALS_PLAN.md** | The original design note for the SEC-filing engine (now built) | You want the reasoning behind the filings signal |

Regenerate the measured numbers with `python scripts/measure_accuracy.py`.
