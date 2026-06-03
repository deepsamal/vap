.PHONY: e2e test agent compose validate typecheck clean

# Run the E2E suite without docker. Uses pytest if available, else the stdlib
# runner baked into tests/test_e2e.py (the sandbox has no PyPI).
e2e test:
	@if command -v pytest >/dev/null 2>&1; then \
		pytest -q tests/test_e2e.py ; \
	else \
		echo "pytest not installed; using stdlib runner" ; \
		python3 tests/test_e2e.py ; \
	fi

# Start server+proxy locally and run the scripted agent workflows.
agent:
	@./run.sh agent

compose:
	docker compose up --build

validate:
	docker compose config

# Typecheck the TypeScript client (requires node/npm with registry access).
typecheck:
	cd clients/typescript && npm install && npm run typecheck

clean:
	rm -f vap-audit.jsonl tests/_test_audit.jsonl proxy/vap-audit.jsonl
