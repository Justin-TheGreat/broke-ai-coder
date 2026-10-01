$ErrorActionPreference = "Stop"
py -3.14 -m venv .venv
if ($LASTEXITCODE -ne 0) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
.venv\Scripts\python -m pip install -e ".[dev]"
exit $LASTEXITCODE
