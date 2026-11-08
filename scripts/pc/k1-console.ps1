<#
.SYNOPSIS
    K1 / MUSE Pi Pro 串口控制台（Windows，免装 MobaXterm）

.DESCRIPTION
    通过 USB-TTL(CH340) 串口直接进 K1 的 Linux 终端。
    默认 COM6 / 115200 / 8N1；本机 2026-11-08 的 CH340 在 COM6。MobaXterm 已占用时先关掉它的 Serial 会话。

    操作：
      - 直接敲键盘就是板子的键盘；回车=回车，退格/方向键/Tab 均可用
      - Ctrl+]  退出（不会关板子，只是断开串口）
      - 板子重启时先运行本脚本，能看到完整 boot log

.PARAMETER Port
    串口号，默认 COM6。可先用 -ListPorts 看当前有哪些。

.PARAMETER AutoLogin
    自动登录：识别到 "login:" / "用户名：" 自动填用户名，识别到 "密码" 自动填密码。
    密码不写进脚本：不传 -Password 时会在本机安全提示输入一次（只在内存里）。

.PARAMETER SelfTest
    非交互自检：只连 3 秒把板子输出打印出来就退出。用来确认串口是否真的通。

.EXAMPLE
    .\k1-console.ps1
    直接进串口终端，手动输用户名 bianbu 和密码（新板默认用户；biandu 用于 SSH/恢复）。

.EXAMPLE
    .\k1-console.ps1 -AutoLogin -User bianbu
    自动登录后进入 shell（会提示输入密码）。

.EXAMPLE
    .\k1-console.ps1 -ListPorts
    列出本机串口。

.NOTES
    板端事实（2026-11-08 新板实测）：
      hostname : k1
      用户名   : bianbu（默认，串口登录/密码）；biandu（为恢复创建，SSH 用）
      系统     : Bianbu 2.3.5, kernel 6.6.63, riscv64
      SSH      : sshd 在听 22；新板当前 IP 172.20.10.2（iPhone 热点 DHCP）
                 用 biandu@172.20.10.2 + ~/.ssh/id_k1_agent 登录
#>
[CmdletBinding()]
param(
    [string]$Port = 'COM6',
    [int]$Baud = 115200,
    [switch]$AutoLogin,
    [string]$User = 'bianbu',
    [string]$Password,
    [switch]$SelfTest,
    [int]$SelfTestSeconds = 3,
    [switch]$ListPorts
)

$ErrorActionPreference = 'Stop'

function Write-Info([string]$msg) { Write-Host "[k1] $msg" -ForegroundColor DarkCyan }
function Write-Warn2([string]$msg) { Write-Host "[k1] $msg" -ForegroundColor Yellow }

if ($ListPorts) {
    $names = [System.IO.Ports.SerialPort]::GetPortNames()
    if (-not $names) { Write-Warn2 '没有检测到任何串口。检查 USB-TTL 是否插好、驱动(CH340)是否装好。' }
    else { $names | ForEach-Object { "  $_" } }
    return
}

$sp = New-Object System.IO.Ports.SerialPort $Port, $Baud, 'None', 8, 'One'
$sp.Encoding = [System.Text.Encoding]::UTF8
$sp.ReadTimeout = 100
$sp.WriteTimeout = 1000
# 不拉 DTR/RTS：避免某些 CH340 接线把板子按住复位
$sp.DtrEnable = $false
$sp.RtsEnable = $false

try {
    $sp.Open()
} catch {
    Write-Warn2 "打不开 $Port ：$($_.Exception.Message)"
    $names = [System.IO.Ports.SerialPort]::GetPortNames()
    if ($names) { Write-Warn2 "当前可用串口：$($names -join ', ')" }
    Write-Warn2 '被占用的话先关掉 MobaXterm / PuTTY / 另一个本脚本窗口。'
    exit 1
}

Write-Info "已连接 $Port @ $Baud   (Ctrl+] 退出)"
Write-Host ''

# ---------- 自检模式 ----------
if ($SelfTest) {
    $sp.Write("`r"); Start-Sleep -Milliseconds 200   # 敲一下回车，让板子把提示符吐出来，证明是双向通
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        while ($sw.Elapsed.TotalSeconds -lt $SelfTestSeconds) {
            if ($sp.BytesToRead -gt 0) { Write-Host $sp.ReadExisting() -NoNewline }
            Start-Sleep -Milliseconds 50
        }
    } finally {
        $sp.Close(); $sp.Dispose()
    }
    Write-Host ''
    Write-Info '自检结束。上面如果看到 login: 之类的提示，说明串口通。'
    return
}

# ---------- 自动登录用的密码 ----------
if ($AutoLogin -and -not $Password) {
    $sec = Read-Host -AsSecureString "请输入 $User 的密码（只在本进程内存里）"
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { $Password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

$buf = New-Object System.Text.StringBuilder
$sentUser = $false
$sentPass = $false
$quit = $false

if ($AutoLogin) {
    # 先敲一下回车把 login: 提示符"钓"出来——串口在关闭期间板子输出的内容会丢，
    # 不主动触发的话状态机可能一直等不到提示符。
    $sp.Write("`r")
    Start-Sleep -Milliseconds 400
}

function Send-Text([string]$s) {
    $sp.Write($s)
    Start-Sleep -Milliseconds 60
}

try {
    while (-not $quit) {

        # 1) 板子 -> PC
        if ($sp.IsOpen -and $sp.BytesToRead -gt 0) {
            $chunk = $sp.ReadExisting()
            Write-Host $chunk -NoNewline
            [void]$buf.Append($chunk)
            if ($buf.Length -gt 4096) { [void]$buf.Remove(0, $buf.Length - 2048) }
        }

        # 2) 自动登录状态机
        if ($AutoLogin -and -not $sentPass) {
            $tail = $buf.ToString()
            if (-not $sentUser -and $tail -match '(login:|用户名：)\s*$') {
                Send-Text "$User`r"
                $sentUser = $true
                Start-Sleep -Milliseconds 700
            }
            elseif ($sentUser -and $tail -match '(密码|Password)\s*[:：]?\s*$') {
                Send-Text "$Password`r"
                $sentPass = $true
                Start-Sleep -Milliseconds 1500
            }
        }

        # 3) PC -> 板子
        $hasKey = $false
        try { $hasKey = [Console]::KeyAvailable } catch { $hasKey = $false }

        if ($hasKey) {
            $k = [Console]::ReadKey($true)
            switch ($k.Key) {
                'Enter'      { Send-Text "`r"; continue }
                'Backspace'  { Send-Text ([string][char]8); continue }
                'Tab'        { Send-Text "`t"; continue }
                'Escape'     { Send-Text ([string][char]27); continue }
                'UpArrow'    { Send-Text "`e[A"; continue }
                'DownArrow'  { Send-Text "`e[B"; continue }
                'RightArrow' { Send-Text "`e[C"; continue }
                'LeftArrow'  { Send-Text "`e[D"; continue }
                'Home'       { Send-Text "`e[H"; continue }
                'End'        { Send-Text "`e[F"; continue }
                'Delete'     { Send-Text "`e[3~"; continue }
            }
            $c = $k.KeyChar
            if ([int]$c -eq 29) { $quit = $true; continue }   # Ctrl+]
            if ([int]$c -ne 0) { Send-Text ([string]$c) }
        }

        Start-Sleep -Milliseconds 15
    }
}
finally {
    if ($sp.IsOpen) { $sp.Close() }
    $sp.Dispose()
    Write-Host ''
    Write-Info "已断开 $Port（板子仍在运行）"
}
