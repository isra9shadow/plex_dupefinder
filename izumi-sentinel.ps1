<#
.SYNOPSIS
  izumi sentinel — vigilante externo (dead-man's switch) para Windows 10/11.

.DESCRIPTION
  Pensado para un PC secundario SIEMPRE ENCENDIDO en la MISMA RED que el servidor
  Unraid. Sondea el servidor y sus servicios y te avisa por Telegram cuando algo
  cambia de estado — y, sobre todo, cuando el SERVIDOR ENTERO no responde (con lo
  que ni el bot que vive en el servidor podria avisarte).

  CERO instalacion: es PowerShell nativo de Windows (no necesita Python).
  CERO mover secretos a mano: en el primer arranque se conecta al Unraid por SSH
  (te pide IP + usuario root + contraseña UNA vez), trae el token de Telegram del
  .env del servidor y DESCUBRE solo los servicios (docker ps). Guarda esa config
  en %LOCALAPPDATA%\izumi-sentinel\config.json y ya no vuelve a pedir nada.

  Solo ENVIA mensajes de Telegram (no hace getUpdates), asi que reutiliza sin
  conflicto el mismo token del bot.

.PARAMETER Install
  Registra una Tarea Programada que arranca el centinela al iniciar sesion
  (en segundo plano) y bootstrapea si hace falta.

.PARAMETER Setup
  Fuerza re-configurar (vuelve a preguntar IP/credenciales y re-descubre servicios).

.EXAMPLE
  # Primera vez (configura + deja vigilando en esta ventana):
  powershell -ExecutionPolicy Bypass -File .\izumi-sentinel.ps1

.EXAMPLE
  # Configura y lo deja arrancando solo en cada inicio de sesion:
  powershell -ExecutionPolicy Bypass -File .\izumi-sentinel.ps1 -Install
#>

param(
    [switch]$Install,
    [switch]$Setup,
    [int]$IntervalSeconds = 60,
    [int]$Fails = 3,
    [int]$TimeoutMs = 8000
)

$ErrorActionPreference = 'Stop'
$ConfigDir = Join-Path $env:LOCALAPPDATA 'izumi-sentinel'
$ConfigPath = Join-Path $ConfigDir 'config.json'
$RemoteEnv = '/mnt/cache/appdata/scripts/plex_dupefinder/.env'

# --- helpers --------------------------------------------------------------------

function Read-EnvValue {
    param([string[]]$Lines, [string[]]$Keys)
    foreach ($key in $Keys) {
        foreach ($line in $Lines) {
            if ($line -match "^\s*$([regex]::Escape($key))\s*=\s*(.+?)\s*$") {
                return $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }
    return $null
}

function ConvertTo-Targets {
    # Parse `docker ps --format '{{.Names}}|{{.Ports}}'` lines into probe targets.
    param([string[]]$Lines, [string]$Host)
    $targets = @()
    foreach ($line in $Lines) {
        $parts = $line -split '\|', 2
        if ($parts.Count -lt 2) { continue }
        $name = $parts[0].Trim()
        # First published host port, e.g. "0.0.0.0:8989->8989/tcp" -> 8989
        $m = [regex]::Match($parts[1], ':(\d+)->')
        if (-not $m.Success) { continue }  # no published port -> not externally probeable
        $targets += [pscustomobject]@{ name = $name; host = $Host; port = [int]$m.Groups[1].Value }
    }
    return $targets
}

function Invoke-Bootstrap {
    Write-Host ''
    Write-Host '== Configuracion del centinela izumi ==' -ForegroundColor Cyan
    $unraid = Read-Host 'IP/host del servidor Unraid (ej. 192.168.6.62)'
    if (-not $unraid) { throw 'Necesito la IP del servidor.' }
    $ruser = Read-Host 'Usuario SSH de Unraid [enter = root]'
    if (-not $ruser) { $ruser = 'root' }

    Write-Host ''
    Write-Host 'Conectando por SSH para traer la config (te pedira la contraseña UNA vez)...' -ForegroundColor Yellow
    $remoteCmd = "cat $RemoteEnv; echo '---DOCKER---'; docker ps --format '{{.Names}}|{{.Ports}}'"
    $raw = & ssh -o StrictHostKeyChecking=accept-new "$ruser@$unraid" $remoteCmd
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        throw "No pude leer la config por SSH (revisa IP/usuario/contraseña y que SSH este activo en Unraid)."
    }

    $lines = @($raw)
    $split = [array]::IndexOf($lines, ($lines | Where-Object { $_ -match '^---DOCKER---' } | Select-Object -First 1))
    if ($split -lt 0) { $split = $lines.Count }
    $envLines = $lines[0..([math]::Max(0, $split - 1))]
    $dockerLines = if ($split + 1 -lt $lines.Count) { $lines[($split + 1)..($lines.Count - 1)] } else { @() }

    $token = Read-EnvValue -Lines $envLines -Keys @('IZUMI_TELEGRAM_BOT_TOKEN', 'TOKEN')
    $chat = Read-EnvValue -Lines $envLines -Keys @('IZUMI_TELEGRAM_CHAT_ID', 'CHAT_ID')
    if (-not $token -or -not $chat) {
        throw "No encontre IZUMI_TELEGRAM_BOT_TOKEN / IZUMI_TELEGRAM_CHAT_ID en $RemoteEnv del servidor."
    }

    $targets = ConvertTo-Targets -Lines $dockerLines -Host $unraid
    Write-Host ("Descubiertos {0} servicio(s) con puerto publicado." -f $targets.Count) -ForegroundColor Green

    $config = [pscustomobject]@{
        token    = $token
        chat_id  = $chat
        server   = [pscustomobject]@{ name = 'Servidor Unraid'; host = $unraid; port = 22 }
        targets  = $targets
        interval = $IntervalSeconds
        fails    = $Fails
        timeout  = $TimeoutMs
    }
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    $config | ConvertTo-Json -Depth 6 | Set-Content -Path $ConfigPath -Encoding UTF8
    Write-Host "Config guardada en $ConfigPath" -ForegroundColor Green
    return $config
}

function Test-Tcp {
    param([string]$TargetHost, [int]$Port, [int]$TimeoutMs)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            $client.EndConnect($iar)
            return $true
        }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Send-Telegram {
    param([string]$Token, [string]$ChatId, [string]$Text)
    $uri = "https://api.telegram.org/bot$Token/sendMessage"
    $body = @{ chat_id = $ChatId; text = $Text } | ConvertTo-Json
    try {
        Invoke-RestMethod -Method Post -Uri $uri -ContentType 'application/json' -Body $body | Out-Null
        return $true
    } catch {
        Write-Host "[sentinel] no pude enviar a Telegram: $_"
        return $false
    }
}

function Install-Task {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName 'izumi-sentinel' -Action $action -Trigger $trigger -Force | Out-Null
    Write-Host "Tarea programada 'izumi-sentinel' creada (arranca al iniciar sesion)." -ForegroundColor Green
}

# --- main -----------------------------------------------------------------------

if ($Setup -or -not (Test-Path $ConfigPath)) {
    $config = Invoke-Bootstrap
} else {
    $config = Get-Content -Path $ConfigPath -Raw | ConvertFrom-Json
}

if ($Install) {
    Install-Task
    Write-Host 'Listo. El centinela arrancara solo al iniciar sesion. Puedes cerrar esta ventana.'
    return
}

$names = @($config.server.name) + @($config.targets | ForEach-Object { $_.name })
Write-Host ("[sentinel] vigilando {0} objetivo(s): {1}" -f $names.Count, ($names -join ', ')) -ForegroundColor Cyan
Send-Telegram -Token $config.token -ChatId $config.chat_id `
    -Text ("[OK] izumi sentinel arrancado - vigilando: " + ($names -join ', ')) | Out-Null

$state = @{}
function Step-State {
    param($State, [string]$Name, [bool]$Ok, [int]$Threshold)
    if (-not $State.ContainsKey($Name)) { $State[$Name] = @{ fails = 0; down = $false } }
    $entry = $State[$Name]
    $event = $null
    if ($Ok) {
        if ($entry.down) { $event = 'up' }
        $entry.fails = 0
        $entry.down = $false
    } else {
        $entry.fails++
        if (-not $entry.down -and $entry.fails -ge $Threshold) {
            $entry.down = $true
            $event = 'down'
        }
    }
    return $event
}

while ($true) {
    try {
        # Server first: if the whole box is down, send ONE alarm and skip services.
        $srv = $config.server
        $srvOk = Test-Tcp -TargetHost $srv.host -Port $srv.port -TimeoutMs $config.timeout
        $srvEvent = Step-State -State $state -Name $srv.name -Ok $srvOk -Threshold $config.fails
        if ($srvEvent -eq 'down') {
            Send-Telegram -Token $config.token -ChatId $config.chat_id `
                -Text ("[ALERTA] {0} NO RESPONDE - el servidor (y por tanto el bot) parece caido. Revisa alimentacion/red." -f $srv.name) | Out-Null
        } elseif ($srvEvent -eq 'up') {
            Send-Telegram -Token $config.token -ChatId $config.chat_id `
                -Text ("[OK] Recuperado: {0}" -f $srv.name) | Out-Null
        }

        if (-not $state[$srv.name].down) {
            foreach ($t in $config.targets) {
                $ok = Test-Tcp -TargetHost $t.host -Port $t.port -TimeoutMs $config.timeout
                $event = Step-State -State $state -Name $t.name -Ok $ok -Threshold $config.fails
                if ($event -eq 'down') {
                    Send-Telegram -Token $config.token -ChatId $config.chat_id `
                        -Text ("[CAIDO] Servicio caido: {0}" -f $t.name) | Out-Null
                } elseif ($event -eq 'up') {
                    Send-Telegram -Token $config.token -ChatId $config.chat_id `
                        -Text ("[OK] Recuperado: {0}" -f $t.name) | Out-Null
                }
            }
        }
    } catch {
        Write-Host "[sentinel] error en la pasada: $_"
    }
    Start-Sleep -Seconds $config.interval
}
