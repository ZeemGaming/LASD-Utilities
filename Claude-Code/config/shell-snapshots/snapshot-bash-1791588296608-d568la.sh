# Snapshot file
# Unset all aliases to avoid conflicts with functions
unalias -a 2>/dev/null || true
# Shopt
shopt -u autocd
shopt -u assoc_expand_once
shopt -u cdable_vars
shopt -u cdspell
shopt -u checkhash
shopt -u checkjobs
shopt -s checkwinsize
shopt -s cmdhist
shopt -u compat31
shopt -u compat32
shopt -u compat40
shopt -u compat41
shopt -u compat42
shopt -u compat43
shopt -u compat44
shopt -s complete_fullquote
shopt -u direxpand
shopt -u dirspell
shopt -u dotglob
shopt -u execfail
shopt -u expand_aliases
shopt -u extdebug
shopt -s extglob
shopt -s extquote
shopt -u failglob
shopt -s force_fignore
shopt -s globasciiranges
shopt -s globskipdots
shopt -u globstar
shopt -u gnu_errfmt
shopt -u histappend
shopt -u histreedit
shopt -u histverify
shopt -u hostcomplete
shopt -u huponexit
shopt -u inherit_errexit
shopt -s interactive_comments
shopt -u lastpipe
shopt -u lithist
shopt -u localvar_inherit
shopt -u localvar_unset
shopt -s login_shell
shopt -u mailwarn
shopt -u no_empty_cmd_completion
shopt -u nocaseglob
shopt -u nocasematch
shopt -u noexpand_translation
shopt -u nullglob
shopt -s patsub_replacement
shopt -s progcomp
shopt -u progcomp_alias
shopt -s promptvars
shopt -u restricted_shell
shopt -u shift_verbose
shopt -s sourcepath
shopt -u varredir_close
shopt -u xpg_echo
# Functions
eval $'__bp_adjust_histcontrol () \n{ \n    local histcontrol;\n    histcontrol="${HISTCONTROL:-}";\n    histcontrol="${histcontrol//ignorespace}";\n    if [[ "$histcontrol" == *"ignoreboth"* ]]; then\n        histcontrol="ignoredups:${histcontrol//ignoreboth}";\n    fi;\n    export HISTCONTROL="$histcontrol"\n}' > /dev/null 2>&1
eval $'__bp_in_prompt_command () \n{ \n    local prompt_command_array IFS=\'\n;\';\n    read -rd \'\' -a prompt_command_array <<< "${PROMPT_COMMAND[*]:-}";\n    local trimmed_arg;\n    __bp_trim_whitespace trimmed_arg "${1:-}";\n    local command trimmed_command;\n    for command in "${prompt_command_array[@]:-}";\n    do\n        __bp_trim_whitespace trimmed_command "$command";\n        if [[ "$trimmed_command" == "$trimmed_arg" ]]; then\n            return 0;\n        fi;\n    done;\n    return 1\n}' > /dev/null 2>&1
eval $'__bp_install () \n{ \n    if [[ "${PROMPT_COMMAND[*]:-}" == *"__bp_precmd_invoke_cmd"* ]]; then\n        return 1;\n    fi;\n    trap \'__bp_preexec_invoke_exec "$_"\' DEBUG;\n    local prior_trap;\n    prior_trap=$(sed "s/[^\']*\'\\(.*\\)\'[^\']*/\\1/" <<< "${__bp_trap_string:-}");\n    unset __bp_trap_string;\n    if [[ -n "$prior_trap" ]]; then\n        eval \'__bp_original_debug_trap() {\n          \'"$prior_trap"\'\n        }\';\n        preexec_functions+=(__bp_original_debug_trap);\n    fi;\n    __bp_adjust_histcontrol;\n    if [[ -n "${__bp_enable_subshells:-}" ]]; then\n        set -o functrace > /dev/null 2>&1;\n        shopt -s extdebug > /dev/null 2>&1;\n    fi;\n    local existing_prompt_command;\n    existing_prompt_command="${PROMPT_COMMAND:-}";\n    existing_prompt_command="${existing_prompt_command//$__bp_install_string/:}";\n    existing_prompt_command="${existing_prompt_command//\'\n\':\'\n\'/\'\n\'}";\n    existing_prompt_command="${existing_prompt_command//\'\n\':;/\'\n\'}";\n    __bp_sanitize_string existing_prompt_command "$existing_prompt_command";\n    if [[ "${existing_prompt_command:-:}" == ":" ]]; then\n        existing_prompt_command=;\n    fi;\n    PROMPT_COMMAND=\'__bp_precmd_invoke_cmd\';\n    PROMPT_COMMAND+=${existing_prompt_command:+\'\n\'$existing_prompt_command};\n    if (( BASH_VERSINFO[0] > 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] >= 1) )); then\n        PROMPT_COMMAND+=(\'__bp_interactive_mode\');\n    else\n        PROMPT_COMMAND+=\'\n__bp_interactive_mode\';\n    fi;\n    precmd_functions+=(precmd);\n    preexec_functions+=(preexec);\n    __bp_precmd_invoke_cmd;\n    __bp_interactive_mode\n}' > /dev/null 2>&1
eval $'__bp_install_after_session_init () \n{ \n    __bp_require_not_readonly PROMPT_COMMAND HISTCONTROL HISTTIMEFORMAT || return;\n    local sanitized_prompt_command;\n    __bp_sanitize_string sanitized_prompt_command "${PROMPT_COMMAND:-}";\n    if [[ -n "$sanitized_prompt_command" ]]; then\n        PROMPT_COMMAND=${sanitized_prompt_command}\'\n\';\n    fi;\n    PROMPT_COMMAND+=${__bp_install_string}\n}' > /dev/null 2>&1
eval $'__bp_interactive_mode () \n{ \n    __bp_preexec_interactive_mode="on"\n}' > /dev/null 2>&1
eval $'__bp_precmd_invoke_cmd () \n{ \n    __bp_last_ret_value="$?" BP_PIPESTATUS=("${PIPESTATUS[@]}");\n    if (( __bp_inside_precmd > 0 )); then\n        return;\n    fi;\n    local __bp_inside_precmd=1;\n    local precmd_function;\n    for precmd_function in "${precmd_functions[@]}";\n    do\n        if type -t "$precmd_function" > /dev/null; then\n            __bp_set_ret_value "$__bp_last_ret_value" "$__bp_last_argument_prev_command";\n            "$precmd_function";\n        fi;\n    done;\n    __bp_set_ret_value "$__bp_last_ret_value"\n}' > /dev/null 2>&1
eval $'__bp_preexec_invoke_exec () \n{ \n    __bp_last_argument_prev_command="${1:-}";\n    if (( __bp_inside_preexec > 0 )); then\n        return;\n    fi;\n    local __bp_inside_preexec=1;\n    if [[ ! -t 1 && -z "${__bp_delay_install:-}" ]]; then\n        return;\n    fi;\n    if [[ -n "${COMP_LINE:-}" ]]; then\n        return;\n    fi;\n    if [[ -z "${__bp_preexec_interactive_mode:-}" ]]; then\n        return;\n    else\n        if [[ 0 -eq "${BASH_SUBSHELL:-}" ]]; then\n            __bp_preexec_interactive_mode="";\n        fi;\n    fi;\n    if __bp_in_prompt_command "${BASH_COMMAND:-}"; then\n        __bp_preexec_interactive_mode="";\n        return;\n    fi;\n    local this_command;\n    this_command=$(export LC_ALL=C\nHISTTIMEFORMAT=\'\' builtin history 1 | sed \'1 s/^ *[0-9][0-9]*[* ] //\');\n    if [[ -z "$this_command" ]]; then\n        return;\n    fi;\n    local preexec_function;\n    local preexec_function_ret_value;\n    local preexec_ret_value=0;\n    for preexec_function in "${preexec_functions[@]:-}";\n    do\n        if type -t "$preexec_function" > /dev/null; then\n            __bp_set_ret_value "${__bp_last_ret_value:-}";\n            "$preexec_function" "$this_command";\n            preexec_function_ret_value="$?";\n            if [[ "$preexec_function_ret_value" != 0 ]]; then\n                preexec_ret_value="$preexec_function_ret_value";\n            fi;\n        fi;\n    done;\n    __bp_set_ret_value "$preexec_ret_value" "$__bp_last_argument_prev_command"\n}' > /dev/null 2>&1
eval $'__bp_require_not_readonly () \n{ \n    local var;\n    for var in "$@";\n    do\n        if ! ( unset "$var" 2> /dev/null ); then\n            echo "bash-preexec requires write access to ${var}" 1>&2;\n            return 1;\n        fi;\n    done\n}' > /dev/null 2>&1
eval $'__bp_sanitize_string () \n{ \n    local var=${1:?} text=${2:-} sanitized;\n    __bp_trim_whitespace sanitized "$text";\n    sanitized=${sanitized%;};\n    sanitized=${sanitized#;};\n    __bp_trim_whitespace sanitized "$sanitized";\n    printf -v "$var" \'%s\' "$sanitized"\n}' > /dev/null 2>&1
eval $'__bp_set_ret_value () \n{ \n    return ${1:+"$1"}\n}' > /dev/null 2>&1
eval $'__bp_trim_whitespace () \n{ \n    local var=${1:?} text=${2:-};\n    text="${text#"${text%%[![:space:]]*}"}";\n    text="${text%"${text##*[![:space:]]}"}";\n    printf -v "$var" \'%s\' "$text"\n}' > /dev/null 2>&1
eval $'__expand_tilde_by_ref () \n{ \n    [[ -n ${1+set} ]] || return 0;\n    [[ $1 == REPLY ]] || local REPLY;\n    _comp_expand_tilde "${!1-}";\n    [[ $1 == REPLY ]] || printf -v "$1" "$REPLY"\n}' > /dev/null 2>&1
eval $'__load_completion () \n{ \n    _comp_load "$@"\n}' > /dev/null 2>&1
eval $'__ltrim_colon_completions () \n{ \n    _comp_ltrim_colon_completions "$@"\n}' > /dev/null 2>&1
eval $'__parse_options () \n{ \n    local -a _options=();\n    _comp_compgen_help__parse "$1";\n    printf \'%s\\n\' "${_options[@]}"\n}' > /dev/null 2>&1
eval $'command_not_found_handle () \n{ \n    if ! [[ -t 0 ]] || [[ $- != *i* ]]; then\n        echo "bash: $1: command not found" 1>&2;\n        return 127;\n    fi;\n    if [[ -n "${MC_SID-}" ]] || ! [[ -t 1 ]]; then\n        echo "bash: $1: command not found" 1>&2;\n        return 127;\n    fi;\n    wait_till_env_up_to_date;\n    maybe_notify_error;\n    if [[ -f "${SHELL_ENV}" ]] && [[ "${ACTIVE_TS}" -lt "$(/nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/date -r "${SHELL_ENV}" "${TS_FMT}" 2> /dev/null || echo 0)" ]]; then\n        update_environment;\n        "$@";\n        return $?;\n    fi;\n    cmd="$1";\n    nixmodule_installed=;\n    if [[ "$cmd" == @(python|poetry|pip)* ]]; then\n        if /nix/store/2d2jxw071wai1hh6dj0shlgqmlz8zmy1-gnugrep-3.12/bin/grep --silent \'python-3\\.\\(8\\|10\\|11\\|12\\):\' "${REPL_HOME}/.replit" 2> /dev/null; then\n            echo "bash: $1: command not found" 1>&2;\n            return 127;\n        fi;\n        maybe_install_nix_module "Python" "python-3.11";\n        nixmodule_installed="$?";\n    else\n        if [[ "$cmd" == @(node|npm|npx|pnpm|pnpx|yarn)* ]]; then\n            if /nix/store/2d2jxw071wai1hh6dj0shlgqmlz8zmy1-gnugrep-3.12/bin/grep --silent \'(nodejs|bun)-[0-9]*(\\.[0-9])?:\' "${REPL_HOME}/.replit" 2> /dev/null; then\n                echo "bash: $1: command not found" 1>&2;\n                return 127;\n            fi;\n            maybe_install_nix_module "Node" "nodejs-20";\n            nixmodule_installed="$?";\n        else\n            if [[ "$cmd" == @(bun|bunx)* ]]; then\n                if /nix/store/2d2jxw071wai1hh6dj0shlgqmlz8zmy1-gnugrep-3.12/bin/grep --silent \'(nodejs|bun)-[0-9]*(\\.[0-9])?:\' "${REPL_HOME}/.replit" 2> /dev/null; then\n                    echo "bash: $1: command not found" 1>&2;\n                    return 127;\n                fi;\n                maybe_install_nix_module "Bun" "bun-1.1";\n                nixmodule_installed="$?";\n            fi;\n        fi;\n    fi;\n    if [[ -n "${nixmodule_installed}" ]] && [[ "${nixmodule_installed}" -eq 0 ]]; then\n        ( wait_till_env_up_to_date && source "$SHELL_ENV";\n        rc="$?";\n        if [[ "${rc}" -eq 0 ]]; then\n            if /nix/store/05si422r8jpysq89w68yrnx21zr0kiym-which-2.23/bin/which "$1" &> /dev/null; then\n                "$@";\n            else\n                echo "bash: ${cmd}: command not found" 1>&2;\n                exit 127;\n            fi;\n        else\n            exit "$rc";\n        fi );\n        return "$?";\n    fi;\n    toplevel=nixpkgs;\n    mapfile -t choices < <(/nix/store/54csb1plrbhq0ay1faviw6sgcn8dzqvh-replit-nix-locate/bin/nix-locate --minimal --at-root --whole-name "/bin/${cmd}" | /nix/store/vi98k7lyazj240xs82a8990gg5yqcq6g-jq-1.8.2-bin/bin/jq -Rr \'\n            . as $fullAttr\n            | $fullAttr[:$fullAttr | rindex(".")]\n            | select([.] | inside(["busybox", "toybox"]) | not)\n          \' | /nix/store/g6d53gb3cw6x1imfbw5v4088lkdkkc6i-findutils-4.10.0/bin/xargs -n 1 /nix/store/dbhk3fqdmnwi1k37zq97mj9aag9ki0r6-replit-rippkgs/bin/rippkgs --json --exact 2> /dev/null | /nix/store/vi98k7lyazj240xs82a8990gg5yqcq6g-jq-1.8.2-bin/bin/jq -r \'\n            map([.attribute, .version, .description])[]\n            | @tsv\n          \' | /nix/store/5m2ah9zf8zsp7a6y1hv2lfq6gzhv25xh-util-linux-2.42.3-bin/bin/column -ts \'\t\');\n    case "${#choices[@]}" in \n        0)\n            echo "bash: ${cmd}: command not found" 1>&2;\n            return 127\n        ;;\n        1)\n            /nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/cat 1>&2 <<EOF\n${cmd}: command not installed, but was located via Nix.\npackage: ${choices[0]}\nEOF\n\n            case "$(read -r -p "Would you like to run ${cmd} from Nix and add it to your project? [Yn]: " < /dev/tty && echo "${REPLY}")" in \n                "y" | "Y" | "")\n                    selection="${choices[0]}"\n                ;;\n                *)\n                    return 127\n                ;;\n            esac\n        ;;\n        *)\n            /nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/cat 1>&2 <<EOF\n${cmd}: command not installed. Multiple versions of this command were found in Nix.\nSelect one to run (or press Ctrl-C to cancel):\nEOF\n\n            selection="$(printf \'%s\\n\' "${choices[@]}" | /nix/store/nm7sn3g01ndf96ip2pnxpba13mpskwcg-fzy-1.1/bin/fzy)"\n            if [[ "$?" -ne 0 ]]; then\n                return 127;\n            fi\n        ;;\n    esac;\n    echo "$selection";\n    attr="$(echo "$selection" | /nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/cut -d \' \' -f 1)";\n    output="$(/nix/store/54csb1plrbhq0ay1faviw6sgcn8dzqvh-replit-nix-locate/bin/nix-locate --minimal --at-root --whole-name "/bin/${cmd}" | grep "^${attr}\\." | awk -F \'.\' \'{print $NF}\')";\n    storepath="/nix/store/$(/nix/store/dbhk3fqdmnwi1k37zq97mj9aag9ki0r6-replit-rippkgs/bin/rippkgs --json --exact "${attr}" | /nix/store/vi98k7lyazj240xs82a8990gg5yqcq6g-jq-1.8.2-bin/bin/jq -r ".[0].store_paths.${output}")";\n    binpath="${storepath}/bin/${cmd}";\n    channel="${REPLIT_NIX_CHANNEL:-unknown}";\n    if install_nix_package "${attr}" "${storepath}" "${binpath}" "${channel}" "${toplevel}"; then\n        build_status=0;\n    else\n        build_status="$?";\n    fi;\n    if [[ "${build_status}" -eq 86 ]]; then\n        return "${build_status}";\n    fi;\n    if [[ "${build_status}" -eq 0 ]]; then\n        if [[ ! -f "${REPLIT_NIX}" ]]; then\n            echo "Adding ${attr} to .replit" 1>&2;\n            pkgs="$(echo \'[{"op":"get","path":"nix/packages"}]\' | /nix/store/qnfa9kf79m6b3f6swivch233w5gqxly9-toml-editor-0.0.0-7452ace/bin/toml-editor --path "${DOT_REPLIT}" 2> /dev/null | /nix/store/vi98k7lyazj240xs82a8990gg5yqcq6g-jq-1.8.2-bin/bin/jq -c \'.results[]\')";\n            if [[ "$pkgs" == "null" ]]; then\n                pkgs="[]";\n            fi;\n            if ! echo "$pkgs" | /nix/store/vi98k7lyazj240xs82a8990gg5yqcq6g-jq-1.8.2-bin/bin/jq -e --arg attr "$attr" \'index($attr) != null\' &> /dev/null; then\n                pkgs="$(echo "$pkgs" | /nix/store/vi98k7lyazj240xs82a8990gg5yqcq6g-jq-1.8.2-bin/bin/jq -c --arg attr "$attr" \'. + [$attr]\')";\n            fi;\n            echo \'[{"op":"add","path":"nix/packages","value":"\'"$(echo "$pkgs" | /nix/store/sazlbkll5x2dw1kkx0l2a8b74w6rgdxm-gnused-4.10/bin/sed \'s/"/\\\\"/g\')"\'"}]\' | /nix/store/qnfa9kf79m6b3f6swivch233w5gqxly9-toml-editor-0.0.0-7452ace/bin/toml-editor --path "${DOT_REPLIT}" &> /dev/null;\n        else\n            echo "Adding ${attr} to replit.nix" 1>&2;\n            /nix/store/f2p0cxqbamnrqv8kr6qj3hg4izld7j71-nix-editor-0.0.0-9472fbd/bin/nix-editor --add "pkgs.${attr}" --human --path "${REPLIT_NIX}";\n        fi;\n        if [[ -f "${binpath}" ]]; then\n            shift 1;\n            "${binpath}" "${@}";\n        else\n            nix-shell -p "$attr" --run "$(printf \'%q \' "$@")";\n        fi;\n        return $?;\n    else\n        /nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/cat 1>&2 <<EOF\nFailed to install ${toplevel}.${attr}.\n$cmd: command not found\nEOF\n\n        return 127;\n    fi\n}' > /dev/null 2>&1
eval $'dequote () \n{ \n    local REPLY;\n    _comp_dequote "$1";\n    local rc=$?;\n    printf %s "$REPLY";\n    return $rc\n}' > /dev/null 2>&1
eval $'ensure_gated_profile_path () \n{ \n    if [ "${REPLIT_PID1_FLAG_NO_SNIX:-}" = "1" ] && [ "${REPLIT_MACHINE:-}" != "1" ] && [ -n "${HOME:-}" ]; then\n        if [ "${HOME}" != "/home/runner" ]; then\n            PATH=$(_replit_normalize_profile_path);\n            export PATH;\n            return;\n        fi;\n        case ":${PATH}:" in \n            *":${HOME}/.nix-profile/bin:"*)\n\n            ;;\n            *)\n                export PATH="${HOME}/.nix-profile/bin:${PATH}"\n            ;;\n        esac;\n    fi\n}' > /dev/null 2>&1
eval $'ensure_or_build_nix_package () \n{ \n    if realise_nix_package "$2"; then\n        return 0;\n    else\n        realise_status="$?";\n    fi;\n    if [[ "${realise_status}" -eq 86 ]]; then\n        return "${realise_status}";\n    fi;\n    echo "Package-store install failed; building $1 locally" 1>&2;\n    if nix-build --no-out-link -A "$1" "<$4>"; then\n        build_status=0;\n    else\n        build_status="$?";\n    fi;\n    if [[ "${build_status}" -eq 0 ]]; then\n        realise_nix_package "$2";\n        realise_status="$?";\n        if [[ "${realise_status}" -eq 86 ]]; then\n            return "${realise_status}";\n        else\n            if [[ "${realise_status}" -ne 0 ]]; then\n                echo "Warning: $1 may need to be rebuilt after a reboot" 1>&2;\n            fi;\n        fi;\n    fi;\n    return "${build_status}"\n}' > /dev/null 2>&1
eval $'env_has_pending_build () \n{ \n    if [[ "${REPLIT_NIX}" -nt "${SHELL_ENV}" ]] || [[ "${DOT_REPLIT}" -nt "${SHELL_ENV}" ]] || [[ "${MODULES_STAMP}" -nt "${SHELL_ENV}" ]]; then\n        if [[ -f "${SHELL_ENV_ERROR}" ]]; then\n            if [[ "${REPLIT_NIX}" -nt "${SHELL_ENV_ERROR}" ]] || [[ "${DOT_REPLIT}" -nt "${SHELL_ENV_ERROR}" ]] || [[ "${MODULES_STAMP}" -nt "${SHELL_ENV_ERROR}" ]]; then\n                return 0;\n            else\n                return 1;\n            fi;\n        else\n            return 0;\n        fi;\n    else\n        return 1;\n    fi\n}' > /dev/null 2>&1
eval $'install_nix_package () \n{ \n    if [[ "${REPLIT_PID1_FLAG_NO_SNIX:-}" == "1" ]]; then\n        ensure_or_build_nix_package "$1" "$2" "$4" "$5";\n        return $?;\n    fi;\n    [[ -f "$3" ]] || nix-build --no-out-link -A "$1" "<$5>"\n}' > /dev/null 2>&1
eval $'maybe_install_nix_module () \n{ \n    TOOL_NAME="$1";\n    MODULE_ID="$2";\n    yes_or_no "Install Replit\'s ${TOOL_NAME} tools" || return 1;\n    result="$(/nix/store/2pfh66qmng1460mydmghrcfngckrgp0c-curl-8.22.0-bin/bin/curl --silent --header "Content-Type: application/json" --request POST --data "{\\"ids\\":[\\"${MODULE_ID}\\"]}" localhost:8283/nixmodule/add)";\n    if [[ "${result}" != \'{"status":"ok"}\' ]]; then\n        echo -e "\\e[0;33m${__REPLIT_LOGO} Failed to add tools, check whether your .replit file is properly formatted.\\e[0m" 1>&2;\n        return 1;\n    fi\n}' > /dev/null 2>&1
eval $'maybe_notify_error () \n{ \n    if [[ -f "${SHELL_ENV_ERROR}" && "${ACTIVE_TS}" -lt "$(/nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/date -r "${SHELL_ENV_ERROR}" "${TS_FMT}" 2> /dev/null || echo 0)" ]]; then\n        echo -e "\\e[0;33m${__REPLIT_LOGO} Failed to compile new environment.\\e[0m" 1>&2;\n        echo -e "\\e[0;33m${__REPLIT_LOGO} Run \\`cat ${SHELL_ENV_ERROR}\\` to display the error.\\e[0m" 1>&2;\n        ACTIVE_TS="$(/nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/date -r "${SHELL_ENV_ERROR}" "${TS_FMT}" 2> /dev/null || echo 0)";\n    fi\n}' > /dev/null 2>&1
eval $'pbcopy () \n{ \n    printf "\\e]52;c;%s\\a" "$(/nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/base64 -w0)"\n}' > /dev/null 2>&1
eval $'precmd () \n{ \n    _replit_pwd_tracking\n}' > /dev/null 2>&1
eval $'preexec () \n{ \n    escaped="$(echo "$1" | /nix/store/sazlbkll5x2dw1kkx0l2a8b74w6rgdxm-gnused-4.10/bin/sed \'s/"/\\\\"/g\')";\n    _replit_command_tracking "${escaped}";\n    _replit_pwd_tracking;\n    _ftcs_command_executed\n}' > /dev/null 2>&1
eval $'prompt_command () \n{ \n    history -a;\n    if [[ -f "${SHELL_ENV}" ]] && [[ "${ACTIVE_TS}" -lt "$(/nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/date -r "${SHELL_ENV}" "${TS_FMT}" 2> /dev/null || echo 0)" ]]; then\n        update_environment;\n    fi\n}' > /dev/null 2>&1
eval $'quote () \n{ \n    local quoted=${1//\\\'/\\\'\\\\\\\'\\\'};\n    printf "\'%s\'" "$quoted"\n}' > /dev/null 2>&1
eval $'quote_readline () \n{ \n    local REPLY;\n    _comp_quote_compgen "$1";\n    printf %s "$REPLY"\n}' > /dev/null 2>&1
eval $'realise_nix_package () \n{ \n    realise_storepath="$1";\n    realise_store_prefix="/nix/store/";\n    if [[ "${realise_storepath}" != "${realise_store_prefix}"* ]]; then\n        return 1;\n    fi;\n    realise_store_name="${realise_storepath#"${realise_store_prefix}"}";\n    case "${realise_store_name}" in \n        */*)\n            return 1\n        ;;\n    esac;\n    realise_store_hash="${realise_store_name%%-*}";\n    case "${realise_store_name}" in \n        "${realise_store_hash}"-*)\n\n        ;;\n        *)\n            return 1\n        ;;\n    esac;\n    realise_store_suffix="${realise_store_name#"${realise_store_hash}"-}";\n    if [[ "${#realise_store_hash}" -ne 32 ]] || [[ -z "${realise_store_suffix}" ]]; then\n        return 1;\n    fi;\n    case "${realise_store_hash}" in \n        *[!0-9a-df-np-sv-z]*)\n            return 1\n        ;;\n    esac;\n    realise_root_dir="/nix/var/nix/gcroots/replit-package-store";\n    if [[ "${REPLIT_PID1_FLAG_NO_SNIX:-}" == "1" && "${REPLIT_MACHINE:-}" != "1" ]]; then\n        realise_root_dir="/nix/var/nix-local/gcroots/replit-package-store";\n    fi;\n    realise_root_indirect=;\n    if ! /nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/mkdir -p -- "${realise_root_dir}" 2> /dev/null || [[ ! -w "${realise_root_dir}" ]]; then\n        if [[ -z "${HOME:-}" || "${HOME}" != /* ]]; then\n            return 1;\n        fi;\n        realise_root_dir="${HOME}/.local/state/nix/gcroots/replit-package-store";\n        if ! /nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/mkdir -p -- "${realise_root_dir}" || [[ ! -w "${realise_root_dir}" ]]; then\n            return 1;\n        fi;\n        realise_root_indirect=1;\n    fi;\n    realise_root="${realise_root_dir}/command-not-found-${realise_store_hash}";\n    nix-store --realise "${realise_storepath}" ${realise_root_indirect:+--indirect} --add-root "${realise_root}" --option require-sigs true --option fallback false --option builders "" --option max-jobs 0 > /dev/null;\n    realise_status="$?";\n    if [[ "${realise_status}" -ne 0 ]]; then\n        return "${realise_status}";\n    fi;\n    if ! nix store verify --recursive --no-trust "${realise_storepath}"; then\n        echo "Integrity verification failed for ${realise_storepath}; repair or reset the package store before retrying" 1>&2;\n        return 86;\n    fi\n}' > /dev/null 2>&1
eval $'update_environment () \n{ \n    ACTIVE_TS="$(/nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/date -r "${SHELL_ENV}" "${TS_FMT}")";\n    source "${SHELL_ENV}" || exit;\n    ensure_gated_profile_path\n}' > /dev/null 2>&1
eval $'wait_till_env_up_to_date () \n{ \n    if env_has_pending_build; then\n        echo -ne "\\e[33m${__REPLIT_LOGO} Waiting for environment to update.";\n        /nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/sleep 1;\n        while env_has_pending_build; do\n            echo -n ".";\n            /nix/store/833invqvsk7anqnqrki5hqncrcllj825-coreutils-9.11/bin/sleep 1;\n        done;\n        echo -ne "\\e[0m\\r";\n    fi\n}' > /dev/null 2>&1
eval $'yes_or_no () \n{ \n    while true; do\n        printf "\\e[33m$* [y/n] %s \\e[0m" "${__REPLIT_LOGO}";\n        read -rp "" yn;\n        case $yn in \n            [Yy]*)\n                return 0\n            ;;\n            [Nn]*)\n                echo "Aborted";\n                return 1\n            ;;\n        esac;\n    done\n}' > /dev/null 2>&1
# Shell Options
set -o braceexpand
set -o hashall
set -o interactive-comments
set -o monitor
set -o onecmd
shopt -s expand_aliases
# Aliases
alias -- egrep='egrep --color=auto'
alias -- fgrep='fgrep --color=auto'
alias -- grep='/nix/store/2d2jxw071wai1hh6dj0shlgqmlz8zmy1-gnugrep-3.12/bin/grep --color=auto'
alias -- l='ls -CF'
alias -- la='ls -A'
alias -- ll='ls -alF'
alias -- ls='ls --color=auto'
# Check for rg availability
if ! hash rg 2>/dev/null && ! typeset -f rg >/dev/null 2>&1; then
  function rg {
  local _cc_bin="${CLAUDE_CODE_EXECPATH:-}"
  [[ -x $_cc_bin ]] || _cc_bin=/home/runner/.local/bin/claude
  if [[ ! -x $_cc_bin ]]; then command rg ${1+"$@"}; return; fi
  if [[ -n ${ZSH_VERSION:-} ]]; then
    ARGV0=rg "$_cc_bin" ${1+"$@"}
  elif [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]] || [[ "$OSTYPE" == "win32" ]]; then
    ARGV0=rg "$_cc_bin" ${1+"$@"}
  else
    (exec -a rg "$_cc_bin" ${1+"$@"})
  fi
}
fi
# Shadow find/grep with embedded bfs/ugrep
unalias find 2>/dev/null || true
unalias grep 2>/dev/null || true
function find {
  local _cc_bin="${CLAUDE_CODE_EXECPATH:-}"
  [[ -x $_cc_bin ]] || _cc_bin=/home/runner/.local/bin/claude
  if [[ ! -x $_cc_bin ]]; then command find ${1+"$@"}; return; fi
  if [[ -n ${ZSH_VERSION:-} ]]; then
    ARGV0=bfs "$_cc_bin" -S dfs -regextype findutils-default ${1+"$@"}
  elif [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]] || [[ "$OSTYPE" == "win32" ]]; then
    ARGV0=bfs "$_cc_bin" -S dfs -regextype findutils-default ${1+"$@"}
  else
    (exec -a bfs "$_cc_bin" -S dfs -regextype findutils-default ${1+"$@"})
  fi
}
function grep {
  local _cc_a
  for _cc_a in ${1+"$@"}; do
    case "$_cc_a" in -*-filter*|-*-pager*|-*-view*|-*-format-open*|-*-config*|---*|-@*|-*-save-config*|-[Zz]*|-[!-]*[Zz]*|--null|--null-data) command grep ${1+"$@"}; return ;; esac
  done
  local _cc_bin="${CLAUDE_CODE_EXECPATH:-}"
  [[ -x $_cc_bin ]] || _cc_bin=/home/runner/.local/bin/claude
  if [[ ! -x $_cc_bin ]]; then command grep ${1+"$@"}; return; fi
  if [[ -n ${ZSH_VERSION:-} ]]; then
    ARGV0=ugrep "$_cc_bin" -G --ignore-files --hidden -I --exclude-dir=.git --exclude-dir=.svn --exclude-dir=.hg --exclude-dir=.bzr --exclude-dir=.jj --exclude-dir=.sl ${1+"$@"}
  elif [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]] || [[ "$OSTYPE" == "win32" ]]; then
    ARGV0=ugrep "$_cc_bin" -G --ignore-files --hidden -I --exclude-dir=.git --exclude-dir=.svn --exclude-dir=.hg --exclude-dir=.bzr --exclude-dir=.jj --exclude-dir=.sl ${1+"$@"}
  else
    (exec -a ugrep "$_cc_bin" -G --ignore-files --hidden -I --exclude-dir=.git --exclude-dir=.svn --exclude-dir=.hg --exclude-dir=.bzr --exclude-dir=.jj --exclude-dir=.sl ${1+"$@"})
  fi
}
# Shadow pkill to refuse patterns matching the CLI process
unalias pkill 2>/dev/null || true
function pkill {
  if [ -n "${CLAUDE_PID:-}" ] && [ -r "/proc/${CLAUDE_PID}/comm" ]; then
    local _cc_skip="" _cc_a
    local -a _cc_probe=()
    for _cc_a in ${1+"$@"}; do
      if [ -n "$_cc_skip" ]; then _cc_skip=""; continue; fi
      case "$_cc_a" in
        --signal) _cc_skip=1 ;;
        --signal=*|-e|--echo) ;;
        -[0-9]*) ;;
        -[PUGOF]?*) _cc_probe+=("$_cc_a") ;;
        -[ABCDEFGHIJKLMNOPQRSTUVWXYZ][ABCDEFGHIJKLMNOPQRSTUVWXYZ0-9]*) ;;
        *) _cc_probe+=("$_cc_a") ;;
      esac
    done
    if command pgrep ${_cc_probe[@]+"${_cc_probe[@]}"} 2>/dev/null | command grep -qx "${CLAUDE_PID}"; then
      printf 'pkill: refusing to run — this pattern matches the Claude CLI process (PID %s). Narrow the pattern, or target your own children with `pkill -P $$ ...`.\n' "${CLAUDE_PID}" >&2
      return 1
    fi
  fi
  command pkill ${1+"$@"}
}
export PATH=/home/runner/workspace/.pythonlibs/bin:/nix/store/flbj8bq2vznkcwss7sm0ky8rd0k6kar7-python-wrapped-0.1.0/bin:/nix/store/xwg0ddq9mjf6ibwdvp93jsp0cf51z3xr-pip-wrapper/bin:/nix/store/ypy3l3k428kc1kmcw090wlbxi8vj1m8l-poetry-wrapper/bin:/nix/store/6m2322jq0rkfdnv6cm3dq8437djbfv1l-uv-0.9.5/bin:/nix/store/b1kq183rmmiv453fn6f8bfmi1drxvxba-npx/bin:/home/runner/workspace/.config/npm/node_global/bin:/home/runner/workspace/node_modules/.bin:/nix/store/1lagpgadaybvs1n2312gysg2phjk89y8-nodejs-20.20.0-wrapped/bin:/nix/store/1xk3mgscq548ypyrgm2n5kwdii92w9ql-bun-1.3.6/bin:/nix/store/61lr9izijvg30pcribjdxgjxvh3bysp4-pnpm-10.26.1/bin:/nix/store/23078nfww258q1vjxbmyak0svvxcvj4s-yarn-1.22.22/bin:/nix/store/8sa75mbvbn3kxicggyyjggmkigvzddks-prettier-3.6.2/bin:/nix/store/qxsmrf8cb7f9cd4z4rczj1dh2qf55nz6-pid1/bin:/nix/store/xv7zv44cnz26d6ich0xargi6wqik75fy-replit-runtime-path/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/repl/tools/bin
