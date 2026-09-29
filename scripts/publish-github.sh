#!/bin/bash
# 发布到 GitHub：创建/使用仓库 → 推送 → 建立 Release 并上传 .fpk
#
# 用法：
#   GH_TOKEN=xxx ./scripts/publish-github.sh --repo <owner>/<name> [--private]
#   ./scripts/publish-github.sh --repo <owner>/<name> --token-file /path/to/token
#
# 说明：
# * 令牌只从环境变量或文件读取，**不会**被回显，也**不会**写进 .git/config。
# * 推送可通过 HTTP header 临时注入令牌，不落盘。
# * 仓库内含 .github/workflows/，因此令牌必须带 **workflow** 作用域，
#   否则 GitHub 会拒绝推送（classic PAT 需要 repo + workflow）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO=""
VISIBILITY="public"
TOKEN_FILE=""
TAG="v$(sed -n 's/^version[[:space:]]*=[[:space:]]*//p' "$ROOT/manifest" | tr -d '[:space:]')"
FPK="$ROOT/dist/AirPlay2-DLNA-Bridge-${TAG#v}.fpk"

while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --private) VISIBILITY="private"; shift ;;
        --public) VISIBILITY="public"; shift ;;
        --token-file) TOKEN_FILE="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

[ -n "$REPO" ] || { echo "必须指定 --repo <owner>/<name>" >&2; exit 2; }

if [ -z "${GH_TOKEN:-}" ]; then
    for candidate in "$TOKEN_FILE" "$HOME/.gh_token" /root/.gh_token /tmp/gh_token; do
        [ -n "$candidate" ] && [ -r "$candidate" ] || continue
        GH_TOKEN="$(tr -d '[:space:]' < "$candidate")"
        [ -n "$GH_TOKEN" ] && echo "    从 $candidate 读取令牌" && break
    done
    if [ -z "${GH_TOKEN:-}" ]; then
        echo "缺少令牌：请设置 GH_TOKEN，或用 --token-file 指定文件。" >&2
        echo "已尝试：\$HOME/.gh_token ($HOME/.gh_token)、/root/.gh_token、/tmp/gh_token" >&2
        exit 2
    fi
fi
[ -n "$GH_TOKEN" ] || { echo "令牌为空" >&2; exit 2; }

api() { # api <method> <path> [data]
    local method="$1" path="$2" data="${3:-}"
    if [ -n "$data" ]; then
        curl -sS -X "$method" -H "Authorization: Bearer $GH_TOKEN" \
             -H "Accept: application/vnd.github+json" \
             -H "X-GitHub-Api-Version: 2022-11-28" \
             -d "$data" "https://api.github.com$path"
    else
        curl -sS -X "$method" -H "Authorization: Bearer $GH_TOKEN" \
             -H "Accept: application/vnd.github+json" \
             -H "X-GitHub-Api-Version: 2022-11-28" \
             "https://api.github.com$path"
    fi
}

echo "==> 校验令牌"
LOGIN="$(api GET /user | python3 -c 'import json,sys; print(json.load(sys.stdin).get("login",""))')"
[ -n "$LOGIN" ] || { echo "令牌无效或没有权限" >&2; exit 1; }
echo "    已认证为: $LOGIN"

echo "==> 确保仓库存在: $REPO ($VISIBILITY)"
if api GET "/repos/$REPO" | grep -q '"full_name"'; then
    echo "    仓库已存在"
else
    NAME="${REPO#*/}"
    api POST /user/repos "$(python3 -c "
import json,sys
print(json.dumps({'name': sys.argv[1], 'private': sys.argv[2]=='private',
                  'description': '飞牛 OS 原生 AirPlay 2 → DLNA 桥接应用（无 Docker）',
                  'has_issues': True, 'has_wiki': False}))" "$NAME" "$VISIBILITY")" >/dev/null
    echo "    已创建仓库"
fi

cd "$ROOT"
echo "==> 推送 main"
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/$REPO.git"
# 令牌通过临时 header 注入，不写入 .git/config
git -c http.extraheader="AUTHORIZATION: bearer $GH_TOKEN" push -u origin main --tags

if [ -f "$FPK" ]; then
    echo "==> 建立 Release $TAG 并上传 $(basename "$FPK")"
    RELEASE_ID="$(api GET "/repos/$REPO/releases/tags/$TAG" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)"
    if [ -z "$RELEASE_ID" ]; then
        RELEASE_ID="$(api POST "/repos/$REPO/releases" "$(python3 -c "
import json,sys
print(json.dumps({'tag_name': sys.argv[1], 'name': sys.argv[1],
  'body': '飞牛 OS 原生 AirPlay 2 → DLNA 桥接应用。\\n\\n'
          '**安装**：应用中心 →「手动安装」选择本页的 .fpk，或\\n'
          '`appcenter-cli install-fpk AirPlay2-DLNA-Bridge-1.0.0.fpk --volume 1`\\n\\n'
          '依赖应用中心的 Python 3.12（python312）。不需要 Docker。',
  'draft': False, 'prerelease': False}))" "$TAG")" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')"
    fi
    [ -n "$RELEASE_ID" ] || { echo "创建 Release 失败" >&2; exit 1; }
    curl -sS -X POST -H "Authorization: Bearer $GH_TOKEN" \
         -H "Content-Type: application/octet-stream" \
         --data-binary "@$FPK" \
         "https://uploads.github.com/repos/$REPO/releases/$RELEASE_ID/assets?name=$(basename "$FPK")" \
         | python3 -c 'import json,sys; d=json.load(sys.stdin); print("    上传完成:", d.get("browser_download_url", d))'
else
    echo "    未找到 $FPK，跳过 Release（先运行 ./scripts/build.sh）"
fi

echo
echo "完成：https://github.com/$REPO"
