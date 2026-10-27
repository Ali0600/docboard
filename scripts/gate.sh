#!/bin/sh
# The local gate: the same checks CI runs. Every step runs and reports; the exit code is
# non-zero if any failed.
set -u
cd "$(dirname "$0")/.." || exit 2

failed=""
step() {
	name=$1
	shift
	echo "== $name: $*"
	"$@"
	code=$?
	echo "== $name: exit $code"
	[ "$code" -eq 0 ] || failed="$failed $name"
}

step sync uv sync --locked
step lint uv run ruff check .
step format uv run ruff format --check .
step test uv run pytest

if [ -n "$failed" ]; then
	echo "gate: FAILED:$failed"
	exit 1
fi
echo "gate: all green"
