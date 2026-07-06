.PHONY: setup doctor backend frontend dev test smoke import clean-safe

PYTHON ?= python3
VENV := backend/.venv
VENV_PY := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip

setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -r backend/requirements.txt
	cd frontend && npm install
	@if [ ! -f backend/.env ]; then \
		cp .env.example backend/.env; \
		echo "Created backend/.env from .env.example -- add your real BETSAPI_KEY there."; \
	fi

doctor:
	$(PYTHON) scripts/doctor.py

backend:
	cd backend && ../$(VENV)/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

frontend:
	cd frontend && npm run dev

dev:
	$(VENV_PY) scripts/start_dev.py

test:
	cd backend && ../$(VENV)/bin/python -m pytest -q
	cd frontend && npm run build

smoke:
	$(VENV_PY) scripts/smoke_test.py

import:
	$(VENV_PY) scripts/import_release.py --zip $(ZIP)

clean-safe:
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
	find . -name "*.pyc" -delete
	rm -rf frontend/dist
	@echo "Removed caches and build output only. .env, esoccer.db, and data/ were not touched."
