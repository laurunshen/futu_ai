#!/usr/bin/env bash
set -euo pipefail

cd /home/ec2-user/opend/current

args=(
  "-ip=127.0.0.1"
  "-api_port=11111"
)

if [[ -n "${FUTU_OPEND_LOGIN_ACCOUNT:-}" ]]; then
  args+=("-login_account=${FUTU_OPEND_LOGIN_ACCOUNT}")
fi

if [[ -n "${FUTU_OPEND_LOGIN_PWD_MD5:-}" ]]; then
  args+=("-login_pwd_md5=${FUTU_OPEND_LOGIN_PWD_MD5}")
elif [[ -n "${FUTU_OPEND_LOGIN_PWD:-}" ]]; then
  args+=("-login_pwd=${FUTU_OPEND_LOGIN_PWD}")
fi

exec ./FutuOpenD "${args[@]}"
