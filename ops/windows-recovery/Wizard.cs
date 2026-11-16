using System;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text.RegularExpressions;
using System.Windows.Forms;

internal sealed class Wizard : Form
{
    private readonly TextBox address = new TextBox { Text = "192.168.1.10", Width = 130 };
    private readonly TextBox login = new TextBox { Width = 145 };
    private readonly Label heading = new Label { AutoSize = true, Font = new Font("Segoe UI", 16, FontStyle.Bold) };
    private readonly TextBox instructions = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill };
    private readonly TextBox command = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill, Font = new Font("Consolas", 10) };
    private readonly Button back = new Button { Text = "Назад", AutoSize = true };
    private readonly Button next = new Button { Text = "Далее", AutoSize = true };
    private readonly Button copy = new Button { Text = "Копировать команду", AutoSize = true };
    private readonly Button export = new Button { Text = "Извлечь комплект…", AutoSize = true };
    private readonly Button chooseArchive = new Button { Text = "Выбрать архив Pi…", AutoSize = true };
    private readonly Button cleanInstall = new Button { Text = "У меня нет архива", AutoSize = true };
    private string bundlePath;
    private string archivePath;
    private bool modeSelected;
    private int page = -1;

    [STAThread]
    private static void Main(string[] args)
    {
        if (args.Length == 2 && args[0] == "--export-test")
        {
            Extract(args[1]);
            return;
        }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new Wizard());
    }

    private Wizard()
    {
        Text = "NetWatch — установка на Raspberry Pi";
        ClientSize = new Size(760, 435);
        MinimumSize = new Size(630, 410);
        StartPosition = FormStartPosition.CenterScreen;
        Font = new Font("Segoe UI", 10);
        var grid = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(16), ColumnCount = 1, RowCount = 5 };
        grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        grid.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        grid.RowStyles.Add(new RowStyle(SizeType.Absolute, 92));
        grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        var fields = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
        fields.Controls.Add(new Label { Text = "IPv4 Pi", AutoSize = true });
        fields.Controls.Add(address);
        fields.Controls.Add(new Label { Text = "Пользователь SSH", AutoSize = true });
        fields.Controls.Add(login);
        var buttons = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true };
        buttons.Controls.AddRange(new Control[] { back, next, chooseArchive, cleanInstall, export, copy });
        grid.Controls.Add(heading, 0, 0);
        grid.Controls.Add(fields, 0, 1);
        grid.Controls.Add(instructions, 0, 2);
        grid.Controls.Add(command, 0, 3);
        grid.Controls.Add(buttons, 0, 4);
        Controls.Add(grid);
        back.Click += delegate { page--; ShowPage(); };
        next.Click += delegate { if (page == 5) Close(); else { page++; ShowPage(); } };
        address.TextChanged += delegate { ShowPage(); };
        login.TextChanged += delegate { ShowPage(); };
        copy.Click += delegate { if (command.TextLength > 0) Clipboard.SetText(command.Text); };
        export.Click += delegate { Export(); };
        chooseArchive.Click += delegate { ChooseArchive(); };
        cleanInstall.Click += delegate { archivePath = null; modeSelected = true; ShowPage(); };
        ShowPage();
    }

    private static void Extract(string path)
    {
        using (Stream resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("NetWatch.Payload"))
        using (var zip = new ZipArchive(resource, ZipArchiveMode.Read))
            zip.ExtractToDirectory(path);
    }

    private void Export()
    {
        using (var dialog = new FolderBrowserDialog { Description = "Куда извлечь комплект для Raspberry Pi?" })
        {
            if (dialog.ShowDialog(this) != DialogResult.OK) return;
            try
            {
                string folder = "netwatch-pi-" + Guid.NewGuid().ToString("N").Substring(0, 8);
                string path = Path.Combine(dialog.SelectedPath, folder);
                Extract(path);
                bundlePath = path;
                ShowPage();
            }
            catch (Exception error)
            {
                MessageBox.Show(this, error.Message, "Ошибка извлечения", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
    }

    private void ChooseArchive()
    {
        using (var dialog = new OpenFileDialog {
            Title = "Выберите резервную копию NetWatch с Raspberry Pi",
            Filter = "Архив NetWatch (*.tar.gz)|*.tar.gz",
            CheckFileExists = true,
        })
        {
            if (dialog.ShowDialog(this) != DialogResult.OK) return;
            archivePath = dialog.FileName;
            modeSelected = true;
            ShowPage();
        }
    }

    private static string QuotePowerShell(string value)
    {
        return "'" + value.Replace("'", "''") + "'";
    }

    private void ShowPage()
    {
        IPAddress ip;
        string host = address.Text.Trim();
        string user = login.Text.Trim();
        bool valid = IPAddress.TryParse(host, out ip) && ip.AddressFamily == AddressFamily.InterNetwork
            && ip.ToString() == host && Regex.IsMatch(user, "^[a-z_][a-z0-9_-]{0,31}$");
        string target = user + "@" + host;
        string remoteFolder = bundlePath == null ? "" : Path.GetFileName(bundlePath);
        bool fullRestore = archivePath != null;
        string privateFolder = remoteFolder + "/private";
        string remote = remoteFolder + "/fresh_install.py";
        if (fullRestore) remote += " --archive " + privateFolder + "/backup.tar.gz";
        string step = "";
        string title;
        string body;
        switch (page)
        {
            case -1:
                title = "Выберите режим восстановления";
                body = "Если у вас есть архив NetWatch с Raspberry Pi (*.tar.gz), выберите его на компьютере или флешке. "
                     + "Он восстановит пользователей, настройки, историю и привязки роутеров на дату бэкапа.\r\n\r\n"
                     + "Если архива нет, мастер установит NetWatch, веб-монитор и пустую базу из встроенного комплекта.\r\n\r\n";
                if (modeSelected)
                    body += fullRestore ? "Выбран архив: " + Path.GetFileName(archivePath) : "Выбрана чистая установка без пользовательских данных.";
                break;
            case 0:
                title = "Подготовьте загрузочный носитель";
                body = "Установите Raspberry Pi OS 64-bit на базе Debian 13 (Trixie) через Raspberry Pi Imager. "
                     + "Задайте пользователя и включите SSH. Вставьте носитель в Pi, подключите Ethernet и питание.\r\n\r\n"
                     + (fullRestore ? "Сохраняйте выбранный архив доступным на Windows до окончания передачи."
                         : "Токен бота и пароль монитора вводятся позже на Pi.");
                break;
            case 1:
                title = "Проверьте адрес Pi и SSH";
                body = "На TR3000 проверьте DHCP-резервацию по MAC Ethernet Pi. Если меняется только загрузочный носитель, прежняя резервация обычно выдаёт тот же адрес. "
                     + "При смене адреса обновите адрес Pi в локальном DNS и пробросах TCP 80/443.\r\n\r\n"
                     + "Введите адрес и пользователя SSH сверху. Если это замена действующей Pi, выключите старую систему. Команду ниже выполните в PowerShell. "
                     + "При изменении ключа SSH после переустановки сначала сверьте новый отпечаток на самой Pi.";
                if (valid) step = "ssh " + target + " 'hostname -I; uname -m; python3 --version'";
                break;
            case 2:
                title = "Извлеките комплект и перенесите его";
                body = "Нажмите «Извлечь комплект…» и выберите локальную папку. Затем выполните команду в PowerShell. "
                     + "Для передачи нужен клиент OpenSSH (scp) в Windows и доступ по SSH к Pi.";
                if (bundlePath != null)
                {
                    body += "\r\n\r\nИзвлечено в " + bundlePath;
                    if (valid)
                    {
                        step = "scp -r " + QuotePowerShell(bundlePath) + " " + target + ":";
                        if (fullRestore)
                        {
                            body += "\r\nАрхив будет передан по SSH в закрытый каталог на Pi.";
                            step += "; if ($LASTEXITCODE -ne 0) { throw 'Не удалось передать комплект' }; "
                                + "ssh " + target + " 'install -d -m 0700 " + privateFolder + "'"
                                + "; if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить каталог' }; "
                                + "scp " + QuotePowerShell(archivePath) + " " + target + ":" + privateFolder + "/backup.tar.gz";
                        }
                    }
                }
                break;
            case 3:
                title = "Проверьте архив на Pi";
                body = "Выполните команду в PowerShell. На Pi будет запущена проверка архива, включая SHA-256 и SQLite. "
                     + "Переходите дальше после сообщения об успешной проверке.";
                if (valid && remoteFolder != "") step = "ssh " + target + " 'python3 -IB " + remote + "'";
                break;
            case 4:
                title = "Установите NetWatch";
                body = "Выполните команду в PowerShell. "
                     + (fullRestore ? "Будут восстановлены базы, токен бота и настройки HTTPS из выбранного архива.\r\n\r\n"
                         : "В SSH-сеансе укажите публичный DNS-домен, токен из BotFather, при необходимости Telegram ID администратора "
                         + "и новый пароль монитора. Токен и пароль вводятся без отображения.\r\n\r\n")
                     + "Для получения HTTPS-сертификата домен должен вести на ваш шлюз, а TCP 80/443 — на Pi. "
                     + "Установка проверяет, что целевая система чистая, и требует доступа в интернет.";
                if (valid && remoteFolder != "") step = "ssh -t " + target + " 'sudo python3 -IB " + remote + " --apply'";
                break;
            default:
                title = "Проверьте сервисы";
                body = "Выполните проверку служб в PowerShell. Затем откройте /monitor/ и войдите. "
                     + (fullRestore ? "В Telegram проверьте /routers и свежие данные через /health. Действующие привязки вернутся из архива.\r\n\r\n"
                         : "В Telegram вызовите /start, заново привяжите роутеры через /add_router и проверьте /health. "
                         + "Для роутера со старой привязкой может понадобиться --re-pair.\r\n\r\n")
                     + "Для ежедневного бэкапа отдельно настройте монтирование USB, проверьте ручной запуск netwatch-backup.service и включите netwatch-backup.timer.";
                if (valid) step = "ssh " + target + " 'systemctl is-active caddy netwatch-api netwatch-bot netwatch-monitor'";
                break;
        }
        heading.Text = (page + 2) + " / 7   " + title;
        instructions.Text = body;
        command.Text = step;
        copy.Enabled = step.Length != 0;
        export.Enabled = page == 2;
        chooseArchive.Visible = page == -1;
        cleanInstall.Visible = page == -1;
        back.Enabled = page > -1;
        next.Enabled = page != 1 || valid;
        if (page == -1) next.Enabled = modeSelected;
        if (page == 2) next.Enabled = bundlePath != null && valid;
        next.Text = page == 5 ? "Закрыть" : "Далее";
    }
}
