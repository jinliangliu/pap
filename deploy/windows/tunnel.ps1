# deploy/windows/tunnel.ps1
#
# 在本机（Windows）建立到远程服务器的 SSH 隧道，让你能用浏览器访问平台。
#
# 方向说明（最容易搞混的一点）：
#   -L 8080:127.0.0.1:80
#   左边 8080   = 本机（Windows）监听端口，浏览器访问它；
#   右边 80     = 由 ssh 在【服务器上】连过去的端口，所以 127.0.0.1 是服务器的回环。
#   也就是：ssh 客户端跑在本机，转发目标在服务器视角解析。
#
# 必须转发 80 而不是 8000：8000 是应用本身，AUTH_MODE=gateway 不信任客户端
# 身份头，直连必然 401；X-Authenticated-Subject 只由 Nginx 注入。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tunnel.ps1            # 建立
#   powershell -ExecutionPolicy Bypass -File tunnel.ps1 -Action stop
#   powershell -ExecutionPolicy Bypass -File tunnel.ps1 -Action status
# 或直接双击同目录的 tunnel.cmd。

param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start',

    [int]$LocalPort = 8080,

    [string]$SshHost = 'pap'
)

$ErrorActionPreference = 'Stop'

$PidFile = Join-Path $env:TEMP 'pap-tunnel.pid'
$ConsoleUrl = "http://127.0.0.1:$LocalPort/console/"
$HealthUrl = "http://127.0.0.1:$LocalPort/health"

function Test-Listening {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$conn
}

function Get-HealthCode {
    # 用 curl.exe 而不是 Invoke-WebRequest：后者会走系统代理，
    # 访问 127.0.0.1 时反而可能失败。
    $code = & curl.exe -s -m 3 --noproxy '*' -o NUL -w '%{http_code}' $HealthUrl 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $code) { return '000' }
    return $code
}

function Start-Tunnel {
    if (Test-Listening -Port $LocalPort) {
        $code = Get-HealthCode
        Write-Host "[i] Port $LocalPort is already listening (health=$code)."
        Write-Host "[i] Open: $ConsoleUrl"
        return
    }

    $sshArgs = @(
        '-N',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=4',
        '-o', 'TCPKeepAlive=yes',
        '-L', "${LocalPort}:127.0.0.1:80",
        $SshHost
    )

    Write-Host "[*] Starting tunnel: ssh $($sshArgs -join ' ')"

    # 这里不用 Start-Process：它会重建一份环境变量字典，而这台机器上同时存在
    # HTTPS_PROXY 与 https_proxy（Windows 不区分大小写），字典插入直接抛
    # "Item has already been added. Key in dictionary: 'HTTPS_PROXY'"。
    # ProcessStartInfo + UseShellExecute=false 让子进程直接继承环境，不重建字典。
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'ssh'
    $psi.Arguments = ($sshArgs -join ' ')
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = $env:TEMP

    $proc = [System.Diagnostics.Process]::Start($psi)
    Set-Content -Path $PidFile -Value $proc.Id -Encoding ascii

    # 给 ssh 一点时间完成端口绑定与目标连通
    $code = '000'
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Milliseconds 500
        $code = Get-HealthCode
        if ($code -eq '200') { break }
        $proc.Refresh()
        if ($proc.HasExited) { break }
    }

    if ($code -eq '200') {
        Write-Host "[+] Tunnel is up. ssh pid=$($proc.Id)"
        Write-Host "[+] Open: $ConsoleUrl"
        Write-Host "    Login with the htpasswd account (pap-admin) and its nginx password."
        Write-Host "    Stop with: tunnel.cmd stop"
    }
    else {
        $proc.Refresh()
        Write-Host "[-] Tunnel did not become ready (health=$code)."
        if ($proc.HasExited) {
            Write-Host "[-] ssh already exited, code=$($proc.ExitCode)."
            Write-Host "[-] Reproduce it in a terminal to see the error:"
            Write-Host "    ssh $($sshArgs -join ' ')"
        }
        Write-Host "[-] Checklist: ssh alias '$SshHost' works? port $LocalPort free? server nginx up?"
    }
}

function Stop-Tunnel {
    $stopped = $false

    if (Test-Path $PidFile) {
        $tunnelPid = Get-Content $PidFile | Select-Object -First 1
        if ($tunnelPid) {
            $proc = Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -eq 'ssh') {
                Stop-Process -Id $tunnelPid -Force
                Write-Host "[+] Stopped ssh pid=$tunnelPid"
                $stopped = $true
            }
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }

    # 兜底：pid 文件丢失时按监听端口找进程
    if (-not $stopped) {
        $conn = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($conn) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -eq 'ssh') {
                Stop-Process -Id $proc.Id -Force
                Write-Host "[+] Stopped ssh pid=$($proc.Id) listening on $LocalPort"
                $stopped = $true
            }
            else {
                Write-Host "[-] Port $LocalPort is held by '$($proc.ProcessName)', not ssh. Left untouched."
            }
        }
    }

    if (-not $stopped) {
        Write-Host "[i] No tunnel to stop."
    }
}

function Show-Status {
    $listening = Test-Listening -Port $LocalPort
    $code = Get-HealthCode
    Write-Host "port $LocalPort listening : $listening"
    Write-Host "health via tunnel         : $code"

    if (Test-Path $PidFile) {
        $tunnelPid = Get-Content $PidFile | Select-Object -First 1
        $alive = [bool](Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue)
        Write-Host "recorded ssh pid          : $tunnelPid (alive=$alive)"
    }
    if ($listening) {
        Write-Host "url                       : $ConsoleUrl"
    }
}

switch ($Action) {
    'start' { Start-Tunnel }
    'stop' { Stop-Tunnel }
    'status' { Show-Status }
}
