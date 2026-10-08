#!/usr/bin/env zsh
# In-process hook latency benchmark.
#
# Sources the adapter into a clean `zsh -f`, then drives the real hook functions exactly as an
# interactive shell would (preexec -> precmd, and chpwd on directory changes) over a corpus of
# realistic commands, timing each call with $EPOCHREALTIME. Prints p50/p95/p99/max in µs.
#
# usage: hook-bench.zsh ITERATIONS
# The caller provides a hermetic environment (HOME, DIRVANA_ROOT, ...); see bench/run.sh.

emulate -L zsh
setopt extended_glob
zmodload zsh/datetime zsh/mathfunc

local -i iters=${1:-2000}
local here=${0:A:h}
source $here/../shell/zsh/dirvana.zsh

# A small fixture tree that makes the corpus resolve to real edges.
local base=$HOME/src
local a=$base/acme-build-config-2026q3-variant-a b=$base/acme-build-config-2026q3-variant-b
mkdir -p $a/src $b/config $base/c
print x > $b/f; print x > $a/f; print x > $b/config/flags.mk

local -a corpus=(
  'ls ../acme-build-config-2026q3-variant-b'
  'cp ../acme-build-config-2026q3-variant-b/f .'
  'diff ../acme-build-config-2026q3-variant-b/f f'
  'nvim ../acme-build-config-2026q3-variant-b/config/flags.mk'
  'make -C ../acme-build-config-2026q3-variant-b test'
  'git status'
  'cat ../acme-build-config-2026q3-variant-b/config/flags.mk | grep FOO | wc -l'
  'rsync -av ../acme-build-config-2026q3-variant-b/ ../c/'
  'echo hello world'
  'for f in *.c; do echo $f; done'
  'export API_TOKEN=abc123'
  "curl -H 'Authorization: Bearer xyz' https://example.com/api"
  'docker run --rm -v "$PWD:/src" -w /src img make'
  "find ../acme-build-config-2026q3-variant-b -name '*.mk' -exec grep -l X {} +"
  'ls'
  'vim -d ../acme-build-config-2026q3-variant-b/f f && git add -p'
)
# One pathological line, measured separately: 4 KiB, 40 path-like arguments.
local long='echo'
local -i k
for (( k = 1; k <= 40; k++ )); do long+=" ../acme-build-config-2026q3-variant-b/f$k"; done
long+=" ${(l:3000::x:)}"

typeset -ga lat_preexec lat_precmd lat_chpwd lat_chpwd_new lat_long
local -F t0
local cmd
cd $a
_dirvana_enter

local -i i
for (( i = 1; i <= iters; i++ )); do
  cmd=$corpus[$(( (i - 1) % $#corpus + 1 ))]
  _dirvana_skip=0
  t0=$EPOCHREALTIME
  _dirvana_preexec "$cmd" "$cmd" "$cmd"
  lat_preexec+=$(( int((EPOCHREALTIME - t0) * 1e6) ))
  t0=$EPOCHREALTIME
  _dirvana_precmd
  lat_precmd+=$(( int((EPOCHREALTIME - t0) * 1e6) ))
  if (( i % 20 == 0 )); then
    _dirvana_skip=0
    t0=$EPOCHREALTIME
    _dirvana_preexec "$long" "$long" "$long"
    _dirvana_precmd
    lat_long+=$(( int((EPOCHREALTIME - t0) * 1e6) ))
  fi
  if (( i % 8 == 0 )); then
    # Bounce between two known directories (the common case: recon gate says fresh).
    builtin cd -q $b
    t0=$EPOCHREALTIME
    _dirvana_chpwd
    lat_chpwd+=$(( int((EPOCHREALTIME - t0) * 1e6) ))
    builtin cd -q $a
    _dirvana_chpwd
  fi
  if (( i % 50 == 0 )); then
    # First visit to a brand-new directory: recon gate is stale and forks a job.
    mkdir -p $base/new/$i
    builtin cd -q $base/new/$i
    t0=$EPOCHREALTIME
    _dirvana_chpwd
    lat_chpwd_new+=$(( int((EPOCHREALTIME - t0) * 1e6) ))
    builtin cd -q $a
    _dirvana_chpwd
  fi
done

stats() {
  local name=$1; shift
  local -a s=(${(on)@})
  local -i n=$#s
  pct() { print -r -- ${s[$(( (n * $1 + 99) / 100 ))]} }
  printf '%-22s n=%-5d p50=%-6s p95=%-6s p99=%-6s max=%s\n' \
    $name $n $(pct 50) $(pct 95) $(pct 99) $s[-1]
}
print "hook latency in µs (zsh $ZSH_VERSION, $(uname -sm))"
stats preexec $lat_preexec
stats precmd $lat_precmd
stats chpwd $lat_chpwd
stats chpwd-first-visit $lat_chpwd_new
stats 4KiB-line-pre+precmd $lat_long
wait
