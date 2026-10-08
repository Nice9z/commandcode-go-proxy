#!/usr/bin/env bash
# ============================================================
# Command Code Go 反代 - VPS 一键部署 (v2)
#
#   curl -fsSL https://raw.githubusercontent.com/Nice9z/commandcode-go-proxy/main/deploy.sh | bash
#
# 适配: Ubuntu / Debian (systemd, python3>=3.8, curl)
# 安装位置: /opt/cc-go-proxy  (root 必需)
# ============================================================
set -euo pipefail

APP="cc-go-proxy"
INSTALL_DIR="/opt/cc-go-proxy"
RAW_BASES=(
  "https://raw.githubusercontent.com/Nice9z/commandcode-go-proxy/main"
  "https://ghfast.top/https://raw.githubusercontent.com/Nice9z/commandcode-go-proxy/main"
)

[ "$(id -u)" = "0" ] || { echo "请用 root 运行 (sudo bash)"; exit 1; }


ask() { read -r -p "$1" "$2" < /dev/tty; }
token_ok() { case "$1" in user_*) return 0 ;; *) return 1 ;; esac; }

echo "=============================================="
echo " Command Code Go 反代 VPS 一键部署"
echo "=============================================="

# ---- 0. 清理旧安装 ----
systemctl disable --now "$APP" >/dev/null 2>&1 || true
rm -f "/etc/systemd/system/${APP}.service"
systemctl daemon-reload 2>/dev/null || true

# ---- 1. 依赖 ----
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "[*] 安装 python3..."
  if command -v apt-get >/dev/null; then apt-get update -y && apt-get install -y python3
  elif command -v dnf >/dev/null; then dnf install -y python3
  elif command -v yum >/dev/null; then yum install -y python3
  else echo "请手动安装 python3"; exit 1; fi
  PY="$(command -v python3)"
fi
command -v curl >/dev/null || { apt-get update -y && apt-get install -y curl; }

# ---- 2. 安装 proxy.py 到 /opt ----
mkdir -p "$INSTALL_DIR"
if [ -f "$(pwd)/proxy.py" ] && grep -q "alpha/generate" "$(pwd)/proxy.py" 2>/dev/null; then
  cp "$(pwd)/proxy.py" "$INSTALL_DIR/proxy.py"
  echo "[*] 使用本地 proxy.py"
else
  echo "[*] 下载 proxy.py..."
  OK=""
  for base in "${RAW_BASES[@]}"; do
    if curl -fsSL --max-time 60 -o "$INSTALL_DIR/proxy.py.tmp" "$base/proxy.py?v=$(date +%s)" \
       && grep -q "alpha/generate" "$INSTALL_DIR/proxy.py.tmp"; then
      mv "$INSTALL_DIR/proxy.py.tmp" "$INSTALL_DIR/proxy.py"; OK=1; break
    fi
    rm -f "$INSTALL_DIR/proxy.py.tmp"
  done
  [ -n "$OK" ] || { echo "下载失败，检查网络后重试"; exit 1; }
fi
PROXY="$INSTALL_DIR/proxy.py"

# 版本检查：本地 vs GitHub 最新 Release，有新版本就自动换上
LOCAL_VER="$(grep -oE 'APP_VERSION = "[0-9.]+"' "$PROXY" 2>/dev/null | grep -oE '[0-9.]+' || echo 0)"
GH_VER="$(curl -sS --max-time 10 "https://api.github.com/repos/Nice9z/commandcode-go-proxy/releases/latest" 2>/dev/null | grep -oE '"tag_name": *"v?[0-9.]+"' | grep -oE '[0-9.]+' || echo '')"
if [ -n "$GH_VER" ] && [ "$LOCAL_VER" != "$GH_VER" ]; then
  echo "[*] 发现新版本 v$GH_VER（当前 v${LOCAL_VER:-无}），下载新版..."
  DL=""
  for base in "${RAW_BASES[@]}"; do
    if curl -fsSL --max-time 60 -o "$INSTALL_DIR/proxy.py.tmp" "$base/proxy.py?v=$(date +%s)" \
       && grep -q "alpha/generate" "$INSTALL_DIR/proxy.py.tmp" \
       && grep -q "APP_VERSION = \"$GH_VER\"" "$INSTALL_DIR/proxy.py.tmp"; then
      mv "$INSTALL_DIR/proxy.py.tmp" "$INSTALL_DIR/proxy.py"; DL=1; break
    fi
    rm -f "$INSTALL_DIR/proxy.py.tmp"
  done
  if [ -n "$DL" ]; then
    echo "[*] 已更新到 v$GH_VER"
  else
    echo "[!] 自动更新失败，继续用现有版本 v$LOCAL_VER"
  fi
elif [ -n "$GH_VER" ]; then
  echo "[*] 已是最新版本 v$LOCAL_VER"
fi

# ---- 3. 配置 ----
ENVF="/etc/${APP}.env"
if [ -f "$ENVF" ]; then
  OLD_TOKEN="$(grep -oE '^CMD_CODE_TOKEN=.*' "$ENVF" | cut -d= -f2-)"
  OLD_KEY="$(grep -oE '^CMD_CODE_KEY=.*' "$ENVF" | cut -d= -f2-)"
  OLD_PORT="$(grep -oE '^CMD_CODE_PORT=.*' "$ENVF" | cut -d= -f2-)"
  OLD_MODEL="$(grep -oE '^CMD_CODE_DEFAULT_MODEL=.*' "$ENVF" | cut -d= -f2-)"
  echo "[*] 检测到已有配置"

  # API 密钥：回车=不变，其他=输入新密钥
  ask "API 密钥 (user_ 开头) [回车=不变]: " TOKEN
  TOKEN=${TOKEN:-$OLD_TOKEN}
  while ! token_ok "$TOKEN"; do
    echo "  应该以 user_ 开头"
    ask "API 密钥 (user_ 开头) [回车=不变]: " TOKEN
    TOKEN=${TOKEN:-$OLD_TOKEN}
  done

  # 网关密码：回车=不变，1=重新随机生成，其他=用输入值
  ask "网关密码 [回车=不变 | 1=重新生成]: " GKEY
  if [ "$GKEY" = "1" ]; then
    GKEY="sk-gw-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "  已重新生成: $GKEY"
  fi
  GKEY=${GKEY:-$OLD_KEY}

  # 端口：回车=不变，其他=新端口
  ask "端口 [回车=不变，当前 $OLD_PORT]: " PORT
  PORT=${PORT:-$OLD_PORT}

  # 默认模型：回车=不变，其他=新模型
  ask "默认模型 [回车=不变，当前 $OLD_MODEL]: " DEFMODEL
  DEFMODEL=${DEFMODEL:-$OLD_MODEL}
else
  echo "[*] 首次安装"
  TOKEN="${CMD_CODE_TOKEN:-}"
  while ! token_ok "${TOKEN:-}"; do
    ask "粘贴你的 API 密钥 (user_ 开头, 在 commandcode.ai 后台设置里创建): " TOKEN
    token_ok "$TOKEN" || echo "  应该以 user_ 开头"
  done
  GKEY="${CMD_CODE_KEY:-}"
  if [ -z "$GKEY" ]; then
    ask "客户端网关密码 [回车=自动生成强随机]: " GKEY
    [ -z "$GKEY" ] && { GKEY="sk-gw-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"; echo "  已生成: $GKEY"; }
  fi
  ask "监听端口 [18787]: " PORT;  PORT=${PORT:-18787}
  ask "默认模型 [z-ai/glm-5.3-flash]: " DEFMODEL;  DEFMODEL=${DEFMODEL:-z-ai/glm-5.3-flash}
fi

# ---- 4. 环境文件 (0600) ----
ENVF="/etc/${APP}.env"
umask 077
cat > "$ENVF" <<EOF
CMD_CODE_TOKEN=$TOKEN
CMD_CODE_KEY=$GKEY
CMD_CODE_HOST=0.0.0.0
CMD_CODE_PORT=$PORT
CMD_CODE_DEFAULT_MODEL=$DEFMODEL
CMD_CODE_DB=$INSTALL_DIR/cc_proxy_usage.db
EOF
umask 022

# ---- 5. systemd 服务 ----
# 注意: ProtectHome=true 会隐藏 /root, 所以程序放在 /opt; DB 也指向 /opt
cat > "/etc/systemd/system/${APP}.service" <<EOF
[Unit]
Description=Command Code Go-plan reverse proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENVF
ExecStart=$PY $PROXY
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$INSTALL_DIR
MemoryMax=512M

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$APP"
sleep 2

# ---- 6. 自动测试（发测试请求，确认服务正常）----
echo ""
echo "--- 服务状态 ---"
if systemctl is-active --quiet "$APP"; then
  echo "[OK] 服务运行中"
else
  echo "[FAIL] 服务启动失败:"; journalctl -u "$APP" -n 20 --no-pager; exit 1
fi
echo "--- 检查 1：服务是否在线 ---"
curl -s "http://127.0.0.1:$PORT/healthz" | head -c 200; echo ""
echo "--- 检查 2：发一条真实请求（用免费模型，不消耗额度）---"
curl -s --max-time 90 -X POST "http://127.0.0.1:$PORT/v1/chat/completions" \
  -H "Content-Type: application/json" -H "Authorization: Bearer $GKEY" \
  -d '{"model":"inclusionai/ling-3.1-flash:free","messages":[{"role":"user","content":"回复OK"}],"max_tokens":20}' \
  | head -c 300
echo ""
echo ""
echo "=============================================="
echo " 部署完成! (版本 v${LOCAL_VER:-unknown})"
echo "   API:       http://<VPS_IP>:$PORT/v1"
echo "   看板:      http://<VPS_IP>:$PORT/dashboard"
echo "   网关密码:  $GKEY"
echo "   默认模型:  $DEFMODEL"
echo "   安装位置:  $INSTALL_DIR"
echo "   日志:      journalctl -u $APP -f"
echo "   换API密钥:  编辑 $ENVF 后 systemctl restart $APP"
echo ""
echo " 建议: 防火墙只放行你自己的 IP 访问 $PORT，或套 nginx/caddy 上 TLS"
echo "=============================================="
