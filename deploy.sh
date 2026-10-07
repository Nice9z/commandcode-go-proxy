#!/usr/bin/env bash
# ============================================================
# Command Code Go-plan 反代 - VPS 交互式部署脚本
# 适配: Ubuntu / Debian (systemd)   Python >= 3.8 即可
# 用法: 把 proxy.py 和本脚本传到 VPS 同一目录, 然后:
#       bash deploy.sh
# ============================================================
set -euo pipefail

APP="cc-go-proxy"
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)"

if [ -z "$PY" ]; then
  echo "未找到 python3，先安装: apt install -y python3"; exit 1
fi

echo "=============================================="
echo " Command Code Go 反代 VPS 部署"
echo "=============================================="

# ---- 1. 上游 token (必填, 手动输入, 不落盘明文配置文件, 存进 systemd 环境文件 600 权限) ----
if [ -n "${CMD_CODE_TOKEN:-}" ]; then
  TOKEN="$CMD_CODE_TOKEN"
  echo "检测到环境变量里的 token, 直接使用"
else
  read -r -p "粘贴你的 user_ API token (commandcode.ai/settings/billing): " TOKEN
fi
while [ ! "${TOKEN:-}" =~ ^user_ ]; do
  echo "  token 应该以 user_ 开头, 请重试"
  read -r -p "粘贴你的 user_ API token: " TOKEN
done

# ---- 2. 网关 key (客户端密码, 可自动生成) ----
read -r -p "客户端网关 key [回车=自动生成强随机]: " GKEY
if [ -z "$GKEY" ]; then
  GKEY="sk-gw-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo "  已生成: $GKEY"
fi

# ---- 3. 端口 / 默认模型 ----
read -r -p "监听端口 [18787]: " PORT
PORT=${PORT:-18787}
read -r -p "默认模型 [z-ai/glm-5.3-flash]: " DEFMODEL
DEFMODEL=${DEFMODEL:-z-ai/glm-5.3-flash}

# ---- 4. 写 systemd 环境文件 (0600, 仅 root 可读) ----
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

# ---- 5. systemd 服务 ----
cat > "/etc/systemd/system/${APP}.service" <<EOF
[Unit]
Description=Command Code Go-plan reverse proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
EnvironmentFile=$ENVF
ExecStart=$PY $DIR/proxy.py
Restart=always
RestartSec=5
# 硬化: 只留必要权限
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DIR
MemoryMax=512M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$APP"
sleep 2

# ---- 6. 冒烟验证 ----
echo ""
echo "--- 服务状态 ---"
systemctl is-active "$APP" && echo "服务已启动" || { journalctl -u "$APP" -n 20 --no-pager; exit 1; }
echo "--- 冒烟: /healthz ---"
curl -s "http://127.0.0.1:$PORT/healthz" | head -c 200; echo ""
echo "--- 冒烟: 真实请求 (免费模型, 不烧额度) ---"
curl -s --max-time 90 -X POST "http://127.0.0.1:$PORT/v1/chat/completions" \
  -H "Content-Type: application/json" -H "Authorization: Bearer $GKEY" \
  -d "{\"model\":\"inclusionai/ling-3.1-flash:free\",\"messages\":[{\"role\":\"user\",\"content\":\"回复OK\"}],\"max_tokens\":20}" \
  | head -c 300
echo ""
echo ""
echo "=============================================="
echo " 部署完成!"
echo "   API:     http://<VPS_IP>:$PORT/v1"
echo "   看板:    http://<VPS_IP>:$PORT/dashboard"
echo "   客户端key: $GKEY"
echo "   默认模型:  $DEFMODEL"
echo "   日志:    journalctl -u $APP -f"
echo "   改token: 编辑 $ENVF 后 systemctl restart $APP"
echo ""
echo " 建议下一步: 套 nginx/caddy 上 TLS, 或用 iptables"
echo " 把 18787 限制为仅你的 IP 可访问"
echo "=============================================="
