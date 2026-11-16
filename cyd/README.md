# Монитор Raspberry Pi на CYD

Скетч рассчитан на ESP32-2432S028 с TFT_eSPI, XPT2046_Touchscreen, ArduinoJson и LVGL 9. На проверенной плате использовался дисплей ILI9341. Для другого варианта платы подберите драйвер и пины TFT_eSPI.

1. Скопируйте каталог `NetWatchCYD_LVGL` локально. В рабочей копии задайте `WIFI_NAME`, `WIFI_PASSWORD` и `METRICS_URL`.
2. Настройте LVGL с приложенным `lv_conf.h` и включённым `LV_USE_TFT_ESPI`.
3. Откройте `NetWatchCYD_LVGL.ino` в Arduino IDE, выберите ESP32 Dev Module и порт своего дисплея. Соберите и загрузите.
4. На Pi добавьте LAN-маршрут по шаблону [Caddyfile.cyd.example](../monitor/Caddyfile.cyd.example). В `METRICS_URL` подставьте тот же LAN IP. Проверьте JSON с другого устройства в домашней сети.

Скетч показывает CPU, температуру, RAM, диски, Ethernet RX/TX и uptime. Темы, яркость и ночной режим сохраняются на устройстве.
