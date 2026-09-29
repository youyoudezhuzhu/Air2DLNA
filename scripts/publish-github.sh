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
FPK="$ROOT/dist/Air2DLNA-${TAG#v}.fpk"

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
                  'description': '飞牛 OS 原生 Air2DLNA应用（无 Docker）',
                  'has_issues': True, 'has_wiki': False}))" "$NAME" "$VISIBILITY")" >/dev/null
    echo "    已创建仓库"
fi

cd "$ROOT"
echo "==> 推送 main"
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/$REPO.git"

# 通过 GIT_ASKPASS 提供凭据：
#  * 令牌不出现在命令行（ps 看不到）、不写入 .git/config、不进入 shell 历史；
#  * 之前用 http.extraheader 的写法 GitHub 不接受，会报
#    "could not read Username for 'https://github.com'"。
TOKEN_TMP="$(mktemp)"; chmod 600 "$TOKEN_TMP"; printf '%s' "$GH_TOKEN" > "$TOKEN_TMP"
ASKPASS_TMP="$(mktemp)"; chmod 700 "$ASKPASS_TMP"
cat > "$ASKPASS_TMP" <<'ASKPASS_EOF'
#!/bin/sh
case "$1" in
    *Username*) printf '%s' "x-access-token" ;;
    *Password*) cat "$A2D_TOKEN_FILE" ;;
    *)          printf '%s' "" ;;
esac
ASKPASS_EOF
cleanup_creds() { rm -f "$TOKEN_TMP" "$ASKPASS_TMP"; }
trap cleanup_creds EXIT INT TERM

A2D_TOKEN_FILE="$TOKEN_TMP" GIT_ASKPASS="$ASKPASS_TMP" \
    git push -u origin main --tags
cleanup_creds
trap - EXIT INT TERM

if [ -f "$FPK" ]; then
    echo "==> 建立 Release $TAG 并上传 $(basename "$FPK")"
    SHA="$(sha256sum "$FPK" | cut -d' ' -f1)"
    SIZE="$(stat -c %s "$FPK")"
    ASSET="$(basename "$FPK")"

    BODY_FILE="$(mktemp)"
    cat > "$BODY_FILE" <<BODY_EOF
飞牛 OS 原生 Air2DLNA应用（不使用 Docker）。

## 安装
1. 应用中心 →「手动安装」→ 选择本页的 \`.fpk\`；或
2. \`\`\`bash
   appcenter-cli install-fpk $ASSET --volume 1
   appcenter-cli start air2dlna
   \`\`\`

依赖：应用中心的 **Python 3.12**（\`python312\`，安装时自动准备）。

安装后在 Web UI（\`http://<NAS_IP>:8788\`）里设置 AirPlay 名称并选择 DLNA 音响。
真机验收清单见仓库 \`docs/ACCEPTANCE.md\`。

## 校验
- 文件：\`$ASSET\`
- 体积：\`$SIZE\` 字节
- SHA-256：\`$SHA\`

> 重建：\`./scripts/build.sh\`（需要 fnpack）。CI 也会跑单元测试与端到端集成测试。
BODY_EOF

    RELEASE_ID="$(api GET "/repos/$REPO/releases/tags/$TAG" 2>/dev/null \
        | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("id","") or "")
except Exception: print("")' || true)"

    if [ -z "$RELEASE_ID" ]; then
        JSON_FILE="$(mktemp)"
        python3 -c 'import json,sys
tag, body_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
body = open(body_path, encoding="utf-8").read()
json.dump({"tag_name": tag, "name": tag, "body": body,
           "draft": False, "prerelease": False}, open(out, "w", encoding="utf-8"))' \
            "$TAG" "$BODY_FILE" "$JSON_FILE"
        # 用 --data-binary 读取 JSON 文件，保留换行
        RELEASE_ID="$(curl -sS -X POST -H "Authorization: Bearer $GH_TOKEN" \
            -H "Accept: application/vnd.github+json" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            --data-binary "@$JSON_FILE" \
            "https://api.github.com/repos/$REPO/releases" \
            | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')"
        rm -f "$JSON_FILE"
    else
        echo "    Release $TAG 已存在，仅上传资产"
    fi
    rm -f "$BODY_FILE"

    [ -n "$RELEASE_ID" ] || { echo "创建 Release 失败" >&2; exit 1; }
    curl -sS -X POST -H "Authorization: Bearer $GH_TOKEN" \
         -H "Content-Type: application/octet-stream" \
         --data-binary "@$FPK" \
         "https://uploads.github.com/repos/$REPO/releases/$RELEASE_ID/assets?name=$ASSET" \
         | python3 -c 'import json,sys
d = json.load(sys.stdin)
print("    上传完成:", d.get("browser_download_url") or d.get("message") or d)'
else
    echo "    未找到 $FPK，跳过 Release（先运行 ./scripts/build.sh）"
fi

echo
echo "仓库：https://github.com/$REPO"
echo "发布：https://github.com/$REPO/releases/tag/$TAG"
