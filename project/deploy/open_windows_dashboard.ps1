# Windows entry point for the existing, account-specific DEV loopback connection.
[CmdletBinding()]
param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$baseUrl = 'http://127.0.0.1:8000/'
$starter = Join-Path $PSScriptRoot 'start_windows_loopback_tunnel.ps1'
$stopper = Join-Path $PSScriptRoot 'stop_windows_loopback_tunnel.ps1'
$gateway = Join-Path $PSScriptRoot 'local_gateway.py'
$shell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$mutex = $null
$locked = $false

function Get-WebJson([string]$Path, [switch]$Authorize) {
    $request = [System.Net.HttpWebRequest]::Create($baseUrl.TrimEnd('/') + $Path)
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = 2500
    $request.ReadWriteTimeout = 2500
    if ($Authorize) { $request.Headers.Add('X-NMS-Local-Auth', '1') }
    $response = $null
    $reader = $null
    try {
        $response = $request.GetResponse()
        if ($Authorize -and $response.Headers['X-NMS-Local-Gateway'] -ne '1') {
            throw '本机入口未提供自动授权标识。'
        }
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $body = $reader.ReadToEnd() | ConvertFrom-Json
        if ($body.status -ne 'ok') { throw '服务尚未就绪。' }
        return $body.data
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Close() }
    }
}

function Test-Ready {
    try {
        $health = Get-WebJson '/api/v1/health'
        $auth = Get-WebJson '/api/v1/auth/status' -Authorize
        return ($health.database -eq 'ok' -and $health.live_enabled -eq $true -and $auth.authenticated -eq $true)
    } catch { return $false }
}

function Get-ProjectGateway {
    $listeners = @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) { return $null }
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') {
        throw '端口 8000 已被其他监听占用；请关闭占用程序或检查原连接后重试。'
    }
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listeners[0].OwningProcess)"
    $command = if ($owner -and $owner.CommandLine) { $owner.CommandLine.Replace('\', '/') } else { '' }
    $expected = $gateway.Replace('\', '/')
    if ($owner.Name -notin @('python.exe','pythonw.exe') -or -not $command.Contains($expected) -or
        $command -notlike '*root@240a:a019::100*') {
        throw '端口 8000 被其他程序或旧式转发占用。请先关闭该连接，再双击启动器；未停止占用程序。'
    }
    return $owner
}

function Ensure-WarpProxy {
    $warp = Join-Path $env:ProgramFiles 'Cloudflare\Cloudflare WARP\warp-cli.exe'
    if (-not (Test-Path -LiteralPath $warp -PathType Leaf)) {
        throw '未找到已配置的 Cloudflare WARP。请先完成这台电脑的实验网代理配置。'
    }
    $settings = (& $warp settings 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { throw '无法读取 WARP 状态。请打开 Cloudflare WARP，检查其后台服务后重试。' }
    if ($settings -notmatch 'Mode:\s*WarpProxy on port 40000\b') {
        throw 'WARP 不是本组已验证的代理模式（WarpProxy，端口 40000）。请恢复原代理配置后重试。'
    }
    Write-Host '[2/4] 正在连接现有 WARP 代理…'
    & $warp connect 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'WARP 连接失败。请打开 Cloudflare WARP 查看原因，再重新运行启动器。' }
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $pending = $client.ConnectAsync('127.0.0.1', 40000)
            if ($pending.Wait(400) -and $client.Connected) { return }
        } catch { } finally { $client.Dispose() }
        Start-Sleep -Milliseconds 500
    }
    throw 'WARP 的本机代理端口 40000 尚未就绪。请检查 WARP 后重试。'
}

try {
    $mutex = New-Object System.Threading.Mutex($false, 'Local\NMS-Team2-Open-Dashboard-8000')
    try { $locked = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw '另一个启动器正在建立连接，请等原窗口提示就绪。' }
    Write-Host '第 2 组 · 真实实验网络管理网页'
    Write-Host '[1/4] 检查本机连接…'
    $owner = Get-ProjectGateway
    if ($owner -and (Test-Ready)) {
        Write-Host '现有连接及管理员授权正常，直接复用。'
    } else {
        foreach ($path in @($starter, $stopper, $gateway)) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                throw "缺少本机连接文件：$path。此入口需要维护者本机已配置的连接资料。"
            }
        }
        $python = (Get-Command python.exe -ErrorAction Stop).Source
        & $python $gateway check 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw '当前 Windows 账户无法读取本机加密凭据。请完成本机自动授权配置；实际令牌不写入启动器。'
        }
        Ensure-WarpProxy
        if ($owner) {
            Write-Host '已有项目连接未就绪，重新建立该连接…'
            & $shell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $stopper
            if ($LASTEXITCODE -ne 0) { throw '原项目连接未能关闭，请检查占用情况后重试。' }
        }
        if (Get-NetTCPConnection -LocalPort 18080 -State Listen -ErrorAction SilentlyContinue) {
            throw '内部转发端口 18080 已占用。请关闭原实验网转发后重试；未停止该端口上的程序。'
        }
        Write-Host '[3/4] 正在建立 DEV 通路并验证自动授权，请稍候…'
        & $shell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $starter -UseCoursePassword -Detached
        if ($LASTEXITCODE -ne 0) {
            throw '连接未能启动。请检查上方提示、WARP、课程登录资料和 DEV 可达性；重新双击可再次尝试。'
        }
        $verifiedOwner = Get-ProjectGateway
        if (-not $verifiedOwner -or -not (Test-Ready)) {
            throw '通路未通过网页和自动授权检查。若 DEV 管理令牌已更改，请更新本机加密凭据后重试。'
        }
    }
    Write-Host "[4/4] 网页已就绪：$baseUrl"
    if (-not $NoBrowser) { Start-Process $baseUrl }
    Write-Host '本机管理员已连接。可以关闭启动窗口，后台连接会继续运行。'
    exit 0
} catch {
    Write-Host "启动未完成：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host '排障说明：project\docs\使用说明.md → 一键打开真实网页'
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
