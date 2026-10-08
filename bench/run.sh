#!/usr/bin/env bash
# Hook latency + interactive startup benchmarks in a hermetic temp HOME.
# usage: bench/run.sh [ITERATIONS]     (needs zsh; hyperfine for the startup numbers)
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
iters=${1:-2000}
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT

export HOME=$tmp/home ZDOTDIR=$tmp/zdot
export DIRVANA_ROOT=$tmp/root DIRVANA_CONFIG_DIR=$tmp/cfg DIRVANA_STATE_DIR=$tmp/state
mkdir -p "$HOME" "$ZDOTDIR" "$tmp/baseline"

echo "## Hook latency"
zsh -f "$here/hook-bench.zsh" "$iters"

echo
echo "## Interactive startup (zsh -i -c exit)"
if ! command -v hyperfine >/dev/null; then
  echo "hyperfine not installed (brew install hyperfine / paru -S hyperfine); skipping"
  exit 0
fi
cp "$here/zshrc.baseline" "$tmp/baseline/.zshrc"
{ cat "$here/zshrc.baseline"; echo "source $repo/dirvana.plugin.zsh"; } > "$ZDOTDIR/.zshrc"
# A zcompiled copy, as Antidote (zcompile style) or `make zcompile` would produce.
mkdir -p "$tmp/compiled" "$tmp/zdot-compiled"
cp -R "$repo/dirvana.plugin.zsh" "$repo/shell" "$tmp/compiled/"
for f in "$tmp/compiled/dirvana.plugin.zsh" "$tmp/compiled/shell/zsh/dirvana.zsh"; do zsh -fc "zcompile $f"; done
{ cat "$here/zshrc.baseline"; echo "source $tmp/compiled/dirvana.plugin.zsh"; } > "$tmp/zdot-compiled/.zshrc"
# Make sure first-run work (machine-id) is not counted.
zsh -i -c exit
hyperfine -N --warmup 10 --runs 300 --export-json "$tmp/startup.json" \
  -n baseline "env ZDOTDIR=$tmp/baseline zsh -i -c exit" \
  -n source "env ZDOTDIR=$ZDOTDIR zsh -i -c exit" \
  -n zcompiled "env ZDOTDIR=$tmp/zdot-compiled zsh -i -c exit" >/dev/null 2>&1
python3 - "$tmp/startup.json" <<'PY'
import json, statistics, sys
r = {x["command"]: x for x in json.load(open(sys.argv[1]))["results"]}
med = lambda x: statistics.median(x["times"]) * 1000
b = med(r["baseline"])
print(f"{'baseline':<22} median {b:6.2f} ms")
for name in ("source", "zcompiled"):
    print(f"{'+ dirvana (' + name + ')':<22} median {med(r[name]):6.2f} ms   added {med(r[name]) - b:5.2f} ms")
PY
