#!/bin/sh
# NetWatch Agent v3 (OpenWrt)
# Только Wi-Fi подключения — проводные игнорируются

set -e

CONFIG_FILE="/etc/netwatch/config"
LOG_FILE="/mnt/usb/log/netwatch/netwatch-agent.log"

# Загрузка конфига
if [ -f "$CONFIG_FILE" ]; then
    . "$CONFIG_FILE"
else
    PI_URL="https://netwatchagent.duckdns.org"
    ROUTER_ID=""
    AGENT_SECRET=""
fi

log() {
    local ts=$(date '+%Y-%m-%d %H:%M:%S')
    local msg="[$ts] $1"
    echo "$msg"
    if [ -d "$(dirname "$LOG_FILE")" ] 2>/dev/null; then
        echo "$msg" >> "$LOG_FILE"
    fi
}

# JSON escape function for shell strings
json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\//\\\//g; s/\t/\\t/g; s/\n/\\n/g; s/\r/\\r/g'
}

# --- pair ---
cmd_pair() {
    log "Начинаем pair..."

    # Parse flags: --code <code> --url <url>
    local code=""
    local url=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --code) code="$2"; shift 2 ;;
            --url) url="$2"; shift 2 ;;
            *) shift ;;
        esac
    done

    if [ -z "$code" ] || [ -z "$url" ]; then
        echo "Использование: $0 pair --code <код> --url <url>"
        exit 1
    fi

    local router_id="rt_$(cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "rt_$(date +%s)_$(cat /proc/sys/kernel/random/uuid 2>/dev/null || echo 0)")"
    router_id="${router_id:0:13}"
    local mac=$(cat /sys/class/net/wlan0/address 2>/dev/null || cat /sys/class/net/eth0/address 2>/dev/null || echo "unknown")
    local model=$(cat /tmp/sysinfo/model 2>/dev/null || echo "OpenWrt Router")

    local json_payload='{"code":"'"$code"'","router_id":"'"$router_id"'","mac":"'"$mac"'","name":"'"$model"'","model":"'"$model"'"}'

    local response=$(curl -sS -4 -X POST "$url/pair" \
        -H "Content-Type: application/json" \
        -d "$json_payload")

    echo "Ответ Pi: $response"

    local agent_secret=$(echo "$response" | grep -o '"agent_secret"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*:[^"]*"\([^"]*\)"$/\1/')
    if [ -z "$agent_secret" ]; then
        echo "Ошибка: не удалось получить agent_secret"
        exit 1
    fi

    mkdir -p /etc/netwatch
    cat > "$CONFIG_FILE" << EOF
PI_URL="$url"
ROUTER_ID="$router_id"
AGENT_SECRET="$agent_secret"
EOF
    chmod 600 "$CONFIG_FILE"
    # Copy this script to /etc/netwatch/ for cron
    cp "$0" /etc/netwatch/agent3.sh
    chmod +x /etc/netwatch/agent3.sh

    # Авто-установка cron
    cmd_install_cron

    # Авто-запуск агента после pair
    log "Авто-запуск агента..."
    # Первый запуск сразу
    /etc/netwatch/agent3.sh run
    /etc/init.d/netwatch-wifi-monitor start 2>/dev/null || true
    log "Агент установлен и запущен (snapshot каждую минуту, health-check каждые 5 мин, wifi-monitor)"
}

# --- snapshot ---
cmd_snapshot() {
    local router_id
    router_id=$(get_router_id)
    if [ -z "$router_id" ]; then
        log "Router is not paired"
        return 1
    fi

    local tool
    for tool in iw iwinfo jq curl; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            log "Missing snapshot dependency: $tool"
            return 1
        fi
    done

    local wireless interfaces iface assoc macs mac ip hostname
    local devices_json='[]' payload
    wireless=$(iw dev) || return 1
    interfaces=$(printf '%s\n' "$wireless" | awk '$1 == "Interface" {print $2}')
    if [ -z "$interfaces" ]; then
        log "No Wi-Fi interfaces found"
        return 1
    fi

    for iface in $interfaces; do
        assoc=$(iwinfo "$iface" assoclist) || return 1
        macs=$(printf '%s\n' "$assoc" | awk '/dBm/ {print tolower($1)}')
        for mac in $macs; do
            ip='unknown'
            hostname='unknown'
            if [ -f /tmp/dhcp.leases ]; then
                ip=$(awk -v mac="$mac" 'tolower($2) == mac {print $3; exit}' /tmp/dhcp.leases)
                hostname=$(awk -v mac="$mac" 'tolower($2) == mac {print $4; exit}' /tmp/dhcp.leases)
            fi
            if [ -z "$ip" ] || [ "$ip" = 'unknown' ]; then
                ip=$(ip neigh 2>/dev/null | awk -v mac="$mac" 'tolower($5) == mac {print $1; exit}')
            fi
            [ -n "$ip" ] || ip='unknown'
            if [ -z "$hostname" ] || [ "$hostname" = '*' ]; then
                hostname='unknown'
            fi
            devices_json=$(printf '%s' "$devices_json" |
                jq -c --arg mac "$mac" --arg ip "$ip" --arg hostname "$hostname" \
                '. + [{mac:$mac, ip:$ip, hostname:$hostname}] | unique_by(.mac)') || return 1
        done
    done

    payload=$(jq -cn --arg router_id "$router_id" \
        --argjson devices "$devices_json" --argjson timestamp "$(date +%s)" \
        '{router_id:$router_id, devices:$devices, timestamp:$timestamp}') || return 1

    # Keep the secret out of process arguments; report HTTP/transport failures.
    printf 'X-Agent-Secret: %s\n' "$AGENT_SECRET" |
        curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
        --header @- --header 'Content-Type: application/json' \
        --data-binary "$payload" "${PI_URL%/}/devices"
}

# --- install-cron ---
cmd_install_cron() {
    log "Установка cron-задач для NetWatch Agent"

    local crontab_content=$(crontab -l 2>/dev/null || echo "")

    # Добавляем задачу snapshot раз в минуту
    local snapshot_cron="* * * * * $0 snapshot >> /mnt/usb/log/netwatch/snapshot.log 2>&1"
    if [[ "$crontab_content" != *"$0 snapshot"* ]]; then
        echo "$crontab_content" | crontab -
        echo "$snapshot_cron" | crontab -
        log "Добавлена cron-задача: $snapshot_cron"
    else
        log "Cron-задача уже существует, пропускаем"
    fi

    # Добавляем задачу run каждые 5 минут
    local run_cron="*/5 * * * * $0 run >> /mnt/usb/log/netwatch/run.log 2>&1"
    if [[ "$crontab_content" != *"$0 run"* ]]; then
        echo "$crontab_content" | crontab -
        echo "$run_cron" | crontab -
        log "Добавлена cron-задача: $run_cron"
    else
        log "Cron-задача run уже существует, пропускаем"
    fi

    log "Cron-задачи установлены"
}

# --- run ---
cmd_run() {
    local router_id=$(get_router_id)
    if [ -z "$router_id" ]; then
        log "Роутер не зарегистрирован. Выполните pair."
        exit 1
    fi

    local timestamp=$(date +%s)
    local internet=1
    local wifi_status=1
    local cpu=""
    local ram=""
    local wifi_clients=0
    local podkop_status="unknown"
    local podkop_test=null
    local podkop_latency_ms=null

    # Проверка интернета
    if ping -c 1 -W 3 8.8.8.8 > /dev/null 2>&1; then
        internet=1
    else
        internet=0
    fi

    # Radio health and clients: AP interfaces from iw, stations from iwinfo.
    # Do not report a successful zero-client sample if collection failed.
    local wireless iface_list iface assoc client_macs=''
    wireless=$(iw dev) || return 1
    iface_list=$(printf '%s\n' "$wireless" | awk '
        $1 == "Interface" { iface=$2 }
        $1 == "type" && $2 == "AP" { print iface }
    ')
    wifi_status=0
    for iface in $iface_list; do
        assoc=$(iwinfo "$iface" assoclist) || return 1
        wifi_status=1
        client_macs="$client_macs
$(printf '%s\n' "$assoc" | awk '/dBm/ {print tolower($1)}')"
    done
    wifi_clients=$(printf '%s\n' "$client_macs" | awk '
        NF && !seen[$1]++ { count++ }
        END { print count+0 }
    ')

    # Aggregate CPU busy percentage over one second. Guest counters are already
    # included in user/nice, so sum only user..steal (the first eight counters).
    local cpu_before cpu_after
    cpu_before=$(awk '/^cpu / {
        total=0; for (i=2; i<=9 && i<=NF; i++) total+=$i
        print total, $5+$6; exit
    }' /proc/stat) || return 1
    sleep 1
    cpu_after=$(awk '/^cpu / {
        total=0; for (i=2; i<=9 && i<=NF; i++) total+=$i
        print total, $5+$6; exit
    }' /proc/stat) || return 1
    cpu=$(awk -v before="$cpu_before" -v after="$cpu_after" 'BEGIN {
        if (split(before,b)!=2 || split(after,a)!=2) {print "n/a"; exit}
        total=a[1]-b[1]; idle=a[2]-b[2]
        if (total<=0 || idle<0 || idle>total) {print "n/a"; exit}
        printf "%.1f%%", 100*(total-idle)/total
    }')

    # /proc/meminfo uses KiB. Used memory excludes reclaimable cache via
    # MemAvailable; use a conservative fallback on kernels without that field.
    ram=$(awk '
        /^MemTotal:/ {total=$2}
        /^MemAvailable:/ {available=$2; have_available=1}
        /^MemFree:/ {free=$2}
        /^Buffers:/ {buffers=$2}
        /^Cached:/ {cached=$2}
        /^SReclaimable:/ {reclaimable=$2}
        /^Shmem:/ {shared=$2}
        END {
            if (total<=0) {print "n/a"; exit}
            if (!have_available) available=free+buffers+cached+reclaimable-shared
            if (available<0) available=0
            if (available>total) available=total
            printf "%.0fMiB/%.0fMiB", (total-available)/1024, total/1024
        }
    ' /proc/meminfo) || return 1


    # Podkop 0.7.x exposes sing-box runtime state separately from autostart.
    if command -v podkop > /dev/null 2>&1; then
        local podkop_json
        if podkop_json=$(podkop get_sing_box_status 2>/dev/null); then
            podkop_status=$(printf '%s' "$podkop_json" | jq -er '
                if .running == 1 or .running == true then "running"
                elif .running == 0 or .running == false then "stopped"
                else empty end
            ' 2>/dev/null) || podkop_status="unknown"
        fi
    else
        podkop_status="not_installed"
    fi

    # Follow the currently selected proxy, including nested Selector/URLTest.
    # CURL_HOME confines timeouts to these Podkop CLI calls; no global config
    # or proxy selection is modified. Old/unsupported commands stay unknown.
    if [ "$podkop_status" = running ]; then
        local probe_dir proxies proxy_tag encoded_tag latency_json measured
        if probe_dir=$(mktemp -d /tmp/netwatch-probe.XXXXXX); then
            if printf 'connect-timeout = 3\nmax-time = 8\n' > "$probe_dir/.curlrc"; then
                if proxies=$(CURL_HOME="$probe_dir" podkop clash_api get_proxies 2>/dev/null); then
                    proxy_tag=$(printf '%s' "$proxies" | jq -er --arg root "${PODKOP_GROUP:-main-out}" '
                        .proxies as $p |
                        def leaf($tag; $seen):
                            if ($seen | index($tag)) != null or ($seen | length) >= 8 then empty
                            elif ($p[$tag] | type) != "object" then empty
                            elif $p[$tag].type == "Selector" or $p[$tag].type == "URLTest" then
                                ($p[$tag].now // "") as $next |
                                if $next == "" then empty else leaf($next; $seen + [$tag]) end
                            elif ($p[$tag].type | type) != "string" then empty
                            elif ($p[$tag].type | ascii_downcase) as $t |
                                 ($t == "direct" or $t == "reject" or $t == "block" or $t == "dns") then empty
                            else $tag end;
                        leaf($root; [])
                    ' 2>/dev/null) || proxy_tag=''
                    if [ -n "$proxy_tag" ]; then
                        encoded_tag=$(printf '%s' "$proxy_tag" | jq -sRr @uri)
                        if latency_json=$(CURL_HOME="$probe_dir" podkop clash_api get_proxy_latency "$encoded_tag" 5000 2>/dev/null); then
                            if measured=$(printf '%s' "$latency_json" | jq -er '
                                .delay | select(type == "number" and . >= 0 and . <= 60000 and . == floor)
                            ' 2>/dev/null); then
                                podkop_latency_ms="$measured"
                                podkop_test=1
                            elif printf '%s' "$latency_json" | jq -e '
                                type == "object" and (has("message") or has("error"))
                            ' >/dev/null 2>&1; then
                                podkop_test=0
                            fi
                        fi
                    fi
                fi
            fi
            rm -f "$probe_dir/.curlrc"
            rmdir "$probe_dir" 2>/dev/null || true
        fi
    fi

    # Экранируем строковые значения для JSON
    local cpu_escaped=$(json_escape "$cpu")
    local ram_escaped=$(json_escape "$ram")
    local podkop_status_escaped=$(json_escape "$podkop_status")

    # Сбор устройств для /devices endpoint (Associated Stations via iwinfo + DHCP leases correlation)
    # iwinfo — универсальный интерфейс OpenWrt, работает со всеми драйверами (mac80211, Broadcom wl и т.д.)
    local devices_json="[]"
    local all_devices=""

    if command -v iwinfo > /dev/null 2>&1; then
        # Список всех беспроводных интерфейсов
        local iface_list=$(iwinfo 2>/dev/null | grep ESSID | cut -f 1 -s -d" ")
        
        for iface in $iface_list; do
            # assoclist показывает ТОЛЬКО ассоциированные (подключенные сейчас) станции с RSSI
            local mac_list=$(iwinfo "$iface" assoclist 2>/dev/null | grep dBm | cut -f 1 -s -d" ")
            
            for mac in $mac_list; do
                mac=$(echo "$mac" | tr '[:upper:]' '[:lower:]')
                
                # Корреляция с DHCP leases для получения IP и hostname
                local ip="unknown"
                local host="unknown"
                if [ -f /tmp/dhcp.leases ]; then
                    local lease=$(grep -i "$mac" /tmp/dhcp.leases 2>/dev/null | head -1)
                    if [ -n "$lease" ]; then
                        ip=$(echo "$lease" | cut -f 3 -s -d" ")
                        host=$(echo "$lease" | cut -f 4 -s -d" ")
                        [ -z "$ip" ] && ip="unknown"
                        [ -z "$host" ] && host="unknown"
                    fi
                fi

                # Fallback для репитеров без DHCP: пытаемся взять IP из ARP-таблицы
                if [ "$ip" = "unknown" ]; then
                    local arp_ip=""
                    arp_ip=$(arp -n 2>/dev/null | grep -i "$mac" | awk '{print $1}' | head -1)
                    [ -z "$arp_ip" ] && arp_ip=$(ip neigh 2>/dev/null | grep -i "$mac" | awk '{print $1}' | head -1)
                    [ -n "$arp_ip" ] && ip="$arp_ip"
                fi
                
                local dev_obj=$(printf '{"mac":"%s","ip":"%s","hostname":"%s"}' "$mac" "$ip" "$host")
                all_devices="${all_devices}${dev_obj},"
            done
        done
    fi

    # Убираем последнюю запятую и формируем JSON
    all_devices=$(echo "$all_devices" | sed 's/,$//')
    [ -n "$all_devices" ] && devices_json="[$all_devices]"

    # Отправка устройств на /devices
    curl -sS -4 -X POST "$PI_URL/devices" \
        -H "Content-Type: application/json" \
        -H "X-Agent-Secret: $AGENT_SECRET" \
        -d "{\"router_id\":\"$router_id\",\"devices\":$devices_json,\"timestamp\":$timestamp}" >/dev/null 2>&1
    echo ""

    # Отправка health-check на /health
    curl -sS -4 -X POST "$PI_URL/health" \
        -H "Content-Type: application/json" \
        -H "X-Agent-Secret: $AGENT_SECRET" \
        -d "{\"router_id\":\"$router_id\",\"internet\":$internet,\"wifi\":$wifi_status,\"cpu\":\"$cpu_escaped\",\"ram\":\"$ram_escaped\",\"wifi_clients\":$wifi_clients,\"podkop_status\":\"$podkop_status_escaped\",\"podkop_test\":$podkop_test,\"podkop_latency_ms\":$podkop_latency_ms,\"timestamp\":$timestamp}"
    echo ""
}

# --- wifi-monitor ---
cmd_wifi_monitor() {
    local router_id=$(get_router_id)
    if [ -z "$router_id" ]; then
        log "Роутер не зарегистрирован. Выполните pair."
        exit 1
    fi

    log "Запуск wifi-monitor (hostapd_cli)..."

    # Мониторинг событий Wi-Fi (только беспроводные подключения)
    hostapd_cli -i wlan0 -i wlan1 -a /usr/bin/netwatch-agent-hostapd.sh 2>/dev/null &

    # Пишем PID для возможности остановки
    echo $! > /var/run/netwatch-wifi-monitor.pid
    log "wifi-monitor запущен (PID: $(cat /var/run/netwatch-wifi-monitor.pid))"

    # Ждём
    wait
}

# --- uninstall ---
cmd_uninstall() {
    log "Удаление NetWatch Agent..."
    
    # Остановка сервисов
    /etc/init.d/netwatch-wifi-monitor stop 2>/dev/null || true
    kill $(cat /var/run/netwatch-wifi-monitor.pid 2>/dev/null) 2>/dev/null || true
    rm -f /var/run/netwatch-wifi-monitor.pid
    
    # Удаление cron
    crontab -l 2>/dev/null | grep -v agent3.sh | crontab -
    
    # Удаление конфига и логов
    rm -f /etc/netwatch/config
    rm -f /mnt/usb/log/netwatch/netwatch-agent.log
    rm -rf /etc/netwatch
    
    echo "Агент полностью удалён с роутера"
}

# --- get_router_id из файла ---
get_router_id() {
    if [ -f /etc/netwatch/router_id ]; then
        cat /etc/netwatch/router_id
    elif [ -f /etc/netwatch/config ] && grep -q '^ROUTER_ID=' /etc/netwatch/config; then
        grep '^ROUTER_ID=' /etc/netwatch/config | cut -d'=' -f2- | sed 's/^"//; s/"$//'
    fi
}

# --- Главное меню ---
case "$1" in
    pair)
        shift
        cmd_pair "$@"
        ;;
    run)
        cmd_run
        ;;
    snapshot)
        cmd_snapshot
        ;;
    install-cron)
        cmd_install_cron
        ;;
    wifi-monitor)
        cmd_wifi_monitor
        ;;
    health-check)
        cmd_run
        ;;
    uninstall)
        cmd_uninstall
        ;;
    *)
        echo "NetWatch Agent v3 (OpenWrt)"
        echo "Использование: $0 {pair|run|snapshot|install-cron|wifi-monitor|health-check|uninstall}"
        echo ""
        echo "  pair          -- Регистрация роутера на Pi + авто-установка агента"
        echo "  run           -- Health-check + heartbeat (cron) + отправка устройств"
        echo "  snapshot      -- Сбор данных устройств (Wi-Fi клиенты) (cron)"
        echo "  install-cron  -- Установка cron-задач (snapshot + run)"
        echo "  wifi-monitor  -- Мониторинг Wi-Fi в реальном времени (hostapd_cli)"
        echo "  health-check  -- Проверка здоровья роутера (интернет, Wi-Fi, CPU, RAM, клиенты, Podkop)"
        echo "  uninstall     -- Полное удаление агента с роутера"
        ;;
esac
