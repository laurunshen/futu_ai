#!/usr/bin/env bash
set -euo pipefail

cd /home/ec2-user/opend/current

runtime_cfg="/home/ec2-user/opend/FutuOpenD.runtime.xml"

python3 - "$runtime_cfg" <<'PY'
import html
import os
import pathlib
import re
import sys

runtime_path = pathlib.Path(sys.argv[1])
account = os.environ.get("FUTU_OPEND_LOGIN_ACCOUNT", "").strip()
pwd_md5 = os.environ.get("FUTU_OPEND_LOGIN_PWD_MD5", "").strip()
pwd = os.environ.get("FUTU_OPEND_LOGIN_PWD", "")

if not account:
    raise SystemExit("FUTU_OPEND_LOGIN_ACCOUNT is required")
if not (pwd_md5 or pwd):
    raise SystemExit("FUTU_OPEND_LOGIN_PWD_MD5 or FUTU_OPEND_LOGIN_PWD is required")

xml = pathlib.Path("FutuOpenD.xml").read_text(encoding="utf-8-sig")

def set_tag(text: str, tag: str, value: str) -> str:
    escaped = html.escape(value, quote=False)
    pattern = re.compile(rf"(?s)(?:<!--\s*)?<{tag}>.*?</{tag}>(?:\s*-->)?")
    replacement = f"<{tag}>{escaped}</{tag}>"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    return text.replace("</futu_opend>", f"\t<{tag}>{escaped}</{tag}>\n</futu_opend>")

def delete_tag(text: str, tag: str) -> str:
    pattern = re.compile(rf"(?s)\s*(?:<!--\s*)?<{tag}>.*?</{tag}>(?:\s*-->)?")
    return pattern.sub("", text, count=1)

xml = set_tag(xml, "ip", "127.0.0.1")
xml = set_tag(xml, "api_port", "11111")
xml = set_tag(xml, "login_account", account)

if pwd_md5:
    xml = set_tag(xml, "login_pwd_md5", pwd_md5)
    xml = delete_tag(xml, "login_pwd")
else:
    xml = set_tag(xml, "login_pwd", pwd)
    xml = delete_tag(xml, "login_pwd_md5")

runtime_path.write_text(xml, encoding="utf-8")
runtime_path.chmod(0o600)
PY

args=(
  "-cfg_file=${runtime_cfg}"
  "-console=0"
)

exec ./FutuOpenD "${args[@]}"
