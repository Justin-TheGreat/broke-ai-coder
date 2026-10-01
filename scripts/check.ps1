.venv\Scripts\python -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
.venv\Scripts\python -m ruff format --check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
.venv\Scripts\python -m pytest
exit $LASTEXITCODE
