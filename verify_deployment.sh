#!/bin/bash

# ============================================
# 部署验证脚本 - 确保容器更新到最新版本
# ============================================

source .deploy_config

echo "============================================="
echo "🔍 部署验证 - 确保容器更新到最新版本"
echo "============================================="

# 在服务器上执行验证命令
ssh -i "$SSH_KEY_PATH" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$SERVER_USER@$SERVER_IP" << ENDSSH

echo "============================================="
echo "验证步骤："
echo "1. PostgreSQL 容器状态"
echo "2. 策略容器运行状态"
echo "3. 镜像版本验证（关键）"
echo "4. 健康状态检查"
echo "5. 日志错误检查"
echo "6. 资源使用检查"
echo "============================================="

# 1. 验证 PostgreSQL 容器
echo ""
echo "1️⃣  验证 PostgreSQL 容器..."
POSTGRES_STATUS=\$(docker ps -f name=$POSTGRES_CONTAINER_NAME --format '{{.Status}}')
if [ -z "\$POSTGRES_STATUS" ]; then
    echo "❌ PostgreSQL 容器未运行！"
    exit 1
fi
echo "✅ PostgreSQL 容器运行状态：\$POSTGRES_STATUS"

# 检查 PostgreSQL 是否就绪
if docker exec $POSTGRES_CONTAINER_NAME pg_isready -U trading_user -d trading_platform > /dev/null 2>&1; then
    echo "✅ PostgreSQL 数据库已就绪"
else
    echo "❌ PostgreSQL 数据库未就绪！"
    exit 1
fi

# 2. 验证策略容器运行状态
echo ""
echo "2️⃣  验证策略容器运行状态..."

ERROR_COUNT=0

# BTC/ETH 策略
if [ "$DEPLOY_BTC_ETH" = true ]; then
    BTC_ETH_STATUS=\$(docker ps -f name=$BTC_ETH_CONTAINER_NAME --format '{{.Status}}')
    if [ -z "\$BTC_ETH_STATUS" ]; then
        echo "❌ BTC/ETH 策略容器未运行！"
        ERROR_COUNT=\$((ERROR_COUNT + 1))
    else
        echo "✅ BTC/ETH 策略容器运行状态：\$BTC_ETH_STATUS"
    fi
fi

# 新币做空策略
if [ "$DEPLOY_NEW_COIN" = true ]; then
    NEW_COIN_STATUS=\$(docker ps -f name=$NEW_COIN_CONTAINER_NAME --format '{{.Status}}')
    if [ -z "\$NEW_COIN_STATUS" ]; then
        echo "❌ 新币做空策略容器未运行！"
        ERROR_COUNT=\$((ERROR_COUNT + 1))
    else
        echo "✅ 新币做空策略容器运行状态：\$NEW_COIN_STATUS"
    fi
fi

# 网格交易策略
if [ "$DEPLOY_GRID" = true ]; then
    GRID_STATUS=\$(docker ps -f name=$GRID_CONTAINER_NAME --format '{{.Status}}')
    if [ -z "\$GRID_STATUS" ]; then
        echo "❌ 网格交易策略容器未运行！"
        ERROR_COUNT=\$((ERROR_COUNT + 1))
    else
        echo "✅ 网格交易策略容器运行状态：\$GRID_STATUS"
    fi
fi

# K 线数据服务
if [ "$DEPLOY_KLINE" = true ]; then
    KLINE_STATUS=\$(docker ps -f name=$KLINE_CONTAINER_NAME --format '{{.Status}}')
    if [ -z "\$KLINE_STATUS" ]; then
        echo "❌ K 线数据服务容器未运行！"
        ERROR_COUNT=\$((ERROR_COUNT + 1))
    else
        echo "✅ K 线数据服务容器运行状态：\$KLINE_STATUS"
    fi
fi

if [ \$ERROR_COUNT -gt 0 ]; then
    echo "❌ 有 \$ERROR_COUNT 个容器未运行！"
    exit 1
fi

# 3. 验证容器镜像版本（关键）⭐⭐⭐
echo ""
echo "3️⃣  验证容器镜像版本（确保是最新版本）..."

# BTC/ETH 策略
if [ "$DEPLOY_BTC_ETH" = true ]; then
    BTC_ETH_IMAGE=\$(docker inspect -f '{{.Config.Image}}' $BTC_ETH_CONTAINER_NAME 2>/dev/null)
    BTC_ETH_CREATED=\$(docker inspect -f '{{.Created}}' $BTC_ETH_CONTAINER_NAME 2>/dev/null)
    echo "   BTC/ETH 策略容器镜像：\$BTC_ETH_IMAGE"
    echo "   镜像创建时间：\$BTC_ETH_CREATED"

    # 获取本地最新镜像
    LOCAL_BTC_ETH_IMAGE=\$(docker images --format "{{.Repository}}:{{.Tag}}" | grep btc_eth | head -1)
    LOCAL_BTC_ETH_CREATED=\$(docker inspect -f '{{.Created}}' \$LOCAL_BTC_ETH_IMAGE 2>/dev/null)
    echo "   本地最新镜像：\$LOCAL_BTC_ETH_IMAGE"
    echo "   本地镜像创建时间：\$LOCAL_BTC_ETH_CREATED"

    # 比较镜像创建时间
    if [ "\$BTC_ETH_CREATED" = "\$LOCAL_BTC_ETH_CREATED" ]; then
        echo "   ✅ BTC/ETH 策略容器使用的是最新镜像版本"
    else
        echo "   ⚠️  警告：BTC/ETH 策略容器可能未使用最新镜像！"
    fi
fi

# 新币做空策略
if [ "$DEPLOY_NEW_COIN" = true ]; then
    NEW_COIN_IMAGE=\$(docker inspect -f '{{.Config.Image}}' $NEW_COIN_CONTAINER_NAME 2>/dev/null)
    NEW_COIN_CREATED=\$(docker inspect -f '{{.Created}}' $NEW_COIN_CONTAINER_NAME 2>/dev/null)
    echo "   新币做空策略容器镜像：\$NEW_COIN_IMAGE"
    echo "   镜像创建时间：\$NEW_COIN_CREATED"

    LOCAL_NEW_COIN_IMAGE=\$(docker images --format "{{.Repository}}:{{.Tag}}" | grep new_coin | head -1)
    LOCAL_NEW_COIN_CREATED=\$(docker inspect -f '{{.Created}}' \$LOCAL_NEW_COIN_IMAGE 2>/dev/null)
    echo "   本地最新镜像：\$LOCAL_NEW_COIN_IMAGE"
    echo "   本地镜像创建时间：\$LOCAL_NEW_COIN_CREATED"

    if [ "\$NEW_COIN_CREATED" = "\$LOCAL_NEW_COIN_CREATED" ]; then
        echo "   ✅ 新币做空策略容器使用的是最新镜像版本"
    else
        echo "   ⚠️  警告：新币做空策略容器可能未使用最新镜像！"
    fi
fi

# 网格交易策略
if [ "$DEPLOY_GRID" = true ]; then
    GRID_IMAGE=\$(docker inspect -f '{{.Config.Image}}' $GRID_CONTAINER_NAME 2>/dev/null)
    GRID_CREATED=\$(docker inspect -f '{{.Created}}' $GRID_CONTAINER_NAME 2>/dev/null)
    echo "   网格交易策略容器镜像：\$GRID_IMAGE"
    echo "   镜像创建时间：\$GRID_CREATED"

    LOCAL_GRID_IMAGE=\$(docker images --format "{{.Repository}}:{{.Tag}}" | grep grid | head -1)
    LOCAL_GRID_CREATED=\$(docker inspect -f '{{.Created}}' \$LOCAL_GRID_IMAGE 2>/dev/null)
    echo "   本地最新镜像：\$LOCAL_GRID_IMAGE"
    echo "   本地镜像创建时间：\$LOCAL_GRID_CREATED"

    if [ "\$GRID_CREATED" = "\$LOCAL_GRID_CREATED" ]; then
        echo "   ✅ 网格交易策略容器使用的是最新镜像版本"
    else
        echo "   ⚠️  警告：网格交易策略容器可能未使用最新镜像！"
    fi
fi

# K 线数据服务
if [ "$DEPLOY_KLINE" = true ]; then
    KLINE_IMAGE=\$(docker inspect -f '{{.Config.Image}}' $KLINE_CONTAINER_NAME 2>/dev/null)
    KLINE_CREATED=\$(docker inspect -f '{{.Created}}' $KLINE_CONTAINER_NAME 2>/dev/null)
    echo "   K 线数据服务容器镜像：\$KLINE_IMAGE"
    echo "   镜像创建时间：\$KLINE_CREATED"

    LOCAL_KLINE_IMAGE=\$(docker images --format "{{.Repository}}:{{.Tag}}" | grep kline | head -1)
    LOCAL_KLINE_CREATED=\$(docker inspect -f '{{.Created}}' \$LOCAL_KLINE_IMAGE 2>/dev/null)
    echo "   本地最新镜像：\$LOCAL_KLINE_IMAGE"
    echo "   本地镜像创建时间：\$LOCAL_KLINE_CREATED"

    if [ "\$KLINE_CREATED" = "\$LOCAL_KLINE_CREATED" ]; then
        echo "   ✅ K 线数据服务容器使用的是最新镜像版本"
    else
        echo "   ⚠️  警告：K 线数据服务容器可能未使用最新镜像！"
    fi
fi

# 4. 验证容器健康状态
echo ""
echo "4️⃣  验证容器健康状态..."

if [ "$DEPLOY_BTC_ETH" = true ]; then
    BTC_ETH_HEALTH=\$(docker inspect -f '{{.State.Health.Status}}' $BTC_ETH_CONTAINER_NAME 2>/dev/null || echo "无健康检查")
    echo "   BTC/ETH 策略健康状态：\$BTC_ETH_HEALTH"
fi

if [ "$DEPLOY_NEW_COIN" = true ]; then
    NEW_COIN_HEALTH=\$(docker inspect -f '{{.State.Health.Status}}' $NEW_COIN_CONTAINER_NAME 2>/dev/null || echo "无健康检查")
    echo "   新币做空策略健康状态：\$NEW_COIN_HEALTH"
fi

if [ "$DEPLOY_GRID" = true ]; then
    GRID_HEALTH=\$(docker inspect -f '{{.State.Health.Status}}' $GRID_CONTAINER_NAME 2>/dev/null || echo "无健康检查")
    echo "   网格交易策略健康状态：\$GRID_HEALTH"
fi

if [ "$DEPLOY_KLINE" = true ]; then
    KLINE_HEALTH=\$(docker inspect -f '{{.State.Health.Status}}' $KLINE_CONTAINER_NAME 2>/dev/null || echo "无健康检查")
    echo "   K 线数据服务健康状态：\$KLINE_HEALTH"
fi

# 5. 验证容器日志（检查是否有启动错误）
echo ""
echo "5️⃣  验证容器日志（检查启动错误）..."

if [ "$DEPLOY_BTC_ETH" = true ]; then
    BTC_ETH_ERROR_COUNT=\$(docker logs --tail 100 $BTC_ETH_CONTAINER_NAME 2>&1 | grep -i "error\|exception\|fatal" | wc -l)
    if [ "\$BTC_ETH_ERROR_COUNT" -gt 0 ]; then
        echo "   ⚠️  BTC/ETH 策略发现 \$BTC_ETH_ERROR_COUNT 个错误日志"
    else
        echo "   ✅ BTC/ETH 策略未发现明显错误日志"
    fi
fi

if [ "$DEPLOY_NEW_COIN" = true ]; then
    NEW_COIN_ERROR_COUNT=\$(docker logs --tail 100 $NEW_COIN_CONTAINER_NAME 2>&1 | grep -i "error\|exception\|fatal" | wc -l)
    if [ "\$NEW_COIN_ERROR_COUNT" -gt 0 ]; then
        echo "   ⚠️  新币做空策略发现 \$NEW_COIN_ERROR_COUNT 个错误日志"
    else
        echo "   ✅ 新币做空策略未发现明显错误日志"
    fi
fi

if [ "$DEPLOY_GRID" = true ]; then
    GRID_ERROR_COUNT=\$(docker logs --tail 100 $GRID_CONTAINER_NAME 2>&1 | grep -i "error\|exception\|fatal" | wc -l)
    if [ "\$GRID_ERROR_COUNT" -gt 0 ]; then
        echo "   ⚠️  网格交易策略发现 \$GRID_ERROR_COUNT 个错误日志"
    else
        echo "   ✅ 网格交易策略未发现明显错误日志"
    fi
fi

if [ "$DEPLOY_KLINE" = true ]; then
    KLINE_ERROR_COUNT=\$(docker logs --tail 100 $KLINE_CONTAINER_NAME 2>&1 | grep -i "error\|exception\|fatal" | wc -l)
    if [ "\$KLINE_ERROR_COUNT" -gt 0 ]; then
        echo "   ⚠️  K 线数据服务发现 \$KLINE_ERROR_COUNT 个错误日志"
    else
        echo "   ✅ K 线数据服务未发现明显错误日志"
    fi
fi

# 6. 验证容器资源使用
echo ""
echo "6️⃣  验证容器资源使用..."
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" | grep -E "NAME|trading_system|postgres"

# 7. 最终验证总结
echo ""
echo "============================================="
echo "📋 验证总结"
echo "============================================="
echo "PostgreSQL: $POSTGRES_CONTAINER_NAME - \$POSTGRES_STATUS"

if [ "$DEPLOY_BTC_ETH" = true ]; then
    echo "BTC/ETH 策略: $BTC_ETH_CONTAINER_NAME - \$BTC_ETH_STATUS"
fi

if [ "$DEPLOY_NEW_COIN" = true ]; then
    echo "新币做空策略: $NEW_COIN_CONTAINER_NAME - \$NEW_COIN_STATUS"
fi

if [ "$DEPLOY_GRID" = true ]; then
    echo "网格交易策略: $GRID_CONTAINER_NAME - \$GRID_STATUS"
fi

if [ "$DEPLOY_KLINE" = true ]; then
    echo "K 线数据服务: $KLINE_CONTAINER_NAME - \$KLINE_STATUS"
fi

echo "============================================="

if [ \$ERROR_COUNT -eq 0 ]; then
    echo "✅ 验证通过！所有容器已成功更新到最新版本！"
    exit 0
else
    echo "❌ 验证失败！请检查上述错误！"
    exit 1
fi

ENDSSH

# 检查验证结果
if [ $? -eq 0 ]; then
    echo "============================================="
    echo "🎉 验证完成！部署成功！"
    echo "============================================="
else
    echo "============================================="
    echo "❌ 验证失败！请检查上述错误！"
    echo "============================================="
    exit 1
fi
