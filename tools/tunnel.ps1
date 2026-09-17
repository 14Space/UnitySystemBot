<#
    Постоянный обратный SOCKS-туннель «домашний ПК → сервер».

    Зачем: сервер стоит во Франции, и его дата-центровый адрес площадки не любят —
    YouTube отвечает бот-чеком, PornHub закрыт по стране, tikwm режет по IP. Туннель
    даёт боту выход в интернет через ДОМАШНИЙ адрес: на сервере поднимается SOCKS-порт,
    а сам прокси работает здесь, на этой машине.

    Как это устроено: ssh сам умеет быть SOCKS-прокси на стороне клиента (обратное
    динамическое перенаправление), поэтому никакой отдельной программы ставить не надо.
    Соединение идёт ИЗ дома НА сервер, значит проброс портов на домашнем роутере не
    нужен — серый IP и недоступная провайдерская коробка не мешают.

    Порт на сервере привязан к 172.18.0.1 (шлюз docker-сети) — оттуда его видит только
    контейнер бота. Снаружи он закрыт: в sshd стоит GatewayPorts clientspecified, плюс
    правило iptables отбрасывает 1080 на eth0. Открытый SOCKS в интернете означал бы,
    что кто угодно ходит по сети от вашего домашнего адреса.

    Запуск вручную:
        powershell -ExecutionPolicy Bypass -File tools\tunnel.ps1

    Автозапуск при входе в систему:
        powershell -ExecutionPolicy Bypass -File tools\tunnel.ps1 -Install

    Проверить, что туннель жив (выполнить на сервере):
        curl --proxy socks5h://172.18.0.1:1080 https://api.ipify.org
    Должен ответить ваш домашний адрес, а не адрес сервера.
#>
param(
    [switch]$Install,                     # поставить задание в планировщик и выйти
    [string]$ServerHost = "212.47.73.9",
    [string]$ServerUser = "root",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\contabo_key",
    [string]$BindAddress = "172.18.0.1",  # шлюз docker-сети на сервере
    [int]$Port = 1080,
    [int]$RetrySeconds = 15
)

$TaskName = "UnitySystemBot-tunnel"

if ($Install) {
    $me = $MyInvocation.MyCommand.Path
    $installed = $false

    # Сначала пробуем планировщик: у него есть перезапуск при сбое и работа на батарее.
    # Но в корневую папку заданий Windows пускает только с правами администратора,
    # поэтому провал здесь — обычное дело, а не поломка. Раньше скрипт в этом месте
    # печатал «поставлено» независимо от результата и врал.
    try {
        $action = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$me`""
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        # Задание не должно выключаться по таймауту и должно пережить переход на батарею:
        # ноутбук без розетки — обычное дело, а туннель нужен всё время.
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Minutes 1)
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Force -ErrorAction Stop | Out-Null
        $installed = $true
        Write-Host "Готово: задание планировщика '$TaskName' создано (запуск при входе в систему)."
        Write-Host "  запустить сейчас:  Start-ScheduledTask -TaskName $TaskName"
        Write-Host "  убрать:            Unregister-ScheduledTask -TaskName $TaskName"
    } catch {
        Write-Host "Планировщик отказал ($($_.Exception.Message.Trim())) — ставлю в автозагрузку."
    }

    # Запасной путь без прав администратора: ярлык в папке «Автозагрузка». Запускаем
    # через .vbs, иначе при каждом входе моргало бы чёрное окно консоли.
    if (-not $installed) {
        $startup = [Environment]::GetFolderPath("Startup")
        $vbs = Join-Path $startup "UnitySystemBot-tunnel.vbs"
        $cmd = "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File """ + $me + """"
        @(
            "' Поднимает SOCKS-туннель до сервера при входе в систему.",
            "' Создано tools	unnel.ps1 -Install. Чтобы отключить — удалите этот файл.",
            "CreateObject(""Wscript.Shell"").Run ""$cmd"", 0, False"
        ) | Set-Content -Path $vbs -Encoding ASCII
        if (Test-Path $vbs) {
            Write-Host "Готово: туннель добавлен в автозагрузку."
            Write-Host "  файл:     $vbs"
            Write-Host "  убрать:   удалить этот файл"
        } else {
            Write-Error "Не вышло ни через планировщик, ни через автозагрузку."
            exit 1
        }
    }
    return
}

if (-not (Test-Path $KeyPath)) { throw "Не нашёл ключ: $KeyPath" }

Write-Host "Туннель $ServerUser@$ServerHost -> ${BindAddress}:$Port. Ctrl+C чтобы остановить."

while ($true) {
    $started = Get-Date
    # ExitOnForwardFailure: если порт на сервере занять не вышло, ssh должен упасть,
    # а не висеть «подключённым» без работающего проброса — иначе бот молча остался бы
    # без прокси, а мы бы думали, что всё хорошо.
    # ServerAliveInterval/CountMax: рвём соединение через ~60с тишины, чтобы зависший
    # канал не выглядел живым (типично при засыпании ноутбука и смене сети).
    & ssh -i $KeyPath `
        -o BatchMode=yes `
        -o ExitOnForwardFailure=yes `
        -o ServerAliveInterval=20 `
        -o ServerAliveCountMax=3 `
        -o StrictHostKeyChecking=accept-new `
        -N -R "${BindAddress}:${Port}" "$ServerUser@$ServerHost"

    $lived = [int]((Get-Date) - $started).TotalSeconds
    Write-Host ("[{0}] туннель отвалился (продержался {1}с), переподключаюсь через {2}с" -f `
        (Get-Date -Format "HH:mm:ss"), $lived, $RetrySeconds)
    Start-Sleep -Seconds $RetrySeconds
}
