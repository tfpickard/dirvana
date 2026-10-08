# dirvana zsh adapter.
#
# Implements the hook side of docs/hook-protocol.md: it records observations into the shadow
# tree under $_dirvana_root, captures path arguments as cross-directory edges, runs recon on
# directory entry, and enforces the privacy floor before anything is written.
#
# Hot-path rules: no forks in preexec/precmd, no held file descriptors, no Python, no network.
# chpwd may fork at most one disowned background recon job.
#
# Calling convention: only entry points (hooks, widgets, load) run `emulate -L zsh` and
# `setopt extended_glob`; internal helpers inherit those options. A zsh function call costs
# microseconds, so the hot path also inlines cheap pre-checks before calling helpers.

zmodload zsh/datetime zsh/system 2>/dev/null || return 0
zmodload -F zsh/stat b:zstat 2>/dev/null || return 0
zmodload -F zsh/files b:zf_mkdir b:zf_mv b:zf_rm 2>/dev/null || return 0
autoload -Uz add-zsh-hook add-zle-hook-widget

# The program name. Must match dirvana._meta.NAME (tests assert this).
typeset -g _DIRVANA_NAME=dirvana

typeset -g _dirvana_zsh_dir=${${(%):-%x}:A:h}

# --- configuration ------------------------------------------------------------------------

# _dirvana_env SUFFIX -> REPLY: value of ${NAME}_SUFFIX (e.g. DIRVANA_ROOT).
_dirvana_env() {
  local var=${(U)_DIRVANA_NAME}_$1
  REPLY=${(P)var}
}

# _dirvana_xdg VAR FALLBACK -> REPLY: an XDG base dir; relative values are invalid per spec.
_dirvana_xdg() {
  local v=${(P)1}
  [[ $v == /* ]] && REPLY=$v || REPLY=$HOME/$2
}

_dirvana_init_dirs() {
  _dirvana_env ROOT
  if [[ -n $REPLY ]]; then
    typeset -g _dirvana_root=$REPLY
  else
    _dirvana_xdg XDG_DATA_HOME .local/share; typeset -g _dirvana_root=$REPLY/$_DIRVANA_NAME
  fi
  _dirvana_env CONFIG_DIR
  if [[ -n $REPLY ]]; then
    typeset -g _dirvana_cfg=$REPLY
  else
    _dirvana_xdg XDG_CONFIG_HOME .config; typeset -g _dirvana_cfg=$REPLY/$_DIRVANA_NAME
  fi
  _dirvana_env STATE_DIR
  if [[ -n $REPLY ]]; then
    typeset -g _dirvana_state=$REPLY
  else
    _dirvana_xdg XDG_STATE_HOME .local/state; typeset -g _dirvana_state=$REPLY/$_DIRVANA_NAME
  fi
}

# --- session state ------------------------------------------------------------------------

typeset -g  _dirvana_mid= _dirvana_sid= _dirvana_host_json=
typeset -gi _dirvana_seq=0 _dirvana_paused=0 _dirvana_incognito=0 _dirvana_skip=0
typeset -gi _dirvana_pending=0 _dirvana_errors=0 _dirvana_trunc=0
typeset -g  _dirvana_t0= _dirvana_cmd= _dirvana_cmd_cwd= _dirvana_cmd_node= _dirvana_paths=
typeset -g  _dirvana_pwd_phys= _dirvana_pwd_shadow=
typeset -gi _dirvana_pwd_ignored=0
typeset -gA _dirvana_phys _dirvana_recon_checked _dirvana_gitdir
typeset -ga _dirvana_ring _dirvana_ign_pats _dirvana_ign_vals _dirvana_ignored_words
typeset -ga _dirvana_policy_files
typeset -g  _dirvana_policy_stamp= _dirvana_ign_any=
typeset -gi _dirvana_ring_max=20

# --- small utilities ----------------------------------------------------------------------

# _dirvana_jstr STRING -> REPLY: STRING as a JSON string literal (quotes included).
_dirvana_jstr() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  if [[ $s == *[[:cntrl:]]* ]]; then
    s=${s//$'\n'/\\n}
    s=${s//$'\t'/\\t}
    s=${s//$'\r'/\\r}
    s=${s//(#m)[[:cntrl:]]/\\u${(l:4::0:)$(( [##16] #MATCH ))}}
  fi
  REPLY=\"$s\"
}

# Cheap necessary condition for any redaction rule to fire (callers test it inline).
typeset -g _dirvana_redact_trigger='(#i)*(auth|bearer|key|secret|token|passw|://|gh[pousr]_|github_pat|sk-|xox|akia|aiza|glpat|eyj|-----begin)*'

# _dirvana_redact STRING -> REPLY. Mirrors dirvana/redact.py; see tests/vectors/redact.json.
# Each rule is gated on a substring it cannot match without; the gates change nothing else.
_dirvana_redact() {
  local s=$1 m='<redacted>'
  # Quoted or bare value. Quotes are bracketed: a backslash inside [...] would be a member.
  local v='(["][^"]#["]|['\''][^'\'']#['\'']|[^[:space:]"'\'']##)'
  if [[ $s != ${~_dirvana_redact_trigger} ]]; then
    REPLY=$s
    return
  fi
  if [[ $s == *-----BEGIN* ]]; then
    s=${(S)s//-----BEGIN[A-Z0-9 ]#PRIVATE KEY-----*-----END[A-Z0-9 ]#PRIVATE KEY-----/<redacted:private-key>}
    s=${s//-----BEGIN[A-Z0-9 ]#PRIVATE KEY-----*/<redacted:private-key>}
  fi
  [[ $s == (#i)*authorization* ]] &&
    s=${s//(#b)((#i)authorization[ $'\t']#:[ $'\t']#)((#i)(bearer|basic|token|digest)[ $'\t']##|)[^[:space:]\"\']##/${match[1]}${match[2]}$m}
  [[ $s == (#i)*bearer* ]] &&
    s=${s//(#b)((#i)bearer[ $'\t']##)[A-Za-z0-9._~+\/=-]##/${match[1]}$m}
  if [[ $s == (#i)*(key|secret|token|passw)* ]]; then
    [[ $s == *--* ]] &&
      s=${s//(#b)((#i)--[A-Za-z0-9-]#(key|secret|token|passw(or|)d)[A-Za-z0-9-]#)(=|[ $'\t']##)${~v}/${match[1]}${match[4]}$m}
    [[ $s == *=* ]] &&
      s=${s//(#b)((#i)[A-Za-z0-9_]#(key|secret|token|passw(or|)d)[A-Za-z0-9_]#=)${~v}/${match[1]}$m}
  fi
  [[ $s == *://*@* ]] &&
    s=${s//(#b)([A-Za-z][A-Za-z0-9+.-]#:\/\/)[^[:space:]\/@:]##:[^[:space:]\/@]##@/${match[1]}$m@}
  [[ $s == *github_pat_* ]] && s=${s//github_pat_[A-Za-z0-9_](#c20,)/$m}
  [[ $s == *gh[pousr]_* ]] && s=${s//gh[pousr]_[A-Za-z0-9](#c20,)/$m}
  if [[ $s == *sk-* ]]; then
    s=${s//sk-ant-[A-Za-z0-9_-](#c20,)/$m}
    s=${s//sk-[A-Za-z0-9_-](#c20,)/$m}
  fi
  [[ $s == *xox[abpr]-* ]] && s=${s//xox[abpr]-[A-Za-z0-9-](#c10,)/$m}
  [[ $s == *AKIA* ]] && s=${s//AKIA[A-Z0-9](#c16)/$m}
  [[ $s == *AIza* ]] && s=${s//AIza[A-Za-z0-9_-](#c35)/$m}
  [[ $s == *glpat-* ]] && s=${s//glpat-[A-Za-z0-9_-](#c20,)/$m}
  [[ $s == *eyJ* ]] && s=${s//eyJ[A-Za-z0-9_-](#c10,).[A-Za-z0-9_-](#c10,).[A-Za-z0-9_-](#c10,)/$m}
  REPLY=$s
}

# _dirvana_shadow ABS -> REPLY: the shadow directory for an absolute physical path.
# Components starting with % get the % doubled; a single leading % is reserved for metadata.
_dirvana_shadow() {
  local p=$1
  if [[ $p != *%* ]]; then
    REPLY=$_dirvana_root/system${p%/}
    return
  fi
  local c out=
  for c in ${(s:/:)p}; do
    [[ $c == %* ]] && c=%$c
    out+=/$c
  done
  REPLY=$_dirvana_root/system$out
}

_dirvana_mkdirs() {
  if [[ ! -d $_dirvana_root ]]; then
    zf_mkdir -p -- ${_dirvana_root:h} 2>/dev/null
    zf_mkdir -m 700 -- $_dirvana_root 2>/dev/null
  fi
  [[ -d $1 ]] || zf_mkdir -p -m 700 -- $1 2>/dev/null
}

# _dirvana_append SHADOW_DIR LINE: append one record with a single write(2).
# The file is opened per record on purpose: compaction renames %obs.jsonl, and a held fd
# would keep writing into a segment that has already been sealed.
_dirvana_append() {
  local fd
  if ! sysopen -a -o cloexec,creat -m 600 -u fd $1/%obs.jsonl 2>/dev/null; then
    _dirvana_mkdirs $1
    if ! sysopen -a -o cloexec,creat -m 600 -u fd $1/%obs.jsonl 2>/dev/null; then
      (( _dirvana_errors++ ))
      return 1
    fi
  fi
  syswrite -o $fd -- "$2"$'\n' 2>/dev/null || (( _dirvana_errors++ ))
  exec {fd}>&-
}

_dirvana_machine_id() {
  local f=$_dirvana_state/machine-id id=
  [[ -r $f ]] && IFS= read -r id < $f
  if [[ $id != [0-9a-f](#c32) ]]; then
    # First run on this machine: one fork, never repeated.
    id=$(od -An -N16 -tx1 /dev/urandom 2>/dev/null)
    id=${id//[^0-9a-f]/}
    zf_mkdir -p -m 700 -- $_dirvana_state 2>/dev/null
    print -r -- $id > $f.$$ 2>/dev/null && zf_mv -f -- $f.$$ $f 2>/dev/null
    # Another shell may have won the race; adopt whatever landed.
    [[ -r $f ]] && IFS= read -r id < $f
  fi
  _dirvana_mid=${id[1,12]}
}

# --- policy: ignore rules -----------------------------------------------------------------
#
# Only `ignore` matters to the hook: it must be decided before anything is stored. Everything
# else in the policy files is resolved by the Python side. Pattern semantics (shared with
# dirvana/policy.py, see tests/vectors/ignore.json):
#   ~/x  -> under $HOME;  /x -> anchored at the file's base;  a/b -> anchored at the base;
#   name -> any depth below the base;  * ? [..] stay within a component;  ** spans components;
#   a trailing /** is redundant: a rule matching X always covers X and everything below it.

# _dirvana_globcomp COMPONENT -> REPLY: one path component of a policy glob as a zsh pattern.
_dirvana_globcomp() {
  local s=$1 r= c rest cls
  local -i i=1 n=${#1}
  while (( i <= n )); do
    c=$s[i]
    case $c in
      ('*') r+='[^/]#' ;;
      ('?') r+='[^/]' ;;
      ('[')
        rest=$s[i+1,-1]
        if [[ $rest == *']'* ]]; then
          cls=${rest%%']'*}
          (( i += ${#cls} + 1 ))
          [[ $cls == '!'* ]] && cls="^${cls#!}"
          r+="[$cls]"
        else
          r+='\['
        fi
        ;;
      (*) r+=${(b)c} ;;
    esac
    (( i++ ))
  done
  REPLY=$r
}

# _dirvana_glob2pat BASE GLOB -> REPLY: zsh pattern matching the subtree(s) GLOB names.
_dirvana_glob2pat() {
  local base=$1 pat=$2 comp out
  pat=${pat%/}
  [[ $pat == */'**' ]] && pat=${pat%/'**'}
  [[ $pat == '**' ]] && pat=
  if [[ $pat == '~' || $pat == '~/'* ]]; then
    base=$HOME
    pat=${${pat#'~'}#/}
  elif [[ $pat == /* ]]; then
    pat=${pat#/}
  elif [[ -n $pat && $pat != */* ]]; then
    pat="**/$pat"
  fi
  base=${base%/}
  out=${(b)base}
  for comp in ${(s:/:)pat}; do
    if [[ $comp == '**' ]]; then
      out+='(/*|)'
    else
      _dirvana_globcomp $comp
      out+=/$REPLY
    fi
  done
  REPLY="$out(/*|)"
}

# _dirvana_policy_file FILE BASE: append the file's ignore rules. A policy.d file must start
# with `@root DIR`; BASE is empty for those and / for the global file.
_dirvana_policy_file() {
  local f=$1 base=$2 line pat tok
  local -i val
  local -a w
  while IFS= read -r line || [[ -n $line ]]; do
    line=${line%%[[:space:]]##\#*}
    [[ $line == [[:space:]]#(\#*|) ]] && continue
    w=(${=line})
    if [[ $w[1] == @root ]]; then
      base=${w[2]}
      [[ $base == '~' || $base == '~/'* ]] && base=$HOME${base#'~'}
      continue
    fi
    [[ -n $base ]] || continue
    pat=$w[1]
    val=-1
    for tok in $w[2,-1]; do
      case $tok in
        (ignore|ignore=true) val=0 ;;
        (ignore=false) val=1 ;;
      esac
    done
    (( val < 0 )) && continue
    _dirvana_glob2pat $base $pat
    _dirvana_ign_pats+=("$REPLY")
    _dirvana_ign_vals+=($val)
  done < $f
}

_dirvana_policy_stamp_now() {
  local f s=
  local -a m
  for f in $_dirvana_cfg/policy $_dirvana_cfg/policy.d $_dirvana_policy_files; do
    if zstat -A m +mtime -- $f 2>/dev/null; then s+="$m[1]:"; else s+="-:"; fi
  done
  REPLY=$s
}

_dirvana_policy_load() {
  local LC_COLLATE=C p f line depth
  local -a keyed
  _dirvana_ign_pats=()
  _dirvana_ign_vals=()
  # Built-in floor: secret stores, plus dirvana's own directories.
  for p in $HOME/.ssh $HOME/.gnupg $HOME/.password-store ${PASSWORD_STORE_DIR:-} \
           $HOME/.local/share/gopass $HOME/.aws $HOME/.config/gcloud $HOME/.kube \
           $HOME/.docker $_dirvana_root $_dirvana_cfg $_dirvana_state; do
    [[ -n $p ]] || continue
    _dirvana_ign_pats+=("${(b)p%/}(/*|)")
    _dirvana_ign_vals+=(0)
  done
  [[ -f $_dirvana_cfg/policy ]] && _dirvana_policy_file $_dirvana_cfg/policy /
  # policy.d: shallower @root first, then file name (byte order), as dirvana/policy.py does.
  for f in $_dirvana_cfg/policy.d/*.policy(N.); do
    p=
    while IFS= read -r line || [[ -n $line ]]; do
      [[ $line == [[:space:]]#(\#*|) ]] && continue
      [[ $line == @root[[:space:]]* ]] && p=${${line#@root[[:space:]]##}%%[[:space:]]#}
      break
    done < $f
    [[ -n $p ]] || continue
    [[ $p == '~' || $p == '~/'* ]] && p=$HOME${p#'~'}
    depth=${#${(s:/:)p}}
    keyed+=("${(l:4::0:)depth}/$f")
  done
  _dirvana_policy_files=()
  for f in ${(o)keyed}; do
    f=${f#*/}
    _dirvana_policy_files+=($f)
    _dirvana_policy_file $f ''
  done
  # Common case: no ignore=false overrides, so "ignored" is one alternation, compiled once
  # per check instead of once per rule.
  if (( ${_dirvana_ign_vals[(I)1]} == 0 )); then
    _dirvana_ign_any="(${(j:|:)_dirvana_ign_pats})"
  else
    _dirvana_ign_any=
  fi
  _dirvana_policy_stamp_now
  _dirvana_policy_stamp=$REPLY
}

_dirvana_policy_maybe_reload() {
  _dirvana_policy_stamp_now
  [[ $REPLY == $_dirvana_policy_stamp ]] || _dirvana_policy_load
}

# _dirvana_ignored ABS: status 0 if ABS is ignored (last matching rule wins).
_dirvana_ignored() {
  if [[ -n $_dirvana_ign_any ]]; then
    [[ $1 == ${~_dirvana_ign_any} ]]
    return
  fi
  local p=$1
  local -i i r=1
  for (( i = 1; i <= $#_dirvana_ign_pats; i++ )); do
    [[ $p == ${~_dirvana_ign_pats[i]} ]] && r=$_dirvana_ign_vals[i]
  done
  return r
}

# --- path-argument capture ----------------------------------------------------------------

# _dirvana_emit ARG VERB RAW: resolve one candidate argument; record an edge if it names an
# existing path outside the cwd. Runs in the dynamic scope of _dirvana_capture.
_dirvana_emit() {
  local arg=$1 verb=$2 raw=$3 abs node p user key
  local -i glob=0
  # Bare names only count once a `cd` in the same line moved the virtual cwd elsewhere.
  [[ $arg == */* || $arg == '~'* || $arg == .. || $vphys != $_dirvana_pwd_phys ]] || return 0
  [[ $raw == *[\$\`]* ]] && return 0
  (( budget > 0 )) || return 0
  (( budget-- ))
  if [[ $raw == *[*?\[]* && $raw != [\"\']* ]]; then
    glob=1
    arg=${arg%%[*?\[]*}
    [[ $arg == */* ]] || return 0
    arg=${arg%/*}
    [[ -n $arg ]] || arg=/
  fi
  if [[ $arg == '~' || $arg == '~/'* ]]; then
    arg=$HOME${arg#'~'}
  elif [[ $arg == '~'* ]]; then
    user=${${arg#'~'}%%/*}
    [[ -n ${userdirs[$user]} ]] || return 0
    arg=${userdirs[$user]}${arg#'~'$user}
  fi
  if [[ $arg == /* ]]; then abs=${arg:a}; else abs=${vphys%/}/$arg; abs=${abs:a}; fi
  if [[ -d $abs ]]; then
    node=$abs
  elif [[ -e $abs ]]; then
    node=${abs:h}
  elif [[ $verb == (copy|move)-to && -d ${abs:h} ]]; then
    node=${abs:h}
  else
    return 0
  fi
  p=${_dirvana_phys[$node]}
  if [[ -z $p ]]; then
    p=${node:A}
    _dirvana_phys[$node]=$p
  fi
  # Ignore first: an ignored subtree inside the cwd (~/.ssh from ~) must still be scrubbed.
  if _dirvana_ignored $p; then
    _dirvana_ignored_words+=("$raw")
    return 0
  fi
  [[ $p == $_dirvana_pwd_phys || $p == ${_dirvana_pwd_phys%/}/* ]] && return 0
  [[ $node == $abs ]] && abs=$p || abs=${p%/}/${abs:t}
  key="$verb|$abs"
  [[ -n ${seen[$key]} ]] && return 0
  seen[$key]=1
  local ja=$1 jb jn
  [[ $ja == ${~_dirvana_redact_trigger} ]] && { _dirvana_redact "$ja"; ja=$REPLY; }
  if [[ $ja$abs$p == *[\"\\[:cntrl:]]* ]]; then
    _dirvana_jstr "$ja"; ja=$REPLY
    _dirvana_jstr "$abs"; jb=$REPLY
    _dirvana_jstr "$p"; jn=$REPLY
  else
    ja=\"$ja\" jb=\"$abs\" jn=\"$p\"
  fi
  local entry="{\"verb\":\"$verb\",\"arg\":$ja,\"abs\":$jb,\"node\":$jn"
  (( glob )) && entry+=',"glob":true'
  _dirvana_paths+=${_dirvana_paths:+,}$entry'}'
}

# _dirvana_simple: process the simple command in $cmdw (dynamic scope of _dirvana_capture).
_dirvana_simple() {
  local -a a raw args rargs
  a=("${(@Q)cmdw}")
  raw=("${cmdw[@]}")
  local -i i=1 n=$#a j
  # Peel redirections off first; their targets are candidates too.
  if (( ! ${raw[(I)[0-9\<\>\&]*]} )); then
    args=("${a[@]}")
    rargs=("${raw[@]}")
    i=n+1
  fi
  while (( i <= n )); do
    if [[ $raw[i] != [0-9\<\>\&]* ]]; then
      args+=("$a[i]"); rargs+=("$raw[i]")
      (( i++ ))
      continue
    fi
    case $raw[i] in
      ([0-9]#(\<\<|\<\<-|\<\<\<|\<\&|\>\&|\&\>\&))
        (( i += 2 )) ;;
      ([0-9]#\<)
        (( i < n )) && _dirvana_emit "$a[i+1]" read "$raw[i+1]"
        (( i += 2 )) ;;
      ([0-9]#(\>|\>\>|\>\||\>\!|\>\>\||\>\>\!|\<\>)|\&\>|\&\>\>|\&\>\||\&\>\!)
        (( i < n )) && _dirvana_emit "$a[i+1]" copy-to "$raw[i+1]"
        (( i += 2 )) ;;
      (*)
        args+=("$a[i]"); rargs+=("$raw[i]")
        (( i++ )) ;;
    esac
  done
  n=$#args
  j=1
  # Skip assignments, precommand modifiers and reserved words.
  while (( j <= n )); do
    case $args[j] in
      ([A-Za-z_][A-Za-z0-9_]#=*) (( j++ )) ;;
      (sudo|doas)
        (( j++ ))
        while [[ $args[j] == -* ]]; do
          [[ $args[j] == -[ugCDhprtU] ]] && (( j++ ))
          (( j++ ))
        done ;;
      (env)
        (( j++ ))
        while [[ $args[j] == -* || $args[j] == [A-Za-z_][A-Za-z0-9_]#=* ]]; do
          [[ $args[j] == -[uSC] ]] && (( j++ ))
          (( j++ ))
        done ;;
      (nice)
        (( j++ ))
        [[ $args[j] == -n ]] && (( j += 2 ))
        [[ $args[j] == -<-> ]] && (( j++ )) ;;
      (command|builtin|exec|nocorrect|noglob|time|nohup|if|then|else|elif|while|until|do|\!|coproc)
        (( j++ ))
        while [[ $args[j] == -[a-zA-Z] ]]; do (( j++ )); done ;;
      (*) break ;;
    esac
  done
  (( j <= n )) || return 0
  local cmd=$args[j] name=${args[j]:t} verb=ref mode= tgt
  case $cmd in
    (for|case|select|function|fi|done|esac|'[['|'[['*|'(('*|'['|test|'}'|'{') return 0 ;;
  esac
  local -a av rav
  av=("${(@)args[j+1,-1]}")
  rav=("${(@)rargs[j+1,-1]}")
  [[ $cmd == */* ]] && _dirvana_emit "$cmd" run "$rargs[j]"

  case $name in
    (cd|pushd)
      tgt=
      for (( i = 1; i <= $#av; i++ )); do
        [[ $av[i] == -* && $av[i] != - ]] && continue
        tgt=$av[i]
        break
      done
      [[ -z $tgt ]] && tgt=$HOME
      [[ $tgt == - || $tgt == [+-]<-> ]] && return 0
      [[ $tgt == '~' || $tgt == '~/'* ]] && tgt=$HOME${tgt#'~'}
      local newlog
      if [[ $tgt == /* ]]; then newlog=${tgt:a}; else newlog=${vlog%/}/$tgt; newlog=${newlog:a}; fi
      [[ -d $newlog ]] || return 0
      if _dirvana_ignored ${newlog:A}; then
        [[ -n $rav[i] ]] && _dirvana_ignored_words+=("$rav[i]")
        vlog=$newlog
        vphys=${newlog:A}
        return 0
      fi
      if (( depth > 0 )); then
        local savephys=$vphys
        vphys=$vlog
        _dirvana_emit "$newlog" cd "$tgt"
        vphys=$savephys
      fi
      vlog=$newlog
      vphys=${newlog:A}
      return 0 ;;
    (popd) return 0 ;;
    (ls|tree|eza|exa|lsd|du|dir|vdir) verb=list ;;
    (cat|less|more|bat|batcat|head|tail|view|file|wc|stat|xxd|hexdump|od|strings|jq|yq|sha256sum|md5sum) verb=read ;;
    (grep|egrep|fgrep|rg|ag|ack) verb=search mode=pattern ;;
    (find|fd|fdfind) verb=search ;;
    (cp|rsync|install|ln|scp) mode=copy ;;
    (mv) mode=move ;;
    (diff|cmp|vimdiff|nvimdiff|colordiff|sdiff|delta|difft|meld|kdiff3|icdiff|wdiff) verb=diff ;;
    (nvim|vim|vi|gvim|emacs|emacsclient|code|hx|helix|kak|micro|nano|ed|${EDITOR:t}|${VISUAL:t})
      verb=edit
      for tgt in $av; do
        case $tgt in
          (-d) verb=diff ;;
          (-R|-M) verb=read ;;
        esac
      done ;;
    (make|gmake|ninja|cmake|meson|just) verb=run mode=dirflag ;;
    (git)
      verb=ref mode=dirflag
      [[ ${av[(I)diff]} -gt 0 && ${av[(I)--no-index]} -gt 0 ]] && verb=diff ;;
    (tar|bsdtar) verb=ref mode=dirflag ;;
  esac

  local -a pos rpos
  local -i haspat=0
  i=1
  if (( ! ${av[(I)-*]} )); then
    pos=("${av[@]}")
    rpos=("${rav[@]}")
    i=$#av+1
  fi
  for (( ; i <= $#av; i++ )); do
    (( budget > 0 )) || break
    tgt=$av[i]
    if [[ $tgt != -* ]]; then
      pos+=("$tgt"); rpos+=("$rav[i]")
      continue
    fi
    case $tgt in
      (--)
        pos+=("${(@)av[i+1,-1]}"); rpos+=("${(@)rav[i+1,-1]}")
        break ;;
      (-C|-S|-B|--directory|--source-dir|--build-dir|-f|--file|--makefile)
        if [[ $mode == dirflag ]]; then
          _dirvana_emit "$av[i+1]" run "$rav[i+1]"
          (( i++ ))
        elif [[ $mode == pattern && $tgt == -f ]]; then
          haspat=1
          _dirvana_emit "$av[i+1]" read "$rav[i+1]"
          (( i++ ))
        fi ;;
      (-t|--target-directory)
        if [[ $mode == (copy|move) ]]; then
          _dirvana_emit "$av[i+1]" $mode-to "$rav[i+1]"
          mode+=-t
          (( i++ ))
        fi ;;
      (--target-directory=*)
        if [[ $mode == (copy|move) ]]; then
          _dirvana_emit "${tgt#*=}" $mode-to "$rav[i]"
          mode+=-t
        fi ;;
      (-e|--regexp|--regexp=*) haspat=1; [[ $tgt == -e || $tgt == --regexp ]] && (( i++ )) ;;
      (--*=*)
        [[ $mode == dirflag ]] && _dirvana_emit "${tgt#*=}" run "${rav[i]#*=}" ||
          _dirvana_emit "${tgt#*=}" $verb "${rav[i]#*=}" ;;
      (-?*) ;;
      (*) pos+=("$tgt"); rpos+=("$rav[i]") ;;
    esac
  done

  (( budget > 0 )) || return 0
  case $mode in
    (copy|move)
      for (( i = 1; i < $#pos && budget > 0; i++ )); do _dirvana_emit "$pos[i]" $mode-from "$rpos[i]"; done
      (( $#pos >= 2 )) && _dirvana_emit "$pos[-1]" $mode-to "$rpos[-1]"
      (( $#pos == 1 )) && _dirvana_emit "$pos[1]" $mode-from "$rpos[1]" ;;
    (copy-t|move-t)
      for (( i = 1; i <= $#pos && budget > 0; i++ )); do _dirvana_emit "$pos[i]" ${mode%-t}-from "$rpos[i]"; done ;;
    (pattern)
      (( haspat )) || { pos[1]=(); rpos[1]=(); }
      for (( i = 1; i <= $#pos && budget > 0; i++ )); do _dirvana_emit "$pos[i]" $verb "$rpos[i]"; done ;;
    (*)
      for (( i = 1; i <= $#pos && budget > 0; i++ )); do _dirvana_emit "$pos[i]" $verb "$rpos[i]"; done ;;
  esac
  return 0
}

# _dirvana_capture TEXT: lex TEXT and collect path arguments outside the cwd into
# $_dirvana_paths (comma-separated JSON objects) and $_dirvana_ignored_words.
_dirvana_capture() {
  emulate -L zsh
  setopt extended_glob
  _dirvana_paths=
  _dirvana_ignored_words=()
  local text=${1[1,1024]}
  [[ $text == *[/~]* || $text == *..* ]] || return 0
  local -a words cmdw cwdstack
  local -A seen
  local vphys=$_dirvana_pwd_phys vlog=$PWD w
  local -i depth=0 budget=8
  words=(${(Z+C+)text})
  # One simple command (no separators anywhere): skip the per-word walk.
  if (( ! ${words[(I)[\|\&\;\(\)\{\}]*]} )); then
    cmdw=($words)
    _dirvana_simple
    return 0
  fi
  for w in $words ';'; do
    # Most words are plain; skip the separator case unless the first character could be one.
    if [[ $w != [\|\&\;\(\)\{\}]* ]]; then
      cmdw+=("$w")
      continue
    fi
    case $w in
      ('|'|'||'|'&&'|';'|'&'|'|&'|'&|'|'&!'|';;'|';&'|';|'|'{'|'}')
        (( $#cmdw )) && _dirvana_simple
        cmdw=() ;;
      ('(')
        (( $#cmdw )) && _dirvana_simple
        cmdw=()
        cwdstack+=("$vphys"$'\0'"$vlog")
        (( depth++ )) ;;
      (')')
        (( $#cmdw )) && _dirvana_simple
        cmdw=()
        if (( $#cwdstack )); then
          vphys=${cwdstack[-1]%%$'\0'*}
          vlog=${cwdstack[-1]#*$'\0'}
          cwdstack[-1]=()
          (( depth-- ))
        fi ;;
      (*) cmdw+=("$w") ;;
    esac
  done
}

# --- recon --------------------------------------------------------------------------------

# _dirvana_find_gitdir DIR -> REPLY (empty if not in a repo). Cached per session.
_dirvana_find_gitdir() {
  local d=$1 cur=$1 l
  if (( ${+_dirvana_gitdir[$d]} )); then
    REPLY=$_dirvana_gitdir[$d]
    [[ -n $REPLY ]]
    return
  fi
  REPLY=
  while true; do
    if [[ -d $cur/.git ]]; then
      REPLY=$cur/.git
      break
    elif [[ -f $cur/.git ]]; then
      l=
      IFS= read -r l < $cur/.git
      l=${l#gitdir: }
      [[ $l == /* ]] || l=$cur/$l
      REPLY=${l:a}
      break
    fi
    [[ $cur == / ]] && break
    cur=${cur:h}
  done
  _dirvana_gitdir[$d]=$REPLY
  [[ -n $REPLY ]]
}

# Decide (without forking) whether the cwd's recon is stale; if so, refresh it in the
# background. Set DIRVANA_RECON_SYNC=1 to run it in the foreground (tests).
_dirvana_recon_gate() {
  local phys=$_dirvana_pwd_phys shadow=$_dirvana_pwd_shadow f
  local -i now=$EPOCHSECONDS rmt dmt=0 g=0
  local -a m
  (( now - ${_dirvana_recon_checked[$phys]:-0} < 60 )) && return 0
  _dirvana_recon_checked[$phys]=$now
  if zstat -A m +mtime -- $shadow/%recon.json 2>/dev/null; then
    rmt=$m[1]
    if (( now - rmt < 86400 )); then
      zstat -A m +mtime -- $phys 2>/dev/null && dmt=$m[1]
      if _dirvana_find_gitdir $phys; then
        for f in $REPLY/HEAD $REPLY/config; do
          zstat -A m +mtime -- $f 2>/dev/null && (( m[1] > g )) && g=$m[1]
        done
      fi
      (( dmt < rmt && g < rmt )) && return 0
    fi
  fi
  _dirvana_env RECON_SYNC
  if [[ -n $REPLY ]]; then
    _dirvana_recon_job $phys $shadow
  else
    _dirvana_recon_job $phys $shadow </dev/null >/dev/null 2>&1 &!
  fi
}

# --- hooks --------------------------------------------------------------------------------

_dirvana_enter() {
  _dirvana_pwd_phys=${PWD:A}
  _dirvana_shadow $_dirvana_pwd_phys
  _dirvana_pwd_shadow=$REPLY
  if _dirvana_ignored $_dirvana_pwd_phys; then _dirvana_pwd_ignored=1; else _dirvana_pwd_ignored=0; fi
}

_dirvana_line_finish() {
  if [[ $BUFFER == [[:space:]]* ]]; then _dirvana_skip=1; else _dirvana_skip=0; fi
}

_dirvana_preexec() {
  emulate -L zsh
  setopt extended_glob
  _dirvana_pending=0
  (( _dirvana_paused || _dirvana_skip || _dirvana_pwd_ignored )) && return 0
  # zle-line-finish already caught leading spaces; this covers input that bypassed zle.
  [[ $1 == [[:space:]]* ]] && return 0
  [[ -e $_dirvana_root/var/paused ]] && return 0
  _dirvana_t0=$EPOCHREALTIME
  local typed=${1:-$3} text=${3:-$1} w
  # Pattern matching is linear in length, so bound everything first. 1 KiB is far beyond a
  # real command; longer (pasted) lines are stored truncated and flagged.
  _dirvana_trunc=0
  if (( ${#typed} > 1024 )); then
    typed=${typed[1,1024]}
    _dirvana_trunc=1
  fi
  text=${text[1,1024]}
  if [[ $text == *[/~]* || $text == *..* ]]; then
    _dirvana_capture "$text"
  else
    _dirvana_paths=
    _dirvana_ignored_words=()
  fi
  for w in $_dirvana_ignored_words; do
    # A substituted parameter matches literally in a // pattern.
    [[ -n $w ]] && typed=${typed//$w/<ignored>}
  done
  if [[ $typed == ${~_dirvana_redact_trigger} ]]; then
    _dirvana_redact "$typed"
    typed=$REPLY
  fi
  _dirvana_cmd=$typed
  _dirvana_cmd_cwd=$_dirvana_pwd_phys
  _dirvana_cmd_node=$_dirvana_pwd_shadow
  _dirvana_pending=1
}

_dirvana_precmd() {
  local -i st=$?
  emulate -L zsh
  setopt extended_glob
  if (( _dirvana_pending )); then
    _dirvana_pending=0
    local jc jw
    typeset -F 3 dur=$(( EPOCHREALTIME - _dirvana_t0 ))
    typeset -F 3 t=$_dirvana_t0
    (( _dirvana_seq++ ))
    if [[ $_dirvana_cmd$_dirvana_cmd_cwd == *[\"\\[:cntrl:]]* ]]; then
      _dirvana_jstr "$_dirvana_cmd"; jc=$REPLY
      _dirvana_jstr "$_dirvana_cmd_cwd"; jw=$REPLY
    else
      jc=\"$_dirvana_cmd\" jw=\"$_dirvana_cmd_cwd\"
    fi
    local line="{\"v\":1,\"id\":\"$_dirvana_sid:$_dirvana_seq\",\"k\":\"cmd\",\"t\":$t"
    line+=",\"mid\":\"$_dirvana_mid\",\"host\":$_dirvana_host_json,\"sid\":\"$_dirvana_sid\""
    line+=",\"cwd\":$jw,\"cmd\":$jc,\"st\":$st,\"dur\":$dur,\"paths\":[$_dirvana_paths]"
    (( _dirvana_trunc )) && line+=',"trunc":true'
    _dirvana_append $_dirvana_cmd_node "$line}"
    if (( ! _dirvana_incognito )); then
      _dirvana_ring+=("$_dirvana_cmd"$'\x1f'"$_dirvana_cmd_cwd")
      (( $#_dirvana_ring > _dirvana_ring_max )) && shift _dirvana_ring
    fi
  fi
  _dirvana_skip=0
}

_dirvana_chpwd() {
  emulate -L zsh
  setopt extended_glob
  local old=$_dirvana_pwd_phys old_shadow=$_dirvana_pwd_shadow
  local -i old_ign=$_dirvana_pwd_ignored
  _dirvana_policy_maybe_reload
  _dirvana_enter
  (( _dirvana_paused || _dirvana_skip )) && return 0
  [[ -e $_dirvana_root/var/paused ]] && return 0
  if [[ -n $old && $old != $_dirvana_pwd_phys ]] && (( ! old_ign && ! _dirvana_pwd_ignored )); then
    local jf jt
    typeset -F 3 t=$EPOCHREALTIME
    (( _dirvana_seq++ ))
    _dirvana_jstr "$old"; jf=$REPLY
    _dirvana_jstr "$_dirvana_pwd_phys"; jt=$REPLY
    _dirvana_append $old_shadow "{\"v\":1,\"id\":\"$_dirvana_sid:$_dirvana_seq\",\"k\":\"cd\",\"t\":$t,\"mid\":\"$_dirvana_mid\",\"host\":$_dirvana_host_json,\"sid\":\"$_dirvana_sid\",\"from\":$jf,\"to\":$jt}"
  fi
  (( _dirvana_pwd_ignored )) || _dirvana_recon_gate
}

# --- user-facing session controls ---------------------------------------------------------

# Session-scoped commands must run in this shell; everything else goes to the CLI.
dirvana() {
  case $1 in
    (pause|resume|incognito)
      if [[ $2 == --global ]]; then
        command dirvana "$@"
        return
      fi
      case $1 in
        (pause) _dirvana_paused=1; print -u2 -- "$_DIRVANA_NAME: paused for this session" ;;
        (resume) _dirvana_paused=0 _dirvana_incognito=0; print -u2 -- "$_DIRVANA_NAME: recording" ;;
        (incognito)
          _dirvana_paused=1 _dirvana_incognito=1
          _dirvana_ring=()
          print -u2 -- "$_DIRVANA_NAME: incognito: not recording, no session history sent" ;;
      esac ;;
    (*) command dirvana "$@" ;;
  esac
}

dirvana_plugin_unload() {
  add-zsh-hook -d preexec _dirvana_preexec
  add-zsh-hook -d precmd _dirvana_precmd
  add-zsh-hook -d chpwd _dirvana_chpwd
  (( ${+functions[add-zle-hook-widget]} )) && add-zle-hook-widget -d line-finish _dirvana_line_finish 2>/dev/null
  fpath=(${fpath:#$_dirvana_zsh_dir/functions})
  unfunction -m '_dirvana_*' 2>/dev/null
  unfunction dirvana dirvana_plugin_unload 2>/dev/null
  unset -m '_dirvana_*' 2>/dev/null
}

# --- load ---------------------------------------------------------------------------------

# Load runs under the user's options (this file is sourced), so do the work in a function.
_dirvana_load() {
  emulate -L zsh
  setopt extended_glob
  fpath=($_dirvana_zsh_dir/functions $fpath)
  autoload -Uz _dirvana_recon_job
  _dirvana_init_dirs
  _dirvana_machine_id
  _dirvana_sid=$_dirvana_mid:$$:$EPOCHSECONDS
  _dirvana_jstr "$HOST"
  _dirvana_host_json=$REPLY
  _dirvana_policy_load
  _dirvana_enter
  [[ -o interactive ]] || return 0
  add-zsh-hook preexec _dirvana_preexec
  add-zsh-hook precmd _dirvana_precmd
  add-zsh-hook chpwd _dirvana_chpwd
  # add-zle-hook-widget silently does nothing unless zsh/zle is already loaded, and in
  # .zshrc it usually is not yet.
  zmodload zsh/zle 2>/dev/null && add-zle-hook-widget line-finish _dirvana_line_finish
  # A deferred load (zsh-defer) misses the first chpwd; recon the starting directory now.
  (( _dirvana_pwd_ignored )) || _dirvana_recon_gate
}

_dirvana_load
