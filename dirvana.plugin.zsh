# dirvana: per-directory context graph for the shell.
# Entry point for Antidote and plain `source`. See README.md for installation.
source "${${ZERO:-${${0:#$ZSH_ARGZERO}:-${(%):-%N}}}:A:h}/shell/zsh/dirvana.zsh"
