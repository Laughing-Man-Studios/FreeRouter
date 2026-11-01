# The local mirror of `.github/workflows/ci.yml`.
#
# The CI workflow header states that a green run means `make check` would pass.
# That was aspirational until this file existed. Now each job below maps to one
# workflow job, so a change to a gate is a one-line edit in two visible places
# rather than a mental mapping.
#
# `check` is the gate. `docker` is the remaining CI job and is deliberately not
# part of `check`: it builds an image, and a slow gate is a gate people learn to
# skip.

UV ?= uv
DOCKER ?= docker
IMAGE ?= free-router:local

.PHONY: setup check lint typecheck test test-hermetic markdown format docker

setup:
	$(UV) sync --frozen --dev

check: lint typecheck test markdown

## CI job "Lint & Format". Two invocations on purpose: a failure then names
## which half disagreed.
lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

## CI job "Type Check (mypy strict)". Strict is configured in pyproject.toml and
## repeated here so a config change cannot silently relax the gate.
typecheck:
	$(UV) run mypy --strict src/

## CI job "Test".
test:
	$(UV) run pytest -v

## The credential-independence half of the "Test" job.
##
## CI asserts no provider credential is present, which proves the suite cannot
## reach a provider and therefore cannot spend quota. A developer shell usually
## has one set, so this reproduces that condition rather than trusting it.
test-hermetic:
	env -u GOOGLE_KEY_PRIMARY -u GOOGLE_KEY_SECONDARY \
	    -u MISTRAL_KEY_PRIMARY -u MISTRAL_KEY_SECONDARY \
	    $(UV) run pytest -v

## CI job "Markdown Lint". Uses `.markdownlint-cli2.yaml`, so no globs here.
markdown:
	npx --yes markdownlint-cli2

## Not a CI job: apply what the two lint steps would have complained about.
format:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

## CI job "Docker Build", up to but not including the container smoke test.
##
## The smoke test needs a long-lived container and a published port, which is
## awkward to make idempotent in a make target; run it by hand from ci.yml when a
## change touches `config.yaml` or the Dockerfile's startup path.
docker:
	$(DOCKER) build -t $(IMAGE) .
	@user="$$($(DOCKER) inspect $(IMAGE) --format '{{.Config.User}}')"; \
	if [ -z "$$user" ] || [ "$$user" = "0" ] || [ "$$user" = "root" ]; then \
		echo "FAIL: image would run as root (USER='$$user')" >&2; \
		exit 1; \
	fi; \
	echo "runs as user: $$user"
	@if [ "$$($(DOCKER) inspect $(IMAGE) --format '{{json .Config.Healthcheck}}')" = "null" ]; then \
		echo "FAIL: no HEALTHCHECK defined" >&2; \
		exit 1; \
	fi; \
	echo "healthcheck present"
	@$(DOCKER) run --rm $(IMAGE) python -c \
		"import sys; assert sys.version_info[:2] == (3, 13), sys.version"
	@echo "runtime is python 3.13"
