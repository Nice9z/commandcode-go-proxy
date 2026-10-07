#!/usr/bin/env bash
# ============================================================
# Command Code Go 反代 - VPS 一键部署
#
# 一键执行（无需 clone 仓库）:
#   curl -fsSL https://raw.githubusercontent.com/Nice9z/commandcode-go-proxy/main/deploy.sh | bash
#
# 或先下载再跑（效果相同）:
#   bash deploy.sh
#
# 适配: Ubuntu / Debian (systemd, python3>=3.8, curl)
# 交互输入走 /dev/tty，所以 curl | bash 管道模式下也能正常提问
# ============================================================
set -euo pipefail

APP="cc-go-proxy"
SELFDIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || pwd)"
RAW_BASES=(
  "https://raw.githubusercontent.com/Nice9z/commandcode-go-proxy/main"
  "https://ghfast.top/https://raw.githubusercontent.com/Nice9z/commandcode-go-proxy/main"
)

# tty 读取（管道下仍可交互）
ask() { read -r -p "$1" "$2" < /dev/tty; }

echo "=============================================="
echo " Command Code Go 反代 VPS 一键部署"
echo "=============================================="

# ---- 0. 依赖 ----
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "[*] 未找到 python3，尝试安装..."
  if command -v apt-get >/dev/null; then apt-get update -y && apt-get install -y python3
  elif command -v dnf >/dev/null; then dnf install -y python3
  elif command -v yum >/dev/null; then yum install -y python3
  else echo "请手动安装 python3 后重试"; exit 1; fi
  PY="$(command -v python3)"
fi
command -v curl >/dev/null || { echo "[*] 安装 curl..."; apt-get update -y && apt-get install -y curl; }

# ---- 1. 拿 proxy.py（当前目录没有就从 GitHub 拉，含国内镜像回退）----
PROXY=""
if [ -f "$SELFDIR/proxy.py" ]; then
  PROXY="$SELFDIR/proxy.py"
else
  echo "[*] 下载 proxy.py..."
  for base in "${RAW_BASES[@]}"; do
    if curl -fsSL --max-time 60 -o /tmp/cc-proxy.py "$base/proxy.py"; then
      grep -q "alpha/generate" /tmp/cc-proxy.py 2>/dev/null || { rm -f /tmp/cc-proxy.py; continue; }
      mv /tmp/cc-proxy.py ./proxy.py; PROXY="$PWD/proxy.py"; break
    fi
  done
  [ -n "$PROXY" ] || { echo "下载 proxy.py 失败（检查网络或手动上传）"; exit 1; }
  echo "    OK -> $PROXY"
fi
WORKDIR="$(dirname "$PROXY")"

# ---- 2. 上游 token ----
TOKEN="${CMD_CODE_TOKEN:-}"
if [ -n "$TOKEN" ]; then
  echo "[*] 检测到环境变量 token，直接使用"
else
  ask "粘贴你的 user_ API token (commandcode.ai/settings/billing): " TOKEN
fi
while [ ! "${TOKEN:-}" =~ ^user_ ]; do
  echo "  token 应该以 user_ 开头，请重试"
  ask "粘贴你的 user_ API token: " TOKEN
done

# ---- 3. 网关 key ----
GKEY="${CMD_CODE_KEY:-}"
if [ -z "$GKEY" ]; then
  ask "客户端网关 key [回车=自动生成强随机]: " GKEY
fi
if [ -z "$GKEY" ]; then
  GKEY="sk-gw-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo "  已生成: $GKEY"
fi

# ---- 4. 端口 / 默认模型 ----
ask "监听端口 [18787]: " PORT
PORT=${PORT:-18787}
ask "默认模型 [z-ai/glm-5.3-flash]: " DEFMODEL
DEFMODEL=${DEFMODEL:-z-ai/glm-5.3-flash}

# ---- 5. systemd 环境文件 (0600) ----
ENVF="/etc/${APP}.env"
umask 077
cat > "$ENVF" <<EOF
CMD_CODE_TOKEN=$TOKEN
CMD_CODE_KEY=$GKEY
CMD_CODE_HOST=0.0.0.0
CMD_CODE_PORT=$PORT
CMD_CODE_DEFAULT_MODEL=$DEFMODEL
EOF
umask 022

# ---- 6. systemd 服务（安全加固）----
cat > "/etc/systemd/system/${APP}.service" <<EOF
[Unit]
Description=Command Code Go-plan reverse proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$WORKDIR
EnvironmentFile=$ENVF
ExecStart=$PY $PROXY
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$WORKDIR
MemoryMax=512M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$APP"
sleep 2

# ---- 7. 冒烟验证 ----
echo ""
echo "--- 服务状态 ---"
systemctl is-active "$APP" && echo "[OK] 服务已启动" || { journalctl -u "$APP" -n 20 --no-pager; exit 1; }
echo "--- 冒烟: /healthz ---"
curl -s "http://127.0.0.1:$PORT/healthz" | head -c 200; echo ""
echo "--- 冒烟: 真实请求（免费模型，不烧额度）---"
curl -s --max-time 90 -X POST "http://127.0.0.1:$PORT/v1/chat/completions" \
  -H "Content-Type: application/json" -H "Authorization: Bearer $GKEY" \
  -d '{"model":"inclusionai/ling-3.1-flash:free","messages":[{"role":"user","content":"回复OK"}],"max_tokens":20}' \
  | head -c 300
echo ""
echo ""
echo "=============================================="
echo " 部署完成!"
echo "   API:       http://<VPS_IP>:$PORT/v1"
echo "   看板:      http://<VPS_IP>:$PORT/dashboard"
echo "   客户端key: $GKEY   (只显示这一次, 存好)"
echo "   默认模型:  $DEFMODEL"
echo "   日志:      journalctl -u $APP -f"
echo "   换token:   编辑 $ENVF 后 systemctl restart $APP"
echo ""
echo " 建议: 防火墙只放行你自己的 IP 访问 $PORT，或套 nginx/caddy 上 TLS"
echo "=============================================="
