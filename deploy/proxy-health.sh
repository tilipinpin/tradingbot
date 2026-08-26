#!/bin/zsh

# Shared boot-time proxy checks for desktop apps and the live trading bot.
typeset -gr SHARED_PROXY_HTTP="http://127.0.0.1:7897"
typeset -gr SHARED_PROXY_SOCKS="socks5h://127.0.0.1:7898"

proxy_export_launchd_environment() {
  /bin/launchctl setenv HTTP_PROXY "$SHARED_PROXY_HTTP"
  /bin/launchctl setenv HTTPS_PROXY "$SHARED_PROXY_HTTP"
  /bin/launchctl setenv ALL_PROXY "$SHARED_PROXY_SOCKS"
  /bin/launchctl setenv NO_PROXY "localhost,127.0.0.1,::1,*.local"
}

proxy_wait_ready() {
  local wait_seconds="${1:-90}"
  local require_system_proxy="${2:-0}"
  local deadline=$(( $(/bin/date +%s) + wait_seconds ))

  while (( $(/bin/date +%s) < deadline )); do
    if /usr/bin/nc -z 127.0.0.1 7897 >/dev/null 2>&1; then
      if (( require_system_proxy == 0 )) || \
        /usr/sbin/scutil --proxy | /usr/bin/grep -q 'HTTPEnable : 1'; then
        return 0
      fi
    fi
    /bin/sleep 1
  done
  return 1
}

polymarket_proxy_is_eligible() {
  local geo=""

  /usr/bin/curl --silent --show-error --fail --max-time 5 \
    --proxy "$SHARED_PROXY_HTTP" \
    https://clob.polymarket.com/time >/dev/null 2>&1 || return 1

  geo=$(/usr/bin/curl --silent --show-error --fail --max-time 5 \
    --proxy "$SHARED_PROXY_HTTP" \
    https://polymarket.com/api/geoblock 2>/dev/null) || return 1

  if [[ "$geo" == *'"blocked":false'* || "$geo" == *'"blocked": false'* ]]; then
    return 0
  fi
  if [[ "$geo" == *'"blocked":true'* || "$geo" == *'"blocked": true'* ]]; then
    return 2
  fi
  return 1
}
