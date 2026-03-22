.PHONY: up down logs test lint init-db seed fmt clean

# Start all Docker infrastructure services
up:
	docker compose up -d

# Stop all services
down:
	docker compose down

# View logs (all services)
logs:
	docker compose logs -f

# View logs for a specific service (usage: make logs-svc SVC=redpanda)
logs-svc:
	docker compose logs -f $(SVC)

# Initialize the database schema
init-db:
	python scripts/init_db.py

# Seed historical data
seed:
	python scripts/seed_data.py

# Run all tests
test:
	pytest tests/ -v --tb=short

# Run unit tests only
test-unit:
	pytest tests/unit/ -v --tb=short

# Run integration tests (requires Docker services)
test-integration:
	pytest tests/integration/ -v --tb=short -m integration

# Run tests with coverage
test-cov:
	pytest tests/ -v --cov=core --cov=data --cov=strategies --cov=portfolio --cov=execution --cov=backtesting --cov-report=html

# Lint and type check
lint:
	ruff check .
	mypy core/ data/ strategies/ portfolio/ execution/

# Format code
fmt:
	ruff format .
	ruff check --fix .

# Start FastAPI backend (development)
backend:
	uvicorn dashboard.backend.main:app --host 0.0.0.0 --port 8000 --reload

# Start Next.js frontend (development)
frontend:
	cd dashboard/frontend && npm run dev

# Clean up generated files
clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .mypy_cache -exec rm -rf {} +
	rm -rf htmlcov/ .coverage
