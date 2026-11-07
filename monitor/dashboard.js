"use strict";

const byId = (id) => document.getElementById(id);
const decimal = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
const percent = (value) => Number.isFinite(value) ? `${decimal.format(value)} %` : "—";

function bytes(value) {
  if (!Number.isFinite(value)) return "—";
  const units = ["Б", "КиБ", "МиБ", "ГиБ", "ТиБ"];
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${decimal.format(value)} ${units[unit]}`;
}

function uptime(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor(seconds / 3600) % 24;
  const minutes = Math.floor(seconds / 60) % 60;
  return days ? `${days} д ${hours} ч` : `${hours} ч ${minutes} мин`;
}

function rows(id, entries, empty) {
  const parent = byId(id);
  if (!entries.length) {
    const note = document.createElement("p");
    note.textContent = empty;
    parent.replaceChildren(note);
    return;
  }
  parent.replaceChildren(...entries.map(([label, value, status]) => {
    const line = document.createElement("div");
    line.className = "row";
    const left = document.createElement("span");
    const right = document.createElement("span");
    left.textContent = label;
    right.textContent = value;
    if (status) right.className = status;
    line.append(left, right);
    return line;
  }));
}

function progress(id, value) {
  if (Number.isFinite(value)) byId(id).value = value;
  else byId(id).removeAttribute("value");
}

function render(snapshot) {
  const { memory, services } = snapshot;
  const used = memory.total_bytes > 0 ? 100 * memory.used_bytes / memory.total_bytes : null;
  byId("cpu").textContent = percent(snapshot.cpu_percent);
  byId("ram").textContent = percent(used);
  progress("cpu-bar", snapshot.cpu_percent);
  progress("ram-bar", used);
  byId("ram-detail").textContent = `${bytes(memory.used_bytes)} из ${bytes(memory.total_bytes)}`;
  byId("temperature").textContent = Number.isFinite(snapshot.temperature_c)
    ? `${decimal.format(snapshot.temperature_c)} °C` : "—";
  byId("uptime").textContent = uptime(snapshot.uptime_seconds);
  rows("storage", snapshot.storage.map((disk) => [disk.mount, disk.available
    ? `Занято ${bytes(disk.used_bytes)} · свободно ${bytes(disk.available_bytes)} из ${bytes(disk.total_bytes)}`
    : "Недоступен"]), "Накопители не обнаружены");
  const speed = (value) => Number.isFinite(value) ? `${bytes(value)}/с` : "—";
  rows("network", snapshot.network.map((nic) => [nic.interface,
    `↓ ${speed(nic.rx_bytes_per_s)} · ↑ ${speed(nic.tx_bytes_per_s)}`]), "Интерфейсы не обнаружены");
  const states = { active: "Активна", inactive: "Остановлена", failed: "Ошибка",
    activating: "Запускается", deactivating: "Останавливается", unknown: "Нет данных" };
  rows("core", services.core.map((service) => [service.name,
    states[service.state] ?? service.state, service.state === "active" ? "good" : "warn"]), "Нет данных");
  byId("service-count").textContent = services.running ? `(${services.running.length})` : "";
  byId("services").replaceChildren(...(services.running ?? ["Список недоступен"]).map((name) => {
    const item = document.createElement("li");
    item.textContent = name;
    return item;
  }));
  rows("containers", snapshot.containers.map((item) => [`${item.runtime} · ${item.id}`, "Запущен", "good"]),
    "Запущенные контейнеры не обнаружены");
  byId("updated").textContent = `Обновлено в ${new Date(snapshot.sampled_at * 1000).toLocaleTimeString("ru-RU")}`;
}

async function refresh() {
  const indicator = byId("connection");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch("./api/v1/metrics", { signal: controller.signal, cache: "no-store" });
    if (!response.ok) throw new Error("Metrics unavailable");
    render(await response.json());
    indicator.textContent = "Есть связь";
    indicator.className = "badge good";
  } catch {
    indicator.textContent = "Нет свежих данных";
    indicator.className = "badge warn";
  } finally {
    clearTimeout(timeout);
    setTimeout(refresh, 5000);
  }
}

refresh();
