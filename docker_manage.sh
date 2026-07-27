#!/bin/bash

# ============================================
# Docker 容器管理脚本
# ============================================

source .deploy_config

show_menu() {
    echo "============================================="
    echo "Docker 容器管理 - 统一交易系统"
    echo "============================================="
    echo "1. 查看所有容器状态"
    echo "2. 查看容器实时日志"
    echo "3. 重启所有容器"
    echo "4. 停止所有容器"
    echo "5. 启动所有容器"
    echo "6. 重启单个容器"
    echo "7. 查看容器资源使用"
    echo "8. 进入容器终端"
    echo "9. 清理 Docker 缓存"
    echo "10. 备份数据库"
    echo "0. 退出"
    echo "============================================="
}

check_status() {
    echo "📊 所有容器状态："
    ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" \
        "docker ps -a -f name=trading_system -f name=postgres-db"
}

view_logs() {
    echo "请选择要查看日志的容器："
    echo "1. PostgreSQL"
    echo "2. BTC/ETH 策略"
    echo "3. 新币做空策略"
    echo "4. 网格交易策略"
    read -p "请选择 [1-4]: " choice

    case $choice in
        1) CONTAINER_NAME=$POSTGRES_CONTAINER_NAME ;;
        2) CONTAINER_NAME=$BTC_ETH_CONTAINER_NAME ;;
        3) CONTAINER_NAME=$NEW_COIN_CONTAINER_NAME ;;
        4) CONTAINER_NAME=$GRID_CONTAINER_NAME ;;
        *) echo "❌ 无效选择"; return ;;
    esac

    echo "📋 查看 $CONTAINER_NAME 实时日志（按 Ctrl+C 退出）..."
    ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" \
        "docker logs -f --tail 100 $CONTAINER_NAME"
}

restart_all() {
    echo "🔄 重启所有容器..."
    ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" << 'EOF'
cd /root/trading_system
docker-compose restart
EOF
    echo "✅ 重启完成"
}

stop_all() {
    echo "🛑 停止所有容器..."
    ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" << 'EOF'
cd /root/trading_system
docker-compose stop
EOF
    echo "✅ 停止完成"
}

start_all() {
    echo "🚀 启动所有容器..."
    ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" << 'EOF'
cd /root/trading_system
docker-compose start
EOF
    echo "✅ 启动完成"
}

restart_single() {
    echo "请选择要重启的容器："
    echo "1. PostgreSQL"
    echo "2. BTC/ETH 策略"
    echo "3. 新币做空策略"
    echo "4. 网格交易策略"
    read -p "请选择 [1-4]: " choice

    case $choice in
        1) CONTAINER_NAME=$POSTGRES_CONTAINER_NAME ;;
        2) CONTAINER_NAME=$BTC_ETH_CONTAINER_NAME ;;
        3) CONTAINER_NAME=$NEW_COIN_CONTAINER_NAME ;;
        4) CONTAINER_NAME=$GRID_CONTAINER_NAME ;;
        *) echo "❌ 无效选择"; return ;;
    esac

    echo "🔄 重启 $CONTAINER_NAME..."
    ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" \
        "docker restart $CONTAINER_NAME"
    echo "✅ 重启完成"
}

view_resources() {
    echo "📊 容器资源使用情况："
    ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" \
        "docker stats --no-stream | grep -E 'CONTAINER|trading_system|postgres'"
}

enter_container() {
    echo "请选择要进入的容器："
    echo "1. PostgreSQL"
    echo "2. BTC/ETH 策略"
    echo "3. 新币做空策略"
    echo "4. 网格交易策略"
    read -p "请选择 [1-4]: " choice

    case $choice in
        1) CONTAINER_NAME=$POSTGRES_CONTAINER_NAME ;;
        2) CONTAINER_NAME=$BTC_ETH_CONTAINER_NAME ;;
        3) CONTAINER_NAME=$NEW_COIN_CONTAINER_NAME ;;
        4) CONTAINER_NAME=$GRID_CONTAINER_NAME ;;
        *) echo "❌ 无效选择"; return ;;
    esac

    echo "🔌 进入 $CONTAINER_NAME 终端..."
    ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" \
        "docker exec -it $CONTAINER_NAME /bin/bash"
}

clean_cache() {
    echo "⚠️  警告：此操作将清理所有悬空镜像和未使用的容器！"
    read -p "确定要继续吗？(y/N): " confirm
    if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
        echo "🧹 清理 Docker 缓存..."
        ssh -i "$SSH_KEY_PATH" \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            "$SERVER_USER@$SERVER_IP" << 'EOF'
docker system prune -f
docker image prune -a -f --filter "until=24h"
docker volume prune -f
EOF
        echo "✅ 清理完成"
    else
        echo "❌ 操作已取消"
    fi
}

backup_database() {
    echo "💾 备份数据库..."
    ssh -i "$SSH_KEY_PATH" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "$SERVER_USER@$SERVER_IP" << 'EOF'
cd /root/trading_system/database/postgres/scripts
chmod +x backup-postgres.sh
./backup-postgres.sh
EOF
    echo "✅ 备份完成"
}

# 主循环
while true; do
    show_menu
    read -p "请选择操作 [0-10]: " choice

    case $choice in
        1) check_status ;;
        2) view_logs ;;
        3) restart_all ;;
        4) stop_all ;;
        5) start_all ;;
        6) restart_single ;;
        7) view_resources ;;
        8) enter_container ;;
        9) clean_cache ;;
        10) backup_database ;;
        0) echo "👋 退出"; exit 0 ;;
        *) echo "❌ 无效选择" ;;
    esac

    echo ""
    read -p "按回车键继续..."
done
